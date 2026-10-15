# OMX URDF / Mesh Files

`omx_f.urdf` 와 `open_manipulator_description/meshes/omx_f/*.stl` 은 ROBOTIS 의
**open_manipulator** 저장소에서 가져온 원본 그대로입니다 (수정 없음).

- 출처: https://github.com/ROBOTIS-GIT/open_manipulator
- 경로: `open_manipulator_description/urdf/omx_f/omx_f.urdf`, `open_manipulator_description/meshes/omx_f/`
- 커밋: `d31000d90c679af9c982e73de8b12d777c5ff7dd` (2026-09-28)
- 라이선스: Apache License 2.0 — Copyright ROBOTIS CO., LTD.

URDF 의 메시 경로(`package://open_manipulator_description/...`)를 고치지 않으려고
같은 폴더 구조로 두었습니다. OpenMimic 3D 는 `package://` 를 `/urdf/` 로 풀어 읽습니다.
