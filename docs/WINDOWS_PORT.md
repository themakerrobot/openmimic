# Windows 네이티브 포팅 보고서

한 코드베이스로 Ubuntu 22.04 / 24.04 / 26.04 와 Windows 10 / 11 에서 돌도록 바꾼 내용, 확인한 것과 못 한 것을 모았습니다.

> 포팅 뒤 가상 장치 기능(가상 팔·가상 카메라·MuJoCo 물리 시뮬레이션 — `tools_simarms.py`, `openmimic_fakeserial.py`,
> `openmimic_simcam.py`, `openmimic_mujoco.py`)은 제거했습니다. 아래에서 가상 장치를 다룬 항목은 포팅 당시의 기록입니다.

**요약**

- Ubuntu 는 가상 장치 전 구성 회귀 시험을 통과했습니다. 변경 전 코드로 같은 시험을 돌려 결과를 비교했습니다.
- Windows 실기에서는 **설치까지만 확인**했습니다 (Core Ultra 200V · Arc 140V 기기, `win-intel`). 서버 실행·팔·카메라·가상 장치는 아직입니다.
- Windows 쪽은 다음까지만 확인했습니다.
  - 모의 장치로 논리 시험
  - 출력 파싱 시험
  - PowerShell 스크립트 정적 점검
- 확신 없는 Windows 동작은 코드에 `TODO(win-verify)` 로 남겼습니다 (아래 목록).

## 1. 바뀐 파일과 이유

| 파일 | 무엇이 · 왜 |
|---|---|
| `openmimic_platform.py` (새 파일) | OS 에 따라 다른 코드를 한 곳에 모았습니다. 포트·카메라 나열과 식별자 해석, 작업 띄우기·중지·생존 확인, 공유 폴더, MuJoCo 렌더 백엔드, OS 별 안내 문구(`TEXT`), 플랫폼 이름. Linux 구현은 기존 코드를 그대로 옮겼습니다 |
| `openmimic_fakeserial.py` (새 파일) | 가상 팔 포트 `sim://<이름>`. `serial.Serial` 자리에 생성 함수를 두어 `sim://` 일 때만 `FakeSerial`(127.0.0.1 소켓)을 만들고, 실제 포트는 원래 pyserial 을 씁니다. PTY 를 대신해 Linux · Windows 가 같은 경로를 탑니다 |
| `tools_simarms.py` | PTY · 심볼릭 링크 · `select(stdin)` 을 없애고, `SimPortServer` 로 보드를 제공합니다. 중지 처리기에 CTRL_BREAK 를 더했습니다. 보드 잠금과 월드 잠금을 겹쳐 잡던 문제도 고쳤습니다 (3절) |
| `main.py` | 작업 띄우기·중지·생존 확인, 포트·카메라 나열·비교·해석, 포트·카메라 형식 검사와 안내 문구, 수집 worker 중지 처리기, 명령 파일 읽기(`take_file`), 작업 환경에 `PYTHONUTF8=1`, 셋업 화면 플랫폼 표시를 바꿨습니다. `os.name` · `sys.platform` 은 직접 보지 않습니다 |
| `openmimic_rollout.py` | CTRL_BREAK 를 lerobot 의 SIGINT 처리기로 넘깁니다 — lerobot 은 SIGINT·SIGTERM 만 받아 Windows 에서는 '중지' 가 정리 없이 끝납니다. 서보 이상 정지는 `os.kill(getpid, SIGINT)` 대신 `signal.raise_signal` 을 씁니다. 가상 시리얼도 설치합니다 |
| `tools_armcheck.py` · `tools_dxlcheck.py` | 포트 나열·해석, stdin Enter 감지를 플랫폼 함수로 옮겼습니다. CLI 에서 `sim://` 포트를 쓸 수 있습니다 |
| `openmimic_mujoco.py` · `openmimic_simcam.py` | 렌더 프레임 폴더를 `frame_dir()` (Windows 는 `%TEMP%\openmimic_sim`)로, 렌더 백엔드 결정을 `setup_mujoco_gl()` (Windows 는 `wgl`) 로 바꿨습니다. 크기가 같은 프레임 파일은 덮어씁니다 (Windows 는 매핑 중인 파일을 잘라낼 수 없음) |
| `openmimic_ov.py` | NPU·GPU 드라이버 안내 문구를 OS 별로 나눴습니다 |
| `install.ps1` · `openmimic.ps1` · `activate.ps1` (새 파일) | `lerobot_conda.sh` · `openmimic.sh` · `activate.sh` 의 Windows 판입니다. UTF-8 BOM 으로 저장했습니다 (Windows PowerShell 5.1 이 BOM 없는 파일을 시스템 코드 페이지로 읽음) |
| `.gitattributes` (새 파일) | `*.ps1` 은 CRLF, `*.sh` 는 LF 로 체크아웃합니다 |
| `openmimic_i18n_en.json` | Windows 안내 문구와 새 화면 문구의 영어를 넣었습니다 |
| `README.md` · `README.en.md` · `docs/INSTALL.md` · `docs/INTEL.md` · `docs/DEVELOPMENT.md` | Windows 빠른 시작, 설치·파일 위치·`openmimic.ps1`·플랫폼 표, 문제 해결(COM 드라이버·카메라), 설계 설명을 넣었습니다 |

