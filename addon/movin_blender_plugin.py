bl_info = {
    "name": "MOVIN Live Receiver",
    "author": "MOVIN",
    "version": (1, 0, 0),
    "blender": (4, 3, 2),
    "location": "View3D > N-Panel > MOVIN Live Receiver",
    "description": "Receives /MOVIN/Frame and /MOVIN/PointCloud OSC from Unity.",
    "category": "Animation",
}

import bpy
from bpy.props import (
    PointerProperty, StringProperty, IntProperty, BoolProperty, FloatProperty, EnumProperty
)
from bpy.types import (
    PropertyGroup, Panel, Operator
)
import socket
import struct
import threading
import time
import traceback
from collections import deque
from mathutils import Quaternion, Vector
import math

# -----------------------
# OSC Reader
# -----------------------

class _OscReader:
    """Tiny OSC reader for a single message packet.
    Supports address + typetags with 'i', 'f', 's'."""
    def __init__(self, data: bytes):
        self.data = data
        self.i = 0
        self.n = len(data)

    def _read_padded_string(self):
        start = self.i
        try:
            end = self.data.index(b'\x00', start)
        except ValueError:
            raise ValueError("OSC string not null-terminated")
        s = self.data[start:end].decode('utf-8', errors='replace')
        self.i = (end + 4) & ~0x03
        if self.i > self.n:
            raise ValueError("OSC string padding overflow")
        return s

    def _read_int32(self):
        if self.i + 4 > self.n:
            raise ValueError("OSC int32 truncated")
        val = struct.unpack(">i", self.data[self.i:self.i+4])[0]
        self.i += 4
        return val

    def _read_float32(self):
        if self.i + 4 > self.n:
            raise ValueError("OSC float32 truncated")
        val = struct.unpack(">f", self.data[self.i:self.i+4])[0]
        self.i += 4
        return val

    def read_message(self):
        address = self._read_padded_string()
        if not address:
            raise ValueError("Empty OSC address")
        if self.i >= self.n:
            return address, []
        typetags = self._read_padded_string()
        if not typetags.startswith(','):
            raise ValueError("OSC typetags missing ',' prefix")
        argspec = typetags[1:]
        args = []
        for t in argspec:
            if t == 'i':
                args.append(self._read_int32())
            elif t == 'f':
                args.append(self._read_float32())
            elif t == 's':
                args.append(self._read_padded_string())
            else:
                raise ValueError(f"Unsupported OSC arg type: {t}")
        return address, args

# -----------------------
# Runtime
# -----------------------

class MOVINRuntime:
    def __init__(self):
        self.thread = None
        self.sock = None
        self.running = False
        self.lock = threading.Lock()
        self.frame_buffers = {}
        self.pointcloud_buffers = {}
        self.ready_frames = deque(maxlen=4)
        self.ready_pointclouds = deque(maxlen=2)
        self.last_applied = None
        self.last_pointcloud_frame = None
        self.last_actor = ""
        self.last_ts = ""
        self.last_pointcloud_count = 0
        self.last_visualized_point_count = 0
        self.recv_count = 0
        self.last_rate_time = time.time()
        self.recv_rate_hz = 0.0
        self.warned_constraints = False  # NEW: one-time console warning

    def reset(self):
        with self.lock:
            self.frame_buffers.clear()
            self.pointcloud_buffers.clear()
            self.ready_frames.clear()
            self.ready_pointclouds.clear()
            self.last_applied = None
            self.last_pointcloud_frame = None
            self.last_actor = ""
            self.last_ts = ""
            self.last_pointcloud_count = 0
            self.last_visualized_point_count = 0
            self.recv_count = 0
            self.recv_rate_hz = 0.0
            self.warned_constraints = False

_runtime = MOVINRuntime()

# -----------------------
# Scene Properties
# -----------------------

