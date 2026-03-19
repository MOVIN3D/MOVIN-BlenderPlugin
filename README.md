# MOVIN Blender Plugin

Blender add-on for receiving and previewing live MOVIN motion and point cloud OSC streams.

This add-on lets you preview MOVIN data directly in Blender by:

- driving a selected armature from `/MOVIN/Frame`
- visualizing `/MOVIN/PointCloud` in the viewport

It is set up for Blender 5.1 and includes sample assets for quick testing.

## Highlights

- Live armature retargeting by bone name
- Live point cloud preview in Blender
- Simple N-panel workflow
- Sample `.blend` and `.fbx` files included

## Repository Contents

- `addon/movin_blender_plugin.py`  
  Blender add-on script
- `samples/blend/MOVINMan_Sample.blend`  
  Sample scene for MOVINMan
- `samples/blend/Ch14_Sample.blend`  
  Additional sample scene
- `samples/fbx/MOVINManBlender.fbx`  
  Sample MOVINMan FBX
- `samples/fbx/Ch14_nonPBR.fbx`  
  Sample character FBX

## Installation

1. Open Blender
2. Go to `Edit > Preferences > Add-ons`
3. Click `Install...`
4. Select `addon/movin_blender_plugin.py`
5. Enable `MOVIN Live Receiver`

## Usage

1. Open the `MOVIN Live` tab in the 3D Viewport side panel
2. Select the target armature and click `Use Active Armature`
3. Set the OSC port if needed
4. Enable `Visualize Point Cloud` if you want point cloud preview
5. Click `Start`

## OSC Formats

### `/MOVIN/Frame`

Header:

`[timestamp, actorName, frameIdx, numChunks, chunkIdx, totalBoneCount, chunkBoneCount]`

Per-bone payload:

`[boneIndex, parentIndex, boneName, px, py, pz, rqx, rqy, rqz, rqw, qx, qy, qz, qw, sx, sy, sz]`

### `/MOVIN/PointCloud`

Header:

`[frameIdx, totalPoints, chunkIdx, numChunks, chunkPointCount]`

Per-point payload:

`[x, y, z]`

## Notes

- Motion and point cloud streams are received on the same UDP port
- Bone transforms are applied by name, so incoming bone names should match the target rig

## Project Structure

```text
MOVIN_Blender/
|- addon/
|  `- movin_blender_plugin.py
|- samples/
|  |- blend/
|  |  |- MOVINMan_Sample.blend
|  |  `- Ch14_Sample.blend
|  `- fbx/
|     |- MOVINManBlender.fbx
|     `- Ch14_nonPBR.fbx
|- .gitignore
|- LICENSE
`- README.md
```

## Recommended Blender Version

- Blender 5.1

## License

See [LICENSE](LICENSE).
