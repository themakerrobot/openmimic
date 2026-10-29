"""OpenMimic 플랫폼 분기 — Linux(Ubuntu) 와 Windows 의 차이는 이 파일에만 둡니다.

다른 파일은 os.name / sys.platform 을 직접 보지 않고 여기 함수를 부릅니다.
Linux 쪽은 원래 main.py 에 있던 코드를 그대로 옮긴 것입니다.

    시리얼 포트   list_serial_ports · resolve_serial_port · port_key · port_exists · serial_port_ok
    카메라        list_cameras · resolve_camera · camera_path_ok
    작업 프로세스 spawn_job · stop_job · job_alive · proc_start_time · install_stop_handlers · interrupt_self
    공유 폴더     shm_dir
    안내 문구     serial_group_hint · serial_port_hint · camera_path_hint
"""
from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
HERE = Path(__file__).resolve().parent
DSHOW_PREFIX = "dshow:"                   # Windows 카메라 안정 식별자 (DirectShow 장치 경로)


class PortNotFound(OSError):
    """설정에 저장된 포트(by-id · USB 위치 · COM)를 지금 연결된 장치에서 찾지 못함."""


class CameraNotFound(OSError):
    """설정에 저장된 카메라 식별자를 지금 연결된 장치에서 찾지 못함."""


# ============================================================================ 기기 플랫폼
def platform_name() -> str:
    """설치 스크립트와 같은 플랫폼 이름 — Linux: intel | cuda | thor, Windows: win-intel | win-cuda.
    OPENMIMIC_PLATFORM 으로 강제할 수 있습니다 (설치 때와 같은 변수)."""
    forced = os.environ.get("OPENMIMIC_PLATFORM", "").strip()
    if forced:
        return forced
    from shutil import which
    nvidia = which("nvidia-smi") is not None
    if IS_WIN:
        return "win-cuda" if nvidia else "win-intel"
    import platform
    if platform.machine() == "aarch64":
        return "thor"
    return "cuda" if nvidia or Path("/proc/driver/nvidia").exists() else "intel"


def child_env_extra() -> dict:
    """작업 worker 에 더 넘길 환경 변수. Windows: 파이썬 UTF-8 모드 — 기본 인코딩(cp949·cp1252)으로는
    한국어가 든 설정·로그·표준출력을 읽고 쓰다 깨지거나 UnicodeError 로 멈춥니다."""
    return {"PYTHONUTF8": "1"} if IS_WIN else {}


# ============================================================================ 공유 폴더
# 수집 때 '녹화하면서 바로 영상 압축'(lerobot streaming_encoding) 기본값.
# Windows 는 끄면 에피소드마다 PNG 수천 장을 쓰고(실시간 검사가 파일마다 검사) 저장 때 한꺼번에 압축해 저장이 수십 초 걸립니다.
# Linux 는 지금까지와 같게 끔 (수집 화면에서 켤 수 있음).
STREAMING_ENCODING_DEFAULT = IS_WIN


def avoid_spawn_pools() -> None:
    """Windows: 이 프로세스의 ProcessPoolExecutor 를 ThreadPoolExecutor 로 바꿉니다 (수집 worker 에서만 부름).
    lerobot 은 에피소드를 저장할 때마다 카메라 수만큼 ProcessPoolExecutor 로 영상을 인코딩합니다. Windows 는 fork 가 없어(spawn)
    작업자마다 새 파이썬이 main.py · torch · lerobot 을 처음부터 다시 import 해 에피소드마다 수십 초가 더 걸립니다.
    인코딩(PyAV/FFmpeg)과 PNG 읽기는 GIL 을 풀고 돌아서 스레드로도 카메라별로 나란히 돕니다.
    Linux 는 그대로 둡니다 (fork 라 싸고, 지금까지와 같은 동작)."""
    if not IS_WIN:
        return
    import concurrent.futures as cf
    cf.ProcessPoolExecutor = cf.ThreadPoolExecutor


def shm_dir(name: str, fallback: Path) -> Path:
    """초당 여러 번 쓰는 작은 파일용 폴더. Linux 는 tmpfs(/dev/shm) 가 있으면 그것, 아니면 fallback."""
    if not IS_WIN and Path("/dev/shm").is_dir():
        return Path("/dev/shm") / name
    return fallback


def replace_file(src, dst, wait: float = 2.0) -> None:
    """os.replace — Windows 는 다른 프로세스·스레드(화면 갱신 읽기, Windows Defender 검사)가 대상 파일을 잠깐 열고 있으면
    PermissionError 로 실패합니다. 그 사이 wait 초까지 잠깐씩 다시 시도합니다. Linux 는 os.replace 그대로."""
    if not IS_WIN:
        os.replace(src, dst)
        return
    import time
    end = time.monotonic() + wait
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= end:
                raise
            time.sleep(0.03)


