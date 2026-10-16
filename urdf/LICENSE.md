# URDF / Mesh Files License

The URDF and STL mesh files in this directory are derived from the
**SO-ARM100 / SO-101** project by The Robot Studio:

- Source: https://github.com/TheRobotStudio/SO-ARM100
- Copyright: The Robot Studio
- License: Apache License 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

Modifications: file layout flattened and mesh path prefixes removed
for use with the OpenMimic URDF viewer.

`so101_new_calib.xml` (MuJoCo MJCF) comes from the same project
(`Simulation/SO101/so101_new_calib.xml`). Modification: `meshdir` changed from
`assets` to `.` so it uses the STL files in this directory. OpenMimic adds the
table, cube, target area and cameras at load time (`openmimic_mujoco.py`).