class MOVIN_Props(PropertyGroup):
    armature_name: StringProperty(
        name="Armature",
        description="Armature object to drive",
        default=""
    )
    port: IntProperty(
        name="Port",
        default=11235,
        min=1, max=65535
    )
    hips_bone_name: StringProperty(
        name="Hips Bone",
        description="Bone that should receive the global translation/rotation when Root is not applied to object",
        default="Hips"
    )
    hips_translational_scale: FloatProperty(
        name="Hips Translational Scale",
        description="Scale the hips bone's translational movement",
        default=1.0
    )
    hips_y_offset: FloatProperty(
        name="Hips Y Offset",
        description="Offset the hips bone's Y position. Adjust according to your character's hips height.",
        default=-0.87
    )
    is_running: BoolProperty(
        name="Running",
        default=False
    )
    pointcloud_enabled: BoolProperty(
        name="Visualize Point Cloud",
        description="Receive /MOVIN/PointCloud and update a Blender point cloud object",
        default=True
    )
    pointcloud_object_name: StringProperty(
        name="Point Cloud Object",
        description="Object name used for the live point cloud visualization",
        default="MOVIN_PointCloud"
    )
# -----------------------
# OSC Server Thread
# -----------------------

def _udp_server_loop(port):
    global _runtime
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    except OSError:
        pass
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    _runtime.sock = sock

    PARTIAL_TTL_SEC = 0.5

    try:
        while _runtime.running:
            try:
                data, _addr = sock.recvfrom(65535)
            except socket.timeout:
                now = time.time()
                with _runtime.lock:
                    stale_frames = [k for k, v in _runtime.frame_buffers.items()
                                    if now - v.get("_t0", now) > PARTIAL_TTL_SEC]
                    stale_pointclouds = [k for k, v in _runtime.pointcloud_buffers.items()
                                         if now - v.get("_t0", now) > PARTIAL_TTL_SEC]
                    for k in stale_frames:
                        del _runtime.frame_buffers[k]
                    for k in stale_pointclouds:
                        del _runtime.pointcloud_buffers[k]
                continue
            except OSError:
                break

            try:
                reader = _OscReader(data)
                address, args = reader.read_message()
            except Exception as e:
                print("[MOVIN Live] OSC parse error:", e)
                continue

            now = time.time()

            if address == "/MOVIN/Frame":
                try:
                    ts = args[0]
                    actor_name = args[1]
                    frame_idx = int(args[2])
                    num_chunks = int(args[3])
                    chunk_idx = int(args[4])
                    total_bones = int(args[5])
                    chunk_bones = int(args[6])
                except Exception as e:
                    print("[MOVIN Live] Bad frame header args:", e)
                    continue

                k = 7
                bones_in_chunk = []
                try:
                    for _ in range(chunk_bones):
                        bone_index = int(args[k]); k += 1
                        parent_index = int(args[k]); k += 1
                        bone_name = args[k]; k += 1
                        px = float(args[k]); py = float(args[k+1]); pz = float(args[k+2]); k += 3
                        rqx = float(args[k]); rqy = float(args[k+1]); rqz = float(args[k+2]); rqw = float(args[k+3]); k += 4
                        qx = float(args[k]); qy = float(args[k+1]); qz = float(args[k+2]); qw = float(args[k+3]); k += 4
                        sx = float(args[k]); sy = float(args[k+1]); sz = float(args[k+2]); k += 3
                        bones_in_chunk.append({
                            "bone_index": bone_index,
                            "parent_index": parent_index,
                            "bone_name": bone_name,
                            "p": (px, py, pz),
                            "rq": (rqw, rqx, rqy, rqz),
                            "q": (qw, qx, qy, qz),  # (w,x,y,z)
                            "s": (sx, sy, sz),
                        })
                except Exception as e:
                    print("[MOVIN Live] Truncated/invalid bone block:", e)
                    continue

                key = (actor_name, frame_idx)
                with _runtime.lock:
                    buf = _runtime.frame_buffers.get(key)
                    if buf is None:
                        buf = {
                            "_t0": now,
                            "timestamp": ts,
                            "actor": actor_name,
                            "frame_idx": frame_idx,
                            "num_chunks": num_chunks,
                            "total_bones": total_bones,
                            "chunks": {},
                        }
                        _runtime.frame_buffers[key] = buf

                    buf["chunks"][chunk_idx] = bones_in_chunk

                    if len(buf["chunks"]) >= buf["num_chunks"]:
                        ordered = []
                        complete = True
                        for ci in range(buf["num_chunks"]):
                            part = buf["chunks"].get(ci)
                            if not part:
                                complete = False
                                break
                            ordered.extend(part)
                        if complete and ordered:
                            frame = {
                                "timestamp": buf["timestamp"],
                                "actor": buf["actor"],
                                "frame_idx": buf["frame_idx"],
                                "bones": ordered,
                            }
                            _runtime.ready_frames.append(frame)
                            _runtime.last_actor = buf["actor"]
                            _runtime.last_ts = buf["timestamp"]
                        del _runtime.frame_buffers[key]

            elif address == "/MOVIN/PointCloud":
                try:
                    frame_idx = int(args[0])
                    total_points = int(args[1])
                    chunk_idx = int(args[2])
                    num_chunks = int(args[3])
                    chunk_point_count = int(args[4])
                except Exception as e:
                    print("[MOVIN Live] Bad point cloud header args:", e)
                    continue

                k = 5
                points_in_chunk = []
                try:
                    for _ in range(chunk_point_count):
                        px = float(args[k]); py = float(args[k+1]); pz = float(args[k+2]); k += 3
                        points_in_chunk.append((px, py, pz))
                except Exception as e:
                    print("[MOVIN Live] Truncated/invalid point cloud block:", e)
                    continue

                key = frame_idx
                with _runtime.lock:
                    buf = _runtime.pointcloud_buffers.get(key)
                    if buf is None:
                        buf = {
                            "_t0": now,
                            "frame_idx": frame_idx,
                            "num_chunks": num_chunks,
                            "total_points": total_points,
                            "chunks": {},
                        }
                        _runtime.pointcloud_buffers[key] = buf

                    buf["chunks"][chunk_idx] = points_in_chunk

                    if len(buf["chunks"]) >= buf["num_chunks"]:
                        ordered = []
                        complete = True
                        for ci in range(buf["num_chunks"]):
                            part = buf["chunks"].get(ci)
                            if part is None:
                                complete = False
                                break
                            ordered.extend(part)
                        if complete:
                            pointcloud = {
                                "frame_idx": buf["frame_idx"],
                                "points": ordered,
                            }
                            _runtime.ready_pointclouds.append(pointcloud)
                            _runtime.last_pointcloud_frame = buf["frame_idx"]
                            _runtime.last_pointcloud_count = len(ordered)
                        del _runtime.pointcloud_buffers[key]

            else:
                continue

            with _runtime.lock:
                _runtime.recv_count += 1
                dt = now - _runtime.last_rate_time
                if dt >= 1.0:
                    _runtime.recv_rate_hz = _runtime.recv_count / dt
                    _runtime.recv_count = 0
                    _runtime.last_rate_time = now

    finally:
        try:
            sock.close()
        except Exception:
            pass
        _runtime.sock = None