def read_text(path, wait: float = 1.0) -> str:
    """Path.read_text — Windows 는 바꿔치기 중인 파일을 여는 순간 PermissionError 가 날 수 있어 잠깐 다시 시도합니다.
    (읽기 실패를 '파일 없음' 으로 보고 기본값을 저장하면 설정이 지워집니다) Linux 는 그대로."""
    if not IS_WIN:
        return Path(path).read_text()
    import time
    end = time.monotonic() + wait
    while True:
        try:
            return Path(path).read_text()
        except PermissionError:
            if time.monotonic() >= end:
                raise
            time.sleep(0.03)


def take_file(path: Path):
    """명령 파일을 통째로 가져갑니다 (내용 문자열, 없거나 지금 못 가져가면 None).
    이름을 바꾼 뒤 읽고 지웁니다 — Windows 는 다른 프로세스가 쓰려고 연 파일을 지울 수 없어
    '읽고 → 지우기' 로 하면 지우기가 실패해 같은 명령이 다시 실행됩니다. 이름 바꾸기가 실패하면 다음 차례에 다시 시도."""
    path = Path(path)
    if not path.exists():
        return None
    tmp = path.with_name(path.name + ".take")
    try:
        os.replace(path, tmp)
    except OSError:
        return None
    try:
        return tmp.read_text()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


# ============================================================================ 작업 프로세스
def spawn_job(cmd, env, stdout, cwd) -> subprocess.Popen:
    """작업 worker 를 OpenMimic 과 다른 프로세스 그룹으로 띄웁니다 — 중지 신호가 OpenMimic 자신에게 가지 않게.
    Linux: 새 세션(start_new_session) · Windows: 새 프로세스 그룹(CTRL_BREAK 를 이 그룹에만 보냄)."""
    kw = {}
    if IS_WIN:
        kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stdout,
                            stderr=subprocess.STDOUT, env=env, **kw)


# Windows CTRL_BREAK 보내기 — GenerateConsoleCtrlEvent 는 '호출한 쪽과 같은 콘솔' 의 프로세스 그룹에만 닿습니다.
# OpenMimic 을 다시 켜면(openmimic.ps1 restart·update) 서버는 새 콘솔이고, 남아 있던 작업은 옛 콘솔에 있어 직접 보내면 안 닿습니다.
# 그래서 콘솔 없는 작은 도우미 프로세스가 작업의 콘솔에 붙어(AttachConsole) 그 그룹에 보냅니다.
_BREAK_HELPER = (
    "import ctypes, sys\n"
    "k = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "pid = int(sys.argv[1])\n"
    "k.FreeConsole()\n"
    "if not k.AttachConsole(pid):\n"
    "    sys.exit(2)\n"
    "k.SetConsoleCtrlHandler(None, True)\n"
    "sys.exit(0 if k.GenerateConsoleCtrlEvent(1, pid) else 3)\n")


def _win_break(pid: int) -> None:
    # TODO(win-verify): 도우미가 다른 콘솔의 작업 그룹에 CTRL_BREAK 를 전달하는지 (restart 뒤 '중지')
    try:
        r = subprocess.run([sys.executable, "-c", _BREAK_HELPER, str(pid)], capture_output=True, timeout=15,
                           creationflags=subprocess.DETACHED_PROCESS)
        if r.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    os.kill(pid, signal.CTRL_BREAK_EVENT)        # 같은 콘솔이면 직접 (= GenerateConsoleCtrlEvent). 실패하면 OSError


def stop_job(pid: int, force: bool) -> None:
    """force=False: 정상 종료 요청 (Linux SIGINT · Windows CTRL_BREAK), force=True: 즉시 종료.
    프로세스 그룹(자식 포함) 전체에 보냅니다. 실패하면 OSError."""
    pid = int(pid)
    if IS_WIN:
        if force:
            # taskkill 은 시스템 코드 페이지(cp949 등)로 출력합니다 — UTF-8 모드의 text=True 로 읽으면 UnicodeDecodeError
            r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                               encoding="oem", errors="replace")
            if r.returncode != 0:
                raise OSError(r.stderr.strip() or r.stdout.strip() or f"taskkill 실패 ({r.returncode})")
        else:
            _win_break(pid)
        return
    os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGINT)