포맷은 바꾸지 않았습니다 — 캘리브레이션 json, 데이터셋(lerobot v3), 체크포인트, `openmimic_config.json`, 환경 zip.
포트 값은 기존 규칙(by-id 우선, 없으면 by-path)으로 저장합니다. Windows 의 by-id 는 `usb-<vid>_<pid>_<시리얼>`, by-path 는 USB 위치(`1-3.2`)입니다.
`COM5` 같은 가변 이름은 저장하지 않습니다.

## 2. 지시서와 다르게 한 것 (이유)

| 지시 | 실제 | 이유 |
|---|---|---|
| FakeSerial 안에 보드를 붙여 함수 호출로 응답 | 보드는 가상 팔 도구 한 프로세스에 두고, FakeSerial 은 127.0.0.1 소켓으로 그 보드에 붙음 | 서버·수집 worker·롤아웃·점검 도구가 **같은 팔**을 봐야 합니다. 예: 서버가 쓴 캘리브레이션 레지스터를 worker 가 읽고, MuJoCo 장면·리더 시연은 하나뿐입니다. 프로세스마다 보드를 따로 두면 상태가 갈라집니다. 소켓은 드라이버 없이 pip 만으로 되고 Linux · Windows 가 같은 경로입니다 (두 경로 유지 없음) |
| `OPENMIMIC_SIM=1` 일 때만 워커에서 `serial.Serial` 교체 | 시리얼을 여는 OpenMimic 프로세스(서버·수집 worker·롤아웃·점검 도구)가 시작할 때 항상 설치. 교체 함수는 `openmimic_fakeserial.install()` 한 곳 | 서버 자신도 가상 포트를 엽니다 (제어·캘리브레이션·마법사). 환경 변수로 막으면 가상 장치를 켤 때마다 서버를 다시 켜야 합니다. `sim://` 가 아닌 포트는 원래 pyserial 객체가 그대로 만들어져 실제 팔에는 영향이 없습니다 |
| `resolve_serial_port` Linux = `os.path.realpath` · 없으면 예외 | Linux 는 받은 문자열을 그대로 돌려줌, 예외 없음. 같은 장치 비교는 `port_key`(= realpath) | 변경 전 Linux 는 by-id 경로를 그대로 lerobot 에 넘겼습니다 (pyserial 이 링크를 따라감). 포트가 없을 때의 오류 문구·실패 힌트도 그대로 두려고 동작을 바꾸지 않았습니다. Windows 는 지시대로 못 찾으면 `PortNotFound` |
| usb 정보 `{vid, pid, sn, …}` | 키 이름은 기존 그대로 `serial` | 화면 JS·마법사가 `usb.serial` 로 by-id / by-path 를 고릅니다. 이름을 바꾸면 Linux 화면 동작이 달라집니다 |
| 카메라 안정 식별자 by_path | Windows 는 by_id = by_path = `dshow:<장치 경로>` | DirectShow 장치 경로는 시리얼이 있으면 시리얼 기반, 없으면 Windows 가 USB 위치로 만든 값입니다. 어느 규칙으로 골라도 같은 값이 저장되게 했습니다 |
| 자동 시작은 작업 스케줄러(`schtasks /SC ONLOGON`) | 시작프로그램 폴더 바로 가기(`.lnk`) | 로그온 트리거 작업은 관리자 권한이 필요하고, 기본값이 'AC 전원일 때만 시작'·낮은 우선순위라 노트북 배터리에서는 켜지지 않고 수집·학습이 느려집니다. 바로 가기는 권한이 필요 없고 경로에 한글이 있어도 됩니다 |