# -----------------------
# Coordinate & Math
# -----------------------

def unity_to_blender_vec(v):
    return (-v[0], v[1], v[2])

def unity_to_blender_pointcloud_vec(v):
    return (-v[0], -v[2], v[1])

def unity_to_blender_quat(q):
    return (q[0], q[1], -q[2], -q[3])

def rotate_vec(v, q):
    """
    Rotate vector v by quaternion q.
    v: (x,y,z) tuple/list
    q: (w,x,y,z) tuple/list
    returns (x,y,z) tuple
    """
    quat = Quaternion((q[0], q[1], q[2], q[3]))  # one iterable!
    vec = Vector(v)
    vec.rotate(quat)  # in-place rotation
    return (vec.x, vec.y, vec.z)

def quat_mul(q1, q2):
    w1,x1,y1,z1 = q1; w2,x2,y2,z2 = q2
    return (
        w1*w2 - x1*x2 - y1*y2 - z1*z2,
        w1*x2 + x1*w2 + y1*z2 - z1*y2,
        w1*y2 - x1*z2 + y1*w2 + z1*x2,
        w1*z2 + x1*y2 - y1*x2 + z1*w2
    )

def quat_conj(q):
    w,x,y,z = q
    return (w, -x, -y, -z)

def quat_from_euler(euler):
    x, y, z = euler
    x = math.radians(x)
    y = math.radians(y)
    z = math.radians(z)
    return (
        math.cos(x/2) * math.cos(y/2) * math.cos(z/2) + math.sin(x/2) * math.sin(y/2) * math.sin(z/2),
        math.sin(x/2) * math.cos(y/2) * math.cos(z/2) - math.cos(x/2) * math.sin(y/2) * math.sin(z/2),
        math.cos(x/2) * math.sin(y/2) * math.cos(z/2) + math.sin(x/2) * math.cos(y/2) * math.sin(z/2),
        math.cos(x/2) * math.cos(y/2) * math.sin(z/2) - math.sin(x/2) * math.sin(y/2) * math.cos(z/2)
    )