def exec_replace(argv) -> None:
    """이 프로세스를 argv 로 바꿉니다 (작업 추적이 pid 를 그대로 봄). Linux: os.execv.
    Windows 의 os.execv 는 새 pid 로 띄우고 지금 프로세스를 끝내 OpenMimic 이 작업이 끝났다고 보므로,
    자식으로 돌리고 그 종료 코드로 끝냅니다 (같은 프로세스 그룹이라 '중지' 도 그대로 닿음)."""
    if IS_WIN:
        forward_break_to_sigint()
        try:
            sys.exit(subprocess.call(argv))
        except KeyboardInterrupt:
            sys.exit(130)
    os.execv(argv[0], argv)


_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def name_ok(name: str) -> bool:
    """폴더·파일 이름으로 쓸 수 있는지 — Windows 는 끝의 '.'(조용히 잘림)과 예약 이름(con·nul·com1 …)을 못 씁니다.
    Linux 는 항상 True (기존 규칙 그대로)."""
    if not IS_WIN:
        return True
    return not name.endswith((".", " ")) and name.split(".")[0].lower() not in _WIN_RESERVED


def lan_ip_command():
    """이 PC 의 LAN 주소를 알려 주는 명령 (없으면 None — 소켓으로 대신 찾음). Linux: hostname -I."""
    return None if IS_WIN else ["hostname", "-I"]


def utf8_stdio() -> None:
    """터미널에서 바로 돌리는 도구(tools_*.py)의 표준출력을 UTF-8 로 — Windows 에서 activate.ps1 없이 돌려도 한국어가 깨지지 않게."""
    if IS_WIN:
        for st in (sys.stdout, sys.stderr):
            try:
                st.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def unlink(path, wait: float = 1.0) -> None:
    """파일 지우기 — Windows 는 다른 쪽이 잠깐 열고 있으면 PermissionError 라 잠깐 다시 시도합니다. 없으면 조용히."""
    import time
    end = time.monotonic() + (wait if IS_WIN else 0)
    while True:
        try:
            Path(path).unlink()
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if not IS_WIN or time.monotonic() >= end:
                raise
            time.sleep(0.03)


def move_dir(src, dst) -> None:
    """폴더 이름 바꾸기 — Linux: shutil.move 그대로. Windows: 같은 드라이브 안에서 rename 만 합니다.
    (안의 파일이 열려 있으면 shutil.move 가 통째 복사 → 원본 일부만 지움으로 넘어가 데이터셋이 둘로 갈라짐) 실패하면 OSError."""
    if IS_WIN:
        os.rename(src, dst)
        return
    import shutil
    shutil.move(str(src), str(dst))


def remove_dir(path) -> None:
    """폴더 통째로 지우기 — Linux: shutil.rmtree 그대로. Windows: 먼저 이름을 바꿔 보고(열린 파일이 있으면 여기서 통째 실패,
    아무것도 안 지움) 그다음 지웁니다. 일부만 지워진 데이터셋이 남지 않게. 실패하면 OSError."""
    import shutil
    if IS_WIN:
        p = Path(path)
        tmp = p.with_name(f".{p.name}.del")
        os.rename(p, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        return
    shutil.rmtree(path)


def job_alive(pid) -> bool:
    """pid 프로세스가 살아 있는지 (좀비 제외).
    주의: Windows 에서 os.kill(pid, 0) 은 '확인' 이 아니라 그 프로세스를 종료시킵니다 — 반드시 이 함수를 씁니다."""
    if not pid:
        return False
    pid = int(pid)
    if IS_WIN:
        try:
            import psutil
        except ImportError:
            return _win_pid_alive(pid)
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.AccessDenied:
            return True                               # 다른 계정 프로세스 — 살아 있음
        except (psutil.NoSuchProcess, ValueError):
            return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        if state == "Z":
            return False
    except Exception:
        pass
    return True


def _win_pid_alive(pid):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)          # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return k32.GetLastError() == 5                # ERROR_ACCESS_DENIED → 있긴 함
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259                      # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def proc_start_time(pid):
    """프로세스 시작 시각 표식 — PID 가 재사용됐는지 가리는 데만 씁니다 (같은 OS 안에서만 비교).
    Linux: /proc/<pid>/stat 의 starttime(22번째 필드) · Windows: psutil create_time."""
    if IS_WIN:
        try:
            import psutil
            return round(psutil.Process(int(pid)).create_time(), 3)
        except Exception:
            return None
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except Exception:
        return None


def install_stop_handlers(handler) -> None:
    """작업 탭 '중지' 가 보내는 신호를 handler 로 — SIGINT · SIGTERM, Windows 는 CTRL_BREAK(SIGBREAK) 도."""
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, handler)