지시서 표에 없던 Linux 의존 지점도 함께 바꿨습니다.

- `os.kill(pid, 0)` — Windows 에서는 확인이 아니라 **그 프로세스를 종료**합니다 (가상 장치 상태 확인 2곳).
- `os.waitpid(…, WNOHANG)`, `/proc/<pid>/stat` — 작업 생존 확인, PID 재사용 판정.
- `select.select([sys.stdin])` — `tools_armcheck`, `tools_simarms`.
- `os.symlink` — 가상 팔 포트 링크.
- 명령 파일 '읽고 → 지우기' — Windows 는 쓰는 쪽이 연 파일을 지울 수 없어 같은 명령이 다시 실행됩니다.
- 파이썬 기본 인코딩 — 한국어가 든 설정·로그·표준출력을 Windows 기본 코드 페이지로 쓰다 깨지거나 멈춥니다. `PYTHONUTF8=1` · `-X utf8` 로 막았습니다.
- 화면 안내 중 Linux 명령만 가리키던 문구 — dialout, `/dev/serial/by-id`, `lerobot_conda.sh`, `/dev/accel`.

## 3. 함께 고친 기존 문제

**문제**: 물리 시뮬레이션 가상 장치의 자동 녹화가 에피소드 사이에서 `There is no status packet` 으로 끊겼습니다.
**변경 전 코드(PTY)에서도 같은 자리에서 재현**됐습니다.

**원인**:

- 가상 팔 도구가 팔로워 갱신 때 보드 잠금을 쥔 채 MuJoCo 월드 잠금을 기다립니다.
- 그사이 큐브 재배치·시연 계획(이 서버에서 약 80 ms)이 월드 잠금을 쥐고 있습니다.
- 그래서 시리얼 응답이 lerobot 제한 시간(약 35 ms)을 넘깁니다.

**고친 방법**: 보드 잠금 안에서는 레지스터만 읽고, MuJoCo 호출은 잠금 밖에서 합니다.
고친 뒤 자동 녹화 3 에피소드를 끝까지 통과합니다.

**남은 것**: CPU 렌더(OSMesa) 시험 환경에서는 렌더가 파이썬 GIL 을 쥐어 응답 스레드가 드물게 27~300 ms 밀립니다.
MuJoCo 자동 녹화 시험 3회 중 1회(서버를 막 켠 첫 회)에 같은 오류가 한 번 났습니다. GPU 렌더(EGL·wgl)에서는 렌더 시간이 짧아 덜할 것으로 보지만 확인 전입니다.

## 3-1. Windows 실기에서 나온 문제

**설정 저장·환경 전환이 안 되거나 중간에 멈춤.**

- 원인: 설정 파일은 임시 파일에 쓴 뒤 `os.replace` 로 바꿔치기합니다. Windows 는 그 순간 다른 쪽이 대상 파일을 열고 있으면
  `PermissionError` 로 실패합니다. 다른 쪽이란 화면 갱신이 읽는 중이거나 Windows Defender 가 검사 중인 경우입니다 (Linux 는 열려 있어도 바꿔치기가 됨).
- 영향: 저장이 실패하면 포트 지정이 덮어써지지 않습니다. 가상 장치를 끌 때 환경 되돌리기도 중간에 멈춰,
  가상 장치는 꺼졌는데 환경은 가상 팔용(`sim-…`)으로 남을 수 있습니다.