def _ensure_pointcloud_gn_tree():
    group_name = "MOVIN_PointCloud_GN"
    node_group = bpy.data.node_groups.get(group_name)
    if node_group is not None:
        return node_group

    node_group = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')

    interface = node_group.interface
    interface.new_socket(name="Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    interface.new_socket(name="Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    interface.new_socket(name="Radius", in_out='INPUT', socket_type='NodeSocketFloat')

    nodes = node_group.nodes
    links = node_group.links
    nodes.clear()

    group_input = nodes.new("NodeGroupInput")
    group_input.location = (-400, 0)

    mesh_to_points = nodes.new("GeometryNodeMeshToPoints")
    mesh_to_points.location = (-100, 0)
    mesh_to_points.mode = 'VERTICES'

    set_radius = nodes.new("GeometryNodeSetPointRadius")
    set_radius.location = (150, 0)

    group_output = nodes.new("NodeGroupOutput")
    group_output.location = (400, 0)

    links.new(group_input.outputs["Geometry"], mesh_to_points.inputs["Mesh"])
    links.new(mesh_to_points.outputs["Points"], set_radius.inputs["Points"])
    links.new(group_input.outputs["Radius"], set_radius.inputs["Radius"])
    links.new(set_radius.outputs["Points"], group_output.inputs["Geometry"])

    return node_group

def _ensure_pointcloud_modifier(obj):
    modifier = obj.modifiers.get("MOVIN_PointCloud")
    if modifier is None:
        modifier = obj.modifiers.new(name="MOVIN_PointCloud", type='NODES')
    modifier.node_group = _ensure_pointcloud_gn_tree()
    return modifier

def _ensure_pointcloud_material(material_name, rgba):
    mat = bpy.data.materials.get(material_name)
    if mat is None:
        mat = bpy.data.materials.new(material_name)
        mat.use_nodes = True

    if mat.use_nodes:
        nodes = mat.node_tree.nodes
        principled = nodes.get("Principled BSDF")
        if principled is not None:
            principled.inputs["Base Color"].default_value = rgba
            principled.inputs["Emission Color"].default_value = rgba
            principled.inputs["Emission Strength"].default_value = 1.0
            principled.inputs["Roughness"].default_value = 0.35
    mat.diffuse_color = rgba
    return mat

def _ensure_pointcloud_object(scene, object_name):
    obj = bpy.data.objects.get(object_name)
    if obj is not None and obj.type == 'MESH':
        _ensure_pointcloud_modifier(obj)
        return obj

    if obj is not None and obj.type != 'MESH':
        print(f"[MOVIN Live] Existing object '{object_name}' is not a Mesh")
        return None

    mesh = bpy.data.meshes.new(object_name)
    obj = bpy.data.objects.new(object_name, mesh)
    _ensure_pointcloud_modifier(obj)
    obj.display_type = 'WIRE'
    obj.hide_select = True
    obj.show_wire = True

    scene.collection.objects.link(obj)
    return obj

def _update_pointcloud_object(scene, object_name, points, radius, color_rgba):
    obj = _ensure_pointcloud_object(scene, object_name)
    if obj is None:
        return

    mesh = obj.data
    mesh.clear_geometry()
    mesh.from_pydata(points, [], [])
    mesh.update()

    modifier = _ensure_pointcloud_modifier(obj)
    try:
        modifier["Socket_2"] = radius
    except Exception:
        pass

    material = _ensure_pointcloud_material(object_name + "_MAT", color_rgba)
    if len(mesh.materials) == 0:
        mesh.materials.append(material)
    else:
        mesh.materials[0] = material
    obj.color = color_rgba

def _downsample_points(points, max_points):
    if max_points <= 0 or len(points) <= max_points:
        return points
    step = max(1, math.ceil(len(points) / max_points))
    return points[::step][:max_points]

# -----------------------
# Application loop (main thread)
# -----------------------

def _apply_latest_stream_data(scene_name):
    global _runtime
    scene = bpy.data.scenes.get(scene_name)
    if scene is None or not hasattr(scene, "movin_props"):
        return None

    props = scene.movin_props
    arm_obj = bpy.data.objects.get(props.armature_name)
    with _runtime.lock:
        frame = _runtime.ready_frames.pop() if _runtime.ready_frames else None
        if frame is not None:
            _runtime.ready_frames.clear()
            _runtime.last_applied = frame["frame_idx"]

        pointcloud = _runtime.ready_pointclouds.pop() if _runtime.ready_pointclouds else None
        if pointcloud is not None:
            _runtime.ready_pointclouds.clear()

    did_apply = False

    if frame is not None and arm_obj is not None and arm_obj.type == 'ARMATURE':
        # Ensure POSE position so viewport shows pose changes
        if arm_obj.data.pose_position != 'POSE':
            arm_obj.data.pose_position = 'POSE'

        hips_bone_name = props.hips_bone_name.strip()
        pose_bones = arm_obj.pose.bones

        # Check for constraints that might block location changes (warn once)
        if not _runtime.warned_constraints:
            for pb in pose_bones:
                if pb.constraints:
                    print(f"[MOVIN Live] WARNING: Bone '{pb.name}' has constraints that may override location/rotation.")
                    print("  Consider disabling or removing constraints for live motion capture.")
            _runtime.warned_constraints = True

        # coordinate conversions
        vec_conv = unity_to_blender_vec
        quat_conv = unity_to_blender_quat

        # index incoming by bone name
        by_name = {b["bone_name"]: b for b in frame["bones"]}

        # apply pose
        for name, bdat in by_name.items():
            pb = pose_bones.get(name)
            if pb is None:
                continue

            p = vec_conv(bdat["p"])
            rq = quat_conv(bdat["rq"])
            q = quat_conv(bdat["q"])
            rq_inv = quat_conj(rq)
            q = quat_mul(rq_inv, q)
            s = bdat["s"] # local scale wo conversion

            if name == hips_bone_name:
                hips_translational_scale = props.hips_translational_scale
                pb.location = (p[0] * hips_translational_scale, (p[1] + props.hips_y_offset) * hips_translational_scale, p[2] * hips_translational_scale)

            pb.rotation_mode = 'QUATERNION'
            pb.rotation_quaternion = (q[0], q[1], q[2], q[3])
            pb.scale = (s[0], s[1], s[2])

            # thumb offset (skinning)
            # if name == "LeftHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, 70, 0)), pb.rotation_quaternion)
            # elif name == "RightHandThumb1":
            #     pb.rotation_quaternion = quat_mul(quat_from_euler((0, -70, 0)), pb.rotation_quaternion)
        did_apply = True

    if pointcloud is not None and props.pointcloud_enabled:
        sampled_points = _downsample_points(pointcloud["points"], 15000)
        converted_points = [unity_to_blender_pointcloud_vec(point) for point in sampled_points]
        _update_pointcloud_object(
            scene,
            props.pointcloud_object_name.strip() or "MOVIN_PointCloud",
            converted_points,
            0.02,
            (0.10, 0.85, 1.00, 1.00),
        )
        with _runtime.lock:
            _runtime.last_visualized_point_count = len(converted_points)
        did_apply = True

    if not did_apply:
        return 0.03

    return 0.03

def _timer_tick(scene_name):
    try:
        return _apply_latest_stream_data(scene_name)
    except Exception:
        print("[MOVIN Live] Timer callback failed:")
        traceback.print_exc()
        return 0.5

# -----------------------
# Operators and Panel
# -----------------------

class MOVIN_OT_SelectActiveArmature(Operator):
    bl_idname = "movin.select_active_armature"
    bl_label = "Use Active Armature"
    bl_description = "Use the currently selected armature object"
    def execute(self, context):
        obj = context.active_object
        props = context.scene.movin_props
        if obj and obj.type == 'ARMATURE':
            props.armature_name = obj.name
            self.report({'INFO'}, f"Armature set to '{obj.name}'")
            return {'FINISHED'}
        else:
            self.report({'WARNING'}, "Active object is not an Armature")
            return {'CANCELLED'}

class MOVIN_OT_Start(Operator):
    bl_idname = "movin.start_stream"
    bl_label = "Start"
    bl_description = "Start listening for /MOVIN/Frame and applying to the armature"
    _timer = None
    def execute(self, context):
        global _runtime
        props = context.scene.movin_props
        if props.is_running:
            self.report({'INFO'}, "Already running")
            return {'CANCELLED'}
        arm_obj = bpy.data.objects.get(props.armature_name) if props.armature_name else None
        has_valid_armature = arm_obj is not None and arm_obj.type == 'ARMATURE'
        if not has_valid_armature and not props.pointcloud_enabled:
            self.report({'WARNING'}, "Please select a valid Armature or enable point cloud visualization")
            return {'CANCELLED'}

        # Make sure viewport shows pose results
        if arm_obj and hasattr(arm_obj.data, "pose_position"):
            arm_obj.data.pose_position = 'POSE'

        _runtime.reset()
        _runtime.running = True
        t = threading.Thread(target=_udp_server_loop, args=(props.port,), daemon=True)
        _runtime.thread = t
        t.start()
        scene_name = context.scene.name
        self._timer = bpy.app.timers.register(lambda: _timer_tick(scene_name), first_interval=0.01, persistent=True)
        props.is_running = True
        print(f"[MOVIN Live] Listening on UDP {props.port}")
        return {'FINISHED'}

class MOVIN_OT_Stop(Operator):
    bl_idname = "movin.stop_stream"
    bl_label = "Stop"
    bl_description = "Stop listening and applying"
    def execute(self, context):
        global _runtime
        props = context.scene.movin_props
        if not props.is_running:
            self.report({'INFO'}, "Not running")
            return {'CANCELLED'}
        props.is_running = False
        _runtime.running = False
        if _runtime.sock:
            try:
                _runtime.sock.close()
            except Exception:
                pass
        time.sleep(0.05)
        _runtime.thread = None
        _runtime.sock = None
        _runtime.reset()
        print("[MOVIN Live] Stopped")
        return {'FINISHED'}

class MOVIN_OT_DumpStatus(Operator):
    bl_idname = "movin.dump_status"
    bl_label = "Print Status"
    bl_description = "Print receiver status to the console"
    def execute(self, context):
        global _runtime
        with _runtime.lock:
            print("[MOVIN Live] --- Status ---")
            print(" running:", context.scene.movin_props.is_running)
            print(" last actor:", _runtime.last_actor)
            print(" last ts:", _runtime.last_ts)
            print(" last applied frame:", _runtime.last_applied)
            print(" last point cloud frame:", _runtime.last_pointcloud_frame)
            print(" last point count:", _runtime.last_pointcloud_count)
            print(" visualized point count:", _runtime.last_visualized_point_count)
            print(" recv rate (Hz):", f"{_runtime.recv_rate_hz:.1f}")
            print(" queued complete frames:", len(_runtime.ready_frames))
            print(" queued point clouds:", len(_runtime.ready_pointclouds))
            print(" partials:", len(_runtime.frame_buffers))
            print(" point cloud partials:", len(_runtime.pointcloud_buffers))
        return {'FINISHED'}

class MOVIN_PT_Panel(Panel):
    bl_idname = "MOVIN_PT_panel"
    bl_label = "MOVIN Live"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "MOVIN Live"
    def draw(self, context):
        layout = self.layout
        layout.scale_x = 1.5
        layout.scale_y = 1
        props = context.scene.movin_props
        global _runtime

        col = layout.column(align=True)
        col.prop_search(props, "armature_name", bpy.data, "objects", text="Armature")
        col.operator("movin.select_active_armature", text="Use Active Armature", icon="ARMATURE_DATA")

        layout.separator(factor=0.5)
        col = layout.column(align=True)
        col.prop(props, "port")

        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Global Transform Handling", icon="OUTLINER_OB_ARMATURE")
        row = box.row(align=True)
        row.prop(props, "hips_bone_name")
        row = box.row(align=True)
        row.prop(props, "hips_translational_scale")
        row = box.row(align=True)
        row.prop(props, "hips_y_offset")

        layout.separator(factor=0.5)
        box = layout.box()
        box.label(text="Point Cloud", icon="MESH_DATA")
        row = box.row(align=True)
        row.prop(props, "pointcloud_enabled")
        row = box.row(align=True)
        row.prop(props, "pointcloud_object_name")

        layout.separator(factor=0.5)
        row = layout.row(align=True)
        row.enabled = not props.is_running
        row.operator("movin.start_stream", text="Start", icon="PLAY")
        row = layout.row(align=True)
        row.enabled = props.is_running
        row.operator("movin.stop_stream", text="Stop", icon="PAUSE")
        layout.operator("movin.dump_status", text="Print Status", icon="INFO")

        layout.separator(factor=0.8)
        box = layout.box()
        box.label(text="Live Status", icon="RESTRICT_VIEW_OFF")
        with _runtime.lock:
            box.label(text=f"Actor: {_runtime.last_actor or '-'}")
            box.label(text=f"Timestamp: {_runtime.last_ts or '-'}")
            box.label(text=f"Last Frame: {_runtime.last_applied if _runtime.last_applied is not None else '-'}")
            box.label(text=f"PointCloud Frame: {_runtime.last_pointcloud_frame if _runtime.last_pointcloud_frame is not None else '-'}")
            box.label(text=f"Point Count: {_runtime.last_pointcloud_count}")
            box.label(text=f"Displayed Points: {_runtime.last_visualized_point_count}")
            box.label(text=f"Incoming (Hz): {_runtime.recv_rate_hz:.1f}")
            box.label(text=f"Queued Frames: {len(_runtime.ready_frames)}")
            box.label(text=f"Queued PointClouds: {len(_runtime.ready_pointclouds)}")

# -----------------------
# Registration
# -----------------------

classes = (
    MOVIN_Props,
    MOVIN_OT_SelectActiveArmature,
    MOVIN_OT_Start,
    MOVIN_OT_Stop,
    MOVIN_OT_DumpStatus,
    MOVIN_PT_Panel,
)

def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.movin_props = PointerProperty(type=MOVIN_Props)

def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    if hasattr(bpy.types.Scene, "movin_props"):
        del bpy.types.Scene.movin_props

if __name__ == "__main__":
    register()