def forward_break_to_sigint() -> None:
    """Windows: CTRL_BREAK(작업 탭 '중지') 를 받으면 그 순간의 SIGINT 처리기를 부릅니다 —
    lerobot 이 나중에 SIGINT 처리기(ProcessSignalHandler)를 달아도 그쪽 정리 경로(시작 자세 복귀·토크 해제)를 탑니다.
    SIGINT 처리기가 아직 기본값이면 Ctrl+C 와 같은 KeyboardInterrupt. Linux 는 아무것도 하지 않습니다."""
    if not hasattr(signal, "SIGBREAK"):
        return

    def _fwd(signum, frame):
        h = signal.getsignal(signal.SIGINT)
        if callable(h):
            h(signal.SIGINT, frame)
        else:
            raise KeyboardInterrupt
    signal.signal(signal.SIGBREAK, _fwd)


def stdin_line_ready() -> bool:
    # TODO(win-verify): Windows 콘솔에서 msvcrt 로 Enter 감지 (tools_armcheck --sweep 의 '끝내기')
    """터미널에서 Enter 가 눌렸으면 그 줄을 읽어 버리고 True (기다리지 않음).
    Linux: select(stdin) · Windows: msvcrt (Windows 의 select 는 소켓만 받음)."""
    if IS_WIN:
        import msvcrt
        hit = False
        while msvcrt.kbhit():
            hit = msvcrt.getwch() in ("\r", "\n") or hit
        return hit
    import select
    r, _, _ = select.select([sys.stdin], [], [], 0)
    if r:
        sys.stdin.readline()
        return True
    return False


def interrupt_self() -> None:
    """자기 자신에게 '중지' 와 같은 SIGINT. (Windows 에서 os.kill(os.getpid(), SIGINT) 는 프로세스를 바로 끝냅니다)"""
    signal.raise_signal(signal.SIGINT)


# ============================================================================ 시리얼 포트
def _read_sysfs(p):
    try:
        return p.read_text().strip()
    except Exception:
        return ""


def usb_info(dev, cls="tty"):
    """Linux: /dev/ttyACM0 (cls=tty) 또는 /dev/video0 (cls=video4linux) → 그 뒤의 USB 장치 정보.
    sysfs 를 부모 방향으로 거슬러 올라가 idVendor 가 있는 노드를 찾습니다."""
    node = Path("/sys/class") / cls / os.path.basename(dev) / "device"
    if not node.exists():
        return {}
    p = node.resolve()
    for _ in range(8):
        if (p / "idVendor").exists():
            return {"vid": _read_sysfs(p / "idVendor"),
                    "pid": _read_sysfs(p / "idProduct"),
                    "manufacturer": _read_sysfs(p / "manufacturer"),
                    "product": _read_sysfs(p / "product"),
                    "serial": _read_sysfs(p / "serial")}
        if p.parent == p:
            break
        p = p.parent
    return {}


def _alias_map(dirname):
    """Linux: /dev/serial/by-id 등 → {실제 장치경로: 별칭경로}"""
    out = {}
    d = Path(dirname)
    if not d.is_dir():
        return out
    for link in sorted(d.iterdir()):
        try:
            out.setdefault(os.path.realpath(link), str(link))
        except OSError:
            pass
    return out


def _win_comports():
    from serial.tools import list_ports
    return list_ports.comports()


def _win_port_entry(p):
    # TODO(win-verify): SO-ARM101 보드(CH343 1a86:55d3)에서 pyserial 이 시리얼 번호·USB 위치(location)를 채우는지
    """pyserial ListPortInfo → 포트 항목. USB 가 아닌 COM(메인보드 COM1 등)은 None.
    by_id = usb-<vid>_<pid>_<시리얼번호> (보드에 시리얼 번호가 있을 때만), by_path = USB 위치(예 1-3.2)."""
    if p.vid is None:
        return None
    vid, pid = f"{p.vid:04x}", f"{(p.pid or 0):04x}"
    sn = p.serial_number or ""
    return {"dev": p.device,
            "by_id": f"usb-{vid}_{pid}_{sn}" if sn else "",
            "by_path": p.location or "",
            "usb": {"vid": vid, "pid": pid, "manufacturer": p.manufacturer or "",
                    "product": p.product or p.description or "", "serial": sn}}