- 고친 방법: `P.replace_file` 이 Windows 에서 최대 2 초까지 잠깐씩 다시 시도합니다.
  읽기도 `P.read_text` 로 잠깐 다시 시도합니다 — 읽기 실패를 '파일 없음' 으로 보고 기본값을 저장하면 설정이 지워지기 때문입니다.
  `os.replace` 를 쓰던 곳은 모두 바꿨습니다 (설정·환경·캘리브레이션 파일, 수집·롤아웃 상태와 미리보기, 가상 장치 상태, OpenVINO 메타, 전원 기록).

**에피소드 하나 저장이 1분을 넘음 (카메라 3대).**

- 원인: lerobot 은 저장 때마다 카메라 수만큼 `ProcessPoolExecutor` 로 영상을 인코딩합니다. Windows 는 fork 가 없어(spawn)
  작업자마다 새 파이썬이 `main.py`·torch·lerobot 을 처음부터 import 합니다 — 에피소드마다 이 비용을 다시 냅니다.
- 고친 방법: 수집 worker 에서 `P.avoid_spawn_pools()` 가 `ProcessPoolExecutor` 를 `ThreadPoolExecutor` 로 바꿉니다 (Windows 만).
  인코딩·PNG 읽기는 GIL 을 풀어 스레드로도 나란히 돕니다. 시험(Linux, 카메라 3대 · 300 프레임): spawn 11.7 초 → 스레드 5.4 초, 영상 프레임 수 같음.
- 저장에 걸린 시간을 수집 화면 '직전' 칸과 작업 로그에 남깁니다. Windows 실측은 아직입니다.

## 3-2. 전체 재검토에서 고친 것 (Windows)

코드 전체를 Windows 기준으로 다시 읽어 실제로 문제가 되는 곳만 고쳤습니다.

**COM 포트 — Windows 는 한 곳만 열 수 있음** (Linux 는 여러 곳이 같이 열어도 됨)

- 제어 탭 연결이 다른 시작 요청(포트 점검·캘리브레이션·확인 등)과 같은 관문을 지나게 했습니다. 그쪽이 포트를 여는 중에 제어 탭이 같은 포트를 열지 않습니다.
- 실행(롤아웃) 시작에 전원 점검 뒤 '사용 중' 재확인을 넣었습니다. 수집 시작과 같습니다.
- 리더 팔로우를 켜는 중에 제어 탭이 닫히면, 아무도 모르는 리더 포트가 열린 채 남던 문제를 고쳤습니다. 연결과 해제를 잠금으로 묶었고, '사용 중' 판단에 리더 포트도 넣었습니다.
- 캘리브레이션 끝·모터 ID 세팅 끝·확인 끝에서 '끝' 표시를 포트를 닫은 **뒤**로 옮겼습니다.
- 셋업 '전체 스캔'(보드레이트 전부)에서 오류가 나도 포트를 닫습니다. lerobot `scan_port` 는 닫지 않습니다.
- 가상 장치 끄기는 제어 탭·캘리브레이션·포트 감시 등이 쓰는 중이면 막습니다.

**작업 중지**

- '중지'(CTRL_BREAK)는 콘솔 없는 도우미가 작업의 콘솔에 붙어서 보냅니다.
  `openmimic.ps1 restart`·`update` 뒤에는 서버와 작업의 콘솔이 달라서 직접 보내면 닿지 않습니다.
- 강제 중지(`taskkill`)의 출력을 시스템 코드 페이지로 읽습니다. UTF-8 로 읽으면 한국어 Windows 에서 오류가 나 중지가 실패했습니다.
- 클라우드 학습 작업은 `os.execv` 대신 자식으로 돌립니다. Windows `execv` 는 pid 가 바뀌어 OpenMimic 이 작업이 끝났다고 봅니다.

**파일·이름**

- 데이터셋 이름 바꾸기·지우기: 영상이 열려 있으면 통째로 실패하고 안내합니다.
  예전에는 `shutil.move` 가 복사로 넘어가 데이터셋이 둘로 갈라지고, 지우기는 일부만 지워졌습니다.
