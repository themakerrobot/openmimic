# OpenMimic 개발 문서

사용법은 [README](../README.md) 에 있습니다. 이 문서는 구조와 "왜 이렇게 했는지" 를 적습니다.

- [파일 구성](#파일-구성)
- [기종 (SO-ARM101 / OMX)](#기종-so-arm101--omx)
- [설정 파일 `openmimic_config.json`](#설정-파일-openmimic_configjson)
- [포트 지정 — by-id / by-path](#포트-지정--by-id--by-path)
- [셋업 마법사 내부](#셋업-마법사-내부)
- [캘리브레이션 (SO-ARM101)](#캘리브레이션-so-arm101)
- [연결·토크 순서](#연결토크-순서)
- [추종 오차 감시](#추종-오차-감시)
- [서보 상태 감시 · 전원 점검](#서보-상태-감시--전원-점검)
- [수집 worker](#수집-worker)
- [팔 불량 점검 판정 근거](#팔-불량-점검-판정-근거)
- [보안·동시성](#보안동시성)
- [OpenVINO (Intel NPU / GPU / CPU)](#openvino-intel-npu--gpu--cpu)
- [롤아웃 실행기·실시간 모니터·시도 기록](#롤아웃-실행기실시간-모니터시도-기록)
- [모델·데이터셋 내보내기/가져오기, 이어서 학습, 정책](#모델데이터셋-내보내기가져오기-이어서-학습-정책)
- [학습 정책별 처리](#학습-정책별-처리-train_policies--policy_train_args)
- [Hugging Face Hub · HF Jobs](#hugging-face-hub--hf-jobs)
- [데이터셋 복구·사전 점검·수집 신호음 등](#데이터셋-복구사전-점검수집-신호음-등)
- [화면 구조 — 탭·첫 실행·막힘 안내](#화면-구조--탭첫-실행막힘-안내)
- [가상 장치 (화면에서 켜기)](#가상-장치-화면에서-켜기)
- [한/영 전환 (i18n)](#한영-전환-i18n)
- [Intel 기기 학습 (XPU, 실험적)](#intel-기기-학습-xpu-실험적)
- [lerobot 버전·설치](#lerobot-버전설치)
- [테스트](#테스트)

## 파일 구성

| 파일 | 설명 |
|---|---|
| `main.py` | 웹 툴 전체 (FastAPI 한 파일, port 8080). `python main.py --worker record <jid>` 로 수집 worker 도 겸함 |
| `openmimic.sh` | 켜기·끄기·상태·업데이트·자동 시작(systemd --user). 항상 `activate.sh` 로 환경을 켜고 실행 |
| `tools_armcheck.py` | SO-ARM101(STS3215) 팔 점검 — 셋업 탭·마법사 진단과 CLI 공용 |
| `tools_dxlcheck.py` | OMX(Dynamixel X) 팔 점검 — 같은 함수 이름·결과 형식 |
| `openmimic_rollout.py` | 롤아웃 실행기 — lerobot-rollout 을 같은 프로세스에서 돌리며 실시간 상태(카메라·관절·추론 ms·Hz)를 `RUN_DIR/<jid>/` 에 씀. `--openmimic.engine=ov` 면 openmimic_ov 로 신경망 교체 |
| `openmimic_hub.py` | Hugging Face Hub — 로그인 상태(whoami 캐시)·하드웨어 목록, 데이터셋 올리기/받기, 모델 받기, HF Jobs 클라우드 학습 래퍼 |
| `tools_dsrepair.py` | 마무리 안 된(끊긴) 데이터셋 복구 — huggingface/leLab `dataset_repair.py`(Apache-2.0)를 옮겨 와 백업·CLI 추가 |
| `openmimic_ov.py` | ACT·ACT2 → OpenVINO 변환·검증(`convert`), 장치 조회(`devices`), OpenVINO 추론으로 lerobot-rollout 실행(`rollout`). OpenMimic 은 이 파일을 별도 프로세스로 띄웁니다 |
| `tools_jscheck.py` | 모든 페이지의 인라인 JS 를 `node --check` 로 파싱 검증 (`--en` 이면 영어 치환 후 검증) |
| `openmimic_i18n_en.json` | 영어 화면 사전 — 한국어 조각 → 영어 (한/EN 버튼) |
| `tools_simarms.py` | 가상 팔 — STS3215 / Dynamixel X 보드를 흉내내 `sim://<이름>` 포트로 내놓음 (127.0.0.1 소켓). 켜 있으면 `openmimic_sim.json` 에 포트·접속 주소·가상 카메라를 알리고 OpenMimic 포트 목록에 추가됨. 셋업 화면의 '가상 장치 켜기' 가 작업으로 띄움 |
| `openmimic_fakeserial.py` | 가상 팔 포트 — `serial.Serial(port="sim://…")` 만 `FakeSerial`(소켓으로 가상 팔 도구의 보드에 연결)로 바꾸고 실제 포트는 그대로. 서버·수집 worker·롤아웃·점검 도구가 시작할 때 `install()` |
| `openmimic_simcam.py` | 가상 카메라 — `openmimic_sim/cam_<이름>` 경로면 합성 영상, 아니면 원래 `cv2.VideoCapture` |
| `openmimic_health.py` | 서보 상태 감시 — 시작 전 전원 점검(`power_check` 계열)과 실행 중 온도·전압·보호 플래그 감시(`HealthGuard`) |
| `openmimic_platform.py` | Linux / Windows 차이를 모은 곳 — 시리얼 포트·카메라 나열과 식별자 해석, 작업 프로세스 띄우기·중지·생존 확인, 공유 폴더, MuJoCo 렌더 백엔드, OS 별 안내 문구 |
| `plugins/lerobot_robot_bi_omx/` | 양팔 OMX 팔로워 `bi_omx_follower` (lerobot 플러그인) |
| `plugins/lerobot_teleoperator_bi_omx/` | 양팔 OMX 리더 `bi_omx_leader` |
| `plugins/lerobot_policy_act2/` | ACT2 정책 `act2` — lerobot ACT 블록을 import 하고 증강·청크 보간·DINOv2 백본·flow 헤드를 설정으로 붙임 |
| `tools_act2bench.py` | ACT 와 ACT2 변형을 같은 step·시드로 학습해 시간·loss·추론 지연·성공률을 표로 비교 |
| `urdf/` | 3D 용 URDF·STL — SO-101 (TheRobotStudio), OMX-F (ROBOTIS) |
| `lerobot_conda.sh` | 새 기기 설치 스크립트 |
| `activate.sh` | conda 활성화 + `HF_HOME` + 작업 폴더 이동 (`lerobot_conda.sh` 가 생성) |
| `install.ps1` · `openmimic.ps1` · `activate.ps1` | Windows 판 설치·실행 도구·환경 켜기 (UTF-8 BOM — Windows PowerShell 5.1 이 BOM 없는 파일을 시스템 코드 페이지로 읽어 한국어가 깨짐) |
| `.gitattributes` | `*.ps1` 은 CRLF, `*.sh` 는 LF 로 체크아웃 (Windows 에서 clone 해도 bash 스크립트가 깨지지 않게) |

실행하면 `~/project/openmimic/` 아래에 생기는 파일 (전부 `.gitignore`):

| 파일 | 설명 |
|---|---|
| `openmimic_config.json` | 사용 중인 환경의 설정 |
| `openmimic_envs/<이름>.json` | 이름 붙은 환경들 — 전환하면 `openmimic_config.json` 으로 복사 |
| `openmimic_projects.json` | 프로젝트 (태스크·기준 환경·데이터셋·모델) |
| `openmimic_dsmeta.json` | 데이터셋별 수집 환경·생성 시각 |
| `openmimic_wizard.json` | 마법사의 팔별 '확인 완료' 기록 (환경별) |
| `openmimic_jobs/` | 백그라운드 작업 기록·로그 |
| `openmimic_marks.json` | 불량 에피소드 표시 |
| `openmimic_trials.json` | 롤아웃 시도 결과 (체크포인트별 성공/실패) |
| `outputs/<run>/openmimic_import.json` | 다른 기기에서 가져온 모델 표시 |

## 기종 (SO-ARM101 / OMX)

기종마다 다른 것은 `main.py` 의 `ROBOT_KINDS` 표 한 곳에 있습니다. 나머지 코드는 `kind()` 로 꺼내 씁니다.
한팔/양팔(`mode`)과는 독립입니다.

| 항목 | SO-ARM101 | OMX |
|---|---|---|
| lerobot 클래스 | `SOFollower` / `SOLeader`, `BiSOFollower` / `BiSOLeader` | `OmxFollower` / `OmxLeader`, `BiOmxFollower` / `BiOmxLeader` (플러그인) |
| CLI type | `so101_follower`, `bi_so_follower` … | `omx_follower`, `bi_omx_follower` … |
| 버스 | `FeetechMotorsBus` (STS3215, ID 1~6) | `DynamixelMotorsBus` (팔로워 ID 11~16 XL430/XL330, 리더 ID 1~6 XL330) |
| 관절 단위 | ° (`use_degrees=True`), 그리퍼 0~100 | -100~100 (`use_degrees=False`), 그리퍼 0~100 |
| 캘리브레이션 | 범위를 손으로 기록 | lerobot 공장값 (오프셋 0, 범위 0~4095) |
| `Homing_Offset` 부호 | `Present = Actual − Homing` | `Present = Actual + Homing` |
| 리더 토크 | 없음 | 그리퍼만 (전류 제어, 손가락 트리거) |
| 캘리브레이션 폴더 | `robots/so_follower`, `teleoperators/so_leader` | `robots/omx_follower`, `teleoperators/omx_leader` |

- 장치 객체는 `make_arm(role, arm_cfg)` / `make_bi(role, arms)` 로만 만듭니다. 버스만 필요한 곳(포트 감시·모터 ID
  세팅·확인 단계)은 `make_bus(role, port)` — lerobot 장치가 만드는 버스를 그대로 써서 모터 ID·모델·정규화가 같아집니다.
- **OMX 단위**: `omx_leader` 에는 `use_degrees` 가 없고 항상 -100~100 입니다. 팔로워를 °로 두면 리더 값을 그대로 못 따라가서
  팔로워도 -100~100 으로 맞춥니다. 공장값 범위 0~4095 전체가 -100~100 이라 1 단위 ≈ 1.8° 입니다. 제어의 이동 속도 상한,
  데드밴드, 추종 오차 기준은 ° 로 정의돼 있어 `deg_per_unit` 으로 나눠 씁니다. `max_relative_target` 도 이 단위입니다.
- **양팔 OMX 플러그인**: lerobot(`e40b58a`, 2026-09 main 까지)에 양팔 OMX 가 없습니다. lerobot 의 `BimanualMixin` 과
  서드파티 플러그인 규칙(설치된 패키지 이름이 `lerobot_robot_*` / `lerobot_teleoperator_*` 이면
  `register_third_party_plugins()` 가 import)을 써서 `bi_so_follower` 와 같은 구조로 만들었습니다.
  팔별 id `{id}_left` / `{id}_right`, 키 접두사 `left_` / `right_`, 공용 카메라는 접두사 없음 — SO 양팔과 같습니다.
  OpenMimic 은 설치가 안 돼 있어도 `plugins/` 를 직접 읽지만, lerobot CLI(롤아웃)는 설치된 패키지만 찾으므로
  롤아웃 시작 때 설치 여부를 확인합니다.
- **OMX 3D**: ROBOTIS `open_manipulator` 의 `omx_f.urdf`(수정 없음). 관절 이름 `joint1~5`, `gripper_joint_1`(2 는 mimic)로
  매핑하고 -100~100 → ±π rad 로 바꿉니다. URDF 의 0 rad 가 엔코더 2048 이라고 가정했고, 그리퍼 0~100 → 0~0.9 rad 는
  URDF 에 범위가 없어서 추정값입니다 (`kind3d()` 에서 조정). 서보 메시가 따로 없어 모터 색 표시는 SO-ARM101 만 됩니다.
- 데이터셋 `robot_type` 은 기종·모드별로 달라서(`so_follower` / `bi_so_follower` / `omx_follower` / `bi_omx_follower`)
  기종이 다른 데이터셋은 이어받기·추론이 막힙니다. 리뷰 화면 3D 는 데이터셋을 찍은 기종으로 그립니다.

## 설정 파일 `openmimic_config.json`

```json
{
  "robot": "so101",
  "mode": "bimanual",
  "fps": 30,
  "default_task": "Pick up the block and place it in the box",
  "max_relative_target": null,
  "arms": [
    { "side": "left",
      "follower_port": "/dev/serial/by-id/usb-...", "follower_id": "follower_left",
      "leader_port":   "/dev/serial/by-id/usb-...", "leader_id":   "leader_left",
      "cameras": { "wrist": { "index_or_path": "/dev/v4l/by-id/usb-...-video-index0", "width": 640, "height": 480, "fps": 30 } },
      "view": { "x": 0.0, "y": 0.12, "yaw_deg": 0.0 } },
    { "side": "right", "...": "..." }
  ],
  "cameras": { "top": { "index_or_path": "/dev/v4l/by-id/usb-...", "width": 640, "height": 480, "fps": 30 } }
}
```

- `robot` — `so101` | `omx`. 없으면 `so101` (예전 파일 호환)
- `mode` 가 1차 기준. `single` 이면 `arms` 1개(`side: "main"`), `bimanual` 이면 `left`/`right`. 안 맞으면 로드 때 맞춥니다
- `follower_id` / `leader_id` → 캘리브레이션 파일 이름. 양팔은 `X_left` / `X_right` 형식이어야 합니다
  (lerobot 양팔 클래스가 `{id}_left.json` 으로 찾음)
- 양팔에서 팔 카메라 키는 `left_wrist` 처럼 접두사가 붙고, 최상위 `cameras` 는 그대로입니다.
  공용 카메라 이름이 팔 카메라 원래 이름과 같으면 lerobot 이 거부하므로 저장 때 막습니다
- `max_relative_target` — lerobot 상대 이동 제한. Control·수집·롤아웃 공통. 켜면 `send_action` 마다 위치를 한 번 더 읽습니다
- `arms[].view` — 3D 화면 배치 전용 (제어·데이터와 무관)

## 포트 지정 — by-id / by-path

같은 보드 여러 개는 전기적으로 구분이 안 되고, `/dev/ttyACM*` 순서는 재부팅마다 바뀝니다.
보드에 USB 시리얼 번호가 있으면 `/dev/serial/by-id/…`(보드를 따라감), 없으면 `/dev/serial/by-path/…`(꽂은 USB 구멍을 따라감)를
자동으로 고릅니다. Seeed SO-ARM101 Pro 의 보드(`1a86:55d3`)는 시리얼 번호가 있어 by-id 가 잡힙니다.

리더/팔로워는 *포트 감시* 로 구분합니다 — 모든 포트를 토크 OFF 로 열고 엔코더를 읽어, 손으로 움직인 팔의 포트를 찾습니다.
OMX 는 리더(ID 1~6)·팔로워(ID 11~16) 모터 구성이 달라서 두 구성을 차례로 시험합니다.

## 플랫폼 분기 (`openmimic_platform.py`)

OS 에 따라 다른 코드는 이 모듈에만 둡니다. 다른 파일은 `os.name` · `sys.platform` 을 보지 않고 `P.<함수>` 를 부릅니다.
Linux 쪽은 원래 `main.py` 등에 있던 코드를 그대로 옮겼습니다.

| 기능 | Linux | Windows |
|---|---|---|
| 포트 나열 `list_serial_ports` | `/dev/ttyACM*` · `/dev/ttyUSB*` + `/dev/serial/by-id`·`by-path` 역매핑, USB 정보는 sysfs | pyserial `comports()` — `by_id = usb-<vid>_<pid>_<시리얼>`(시리얼 번호가 있을 때), `by_path` = USB 위치(`1-3.2`) |
| 저장된 포트 → 열 포트 `resolve_serial_port` | 그대로 (심볼릭 링크를 pyserial 이 따라감) | by-id → USB 위치 → COM 순서로 지금 연결된 COM 이름을 찾음. 없으면 `PortNotFound` |
| 같은 장치 비교 `port_key` | `realpath` | 해석한 COM 이름 |
| 카메라 `list_cameras` · `resolve_camera` | lerobot `find_cameras` + `/dev/v4l/by-id`·`by-path`, 값은 그대로 | DirectShow 장치 순서 = OpenCV `CAP_DSHOW` 인덱스. 안정 식별자는 `dshow:<장치 경로>`(pygrabber, 없으면 ffmpeg 목록), 열 때 인덱스 + `CAP_DSHOW` 로 바꿈 |
| 작업 띄우기·중지 `spawn_job` · `stop_job` | 새 세션 · 프로세스 그룹에 SIGINT / SIGKILL | 새 프로세스 그룹 · `CTRL_BREAK` / `taskkill /T /F` |
| 생존 확인 `job_alive` · `pid_exists` | `waitpid`·`kill(pid, 0)`·`/proc` 좀비 판정 | psutil (Windows 의 `os.kill(pid, 0)` 은 확인이 아니라 **종료**라 쓰면 안 됨) |
| 중지 처리기 `install_stop_handlers` · `forward_break_to_sigint` | SIGINT·SIGTERM | + SIGBREAK. lerobot 롤아웃은 SIGINT 처리기만 달기 때문에 CTRL_BREAK 를 그 처리기로 넘겨 시작 자세 복귀·토크 해제를 탑니다 |
| 공유 폴더 `shm_dir` · `frame_dir` | `/dev/shm` (없으면 레포 폴더) | 레포 폴더 · 임시 폴더 |
| MuJoCo 렌더 `setup_mujoco_gl` | 하드웨어 EGL → OSMesa | `wgl` 하나만 시험, 안 되면 물리 시뮬레이션 없이 가상 장치를 켬 |
| 안내 문구 `TEXT` | dialout · `/dev/serial/by-id` | 장치 관리자 · COM · USB 위치 |

설정 파일에는 계속 by-id 우선, 없으면 by-path(USB 위치)를 저장합니다. Windows 의 `COM5` 같은 이름은 꽂을 때마다 바뀔 수 있어 저장하지 않고,
lerobot 에 넘기기 직전(`make_arm` · CLI 인자 · 점검 도구)에만 해석합니다.

Windows 에서 따로 챙긴 것:

- **UTF-8 모드**: 작업 worker 에 `PYTHONUTF8=1`(`P.child_env_extra`), 서버는 `openmimic.ps1` 이 `-X utf8` 로 띄웁니다.
  설정·로그·표준출력에 한국어가 많아 Windows 기본 인코딩이면 깨지거나 `UnicodeEncodeError` 로 멈춥니다.
- **중지 신호**: 작업은 `CREATE_NEW_PROCESS_GROUP` 로 띄우고 `CTRL_BREAK` 를 그 그룹에 보냅니다.
  `GenerateConsoleCtrlEvent` 는 같은 콘솔에만 닿는데, `openmimic.ps1 restart` 뒤에는 서버(새 숨은 콘솔)와 남은 작업(옛 콘솔)이 갈립니다.
  그래서 콘솔 없는 도우미 프로세스가 작업 콘솔에 붙어(`AttachConsole`) 보냅니다 (`P._win_break`).
  lerobot 롤아웃은 SIGINT 처리기만 달아서 `P.forward_break_to_sigint` 로 넘깁니다.
- **COM 포트는 한 곳만 엶**: Linux 는 같은 tty 를 여러 곳이 열어도 되지만, Windows 는 두 번째 열기가 `Access is denied` 입니다.
  포트를 여는 시작 요청과 제어 탭 연결은 같은 관문(`_START_GATE`)을 지나고, '사용 중' 플래그는 포트를 닫은 뒤에 내립니다.
- **파일 잠금**: 열린 파일은 지우거나 바꿔치기할 수 없습니다.
  - 설정 등 바꿔치기는 `P.replace_file`, 읽기는 `P.read_text` 로 잠깐 다시 시도합니다.
  - 명령 파일은 `P.take_file`(이름 바꾸고 읽기)로 가져옵니다.
  - 렌더 프레임은 같은 크기면 덮어씁니다.
  - 데이터셋 이름 바꾸기·지우기는 `P.move_dir`·`P.remove_dir`(열린 파일이 있으면 통째로 실패)입니다.
- **이름**: Windows 가 못 쓰는 이름(끝의 `.`, `con`·`nul`·`com1` 등)은 `safe_name` 이 `P.name_ok` 로 막습니다.
- **롤아웃 CLI 카메라 경로**: `C:\Users\…\openmimic_sim\cam_top` 처럼 역슬래시·공백이 든 경로도 draccus 가 따옴표 없이 그대로 받는 것을 확인했습니다.

## 셋업 마법사 내부

셋업 흐름은 전압 → 모터 확인 → EEPROM 캘리브레이션 → 캘리브레이션 → 확인 순서입니다.
모터를 구동하는 단계는 없습니다.

| 단계 | 서보에 쓰는 것 |
|---|---|
| 포트 찾기 | 포트 감시 중 `Torque_Enable=0` |
| 진단 | 없음 (모터 ID 세팅 때만 ID·보드레이트) |
| 캘리브레이션 (SO) | 시작 때 `Homing_Offset=0`·위치 제한 해제, 저장 때 `Homing_Offset`·`Min/Max_Position_Limit`. 가져오기는 읽기만 |
| 캘리브레이션 (OMX) | lerobot `calibrate()` — `Operating_Mode`, `Drive_Mode`, 공장값 오프셋·범위 |
| 확인 | 없음 ('토크 끄기' 때만 `Torque_Enable=0`) |

- **보드에서 가져오기 (SO)**: lerobot 은 캘리브레이션을 파일과 모터 EEPROM 양쪽에 씁니다. 다른 PC 에서 캘리브레이션한 팔은
  EEPROM 만 읽어 파일을 다시 만듭니다 (`read_calibration` 과 같은 값). 공장값이거나 일부만 캘리브레이션돼 있으면 거부.
- **확인 단계 보정**: EEPROM 의 `Homing_Offset` 이 파일과 다르면, 연결 때 lerobot 이 파일 값을 써 넣은 뒤의 값으로 보여 줍니다
  (Feetech `+ (EEPROM − 파일)`, Dynamixel `+ (파일 − EEPROM)`).
- **한팔 ↔ 양팔 전환**: 한팔의 팔이 `left` 가 되고 지금 id 에 `_left` 를, 오른팔은 같은 이름에 `_right` 를 붙입니다
  (`follower` → `follower_left` / `follower_right`, `sim_follower` → `sim_follower_left` / `sim_follower_right`). 양팔 → 한팔은 `_left` 를 뗍니다. 같은 팔이 쓰던 캘리브레이션 파일을
  새 id 로 복사하고 '확인됨' 기록도 옮깁니다 (대상에 다른 파일이 있으면 `.json.bak`).
- **기종 전환**: 보드가 다르니 포트를 비웁니다. 캘리브레이션 파일은 기종별 폴더라 섞이지 않습니다.

## 캘리브레이션 (SO-ARM101)

`lerobot-calibrate` 와 같은 결과를 만들지만 **중앙 자세 단계가 없습니다.** STS3215 는 단일 회전 절대 엔코더라,
CLI 처럼 "중앙에 놓고 Enter" 로 잡은 자세가 실제 중앙에서 벗어나 있으면 반대쪽 끝에서 `4095 → 0` 으로 넘어가 범위가 망가집니다.
그래서 순서를 바꿨습니다:

1. `reset_calibration()` — `Homing_Offset=0`, 읽는 값이 원시 엔코더값
2. 쓸면서 연속 표본 차이로 언랩 (±2048 넘는 점프를 ∓4096 보정) → 진짜 min/max
3. 저장 때 `homing_offset = 중심 − 2047`, `range = 2047 ± span/2` (오프셋이 −2048 이면 2047 로 바꾸고 범위를 +1 — 부호-크기 11비트 한계)

- `wrist_roll` 은 범위를 재지 않고 0~4095. 저장하는 순간의 자세가 0° — 리더·팔로워를 같은 방향으로 두고 저장
- 취소하면 바뀐 EEPROM 을 이전 값으로 되돌립니다

## 캘리브레이션 파일 관리 (여러 팔 세트)

- 파일 이름 = 캘리브 id. 기종·역할별 폴더(`robots/so_follower` 등, `CALIB_DIRS`)라 id 가 같아도 기종끼리 섞이지 않습니다.
- **id 변경 자동 복사** (`calib_rename_copies`): Setup 저장 때 같은 기종·같은 역할·같은 포트의 팔이 id 만 바뀌었으면
  옛 파일을 새 이름으로 복사합니다. 새 이름에 이미 파일이 있으면 **덮어쓰지 않습니다** — 다른 세트의 파일로 바꾸려는 의도일 수 있습니다.
  (한팔↔양팔 전환의 `convert_mode` 는 반대로 .bak 을 남기고 덮어씁니다 — 같은 팔이 이름만 바뀌는 경우라서)
- **내보내기** `/api/calib/export`: 모든 폴더의 `*.json` 을 `calibration/` 아래 상대경로 그대로 zip.
- **가져오기** `/api/calib/import`: zip 만, 4MB 이하. 알려진 폴더(`CALIB_DIRS`)·`safe_name` id·64KB 이하·
  lerobot `MotorCalibration` 모양(`id, drive_mode, homing_offset, range_min, range_max` 정수)만 받고 나머지는 건너뛴 목록으로 돌려줍니다.
  경로 탈출(`..`)·모르는 폴더는 이 검사로 막힙니다. 같은 이름의 다른 내용은 `overwrite=1` 일 때만 `.bak` 을 남기고 덮어씁니다.

## 연결·토크 순서

**토크 ON 전에 목표부터.** STS3215 / Dynamixel 의 `Goal_Position` 은 RAM 에 이전 값이 남아 있어, 토크만 켜면 그 목표로 튑니다.
`set_torque(True)` 는 현재 위치 → `Goal_Position` → `Torque_Enable=1` 순서이고, 한 모터라도 실패하면 전부 다시 끕니다.

**연결도 같은 문제** — lerobot `connect()` 는 `configure()` 를 `with bus.torque_disabled():` 안에서 돌리고 빠져나올 때
토크를 켭니다. 그래서 OpenMimic 은 팔로워를 `connect_follower()` 로 연결합니다 (Control·수집·Calib, 양팔은 팔마다):

1. `bus.connect()` → 모든 모터 `Torque_Enable=0` (Feetech 는 `Lock=0` 도)
2. 파일과 보드가 다르면 **토크가 꺼진 상태에서** `write_calibration`
3. `Goal_Position = Present_Position`
4. 카메라 → `configure()` (토크가 켜짐 — lerobot 과 같은 최종 상태)

lerobot `bus.connect()` 는 모터 확인 실패 때 포트를 연 채로 예외를 내서, 실패하면 `close_arm()` 이 포트·카메라를 닫습니다.

**토크 해제는 모터마다 끝까지.** lerobot `bus.write` 는 에러 비트(과부하 등)만 있어도 예외를 내서, `disable_torque()` 는
멈춘 모터 하나에서 끝나고 나머지(양팔이면 다른 팔 전체)에 토크가 남습니다. E-STOP·해제·토크 끄기는 모터마다 따로 쓰고
통신 성공만 봅니다. E-STOP 은 팔로워부터 끕니다.

## 추종 오차 감시

제어의 명령 적분기는 목표 도달 후 쓰기를 멈추는데, 서보가 명령을 놓치면(막힘·보호·유실) 영원히 어긋납니다.

- 실측과 명령 차이가 `TRACK_WARN_DEG`(6°)를 넘으면 그 관절을 빨갛게 표시
- 도달 후에도 `GOAL_REFRESH_S`(1초)마다 같은 목표를 다시 씀
- 1초마다 온도·`Torque_Enable`·부하(OMX 는 `Present_Current`)를 읽어 원인을 가립니다

| Torque_Enable | 부하 | 판정 |
|---|---|---|
| 0 | — | 서보가 보호로 토크를 뺌 — 토크 껐다 켜기, 안 되면 전원 재투입 |
| 1 | 큼 | 기계적으로 막혀 버티는 중 (과열 위험) |
| 1 | ~0 | 명령을 안 받음 (배선·ID·펌웨어) |

## 서보 상태 감시 · 전원 점검

`openmimic_health.py`. 저가 서보 팔에서 실제로 고장 나는 지점(어댑터 혼용, 과열, 다채널 전원 강하)을 시작 전·실행 중에 봅니다.

**시작 전 전원 점검** — 제어(팔 연결 직후), 수집·실행(작업을 띄우기 전, `power_preflight`)에서 팔(보드)마다 서보 전압을 읽습니다.
수집·실행은 `tools_armcheck` / `tools_dxlcheck` 의 `BusIO` 로 포트를 잠깐 열어 읽기만 합니다 (쓰기 없음, 토크 무변경).

| 조건 | 결과 | 근거 |
|---|---|---|
| 서보 `Max_Voltage_Limit` 초과 / `Min_Voltage_Limit` 미만 | 막음 | [EEPROM] 서보 자신의 보호값 |
| SO-ARM101 리더가 12V 계통 (중앙값 7.0 V 이상) | 막음 — 어댑터가 바뀐 것 | [Seeed] 7.0 V 경계 |
| SO-ARM101 팔로워가 5V 계통 / 계통 범위(4.5~5.5, 10.5~13.5 V) 밖 | 경고 | [Seeed] |
| 같은 팔(역할·캘리브 id)의 지난번 전압과 20% 이상 다름 | 경고 | [경험값] `openmimic_power.json` 에 기억 |

OMX(Dynamixel)는 계통 판정 출처가 없어 EEPROM 한계값과 지난번 비교만 씁니다.

**실행 중 감시 (`HealthGuard`)** — 팔로워 버스에서 `PERIOD_S`(1 s)마다 온도·전압·보호 플래그(Feetech `Status`, Dynamixel
`Hardware_Error_Status`)를 `sync_read` 합니다. 반이중 버스라 다른 스레드에서 읽으면 패킷이 섞이므로, 제어 루프와 같은 스레드에서
`get_observation` 직후에 `poll()` 을 부릅니다 (실행 = `openmimic_rollout` 의 `get_observation` 감싸기, 수집 = worker 의 같은 감싸기,
제어 = 팔 루프의 1 초 읽기 자리). 연결 직후 EEPROM 한계값(`Max_Temperature_Limit`·`Temperature_Limit`, 전압 한계)과
기준 전압(첫 측정 중앙값)을 잡습니다.

| 조건 | 수준 | 근거 |
|---|---|---|
| 새로 선 보호 비트 (과열·과부하·과전류·입력 전압 등) | 정지 | [SDK] / [ROBOTIS] — 시작 때 이미 서 있던 비트는 경고만 |
| 온도 ≥ 한계 − 5 °C / ≥ 한계 − 10 °C | 정지 / 경고 | [EEPROM] + [경험값] 여유 (STS3215 공장 한계 70 → 경고 60 = Seeed 권장 상한) |
| 전압 < `Min_Voltage_Limit` 또는 > `Max_Voltage_Limit` | 정지 | [EEPROM] |
| 시작 때보다 20% / 10% 이상 강하 | 정지 / 경고 | [경험값] |

EEPROM 값이 0 이면(읽지 못함·설정 없음) 그 항목은 판정하지 않습니다. 정지하면:
- **실행** — 자기 자신에게 SIGINT (작업 탭 '중지' 와 같은 경로: lerobot 이 시작 자세 복귀·토크 해제)
- **수집** — `q` 와 같은 경로 (녹화 중이던 에피소드는 버리고 끝냄 — 이상 구간을 데이터에 남기지 않음)
- **제어** — 토크는 두고(팔이 떨어지지 않게) 리더 팔로우를 끄고 명령을 현재 위치로 고정, 정지 조건이 풀릴 때까지 슬라이더 명령 무시

가상 팔에서 잰 비용: 실행 제어 주기 29.8 Hz (목표 30). 실제 팔에서의 지연·오탐 여부는 아직 실측 전입니다.

## 수집 worker

`lerobot-record` 는 터미널 키보드로 조작하는데, PTY 로 키를 넣으면 X11 세션에서 pynput 이 가로채 버려집니다.
그래서 `python main.py --worker record <jid>` 가 lerobot `record_loop()` 를 직접 부르고, 웹 버튼이 `events` 를 바꿉니다.
데이터셋 생성·인코딩·저장 흐름과 인코더 기본값은 `lerobot_record.record()` / `DatasetRecordConfig` 와 같습니다.

worker ↔ 웹은 파일로 통신합니다 (`/dev/shm/openmimic/<jid>/`): `status.json`(상태), `cam_<name>.jpg`(미리보기), `cmd`(s/n/r/q).
그래서 OpenMimic 을 재시작해도 진행 중인 수집에 다시 붙습니다.

- **대기(ready)** 는 `record_loop(dataset=None)` — 기록 없이 리더만 따라갑니다. 녹화는 `s` 를 눌렀을 때만
- 단계에 맞지 않는 키는 무시 (`s` 는 대기에서만, `n`/`r` 은 녹화 중에만)
- 프레임 0개 에피소드는 저장하지 않음
- 끝낼 때 팔부터 놓고(팔마다 토크 해제·포트 닫기) 데이터셋을 마무리
- 양팔 이미지 기록 스레드 수는 관측 키로 셉니다 (`robot.cameras` 는 `wrist` 이름이 겹쳐 적게 셈)
- 일시정지는 지원하지 않습니다 — v3.0 은 등간격 타임스탬프 전제
- 대기 중 2초마다 루프를 끊고 온도를 읽습니다 (반이중 버스라 녹화 루프와 동시에 읽으면 안 됨)

## 팔 불량 점검 판정 근거

**SO-ARM101 (`tools_armcheck.py`)**

| 값 | 출처 |
|---|---|
| 보호 비트 1 전압 · 2 각도센서 · 4 과열 · 8 과전류 · 32 과부하 | Feetech SDK `ERRBIT_*` |
| 전압 `Present_Voltage / 10`, 5V 계통 4.5~5.5 V / 12V 계통 10.5~13.5 V (7.0 V 미만이면 5V) | [Seeed_RoboController](https://github.com/Seeed-Projects/Seeed_RoboController) |
| 온도 60 °C 주의, 70 °C 서보 차단, Status 레지스터(65) | 같은 저장소 |
| 엔코더 흔들림 3/10 tick, 튐 400 tick, 쓸기 최소 범위, 온도 편차 8 °C | 경험값 |

**OMX (`tools_dxlcheck.py`)**

| 값 | 출처 |
|---|---|
| 모터 ID·모델 (1060 XL430-W250, 1200 XL330-M288, 1190 XL330-M077) | lerobot `omx_follower`/`omx_leader`, `dynamixel/tables.py` |
| `Hardware_Error_Status`(70) bit0 입력 전압 · bit2 과열 · bit3 모터 엔코더 · bit4 전기 충격 · bit5 과부하 | ROBOTIS DYNAMIXEL X e-Manual |
| 응답 Error 바이트 bit7 = Alert | ROBOTIS Protocol 2.0 |
| 전압은 표시만 (판정 안 함) | — |

모터를 구동하는 시험은 없습니다 (Seeed `servo_register_diag.py --move` 는 조립된 팔에 쓰면 안 됩니다 — 목표 없이 토크를 켜고
절대 위치로 보냄).

## 보안·동시성

- 인증 기본 꺼짐 (`OPENMIMIC_TOKEN` / `OPENMIMIC_AUTH=on` 으로 켬)
- 다른 사이트에서 온 POST·WebSocket 은 403 (`Sec-Fetch-Site: cross-site` 또는 `Origin` 호스트 불일치) — CSRF 방지
- 팔·GPU 를 잡는 시작 요청(`/api/record`, `/api/calib/start` …)은 한 번에 하나씩 처리 (두 번 누름 경쟁 방지)
- record/rollout/train/Control/Setup 세션은 서로 배타 (학습 + Control 은 동시 허용)
- 데이터셋 사용 중 판정은 작업 spec·CLI 인자를 정확히 비교 (녹화 중 삭제 방지)
- JSON 파일은 임시 파일 + `os.replace` 로 원자적으로 씀
- 작업 PID 재사용 방지 (`/proc/<pid>/stat` starttime 비교)

## OpenVINO (Intel NPU / GPU / CPU)

목표: Intel Core Ultra(Meteor Lake 이상)의 NPU 로 ACT·ACT2 체크포인트를 추론 (학습은 Intel GPU·HF Jobs·NVIDIA 기기 어디서든).

**바꾸는 범위는 신경망 하나뿐입니다.** lerobot 체크포인트에서 정규화/역정규화는 모델 밖
(`policy_preprocessor*.json` / `policy_postprocessor*.json`)에 있으므로, 그 파이프라인은 lerobot 것을 그대로 쓰고
`ACTPolicy.model` 의 추론 경로만 IR 로 바꿉니다 (ACT2 는 `predict_action_chunk` 만 바꿔 청크 보간·큐는 PyTorch 코드 그대로). 관절 순서·단위·카메라 키가 PyTorch 경로와 같다는 게 보장됩니다.

변환 (`openmimic_ov.py convert`)
- `ACTCore(state, [env_state], *images) → actions (1, chunk, A)` 로 감싼 뒤 `openvino.convert_model` (정적 shape).
  추론 때 VAE 인코더는 안 쓰므로(잠재 = 0) IR 에도 없습니다.
- 정적 shape = 학습 데이터의 `input_features` shape. NPU 는 동적 shape 를 못 받습니다.
- FP16 가중치로 저장(`compress_to_fp16`). `--int8` 이면 NNCF `quantize(model_type=TRANSFORMER)` — 보정 데이터는
  학습 데이터셋 64 프레임을 preprocessor 에 통과시킨 것. 데이터셋을 못 찾으면 INT8 은 만들지 않습니다(임의 입력 보정은 의미 없음).
- 검증: 데이터셋 8 프레임(없으면 정규분포 임의 입력)으로 PyTorch CPU 출력과 비교. 정규화 공간 최대 오차 ≤ 0.05(FP16) / 0.25(INT8)
  이면 `parity`. 화면에는 postprocessor 로 되돌린 **관절 단위** 오차를 보여 줍니다. p95 ≤ 1000/fps 면 `realtime`.
- 산출물 `<pretrained_model>/openvino/` : `act_fp16.*`, `act_int8.*`, `ov_meta.json`(입력 이름·shape, 장치별 결과,
  `model.safetensors` 의 크기·mtime 지문), `cache/`(컴파일 캐시 — NPU 첫 컴파일 이후 빨라짐).
- 모델 생성 시 `pretrained_backbone_weights=None` — ImageNet 가중치를 내려받지 않습니다(어차피 체크포인트 가중치로 덮어씀).

추론 (`openmimic_ov.py rollout --ov.* <lerobot-rollout 인자>`)
- 같은 프로세스에서 `lerobot.scripts.lerobot_rollout.main()` 을 그대로 실행하고 `ACTPolicy.predict_action_chunk` 만 교체합니다.
  `select_action` 의 액션 큐·`n_action_steps`·temporal ensemble, `max_relative_target`, 중지(SIGINT) 시 시작 자세 복귀 + 토크 해제는
  PyTorch 경로와 같은 코드입니다.
- IR 컴파일은 로봇 연결 **전**에 끝냅니다(NPU 첫 컴파일이 길어도 팔이 연결된 채 멈춰 있지 않게).
- 장치 대체: 요청 장치 → 그보다 뒤의 NPU → GPU → CPU. 대체되면 로그에 `!!` 로 남깁니다.
- `--device=cpu`, `--policy.pretrained_backbone_weights=null` 을 붙입니다. 후자가 없으면 오프라인 기기에서 torchvision 다운로드로 죽습니다
  (PyTorch 경로는 기존 동작 유지).
- 입력 shape 가 변환 때와 다르면 첫 추론에서 예외 → lerobot teardown(시작 자세 복귀) 후 종료. OpenMimic 은 그 전에
  `ov_shape_problem()` 으로 Setup 카메라 이름·해상도를 대조해 시작 자체를 막습니다.

OpenMimic 쪽
- `openvino` 는 OpenMimic 프로세스에서 import 하지 않습니다. 장치 조회도 `openmimic_ov.py devices` 를 띄워서 합니다(2분 캐시) —
  웹 프로세스가 NPU 를 붙잡지 않게, 그리고 openvino 가 없는 Thor 에서도 그대로 뜨게.
- 작업 종류 `ovconvert` (spec 에 체크포인트). 롤아웃·다른 변환과 동시 실행 금지(지연 측정이 틀어지고 IR 을 덮어씀).
  OV 롤아웃은 작업 종류가 그대로 `rollout` 이라 배타·중지·체크포인트 삭제 보호가 기존 규칙을 따릅니다.
- `/api/ov/status?ckpt=` · `/api/ov/convert` · `/api/rollout` 의 `engine: torch | ov:NPU | ov:GPU | ov:CPU`, `precision: fp16 | int8`.

## 롤아웃 실행기·실시간 모니터·시도 기록

추론 중 카메라·3D 와 함께 관절 명령값·지연을 보여 주고, 시도마다 성공/실패를 기록해 체크포인트별 실기 성공률을 쌓습니다.

- 모든 롤아웃(PyTorch/OpenVINO)은 `openmimic_rollout.py` 로 띄웁니다. `lerobot.scripts.lerobot_rollout.main()` 을 그대로 부르고
  세 곳만 감쌉니다 — 관찰만 하고 로봇에 가는 명령은 바꾸지 않습니다.
  - `lerobot.rollout.context.make_robot_from_config` → 로봇 인스턴스의 `get_observation`(카메라 프레임·관절 실측·제어 주기),
    `send_action`(마지막 명령), `connect`(단계 표시)
  - `SyncInferenceEngine.get_action` → 틱별 시간. 최근 5 초 중앙값 = 보통 틱(큐에서 꺼내기), 최대 = 청크 계산
  - `RolloutStrategy._teardown_hardware` → "시작 자세로 복귀 중" 단계
- 별도 스레드가 10 fps 로 `status.json`·`cam_*.jpg` 를 원자적으로 씁니다 (수집 worker 와 같은 규칙, 제어 루프에 인코딩 비용 없음).
  웹은 `/api/rollout/status/<jid>`, `/api/runstream/<jid>/<cam>`(MJPEG) 로 읽습니다.
- ACT·Diffusion 체크포인트면 `--policy.pretrained_backbone_weights=null` 을 붙입니다. 모델을 만들 때 torchvision 이
  ImageNet 가중치를 내려받는데 곧바로 체크포인트 가중치로 덮어써져 쓸모가 없고, 오프라인 기기에서는 여기서 죽습니다.
  BatchNorm/GroupNorm 구조는 `use_group_norm` 이 따로 정하므로 구조는 같습니다.
- 시도 기록: `POST /api/rollout/trial {job, result: success|fail|undo}` → `openmimic_trials.json` 의 체크포인트(outputs 기준 상대경로) 목록에 추가.
  작업 id·엔진·환경을 같이 남깁니다. 끝난 실행도 `/rollout?job=<jid>` 로 다시 열어 기록할 수 있습니다.
- 화면 갱신은 앞 요청이 끝난 뒤 다음 요청(0.5 s) — 서버가 느려도 요청이 쌓이지 않고, 늦게 온 옛 응답은 버립니다.

## 모델·데이터셋 내보내기/가져오기, 이어서 학습, 정책

- **내보내기** `GET /api/model/download?ckpt=` — `pretrained_model` 만(학습 상태 제외), tar 안 경로를 `<run>/checkpoints/<step>/pretrained_model`
  로 맞춰 받는 쪽에서 같은 구조로 풀리게 합니다. `openvino/cache`(장치별 컴파일 캐시)는 뺍니다. 데이터셋은 기존 `/api/download/<ds>`.
- **가져오기** `POST /api/import/{model|dataset}?filename=` — 원본 바이트를 그대로 스트리밍(python-multipart 의존성 없음).
  - 업로드 크기는 여유 공간의 45% 이하(압축 파일 + 풀린 내용), 풀린 총량은 여유 공간의 90% 이하
  - 절대경로·`..`·심볼릭/하드 링크·장치 파일은 거부, tar 는 `filter="data"` 로 풂. 임시 폴더(`.import_*`)는 끝나면 지움
  - 모델: `config.json + model.safetensors` 폴더를 찾아 배치, 이름이 겹치면 `_2`. 묶음 안의 OpenVINO 변환본은 가중치 크기가 같을 때만
    지문을 새로 찍어 "다시 변환 필요" 로 보이지 않게 함. 데이터셋: `meta/info.json` 이 있는 최상위 폴더
  - 열린 프로젝트가 있으면 그 프로젝트에 넣음
- **이어서 학습** `POST /api/train/resume {run, steps}` — `lerobot_train --config_path=<run>/checkpoints/last/pretrained_model/train_config.json
  --resume=true --steps=<목표> --output_dir=<run>`. `last/training_state` 가 있어야 합니다(가져온 모델은 없음).
- **정책** `TRAIN_POLICIES` 표: ACT(`--policy.type=act`), Diffusion(`--policy.type=diffusion`, `diffusers` 필요),
  SmolVLA(`--policy.path=lerobot/smolvla_base`, `transformers`·`num2words` 필요). 시작 전에 `importlib.util.find_spec` 으로 패키지를
  확인해 lerobot 이 수십 초 뒤 ImportError 로 죽기 전에 설치 명령을 알려 줍니다. ACT2(`--policy.type=act2`)는 이 레포의 플러그인이라
  `lerobot_policy_act2` 가 없으면 설치 명령을 보여 주고 막습니다. OpenVINO 변환은 ACT·ACT2 만.
- **학습 그래프** — lerobot 로그 줄의 `loss`·`grdn`·`lr`·`smp/s` 를 읽어 loss, grad norm·학습률 그래프와 진행률·남은 시간
  (남은 step ÷ (samples/s ÷ batch))을 보여 줍니다.

## 학습 정책별 처리 (`TRAIN_POLICIES` · `policy_train_args`)

lerobot e40b58a 의 정책 19종을 조사해 원격조작 데이터 → 모방학습 흐름에 맞고 CUDA 없이도 학습·실행되는 3종(ACT · Diffusion · SmolVLA)과
이 레포의 ACT 개선 실험 플러그인 ACT2 를 화면에 둡니다. TD-MPC(보상 필요·정사각 이미지)·gaussian_actor(온라인 RL, lerobot-train 불가)는 실측으로 제외,
X-VLA·MolmoAct2·pi0 계열·GR00T 등 CUDA 전용 대형 모델은 Intel PC 에서 학습·실행할 수 없어 넣지 않습니다.

`policy_train_args(pol, ds_root, opts)` 가 데이터셋 `meta/info.json` 을 보고 인자를 만들고, lerobot 이 모델을 내려받은 **뒤에야** 내는 오류를
시작 전에 막습니다. 로컬·HF Jobs·화면 점검(`/api/train/check`)이 같은 함수를 씁니다.

- **Diffusion**: `DiffusionConfig.validate_features` 가 resize 설정과 관계없이 카메라 해상도가 모두 같아야 통과시킵니다 → 미리 거부.
  `ddim` 선택지(기본 켬)는 `--policy.noise_scheduler_type=DDIM --policy.num_inference_steps=10`. 시험 서버(CPU 4코어)에서 동작 묶음 하나가
  23 s(DDPM 100회) → 2.4 s 로 줄었습니다.
- **ACT2**: 선택지 `aug` → `aug_shift=0.06 aug_color=0.2 state_dropout=0.1`, `interp` → `n_action_steps=30 interp_steps=10`,
  `dino` → `vision_backbone=dinov2_small image_size=224`(`transformers` 필요), `flow` → `action_head=flow use_vae=false flow_steps=5`.
  아무것도 안 켜면 ACT 와 같은 모델이라 같은 데이터·step 으로 나란히 비교할 수 있습니다.
- **ImageNet 백본**: ACT·Diffusion 은 학습 시작 때 torchvision ResNet18 가중치를 `download.pytorch.org` 에서 받습니다.
  `nobb` 선택지는 `--policy.pretrained_backbone_weights=null`. 캐시(`$TORCH_HOME/hub/checkpoints/resnet18-f37072fd.pth`)가 없으면 경고.
- **장치 확인**: OpenMimic 본체는 torch 를 import 하지 않으므로 `local_cuda()` 가 하위 프로세스로 `torch.cuda`·`torch.xpu` 를 묻고
  10분 캐시합니다. 학습 탭 점검에 학습 장치(GPU / XPU / CPU)를 표시만 하고 막지는 않습니다.

### 롤아웃 카메라 연결 (`rollout_rename_map`)

1. 체크포인트 `train_config.json` 에 `rename_map` 이 있고 그 카메라가 지금 셋업에 있으면 그대로 `--rename_map` 으로 넘깁니다.
2. 아니면 정책 카메라(`image_keys` 또는 VISUAL 입력)가 셋업에 없을 때 `cam_order` 순(전경 → 손목)으로 연결합니다 — 허브에서 받은,
   카메라 이름이 다른 모델을 쓰는 경우. 카메라 수가 모자라면 연결하지 않고 오류.

`policy_fit` 은 이 연결로 카메라를 대조하고, 스스로 resize 하는 정책(`RESIZING_POLICIES`)은 해상도 경고를 건너뜁니다.
허브에서 받은 CUDA 용 모델(`GPU_POLICIES`)·디노이징 많은 Diffusion 을 GPU 없는 기기에서 고르면 경고합니다.

## Hugging Face Hub · HF Jobs

구현은 `openmimic_hub.py`.

- 토큰: `huggingface_hub.login(add_to_git_credential=False)` — `HF_HOME/token`(activate.sh 가 `data/hf` 로 지정). `/whoami-v2` 는 사용량
  제한이 있어 5 분 캐시, 하드웨어 목록(`HfApi.list_jobs_hardware`, 로그인 없이도 됨)은 10 분 캐시. `unit_cost_usd`·`unit_label` 로 $/h 계산.
- 오래 걸리는 일은 작업으로: `hub`(push-dataset / pull-dataset / pull-model), `cloudtrain`.
- **클라우드 학습**: lerobot e40b58a 의 원격 학습(`lerobot-train --job.target=<flavor>`)을 씁니다. 그 경로의
  `ensure_dataset_available` 은 `HF_LEROBOT_HOME/<repo_id>` 의 로컬 데이터셋을 그 repo id 로 올리는데, OpenMimic 데이터셋은 `local/<이름>` 이라
  남의 네임스페이스(`local`)로 올리려다 실패합니다. 그래서 `cloud-train` 이 먼저 `LeRobotDataset(<user>/<이름>, root=...).push_to_hub(private=True)`
  로 내 계정에 올리고(매번 — 이어서 수집한 에피소드 반영), `--dataset.repo_id=<user>/<이름>` 으로 `lerobot_train` 을 **exec** 합니다
  (PID 유지 → 작업 추적·중지 그대로). `--output_dir`·`--dataset.root` 는 뺍니다(파드에 없는 경로). `--save_checkpoint_to_hub=true`,
  `--job.tags=["openmimic"]`.
- lerobot 은 Ctrl-C 를 "로그 분리" 로 처리해 원격 학습이 계속 과금됩니다. `kill_job` 이 `cloudtrain` 이면 로그의 `Job submitted: <id>` 를 찾아
  `HfApi.cancel_job` 도 부릅니다. 작업 종류를 `train` 과 나눈 이유: 이 기기 GPU·팔을 안 쓰므로 수집·추론·로컬 학습과 배타가 아님.
  학습 그래프(`/api/trainlog`)는 원격 로그 줄이 같은 형식이라 그대로 그려집니다.
- **모델 받기**: repo 파일 목록에서 `checkpoints/<step>/pretrained_model/config.json` 을 찾아 마지막(또는 지정) step 만
  `snapshot_download(allow_patterns=...)`, 없으면 루트의 `config.json + model.safetensors` 를 step `hub` 로. `outputs/<repo이름>/checkpoints/<step>/`
  에 놓고 `openmimic_import.json` 에 출처 기록.
- **데이터셋 받기**: `LeRobotDataset(repo_id, root=임시)` 로 받아(코드베이스 버전 태그 기준) 끝나면 `DATA_ROOT/<이름>` 으로 옮깁니다.
  실패하면 임시 폴더를 지웁니다.
- 실패 로그 끝에 원인 한 줄(네트워크면 `huggingface.co`·`*.xethub.hf.co` 접속 확인, 권한이면 토큰).

## 데이터셋 복구·사전 점검·수집 신호음 등

- **데이터셋 복구** (`tools_dsrepair.py`, LeLab 코드 기반): `finalize()` 전에 끊긴 v3.0 데이터셋은 `meta/episodes/` 가 없어 열리지 않습니다.
  읽을 수 있는 parquet·영상 길이로 색인을 다시 만들고, 꼬리 없는 parquet 는 `.unreadable` 로 치우고, 잃은 에피소드가 있으면 data 를 잘라내고
  stats 를 다시 계산합니다. OpenMimic 쪽 변경: 백업(`DATA_ROOT/.repair_backup/<이름>_<시각>/` — 폴더 밖이라 다운로드·업로드에 안 섞임), 작업(`repair`)으로 실행,
  목록에서 v3.0 이고 수집 중이 아닌데 `meta/episodes/*.parquet` 가 없으면 **마무리 안 됨** 표시.
- **롤아웃 사전 점검** `policy_fit()`: 체크포인트 `config.json` 의 `input_features` 로 카메라 이름(없으면 거부)·해상도(다르면 경고)·
  `observation.state` 차원(한팔 6 / 양팔 12, 다르면 거부)을 대조. 가져온 모델처럼 학습 데이터셋 정보가 없어도 됩니다.
  언어 정책(smolvla·pi0·pi05 등)은 태스크 설명 필수.
- **실패 원인 추정** `FAILURE_HINTS`: 로그 끝의 예외 문구 → 한국어 안내 (정규식 표, 위에서부터 첫 일치).
- **캘리브레이션 프롬프트**: lerobot 이 모터 값 ≠ 파일일 때 `input()` 으로 묻는 것을 `openmimic_rollout.py` 가 "파일을 모터에 쓰기"(ENTER) 로만 답합니다.
  제어 탭 연결과 같은 동작. 처음부터 하는 캘리브레이션 질문이면 `EOFError` 로 멈추고 캘리브레이션 탭 안내.
- **수집 신호음·키**: WebAudio 사인파(파일 없음). 녹화 시작 660→880 Hz, 저장 660→440 Hz, 최대 길이 마지막 3 초 880 Hz.
  Space/→ = 다음 단계, ←/Backspace = 버리고 다시, Esc = 수집 끝내기(대기 중). 음소거는 localStorage.
- **리뷰**: 재생 속도(영상 `playbackRate` + 벽시계 보정), `,` `.` 한 프레임.
- **패키지 설치 버튼** `/api/install/<정책>`: `lerobot[extra]` 대신 그 extra 의 패키지만(e40b58a pyproject 범위) 설치 — 소스 설치가 아닌 환경에서
  PyPI 의 다른 lerobot 이 덮어쓰지 않게. torch·torchvision 은 `-c` 제약 파일로 지금 버전 고정. 끝나면 `importlib.invalidate_caches()` 로 재시작 없이 반영.
- **카메라 FOURCC**: `fourcc: MJPG|YUYV` (선택). Control 미리보기·수집 worker·롤아웃 CLI 모두 전달하고, 안 정했으면 아예 넘기지 않아 예전과 같습니다.

## 화면 구조 — 탭·첫 실행·막힘 안내

- **탭 순서**(`nav_html`): 작업 순서대로 셋업 · 캘리브레이션 · 제어 · 수집 · 데이터셋 · 학습 · 모델 · 실행. 자주 안 쓰는 프로젝트 · 허브 · 작업은
  `<details class=navmore>` **더보기** 메뉴에 둡니다(바깥을 누르면 닫힘). `.nav` 는 줄어들 수 있고(`min-width:0`) 상태 칩은 줄지 않아,
  좁은 화면에서 탭이 칩 밑으로 숨지 않습니다. 1750px 아래에서는 칩 이름표(`.clab`)를, 1300px 아래에서는 기종 표시를 숨깁니다.
- **상태 칩**: 프로젝트 · 환경 · 한팔/양팔 · 포트 미설정 · 작업 상태. 작업 종류는 `JOB_KIND_LABEL` 의 한국어 이름(대기·학습·수집…)으로 보입니다.
- **첫 실행**: `/` 는 포트 미설정 · 데이터셋 없음 · 모델 없음이고 이 브라우저가 마법사를 연 적이 없으면(쿠키 `openmimic_wiz`) 셋업 마법사로 보냅니다.
  그다음부터는 데이터셋 화면 위에 **시작하기**(`getting_started()` — 포트 · 캘리브레이션 파일 · 에피소드 있는 데이터셋 · 체크포인트 · 실행 기록)가
  남은 단계를 보여 줍니다. 다 끝나면 사라지고, 숨기면 쿠키 `openmimic_gs=hide`.
- **막힘 안내**(`need_html`): 먼저 할 일이 남으면 그 화면의 시작 버튼을 끄고 다음에 갈 곳을 큰 버튼으로 보여 줍니다 —
  포트 미설정(수집·실행·제어·캘리브레이션 → 셋업 마법사), 캘리브레이션 파일 없음(제어), 데이터셋 없음(학습 → 수집), 체크포인트 없음(실행 → 학습).
  서버 쪽 검사(`/api/record` 등)는 그대로 두고 화면에서 한 번 더 막는 것입니다.
- **자리 옮김**: 체크포인트 삭제는 실행 화면에서 빼고 모델 화면에만 둡니다(시작 버튼 옆의 파괴적 버튼 제거). OpenVINO 변환은 학습 화면에서
  모델 화면으로 옮겼습니다 — 체크포인트 줄의 **OpenVINO 변환** 버튼이 아래 변환 칸에 그 체크포인트를 골라 줍니다.
- **셋업 화면**: 맨 위가 마법사 안내 카드이고, 모터 ID 세팅(2b) · 팔 불량 점검(2c) · 포트 경로 설명 · 설정 JSON 은 `<details>` 로 접혀 있습니다.
- **캘리브레이션 폴더 경고**(`calib_root_warning`): `HF_LEROBOT_CALIBRATION`·`HF_LEROBOT_HOME` 을 일부러 주지 않았는데
  `CALIB_ROOT` 가 `설치 폴더/data/hf/lerobot/calibration` 이 아니면, 모든 화면 위 경고 띠와 시작 로그(`!!`)로 알립니다.
  `activate.sh` 없이 `python main.py` 로 켜면 lerobot 이 `~/.cache/huggingface` 를 써서 다른 캘리브레이션 파일을 조용히 읽기 때문입니다.
- **실행 도구**(`openmimic.sh`): 항상 `activate.sh` 로 환경을 켠 뒤 `main.py` 를 띄워 위 문제가 생기지 않게 합니다. pid 는
  `openmimic_server.pid`, 로그는 `openmimic.log`. 자동 시작은 `~/.config/systemd/user/openmimic.service` (이 계정만, `Restart=on-failure`).
  끌 때 SIGTERM 만 보내므로 `start_job` 이 새 세션으로 띄운 수집·학습 작업은 그대로 돌고, 다시 켜면 작업 목록에 보입니다.
  시작 로그의 접속 주소는 `hostname -I`(없으면 기본 경로의 내 주소)로 찾습니다.
- **모바일**(820px 이하): 넓은 표는 카드 안에서만 가로로 밀리고(`.card{overflow-x:auto}`), 긴 경고 배지(`p.badge`)는 줄바꿈합니다.
  상태 칩은 포트 미설정 · 작업 상태 · 한/EN 만 남깁니다. 390px 폭에서 전 화면 가로 넘침 0 을 확인했습니다.

## 가상 장치 (화면에서 켜기)

팔·카메라 없이 셋업부터 실행까지 해 보는 기능. `POST /api/sim/start {robot, mode}` 가 `tools_simarms.py` 를 작업(`sim`)으로 띄우고
`openmimic_sim.json` 이 생길 때까지 기다린 뒤, 환경을 `sim-<기종>-<모드>` 로 바꿉니다.

- 켜기 전 환경을 `sync_active_env()` 로 그 환경 파일에 보관하고, 가상 환경에 `sim_prev_env` 로 적어 둡니다.
  `POST /api/sim/stop` 은 가상 팔을 쓰는 연결·작업을 정리하고 그 환경으로 되돌립니다. 가상 장치가 죽은 채 가상 환경이 남아 있으면
  셋업 화면이 '다시 켜기 / 전에 쓰던 환경으로' 를 보여 줍니다.
- 포트는 `sim://<side>_<role>` 이라 껐다 켜도 이름이 같고, 캘리브 id 는 `sim_follower` 처럼 따로 둬 실제 팔 파일과 섞이지 않습니다.
- **가상 시리얼** (`openmimic_fakeserial`): 보드 레지스터(캘리브레이션 값·토크)와 MuJoCo 장면은 서버·수집 worker·롤아웃이 함께 봐야 하므로
  가상 팔 도구 프로세스 하나에 둡니다. 각 프로세스는 `install()` 로 `serial.Serial` 자리에 생성 함수를 두어, `sim://` 포트일 때만
  `FakeSerial` 이 `127.0.0.1` 의 가상 팔 도구에 접속합니다 (`OMSIM2 <토큰> <보드>` 인사 뒤로는 `4바이트 길이 + 내용` 틀 단위 — write 하나에 응답 틀 하나, 답할 패킷이 없으면 빈 틀, 연결마다 따로 응답).
  응답 틀이 오기 전의 read 는 그 틀을 최대 2초 기다립니다. 가상 장치 프로세스가 잠깐 늦어도(Windows 스레드 전환·GIL)
  servo SDK 의 응답 제한 시간(약 34 ms)에 걸려 `There is no status packet` 으로 끊기지 않게 하려는 것입니다.
  scservo_sdk · dynamixel_sdk 가 `serial.Serial(...)` 을 그 자리에서 만들기 때문에 lerobot 코드는 그대로입니다. PTY·com0com 같은 OS 기능이 없어
  Linux · Windows 가 같은 경로를 탑니다. 예전 PTY 판과 같은 시험(가상 장치 전 구성)을 모두 통과합니다.
- 가상 팔 도구는 보드 잠금 안에서 레지스터만 읽고 MuJoCo 호출(월드 잠금)은 그 밖에서 합니다. 두 잠금을 겹쳐 잡으면 에피소드 사이 시연 계획(이 서버에서 약 80 ms)
  동안 시리얼 응답이 멈춰, lerobot 이 응답 제한 시간(약 35 ms)을 넘겨 `There is no status packet` 으로 수집이 끊겼습니다 (예전 PTY 판에서도 재현).
- 명령 파일(`openmimic_sim/cmd`, 수집 worker 의 `cmd`)은 이름을 바꾼 뒤 읽고 지웁니다(`P.take_file`) — Windows 는 쓰는 쪽이 연 파일을 지울 수 없어
  '읽고 → 지우기' 면 같은 명령이 다시 실행됩니다.
- 화면 아래 막대 → `POST /api/sim/cmd` → `openmimic_sim/cmd` 에 한 줄씩 (`w <보드>` 쓸기, `a on|off` 리더 자동, `v <보드> <V>`, `t <보드> <°C>`).
  전압·온도 명령은 서보 상태 감시를 시험하는 용도입니다.
- 가상 카메라(`openmimic_simcam`)는 `openmimic_sim/cam_<이름>` 경로를 연 쪽에서 합성 영상을 만듭니다 (서버·수집 worker 는 `main.py` import 때,
  실행은 `openmimic_rollout.main()` 에서 설치). `cv2.VideoCapture` 를 Python 에서 **상속하면** OpenCV 바인딩의 객체 해제와 맞지 않아
  제어 탭을 빠르게 열고 닫을 때 서버가 segfault 로 죽었습니다 (4회 중 2회 재현). 그래서 상속하지 않고, 가상 경로면 별도 객체·아니면
  원래 `cv2.VideoCapture` 객체를 돌려주는 생성 함수 + `isinstance` 를 통과시키는 메타클래스로 바꿨습니다 (같은 시험 5회 모두 정상).

### 물리 시뮬레이션 (MuJoCo)

`openmimic_mujoco.py`. SO-ARM101 한팔이고 `mujoco` 가 있으면 `tools_simarms.py --physics auto` 가 팔로워 서보의 '몸' 을
MuJoCo 로 바꿉니다. 서보 프로토콜(가상 시리얼 `sim://`)은 그대로라 OpenMimic·lerobot 쪽 코드는 실제 팔과 같은 경로를 탑니다.

- **모델**: `urdf/so101_new_calib.xml` (TheRobotStudio SO-ARM100 의 MJCF, Apache 2.0 — `meshdir` 만 `urdf/` 로).
  책상·물체·목표·카메라는 `MjSpec` 으로 불러올 때 붙입니다. 그리퍼·턱 말고는 충돌을 꺼서 계산량을 줄였습니다.
- **틱 ↔ 관절각**: `rad = (틱 − 2048) × 2π / 4096`. 팔로워 목표(Goal_Position + Homing)가 위치 액추에이터 명령(STS3215 값:
  kp 998, 힘 제한 ±2.94 N·m — MJCF 원본), 관절각이 엔코더 값(Present_Position)이 됩니다. 토크가 꺼진 관절은 지금 자세를 유지하고,
  마법사 '움직이기' 는 관절을 바로 놓습니다(텔레포트). 쓸기 범위는 MJCF 관절 범위라 리더·팔로워 캘리브레이션 범위가 같아집니다.
- **렌더**: 별도 스레드가 top · wrist 를 그려 `frame_dir()/cam_<이름>.rgb`(Linux `/dev/shm/openmimic_sim`, Windows 임시 폴더 · 머리 16 B + RGB)에 쓰고,
  `openmimic_simcam` 이 그 파일을 읽어 카메라로 내줍니다. 백엔드는 별도 프로세스로 시험해 하드웨어 EGL → OSMesa 순으로 고릅니다
  (MuJoCo 는 import 할 때 백엔드를 정함, 소프트웨어 EGL 은 OSMesa 보다 느림). 이 개발 서버(CPU 전용)는 OSMesa 로 320×240 한 장 약 80 ms.
- **그립 보조**: 턱 충돌 모양이 메시의 볼록 껍질이라 마찰만으로는 큐브가 밀려납니다. 그리퍼가 닫히는 중이고 큐브 중심이
  끝점(`gripperframe`) 3 cm 안이면 큐브를 그리퍼 좌표계에 고정하고, 다시 열리면 놓습니다.
- **자동 시연**: IK(끝점 위치 + 그리퍼가 아래를 향하게, 감쇠 최소제곱)로 경유점(위 → 내려가 → 닫기 → 들기 → 목표 위 → 내려놓기 → 열기 → 시작 자세)을
  만들고, 리더 엔코더를 그 궤적대로 움직입니다. 수집 worker 는 자동 녹화일 때 대기 중 `reset`, 녹화 시작에 `demo` 를 보냅니다.
  무작위 큐브 위치 20개에서 시연 성공 20/20.
- **판정**: 큐브가 초록 칸 안 + 책상 위 + 그리퍼가 놓음. 실행 시작 때 `reset` 을 보내고 그 뒤의 성공만 셉니다(`resets` 카운터).
  `/api/rollout/status` 가 성공이면 시도를 기록하고 실행을 끝내며(SIGINT), 성공 없이 끝나면 실패로 기록합니다 (`auto: "sim"`).
- **학습 속도**: 가속 장치 없는 CPU 4코어에서 ACT(카메라 2개 320×240, batch 8)는 스텝당 약 14 초입니다 (시뮬레이션 렌더와 CPU 를 나눠 씀).
  시뮬레이션 데이터라도 실제로 쓸 만한 학습은 Intel GPU(XPU) 같은 가속 장치나 HF 작업이 필요합니다.
- **확장**: `ROBOTS[이름]`(MJCF·관절·끝점·그리퍼 각도·시작 자세·카메라·틱 환산)과 `TASKS[이름]`(`Task` 상속:
  build · bind · reset · plan · step · judge)을 따로 둡니다. 다른 로봇은 MJCF(또는 MuJoCo 가 읽는 URDF + 위치 액추에이터)와 프로필,
  다른 과제는 `Task` 하나를 추가하면 수집·실행 화면이 그대로 씁니다. 로봇의 서보 프로토콜이 Feetech / Dynamixel 이 아니면
  `tools_simarms.py` 에 보드 흉내를 추가해야 합니다.

## 한/영 전환 (i18n)

화면 문자열은 소스에 한국어로 그대로 둡니다. 영어 모드(쿠키 `openmimic_lang=en`, 기본값은 `OPENMIMIC_LANG`)면 서버가 응답을 내보내기
직전에 사전 `openmimic_i18n_en.json` 으로 한국어 조각을 바꿉니다. 화면마다 번역 키를 다는 방식보다 소스 변경이 적고,
번역이 빠진 곳은 한국어로 남을 뿐 깨지지 않습니다.

- **어디서**: HTTP 미들웨어(`i18n_middleware`)가 `text/html`·`application/json` 응답을, Control WebSocket 은 `send_text` 를 감싸서 바꿉니다.
  영상·MJPEG·다운로드는 건드리지 않습니다. `/lang` 은 쿠키를 뒤집고 같은 사이트의 직전 경로로 되돌립니다(외부 referer 는 `/`).
- **어떻게**: 사전 키를 길이 내림차순으로 묶은 정규식 하나로 한 번에 치환합니다. 키는 소스 문자열 리터럴(주석·docstring 제외)을
  따옴표·태그·`{}`·이스케이프·`+=;|` 경계로 잘라 만든 조각이라, 화면 문자열은 조각 단위로 정확히 맞습니다. 사전 파일이 바뀌면
  (mtime) 다시 읽습니다.
- **문법 안전**: 사전 값에는 따옴표(`'` `"` `` ` ``)·역슬래시·`<` `>` `{` `}`·줄바꿈을 넣지 않습니다 — JS 문자열·JSON·HTML 속성 안에서 치환해도 문법이 깨지지
  않습니다. `\n` 이스케이프 뒤의 조각은 키가 `n삭제할까요` 처럼 `n` 으로 시작하므로 값도 `n` 으로 시작해야 합니다.
  `tools_jscheck.py --en` 으로 치환 후 JS 를 검증합니다.
- **띄어쓰기 보정**: 한국어는 조사를 닫는 태그·영문 바로 뒤에 붙여 쓰므로(`</b>을 누르면`, `ACT로`) 치환 결과 앞에 공백을 넣습니다
  (`</…>`·`)]}`·영숫자 뒤, 또는 따옴표 뒤인데 키가 조사로 시작할 때). 뒤가 영숫자면 뒤에도 넣습니다.
- **사용자 글 보호**: HTML 의 `value="…"` 속성과 `<textarea>` 내용, JSON 의 `task`·`tasks`·`default_task`·`config`·`note(s)`·`memo`·`desc(ription)`
  키 아래 값은 바꾸지 않습니다(`I18N_KEEP_KEYS`). 영어 화면에서 불러와 그대로 저장해도 한국어 태스크 설명이 망가지지 않습니다.
- **짧은 낱말**: 탭 이름·작업 상태·페이지 제목·표 머리·입력칸 이름표·작은 제목·시작하기 단계 이름·탭 링크
  (`<span class=tl>` · `<span class=jobk>` · `<h2>` · `<th>` · `<label class=f>` · `<p class=eyebrow>` · `<b class=gsname>` · `<a …>`)이
  `NAV_EN` 의 낱말과 **통째로 같을 때만** 그 자리를 바꿉니다. 사전에는 한 낱말짜리 키를 새로 넣지 않습니다.
  `수집`·`실행` 같은 낱말을 사전에 넣으면 번역이 빠진 문장 속 낱말까지 바뀌기 때문입니다.
- **화면 문자열을 고치면** 사전도 고칩니다(CLAUDE.md). 새 조각은 소스에서 다시 뽑아 사전에 없는 키만 번역해 넣으면 됩니다.

## Intel 기기 학습 (XPU, 실험적)

- `lerobot_conda.sh`: `intel` 플랫폼에서 `OPENMIMIC_TORCH=xpu` 면 `download.pytorch.org/whl/xpu` → `whl/cpu` → PyPI 순으로 torch 를 받습니다.
  받은 휠의 꼬리표(`TORCH_LOCAL`)를 기억해 두고, 4-4 재확인에서 XPU 휠이 다른 휠로 바뀌었으면 멈춥니다. 제약 파일은 꼬리표를 떼므로
  (`torch==2.11.0`) 이후 lerobot·정책 패키지 설치가 XPU 휠을 그대로 유지합니다 (PEP 440 로컬 버전 규칙, CUDA 휠과 같은 원리).
- OpenMimic 은 학습 인자를 바꾸지 않습니다. lerobot-train 의 Accelerate 가 CUDA → XPU → CPU 를 자동 감지하고,
  Thor 에서 학습한 체크포인트(`device: cuda`)도 `PreTrainedConfig.__post_init__` 이 쓸 수 있는 장치로 바꿉니다.
- `local_cuda()` 는 CUDA 가 없을 때 `torch.xpu` 도 물어 학습 탭 점검에 장치를 표시합니다.
- 실기(Ubuntu 26.04, Core Ultra 내장 GPU): XPU 휠(torch 2.11.0+xpu, intel-opencl-rt·oneMKL 등 pip 런타임 동반) 설치 후 `torch.xpu.is_available()` True 확인.
  ACT 52M · batch 8 학습이 XPU 로 돌고(`'device': 'xpu'`) updt_s 1.166 / data_s 0.009 (GPU 계산 병목). 메모리 한계는 아직 확인 전.
  확인한 것: 스크립트 문법, 휠 꼬리표 판정 스크립트(바뀐 휠 → 실패), 화면 점검 문구.

## lerobot 버전·설치

설치 스크립트는 공용 PC 를 전제로 사용자 환경을 바꾸지 않습니다: miniforge 는 `-b`(배치) 설치만 하고 `conda init`·`conda config` 를
부르지 않습니다(`~/.bashrc`·`~/.condarc` 무변경). 파이썬 패키지는 전부 env 안, 캐시는 `activate.sh` 의 `HF_HOME`·`TORCH_HOME`(레포 `data/`).
apt 는 `apt_one` 으로 한 개씩·대체 이름 순서로 설치하고 실패해도 경고만 합니다 (24.04+ 의 `*t64` 개명, 26.04 이름 변화 대비).
Intel NPU·GPU 드라이버는 설치하지 않고 점검·안내만 합니다. sudo 가 필요한 일은 nohup 중 인증 만료를 피하려고 시작 직후에 몰아 둡니다.

- lerobot commit `e40b58a8dfa9e7b86918c374791599d070518d11` 에 맞춰져 있습니다 (`lerobot_conda.sh` 의 `LEROBOT_COMMIT`)
- `pip install -e "lerobot-src[feetech,dynamixel,training,diffusion,smolvla]"`, 그다음 `torchcodec` 제거 + `av>=15,<16` (pyav 디코딩)
- 양팔 OMX: `pip install --no-deps -e plugins/lerobot_robot_bi_omx -e plugins/lerobot_teleoperator_bi_omx`
- Python 3.12, torch 2.9 cu130 (Thor 는 `https://pypi.jetson-ai-lab.io/sbsa/cu130`)
- lerobot 이 `torch<2.12, torchvision<0.27` 을 요구합니다. `cuda`·`intel` 플랫폼은 이 범위로 받습니다(`thor` 는 기존 인덱스 그대로).
- torch 고정용 `PIP_CONSTRAINT` 에서 로컬 버전 꼬리표(`+cu130`, `+cpu`)는 뗍니다 — 붙어 있으면 pip 가
  `Cannot install None … ResolutionImpossible` 로 lerobot 설치를 포기합니다. `torch==2.11.0` 은 설치된 `2.11.0+cu130` 을 그대로 만족합니다.
- `intel` 플랫폼: `openvino>=2025.4`, `nncf>=2.19`, `render`·`video` 그룹. NPU/GPU 드라이버는 점검·안내만 합니다.

## 테스트

하드웨어 없이 다음으로 검증했습니다 (스크립트는 레포에 넣지 않았습니다):

- STS3215 / Dynamixel X(Protocol 2.0) **에뮬레이터**(처음엔 PTY, 지금은 `sim://` 가상 시리얼) + 실제 lerobot 버스 — 진단, 캘리브레이션(범위·공장값·가져오기),
  확인 단계, Control 연결 순서·E-STOP·과부하 모터, 포트 감시, 모터 ID 세팅, 양팔 OMX 플러그인
- lerobot API 스텁 — Collect 상태 기계, 양팔 녹화 worker, 강제 종료, 환경·프로젝트
- 헤드리스 Chromium — SO 한팔·양팔, OMX 한팔·양팔 네 구성에서 전 페이지 JS 오류 0
- `tools_jscheck.py` — 인라인 JS 문법 (한국어·영어 두 모드)
- 영어 모드 — 헤드리스 Chromium 으로 전 페이지 순회: 한/EN 버튼 전환·복귀, JS 오류 0, 화면에 남은 한국어 없음(버튼 글자 제외),
  한국어 태스크 설명이 입력칸·`/api/setup/state`·`/api/projects` 에서 그대로 유지
- OpenVINO: 실제 lerobot(e40b58a)·torch 2.11·OpenVINO 2026.4 로 합성 데이터셋 → CPU 학습 20 step 체크포인트 → 변환(FP16/INT8, CPU) →
  가상 SO-ARM101 팔 + 가상 카메라로 OpenMimic 에서 OV 롤아웃(정상 종료·SIGINT 중지·NPU/GPU 없음 → CPU 대체·해상도 불일치·낡은 IR 거부),
  PyTorch 롤아웃 회귀. **NPU·내장 GPU 실측은 하지 못했습니다** (시험 서버에 장치 없음)
- Hub: 실제 Hub API 로 하드웨어·가격 목록, 잘못된 토큰 거부, 체크포인트 찾기(루트형), 클라우드 학습 인자를 lerobot 이 받아 제출 직전
  ("Not logged in")까지 진행 확인(과금 없음). 대용량 파일 다운로드는 시험 환경에서 `*.xethub.hf.co` 가 막혀 실제 전송은 확인 못 함(실패 시 정리는 확인)
- 복구(색인 삭제 → 2 에피소드 복구 → LeRobotDataset 로드), 사전 점검(해상도 경고·양팔 거부), 실패 힌트(포트 없음), 공장 상태 가상 팔에서
  캘리브레이션 프롬프트 자동 응답, 패키지 설치(transformers 설치 후 torch 2.11 유지), FOURCC 가 lerobot 카메라 설정까지 전달
- 정책: 실제 lerobot 으로 ACT·Diffusion(DDIM)·VQ-BeT 학습 → 가상 팔 롤아웃. 정책 인자 전부를 lerobot-train 설정 파서(validate 포함)로 파싱.
- 롤아웃 모니터·시도 기록(브라우저: 카메라·3D·관절표·S/F/U 키·중지 후 화면), 모델 내보내기→가져오기 왕복(OV 상태 유지),
  악성 tar/zip(경로 탈출·링크) 거부, 데이터셋 왕복, 이어서 학습(20→30 step, 데이터 순서 이어짐), Diffusion 학습·롤아웃(오프라인)