def list_serial_ports() -> list[dict]:
    """USB 시리얼 장치 목록 — [{"dev", "by_id", "by_path", "usb": {vid, pid, manufacturer, product, serial}}].
    dev 는 지금 열 수 있는 경로(/dev/ttyACM0 · COM5), by_id 는 보드 시리얼 번호 기반(번호가 있을 때),
    by_path 는 물리 USB 포트 기반 식별자."""
    if IS_WIN:
        out = []
        for p in sorted(_win_comports(), key=lambda x: (len(x.device), x.device)):      # COM3 이 COM10 보다 앞에
            e = _win_port_entry(p)
            if e:
                out.append(e)
        return out
    by_id = _alias_map("/dev/serial/by-id")
    by_path = _alias_map("/dev/serial/by-path")
    devs = set()
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        devs.update(glob.glob(pat))
    devs.update(by_id)          # 위 패턴에 안 걸리는 이름까지 포함
    devs.update(by_path)
    return [{"dev": dev, "by_id": by_id.get(dev, ""), "by_path": by_path.get(dev, ""), "usb": usb_info(dev)}
            for dev in sorted(devs)]


_COM_RE = re.compile(r"^(\\\\\.\\)?COM\d+$", re.I)


def resolve_serial_port(spec: str) -> str:
    """설정에 저장된 포트 문자열 → 지금 열 수 있는 포트.
    Linux: 그대로 (by-id·by-path 심볼릭 링크를 pyserial 이 따라갑니다 — 원래 동작).
    Windows: usb-<vid>_<pid>_<sn> 또는 USB 위치 → 지금 그 장치의 COM 이름. 못 찾으면 PortNotFound."""
    spec = (spec or "").strip()
    if not IS_WIN or not spec:
        return spec
    ports = list_serial_ports()
    for e in ports:
        if spec == e["by_id"] and e["by_id"]:
            return e["dev"]
    for e in ports:
        if spec == e["by_path"] and e["by_path"]:
            return e["dev"]
    if _COM_RE.match(spec):
        name = spec.split("\\")[-1].upper()
        if any(e["dev"].upper() == name for e in ports):
            return name
    raise PortNotFound(f"포트를 찾을 수 없습니다: {spec} — USB 연결을 확인하고 셋업에서 다시 스캔하세요")


def port_key(spec: str) -> str:
    """같은 장치인지 비교할 때 쓰는 값 — Linux: realpath(/dev/ttyACM0), Windows: COM 이름.
    지금 연결돼 있지 않으면 문자열 그대로."""
    spec = (spec or "").strip()
    if not spec:
        return spec
    if IS_WIN:
        try:
            return resolve_serial_port(spec)
        except PortNotFound:
            return spec
    return os.path.realpath(spec) if os.path.exists(spec) else spec


def port_exists(spec: str) -> bool:
    """실제 포트가 지금 있는지."""
    spec = (spec or "").strip()
    if not spec:
        return False
    if IS_WIN:
        try:
            resolve_serial_port(spec)
            return True
        except PortNotFound:
            return False
    return Path(spec).exists()


def serial_port_ok(spec) -> bool:
    """설정에 넣을 수 있는 포트 문자열인지 (모양만 — 지금 연결돼 있는지는 보지 않음).
    Linux: /dev/… · Windows: COM5 · usb-<vid>_<pid>_<sn> · USB 위치(1-3.2)."""
    if not isinstance(spec, str) or not spec or len(spec) > 512:
        return False
    if IS_WIN:
        return bool(_COM_RE.match(spec) or re.fullmatch(r"usb-[0-9a-f]{4}_[0-9a-f]{4}_\w+", spec)
                    or re.fullmatch(r"[0-9]+-[0-9.]+(:x\.\d+)?", spec))
    return spec.startswith("/dev/")


def serial_port_hint() -> str:
    """포트 형식 오류 안내."""
    return TEXT["port_hint"]