- Windows 가 못 쓰는 이름을 막았습니다: 끝의 `.`, `con`·`nul`·`com1` 같은 예약 이름.
- 실행 성공률 기록의 키를 `/` 로 통일했습니다. `\` 로 저장돼 모델 화면에 성공률이 안 보였고, 예전 키는 읽을 때 바꿉니다.
- 캘리브레이션 내보내기(`os.uname` → `platform.node`), 복구 도구의 이름 바꾸기(`os.replace`), 접속 주소 찾기(`hostname -I` 는 Linux 만).
- 번호로만 잡히는 카메라(pygrabber 없음 등)도 저장할 수 있습니다.
- 영어 화면에서 경로·장치 이름은 번역하지 않습니다. 사용자 폴더 이름에 한국어가 있으면 사전이 그 글자를 바꿔 경로가 깨졌습니다.
- 손으로 넣은 `com5` 를 `COM5` 와 같은 포트로 봅니다. COM 목록은 번호 순서로 보여 줍니다.
- 가상 장치를 끄면 렌더 프레임 머리를 지워, 가상 카메라가 옛 화면에 멈춰 있지 않습니다.
- 터미널에서 바로 돌리는 도구(`tools_armcheck`·`tools_dxlcheck`·`tools_act2bench`)는 출력과 파일을 UTF-8 로 씁니다.

**설치·실행 스크립트**

- `install.ps1`
  - 다른 파이썬 설치의 사용자 site-packages·`PYTHONPATH` 를 막습니다 (`PYTHONNOUSERSITE`).
  - 환경 파이썬이 실행 중이면 설치를 멈춥니다. 쓰는 중인 `.dll` 을 바꾸지 못해 WinError 5 로 깨지기 때문입니다.
  - `conda create -p … --override-channels -c conda-forge` 를 씁니다. 기존 conda 설정을 따라가지 않게 하려는 것입니다.
- `openmimic.ps1`
  - 서버를 `-u` 로 띄워 시작 안내가 로그에 바로 남게 했습니다 (`openmimic.sh` 도).
  - 서버를 보통 우선순위로 맞춥니다.
  - git 오류를 따로 안내하고, 끄기 실패를 알립니다.
  - 자동 시작은 바로 가기로 합니다 (2절).

## 4. TODO(win-verify) 목록

| 위치 | 확인할 것 |
|---|---|
| `openmimic_platform.py` `_win_break` | 콘솔 없는 도우미가 작업 콘솔에 붙어 CTRL_BREAK 를 전달하는지 (특히 `openmimic.ps1 restart` 뒤 '중지') |
| `openmimic.ps1` background | Windows 11 에서 기본 터미널이 Windows Terminal 일 때 숨긴 cmd 창이 보이는 창으로 바뀌지 않는지 (보이는 창을 닫으면 서버·작업이 같이 꺼짐) |
| `openmimic_platform.py` `_win_port_entry` | SO-ARM101 보드(CH343 `1a86:55d3`)에서 pyserial 이 시리얼 번호·USB 위치를 채우는지 |
| `openmimic_platform.py` `_win_cameras` | DirectShow 나열 순서가 OpenCV `CAP_DSHOW` 인덱스와 같은지. 장치 경로가 재부팅 뒤에도 같은지 (같은 모델 2대) |
| `openmimic_platform.py` `_dshow_from_ffmpeg` | ffmpeg 목록에서 빠지는 장치('(none)' 가상 카메라 등)가 있을 때 인덱스가 어긋나는지 |
| `openmimic_platform.py` `_win_cameras` (폴백) | pygrabber·ffmpeg 가 둘 다 없으면 번호로만 지정 — 재부팅·재연결 때 순서가 바뀔 수 있음 |
| `openmimic_platform.py` `stdin_line_ready` | Windows 콘솔에서 msvcrt 로 Enter 감지 (`tools_armcheck --sweep`) |
| `openmimic_platform.py` `replace_file` · `read_text` | 2 초 다시 시도로 충분한지 (Defender 검사가 더 길면 저장 실패가 남음) |
| `install.ps1` XPU | 휠 설치·장치 인식은 확인 (`2.11.0+xpu`, `xpu: True`, Arc 140V). **학습이 실제로 도는지**는 아직 |
| `install.ps1` 사용자 폴더 | 한글 등 영문이 아닌 사용자 폴더에서 miniforge·pip 가 동작하는지 |
| `install.ps1` PyAV | Windows PyAV 휠의 FFmpeg 에 lerobot 기본 코덱(libsvtav1)이 있는지 — 수집 영상 인코딩 |
| `install.ps1` OpenVINO | OpenVINO 가 `CPU GPU NPU` 를 잡는 것은 확인. `openmimic_ov` 변환·NPU 추론은 아직 |
| `openmimic.ps1` autostart | 시작프로그램 바로 가기로 로그인 때 켜지는지 |

## 5. 단계 5 — lerobot Windows 호환 확인 결과

| 항목 | 결과 | 근거 |
|---|---|---|
| 영상 인코딩·디코딩 | **우회 (설계상 PyAV 강제) · Windows 실행 미확인** | 설치 스크립트가 양쪽 OS 모두 `torchcodec` 을 지웁니다. lerobot `e40b58a` 는 torchcodec 이 없으면 디코딩을 PyAV 로 바꾸고, 인코딩은 원래 PyAV 입니다. Windows PyAV 휠에 libsvtav1 이 있는지는 확인하지 못했습니다 |
| lerobot `find_cameras()` | **우회** | Windows 에서는 인덱스 0~N 을 기본 백엔드로 하나씩 열어 봐 느리고, 안정 식별자가 없습니다. OpenMimic 은 DirectShow 나열(pygrabber → ffmpeg)을 먼저 쓰고, 둘 다 안 될 때만 `find_cameras()` 로 내려갑니다. 열 때는 인덱스 + `CAP_DSHOW` 를 lerobot 카메라 설정 `backend` 로 넘깁니다 |
| OpenVINO NPU (`openmimic_ov.py`) | **장치 인식 됨 · 변환·추론 미확인** | Windows 실기에서 OpenVINO 2026.4 가 `['CPU', 'GPU', 'NPU']` 를 잡았습니다. 변환·추론 코드에는 OS 의존이 없습니다 (경로·장치 이름만 씀) |
| `OPENMIMIC_TORCH=xpu` Windows | **설치 됨 · 학습 미확인 — 실험적으로 둠** | CPU 휠로 설치한 뒤 `xpu` 로 다시 돌리면 CPU 휠을 지우고 `torch 2.11.0+xpu` 를 설치했고, `torch.xpu.is_available()` 이 True, 장치 `Intel(R) Arc(TM) 140V GPU` 입니다. lerobot 설치 뒤에도 XPU 휠이 유지됐습니다 |
| 롤아웃 CLI 의 Windows 경로 | **됨** (Linux 에서 draccus 로 확인) | `C:\Users\…\openmimic_sim\cam_top` 처럼 역슬래시·공백이 든 카메라 경로를 dict 인자에 따옴표 없이 넣어도 그대로 파싱됩니다 |

## 6. 검증 체크리스트 결과

### Ubuntu (회귀)

시험 환경은 Ubuntu 24.04 컨테이너입니다 (CPU 4코어, GPU·실기 없음).
lerobot `e40b58a`, torch 2.11, mujoco 3.17 을 썼고 MuJoCo 렌더는 OSMesa 입니다.

| # | 항목 | 결과 |
|---|---|---|
| 1 | `lerobot_conda.sh` 새 설치 → `openmimic.sh start` → 마법사 → 실기 SO-ARM101 | **못 함** (실기·새 설치 기기 없음). `lerobot_conda.sh` · `openmimic.sh` 는 바꾸지 않았습니다 |
| 2 | 실기 수집 5 에피소드 → ACT 100 step → 실행 | **실기로는 못 함**. 가상 팔로 대신했습니다 (3번). CPU 에서 ACT 가 스텝당 약 14 초라 20 step 으로 줄였습니다 |
| 3 | 가상 SO-ARM101 한팔 + MuJoCo: 마법사 → 자동 녹화 3 → 학습 → 실행 자동 판정 → 이상 상황 흉내 자동 정지 | **통과**. 세부는 아래 |
| 4 | 가상 OMX 양팔: 공장값 쓰기 → 확인 → 제어 | **통과** (12/12) |
| 5 | 작업 중지(SIGINT) · 강제 중지(SIGKILL), 중지 후 토크 해제 | **통과**. 수집·실행 모두 중지 후 팔로워 Torque_Enable 이 6개 모두 0. 강제 중지는 worker 종료까지 확인 |

3번 세부 결과:

- 마법사: 포트 목록, probe, 진단, 범위 캘리브레이션 2개, 확인, 제어 연결 — 모두 통과.
- 자동 녹화 3 에피소드 완료. 마지막 시연은 과제 성공입니다.
- 과열 흉내(75 °C)로 수집이 자동 정지했습니다.
- ACT 20 step 학습 → 실행: 시작 때 큐브를 다시 놓고, 끝에 **실패가 자동 기록**됐습니다 (`auto: sim`).
  20 step 모델이라 성공은 기대하지 않았고, 성공 기록 경로는 이번에 다시 보지 못했습니다.
- 포트 찾기 감시: 쓸기한 리더만 travel 이 크게 올라갔습니다.

추가로 돌린 구성:

| 구성 | 결과 |
|---|---|
| 가상 SO-ARM101 한팔 (물리 없음, 수동 녹화) | 23/23 |
| 가상 SO-ARM101 양팔 | 27/27 |
| 가상 OMX 한팔 | 21/21 |
| `tools_jscheck.py` 한/영 | JS 문법 오류 0 |
| 셋업 화면 플랫폼 표시 | 한/영 정상 |

변경 전 코드(같은 시험 스크립트)는 MuJoCo 시나리오에서 22/23이었습니다. 3절의 기존 문제 때문에 '수집 끝남'이 실패했습니다.
단계 1·2 적용 뒤에도 22/23으로 **같았고**, 단계 3 이후 23/23 입니다.

### Windows

| # | 항목 | 결과 |
|---|---|---|
| 1 | `install.ps1` → `openmimic.ps1 start` → 브라우저 | **`install.ps1` 통과** (Windows 실기, `win-intel`, 끝까지 `=== 완료 ===`; 같은 기기에서 `xpu` 로 다시 설치도 통과). `openmimic.ps1 start` 이후는 아직. 실기 설치 로그에서 고친 것: 다시 설치할 때 setuptools 를 최신으로 올려 lerobot·torch 요구 범위 충돌 오류가 찍히던 것(`setuptools<81` 로 고정 — Linux 판도 같이), conda 의 빈 표준오류 줄이 `System.Management.Automation.RemoteException` 으로 찍히던 것 |
| 2 | 실기 SO-ARM101: 포트 찾기(COM + SN) · 진단 · 캘리브레이션 · 확인 · 제어 · 수집 3 | **못 함**. 포트 해석 논리(시리얼 번호 → COM, USB 위치 → COM, 대소문자 COM, 없는 포트 예외, 형식 검사)는 모의 `comports()` 로 시험해 통과 |
| 3 | 카메라 2대 등록 → 재부팅 → 같은 장치 | **못 함**. ffmpeg `-list_devices` 옛·새 출력 형식 파싱과 장치 경로에서 시리얼 판별은 시험해 통과 |
| 4 | 가상 장치 + MuJoCo(wgl) | **못 함**. 가상 시리얼(소켓)은 OS 기능을 쓰지 않아 Linux 와 같은 코드입니다. `wgl` 렌더는 미확인 |
| 5 | Ubuntu 데이터셋·캘리브레이션 zip·모델을 Windows 로 가져와 학습·실행 | **못 함**. 포맷은 바꾸지 않았습니다 |
| 6 | Core Ultra: OpenVINO 변환 → NPU 실행 | **장치 인식만 확인** (`CPU GPU NPU`). 변환·실행은 아직 |
| 7 | 작업 중지·강제 중지 | **못 함**. 코드는 CTRL_BREAK(정상) / `taskkill /T /F`(강제), 처리기는 SIGINT·SIGTERM·SIGBREAK |

Windows 실기 시험은 위 7개 항목과 4절 목록을 같이 확인하면 됩니다.