# 화면·오류 안내 문구 중 OS 에 따라 다른 것. Linux 값은 원래 main.py 문구 그대로입니다.
if IS_WIN:
    TEXT = {
        "port_hint": "포트는 COM 포트 또는 USB 식별자(usb-… · USB 위치)여야 합니다",
        "port_open_fail": "다른 프로그램이 잡고 있거나 COM 포트 드라이버 문제 (장치 관리자 확인)",
        "perm_regex": r"PermissionError.*COM\d+|Access is denied|액세스가 거부",
        "perm_hint": "포트를 열 권한이 없습니다 — 다른 프로그램(시리얼 모니터 등)이 COM 포트를 잡고 있는지 확인하세요.",
        "no_ports_js": "시리얼 포트가 없습니다 — USB 와 장치 관리자의 COM 포트·드라이버를 확인하세요",
        "no_ports2_js": "USB 를 꽂고 다시 스캔하세요 (안 보이면 장치 관리자에서 COM 포트·드라이버 확인)",
        "port_note_html": (
            "지정 경로는 <b>보드에 USB 시리얼 번호가 있으면</b>(sn 표시) <span class=mono>usb-VID_PID_시리얼</span> —\n"
            "    보드 자체를 따라가므로 <b>어느 USB 구멍에 꽂아도</b> 됩니다. 대신 보드를 다른 팔로 옮겨 달면 설정이 어긋나니\n"
            "    보드에 sn 뒷자리를 적어 붙여 두세요.<br>\n"
            "    <b>sn 이 없으면</b> USB 위치(<span class=mono>1-3.2</span> 같은 값)를 씁니다 —\n"
            "    꽂은 USB 물리 포트에 고정되니 이 경우엔 <b>항상 같은 USB 구멍에</b> 꽂아야 합니다.\n"
            "    COM 번호(COM5 등)는 꽂을 때마다 바뀔 수 있어 저장하지 않습니다."),
        "cam_path_hint": "dshow: 로 시작하는 장치 경로여야 합니다",
        "rerun_hint": "activate.ps1 없이 실행한 것 같습니다. OpenMimic 을 끄고 openmimic.ps1 start 로 다시 실행하세요",
        "ov_install": "install.ps1 로 설치하거나 ",
        "reinstall": "install.ps1 을 다시 돌려도 됩니다",
        "ov_driver": "NPU 는 장치 관리자의 신경 프로세서(Intel AI Boost) 드라이버, GPU 는 Intel 그래픽 드라이버를 확인하세요",
        "pip_extra": ". .\\activate.ps1; cd lerobot-src; pip install -e \".[{extra}]\"",
    }
else:
    TEXT = {
        "port_hint": "포트는 /dev/… 경로여야 합니다",
        "port_open_fail": "다른 프로그램이 잡고 있거나 dialout 권한 문제",
        "perm_regex": r"Permission denied.*tty|PermissionError.*tty",
        "perm_hint": "포트 권한이 없습니다 — dialout 그룹에 추가 후 다시 로그인하세요.",
        "no_ports_js": "시리얼 포트가 없습니다 — USB 와 dialout 권한을 확인하세요",
        "no_ports2_js": "USB 를 꽂고 다시 스캔하세요 (권한 문제면 dialout 그룹 확인)",
        "port_note_html": (
            "지정 경로는 <b>보드에 USB 시리얼 번호가 있으면</b>(sn 표시) <span class=mono>/dev/serial/by-id</span> —\n"
            "    보드 자체를 따라가므로 <b>어느 USB 구멍에 꽂아도</b> 됩니다. 대신 보드를 다른 팔로 옮겨 달면 설정이 어긋나니\n"
            "    보드에 sn 뒷자리를 적어 붙여 두세요.<br>\n"
            "    <b>sn 이 없으면</b> 같은 모델끼리 by-id 가 겹치므로 <span class=mono>/dev/serial/by-path</span> 를 씁니다 —\n"
            "    꽂은 USB 물리 포트에 고정되니 이 경우엔 <b>항상 같은 USB 구멍에</b> 꽂아야 합니다."),
        "cam_path_hint": "/dev/ 로 시작해야 합니다",
        "rerun_hint": "activate.sh 없이 실행한 것 같습니다. OpenMimic 을 끄고 ./openmimic.sh start 로 다시 실행하세요",
        "ov_install": "OPENMIMIC_PLATFORM=intel ./lerobot_conda.sh 로 설치하거나 ",
        "reinstall": "lerobot_conda.sh 를 다시 돌려도 됩니다",
        "ov_driver": "NPU 는 /dev/accel/accel0·드라이버, GPU 는 compute-runtime 설치를 확인하세요",
        "pip_extra": "cd ~/project/openmimic/lerobot-src && pip install -e \".[{extra}]\"",
    }


def serial_group_hint() -> str:
    """포트를 못 열 때의 문제 해결 안내."""
    if IS_WIN:
        return "장치 관리자에서 COM 포트와 드라이버(CH340/CH343 · CP210x)를 확인하세요"
    return "groups 에 dialout 이 있는지 확인하세요 (없으면 sudo usermod -aG dialout $USER 후 다시 로그인)"


# ============================================================================ 카메라
_DSHOW_PROBE = r"""
import json
from comtypes import GUID, COMError
from comtypes.persist import IPropertyBag
from pygrabber.dshow_graph import SystemDeviceEnum
from pygrabber.dshow_ids import DeviceCategories
out = []
en = SystemDeviceEnum().system_device_enum.CreateClassEnumerator(GUID(DeviceCategories.VideoInputDevice), dwFlags=0)
try:
    mon, n = en.Next(1)
except ValueError:
    n = 0
while n > 0:
    bag = mon.BindToStorage(0, 0, IPropertyBag._iid_).QueryInterface(IPropertyBag)
    name = bag.Read("FriendlyName", pErrorLog=None)
    try:
        path = bag.Read("DevicePath", pErrorLog=None)
    except COMError:
        path = ""
    out.append({"name": name, "path": path or ""})
    mon, n = en.Next(1)
print(json.dumps(out))
"""


def _dshow_from_pygrabber():
    """DirectShow 비디오 입력 장치 — [{"name", "path"}] (OpenCV CAP_DSHOW 인덱스 순서). 실패하면 None.
    COM 초기화가 스레드마다 필요해 별도 프로세스에서 나열합니다."""
    try:
        r = subprocess.run([sys.executable, "-c", _DSHOW_PROBE], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None


def parse_ffmpeg_dshow(text: str) -> list[dict]:
    """ffmpeg -list_devices true -f dshow -i dummy 의 stderr → [{"name", "path"}] (비디오 장치만, 나열 순서).
    두 가지 출력 형식을 받습니다:
      옛 형식: 'DirectShow video devices' 머리 아래 "이름" 줄 + 'Alternative name "@device_pnp_…"'
      새 형식: "이름" (video) 줄 + 'Alternative name "@device_pnp_…"'"""
    out, section, cur = [], None, None
    for line in text.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        m = re.search(r'\]\s+"(.+)"\s*(\((video|audio|none)\))?\s*$', line)
        if m and "Alternative name" not in line:
            kind = m.group(3) or section
            cur = {"name": m.group(1), "path": ""} if kind == "video" else None
            if cur is not None:
                out.append(cur)
            continue
        m = re.search(r'Alternative name\s+"@device_pnp_(.+)"', line)
        if m and cur is not None:
            cur["path"] = m.group(1)
    return out


def _dshow_from_ffmpeg():
    # TODO(win-verify): ffmpeg 목록에서 빠지는 장치('(none)' 가상 카메라 등)가 있으면 인덱스가 어긋날 수 있음
    exe = None
    for cand in ("ffmpeg", "ffmpeg.exe"):
        from shutil import which
        exe = which(cand)
        if exe:
            break
    if not exe:
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None
    try:
        r = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    devs = parse_ffmpeg_dshow(r.stderr)
    return devs or None


def dshow_devices():
    """Windows DirectShow 카메라 목록 [{"name", "path"}] — pygrabber, 안 되면 ffmpeg. 둘 다 안 되면 None."""
    devs = _dshow_from_pygrabber()
    if devs is None:
        devs = _dshow_from_ffmpeg()
    return devs


def dshow_usb(path: str) -> dict:
    """DirectShow 장치 경로(\\\\?\\usb#vid_046d&pid_0825&mi_00#…#{…}) → USB 정보.
    인스턴스 부분이 영숫자뿐이면 그것이 USB 시리얼 번호, '&' 가 섞여 있으면 Windows 가 만든 위치 기반 ID 입니다."""
    m = re.search(r"usb#vid_([0-9a-f]{4})&pid_([0-9a-f]{4})(&mi_\d+)?#([^#]+)#", path or "", re.I)
    if not m:
        return {}
    inst = m.group(4)
    return {"vid": m.group(1).lower(), "pid": m.group(2).lower(),
            "serial": inst if re.fullmatch(r"\w+", inst) else ""}


def _win_cameras():
    # TODO(win-verify): DirectShow 나열 순서가 OpenCV CAP_DSHOW 인덱스와 같은지, 장치 경로가 재부팅 뒤에도 같은지(같은 모델 2대)
    """Windows: DirectShow 장치 순서 = OpenCV CAP_DSHOW 인덱스. 안정 식별자는 DirectShow 장치 경로
    (보드 시리얼이 있으면 시리얼 기반, 없으면 USB 위치 기반으로 Windows 가 만든 값)."""
    import cv2
    devs = dshow_devices()
    found = []
    if devs is None:
        # TODO(win-verify): pygrabber·ffmpeg 둘 다 없으면 인덱스만 — 재부팅·재연결 때 순서가 바뀔 수 있음
        try:
            from lerobot.cameras.opencv import OpenCVCamera
            for c in OpenCVCamera.find_cameras():
                prof = c.get("default_stream_profile") or {}
                found.append({"dev": str(c.get("id")), "by_id": "", "by_path": "", "usb": {},
                              "width": prof.get("width"), "height": prof.get("height"), "fps": prof.get("fps"),
                              "name": c.get("name", "") + " (안정 식별자 없음 — pygrabber 설치 필요)"})
        except Exception as e:
            found.append({"dev": "0", "by_id": "", "by_path": "", "usb": {}, "width": None, "height": None,
                          "fps": None, "name": f"(probe 실패: {e})"})
        return found
    for i, d in enumerate(devs):
        w = h = fps = None
        cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
        try:
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
                fps = int(round(cap.get(cv2.CAP_PROP_FPS))) or None
        finally:
            cap.release()
        sid = DSHOW_PREFIX + d["path"] if d.get("path") else ""
        usb = dshow_usb(d.get("path", ""))
        usb["product"] = d.get("name", "")
        found.append({"dev": str(i), "by_id": sid, "by_path": sid, "usb": usb,
                      "width": w, "height": h, "fps": fps, "name": d.get("name", "")})
    return found


def list_cameras() -> list[dict]:
    """카메라 후보 — [{"dev", "by_id", "by_path", "usb", "width", "height", "fps", "name"}].
    Linux: lerobot OpenCVCamera.find_cameras() 를 쓰되, 실패하면 /dev/video* 나열로 폴백합니다.
    같은 모델 카메라 2개(wrist ×2)는 USB 시리얼 번호가 없으면 /dev/v4l/by-id 이름이 겹쳐
    어느 링크가 어느 카메라인지 부팅마다 바뀔 수 있습니다. 시리얼 포트와 같은 규칙으로
    시리얼 번호가 있을 때만 by-id, 없으면 by-path(물리 USB 포트 고정) 를 씁니다."""
    if IS_WIN:
        return _win_cameras()
    by_id = _alias_map("/dev/v4l/by-id")
    by_path = _alias_map("/dev/v4l/by-path")

    def entry(dev, prof=None, name=""):
        real = os.path.realpath(dev) if dev.startswith("/dev/") else dev
        info = usb_info(dev, "video4linux") if dev.startswith("/dev/") else {}
        prof = prof or {}
        return {"dev": dev, "by_id": by_id.get(real, ""), "by_path": by_path.get(real, ""),
                "usb": info, "width": prof.get("width"), "height": prof.get("height"),
                "fps": prof.get("fps"), "name": name}

    found = []
    try:
        from lerobot.cameras.opencv import OpenCVCamera
        for c in OpenCVCamera.find_cameras():
            found.append(entry(str(c.get("id")), c.get("default_stream_profile"), c.get("name", "")))
    except Exception as e:
        for dev in sorted(glob.glob("/dev/video*")):
            found.append(entry(dev, None, f"(probe 실패: {e})"))
    return found


def resolve_camera(index_or_path):
    """설정의 index_or_path → (OpenCV 에 넘길 값, backend 또는 None).
    Linux: 그대로 (backend 지정 안 함 — 원래 동작). Windows: dshow:<장치 경로> → 지금 인덱스 + CAP_DSHOW,
    숫자 인덱스 → 그 인덱스 + CAP_DSHOW."""
    if not IS_WIN:
        return index_or_path, None
    import cv2
    if isinstance(index_or_path, str) and index_or_path.startswith(DSHOW_PREFIX):
        want = index_or_path[len(DSHOW_PREFIX):]
        devs = dshow_devices() or []
        for i, d in enumerate(devs):
            if d.get("path") and d["path"].lower() == want.lower():
                return i, cv2.CAP_DSHOW
        raise CameraNotFound(f"카메라를 찾을 수 없습니다: {index_or_path} — USB 연결을 확인하고 셋업에서 다시 스캔하세요")
    if isinstance(index_or_path, int) or (isinstance(index_or_path, str) and index_or_path.isdigit()):
        return int(index_or_path), cv2.CAP_DSHOW
    return index_or_path, None


def camera_key(dev) -> str:
    """썸네일 캐시 등에서 같은 카메라인지 비교하는 값 — Linux: realpath(/dev/video0)."""
    dev = str(dev)
    if not IS_WIN and dev.startswith("/dev/"):
        return os.path.realpath(dev)
    return dev


def camera_path_ok(idx: str) -> bool:
    """문자열 카메라 경로의 모양 — Linux: /dev/…, Windows: dshow:<장치 경로>."""
    if IS_WIN:
        # 안정 식별자가 없는 카메라(pygrabber·ffmpeg 없음, 장치 경로 없는 소프트웨어 카메라)는 번호 문자열로 옵니다
        return (idx.startswith(DSHOW_PREFIX) and len(idx) > len(DSHOW_PREFIX)) or idx.isdigit()
    return idx.startswith("/dev/")


def camera_path_hint() -> str:
    return TEXT["cam_path_hint"]
