#!/usr/bin/env python3
# ============================================================================
#  main.py — OpenMimic: LeRobot 로봇팔(SO-ARM101 / OMX) 통합 웹 툴 (단일 파일 FastAPI)
#
#  Projects / Datasets / Collect / Training / Models / Rollout / Hub / Control / Calib / Setup / 작업을
#  명령어 없이 웹에서 처리합니다. 한팔 · 양팔 지원.
#
#  - 팔·카메라는 lerobot 객체(SOFollower / SOLeader / OpenCVCamera)로 다룹니다.
#  - 수집은 이 파일을 --worker 로 띄워 lerobot record_loop() 를 직접 부릅니다.
#    상태·미리보기·명령은 전부 파일(RUN_DIR)이라 OpenMimic 을 재시작해도 세션이 유지됩니다.
#  - 외부 프로세스는 argv 리스트 + shell=False 로만 실행합니다.
#  - 설정은 openmimic_config.json (셋업 탭에서 채움). 자세한 것은 README.
#
#  실행 (OpenMimic conda env 안에서)
#   source ~/project/openmimic/activate.sh
#   nohup python main.py > openmimic.log 2>&1 &
#   → http://<host>:8080  (기본 인증 없음. OPENMIMIC_TOKEN / OPENMIMIC_AUTH=on 으로 켤 수 있음)
# ============================================================================
import asyncio
import glob
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pandas as pd
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

import openmimic_health  # 시작 전 전원 점검 · 실행 중 서보 온도/전압 감시
import openmimic_ov  # OpenVINO 변환·롤아웃. openvino 자체는 필요할 때 별도 프로세스에서만 import 합니다
import openmimic_simcam  # 가상 카메라 (openmimic_sim/cam_<이름>) — 실제 카메라 경로는 그대로

openmimic_simcam.install()

# ------------------------------- 경로 ---------------------------------------
HOME = Path.home()
# 레포를 clone 한 폴더가 곧 작업 폴더입니다 (데이터·출력·설정이 이 아래에 생김, .gitignore 처리).
# 시험 등으로 다른 곳을 쓰려면 OPENMIMIC_HOME 으로 지정합니다.
PROJ = Path(os.environ.get("OPENMIMIC_HOME") or Path(__file__).resolve().parent)
DATA_ROOT = PROJ / "data/hf/lerobot/local"
OUT_ROOT = PROJ / "outputs"
JOB_DIR = PROJ / "openmimic_jobs"
MARKS_FILE = PROJ / "openmimic_marks.json"
CONFIG_FILE = PROJ / "openmimic_config.json"
TOKEN_FILE = PROJ / "openmimic_token.txt"
URDF_DIR = Path(__file__).resolve().parent / "urdf"     # 레포에 든 자산 — OPENMIMIC_HOME 과 무관


def _lerobot_calib_root():
    """lerobot utils/constants.py 와 같은 규칙: HF_LEROBOT_CALIBRATION > HF_LEROBOT_HOME/calibration
    > HF_HOME/lerobot/calibration > ~/.cache/huggingface/lerobot/calibration.
    activate.sh 의 HF_HOME 기준이면 ~/project/openmimic/data/hf/lerobot/calibration 입니다."""
    if os.environ.get("HF_LEROBOT_CALIBRATION"):
        return Path(os.environ["HF_LEROBOT_CALIBRATION"]).expanduser()
    if os.environ.get("HF_LEROBOT_HOME"):
        return Path(os.environ["HF_LEROBOT_HOME"]).expanduser() / "calibration"
    hf_home = Path(os.environ.get("HF_HOME") or (HOME / ".cache/huggingface")).expanduser()
    return hf_home / "lerobot" / "calibration"


CALIB_ROOT = _lerobot_calib_root()
EXPECTED_CALIB_ROOT = PROJ / "data/hf/lerobot/calibration"      # activate.sh(HF_HOME=설치 폴더/data/hf) 기준
PORT = 8080


def calib_root_warning():
    """activate.sh 없이 실행하면 lerobot 이 ~/.cache/huggingface 같은 다른 캘리브레이션 폴더를 조용히 씁니다 — 알립니다.
    HF_LEROBOT_CALIBRATION / HF_LEROBOT_HOME 을 일부러 지정했으면 그대로 둡니다."""
    if os.environ.get("HF_LEROBOT_CALIBRATION") or os.environ.get("HF_LEROBOT_HOME"):
        return ""
    if CALIB_ROOT.resolve() == EXPECTED_CALIB_ROOT.resolve():
        return ""
    return (f"캘리브레이션 폴더가 설치 폴더({EXPECTED_CALIB_ROOT})가 아니라 {CALIB_ROOT} 입니다 — activate.sh 없이 실행한 것 같습니다. "
            "OpenMimic 을 끄고 ./openmimic.sh start 로 다시 실행하세요")


def lan_urls(port):
    """이 PC 의 접속 주소 (같은 네트워크의 노트북·태블릿에서 여는 주소)."""
    ips = []
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3).stdout.split()
        ips = [x for x in out if "." in x and not x.startswith("127.") and not x.startswith("172.17.")]
    except Exception:          # noqa: BLE001
        pass
    if not ips:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as so:
                so.connect(("10.255.255.255", 1))        # 실제로 보내지 않음 — 기본 경로의 내 주소만 얻음
                ips = [so.getsockname()[0]]
        except OSError:
            pass
    return [f"http://{ip}:{port}/" for ip in ips] or [f"http://<이 PC 의 IP>:{port}/"]

# ------------------------------- 상수 ---------------------------------------
CTL_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper"]
CONTROL_HZ = 20
FEEDBACK_HZ = 10
TRACK_WARN_DEG = 6.0      # 명령과 실측이 이만큼 벌어지면 '안 따라옴' 경고
GOAL_REFRESH_S = 1.0      # 데드밴드 안이어도 이 주기로 목표를 다시 씀 (놓친 명령 복구)
TEMP_READ_S = 1.0         # 서보 온도 읽기 주기
NOTE_HOLD_S = 8.0         # 제어 탭 버튼(토크·팔로우) 실패 문구를 보여 주는 시간
TEMP_WARN_C = 55          # 이 이상이면 경고
TEMP_HOT_C = 65           # 이 이상이면 위험 (STS3215 기본 셧다운 70°C)
MAX_STEP_DEG = 2.5        # 슬라이더 제어 시 스텝당 최대 이동
FOLLOW_STEP_DEG = 6.0     # 리더 팔로우 시 스텝당 최대 이동 (반응성↑)
CTL_STREAM_FPS = 15
PREVIEW_FPS = 10          # record worker 가 미리보기 JPEG 을 갱신하는 주기
READY_CHUNK_S = 2.0       # 수집 대기 중 record_loop 을 끊어 도는 단위 (이 틈에 온도를 읽습니다)
NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
# record worker ↔ 웹 사이의 상태/미리보기/명령 파일. 초당 수십 회 쓰므로 tmpfs 를 우선합니다.
RUN_DIR = Path("/dev/shm/openmimic") if Path("/dev/shm").is_dir() else PROJ / "openmimic_run"

# ------------------------------- 설정 ---------------------------------------
# arms 는 처음부터 리스트입니다. 한팔이면 원소 1개(side="main"),
# 양팔이면 side="left"/"right" 2개 — 스키마를 바꾸지 않고 확장합니다.
DEFAULT_CONFIG = {
    "robot": "so101",          # 기종: so101 (SO-ARM101, Feetech) | omx (ROBOTIS OMX, Dynamixel)
    "mode": "single",
    "robot_id": "so101",
    "fps": 30,
    "default_task": "Pick up the block and place it in the box",
    # None 이면 lerobot 쪽 상대이동 캡을 쓰지 않습니다 (제어 탭의 자체 적분기가 담당).
    # 값을 주면 send_action 마다 Present_Position 을 한 번 더 읽으므로 루프가 느려집니다.
    "max_relative_target": None,
    # 포트는 비워 둡니다 — udev 심볼릭 링크(/dev/so101_follower 같은) 를 전제하지 않습니다.
    # 셋업 탭에서 스캔·판별해서 채웁니다.
    "arms": [
        {
            "side": "main",
            "follower_port": "",
            "follower_id": "follower",
            "leader_port": "",
            "leader_id": "leader",
            "cameras": {},
            "view": {"x": 0.0, "y": 0.0, "yaw_deg": 0.0},
        }
    ],
    # 특정 팔에 속하지 않는 카메라 (양팔에서도 접두사 없이 유지됩니다)
    "cameras": {},
}


def load_config():
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text())
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as e:
            print(f"[openmimic] 설정 파일 파싱 실패 — 기본값 사용: {e}")
    else:
        try:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        except Exception as e:
            print(f"[openmimic] 설정 파일 생성 실패: {e}")
    if not cfg.get("arms"):
        cfg["arms"] = json.loads(json.dumps(DEFAULT_CONFIG["arms"]))
    # mode 가 1차 기준입니다. arms 길이가 안 맞으면 mode 에 맞춰 잘라내거나 채웁니다.
    cfg["mode"] = "bimanual" if str(cfg.get("mode", "")).lower() in ("bimanual", "bi", "dual") else "single"
    want = 2 if cfg["mode"] == "bimanual" else 1
    cfg["arms"] = cfg["arms"][:want]
    while len(cfg["arms"]) < want:
        cfg["arms"].append({})
    names = ("main",) if want == 1 else ("left", "right")
    for i, arm in enumerate(cfg["arms"]):
        arm["side"] = names[i]
        suffix = "" if want == 1 else f"_{names[i]}"
        arm.setdefault("cameras", {})
        arm.setdefault("follower_port", "")
        arm.setdefault("leader_port", "")
        arm.setdefault("follower_id", f"follower{suffix}")
        arm.setdefault("leader_id", f"leader{suffix}")
        # 3D 뷰 전용 배치 (제어·데이터와 무관). 기본값: 양팔은 좌우로 벌림
        dy = 0.0 if want == 1 else (0.12 if names[i] == "left" else -0.12)
        v = arm.get("view") or {}
        arm["view"] = {"x": float(v.get("x", 0.0)),      # 앞뒤 (m, + 앞)
                       "y": float(v.get("y", dy)),       # 좌우 (m, + 왼쪽)
                       "yaw_deg": float(v.get("yaw_deg", 0.0))}
    # 양팔인데 두 배치가 같으면 3D 에서 정확히 포개져 팔 하나로 보입니다. 좌우로 벌려 줍니다.
    if want == 2:
        vl, vr = cfg["arms"][0]["view"], cfg["arms"][1]["view"]
        if (vl["x"], vl["y"], vl["yaw_deg"]) == (vr["x"], vr["y"], vr["yaw_deg"]):
            vl["y"], vr["y"] = 0.12, -0.12
    cfg.setdefault("cameras", {})
    # 예전 설정 파일에는 robot 이 없습니다 → SO-ARM101 (지금까지 동작 그대로)
    if cfg.get("robot") not in ("so101", "omx"):
        cfg["robot"] = "so101"
    return cfg


def all_camera_specs(cfg, arm_cfgs, bimanual):
    """{표시이름: spec} — 한팔에서는 팔 카메라와 공용 카메라를 그냥 합칩니다."""
    out = {}
    for side, arm in arm_cfgs.items():
        for name, spec in arm["cameras"].items():
            out[f"{side}_{name}" if bimanual else name] = spec
    for name, spec in cfg["cameras"].items():
        out[name] = spec
    return out


def _rebind(cfg):
    """설정에서 파생되는 전역을 다시 만듭니다 (셋업 탭 저장 시 재호출)."""
    global CFG, ARM_CFGS, SIDES, BIMANUAL, CAM_SPECS, ARMS, LEADERS
    CFG = cfg
    ARM_CFGS = {a["side"]: a for a in cfg["arms"]}
    SIDES = list(ARM_CFGS)
    BIMANUAL = len(SIDES) > 1
    CAM_SPECS = all_camera_specs(cfg, ARM_CFGS, BIMANUAL)
    ARMS = {s: ArmCtl(c) for s, c in ARM_CFGS.items()}
    LEADERS = {s: LeaderCtl(c) for s, c in ARM_CFGS.items()}


CFG = ARM_CFGS = SIDES = CAM_SPECS = ARMS = LEADERS = None
BIMANUAL = False


def ports_configured():
    return all(a.get("follower_port") for a in ARM_CFGS.values())

JOB_DIR.mkdir(parents=True, exist_ok=True)
OUT_ROOT.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="OpenMimic")

# ----------------------------- 인증 (기본 꺼짐) -------------------------------
# 기본은 인증 없음 — http://<host>:8080 으로 바로 들어갑니다.
# 켜려면 둘 중 하나:
#   OPENMIMIC_TOKEN=원하는값 python main.py     (토큰 직접 지정)
#   OPENMIMIC_AUTH=on        python main.py     (openmimic_token.txt 에 자동 생성)
def _init_token():
    tok = os.environ.get("OPENMIMIC_TOKEN")
    if tok and tok.strip():
        return tok.strip()
    if os.environ.get("OPENMIMIC_AUTH", "").lower() not in ("on", "1", "true", "yes"):
        return None
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    tok = secrets.token_hex(16)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(tok + "\n")
    try:
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return tok


AUTH_TOKEN = _init_token()
COOKIE = "openmimic_token"


def token_ok(tok):
    return bool(tok) and secrets.compare_digest(str(tok), AUTH_TOKEN)


LOGIN_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<style>body{background:#0f1216;color:#e6ebf1;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
form{background:#161b21;border:1px solid #28303a;border-radius:10px;padding:26px;width:320px}
h1{font-size:15px;letter-spacing:.14em;margin:0 0 16px;font-family:ui-monospace,monospace}
input{width:100%;box-sizing:border-box;background:#0f1216;color:#e6ebf1;border:1px solid #28303a;
border-radius:7px;padding:10px;font-size:16px;font-family:ui-monospace,monospace}
button{margin-top:12px;width:100%;background:#2c4257;color:#dceafe;border:1px solid #5d9dd6;
border-radius:7px;padding:10px;font-size:14px;cursor:pointer}
p{color:#8b98a7;font-size:12px;line-height:1.6}</style>
<form method=get action="/"><h1>OpenMimic</h1>
<input name=token placeholder="access token" autofocus autocomplete=off>
<button>접속</button>
<p>토큰은 서버의 <code>openmimic_token.txt</code> 에 있습니다.<br>
해제하려면 <code>OPENMIMIC_AUTH=off</code> 로 실행하세요.</p></form>"""


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if AUTH_TOKEN is None:
        return await call_next(request)
    qtok = request.query_params.get("token")
    if token_ok(qtok) or token_ok(request.cookies.get(COOKIE)) or token_ok(
        request.headers.get("x-openmimic-token")
    ):
        resp = await call_next(request)
        if token_ok(qtok):
            resp.set_cookie(COOKIE, AUTH_TOKEN, max_age=90 * 86400,
                            httponly=True, samesite="lax", path="/")
        return resp
    return HTMLResponse(LOGIN_PAGE, status_code=401)


def _same_origin(headers):
    """다른 사이트의 웹페이지가 브라우저를 통해 몰래 보내는 요청(CSRF) 차단용.
    인증과 별개이며 사용자에게는 보이지 않습니다. curl 처럼 Origin 이 없는 요청은 통과합니다."""
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = headers.get("origin")
    if not origin or origin == "null":
        return True
    from urllib.parse import urlsplit
    host = (headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
    return (urlsplit(origin).hostname or "") == host


# 팔(시리얼)·GPU 를 잡는 '시작' 요청들은 한 번에 하나씩 처리합니다.
# 검사(exclusive_busy)와 시작 사이에 await 가 있어서, 두 번 누르면 둘 다 검사를 통과해
# 같은 포트를 두 번 여는 경쟁이 생기기 때문입니다.
_START_PATHS = {"/api/record", "/api/rollout", "/api/train", "/api/setup/probe", "/api/setup/watch",
                "/api/setup/motors/start", "/api/setup/armcheck/start", "/api/wizard/assign",
                "/api/wizard/verify/start", "/api/wizard/import_calib", "/api/wizard/mode",
                "/api/setup/config", "/api/calib/start", "/api/calib/factory", "/api/wizard/robot",
                "/api/envs/activate", "/api/envs/save_as", "/api/ov/convert",
                "/api/envs/rename", "/api/envs/delete", "/api/hub/push", "/api/hub/pull",
                "/api/sim/start", "/api/sim/stop"}
_START_PREFIXES = ("/api/install/", "/api/dataset/repair/")
_START_GATE = asyncio.Lock()


@app.middleware("http")
async def guard_middleware(request: Request, call_next):
    if request.method == "POST":
        if not _same_origin(request.headers):
            return JSONResponse({"error": "다른 사이트에서 온 요청은 받지 않습니다"}, status_code=403)
        if request.url.path in _START_PATHS or request.url.path.startswith(_START_PREFIXES):
            async with _START_GATE:
                return await call_next(request)
    return await call_next(request)


# ----------------------------- 한/영 전환 ------------------------------------
# 화면 문자열은 소스에 한국어로 있습니다. 영어를 고르면(쿠키 openmimic_lang=en) 서버가 내보내는 HTML·JS·JSON 에서
# 한국어 조각을 openmimic_i18n_en.json 사전으로 바꿉니다 (긴 조각부터 한 번에). 사전 값에는 따옴표·<>{}·역슬래시가
# 없어서 JS 문자열·JSON 안에서 바꿔도 문법이 깨지지 않습니다. 사전에 없는 조각은 한국어 그대로 남습니다.
I18N_FILE = Path(__file__).resolve().parent / "openmimic_i18n_en.json"
LANG_COOKIE = "openmimic_lang"
DEFAULT_LANG = "en" if os.environ.get("OPENMIMIC_LANG", "").lower().startswith("en") else "ko"
_I18N = {"mtime": None, "rx": None, "map": {}}


def _i18n():
    try:
        m = I18N_FILE.stat().st_mtime
    except OSError:
        return None
    if _I18N["mtime"] != m:            # 사전을 고치면 재시작 없이 반영
        d = load_json(I18N_FILE, {})
        keys = sorted((k for k in d if k and d[k] is not None), key=len, reverse=True)
        _I18N.update(mtime=m, map=d, rx=re.compile("|".join(map(re.escape, keys))) if keys else None)
    return _I18N


_JOSA = set("을를이가은는에로와과의도만")


def to_en(text):
    t = _i18n()
    if not t or t["rx"] is None:
        return text

    def rep(mt):
        k, v = mt.group(0), t["map"][mt.group(0)]
        if not v.strip():
            return v
        # 한국어는 조사를 태그·영문 뒤에 붙여 씁니다 (<b>설정 저장</b>을 → </b> to apply) — 영어는 띄어야 합니다
        a = text[mt.start() - 1] if mt.start() else ""
        lt = text.rfind("<", 0, mt.start())
        closing = a == ">" and lt >= 0 and text.startswith("</", lt)      # </b>을 → 띄움, <b>저장 → 안 띄움
        if a and v[0].isalnum() and (closing or a in ")]}" or (a.isascii() and a.isalnum())
                                     or (a in "'\"`" and k[0] in _JOSA)):
            v = " " + v
        z = text[mt.end()] if mt.end() < len(text) else ""
        if v[-1].isalnum() and z.isascii() and z.isalnum():
            v += " "
        return v
    return t["rx"].sub(rep, text)


# 사용자가 입력한 글(작업 설명 task, 설정값, 메모)은 번역하면 저장할 때 망가지므로 건드리지 않습니다.
I18N_KEEP_KEYS = {"task", "tasks", "default_task", "config", "note", "notes", "memo", "desc", "description"}
_I18N_KEEP_HTML = re.compile(r'(\svalue="[^"]*"|<textarea[^>]*>.*?</textarea>)', re.S)


# 탭·작업 상태·페이지 제목처럼 짧은 낱말은 사전에 넣으면 문장 속 같은 낱말까지 바뀌므로, 자리를 정해 따로 바꿉니다.
NAV_EN = {"셋업": "Setup", "캘리브레이션": "Calibration", "제어": "Control", "수집": "Collect", "데이터셋": "Datasets",
          "학습": "Training", "모델": "Models", "실행": "Rollout", "프로젝트": "Projects", "허브": "Hub", "작업": "Jobs",
          "더보기": "More", "대기": "Idle", "클라우드 학습": "Cloud training", "포트 감시": "Port watch", "설치": "Install",
          "OpenVINO 변환": "OpenVINO convert", "복구": "Repair", "삭제": "Delete",
          "가상 장치": "Virtual devices",
          # 표 머리·입력칸 이름표·작은 제목 — 한 낱말짜리라 자리를 정해 바꿉니다
          "로봇": "Robot", "크기": "Size", "상태": "Status", "시작": "Started", "종류": "Kind", "폭": "Span",
          "정책": "Policy", "배치": "Batch", "step 수": "Steps", "녹화": "Recording", "캘리브레이션": "Calibration"}
_NAV_RE = re.compile(r'(<span class=(?:tl|jobk)[^>]*>|<h2>|<b class=gsname>|<th[^>]*>|<p class=eyebrow>|<label class=f>'
                     r'|<a class=btnlink [^>]*>|<a href="/[^"]*">)'
                     r'([^<]+?)( ?</span>| ?</h2>| ?</b>| ?</th>| ?</p>| <| ?</a>)')


def to_en_html(text):
    """HTML·JS — 입력칸의 value="..." 와 textarea 내용(사용자 글)은 남기고 나머지만 바꿉니다."""
    text = _NAV_RE.sub(lambda m: m.group(1) + NAV_EN.get(m.group(2).strip(), m.group(2)) + m.group(3), text)
    parts = _I18N_KEEP_HTML.split(text)
    return "".join(x if i % 2 else to_en(x) for i, x in enumerate(parts))


def to_en_obj(o, key=None):
    """JSON — 문자열 값만 바꾸고 I18N_KEEP_KEYS 아래는 그대로 둡니다."""
    if key in I18N_KEEP_KEYS:
        return o
    if isinstance(o, str):
        return to_en(o)
    if isinstance(o, list):
        return [to_en_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: to_en_obj(v, k) for k, v in o.items()}
    return o


def to_en_json(text):
    try:
        o = json.loads(text)
    except ValueError:
        return to_en(text)
    return json.dumps(to_en_obj(o), ensure_ascii=False)


def lang_of(cookies):
    v = (cookies or {}).get(LANG_COOKIE)
    return v if v in ("ko", "en") else DEFAULT_LANG


@app.middleware("http")
async def i18n_middleware(request: Request, call_next):
    resp = await call_next(request)
    if lang_of(request.cookies) != "en":
        return resp
    ct = resp.headers.get("content-type", "")
    if not (ct.startswith("text/html") or ct.startswith("application/json")):
        return resp                    # 영상·MJPEG·tar 다운로드 등은 그대로 흘림
    body = b"".join([chunk async for chunk in resp.body_iterator])
    text = body.decode("utf-8", "replace")
    out = (to_en_json(text) if ct.startswith("application/json") else to_en_html(text)).encode("utf-8")
    headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
    return Response(out, status_code=resp.status_code, headers=headers)


@app.get("/lang")
def switch_lang(request: Request, to: str = ""):
    """한/영 버튼 — 지금과 반대로(또는 to=ko|en) 바꾸고 보던 화면으로 돌아갑니다."""
    from urllib.parse import urlsplit
    new = to if to in ("ko", "en") else ("ko" if lang_of(request.cookies) == "en" else "en")
    ref = urlsplit(request.headers.get("referer") or "/")
    back = (ref.path or "/") + (f"?{ref.query}" if ref.query else "")
    if not back.startswith("/") or back.startswith("//") or back.startswith("/lang"):
        back = "/"
    r = RedirectResponse(back, status_code=303)
    r.set_cookie(LANG_COOKIE, new, max_age=10 * 365 * 86400, samesite="lax")
    return r


def ws_authed(sock: WebSocket):
    if not _same_origin(sock.headers):
        return False
    return AUTH_TOKEN is None or token_ok(sock.cookies.get(COOKIE))


# ----------------------------- 유틸 -----------------------------------------
def esc(s):
    return html.escape(str(s), quote=True)


def js(v):
    """<script> 블록 안에 박는 JSON 리터럴. (HTML 속성에는 쓰지 말 것 — jsattr 사용)"""
    return json.dumps(v, ensure_ascii=False).replace("</", "<\\/")


def jsattr(v):
    """onclick="..." 같은 HTML 속성 안에 박는 JSON 리터럴.
    js() 를 그대로 쓰면 JSON 의 " 가 속성 따옴표를 닫아버립니다."""
    return html.escape(json.dumps(v, ensure_ascii=False), quote=True)


def safe_name(s):
    # '.' / '..' 은 정규식은 통과하지만 경로로 쓰면 상위 폴더가 됩니다
    return bool(s) and NAME_RE.fullmatch(s) is not None and s.strip(".") != ""


def load_json(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def _atomic_write(p, text):
    """임시 파일에 쓰고 os.replace — 쓰는 도중에 다른 스레드가 읽어도 빈/반쪽 파일을 보지 않습니다.
    (반쪽 파일을 읽으면 load_json 이 기본값을 돌려주고, 그걸 다시 저장하면 전부 지워집니다)"""
    p = Path(p)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, p)


def save_json(p, obj):
    _atomic_write(p, json.dumps(obj, indent=1, ensure_ascii=False))


META_LOCK = threading.Lock()     # 마크처럼 스레드풀에서 동시에 읽고-고치고-쓰는 파일용


def load_marks():
    return load_json(MARKS_FILE, {})


def save_marks(m):
    save_json(MARKS_FILE, m)


# ----------------------------- 환경 · 프로젝트 · 데이터셋 메타 ------------------
# 환경 = 하드웨어 구성 한 벌(모드·포트·캘리브 id·카메라·fps).
# 활성 환경은 언제나 openmimic_config.json 입니다 — 나머지 코드는 이 파일만 봅니다.
# 이름 붙은 사본을 openmimic_envs/<이름>.json 에 두고, 전환하면 그 사본을 활성 설정으로 복사합니다.
ENV_DIR = PROJ / "openmimic_envs"
PROJECTS_FILE = PROJ / "openmimic_projects.json"
DSMETA_FILE = PROJ / "openmimic_dsmeta.json"
DEFAULT_ENV = "default"


def env_name():
    return (CFG or {}).get("env_name") or DEFAULT_ENV


def _env_file(name):
    if not safe_name(name):
        raise ValueError("환경 이름은 영문/숫자/._- 만")
    return ENV_DIR / f"{name}.json"


def _env_summary(name, cfg, mtime=None):
    arms = [{"side": a.get("side"), "follower_port": a.get("follower_port", ""),
             "leader_port": a.get("leader_port", ""),
             "follower_id": a.get("follower_id", ""), "leader_id": a.get("leader_id", ""),
             "cameras": sorted((a.get("cameras") or {}).keys())}
            for a in (cfg.get("arms") or []) if isinstance(a, dict)]
    return {"name": name, "robot": cfg.get("robot") or "so101", "mode": cfg.get("mode", "single"), "arms": arms,
            "cameras": sorted((cfg.get("cameras") or {}).keys()), "fps": cfg.get("fps"),
            "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else "",
            "active": name == env_name()}


def _write_active_config(cfg):
    merged = json.loads(json.dumps(DEFAULT_CONFIG))
    merged.update(cfg)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(CONFIG_FILE, json.dumps(merged, indent=2, ensure_ascii=False))
    _rebind(load_config())


def sync_active_env():
    """활성 설정을 그 이름의 환경 파일에도 씁니다 (Setup 저장 때마다)."""
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    cfg = json.loads(json.dumps(CFG))
    cfg["env_name"] = env_name()
    save_json(_env_file(env_name()), cfg)


def list_envs():
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    if not _env_file(env_name()).exists():
        sync_active_env()                 # 처음 켤 때 지금 설정이 'default' 환경이 됩니다
    out = []
    for f in sorted(ENV_DIR.glob("*.json")):
        cfg = load_json(f, None)
        if isinstance(cfg, dict) and safe_name(f.stem):
            out.append(_env_summary(f.stem, cfg, f.stat().st_mtime))
    return out


def activate_env(name):
    cfg = load_json(_env_file(name), None)
    if not isinstance(cfg, dict):
        raise ValueError(f"환경 없음: {name}")
    cfg["env_name"] = name
    err = validate_config(cfg)
    if err:
        raise ValueError(f"환경 '{name}' 의 설정이 올바르지 않습니다: {err}")
    _write_active_config(cfg)


def save_env_as(name):
    if _env_file(name).exists():
        raise ValueError(f"이미 있는 환경 이름: {name}")
    cfg = json.loads(json.dumps(CFG))
    cfg["env_name"] = name
    _write_active_config(cfg)
    sync_active_env()


def rename_env(old, new):
    src, dst = _env_file(old), _env_file(new)
    if not src.exists():
        raise ValueError(f"환경 없음: {old}")
    if dst.exists():
        raise ValueError(f"이미 있는 환경 이름: {new}")
    cfg = load_json(src, {})
    cfg["env_name"] = new
    save_json(dst, cfg)
    src.unlink()
    if old == env_name():
        c = json.loads(json.dumps(CFG))
        c["env_name"] = new
        _write_active_config(c)
    meta = load_dsmeta()
    for m in meta.values():
        if m.get("env") == old:
            m["env"] = new
    save_dsmeta(meta)
    d = load_projects()
    for pr in d["projects"].values():
        if pr.get("env") == old:
            pr["env"] = new
    save_projects(d)


def delete_env(name):
    if name == env_name():
        raise ValueError("사용 중인 환경은 지울 수 없습니다 — 다른 환경으로 전환한 뒤 지우세요")
    f = _env_file(name)
    if not f.exists():
        raise ValueError(f"환경 없음: {name}")
    f.unlink()


# 데이터셋 메타 — lerobot 파일은 건드리지 않고 OpenMimic 쪽에만 둡니다 {데이터셋: {env, created}}
def load_dsmeta():
    d = load_json(DSMETA_FILE, {})
    return d if isinstance(d, dict) else {}


def save_dsmeta(d):
    save_json(DSMETA_FILE, d)


# 프로젝트 = 한 가지 태스크 묶음 {task, env, datasets[], models[]}.
# 데이터셋·모델은 한 프로젝트에만 속합니다. 프로젝트가 없거나 '전체' 면 지금과 똑같이 동작합니다.
def load_projects():
    d = load_json(PROJECTS_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("active", "")
    d.setdefault("projects", {})
    for pr in d["projects"].values():
        pr.setdefault("task", "")
        pr.setdefault("env", "")
        pr.setdefault("datasets", [])
        pr.setdefault("models", [])
    if d["active"] not in d["projects"]:
        d["active"] = ""
    return d


def save_projects(d):
    save_json(PROJECTS_FILE, d)


def active_project():
    d = load_projects()
    name = d["active"]
    return (name, d["projects"][name]) if name else ("", None)


def _touch(pr):
    pr["updated"] = time.strftime("%Y-%m-%d %H:%M")


def project_of(kind, item):
    """kind: 'datasets' | 'models'"""
    for name, pr in load_projects()["projects"].items():
        if item in pr[kind]:
            return name
    return ""


def assign_to_project(kind, item, project):
    """item 을 project 로 옮깁니다 (빈 문자열이면 미분류)."""
    d = load_projects()
    if project and project not in d["projects"]:
        raise ValueError(f"프로젝트 없음: {project}")
    for pr in d["projects"].values():
        if item in pr[kind]:
            pr[kind].remove(item)
            _touch(pr)
    if project:
        d["projects"][project][kind].append(item)
        _touch(d["projects"][project])
    save_projects(d)


def rename_in_projects(kind, old, new):
    d = load_projects()
    for pr in d["projects"].values():
        if old in pr[kind]:
            pr[kind] = [new if x == old else x for x in pr[kind]]
            _touch(pr)
    save_projects(d)


def _clamp_int(v, default, lo, hi):
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


_ICON_CACHE = {}


def _solid_png(size, rgb):
    """의존성 없는 단색 PNG (PIL 없을 때 폴백)."""
    import struct
    import zlib
    w = h = size
    r, g, b = rgb
    raw = b"".join(b"\x00" + bytes((r, g, b)) * w for _ in range(h))

    def chunk(t, d):
        return (struct.pack(">I", len(d)) + t + d
                + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def icon_png(size):
    """앱 아이콘 PNG 생성 (PIL 있으면 로봇팔 마크, 없으면 단색)."""
    if size in _ICON_CACHE:
        return _ICON_CACHE[size]
    try:
        import io
        from PIL import Image, ImageDraw
        S = size
        img = Image.new("RGB", (S, S), (15, 18, 22))
        d = ImageDraw.Draw(img)
        pad = int(S * 0.11)
        rad = int(S * 0.22)
        d.rounded_rectangle([pad, pad, S - pad, S - pad], radius=rad,
                            fill=(44, 66, 87), outline=(93, 157, 214), width=max(2, S // 36))
        lw = max(3, S // 12)
        pts = [(S * 0.40, S * 0.72), (S * 0.40, S * 0.50), (S * 0.63, S * 0.40)]
        d.line(pts, fill=(93, 157, 214), width=lw, joint="curve")
        for x, y in pts:
            r = lw * 0.6
            d.ellipse([x - r, y - r, x + r, y + r], fill=(93, 157, 214))
        gx, gy, gr = S * 0.63, S * 0.40, S * 0.085
        d.ellipse([gx - gr, gy - gr, gx + gr, gy + gr],
                  outline=(220, 234, 254), width=max(2, S // 40))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        data = buf.getvalue()
    except Exception:
        data = _solid_png(size, (44, 66, 87))
    _ICON_CACHE[size] = data
    return data


def dir_size(root: Path):
    """폴더의 총 바이트와 파일 수. 심볼릭 링크는 따라가지 않습니다 (중복 계산 방지)."""
    total = count = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            f = Path(dirpath) / name
            if f.is_symlink():
                continue
            try:
                total += f.stat().st_size
                count += 1
            except OSError:
                pass
    return total, count


def human_bytes(n):
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{int(v)} {unit}" if unit == "B" else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def list_datasets():
    out = []
    if DATA_ROOT.exists():
        for d in sorted(DATA_ROOT.iterdir()):
            if (d / "meta/info.json").exists():
                info = load_json(d / "meta/info.json", {})
                nbytes, nfiles = dir_size(d)
                out.append({"name": d.name,
                            "episodes": info.get("total_episodes", "?"),
                            "frames": info.get("total_frames", "?"),
                            "fps": info.get("fps", "?"),
                            "robot_type": info.get("robot_type", ""),
                            "version": info.get("codebase_version", "?"),
                            "bytes": nbytes, "files": nfiles})
    return out


def dataset_unfinalized(ds):
    """녹화가 끊겨 meta/episodes 색인이 없는 데이터셋 (수집 중인 것은 제외 — 그건 끝날 때 씀)"""
    root = DATA_ROOT / ds
    if not (root / "meta/info.json").is_file() or dataset_busy(ds):
        return False
    # v2.x 는 meta/episodes.jsonl 이 원래 형식이라 대상이 아닙니다 (v3.0 만 meta/episodes/*.parquet)
    if load_json(root / "meta/info.json", {}).get("codebase_version") != "v3.0":
        return False
    ep = root / "meta" / "episodes"
    return not (ep.is_dir() and any(ep.rglob("*.parquet")))


REPAIR_PY = Path(__file__).resolve().parent / "tools_dsrepair.py"


@app.post("/api/dataset/repair/{ds}")
def api_dataset_repair(ds: str):
    if not safe_name(ds) or not (DATA_ROOT / ds / "meta/info.json").is_file():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=400)
    busy = dataset_busy(ds)
    if busy:
        return JSONResponse({"error": f"{busy['id']} 가 이 데이터셋을 쓰는 중"}, status_code=400)
    try:
        jid = start_job("repair", [sys.executable, str(REPAIR_PY), str(DATA_ROOT / ds)],
                        spec={"root": str(DATA_ROOT / ds)})
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid}


def checkpoint_robot_type(rel):
    """pretrained_model/train_config.json → dataset.repo_id → 로컬 데이터셋 robot_type (best-effort)."""
    tc = load_json(OUT_ROOT / rel / "train_config.json", {})
    repo = ((tc.get("dataset") or {}).get("repo_id") or "")
    name = repo.split("/", 1)[-1] if repo else ""
    if not safe_name(name):
        return ""
    return load_json(DATA_ROOT / name / "meta/info.json", {}).get("robot_type", "")


def list_checkpoints():
    """outputs/*/checkpoints/*/pretrained_model 탐색.
    'last'가 숫자 체크포인트를 가리키는 심볼릭 링크면 중복 제거(숫자 쪽 유지)."""
    entries = []   # (rel, is_last, real_target)
    for run in sorted(OUT_ROOT.iterdir()) if OUT_ROOT.exists() else []:
        ck = run / "checkpoints"
        if not ck.is_dir():
            continue
        for step in sorted(ck.iterdir()):
            pm = step / "pretrained_model"
            if pm.is_dir():
                rel = f"{run.name}/checkpoints/{step.name}/pretrained_model"
                entries.append((rel, step.name == "last", str(pm.resolve())))
    entries.sort(key=lambda e: (e[2], e[1]))
    seen, found = set(), []
    for rel, _is_last, real in entries:
        if real in seen:
            continue
        seen.add(real)
        found.append(rel)
    found.sort(key=lambda r: ("/last/" not in f"/{r}/", r))
    return found


def episodes_df(ds):
    if not safe_name(ds):
        return pd.DataFrame()
    files = sorted(glob.glob(str(DATA_ROOT / ds / "meta/episodes/*/*.parquet")))
    if not files:
        return pd.DataFrame()
    return pd.concat([pd.read_parquet(f) for f in files]).reset_index(drop=True)


def video_keys(df):
    return sorted({m.group(1) for c in df.columns
                   if (m := re.match(r"videos/(.+)/from_timestamp", c))})


def ep_video_segments(df, ep):
    row = df[df["episode_index"] == ep]
    if row.empty:
        return []
    row = row.iloc[0]
    segs = []
    for k in video_keys(df):
        try:
            chunk = int(row.get(f"videos/{k}/chunk_index", 0))
            fidx = int(row.get(f"videos/{k}/file_index", 0))
            segs.append({"cam": k.split(".")[-1],
                         "path": f"{k}/chunk-{chunk:03d}/file-{fidx:03d}.mp4",
                         "from": float(row.get(f"videos/{k}/from_timestamp", 0)),
                         "to": float(row.get(f"videos/{k}/to_timestamp", 0))})
        except Exception:
            continue
    # 전경 카메라(top 등)를 앞에 — 썸네일·첫 칸에 손목 카메라보다 알아보기 쉽습니다
    segs.sort(key=lambda sg: (0 if sg["cam"] in ("top", "overview", "front", "scene") else 1, sg["cam"]))
    return segs


# ----------------------------- lerobot CLI 인자 생성 --------------------------
def _cam_cli(specs):
    """draccus 가 파싱하는 카메라 dict 리터럴 (셸을 안 거치므로 따옴표 없음)."""
    items = []
    for name, s in specs.items():
        idx = s["index_or_path"]
        idx = idx if isinstance(idx, int) else str(idx)
        fc = cam_fourcc(s)
        items.append(f"{name}: {{type: opencv, index_or_path: {idx}, "
                     f"width: {int(s['width'])}, height: {int(s['height'])}, fps: {int(s['fps'])}"
                     + (f", fourcc: {fc}" if fc else "") + "}")
    return "{" + ", ".join(items) + "}"


# ----------------------------- 기종 (SO-ARM101 / OMX) -------------------------
# 기종마다 다른 것은 전부 이 표에 둡니다. 나머지 코드는 kind() 로 꺼내 씁니다.
# 한팔/양팔(mode) 과는 독립 — 두 기종 모두 한팔·양팔을 지원합니다.
ROBOT_KINDS = {
    "so101": {
        "label": "SO-ARM101",
        "follower": ("lerobot.robots.so_follower", "SOFollower", "SOFollowerRobotConfig"),
        "leader": ("lerobot.teleoperators.so_leader", "SOLeader", "SOLeaderTeleopConfig"),
        "bi_follower": ("lerobot.robots.bi_so_follower", "BiSOFollower", "BiSOFollowerConfig"),
        "bi_leader": ("lerobot.teleoperators.bi_so_leader", "BiSOLeader", "BiSOLeaderConfig"),
        "arm_cfg": ("lerobot.robots.so_follower", "SOFollowerConfig"),
        "leader_arm_cfg": ("lerobot.teleoperators.so_leader", "SOLeaderConfig"),
        "cli": {"follower": "so101_follower", "leader": "so101_leader",
                "bi_follower": "bi_so_follower", "bi_leader": "bi_so_leader"},
        "calib_dir": {"follower": "robots/so_follower", "leader": "teleoperators/so_leader"},
        "robot_type": {"single": "so_follower", "bi": "bi_so_follower"},
        "types": {"single": {"so_follower", "so101_follower", "so100_follower"},
                  "bi": {"bi_so_follower", "bi_so101_follower", "bi_so100_follower"}},
        "use_degrees": True,          # 관절값 단위: ° (그리퍼만 0~100)
        "deg_per_unit": 1.0,
        "unit": "°",
        "bus": ("lerobot.motors.feetech", "FeetechMotorsBus"),
        "homing_sign": 1,             # Feetech: Present = Actual - Homing_Offset
        "leader_torque": False,       # SO 리더는 토크를 쓰지 않습니다
        "calib": "range",             # 관절 범위를 손으로 기록
        "urdf": "so101.urdf",
        "plugin": None,
    },
    "omx": {
        "label": "OMX",
        "follower": ("lerobot.robots.omx_follower", "OmxFollower", "OmxFollowerConfig"),
        "leader": ("lerobot.teleoperators.omx_leader", "OmxLeader", "OmxLeaderConfig"),
        # 양팔은 lerobot 에 없어서 이 레포의 plugins/ (lerobot_conda.sh 가 설치)
        "bi_follower": ("lerobot_robot_bi_omx", "BiOmxFollower", "BiOmxFollowerConfig"),
        "bi_leader": ("lerobot_teleoperator_bi_omx", "BiOmxLeader", "BiOmxLeaderConfig"),
        "arm_cfg": ("lerobot_robot_bi_omx", "OmxArmConfig"),
        "leader_arm_cfg": ("lerobot_teleoperator_bi_omx", "OmxLeaderArmConfig"),
        "cli": {"follower": "omx_follower", "leader": "omx_leader",
                "bi_follower": "bi_omx_follower", "bi_leader": "bi_omx_leader"},
        "calib_dir": {"follower": "robots/omx_follower", "leader": "teleoperators/omx_leader"},
        "robot_type": {"single": "omx_follower", "bi": "bi_omx_follower"},
        "types": {"single": {"omx_follower"}, "bi": {"bi_omx_follower"}},
        # omx_leader 는 -100~100 고정(use_degrees 없음) — 팔로워도 맞춰야 리더를 그대로 따라갑니다
        "use_degrees": False,
        "deg_per_unit": 360.0 / 200.0,   # 0~4095 전체 = -100~100 → 1 단위 ≈ 1.8°
        "unit": "",
        "bus": ("lerobot.motors.dynamixel", "DynamixelMotorsBus"),
        "homing_sign": -1,            # Dynamixel: Present = Actual + Homing_Offset
        "leader_torque": True,        # OMX 리더 그리퍼는 전류 제어로 토크가 걸려 있습니다 (트리거 방식)
        "calib": "factory",           # lerobot 이 공장값(오프셋 0, 0~4095)을 씁니다 — 범위 기록 없음
        "urdf": "omx_f.urdf",
        "plugin": ("lerobot_robot_bi_omx", "lerobot_teleoperator_bi_omx"),
    },
}
DEFAULT_ROBOT = "so101"


def robot_key(cfg=None):
    r = (cfg if cfg is not None else CFG).get("robot") or DEFAULT_ROBOT
    return r if r in ROBOT_KINDS else DEFAULT_ROBOT


def kind(cfg=None):
    return ROBOT_KINDS[robot_key(cfg)]


_PLUGIN_DIR = Path(__file__).resolve().parent / "plugins"


def _load(mod, name):
    """kind 표의 (모듈, 이름) 을 import. 양팔 OMX 플러그인이 pip 로 안 깔려 있으면 레포의 plugins/ 에서 찾습니다
    (OpenMimic 자신은 이걸로 충분하지만, lerobot CLI(롤아웃) 는 설치돼 있어야 합니다)."""
    import importlib
    try:
        m = importlib.import_module(mod)
    except ModuleNotFoundError:
        pdir = _PLUGIN_DIR / mod
        if not (pdir / mod / "__init__.py").exists():
            raise
        sys.path.insert(0, str(pdir))
        m = importlib.import_module(mod)
    return getattr(m, name)


def plugin_missing(k=None):
    """lerobot CLI 가 양팔 OMX 를 쓰려면 플러그인이 '설치' 돼 있어야 합니다. 빠진 패키지 이름 목록."""
    import importlib.metadata as md
    k = k or kind()
    out = []
    for name in (k.get("plugin") or ()):
        try:
            md.distribution(name)
        except md.PackageNotFoundError:
            out.append(name)
    return out


def make_arm(role, arm_cfg=None, *, k=None, port=None, cameras=None, mrt=None):
    """팔 하나의 lerobot 객체(연결 안 함). role = follower | leader.
    기종에 맞는 클래스·단위(use_degrees)·캘리브레이션 폴더가 정해집니다."""
    k = k or kind()
    mod, cls_name, cfg_name = k[role]
    cls, cfg_cls = _load(mod, cls_name), _load(mod, cfg_name)
    arm_cfg = arm_cfg or {}
    kw = {"id": arm_cfg.get(f"{role}_id") or role,
          "port": port if port is not None else (arm_cfg.get(f"{role}_port") or "")}
    if role == "follower":
        kw.update(use_degrees=k["use_degrees"], max_relative_target=mrt,
                  cameras=cameras if cameras is not None else {})
    elif k["use_degrees"]:
        kw["use_degrees"] = True          # SO 리더만 use_degrees 가 있습니다 (OMX 리더는 -100~100 고정)
    return cls(cfg_cls(**kw))


def make_bi(role, arm_cfgs, *, k=None, cameras=None, mrt=None, arm_cameras=None):
    """양팔 lerobot 객체(연결 안 함). arm_cfgs = {"left": arm, "right": arm}."""
    k = k or kind()
    mod, cls_name, cfg_name = k[f"bi_{role}"]
    cls, cfg_cls = _load(mod, cls_name), _load(mod, cfg_name)
    L, R = arm_cfgs["left"], arm_cfgs["right"]
    if role == "follower":
        arm_cls = _load(*k["arm_cfg"])
        mk = lambda a: arm_cls(port=a["follower_port"], max_relative_target=mrt,
                               use_degrees=k["use_degrees"], cameras=_cam_configs(a["cameras"]))
        return cls(cfg_cls(id=bimanual_base_id("follower", arm_cfgs), left_arm_config=mk(L),
                           right_arm_config=mk(R), cameras=cameras if cameras is not None else {}))
    arm_cls = _load(*k["leader_arm_cfg"])
    mk = lambda a: arm_cls(port=a["leader_port"])
    return cls(cfg_cls(id=bimanual_base_id("leader", arm_cfgs), left_arm_config=mk(L), right_arm_config=mk(R)))


def make_bus(role, port, *, k=None, calib_id=None):
    """포트 하나를 기종·역할에 맞는 모터 구성으로 여는 버스 객체(연결 안 함).
    lerobot 장치 클래스가 만드는 버스를 그대로 씁니다 (모터 ID·모델·정규화 방식이 lerobot 과 같아짐).
    calib_id 를 주면 그 캘리브레이션 파일이 버스에 실려 있습니다."""
    dev = make_arm(role, {f"{role}_id": calib_id or role}, k=k, port=port)
    return dev.bus


def kind3d(k=None):
    """브라우저 3D 용 기종 정보 — URDF, lerobot 관절 이름 → URDF 관절 이름, 단위 → 라디안.
    SO-ARM101: 관절값이 ° → ×π/180, 그리퍼 0~100 → URDF 그리퍼 한계 사이.
    OMX: 관절값이 -100~100 (0~4095 전체) → ×π/100. URDF 의 0 rad 가 엔코더 2048 이라고 가정합니다.
         그리퍼 0~100 이 실제 몇 rad 인지는 URDF 에 없어서(±2π) 추정값입니다 — 실기 확인 후 조정 (확인 필요)."""
    import math
    k = k or kind()
    if k is ROBOT_KINDS["omx"]:
        return {"urdf": "/urdf/" + k["urdf"], "scale": math.pi / 100,
                "map": {"shoulder_pan": "joint1", "shoulder_lift": "joint2", "elbow_flex": "joint3",
                        "wrist_flex": "joint4", "wrist_roll": "joint5", "gripper": "gripper_joint_1"},
                "sign": {}, "gripper": {"lo": 0.0, "hi": 0.9}, "motor_mat": None}
    return {"urdf": "/urdf/" + k["urdf"], "scale": math.pi / 180, "map": {j: j for j in CTL_JOINTS},
            "sign": {}, "gripper": None, "motor_mat": "sts3215"}


def robot_name():
    """lerobot 이 데이터셋 meta/info.json 의 robot_type 에 기록하는 이름."""
    return kind()["robot_type"]["bi" if BIMANUAL else "single"]


def robot_type_ok(rt):
    """데이터셋/체크포인트의 robot_type 이 지금 기종·모드와 맞는지. 비어 있으면(모름) 통과.
    예전 lerobot 은 so101_follower 로 기록했으므로 같은 계열로 봅니다."""
    if not rt:
        return True
    return rt in kind()["types"]["bi" if BIMANUAL else "single"]


def bimanual_base_id(role, arm_cfgs=None):
    """양팔에서 lerobot BiSO* 는 per-arm 캘리브레이션 id 를 '{id}_left' / '{id}_right' 로 만듭니다.
    설정의 follower_id 가 'X_left' / 'X_right' 면 BiSOFollowerConfig(id='X') 가 됩니다."""
    arm_cfgs = arm_cfgs or ARM_CFGS
    left = arm_cfgs["left"][f"{role}_id"]
    right = arm_cfgs["right"][f"{role}_id"]
    if not (left.endswith("_left") and right.endswith("_right") and left[:-5] == right[:-6] and left[:-5]):
        raise ValueError(f"양팔 {role} id 는 같은 이름에 _left / _right 를 붙여야 합니다 "
                         f"(예: {role}_left / {role}_right) — 현재 {left} / {right}")
    return left[:-5]


def _need_ports(role):
    for side, arm in ARM_CFGS.items():
        if not arm.get(f"{role}_port"):
            raise NotImplementedError(
                f"{side + ' ' if BIMANUAL else ''}{'팔로워' if role == 'follower' else '리더'} 포트가 "
                f"지정되지 않았습니다 — 셋업 탭에서 먼저 설정하세요")


def mrt_value(v):
    """max_relative_target 설정값 → float 또는 None.
    ensure_safe_goal_position 은 float 만 받습니다 (int 면 TypeError) — 반드시 캐스팅."""
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
        return float(v)
    return None


def robot_cli_args():
    """lerobot CLI(rollout) 용 --robot.* 인자. 기종·한팔/양팔에 따라 type 이 정해집니다
    (so101_follower / bi_so_follower / omx_follower / bi_omx_follower)."""
    _need_ports("follower")
    k = kind()
    # Control·Collect 와 같은 안전 제한을 롤아웃에도 겁니다 (정책 출력이 튀면 한 스텝 이동량을 자름)
    mrt = mrt_value(CFG.get("max_relative_target"))
    if BIMANUAL:
        L, R = ARM_CFGS["left"], ARM_CFGS["right"]
        args = [f"--robot.type={k['cli']['bi_follower']}",
                f"--robot.id={bimanual_base_id('follower')}",
                f"--robot.left_arm_config.port={L['follower_port']}",
                f"--robot.left_arm_config.cameras={_cam_cli(L['cameras'])}",
                f"--robot.right_arm_config.port={R['follower_port']}",
                f"--robot.right_arm_config.cameras={_cam_cli(R['cameras'])}",
                f"--robot.cameras={_cam_cli(CFG['cameras'])}"]
        if mrt is not None:
            args += [f"--robot.left_arm_config.max_relative_target={mrt}",
                     f"--robot.right_arm_config.max_relative_target={mrt}"]
        return args
    arm = ARM_CFGS[SIDES[0]]
    cams = dict(arm["cameras"])
    cams.update(CFG["cameras"])
    args = [f"--robot.type={k['cli']['follower']}",
            f"--robot.port={arm['follower_port']}",
            f"--robot.id={arm['follower_id']}",
            f"--robot.cameras={_cam_cli(cams)}"]
    if mrt is not None:
        args.append(f"--robot.max_relative_target={mrt}")
    return args


def teleop_cli_args():
    _need_ports("leader")
    k = kind()
    if BIMANUAL:
        L, R = ARM_CFGS["left"], ARM_CFGS["right"]
        return [f"--teleop.type={k['cli']['bi_leader']}",
                f"--teleop.id={bimanual_base_id('leader')}",
                f"--teleop.left_arm_config.port={L['leader_port']}",
                f"--teleop.right_arm_config.port={R['leader_port']}"]
    arm = ARM_CFGS[SIDES[0]]
    return [f"--teleop.type={k['cli']['leader']}",
            f"--teleop.port={arm['leader_port']}",
            f"--teleop.id={arm['leader_id']}"]


# ----------------------------- 작업(job) 관리 --------------------------------
def pid_alive(pid):
    if not pid:
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


def _proc_start(pid):
    """/proc/<pid>/stat 의 starttime(22번째 필드). PID 가 재사용됐는지 가리는 데 씁니다."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except Exception:
        return None


def job_alive(j):
    """pid 가 살아 있고, 그 pid 가 이 작업을 시작할 때의 프로세스와 같은지까지 확인합니다.
    (끝난 작업의 pid 를 다른 프로세스가 물려받으면 '실행 중' 으로 보이고 중지 버튼이 남의 프로세스를 죽입니다)"""
    pid = j.get("pid")
    if not pid_alive(pid):
        return False
    st = j.get("pid_start")
    return st is None or _proc_start(pid) == st


def jobs_index():
    idx = []
    for jf in sorted(JOB_DIR.glob("*.json"), reverse=True):
        j = load_json(jf, {})
        if j:
            j["alive"] = job_alive(j)
            idx.append(j)
    return idx


def job_uses_dataset(j, ds):
    """작업이 데이터셋 ds 를 쓰는지 — 녹화는 spec, 학습/편집은 CLI 인자로 정확히 비교합니다.
    (예전처럼 cmd 문자열에 이름이 들어 있는지로 보면 녹화 작업은 못 잡고, abc 가 abc_2 에도 걸립니다)"""
    root = os.path.realpath(DATA_ROOT / ds)
    spec = j.get("spec") or {}
    if spec.get("repo_id") == f"local/{ds}" or (spec.get("root") and os.path.realpath(spec["root"]) == root):
        return True
    args = j.get("argv") or (j.get("cmd") or "").split()
    for i, a in enumerate(args):
        k, sep, v = a.partition("=")
        if not sep and i + 1 < len(args):        # '--root X' 형태
            v = args[i + 1]
        if k in ("--dataset.repo_id", "--repo_id") and v == f"local/{ds}":
            return True
        if k in ("--dataset.root", "--root", "--new_root") and v and os.path.realpath(v) == root:
            return True
    return False


def dataset_busy(ds):
    return next((j for j in jobs_index() if j["alive"] and job_uses_dataset(j, ds)), None)


def child_env():
    """lerobot 이 pynput 전역 리스너 대신 터미널(PTY) 리스너를 쓰도록 강제.
    pynput 쪽으로 붙으면 PTY 로 넣는 n/r/q 가 조용히 버려집니다."""
    env = dict(os.environ)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["XDG_SESSION_TYPE"] = "tty"
    return env


class JobStartError(RuntimeError):
    pass


def run_dir(jid):
    return RUN_DIR / jid


def _within(p, root):
    """p(이미 resolve 된 경로)가 root 안에 있는지. 문자열 접두사 비교는 outputs2 같은 형제 폴더도 통과시킵니다."""
    try:
        return Path(p).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def start_job(kind, argv, cwd=None, spec=None):
    """argv 는 반드시 리스트 — shell=False 이므로 셸 인젝션이 불가능합니다.
    spec 은 worker 가 읽을 작업 명세(dict) — job json 에 같이 저장됩니다."""
    if shutil.which(argv[0]) is None:
        raise JobStartError(f"실행 파일을 찾을 수 없습니다: {argv[0]} — openmimic conda env 안에서 OpenMimic 을 띄웠는지 확인")
    jid = f"{kind}_{time.strftime('%m%d_%H%M%S')}"
    n = 2
    while (JOB_DIR / f"{jid}.json").exists():     # 같은 초에 두 개 → 로그·pid 덮어쓰기 방지
        jid = f"{kind}_{time.strftime('%m%d_%H%M%S')}_{n}"
        n += 1
    log = JOB_DIR / f"{jid}.log"
    # worker 가 자기 job json 을 읽으므로 프로세스보다 먼저 써야 합니다
    argv = [str(a).replace("{jid}", jid) for a in argv]
    save_json(JOB_DIR / f"{jid}.json",
              {"id": jid, "kind": kind, "cmd": " ".join(argv), "argv": argv, "pid": None,
               "log": str(log), "started": time.strftime("%F %T"), "spec": spec})
    lf = open(log, "w")
    try:
        p = subprocess.Popen(argv, cwd=cwd or str(HOME), stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT,
                             env=child_env(), start_new_session=True)
    finally:
        lf.close()      # 자식이 dup 를 들고 있으므로 부모 쪽은 닫습니다 (fd 누수 방지)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    j["pid"] = p.pid
    j["pid_start"] = _proc_start(p.pid)
    j["cmd"] = " ".join(argv)
    save_json(JOB_DIR / f"{jid}.json", j)
    return jid


def send_cmd(jid, key):
    """record worker 에 n/r/q 전달 — 파일 큐. OpenMimic 을 재시작해도 그대로 동작합니다."""
    if not safe_name(jid):
        return False
    rd = run_dir(jid)
    try:
        rd.mkdir(parents=True, exist_ok=True)
        with open(rd / "cmd", "a") as f:
            f.write(key)
        return True
    except OSError:
        return False


def record_status(jid):
    if not safe_name(jid):
        return {}
    return load_json(run_dir(jid) / "status.json", {})


def kill_job(jid, force=False):
    """force=False: 1 번째 SIGINT(정상 종료 요청), 2 번째부터 SIGKILL.
    force=True : 곧바로 SIGKILL. 프로세스 그룹 전체에 보냅니다
    (start_job 이 os.setsid 로 새 세션을 열어 두므로 OpenMimic 자신은 안 맞습니다)."""
    if not safe_name(jid):
        return False
    jf = JOB_DIR / f"{jid}.json"
    j = load_json(jf, {})
    if not j.get("pid") or not job_alive(j):
        return False
    sig = signal.SIGKILL if (force or j.get("kill_requested")) else signal.SIGINT
    try:
        os.killpg(os.getpgid(j["pid"]), sig)
    except OSError:
        return False
    j["kill_requested"] = True
    save_json(jf, j)
    if j.get("kind") == "cloudtrain":
        # lerobot 은 Ctrl-C 를 '로그 분리' 로만 처리해 원격 학습이 계속 돌고 과금됩니다 — 원격도 취소
        threading.Thread(target=cancel_cloud, args=(j,), daemon=True).start()
    return True


def delete_job(jid):
    if not safe_name(jid):
        return False
    j = load_json(JOB_DIR / f"{jid}.json", {})
    if j and job_alive(j):
        return False
    for suffix in (".json", ".log"):
        try:
            (JOB_DIR / f"{jid}{suffix}").unlink()
        except FileNotFoundError:
            pass
    shutil.rmtree(run_dir(jid), ignore_errors=True)
    return True


_LOGREC_RE = re.compile(r"(?:INFO|WARNING|ERROR|DEBUG) \d{4}-\d\d-\d\d ")


def collapse_cr(text):
    """진행 막대(tqdm 등)는 줄바꿈 없이 \r 로 같은 줄을 덮어씁니다 — 터미널처럼 줄마다 마지막 내용만 남깁니다.
    lerobot 의 step 로그(INFO … step:200 …)는 막대 바로 뒤에 붙어 나오므로 떼어 내 따로 남깁니다."""
    out = []
    for line in text.split("\n"):
        if "\r" in line:
            logs, bar = [], ""
            for seg in line.split("\r"):
                m = _LOGREC_RE.search(seg)
                if m:
                    logs.append(seg[m.start():])
                    seg = seg[:m.start()]
                if seg.strip():
                    bar = seg
            out.extend(logs)
            line = bar
        out.append(line)
    return "\n".join(out)


def log_tail(jid, nbytes=65536):
    if not safe_name(jid):
        return ""
    j = load_json(JOB_DIR / f"{jid}.json", {})
    try:
        with open(j["log"], "rb") as f:
            f.seek(max(-nbytes, -os.path.getsize(j["log"])), 2)
            return collapse_cr(f.read().decode(errors="ignore"))[-3200:]
    except Exception:
        return ""


# ----------------------------- 팔 연결 · 해제 공용 -----------------------------
def _write_nothrow(bus, motor, reg, value, tries=3):
    """레지스터 쓰기 — 서보가 에러 비트(과부하 보호 등)를 돌려줘도 예외를 내지 않습니다.
    lerobot bus.write 는 에러 비트만 있어도 RuntimeError 라, 과부하로 멈춘 모터 하나 때문에
    나머지 모터의 토크 해제까지 건너뛰게 됩니다. 반환: 통신 성공 여부."""
    from lerobot.motors.motors_bus import get_address
    m = bus.motors[motor]
    try:
        addr, n = get_address(bus.model_ctrl_table, m.model, reg)
    except KeyError:
        return False                    # 이 모터에는 없는 레지스터 (Dynamixel 에는 Lock 이 없음)
    for _ in range(tries):
        try:
            comm, _err = bus._write(addr, n, m.id, value, raise_on_error=False)
        except Exception:
            continue
        if bus._is_comm_success(comm):
            return True
    return False


def torque_off_all(bus):
    """모든 모터 Torque_Enable=0 (+ Lock=0). 한 모터가 실패해도 끝까지 갑니다. 반환: 실패한 모터 목록."""
    try:
        bus.port_handler.clearPort()
        bus.port_handler.is_using = False
    except Exception:
        pass
    failed = []
    for motor in bus.motors:
        if not _write_nothrow(bus, motor, "Torque_Enable", 0):
            failed.append(motor)
        _write_nothrow(bus, motor, "Lock", 0, tries=1)     # Feetech 만 (Dynamixel 은 건너뜀)
    return failed


def close_arm(dev, disable_torque=True):
    """SOFollower/SOLeader 하나를 최대한 닫습니다. 연결 도중 실패해 is_connected 가 False 여도
    (카메라 하나 실패 등) 열린 포트·카메라를 모두 정리합니다. 예외를 내지 않습니다."""
    bus = getattr(dev, "bus", None)
    try:
        if bus is not None and bus.is_connected:
            if disable_torque:
                torque_off_all(bus)
            bus.port_handler.closePort()
    except Exception:
        pass
    for cam in (getattr(dev, "cameras", None) or {}).values():
        try:
            if cam.is_connected:
                cam.disconnect()
        except Exception:
            pass


def connect_follower(dev):
    """SOFollower.connect(calibrate=False) 와 같은 일을 하되 순서를 안전하게 바꿉니다.

    lerobot 원본은 configure() 를 'with bus.torque_disabled()' 안에서 돌리고, 빠져나올 때
    토크를 다시 켭니다 (+ Lock=1). 그러면 ① RAM 에 남은 이전 Goal_Position 으로 팔이 튀고
    ② 그 뒤에 쓰는 캘리브레이션(Homing_Offset)이 토크가 켜진 채로 들어가 기준점이 움직입니다.
    여기서는 토크를 끈 상태에서 캘리브레이션을 먼저 쓰고, Goal_Position=현재 위치로 맞춘 뒤 configure 합니다.
    끝나면 lerobot 과 똑같이 토크가 켜진 상태입니다. 실패하면 포트·카메라를 닫고 예외를 다시 던집니다."""
    bus = dev.bus
    try:
        # lerobot bus.connect 는 핸드셰이크(모터 확인) 실패 때 포트를 연 채로 예외를 냅니다 — 여기서 같이 닫습니다
        bus.connect()
        torque_off_all(bus)
        if dev.calibration and not bus.is_calibrated:
            bus.write_calibration(dev.calibration)
        pos = bus.sync_read("Present_Position", normalize=False)
        bus.sync_write("Goal_Position", pos, normalize=False)
        for cam in dev.cameras.values():
            cam.connect()
        dev.configure()
    except Exception:
        close_arm(dev)
        raise


def connect_leader(dev):
    """리더 connect(calibrate=False) + 캘리브레이션 파일을 보드에 씀.
    SO 리더는 configure 가 토크를 끕니다. OMX 리더는 그리퍼만 토크를 켭니다(손가락 트리거)."""
    try:
        dev.connect(calibrate=False)
        if dev.calibration and not dev.bus.is_calibrated:
            dev.bus.write_calibration(dev.calibration)
    except Exception:
        close_arm(dev, disable_torque=False)
        raise


def power_gate(bus, role, arm_cfg):
    """연결된 버스로 시작 전 전원 점검. 막을 조건이면 RuntimeError, 아니면 경고 문장 목록."""
    try:
        p = openmimic_health.read_board_power(bus)
    except Exception as e:      # noqa: BLE001
        return [f"전압을 읽지 못했습니다 ({type(e).__name__})"]
    level, msgs = _power_judge(role, arm_cfg, p)
    if level == "block":
        raise RuntimeError("전원 점검 실패 — " + " / ".join(msgs))
    return msgs


# ----------------------------- 수동 제어 (제어 탭) -------------------------
class ArmCtl:
    """lerobot SOFollower 래퍼.

    SOFollower.connect() 안에서 configure() 가 돌면서 Operating_Mode / P·I·D 계수 /
    gripper 의 Max_Torque_Limit·Protection_Current·Overload_Torque 까지 세팅됩니다.
    (직접 FeetechMotorsBus 를 열면 이게 전부 빠집니다 — 그리퍼 소손 원인)

    cameras 는 비워 둡니다. SOFollower.is_connected 가 '버스 AND 모든 카메라' 라서
    카메라 하나가 빠지면 팔 제어·E-STOP 까지 같이 죽기 때문입니다.
    카메라는 CamStreamer 가 따로 담당합니다.
    """

    def __init__(self, arm_cfg):
        self.cfg = arm_cfg
        self.side = arm_cfg["side"]
        self.robot = None
        self.torque = False
        self.target = {}   # 사용자가 원하는 최종 목표 (슬라이더 / 리더)
        self.cmd = {}      # 명령 적분기 — target 을 향해 제한 속도로 이동, 엔코더와 안 섞음
        self.actual = {}   # 엔코더 실측 (표시/3D 전용)
        self.limits = {}
        self.err = ""
        self.follow = False
        self.track = {}    # 명령(cmd) 대비 실측 오차 — 서보가 실제로 따라오는지
        self.temp = {}     # 서보 온도(°C)
        self.ten = {}      # Torque_Enable 실제 값 (서보가 스스로 토크를 뺐는지)
        self.load = {}     # Present_Load — 버티는 중이면 큽니다
        self.power_warn = []   # 연결할 때 전원 점검 경고
        self.guard = None      # openmimic_health.HealthGuard — 온도·전압·보호 플래그
        self.health_stop = ""  # 감시가 멈춘 이유 (리더 팔로우를 끄고 그 자리에 멈춤)
        self._last_goal = {}
        self._last_temp = 0.0
        self.lock = threading.Lock()   # 시리얼은 스레드 안전하지 않음 — 루프/명령 직렬화
        self.leader = None
        self.running = False
        self.thread = None

    # --- 연결 ---------------------------------------------------------------
    @property
    def connected(self):
        return self.robot is not None

    def calib_path(self):
        return calib_file("follower", self.cfg["follower_id"])

    def connect(self):
        if not self.cfg.get("follower_port"):
            raise RuntimeError("팔로워 포트가 지정되지 않았습니다 — 셋업 탭에서 먼저 설정하세요")
        mrt = mrt_value(CFG.get("max_relative_target"))
        robot = make_arm("follower", self.cfg, mrt=mrt)     # 기종(SO-ARM101/OMX)에 맞는 lerobot 클래스
        if not robot.calibration:
            raise RuntimeError(
                f"캘리브레이션 파일이 없습니다: {robot.calibration_fpath} — 캘리브레이션 탭에서 만드세요")
        # 캘리브레이션 파일을 보드에 쓰고(어긋난 경우) configure 까지 — 토크가 켜진 채로 끝납니다
        connect_follower(robot)
        try:
            self.power_warn = power_gate(robot.bus, "follower", self.cfg)     # 막을 전원이면 여기서 예외
            self.health_stop = ""
            self.guard = openmimic_health.HealthGuard([("", robot.bus)])
            try:
                self.guard.start()
            except Exception:      # noqa: BLE001 — 감시를 못 켜도 제어는 됩니다
                self.guard = None
            # Control 은 '토크 꺼짐' 으로 시작합니다 (화면 표시와 실제를 일치시킴).
            # 켤 때는 set_torque 가 현재 위치를 목표로 쓰고 켭니다.
            failed = torque_off_all(robot.bus)
            if failed:
                raise RuntimeError(f"토크 해제 실패: {', '.join(failed)} — 전원·케이블 확인")
            self.robot = robot
            self._build_limits()
            self.actual = self.read()
        except Exception:
            self.robot = None
            close_arm(robot)
            raise
        self.target = dict(self.actual)
        self.cmd = dict(self.actual)
        self.track = dict.fromkeys(CTL_JOINTS, 0.0)
        self.temp = {}
        self.ten = {}
        self.load = {}
        self._last_goal = {}
        self._last_temp = 0.0

    def _build_limits(self):
        from lerobot.motors import MotorNormMode
        bus = self.robot.bus
        for name in CTL_JOINTS:
            mode = getattr(bus.motors.get(name), "norm_mode", None)
            if name == "gripper" or mode == MotorNormMode.RANGE_0_100:
                self.limits[name] = (0.0, 100.0)     # MotorNormMode.RANGE_0_100
                continue
            if mode == MotorNormMode.RANGE_M100_100:
                self.limits[name] = (-100.0, 100.0)  # OMX: 캘리브레이션 범위 전체가 -100~100
                continue
            c = bus.calibration.get(name)
            if c is None:
                self.limits[name] = (-170.0, 170.0)
                continue
            # lerobot MotorsBus._normalize(DEGREES) 와 동일한 식
            max_res = bus.model_resolution_table[bus.motors[name].model] - 1
            mid = (c.range_min + c.range_max) / 2
            lo = (c.range_min - mid) * 360 / max_res
            hi = (c.range_max - mid) * 360 / max_res
            if lo > hi:
                lo, hi = hi, lo
            self.limits[name] = (round(lo, 1), round(hi, 1))

    def disconnect(self):
        self.stop_loop()
        with self.lock:
            if self.robot is not None:
                close_arm(self.robot)           # 토크 해제(모터별로 끝까지) + 포트 닫기
            self.robot = None
            self.torque = False
            self.follow = False
            self.err = ""

    # --- 팔별 제어 스레드 (4단계) ---------------------------------------------
    # 팔마다 스레드 하나: 리더 읽기 → 적분기 step → 주기적 실측. 팔이 늘어도 왕복 지연이
    # 직렬로 쌓이지 않습니다. WebSocket 은 상태를 퍼가기만 합니다.
    def start_loop(self, leader):
        self.leader = leader
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, name=f"arm-{self.side}", daemon=True)
        self.thread.start()

    def stop_loop(self):
        self.running = False
        t, self.thread = self.thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.5)

    def _loop(self):
        last_fb = 0.0
        while self.running and self.connected:
            t0 = time.monotonic()
            try:
                with self.lock:
                    if self.robot is None:
                        break
                    ldr = self.leader
                    if self.follow and ldr is not None and ldr.connected:
                        for n, v in ldr.read().items():
                            if n in CTL_JOINTS:
                                self.target[n] = v
                    self.step()
                    if t0 - last_fb > 1.0 / FEEDBACK_HZ:
                        self.actual = self.read()
                        last_fb = t0
                        # 서보가 명령을 실제로 따라오는지. 벌어져 있으면 막혔거나
                        # 과부하 보호로 토크가 빠진 것입니다 (조용히 틀린 데이터를 쌓는 걸 막음)
                        self.track = {n: round(self.actual.get(n, 0.0)
                                               - self.cmd.get(n, self.actual.get(n, 0.0)), 1)
                                      for n in CTL_JOINTS}
                    if t0 - self._last_temp > TEMP_READ_S:
                        self._last_temp = t0
                        # 안 따라가는 원인을 가릅니다:
                        #   Torque_Enable=0 → 서보가 과부하 보호로 스스로 토크를 뺌
                        #   Torque_Enable=1 + 부하 큼 → 기계적으로 막힘 (곧 과열)
                        #   Torque_Enable=1 + 부하 ~0 → 명령 미도달 / 서보 이상
                        # Dynamixel(OMX) 에는 Present_Load 가 없어서 Present_Current 로 대신합니다
                        if self.guard is not None:
                            self.guard.poll(force=True)
                            self.temp = dict(self.guard.temp)
                            self.health_stop = self.guard.stopping
                            if self.health_stop:
                                # 제어 탭은 사람이 보고 있으므로 토크를 빼지 않고(팔이 떨어지지 않게)
                                # 정지 조건이 남아 있는 동안 리더 팔로우를 끄고 지금 명령 위치에 멈춥니다
                                self.follow = False
                                self.target = dict(self.cmd)
                        for names, dst in ((("Torque_Enable",), "ten"),
                                           (("Present_Load", "Present_Current"), "load")):
                            for name in names:
                                try:
                                    setattr(self, dst, {k: int(v) for k, v in self.robot.bus.sync_read(
                                        name, normalize=False).items()})
                                    break
                                except Exception:
                                    continue
                self.err = ""
            except Exception as e:
                self.err = str(e)
            time.sleep(max(0.0, 1.0 / CONTROL_HZ - (time.monotonic() - t0)))

    # --- 입출력 (호출자가 lock 을 잡거나, 루프 스레드 안에서만) -------------------
    def read(self):
        obs = self.robot.get_observation()
        return {k[:-4]: float(v) for k, v in obs.items() if k.endswith(".pos")}

    def set_torque(self, on: bool):
        with self.lock:
            if on:
                # 서보의 Goal_Position 은 이전 세션 값이 RAM 에 그대로 남아 있습니다.
                # 그 상태로 토크만 켜면 서보가 그 목표로 전속 이동합니다.
                # 기계적 스톱에 부딪히면 과부하 보호가 걸려 토크가 빠지고,
                # 보호가 풀릴 때까지 명령을 따르지 않습니다 (위치 명령 재전송으로 해제).
                # 그래서 반드시 '현재 위치를 목표로 먼저 쓰고' 토크를 켭니다.
                self.actual = self.read()
                self.target = dict(self.actual)
                self.cmd = dict(self.actual)
                self.robot.send_action({f"{k}.pos": v for k, v in self.actual.items()})
                try:
                    self.robot.bus.enable_torque()
                except Exception as e:
                    # 한 모터(과부하 보호 등)에서 멈추면 앞쪽 모터만 켜진 채로 남습니다 — 전부 다시 끕니다
                    torque_off_all(self.robot.bus)
                    self.torque = False
                    raise RuntimeError(f"토크 켜기 실패 — 전부 다시 껐습니다: {e}") from e
                self._last_goal = {}
            else:
                failed = torque_off_all(self.robot.bus)
                self.torque = False
                if failed:
                    raise RuntimeError(f"토크 해제 실패: {', '.join(failed)}")
            self.torque = on

    def step(self):
        """cmd 를 target 으로 제한 속도 이동. 엔코더 값은 절대 안 섞음(떨림 방지).
        변화가 없으면 쓰지 않음 — 도달 후엔 서보가 자체 유지."""
        if not (self.robot and self.torque):
            return
        goal = {}
        now = time.monotonic()
        # 상한은 ° 기준입니다. OMX(-100~100)는 1 단위가 약 1.8° 라 그만큼 나눠서 같은 실제 속도로 맞춥니다
        dpu = kind()["deg_per_unit"]
        cap = (FOLLOW_STEP_DEG if self.follow else MAX_STEP_DEG) / dpu
        for n in CTL_JOINTS:
            cur = self.cmd.get(n, self.actual.get(n, 0.0))
            lo, hi = self.limits[n]
            tgt = max(lo, min(hi, self.target.get(n, cur)))
            diff = tgt - cur
            if abs(diff) < 0.2 / (dpu if n != "gripper" else 1.0):   # 데드밴드: 도달로 간주
                self.cmd[n] = tgt
                # 도달했다고 쓰기를 영영 멈추면, 서보가 그 명령을 놓쳤을 때
                # (막힘·통신 유실·보호 동작) 영원히 어긋난 채로 남습니다.
                # 주기적으로 같은 목표를 다시 써서 복구 기회를 줍니다.
                if now - self._last_goal.get(n, 0.0) > GOAL_REFRESH_S:
                    goal[n] = tgt
                continue
            nxt = cur + max(-cap, min(cap, diff))
            self.cmd[n] = nxt
            goal[n] = nxt
        if goal:
            self.robot.send_action({f"{k}.pos": v for k, v in goal.items()})
            for n in goal:
                self._last_goal[n] = now


class LeaderCtl:
    """lerobot SOLeader 래퍼. connect() 안의 configure() 가 토크를 꺼 줍니다."""

    def __init__(self, arm_cfg):
        self.cfg = arm_cfg
        self.side = arm_cfg["side"]
        self.tele = None
        self.power_warn = []
        self.lock = threading.Lock()

    @property
    def connected(self):
        return self.tele is not None

    def connect(self):
        tele = make_arm("leader", self.cfg)
        if not tele.calibration:
            raise RuntimeError(
                f"리더 캘리브레이션 파일이 없습니다: {tele.calibration_fpath} — 캘리브레이션 탭에서 만드세요")
        connect_leader(tele)
        try:
            self.power_warn = power_gate(tele.bus, "leader", self.cfg)
        except Exception:
            close_arm(tele, disable_torque=kind()["leader_torque"])
            raise
        self.tele = tele

    def read(self):
        with self.lock:
            if self.tele is None:
                return {}
            return {k[:-4]: float(v) for k, v in self.tele.get_action().items() if k.endswith(".pos")}

    def disconnect(self):
        with self.lock:
            if self.tele is not None:
                # SO 리더는 토크가 원래 꺼져 있어 해제 재시도(전원 없으면 수 초)를 건너뜁니다 — E-STOP 이 늦어지지 않게.
                # OMX 리더는 그리퍼에 토크가 걸려 있으므로 끕니다.
                close_arm(self.tele, disable_torque=kind()["leader_torque"])
            self.tele = None


class CamStreamer:
    """제어 탭 전용 MJPEG 소스. lerobot OpenCVCamera 를 쓰므로
    해상도/fps 가 record 설정과 동일하게 강제됩니다 (raw cv2 로는 기본값이 잡혔음)."""

    def __init__(self):
        self.on = False
        self.frames = {}     # name -> jpeg bytes
        self.cams = {}
        self.errors = {}     # name -> 열기 실패 사유 (UI 에 그대로 띄웁니다)
        self.thread = None

    def open(self):
        try:
            import cv2  # noqa: F401
            from lerobot.cameras.opencv import OpenCVCamera, OpenCVCameraConfig
        except ImportError:
            return False
        self.close()          # 이전 스트림 스레드가 카메라를 다 놓을 때까지 기다립니다
        self.cams = {}
        self.errors = {}
        for name, spec in CAM_SPECS.items():
            idx = spec["index_or_path"]
            idx = idx if isinstance(idx, int) else Path(str(idx))
            try:
                cam = OpenCVCamera(OpenCVCameraConfig(
                    index_or_path=idx, fps=int(spec["fps"]),
                    width=int(spec["width"]), height=int(spec["height"]),
                    color_mode="bgr",   # imencode 가 BGR 을 기대 — 변환 한 번 아낍니다
                    **cam_fourcc_kw(spec),
                ))
                cam.connect()
                self.cams[name] = cam
            except Exception as e:
                self.errors[name] = f"{type(e).__name__}: {e}"
                print(f"[openmimic] 카메라 '{name}' 열기 실패: {e}")
        if not self.cams:
            return False
        self.on = True
        self.thread = threading.Thread(target=self._loop, args=(self.cams,), daemon=True)
        self.thread.start()
        return True

    def _loop(self, cams):
        # cams 는 이 스레드 몫의 로컬 참조 — 새 open() 이 만든 카메라를 옛 스레드가 지우지 않게
        import cv2
        while self.on and self.cams is cams:
            t0 = time.monotonic()
            for name, cam in cams.items():
                try:
                    frame = cam.read_latest(max_age_ms=1000)
                except Exception:
                    continue
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                if ok:
                    self.frames[name] = buf.tobytes()
            time.sleep(max(0.0, 1.0 / CTL_STREAM_FPS - (time.monotonic() - t0)))
        for cam in cams.values():
            try:
                cam.disconnect()
            except Exception:
                pass
        if self.cams is cams:
            self.cams = {}
            self.frames = {}

    def close(self):
        self.on = False
        t, self.thread = self.thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)


CAMS = CamStreamer()
CTL_OWNER = None    # 현재 제어 탭을 점유한 WebSocket

_rebind(load_config())      # ArmCtl/LeaderCtl 정의 이후에 호출해야 합니다


def any_arm_connected():
    return any(a.connected for a in ARMS.values())


# ----------------------------- 포트 / 카메라 탐색 (셋업 탭) -------------------
def _read_sysfs(p):
    try:
        return p.read_text().strip()
    except Exception:
        return ""


def usb_info(dev, cls="tty"):
    """/dev/ttyACM0 (cls=tty) 또는 /dev/video0 (cls=video4linux) → 그 뒤의 USB 장치 정보.
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
    """/dev/serial/by-id 등 → {실제 장치경로: 별칭경로}"""
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


SIM_FILE = Path(__file__).resolve().parent / "openmimic_sim.json"     # tools_simarms.py 가 켜져 있을 때만 있음
SIM_DIR = Path(__file__).resolve().parent / "openmimic_sim"


def serial_path_ok(p):
    """포트 경로 검사 — /dev/… 또는 가상 팔(tools_simarms) 의 openmimic_sim/<이름> 링크."""
    return isinstance(p, str) and (p.startswith("/dev/") or p.startswith(str(SIM_DIR) + os.sep))


def _sim_ports():
    """가상 팔 도구가 켜져 있으면 그 보드들. 꺼져 있거나 죽었으면 빈 목록."""
    d = load_json(SIM_FILE, {})
    pid = d.get("pid") if isinstance(d, dict) else None
    if not pid:
        return []
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return []
    return [p for p in d.get("ports", []) if os.path.exists(p.get("path", ""))]


# ----------------------------- 가상 장치 (팔·카메라 없이 해 보기) -------------------
# 셋업 화면의 '가상 장치 켜기' 가 tools_simarms.py 를 작업으로 띄우고, 가상 팔·카메라만 쓰는 환경
# 'sim-<기종>-<모드>' 로 바꿉니다. 캘리브 id 는 sim_* 라서 실제 팔의 캘리브레이션 파일과 섞이지 않고,
# 끄면 켜기 전 환경으로 돌아갑니다. 화면 아래 '가상 장치' 막대가 손 대신 관절을 움직입니다 (openmimic_sim/cmd).
SIM_PY = Path(__file__).resolve().parent / "tools_simarms.py"
SIM_ENV_PREFIX = "sim-"
SIM_CAM = {"width": 320, "height": 240, "fps": 30}


def sim_state():
    d = load_json(SIM_FILE, {})
    pid = d.get("pid") if isinstance(d, dict) else None
    try:
        if not pid:
            raise OSError
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return {"on": False}
    d["on"] = True
    d["cams"] = [c for c in d.get("cams", []) if isinstance(c, dict) and c.get("path")]
    return d


def sim_env_name(robot, mode):
    return f"{SIM_ENV_PREFIX}{robot}-{mode}"


def sim_config(st, prev_env):
    """가상 장치 상태(st)로 환경 설정을 만듭니다. 전에 쓰던 같은 이름의 가상 환경이 있으면 fps·과제 등은 이어받습니다."""
    name = sim_env_name(st["robot"], st["mode"])
    old = load_json(_env_file(name), None)
    cfg = old if isinstance(old, dict) else json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update({"robot": st["robot"], "mode": st["mode"], "env_name": name, "sim_prev_env": prev_env})
    ports = {p.get("name"): p["path"] for p in st.get("ports", [])}
    cams = {c["name"]: c["path"] for c in st.get("cams", [])}
    sides = ["main"] if st["mode"] == "single" else ["left", "right"]
    arms = []
    for side in sides:
        sfx = "" if side == "main" else f"_{side}"
        wrist = cams.get("wrist" if side == "main" else f"{side}_wrist")
        arms.append({"side": side, "follower_port": ports.get(f"{side}_follower", ""),
                     "leader_port": ports.get(f"{side}_leader", ""),
                     "follower_id": f"sim_follower{sfx}", "leader_id": f"sim_leader{sfx}",
                     "cameras": {"wrist": dict(SIM_CAM, index_or_path=wrist)} if wrist else {}})
    cfg["arms"] = arms
    cfg["cameras"] = {"top": dict(SIM_CAM, index_or_path=cams["top"])} if cams.get("top") else {}
    return cfg


def sim_start(robot, mode):
    if robot not in ROBOT_KINDS or mode not in ("single", "bimanual"):
        raise ValueError("기종·모드 값이 잘못됐습니다")
    if sim_state().get("on"):
        raise ValueError("가상 장치가 이미 켜져 있습니다 — 끄고 다시 켜세요")
    busy = robot_busy()
    if busy:
        raise ValueError(f"팔을 쓰는 중입니다 ({busy['id']}) — 끝낸 뒤 켜세요")
    jid = start_job("sim", [sys.executable, str(SIM_PY), "--robot", robot, "--mode", mode],
                    cwd=str(SIM_PY.parent), spec={"robot": robot, "mode": mode})
    pid = load_json(JOB_DIR / f"{jid}.json", {}).get("pid")
    st = {}
    for _ in range(50):                    # 가상 보드(PTY)와 상태 파일이 생길 때까지 (보통 0.5초 안)
        st = sim_state()
        if st.get("on") and st.get("pid") == pid and st.get("ports"):
            break
        time.sleep(0.1)
    else:
        kill_job(jid, force=True)
        raise RuntimeError(f"가상 장치를 켜지 못했습니다 — 작업 로그 확인: {jid}")
    prev = env_name()
    if not prev.startswith(SIM_ENV_PREFIX):
        sync_active_env()                  # 실제 팔 설정을 그 환경 파일에 보관
    else:
        prev = (CFG or {}).get("sim_prev_env") or DEFAULT_ENV
    cfg = sim_config(st, prev)
    err = validate_config(cfg)
    if err:
        kill_job(jid, force=True)
        raise RuntimeError(f"가상 환경 설정 오류: {err}")
    ENV_DIR.mkdir(parents=True, exist_ok=True)
    save_json(_env_file(cfg["env_name"]), cfg)
    _write_active_config(cfg)
    return {"ok": True, "job": jid, "env": cfg["env_name"], "prev": prev}


def sim_stop():
    st = sim_state()
    busy = busy_with(("record", "rollout", "calib"))
    if busy:
        raise ValueError(f"{busy['id']} 가 가상 팔을 쓰는 중입니다 — 끝낸 뒤 끄세요")
    for a in list(ARMS.values()) + list(LEADERS.values()):
        try:
            if a.connected:
                a.disconnect()
        except Exception:      # noqa: BLE001 — 가상 보드는 곧 사라지므로 닫기 실패는 무시
            pass
    j = next((j for j in jobs_index() if j["kind"] == "sim" and j["alive"]), None)
    if j:
        kill_job(j["id"])
    elif st.get("pid"):
        try:
            os.kill(int(st["pid"]), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    back = ""
    if env_name().startswith(SIM_ENV_PREFIX):
        sync_active_env()
        back = (CFG or {}).get("sim_prev_env") or DEFAULT_ENV
        if not _env_file(back).exists():
            back = DEFAULT_ENV
        try:
            activate_env(back)
        except ValueError:
            back = ""
    return {"ok": True, "env": back}


def sim_card_html():
    """셋업·셋업 마법사 위쪽 카드. 실제 보드가 없으면 크게, 있으면 접어서."""
    st = sim_state()
    if st.get("on"):
        return (f'<div class="card needcard"><div class=needtxt><b>가상 장치 사용 중 — 환경 {esc(env_name())}</b>'
                '<span class=muted>가상 팔·카메라로 셋업부터 실행까지 해 볼 수 있습니다. 손으로 움직여야 하는 단계는 화면 아래 '
                '<b>팔로워 움직이기</b>·<b>리더 움직이기</b> 버튼을 쓰세요. 실제 팔을 쓰려면 아래 막대의 <b>가상 장치 끄기</b> — '
                '전에 쓰던 환경으로 돌아갑니다.</span></div></div>')
    if env_name().startswith(SIM_ENV_PREFIX):
        # 재부팅 등으로 가상 장치가 꺼졌는데 환경은 가상 그대로인 경우
        return (f'<div class="card needcard"><div class=needtxt><b>가상 장치가 꺼져 있습니다 — 환경 {esc(env_name())} 은 가상 팔용입니다</b>'
                '<span class=muted>다시 켜면 가상 팔·카메라가 같은 경로로 생겨 캘리브레이션을 그대로 씁니다.</span></div>'
                '<div class=toolbar><button class=primary onclick="simAgain(this)">다시 켜기</button>'
                '<button onclick="simBack()">전에 쓰던 환경으로</button></div></div>'
                '<script>async function simAgain(btn){btn.disabled=true;const r=await fetch(\'/api/sim/start\',{method:\'POST\','
                'headers:{\'Content-Type\':\'application/json\'},body:\'{}\'});const j=await r.json();'
                'if(j.error){alert(j.error);btn.disabled=false;return;}location.reload();}'
                'async function simBack(){const r=await fetch(\'/api/sim/stop\',{method:\'POST\'});const j=await r.json();'
                'if(j.error)alert(j.error);else location.reload();}</script>')
    real = [p for p in list_serial_ports() if not p.get("sim")]
    cur_r, cur_m = (CFG or {}).get("robot", "so101"), (CFG or {}).get("mode", "single")
    opts_r = "".join(f'<option value="{k}"{" selected" if k == cur_r else ""}>{esc(v["label"])}</option>'
                     for k, v in ROBOT_KINDS.items())
    opts_m = "".join(f'<option value="{k}"{" selected" if k == cur_m else ""}>{lb}</option>'
                     for k, lb in (("single", "한팔"), ("bimanual", "양팔")))
    form = (f'<div class=toolbar style="margin-top:8px"><select id=simrobot>{opts_r}</select>'
            f'<select id=simmode>{opts_m}</select>'
            '<button class=primary onclick="simStart(this)">가상 장치 켜기</button></div>'
            '<script>async function simStart(btn){btn.disabled=true;btn.textContent=\'켜는 중…\';'
            'const r=await fetch(\'/api/sim/start\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},'
            'body:JSON.stringify({robot:document.getElementById(\'simrobot\').value,mode:document.getElementById(\'simmode\').value})});'
            'const j=await r.json();if(j.error){alert(j.error);btn.disabled=false;btn.textContent=\'가상 장치 켜기\';return;}'
            'location.href=\'/setup/wizard\';}</script>')
    desc = ('가상 팔(리더·팔로워)과 가상 카메라를 만들어 셋업 → 캘리브레이션 → 수집 → 학습 → 실행을 끝까지 해 봅니다. '
            '실제 팔 설정은 그대로 보관되고, 끄면 돌아갑니다.')
    if not real:
        return (f'<div class="card needcard"><div class=needtxt><b>연결된 팔이 없습니다 — 가상 장치로 해 보기</b>'
                f'<span class=muted>{desc}</span>{form}</div></div>')
    return (f'<details class=card><summary><b>팔 없이 해 보기 — 가상 장치</b></summary>'
            f'<p class=muted>{desc}</p>{form}</details>')


def sim_send(line):
    SIM_DIR.mkdir(exist_ok=True)
    with open(SIM_DIR / "cmd", "a") as f:
        f.write(line + "\n")


def sim_panel_html():
    """가상 장치가 켜져 있으면 모든 화면 아래쪽에 뜨는 조작 막대 — 손 대신 관절을 움직입니다."""
    st = sim_state()
    if not st.get("on"):
        return ""
    side_lb = {"left": "왼쪽 ", "right": "오른쪽 "}
    btns = fault = ""
    for i, p in enumerate(st.get("ports", []), 1):
        sd = side_lb.get(p.get("side"), "")
        role = "팔로워" if p.get("role") == "follower" else "리더"
        btns += (f'<button class=simw onclick="simCmd({{cmd:\'w\',board:{i}}})" title="4초 동안 관절을 양 끝까지 움직입니다 '
                 f'(포트 찾기·캘리브레이션·확인 단계에서 손 대신)">{sd}{role} 움직이기</button>')
        if p.get("role") == "follower":
            fault += (f'<span class=simf><button onclick="simCmd({{cmd:\'t\',board:{i},value:68}})">{sd}팔로워 과열 흉내</button>'
                      f'<button onclick="simCmd({{cmd:\'v\',board:{i},value:9}})">{sd}팔로워 전압 강하 흉내</button>'
                      f'<button onclick="simCmd({{cmd:\'t\',board:{i},value:32}});simCmd({{cmd:\'v\',board:{i},value:12}})">'
                      f'{sd}팔로워 정상 값으로</button></span>')
    auto = "리더 자동 움직임: 켜짐" if st.get("auto") else "리더 자동 움직임: 꺼짐"
    return (f'<div class=simbar id=simbar><b>가상 장치 켜짐</b><span class=muted>{esc(st.get("robot", ""))} · '
            f'{"양팔" if st.get("mode") == "bimanual" else "한팔"}</span>{btns}'
            f'<button class=simw onclick="simCmd({{cmd:\'a\',on:{"false" if st.get("auto") else "true"}}})" '
            f'title="리더가 계속 천천히 움직입니다 (제어 리더 팔로우·수집 시험용)">{auto}</button>'
            f'<details class=simfault><summary>이상 상황 흉내</summary>{fault}'
            f'<p class=muted>서보 상태 감시(실행·수집 중 자동 정지)를 시험합니다</p></details>'
            f'<button class=simoff onclick="simStop()">가상 장치 끄기</button></div>'
            '<script>async function simCmd(b){const r=await fetch(\'/api/sim/cmd\',{method:\'POST\','
            'headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify(b)});const j=await r.json();'
            'if(j.error)alert(j.error);else if(b.cmd===\'a\')location.reload();}'
            'async function simStop(){if(!confirm(\'가상 장치를 끄고 전에 쓰던 환경으로 돌아갈까요?\'))return;'
            'const r=await fetch(\'/api/sim/stop\',{method:\'POST\'});const j=await r.json();'
            'if(j.error)alert(j.error);else location.reload();}</script>')


def list_serial_ports():
    """시리얼 후보 나열. udev 심볼릭 링크가 전혀 없어도 동작합니다."""
    by_id = _alias_map("/dev/serial/by-id")
    by_path = _alias_map("/dev/serial/by-path")
    devs = set()
    for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
        devs.update(glob.glob(pat))
    devs.update(by_id)          # 위 패턴에 안 걸리는 이름까지 포함
    devs.update(by_path)
    used = {}
    for side, arm in ARM_CFGS.items():
        for role in ("follower", "leader"):
            p = arm.get(f"{role}_port")
            if p:
                used.setdefault(os.path.realpath(p) if os.path.exists(p) else p, []).append(
                    f"{side}/{role}" if BIMANUAL else role)
    out = []
    for dev in sorted(devs):
        info = usb_info(dev)
        out.append({
            "dev": dev,
            "by_id": by_id.get(dev, ""),
            "by_path": by_path.get(dev, ""),
            "usb": info,
            "label": (f"{info.get('manufacturer', '')} {info.get('product', '')}".strip()
                      or os.path.basename(dev)),
            "used_by": used.get(dev, []),
        })
    # 가상 팔 — 경로가 고정된 openmimic_sim/<이름> 링크를 by_path 로 둬서 지정한 포트가 재시작해도 유지됩니다
    for sp in _sim_ports():
        dev = os.path.realpath(sp["path"])
        out.append({"dev": dev, "by_id": "", "by_path": sp["path"],
                    "usb": {"product": sp.get("label", "가상 팔"), "serial": ""},
                    "label": sp.get("label", "가상 팔"), "sim": True,
                    "used_by": used.get(dev, [])})
    return out


CAM_SNAPS = {}      # realpath(dev) -> jpeg bytes (셋업 탭 카메라 식별용 썸네일)


def _snap_key(dev):
    dev = str(dev)
    return os.path.realpath(dev) if dev.startswith("/dev/") else dev


def camera_snapshot(dev, warm=6, width=320):
    """카메라를 잠깐 열어 한 장 찍습니다. 어느 장치가 어느 카메라인지 눈으로 확인하는 용도.
    자동 노출이 안정될 때까지 몇 장 버립니다."""
    try:
        import cv2
    except ImportError:
        return None
    d = str(dev)
    target = int(d) if d.isdigit() else d
    cap = cv2.VideoCapture(target)
    try:
        if not cap.isOpened():
            return None
        frame = None
        for _ in range(warm):
            ok, f = cap.read()
            if ok:
                frame = f
        if frame is None:
            return None
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, max(1, int(h * width / w))))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        return buf.tobytes() if ok else None
    except Exception:
        return None
    finally:
        cap.release()


def list_video_devices():
    """카메라 후보. lerobot OpenCVCamera.find_cameras() 를 쓰되,
    실패하면 /dev/video* 나열로 폴백합니다.

    같은 모델 카메라 2개(wrist ×2)는 USB 시리얼 번호가 없으면 /dev/v4l/by-id 이름이 겹쳐
    어느 링크가 어느 카메라인지 부팅마다 바뀔 수 있습니다. 시리얼 포트와 같은 규칙으로
    시리얼 번호가 있을 때만 by-id, 없으면 by-path(물리 USB 포트 고정) 를 씁니다."""
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
    for c in sim_state().get("cams", []):           # 가상 장치가 켜져 있으면 가상 카메라
        found.append(entry(c["path"], {"width": 320, "height": 240, "fps": 30}, f"가상 카메라 {c['name']}"))
    CAM_SNAPS.clear()
    for f in found:
        jpg = camera_snapshot(f["dev"])
        if jpg:
            CAM_SNAPS[_snap_key(f["dev"])] = jpg
            for alias in (f["by_id"], f["by_path"]):
                if alias:
                    CAM_SNAPS[_snap_key(alias)] = jpg
        f["snap"] = bool(jpg)
    return found


def bus_class(k=None):
    return _load(*(k or kind())["bus"])        # FeetechMotorsBus (SO-ARM101) / DynamixelMotorsBus (OMX)


def probe_port(port, full=False):
    """포트에 붙은 모터 ID 를 나열합니다 (기종에 맞는 버스: Feetech / Dynamixel).
    full=False 면 기본 보드레이트(1 Mbps)만 — 웹에서 쓰기에 scan_port 는 너무 느립니다."""
    Bus = bus_class()
    if full:
        found = Bus.scan_port(port)
        return {"baudrates": {str(b): sorted(ids) for b, ids in found.items()}}
    bus = Bus(port, {})
    bus.connect(handshake=False)
    try:
        bus.set_baudrate(Bus.default_baudrate)
        ids_models = bus.broadcast_ping() or {}
    finally:
        try:
            bus.disconnect(disable_torque=False)
        except Exception:
            pass
    return {"baudrate": Bus.default_baudrate,
            "ids": sorted(ids_models),
            "models": {str(i): m for i, m in ids_models.items()}}


class PortWatcher:
    """여러 포트를 동시에 토크 OFF 로 열어 두고 Present_Position 을 읽습니다.
    사용자가 팔 하나를 손으로 움직이면 값이 변하는 포트가 그 팔입니다 —
    leader/follower 판별의 유일하게 확실한 방법(전기적으로는 구분 불가)."""

    def __init__(self):
        self.on = False
        self.state = {}          # port -> {"pos":{}, "span":{}, "err":str}
        self.threads = []

    def start(self, ports):
        self.stop()
        self.state = {p: {"pos": {}, "span": {}, "err": ""} for p in ports}
        self.on = True
        for p in ports:
            t = threading.Thread(target=self._loop, args=(p,), daemon=True)
            t.start()
            self.threads.append(t)

    def _loop(self, port):
        st = self.state[port]
        bus = None
        try:
            # 리더/팔로워 중 어느 쪽인지 모르니 두 모터 구성을 차례로 시험합니다.
            # SO-ARM101 은 둘 다 ID 1~6 이라 첫 번째에서 바로 맞고, OMX 는 리더 1~6 / 팔로워 11~16 입니다.
            last = None
            for role in ("leader", "follower"):
                bus = make_bus(role, port)
                bus.connect(handshake=False)
                try:
                    bus.sync_read("Present_Position", normalize=False)
                    break
                except Exception as e:
                    last = e
                    bus.disconnect(disable_torque=False)
                    bus = None
            if bus is None:
                raise last or RuntimeError("모터 응답 없음")
            torque_off_all(bus)           # 손으로 움직일 수 있게 (OMX 리더 그리퍼 토크 포함)
            lo, hi = {}, {}
            while self.on:
                pos = bus.sync_read("Present_Position", normalize=False)
                for k, v in pos.items():
                    lo[k] = min(lo.get(k, v), v)
                    hi[k] = max(hi.get(k, v), v)
                st["pos"] = {k: int(v) for k, v in pos.items()}
                st["span"] = {k: int(hi[k] - lo[k]) for k in pos}
                st["err"] = ""
                time.sleep(0.1)
        except Exception as e:
            st["err"] = str(e)
        finally:
            if bus is not None:
                try:
                    bus.disconnect(disable_torque=False)
                except Exception:
                    pass

    def stop(self):
        self.on = False
        for t in self.threads:
            t.join(timeout=1.5)
        self.threads = []


WATCH = PortWatcher()


class MotorSetupSession:
    """새 팔 모터 ID 세팅 — lerobot-setup-motors 의 웹 버전.

    새 STS3215 는 전부 ID 1 이라, 모터를 **한 개씩만** 보드에 연결하고 순서대로 ID 를 씁니다.
    lerobot 과 같은 순서(gripper=6 → … → shoulder_pan=1)로 bus.setup_motor(name) 을 부릅니다.
    setup_motor 는 응답한 첫 모터를 그냥 골라 쓰므로, 쓰기 전에 응답 ID 가 정확히 1개인지 따로 확인합니다."""

    ORDER = list(reversed(CTL_JOINTS))

    def __init__(self):
        self.lock = threading.Lock()
        self.role = "follower"
        self._reset()

    def _reset(self):
        self.bus = None
        self.port = ""
        self.stage = "idle"      # idle | running | done | error
        self.idx = 0
        self.done = []           # [{"name", "id", "from_id", "baud"}]
        self.err = ""
        self.last = ""

    @property
    def active(self):
        return self.stage == "running"

    @property
    def current(self):
        return self.ORDER[self.idx] if self.idx < len(self.ORDER) else None

    def start(self, port, role="follower"):
        if self.active:
            raise RuntimeError("이미 모터 ID 세팅 진행 중")
        self._reset()
        # 기종·역할별 모터 구성 (SO-ARM101: ID 1~6 sts3215 / OMX 팔로워: ID 11~16, 리더: ID 1~6 — lerobot 정의 그대로)
        bus = make_bus(role, port)
        bus.connect(handshake=False)     # 아직 ID 가 안 맞으니 handshake 는 하면 안 됨
        self.bus = bus
        self.role = role
        self.port = port
        self.stage = "running"

    def write_current(self):
        if not self.active:
            raise RuntimeError("진행 중이 아닙니다")
        name = self.current
        bus = self.bus
        target = bus.motors[name].id
        with self.lock:
            baud, cur_id = bus._find_single_motor(name)     # 보드레이트 전부 훑어 모터 1개 탐색
            bus.set_baudrate(baud)
            ids = bus.broadcast_ping() or {}
            if len(ids) > 1:
                raise RuntimeError(f"모터가 {len(ids)}개 응답합니다 (ID {sorted(ids)}) — "
                                   f"'{name}' 모터 하나만 보드에 연결하세요")
            bus.setup_motor(name, initial_baudrate=baud, initial_id=cur_id)
            bus.set_baudrate(bus.default_baudrate)
            after = bus.broadcast_ping() or {}
        if target not in after:
            raise RuntimeError(f"ID {target} 기록 후 응답이 없습니다 — 전원/배선 확인 후 다시 시도")
        self.done.append({"name": name, "id": target, "from_id": int(cur_id), "baud": int(baud)})
        self.last = f"{name}: ID {cur_id} → {target} (baud {baud} → {bus.default_baudrate})"
        self.idx += 1
        if self.idx >= len(self.ORDER):
            self.stage = "done"
            self._close()

    def _close(self):
        bus, self.bus = self.bus, None
        if bus is not None:
            with self.lock:
                try:
                    bus.disconnect(disable_torque=False)
                except Exception:
                    pass

    def cancel(self):
        self._close()
        self.stage = "idle"

    def state(self):
        cur = self.current
        cid = None
        if cur is not None:
            cid = self.bus.motors[cur].id if self.bus is not None else len(self.ORDER) - self.idx
        return {"stage": self.stage, "port": self.port, "role": self.role, "order": self.ORDER,
                "idx": self.idx, "current": cur, "current_id": cid,
                "done": self.done, "err": self.err, "last": self.last}


MOTORSETUP = MotorSetupSession()


class ArmCheckSession:
    """셋업 탭 '팔 불량 점검'. 판정 로직은 tools_armcheck 를 그대로 씁니다.

    기본 점검은 서보에 아무것도 쓰지 않습니다. 쓸기를 고르면 Torque_Enable=0 만 씁니다.
    모터를 구동하는 시험은 없습니다."""

    def __init__(self):
        self.lock = threading.Lock()
        self.history = {}        # port -> 마지막 판정 요약 (셋업 마법사가 봅니다)
        self._reset()

    def _reset(self):
        self.io = None
        self.port = ""
        self.role = ""
        self.stage = "idle"      # idle | checking | sweeping | done | error
        self.rep = None
        self.tracker = None
        self.present = []
        self.err = ""
        self._run = False
        self._th = None

    @property
    def active(self):
        return self.stage in ("checking", "sweeping")

    @staticmethod
    def module():
        """기종별 점검 모듈 — 같은 함수 이름·결과 형식 (SO-ARM101: tools_armcheck, OMX: tools_dxlcheck)."""
        if kind()["bus"][1] == "DynamixelMotorsBus":
            import tools_dxlcheck as AC
        else:
            import tools_armcheck as AC
        return AC

    def start(self, port, role, sweep):
        AC = self.module()
        if self.active:
            raise RuntimeError("이미 점검 중")
        self._close()
        self._reset()
        self.AC = AC
        self.port, self.role, self.stage = port, role, "checking"
        try:
            # OMX 는 리더/팔로워 모터 ID 가 달라 역할이 필요합니다 (모르면 팔로워)
            self.io = AC.BusIO(port, role or "follower") if hasattr(AC, "MOTORS") else AC.BusIO(port)
        except Exception as e:
            raise RuntimeError(f"포트를 열 수 없습니다 ({type(e).__name__}: {e})")
        rep = AC.new_report(self.io) if hasattr(AC, "new_report") else AC.Report()
        present = AC.check_presence(self.io, rep)
        AC.check_static(self.io, rep, present, role=role or None)
        self.rep, self.present = rep, present
        if sweep and present:
            AC.torque_off(self.io, present)
            self.tracker = AC.SweepTracker(present)
            self.stage = "sweeping"
            self._run = True
            self._th = threading.Thread(target=self._sample, daemon=True)
            self._th.start()
        else:
            AC.finalize(rep)
            self._close()
            self.stage = "done"
            self._remember()

    def _remember(self):
        d = self.rep.as_dict()
        self.history[self.port] = {
            "verdict": d["verdict"], "code": d["code"], "power": d["power"],
            "role": self.role, "when": time.strftime("%H:%M"),
            "missing": [j for j, m in d["motors"].items() if m.get("model") is None],
            # 3D 에서 모터 색으로 보여 줄 관절별 판정
            "levels": {j: ("MISSING" if m.get("model") is None else m.get("level"))
                       for j, m in d["motors"].items()},
            # 모터 EEPROM 에 남아 있는 캘리브레이션 (lerobot write_calibration 이 쓰는 3개 레지스터)
            "eeprom": {j: {"homing_offset": m.get("homing"), "range_min": m.get("min_lim"),
                           "range_max": m.get("max_lim")}
                       for j, m in d["motors"].items() if m.get("model") is not None}}

    def _sample(self):
        AC = self.AC
        ids = getattr(self.io, "ids", None) or AC.IDS
        while self._run:
            for j in self.present:
                if not self._run:
                    break
                with self.lock:
                    v, err = self.io.read(ids[j], "Present_Position")
                if hasattr(AC, "MOTORS"):
                    if err & 0x80:            # Dynamixel: 응답 Error 는 Alert 비트만 의미가 같습니다
                        self.rep.alert[j] = True
                else:
                    self.rep.note_err(j, err)
                self.tracker.feed(j, v)
            time.sleep(0.01)

    def _stop_sampler(self):
        self._run = False
        th, self._th = self._th, None
        if th is not None:
            th.join(timeout=2)

    def finish(self):
        AC = self.AC
        if self.stage != "sweeping":
            raise RuntimeError("쓸기 중이 아닙니다")
        self._stop_sampler()
        self.tracker.judge(self.rep)
        AC.finalize(self.rep)
        self._close()
        self.stage = "done"
        self._remember()

    def fail(self, e):
        self._stop_sampler()
        self._close()
        self.stage = "error"
        self.err = str(e)

    def _close(self):
        io, self.io = self.io, None
        if io is not None:
            with self.lock:
                io.close()

    def cancel(self):
        self._stop_sampler()
        self._close()
        self._reset()

    def state(self):
        d = {"stage": self.stage, "port": self.port, "role": self.role, "err": self.err}
        if self.rep is not None and self.stage in ("sweeping", "done"):
            d["report"] = self.rep.as_dict()
            d["partial"] = self.stage != "done"
        if self.stage == "sweeping" and self.tracker is not None:
            d["live"] = self.tracker.live()
        return d


ARMCHECK = ArmCheckSession()


def calib_file(role, calib_id, k=None):
    """lerobot 이 쓰는 캘리브레이션 파일 경로. 기종마다 폴더가 다릅니다
    (robots/so_follower, robots/omx_follower ...) — 같은 id 라도 서로 섞이지 않습니다."""
    return CALIB_ROOT / (k or kind())["calib_dir"][role] / f"{calib_id}.json"


class VerifySession:
    """셋업 마법사 '확인' 단계. 캘리브레이션 파일로 정규화한 현재 관절값을 읽기만 합니다.
    서보에는 쓰지 않습니다 — '토크 끄기' 를 누를 때만 Torque_Enable=0.

    서보 EEPROM 의 Homing_Offset 이 파일과 다르면, 연결할 때 lerobot 이 파일 값을 써 넣습니다
    (write_calibration). 그때 보일 값을 미리 보여 주려고 그 차이만큼 보정해서 정규화합니다."""

    def __init__(self):
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.bus = None
        self.side = self.role = ""
        self.on = False
        self.joints = {}
        self.torque = {}
        self.adj = {}
        self.warn = []
        self.err = ""
        self._th = None

    @property
    def active(self):
        return self.on

    def start(self, side, role):
        self.stop()
        arm = ARM_CFGS[side]
        port = arm.get(f"{role}_port") or ""
        f = calib_file(role, arm[f"{role}_id"])
        if not port:
            raise RuntimeError("포트가 지정되지 않았습니다")
        if not f.is_file():
            raise RuntimeError(f"캘리브레이션 파일이 없습니다: {f}")
        # lerobot 장치가 만드는 버스 — 모터 구성·정규화 방식·캘리브레이션 파일이 연결 때와 똑같습니다
        bus = make_bus(role, port, calib_id=arm[f"{role}_id"])
        cal = bus.calibration
        if set(cal) != set(CTL_JOINTS):
            raise RuntimeError(f"캘리브레이션 파일이 이 기종 형식이 아닙니다: {f}")
        bus.connect(handshake=False)
        try:
            eeprom = bus.sync_read("Homing_Offset", normalize=False)
            self.torque = {k: int(v) for k, v in bus.sync_read("Torque_Enable", normalize=False).items()}
        except Exception:
            bus.disconnect(disable_torque=False)
            raise
        # 연결 때 파일 값이 써지면 Present 가 얼마나 바뀌는지.
        # Feetech: Present = Actual - Homing → (EEPROM - 파일),  Dynamixel: Present = Actual + Homing → (파일 - EEPROM)
        sign = kind()["homing_sign"]
        self.adj = {n: sign * (int(eeprom[n]) - int(cal[n].homing_offset)) for n in CTL_JOINTS}
        diff = [n for n, v in self.adj.items() if v]
        self.warn = ([f"서보에 저장된 homing offset 이 파일과 다릅니다 ({', '.join(diff)}) — "
                      "Control·수집에서 연결할 때 파일 값이 써집니다. 아래 값은 그 기준입니다."] if diff else [])
        self.bus, self.side, self.role, self.err = bus, side, role, ""
        self.on = True
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self):
        last_t = 0.0
        while self.on:
            try:
                with self.lock:
                    raw = self.bus.sync_read("Present_Position", normalize=False)
                    if time.monotonic() - last_t > 1.0:
                        self.torque = {k: int(v) for k, v in
                                       self.bus.sync_read("Torque_Enable", normalize=False).items()}
                        last_t = time.monotonic()
                ids = {self.bus.motors[n].id: int(v) + self.adj.get(n, 0) for n, v in raw.items()}
                norm = self.bus._normalize(ids)
                self.joints = {n: round(float(norm[self.bus.motors[n].id]), 1) for n in raw}
                self.err = ""
            except Exception as e:
                self.err = f"{type(e).__name__}: {e}"
            time.sleep(0.05)

    def torque_off(self):
        if not self.on:
            raise RuntimeError("확인 중이 아닙니다")
        with self.lock:
            failed = torque_off_all(self.bus)
            self.torque = {k: int(v) for k, v in self.bus.sync_read("Torque_Enable", normalize=False).items()}
        if failed:
            raise RuntimeError(f"토크 해제 실패: {', '.join(failed)}")

    def stop(self):
        self.on = False
        th, self._th = self._th, None
        if th is not None:
            th.join(timeout=2)
        bus, self.bus = self.bus, None
        if bus is not None:
            try:
                bus.disconnect(disable_torque=False)     # 토크 상태는 건드리지 않습니다
            except Exception:
                pass
        self._reset()

    def state(self):
        return {"on": self.on, "side": self.side, "role": self.role, "joints": self.joints,
                "torque_on": [k for k, v in self.torque.items() if v], "warn": self.warn, "err": self.err}


VERIFY = VerifySession()


def busy_with(kinds):
    for j in jobs_index():
        if j["alive"] and j["kind"] in kinds:
            return j
    return None


def robot_busy():
    """팔(시리얼)을 쓰는 작업: record/rollout + 제어 탭 수동 제어 + Setup 포트 감시"""
    if any_arm_connected() or CTL_OWNER is not None:
        return {"id": "manual-control", "kind": "control", "alive": True}
    if WATCH.on:
        return {"id": "port-watch (셋업 탭)", "kind": "setup", "alive": True}
    if CALIB.active:
        return {"id": "calibration (캘리브레이션 탭)", "kind": "calib", "alive": True}
    if MOTORSETUP.active:
        return {"id": "motor-id-setup (셋업 탭)", "kind": "setup", "alive": True}
    if ARMCHECK.active:
        return {"id": "arm-check (셋업 탭)", "kind": "setup", "alive": True}
    if VERIFY.active:
        return {"id": "verify (셋업 마법사)", "kind": "setup", "alive": True}
    return busy_with(("record", "rollout"))


def gpu_or_loop_busy():
    """학습 시작을 막아야 하는 작업 (Control 수동 제어는 학습과 동시 가능)"""
    return busy_with(("record", "rollout", "train"))


def exclusive_busy():
    if any_arm_connected() or CTL_OWNER is not None:     # 연결 중(약 1초)도 점유로 봅니다
        return {"id": "manual-control (제어 탭)", "kind": "control", "alive": True}
    if WATCH.on:
        return {"id": "port-watch (셋업 탭)", "kind": "setup", "alive": True}
    if CALIB.active:
        return {"id": "calibration (캘리브레이션 탭)", "kind": "calib", "alive": True}
    if MOTORSETUP.active:
        return {"id": "motor-id-setup (셋업 탭)", "kind": "setup", "alive": True}
    if ARMCHECK.active:
        return {"id": "arm-check (셋업 탭)", "kind": "setup", "alive": True}
    if VERIFY.active:
        return {"id": "verify (셋업 마법사)", "kind": "setup", "alive": True}
    return busy_with(("record", "rollout", "train"))


# ----------------------------- 화면 공통 ------------------------------------
CSS = """
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#161b21">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="OpenMimic">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="apple-touch-icon" href="/icon-180.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans+KR:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#0f1216; --surface:#161b21; --surface2:#1b222a; --line:#28303a;
  --text:#e6ebf1; --muted:#8b98a7; --dim:#5d6a79;
  --accent:#5d9dd6; --accent-dim:#2c4257;
  --ok:#57b98a; --warn:#d9a13b; --bad:#c96060;
  --mono:'IBM Plex Mono',ui-monospace,monospace;
  --sans:'IBM Plex Sans','IBM Plex Sans KR',system-ui,sans-serif;
}
*{box-sizing:border-box}
body{font-family:var(--sans);margin:0;background:var(--bg);color:var(--text);font-size:14.5px;line-height:1.55}
a{color:var(--accent);text-decoration:none} a:hover{text-decoration:underline}
.appbar{display:flex;align-items:center;gap:26px;background:var(--surface);
  border-bottom:1px solid var(--line);padding:0 22px;height:52px;
  position:sticky;top:0;z-index:10}
.brand{font-family:var(--mono);font-weight:600;font-size:13px;letter-spacing:.14em;color:var(--text);white-space:nowrap}
.brand a{color:inherit;text-decoration:none}
.brand small{color:var(--dim);font-weight:400;letter-spacing:.14em}
.nav{display:flex;gap:2px;height:100%;min-width:0;overflow-x:auto;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav a{flex:0 0 auto}
.navmore{position:relative;flex:0 0 auto;align-self:stretch;display:flex;align-items:center}
.navmore>summary{list-style:none;cursor:pointer;color:var(--muted);font-weight:500;font-size:13.5px;padding:0 10px;
  height:100%;display:flex;align-items:center;gap:4px;border-bottom:2px solid transparent;white-space:nowrap}
.navmore>summary::-webkit-details-marker{display:none}
.navmore[data-on]>summary{color:var(--text);border-bottom-color:var(--accent)}
.navmore>summary:hover{color:var(--text)}
.navmenu{position:absolute;top:100%;left:0;min-width:140px;background:var(--surface);border:1px solid var(--line);
  border-radius:8px;padding:6px;z-index:20;box-shadow:0 8px 24px rgba(0,0,0,.35)}
.navmenu a{display:block;padding:8px 12px;border-radius:6px;color:var(--muted);font-size:13.5px}
.navmenu a:hover{background:var(--surface2);color:var(--text);text-decoration:none}
.navmenu a.on{color:var(--text)}
.nav a{display:flex;align-items:center;padding:0 14px;color:var(--muted);
  border-bottom:2px solid transparent;font-weight:500;font-size:13.5px}
.nav a:hover{color:var(--text);text-decoration:none}
.nav a.on{color:var(--text);border-bottom-color:var(--accent)}
.statuscluster{margin-left:auto;display:flex;align-items:center;gap:10px;flex:0 0 auto;white-space:nowrap;
  font-family:var(--mono);font-size:12px;color:var(--muted)}
.statuscluster a.chip{display:inline-flex;align-items:center;gap:5px;color:inherit;text-decoration:none}
.statuscluster a.chip .badge{max-width:14em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
@media(max-width:700px){ .statuscluster a.chip .badge{max-width:6.5em} }
.clab{font-family:var(--sans);font-size:11px;color:var(--dim)}
.fsbtn{flex:0 0 auto}
.warnbar{background:rgba(217,161,59,.12);border-bottom:1px solid rgba(217,161,59,.5);color:var(--warn);
  padding:8px 22px;font-size:13px;line-height:1.5;overflow-wrap:anywhere}
.simbar{position:fixed;left:0;right:0;bottom:0;z-index:50;display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;
  padding:8px 14px;background:var(--surface);border-top:2px solid var(--accent);font-size:13px}
.simbar button{font-size:12px;padding:4px 9px}
.simbar .simoff{margin-left:auto}
.simbar details.simfault summary{cursor:pointer;color:var(--muted)}
.simbar .simf{display:inline-flex;gap:4px;align-items:center;margin-right:8px}
.simbar details.simfault p{margin:4px 0 0;font-size:11px}
body:has(#simbar){padding-bottom:72px}
.jobtxt{color:inherit;overflow:hidden;text-overflow:ellipsis;min-width:0}
.langbtn{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:6px;padding:2px 7px;text-decoration:none}
.langbtn:hover{color:var(--text);border-color:var(--accent);text-decoration:none}
@media(max-width:1600px){ .statuscluster .jobid{display:none} .nav a{padding:0 10px} .appbar{gap:18px} }
@media(max-width:1750px){ .clab{display:none} }
@media(max-width:1350px){ .nav a{padding:0 8px} }
@media(max-width:1300px) and (min-width:821px){ .brand small{display:none} .nav a{padding:0 6px;font-size:13px}
  .navmore>summary{padding:0 6px;font-size:13px} .statuscluster{gap:6px} .appbar{gap:10px;padding:0 14px} }
.dot{width:8px;height:8px;border-radius:50%;background:var(--dim)}
.dot.live{background:var(--ok);animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@media (prefers-reduced-motion: reduce){.dot.live{animation:none}}
.wrap{padding:26px 22px 60px;max-width:1240px;margin:auto}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:var(--dim);margin:0 0 6px}
h2{margin:0 0 20px;font-size:19px;font-weight:600}
.card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:18px 20px;margin-bottom:18px}
table{border-collapse:collapse;width:100%}
th{font-family:var(--mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
  color:var(--dim);font-weight:500;text-align:left;padding:8px 12px;border-bottom:1px solid var(--line)}
td{padding:9px 12px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
tbody tr:hover{background:var(--surface2)}
td.num{font-family:var(--mono);font-size:13px;text-align:right;color:var(--muted);
  font-variant-numeric:tabular-nums}
th.num{text-align:right}
.mono{font-family:var(--mono);font-size:13px}
.badge{display:inline-block;white-space:nowrap;font-family:var(--mono);font-size:11px;letter-spacing:.06em;
  padding:2px 9px;border-radius:20px;border:1px solid transparent}
.b-ok{color:var(--ok);border-color:var(--ok);background:rgba(87,185,138,.08)}
.b-bad{color:var(--bad);border-color:var(--bad);background:rgba(201,96,96,.08)}
.b-run{color:var(--accent);border-color:var(--accent);background:rgba(93,157,214,.08)}
.b-warn{color:var(--warn);border-color:var(--warn);background:rgba(217,161,59,.08)}
button{font-family:var(--sans);font-size:13px;font-weight:500;
  background:var(--surface2);color:var(--text);border:1px solid var(--line);
  border-radius:7px;padding:7px 14px;cursor:pointer;transition:border-color .12s}
button:hover{border-color:var(--accent)}
button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
button.primary{background:var(--accent-dim);border-color:var(--accent);color:#dceafe}
button.danger{color:var(--bad);border-color:rgba(201,96,96,.5)}
button.danger:hover{border-color:var(--bad);background:rgba(201,96,96,.08)}
button.big{font-size:15px;padding:12px 22px;font-family:var(--mono)}
a.btnlink{display:inline-block;font-family:var(--sans);font-size:13px;font-weight:500;
  background:var(--surface2);color:var(--text);border:1px solid var(--line);
  border-radius:7px;padding:7px 14px;text-decoration:none;transition:border-color .12s}
a.btnlink:hover{border-color:var(--accent);color:var(--text)}
input,select{font-family:var(--sans);font-size:13.5px;background:var(--bg);
  color:var(--text);border:1px solid var(--line);border-radius:7px;padding:7px 10px}
input:focus,select:focus{outline:none;border-color:var(--accent)}
label.f{display:flex;flex-direction:column;gap:4px;font-size:12.5px;color:var(--muted)}
.formgrid{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;margin-bottom:20px}
.toolbar{display:flex;gap:8px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.vpanel{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.vpanel .vhead{display:flex;justify-content:space-between;align-items:center;
  padding:8px 14px;border-bottom:1px solid var(--line);
  font-family:var(--mono);font-size:12px;color:var(--muted)}
.vpanel .vhead b{color:var(--text);font-weight:600;letter-spacing:.1em;text-transform:uppercase}
video{width:100%;display:block;background:#000}
.markbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  background:rgba(201,96,96,.06);border:1px solid rgba(201,96,96,.35);
  border-radius:10px;padding:12px 16px;margin-bottom:18px}
.runbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
  background:rgba(93,157,214,.06);border:1px solid rgba(93,157,214,.4);
  border-radius:10px;padding:12px 16px;margin-bottom:18px}
pre{font-family:var(--mono);font-size:12.5px;line-height:1.5;background:#0a0d10;
  border:1px solid var(--line);padding:12px 14px;border-radius:10px;
  overflow-x:auto;max-height:340px;color:#b9c4d0}
.muted{color:var(--muted);font-size:13px}
form.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:6px 0 22px}
.chartbox{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px;margin-bottom:18px}
.keys{display:flex;gap:12px;margin:14px 0 18px}
p.badge{white-space:normal;max-width:100%}
input[type=file]{max-width:100%}
details.adv{margin-bottom:6px}
details.adv>summary{cursor:pointer;list-style:none;font-family:var(--mono);font-size:11px;letter-spacing:.18em;
  text-transform:uppercase;color:var(--dim);margin:0 0 10px}
details.adv>summary::-webkit-details-marker,details.more>summary::-webkit-details-marker{display:none}
details.adv>summary::before,details.more>summary::before{content:'▸ ';color:var(--accent)}
details.adv[open]>summary::before,details.more[open]>summary::before{content:'▾ '}
details.more>summary{cursor:pointer;list-style:none;color:var(--muted);font-size:13px}
.wizcard{border-color:var(--accent);background:rgba(93,157,214,.06)}
button:disabled,button:disabled:hover{opacity:.42;cursor:not-allowed;border-color:var(--line)}
a.btnlink.primary{background:var(--accent-dim);border-color:var(--accent);color:#dceafe}
.gs .gshead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.gs .gshead b{font-size:15px}
.gs .gshide{margin-left:auto;padding:4px 10px;font-size:12px}
.gsteps{list-style:none;margin:0;padding:0}
.gsteps li{display:flex;align-items:center;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
.gsteps li .gst{flex:1;display:flex;flex-direction:column;min-width:0}
.gsteps li .gst .muted{font-size:12.5px}
.gsn{flex:0 0 26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-family:var(--mono);font-size:12px;border:1px solid var(--line);color:var(--muted)}
.gsteps li.done .gsn{background:rgba(87,185,138,.12);border-color:var(--ok);color:var(--ok)}
.gsteps li.done .gst b{color:var(--muted)}
.gsteps li.next .gsn{border-color:var(--accent);color:var(--accent)}
.gsteps li.todo{opacity:.7}
.trainform select#ds{min-width:220px} .trainform select#target{max-width:280px}
.needcard{display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-color:rgba(217,161,59,.55);
  background:rgba(217,161,59,.06)}
.needcard .needtxt{flex:1;min-width:220px;display:flex;flex-direction:column;gap:2px}
.needcard .needtxt b{font-size:15px}

/* ---- 반응형 (태블릿/모바일) ---- */
@media(max-width:820px){
  .appbar{gap:12px;padding:0 14px;height:auto;min-height:52px;flex-wrap:wrap}
  .brand{font-size:12px}
  .brand small{display:none}
  .statuscluster{font-size:11px;max-width:62vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;gap:6px}
  .statuscluster a.chip,.statuscluster>.badge{display:none}
  .nav{order:3;width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;
    height:46px;gap:0;border-top:1px solid var(--line)}
  .nav a{padding:0 15px;white-space:nowrap;flex:0 0 auto}
  .wrap{padding:18px 14px 48px}
  h2{font-size:18px;margin-bottom:16px}
  button{padding:10px 15px}
  button.big{padding:13px 22px}
  input,select{font-size:16px;padding:9px 11px}   /* 16px = iOS 자동확대 방지 */
  label.f{min-width:0!important}
  .card{padding:16px 15px;overflow-x:auto}
  pre{max-height:52vh}
}
@media(max-width:560px){
  .formgrid{gap:10px}
  .formgrid label.f{flex:1 1 100%}   /* 폼 필드 세로 스택 */
  form.row > *{flex:1 1 100%}
  .nav a{padding:0 13px;font-size:13px}
  table{font-size:13px}
  th,td{padding:8px 9px}
  th{white-space:nowrap}
}
.fsbtn{padding:5px 9px;font-size:15px;line-height:1;background:transparent;
  border-color:var(--line);color:var(--muted);display:inline-flex;align-items:center}
.fsbtn:hover{color:var(--text);border-color:var(--accent)}
</style>
<script>
document.addEventListener('click',function(e){
  document.querySelectorAll('details.navmore[open]').forEach(function(d){ if(!d.contains(e.target)) d.removeAttribute('open'); });
});
function toggleFS(){
  var el=document.documentElement;
  if(document.fullscreenElement){ (document.exitFullscreen||document.webkitExitFullscreen).call(document); }
  else{ (el.requestFullscreen||el.webkitRequestFullscreen).call(el); }
}
if('serviceWorker' in navigator){ navigator.serviceWorker.register('/sw.js').catch(function(){}); }
</script>"""


def need_html(title, desc, href, btn):
    """먼저 해야 할 일이 남아 그 화면의 시작 버튼을 막았을 때 띄우는 큰 안내 — 다음에 갈 곳을 버튼으로."""
    return (f'<div class="card needcard"><div class=needtxt><b>{title}</b><span class=muted>{desc}</span></div>'
            f'<a class="btnlink primary" href="{href}">{btn}</a></div>')


def setup_needed_html():
    """포트가 아직 지정되지 않았을 때 각 탭 상단에 띄우는 안내."""
    if ports_configured():
        return ""
    return need_html("먼저 팔을 연결하세요", "USB 포트가 아직 지정되지 않았습니다 — 셋업 마법사에서 팔(보드)을 하나씩 연결합니다.",
                     "/setup/wizard", "셋업 마법사 시작")


# 상단 작업 표시에 쓰는 작업 종류 이름 (영어 화면은 사전의 <span class=jobk>…</span> 항목으로 바뀝니다)
JOB_KIND_LABEL = {"record": "수집", "train": "학습", "cloudtrain": "클라우드 학습", "rollout": "실행", "control": "제어",
                  "setup": "포트 감시", "hub": "허브", "install": "설치", "ovconvert": "OpenVINO 변환",
                  "repair": "복구", "delete": "삭제", "sim": "가상 장치"}


def nav_html(active=""):
    running = [j for j in jobs_index() if j["alive"] and j["kind"] != "sim"]     # 가상 장치는 아래 막대에 따로 표시
    if any_arm_connected():
        running = [{"id": "manual-control", "kind": "control"}] + running
    if running:
        j = running[0]
        extra = f' +{len(running) - 1}' if len(running) > 1 else ''
        label = JOB_KIND_LABEL.get(j["kind"], j["kind"])
        cluster = (f'<div class=statuscluster><span class="dot live"></span>'
                   f'<a href="/jobs/{esc(j["id"])}" class=jobtxt title="실행 중인 작업 — {esc(j["id"])}">'
                   f'<span class=jobk>{esc(label)}</span><span class=jobid> · {esc(j["id"])}</span>{extra}</a></div>')
    else:
        cluster = '<div class=statuscluster><span class=dot></span><span class=jobk title="실행 중인 작업 없음">대기</span></div>'
    pname, _ = active_project()
    # 상태 칩마다 무엇인지 작은 이름표를 붙입니다 (좁은 화면에서는 이름표를 숨기고 마우스를 올리면 보임)
    mode_badge = (f'<a href="/projects" class=chip title="열린 프로젝트 — 누르면 프로젝트 목록"><span class=clab>프로젝트</span>'
                  f'<span class="badge b-run">{esc(pname)}</span></a> ' if pname else
                  '<a href="/projects" class=chip title="열린 프로젝트 없음 — 모든 데이터셋·모델을 봅니다"><span class=clab>프로젝트</span>'
                  '<span class=badge>전체</span></a> ')
    mode_badge += (f'<a href="/setup#envcard" class=chip title="사용 중인 환경(팔 세트 구성)"><span class=clab>환경</span>'
                   f'<span class=badge>{esc(env_name())}</span></a> ')
    if I18N_FILE.is_file():            # 영어 사전이 있을 때만 한/영 버튼
        mode_badge = '<a href="/lang" class=langbtn title="한국어 / English">한/EN</a> ' + mode_badge
    mode_badge += ('<span class="badge b-run" title="양팔 구성">양팔</span>' if BIMANUAL
                   else '<span class="badge" title="한팔 구성">한팔</span>')
    if not ports_configured():
        mode_badge += ' <a href="/setup/wizard" title="셋업 마법사에서 포트를 정하세요"><span class="badge b-warn">포트 미설정</span></a>'
    cluster = cluster.replace('<div class=statuscluster>',
                              f'<div class=statuscluster>{mode_badge}&nbsp;')

    def tab(href, label, key, sub=False):
        cls = " ".join(c for c in ("on" if key == active else "", "sub" if sub else "") if c)
        return f'<a href="{href}"{f" class={chr(34)}{cls}{chr(34)}" if cls else ""}><span class=tl>{label}</span></a>'

    # 작업 순서대로: 셋업 → 캘리브레이션 → 제어 → 수집 → 데이터셋 → 학습 → 모델 → 실행, 나머지는 '더보기'
    subs = [("/projects", "프로젝트", "pj"), ("/hub", "허브", "hb"), ("/jobs", "작업", "jb")]
    cur = next((lb for _, lb, k in subs if k == active), "")
    more = (f'<details class=navmore{" data-on=1" if cur else ""}><summary><span class=tl>{cur or "더보기"}</span> ▾</summary><div class=navmenu>'
            + "".join(f'<a href="{h}"{" class=on" if k == active else ""}><span class=tl>{lb}</span></a>' for h, lb, k in subs)
            + '</div></details>')
    return (f'<div class=appbar><div class=brand>OpenMimic <a href="/setup/wizard" title="기종 바꾸기 — 셋업 마법사"><small>/ {esc(kind()["label"])}</small></a></div>'
            f'<div class=nav>{tab("/setup", "셋업", "st")}{tab("/calib", "캘리브레이션", "cb")}{tab("/control", "제어", "ct")}'
            f'{tab("/collect", "수집", "co")}{tab("/", "데이터셋", "ds")}{tab("/train", "학습", "tr")}{tab("/models", "모델", "md")}'
            f'{tab("/rollout", "실행", "ro")}</div>{more}{cluster}'
            f'<button class=fsbtn title="전체화면" aria-label="전체화면" onclick="toggleFS()">'
            f'<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="M8 3H5a2 2 0 0 0-2 2v3M16 3h3a2 2 0 0 1 2 2v3M8 21H5a2 2 0 0 1-2-2v-3M16 21h3a2 2 0 0 0 2-2v-3"/>'
            f'</svg></button></div>' + (f'<div class=warnbar>{esc(calib_root_warning())}</div>' if calib_root_warning() else '')
            + sim_panel_html())


# ----------------------------- PWA / 키오스크 -------------------------------
@app.get("/manifest.webmanifest")
def api_manifest():
    return JSONResponse({
        "name": "OpenMimic — Robot Arm Imitation Learning", "short_name": "OpenMimic",
        "start_url": "/", "scope": "/", "display": "fullscreen",
        "orientation": "landscape",
        "background_color": "#0f1216", "theme_color": "#161b21",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
        ],
    }, media_type="application/manifest+json")


@app.get("/icon-{size}.png")
def api_icon(size: int):
    size = max(16, min(1024, size))
    return Response(content=icon_png(size), media_type="image/png")


@app.get("/sw.js")
def api_sw():
    js_src = ("self.addEventListener('install',function(e){self.skipWaiting();});"
              "self.addEventListener('activate',function(e){self.clients.claim();});"
              "self.addEventListener('fetch',function(){});")
    return Response(content=js_src, media_type="application/javascript")


# ----------------------------- 페이지: 데이터셋 -------------------------------
# ----------------------------- 프로젝트 ---------------------------------------
def dataset_thumb(ds):
    """썸네일용: 첫 에피소드의 첫 카메라 영상 URL 과 시작 시각. 프레임은 브라우저가 뽑습니다
    (서버에서 디코딩하지 않으므로 AV1 등 코덱과 무관)."""
    try:
        segs = ep_video_segments(episodes_df(ds), 0)
    except Exception:
        return None
    if not segs:
        return None
    s0 = segs[0]
    return {"url": f"/videos/{ds}/{s0['path']}", "t": s0["from"]}


def projects_view():
    d = load_projects()
    dsets = {x["name"]: x for x in list_datasets()}
    ckpt_runs = {r.split("/checkpoints/")[0] for r in list_checkpoints()}
    runs = {p.name for p in OUT_ROOT.iterdir() if p.is_dir()} if OUT_ROOT.exists() else set()
    assigned = set()
    meta = load_dsmeta()
    out = []
    for name, pr in d["projects"].items():
        ds = [dsets[x] for x in pr["datasets"] if x in dsets]
        assigned.update(x["name"] for x in ds)
        latest = max(ds, key=lambda x: meta.get(x["name"], {}).get("created", ""), default=None)
        out.append({"name": name, "task": pr["task"], "env": pr["env"],
                    "datasets": [{"name": x["name"], "episodes": x["episodes"]} for x in ds],
                    "episodes": sum(x["episodes"] for x in ds if isinstance(x["episodes"], int)),
                    "models": [{"name": m, "has_ckpt": m in ckpt_runs} for m in pr["models"] if m in runs],
                    "updated": pr.get("updated", ""), "created": pr.get("created", ""),
                    "thumb": dataset_thumb(latest["name"]) if latest else None})
    out.sort(key=lambda x: x["updated"], reverse=True)
    unassigned = [{"name": n, "episodes": x["episodes"]} for n, x in dsets.items() if n not in assigned]
    return {"active": d["active"], "projects": out, "unassigned": unassigned,
            "envs": [e["name"] for e in list_envs()], "env": env_name(),
            "all": {"datasets": len(dsets),
                    "episodes": sum(x["episodes"] for x in dsets.values() if isinstance(x["episodes"], int)),
                    "models": len(runs)}}


@app.get("/api/projects")
def api_projects():
    return projects_view()


@app.post("/api/projects/save")
async def api_project_save(req: Request):
    b = await req.json()
    name = (b.get("name") or "").strip()
    old = (b.get("old") or "").strip()
    if not safe_name(name):
        return JSONResponse({"error": "프로젝트 이름은 영문/숫자/._- 만"}, status_code=400)
    env = (b.get("env") or "").strip()
    if env and not safe_name(env):
        return JSONResponse({"error": "환경 이름 오류"}, status_code=400)
    d = load_projects()
    if old and old not in d["projects"]:
        return JSONResponse({"error": f"프로젝트 없음: {old}"}, status_code=400)
    if name != old and name in d["projects"]:
        return JSONResponse({"error": f"이미 있는 프로젝트: {name}"}, status_code=400)
    pr = d["projects"].pop(old) if old else {"datasets": [], "models": [],
                                             "created": time.strftime("%Y-%m-%d %H:%M")}
    pr["task"] = (b.get("task") or "").strip()[:300]
    pr["env"] = env
    _touch(pr)
    d["projects"][name] = pr
    if old and d["active"] == old:
        d["active"] = name
    if not old and b.get("activate"):
        d["active"] = name
    save_projects(d)
    return await asyncio.to_thread(projects_view)


@app.post("/api/projects/activate")
async def api_project_activate(req: Request):
    b = await req.json()
    name = (b.get("name") or "").strip()
    d = load_projects()
    if name and name not in d["projects"]:
        return JSONResponse({"error": f"프로젝트 없음: {name}"}, status_code=400)
    d["active"] = name
    save_projects(d)
    return await asyncio.to_thread(projects_view)


@app.post("/api/projects/delete")
async def api_project_delete(req: Request):
    b = await req.json()
    d = load_projects()
    name = (b.get("name") or "").strip()
    if name not in d["projects"]:
        return JSONResponse({"error": f"프로젝트 없음: {name}"}, status_code=400)
    del d["projects"][name]
    if d["active"] == name:
        d["active"] = ""
    save_projects(d)
    return await asyncio.to_thread(projects_view)


@app.post("/api/projects/assign")
async def api_project_assign(req: Request):
    b = await req.json()
    kind = b.get("kind")
    item = (b.get("item") or "").strip()
    if kind not in ("datasets", "models") or not safe_name(item):
        return JSONResponse({"error": "잘못된 요청"}, status_code=400)
    try:
        assign_to_project(kind, item, (b.get("project") or "").strip())
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return await asyncio.to_thread(projects_view)


@app.get("/projects", response_class=HTMLResponse)
def projects_page():
    return CSS + nav_html("pj") + PROJECTS_HTML


GS_COOKIE = "openmimic_gs"            # "hide" = 시작하기 안내 숨김
WIZ_COOKIE = "openmimic_wiz"           # 셋업 마법사를 한 번이라도 연 브라우저 — 첫 실행 자동 이동은 한 번만


def getting_started():
    """처음 쓰는 사람용 5단계 — (이름, 설명, 링크, 버튼 글, 완료 여부)."""
    cal = calib_status()
    return [
        ("셋업", "팔(보드)을 USB 에 꽂고 리더·팔로워 포트를 정합니다 — 팔이 없으면 셋업 마법사에서 가상 장치로 해 볼 수 있습니다",
         "/setup/wizard", "셋업 마법사", ports_configured()),
        ("캘리브레이션", "관절을 손으로 움직여 범위를 기록합니다 — 셋업 마법사에서 이어서 합니다", "/setup/wizard", "캘리브레이션",
         all(c["ok"] for sd in cal.values() for c in sd.values())),
        ("수집", "리더 팔로 시연해 데이터셋을 만듭니다 (보통 에피소드 50개 안팎)", "/collect", "수집하러 가기",
         any(isinstance(d["episodes"], int) and d["episodes"] > 0 for d in list_datasets())),
        ("학습", "모은 데이터셋으로 정책(ACT 등)을 학습합니다", "/train", "학습하러 가기", bool(list_checkpoints())),
        ("실행", "학습한 모델로 팔을 자율 구동하고 성공률을 기록합니다", "/rollout", "실행하러 가기",
         any(j["kind"] == "rollout" for j in jobs_index())),
    ]


def getting_started_html(request):
    steps = getting_started()
    done = sum(1 for st in steps if st[4])
    if done == len(steps):
        return ""
    if request.cookies.get(GS_COOKIE) == "hide":
        return ('<p class=muted style="margin:-8px 0 14px"><a href="#" onclick="document.cookie=\'openmimic_gs=; path=/; max-age=0\';'
                'location.reload();return false">시작하기 안내 다시 보기</a></p>')
    nxt = next(i for i, st in enumerate(steps) if not st[4])
    lis = ""
    for i, (name, desc, href, btn, ok) in enumerate(steps):
        state = "done" if ok else ("next" if i == nxt else "todo")
        mark = "✓" if ok else str(i + 1)
        action = (f'<a class="btnlink{" primary" if state == "next" else ""}" href="{href}">{btn}</a>'
                  if not ok else '<span class="badge b-ok">완료</span>')
        lis += (f'<li class={state}><span class=gsn>{mark}</span><div class=gst><b class=gsname>{name}</b>'
                f'<span class=muted>{desc}</span></div>{action}</li>')
    return (f'<div class="card gs"><div class=gshead><b>시작하기</b><span class=muted>{done} / {len(steps)} 완료 — '
            f'위에서부터 차례로 하면 됩니다</span><button class=gshide onclick="document.cookie=\'openmimic_gs=hide; path=/; '
            f'max-age=31536000\';location.reload()">숨기기</button></div><ol class=gsteps>{lis}</ol></div>')


@app.get("/", response_class=HTMLResponse)
def index(request: Request, all: int = 0):
    # 처음 실행(포트 미설정 · 데이터셋·모델 없음)이고 이 브라우저가 셋업 마법사를 연 적이 없으면 마법사부터
    if (not request.cookies.get(WIZ_COOKIE) and not ports_configured()
            and not list_datasets() and not list_checkpoints()):
        return RedirectResponse("/setup/wizard", status_code=303)
    pname, pr = active_project()
    meta = load_dsmeta()
    projects = load_projects()["projects"]
    owner = {x: n for n, pj in projects.items() for x in pj["datasets"]}
    dsets = list_datasets()
    show_all = bool(all) or not pname
    view = dsets if show_all else [d for d in dsets if owner.get(d["name"]) == pname]
    cur_env = env_name()

    def _row(d):
        n = d["name"]
        mismatch = ' <span class="badge b-warn">모드 불일치</span>' \
            if not robot_type_ok(d["robot_type"]) else ""
        broken = dataset_unfinalized(n)
        if broken:
            mismatch += (' <span class="badge b-bad" title="녹화가 끊겨 에피소드 색인이 없습니다 — 열리지 않습니다">마무리 안 됨</span>'
                         f' <button onclick="repairDs({jsattr(n)})">복구</button>')
        ver = d["version"]
        verbadge = (f'<span class=badge>{esc(ver)}</span>' if ver == "v3.0"
                    else f'<span class="badge b-warn">{esc(ver)}</span>')
        env = meta.get(n, {}).get("env", "")
        envcell = (f'<span class=mono style="color:{"var(--text)" if env == cur_env else "var(--dim)"}">{esc(env)}</span>'
                   if env else '<span class=tiny>-</span>')
        projcell = ""
        if show_all and projects:
            opts = "".join(f'<option value="{esc(x)}"{" selected" if owner.get(n) == x else ""}>{esc(x)}</option>'
                           for x in projects)
            projcell = (f'<td><select onchange="moveDs({jsattr(n)},this.value)">'
                        f'<option value="">미분류</option>{opts}</select></td>')
        return (f'<tr><td><a href="/ds/{esc(n)}" class=mono>{esc(n)}</a></td>'
                f'<td class=num>{esc(d["episodes"])}</td><td class=num>{esc(d["frames"])}</td>'
                f'<td class=num>{esc(d["fps"])}</td>'
                f'<td class=mono style="color:var(--muted)">{esc(d["robot_type"])}{mismatch}</td>'
                f'<td>{envcell}</td>{projcell}'
                f'<td>{verbadge}</td>'
                f'<td class=num style="color:var(--muted)">{esc(human_bytes(d["bytes"]))}</td>'
                f'<td style="text-align:right;white-space:nowrap">'
                f'<a class=btnlink href="/ds/{esc(n)}">리뷰</a> '
                f'<a class=btnlink href="/api/download/{esc(n)}" '
                f'title="폴더를 그대로 tar 로 내려받습니다 ({d["files"]}개 파일)">다운로드</a> '
                f'<button class=danger onclick="delDs({jsattr(n)})">삭제</button></td></tr>')
    rows = "".join(_row(d) for d in view)
    ncol = 10 + (1 if show_all and projects else 0)
    if rows:
        empty = ""
    elif show_all:
        empty = f'<tr><td colspan={ncol} class=muted>데이터셋이 없습니다 — 수집 탭에서 수집을 시작하세요</td></tr>'
    else:
        empty = (f'<tr><td colspan={ncol} class=muted>이 프로젝트에 데이터셋이 없습니다 — 수집에서 수집하면 자동으로 들어옵니다. '
                 f'기존 데이터셋은 <a href="/?all=1">전체 보기</a>에서 옮기세요.</td></tr>')
    if pname:
        head = (f'<p class=eyebrow>프로젝트 · {esc(pname)}</p><h2>데이터셋</h2>'
                f'<p class=muted>{esc(pr["task"]) or "<i>태스크 설명 없음</i>"} · 기준 환경 '
                f'<span class=mono>{esc(pr["env"] or "지정 안 함")}</span>'
                + (f' <span class="badge b-warn">지금 환경은 {esc(cur_env)}</span>' if pr["env"] and pr["env"] != cur_env else "")
                + (f' · <a href="/">이 프로젝트만 보기</a>' if show_all else f' · <a href="/?all=1">전체 보기</a>')
                + ' · <a href="/projects">프로젝트 목록</a></p>')
    else:
        head = ('<p class=eyebrow>전체 데이터셋</p><h2>데이터셋</h2>'
                '<p class=muted>프로젝트 구분 없이 모두 보는 중입니다 — <a href="/projects">프로젝트</a>를 열면 그 프로젝트 기준으로 보입니다.</p>')
    projhead = '<th>프로젝트</th>' if show_all and projects else ''
    return f"""{CSS}{nav_html('ds')}<div class=wrap>
    {head}{getting_started_html(request)}
    <div class=card><table>
    <tr><th>이름</th><th class=num>에피소드</th><th class=num>프레임</th><th class=num>fps</th>
    <th>로봇</th><th>환경</th>{projhead}<th>형식</th><th class=num>크기</th><th></th></tr>
    {rows}{empty}</table></div>
    <p class=muted>다운로드한 파일은 다른 OpenMimic 의 데이터셋 화면 <b>가져오기</b>로 그대로 옮길 수 있습니다.
    환경 열은 수집할 때 쓴 환경이고, 흐리게 보이면 지금 환경과 다른 것입니다.
    <b>삭제</b>는 폴더를 통째로 지웁니다 (복구 불가, 이름을 입력해야 실행).</p>
    <details class=more><summary>다운로드 파일 형식</summary><p class=muted style="margin-top:6px">
    <b>LeRobotDataset v3.0</b> 폴더를 그대로 tar 로 감싼 것입니다 (압축 없음).
    <span class=mono>tar xf 이름_v3.0.tar</span> 로 풀면 lerobot 에서
    <span class=mono>LeRobotDataset(repo_id, root=풀린폴더)</span> 로 바로 열립니다.</p></details>
    <div class=card style="margin-top:14px"><div class=formgrid>
      <label class=f style="min-width:320px">데이터셋 가져오기 (.tar / .zip)
        <input type=file id=upf accept=".tar,.gz,.tgz,.zip"></label>
      <button class=primary onclick="upload('dataset')">가져오기</button><span class=muted id=upmsg></span></div>
      <p class=muted style="margin:6px 0 0">위 다운로드 파일이나 meta/info.json 이 든 LeRobot 데이터셋 폴더를 묶은 파일. 같은 이름이 있으면 _2 를 붙입니다.</p></div>
    </div>
    <script>
    {UPLOAD_JS}
    async function repairDs(name){{
      if(!confirm('"'+name+'" 를 복구합니다.\\n남아 있는 온전한 에피소드로 색인을 다시 만듭니다 (끊긴 마지막 에피소드는 잃을 수 있습니다).\\n고치기 전 meta·data 를 백업합니다. 계속할까요?')) return;
      const r=await fetch('/api/dataset/repair/'+encodeURIComponent(name),{{method:'POST'}}); const d=await r.json();
      if(d.error) alert(d.error); else location.href='/jobs/'+d.job;
    }}
    async function delDs(name){{
      const typed = prompt('데이터셋 "'+name+'" 을 통째로 삭제합니다.\\n확인을 위해 이름을 그대로 입력하세요:');
      if(typed !== name){{ if(typed!==null) alert('이름 불일치 — 취소됨'); return; }}
      const r = await fetch('/api/delete_dataset/'+encodeURIComponent(name), {{method:'POST'}});
      const d = await r.json();
      if(d.error) alert(d.error); else location.reload();
    }}
    async function moveDs(name, project){{
      const r = await fetch('/api/projects/assign', {{method:'POST', headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{kind:'datasets', item:name, project:project}})}});
      const d = await r.json();
      if(d.error) alert(d.error);
    }}
    </script>"""


@app.post("/api/delete_dataset/{ds}")
def api_delete_dataset(ds: str):
    if not safe_name(ds):
        return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
    p = (DATA_ROOT / ds).resolve()
    if not _within(p, DATA_ROOT) or not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    if not (p / "meta/info.json").exists():
        return JSONResponse({"error": "데이터셋 폴더가 아님"}, status_code=400)
    j = dataset_busy(ds)
    if j:
        return JSONResponse({"error": f"실행 중인 작업({j['id']})이 이 데이터셋을 사용 중"}, status_code=400)
    shutil.rmtree(p)
    m = load_marks()
    m.pop(ds, None)
    save_marks(m)
    meta = load_dsmeta()
    if meta.pop(ds, None) is not None:
        save_dsmeta(meta)
    assign_to_project("datasets", ds, "")
    return {"ok": True}


# ----------------------------- 데이터셋 내려받기 ------------------------------
# OpenMimic 이 수집한 데이터셋은 이미 LeRobotDataset v3.0 레이아웃입니다
# (meta/info.json 의 codebase_version 이 v3.0). 변환할 게 없으므로 폴더를
# 그대로 tar 로 감싸 스트리밍합니다. mp4 / parquet 는 이미 압축된 포맷이라
# gzip 을 걸면 CPU 만 먹고 크기는 거의 안 줄어듭니다.
_TAR_CHUNK = 1 << 20          # 1 MiB 씩 읽어 흘립니다 (파일 전체를 메모리에 올리지 않음)
_TAR_RECORD = 10240           # tar 표준 레코드 크기


def _tar_files(root: Path, skip=()):
    """root 아래 일반 파일만, 경로 정렬해서 (실제경로, tar 내부경로) 로 돌려줍니다.
    skip: 빼낼 하위 폴더 (root 기준 상대경로, 예: 'openvino/cache')"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        dirnames[:] = [d for d in dirnames if (f"{rel_dir}/{d}" if rel_dir != "." else d) not in skip]
        dirnames.sort()
        for name in sorted(filenames):
            f = Path(dirpath) / name
            if f.is_symlink() or not f.is_file():
                continue
            yield f, f.relative_to(root).as_posix()


def _tar_stream(root: Path, arc_root: str, skip=()):
    """tarfile 객체 대신 블록을 직접 만들어 흘립니다.
    tarfile.addfile 은 파일을 통째로 버퍼에 복사해서, 수 GB 짜리 영상이 들어간
    데이터셋에서는 메모리를 그만큼 먹습니다."""
    total = 0
    for path, rel in _tar_files(root, skip):
        try:
            st = path.stat()
        except OSError:
            continue
        ti = tarfile.TarInfo(f"{arc_root}/{rel}")
        ti.size = st.st_size
        ti.mtime = int(st.st_mtime)
        ti.mode = 0o644
        ti.type = tarfile.REGTYPE
        ti.uid = ti.gid = 0
        ti.uname = ti.gname = ""
        # PAX 포맷이면 100 자를 넘는 경로도 tobuf 가 확장 헤더까지 만들어 줍니다.
        head = ti.tobuf(tarfile.PAX_FORMAT)
        total += len(head)
        yield head
        written = 0
        try:
            with path.open("rb") as fh:
                while written < ti.size:
                    b = fh.read(min(_TAR_CHUNK, ti.size - written))
                    if not b:
                        break
                    written += len(b)
                    total += len(b)
                    yield b
        except OSError:
            pass
        if written < ti.size:
            # 스트리밍 도중 파일이 잘렸습니다. 헤더에 적은 크기만큼은 채워야
            # tar 구조가 깨지지 않습니다.
            pad = b"\0" * (ti.size - written)
            total += len(pad)
            yield pad
        pad = -ti.size % 512
        if pad:
            total += pad
            yield b"\0" * pad
    tail = b"\0" * 1024
    total += len(tail)
    yield tail
    pad = -total % _TAR_RECORD
    if pad:
        yield b"\0" * pad


@app.get("/api/download/{ds}")
def api_download_dataset(ds: str):
    if not safe_name(ds):
        return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
    root = (DATA_ROOT / ds).resolve()
    if not _within(root, DATA_ROOT) or not root.is_dir():
        return JSONResponse({"error": "not found"}, status_code=404)
    info = load_json(root / "meta/info.json", {})
    if not info:
        return JSONResponse({"error": "meta/info.json 이 없습니다 — 데이터셋 폴더가 아님"}, status_code=400)
    # 수집 중인 데이터셋은 tar 를 뜨는 동안 파일이 계속 자라 무결성이 깨집니다.
    rec = dataset_busy(ds)
    if rec and rec.get("kind") in ("record", "delete"):
        return JSONResponse({"error": f"{rec['id']} 가 이 데이터셋을 쓰는 중 — 끝난 뒤 받으세요"},
                            status_code=409)
    ver = str(info.get("codebase_version", "unknown")).replace("/", "_")
    fname = f"{ds}_{ver}.tar"
    return StreamingResponse(
        _tar_stream(root, ds),
        media_type="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{fname}"',
                 "X-Dataset-Version": ver,
                 "Cache-Control": "no-store"})


def _feature_names(info, key):
    f = (info.get("features") or {}).get(key) or {}
    names = f.get("names")
    if isinstance(names, dict):                      # {"motors": [...]} 형식
        names = next(iter(names.values()), [])
    return list(names) if isinstance(names, (list, tuple)) else []


def dataset_review_info(ds):
    root = DATA_ROOT / ds
    info = load_json(root / "meta/info.json", {})
    df = episodes_df(ds)
    fps = float(info.get("fps") or 30)
    eps = []
    for _, r in df.iterrows():
        ep = int(r["episode_index"])
        segs = [{"cam": sg["cam"], "url": f"/videos/{ds}/{sg['path']}", "from": sg["from"], "to": sg["to"]}
                for sg in ep_video_segments(df, ep)]
        length = int(r.get("length", 0) or 0)
        tasks = r.get("tasks", "")
        if hasattr(tasks, "tolist"):
            tasks = tasks.tolist()
        if isinstance(tasks, (list, tuple)):
            tasks = " / ".join(str(t) for t in tasks)
        dur = (segs[0]["to"] - segs[0]["from"]) if segs else (length / fps if fps else 0)
        eps.append({"ep": ep, "length": length, "dur": round(dur, 2), "task": str(tasks or "")})
        eps[-1]["segs"] = segs
    lens = sorted(e["length"] for e in eps if e["length"])
    med = lens[len(lens) // 2] if lens else 0
    for e in eps:                                     # 불량 후보 힌트: 중앙값의 절반도 안 되는 에피소드
        e["short"] = bool(med and e["length"] < med * 0.5)
    state = _feature_names(info, "observation.state")
    return {"name": ds, "fps": fps, "robot_type": info.get("robot_type", ""),
            "version": info.get("codebase_version", ""), "episodes": eps,
            "state_names": state, "action_names": _feature_names(info, "action"),
            "bimanual": any(n.startswith("left_") for n in state),
            "marks": load_marks().get(ds, []), "env": load_dsmeta().get(ds, {}).get("env", ""),
            "project": project_of("datasets", ds)}


@app.get("/api/ds/{ds}/info")
def api_ds_info(ds: str):
    if not safe_name(ds) or not (DATA_ROOT / ds / "meta/info.json").exists():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=404)
    return dataset_review_info(ds)


@app.get("/api/ds/{ds}/ep/{ep}/data")
def api_ep_data(ds: str, ep: int):
    """관절 그래프·3D 재생용 — observation.state / action 을 에피소드 하나만."""
    if not safe_name(ds):
        return JSONResponse({"error": "잘못된 데이터셋 이름"}, status_code=400)
    df = episodes_df(ds)
    row = df[df["episode_index"] == ep] if not df.empty else df
    if row.empty:
        return JSONResponse({"error": "에피소드 없음"}, status_code=404)
    row = row.iloc[0]
    f = (DATA_ROOT / ds / "data" / f"chunk-{int(row['data/chunk_index']):03d}"
         / f"file-{int(row['data/file_index']):03d}.parquet")
    info = load_json(DATA_ROOT / ds / "meta/info.json", {})
    cols = [c for c in ("episode_index", "frame_index", "timestamp", "observation.state", "action")]
    try:
        d = pd.read_parquet(f, columns=cols, filters=[("episode_index", "==", ep)])
    except Exception:
        d = pd.read_parquet(f)
        d = d[d["episode_index"] == ep]
    d = d.sort_values("frame_index")

    def mat(col):
        if col not in d:
            return []
        return [[round(float(x), 2) for x in v] for v in d[col]]
    t = [round(float(x), 4) for x in d["timestamp"]] if "timestamp" in d else []
    t0 = t[0] if t else 0.0
    return {"fps": float(info.get("fps") or 30), "t": [round(x - t0, 4) for x in t],
            "state_names": _feature_names(info, "observation.state"),
            "action_names": _feature_names(info, "action"),
            "state": mat("observation.state"), "action": mat("action")}


@app.post("/api/rename_dataset/{ds}")
async def api_rename_dataset(ds: str, req: Request):
    b = await req.json()
    new = (b.get("new") or "").strip()
    if not safe_name(ds) or not safe_name(new):
        return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
    src, dst = DATA_ROOT / ds, DATA_ROOT / new
    if not (src / "meta/info.json").exists():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=404)
    if dst.exists():
        return JSONResponse({"error": f"이미 있는 이름: {new}"}, status_code=400)
    j = dataset_busy(ds)
    if j:
        return JSONResponse({"error": f"실행 중인 작업({j['id']})이 이 데이터셋을 사용 중"}, status_code=400)
    await asyncio.to_thread(shutil.move, str(src), str(dst))
    m = load_marks()
    if ds in m:
        m[new] = m.pop(ds)
        save_marks(m)
    meta = load_dsmeta()
    if ds in meta:
        meta[new] = meta.pop(ds)
        save_dsmeta(meta)
    rename_in_projects("datasets", ds, new)
    return {"ok": True, "name": new}


@app.get("/ds/{ds}", response_class=HTMLResponse)
def dataset_page(ds: str):
    if not safe_name(ds):
        return HTMLResponse(f"{CSS}{nav_html('ds')}<div class=wrap>잘못된 데이터셋 이름</div>", 400)
    if not (DATA_ROOT / ds / "meta/info.json").exists():
        return HTMLResponse(f"{CSS}{nav_html('ds')}<div class=wrap>데이터셋 없음: {esc(ds)}</div>", 404)
    views = {sd: (a.get("view") or {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}) for sd, a in ARM_CFGS.items()}
    # 3D 는 데이터셋을 찍은 기종으로 (지금 기종과 다를 수 있음)
    rt = load_json(DATA_ROOT / ds / "meta/info.json", {}).get("robot_type", "")
    k = next((v for v in ROBOT_KINDS.values() if rt in v["types"]["single"] | v["types"]["bi"]), kind())
    k3 = kind3d(k)
    return (CSS + nav_html("ds")
            + f"<script>const DS={js(ds)}, URDF_OK={js((URDF_DIR / k3['urdf'][len('/urdf/'):]).exists())}, "
              f"VIEWS_CFG={js(views)}, K3={js(k3)};</script>"
            + REVIEW_HTML)


@app.get("/ds/{ds}/ep/{ep}")
def episode_page(ds: str, ep: int):
    """예전 링크 호환 — 리뷰 화면의 해당 에피소드로 보냅니다."""
    if not safe_name(ds):
        return HTMLResponse("잘못된 데이터셋 이름", 400)
    return RedirectResponse(f"/ds/{ds}?ep={int(ep)}")


@app.post("/api/mark/{ds}/{ep}")
def api_mark(ds: str, ep: int):
    if not safe_name(ds):
        return JSONResponse({"error": "잘못된 데이터셋 이름"}, status_code=400)
    with META_LOCK:
        m = load_marks()
        lst = set(m.get(ds, []))
        lst.symmetric_difference_update({ep})
        m[ds] = sorted(lst)
        save_marks(m)
    return {"ok": True, "marks": m[ds]}


@app.post("/api/delete/{ds}")
def api_delete(ds: str):
    if not safe_name(ds):
        return JSONResponse({"error": "잘못된 데이터셋 이름"}, status_code=400)
    marks = load_marks().get(ds, [])
    if not marks:
        return JSONResponse({"error": "no marks"}, status_code=400)
    root = DATA_ROOT / ds
    if not root.exists():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=400)
    j = dataset_busy(ds)
    if j:
        return JSONResponse({"error": f"실행 중인 작업({j['id']})이 이 데이터셋을 사용 중"}, status_code=400)
    # --new_root 를 같은 폴더로 줘야 '제자리 편집' 이 됩니다. 안 주면 HF_HOME 설정에 따라
    # ~/.cache 쪽에 결과가 생기고 원본은 그대로 남습니다. 제자리 편집이면 lerobot 이 원본을
    # <ds>_old 로 옮겨 백업합니다 (이미 있으면 lerobot 이 지우고 새로 만듦).
    old = DATA_ROOT / f"{ds}_old"
    if old.exists() and f"{ds}_old" in load_dsmeta():
        return JSONResponse({"error": f"{ds}_old 라는 데이터셋이 따로 있습니다 — lerobot 이 백업하면서 지워버리므로 "
                                      f"먼저 이름을 바꾸세요"}, status_code=400)
    argv = ["lerobot-edit-dataset",
            "--repo_id", f"local/{ds}",
            "--root", str(root),
            "--new_root", str(root),
            "--operation.type", "delete_episodes",
            "--operation.episode_indices", str(sorted(marks))]
    try:
        jid = start_job("delete", argv)
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    m = load_marks()
    m[ds] = []
    save_marks(m)
    return {"ok": True, "job": jid}


# ----------------------------- 페이지: 수집 (Collect) ------------------------
@app.get("/collect", response_class=HTMLResponse)
def collect_page(resume: str = ""):
    rec = next((j for j in jobs_index() if j["kind"] == "record" and j["alive"]), None)
    if rec:
        return (CSS + nav_html("co") + f"<script>const JID={js(rec['id'])};</script>" + COLLECT_RUN_HTML)
    busy = exclusive_busy()
    busywarn = (f'<p class="badge b-warn">실행 중: {esc(busy["id"])} — 끝나야 수집을 시작할 수 있습니다</p>'
                if busy else "") + setup_needed_html()
    pname, pr = active_project()
    mine = set(pr["datasets"]) if pr else set()
    dsets = sorted(list_datasets(), key=lambda d: (d["name"] not in mine, d["name"]))
    resume_opts = "".join(
        f'<option value="{esc(d["name"])}" {"" if robot_type_ok(d["robot_type"]) else "disabled"}>'
        f'{esc(d["name"])} ({esc(d["episodes"])}ep{"" if robot_type_ok(d["robot_type"]) else " · " + esc(d["robot_type"]) + " — 모드 불일치"})'
        f'{" · 다른 프로젝트/미분류" if pname and d["name"] not in mine else ""}</option>'
        for d in dsets)
    task0 = (pr["task"] if pr and pr["task"] else CFG["default_task"])
    name_hint = f"{pname}_v{len(mine) + 1}" if pname else "pick_place_v2"
    if pname:
        projline = (f'<p class=muted>프로젝트 <b class=mono>{esc(pname)}</b> · 환경 <b class=mono>{esc(env_name())}</b>'
                    f' — 새 데이터셋은 이 프로젝트에 자동으로 들어갑니다.</p>')
        if pr["env"] and pr["env"] != env_name():
            projline += (f'<p class="badge b-warn">이 프로젝트의 기준 환경은 {esc(pr["env"])} 인데 지금은 {esc(env_name())} 입니다 — '
                         f'<a href="/setup#envcard">셋업에서 전환</a>하거나 그대로 진행하세요.</p>')
    else:
        projline = (f'<p class=muted>환경 <b class=mono>{esc(env_name())}</b> · 프로젝트 없이 수집합니다 — '
                    f'<a href="/projects">프로젝트</a>를 열면 태스크 설명과 이름이 채워지고 데이터셋이 묶입니다.</p>')
    return f"""{CSS}{nav_html('co')}<div class=wrap>
    <p class=eyebrow>원격조작 녹화 · {esc(kind()["label"])} · {"양팔 " + kind()["cli"]["bi_follower"] if BIMANUAL else "한팔 " + kind()["cli"]["follower"]}</p><h2>수집</h2>
    {busywarn}{projline}
    <div class=card>
    <div class=formgrid>
      <label class=f>모드
        <select id=mode onchange="modeSw()">
          <option value=new>새 데이터셋</option>
          <option value=resume>기존에 이어서</option>
        </select></label>
      <label class=f id=f_new>데이터셋 이름
        <input id=name placeholder="{esc(name_hint)}" value="{esc(name_hint) if pname else ''}" size=22></label>
      <label class=f id=f_resume style="display:none">이어서 수집할 데이터셋
        <select id=resume_ds>{resume_opts}</select></label>
      <label class=f>목표 에피소드 수 <input id=neps value=50 size=5></label>
      <label class=f>에피소드 최대(초) <input id=ept value=30 size=5></label>
      <label class=f style="flex:1;min-width:260px">태스크 설명
        <input id=task value="{esc(task0)}"></label>
      <button class=primary id=bstart onclick="startRec(this)" {'' if ports_configured() and not busy else 'disabled'}>수집 시작</button>
    </div>
    <p class=muted><b>자동으로 녹화되지 않습니다.</b> 시작하면 <b>대기</b> 상태로 들어가고,
    그 화면에서 <b>녹화 시작</b>을 눌러야 그때부터 기록됩니다. 한 에피소드를 끝낼 때마다 다시 대기로 돌아옵니다.<br>
    목표 에피소드 수는 진행률 표시용입니다 — 도달해도 멈추지 않으니 <b>수집 끝내기</b>로 마치세요.
    에피소드 최대(초)가 지나면 자동으로 저장 단계로 넘어갑니다.<br>
    카메라: <span class=mono>{esc(", ".join(CAM_SPECS)) or '없음 — <a href="/setup#cameras">셋업 › 카메라</a>에서 등록'}</span></p>
    </div></div>
    <script>
    const RESUME={js(resume if safe_name(resume) else "")};
    if(RESUME){{
      addEventListener('DOMContentLoaded',()=>{{
        document.getElementById('mode').value='resume'; modeSw();
        const sel=document.getElementById('resume_ds'); sel.value=RESUME;
      }});
    }}
    function modeSw(){{
      const m=document.getElementById('mode').value;
      document.getElementById('f_new').style.display = m==='new'?'':'none';
      document.getElementById('f_resume').style.display = m==='resume'?'':'none';
    }}
    async function startRec(el){{
      // 포트를 실제로 열어 점검하므로 몇 초 걸립니다 — 안 잠그면 먹통으로 보입니다.
      if(el){{ el.disabled=true; el.textContent='포트 점검 중…'; }}
      try{{ await doStart(); }}
      finally{{ if(el){{ el.disabled=false; el.textContent='수집 시작'; }} }}
    }}
    async function doStart(){{
      const b={{mode:document.getElementById('mode').value,
        name:document.getElementById('name').value,
        resume_ds:document.getElementById('resume_ds')?.value||'',
        num_episodes:document.getElementById('neps').value,
        episode_time_s:document.getElementById('ept').value,
        task:document.getElementById('task').value}};
      const r=await fetch('/api/record',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
      const d=await r.json(); if(d.error)alert(d.error); else {{ if(d.warn&&d.warn.length) alert('전원 점검 — 확인하세요\\n\\n'+d.warn.join('\\n')); location.reload(); }}
    }}
    </script>"""


def _preflight_ports():
    """설정된 팔로워/리더 포트를 실제로 열어 모터 응답을 확인합니다.
    블로킹이므로 호출 쪽에서 스레드로 돌릴 것."""
    bad = []
    for side, arm in ARM_CFGS.items():
        for role in ("follower", "leader"):
            port = arm.get(f"{role}_port") or ""
            tag = f"{side}/{role}" if BIMANUAL else role
            if not Path(port).exists():
                bad.append(f"{tag}: {port or '(미지정)'} 가 없습니다 — USB 를 다시 꽂고 셋업 탭에서 재지정")
                continue
            try:
                ids = probe_port(port).get("ids") or []
            except Exception as e:
                bad.append(f"{tag}: {port} 를 열 수 없습니다 ({type(e).__name__}: {e}) — "
                           f"다른 프로그램이 잡고 있거나 dialout 권한 문제")
                continue
            if not ids:
                bad.append(f"{tag}: {port} 에서 모터 응답이 없습니다 — 전원과 케이블을 확인하세요")
    return bad


POWER_MEM = openmimic_health.PowerMemory(PROJ / "openmimic_power.json")


def _power_judge(role, arm_cfg, p):
    """(수준, 문장 목록) — 지난번 전압 기억과 비교하고, 막지 않았으면 이번 값을 기억합니다."""
    key = f"{role}:{arm_cfg.get(f'{role}_id', '')}"
    level, msgs = openmimic_health.judge_power(robot_key(), role, p, POWER_MEM.get(key))
    if level != "block" and p.get("median"):
        try:
            POWER_MEM.put(key, p["median"])
        except OSError:
            pass
    return level, msgs


def power_preflight(roles=("follower", "leader")):
    """시작 전 전원 점검 — (막을 이유 목록, 경고 목록). 포트를 잠깐 열어 서보 전압만 읽습니다 (아무것도 쓰지 않음).
    블로킹이므로 호출 쪽에서 스레드로 돌릴 것."""
    bad, warns = [], []
    for side, arm in ARM_CFGS.items():
        for role in roles:
            port = arm.get(f"{role}_port") or ""
            if not port or not Path(port).exists():
                continue                       # 없는 포트는 _preflight_ports 가 따로 막습니다
            tag = f"{side}/{role}" if BIMANUAL else role
            try:
                if robot_key() == "omx":
                    import tools_dxlcheck as M
                    io = M.BusIO(port, role)
                else:
                    import tools_armcheck as M
                    io = M.BusIO(port)
                try:
                    p = openmimic_health.read_board_power(io.bus)
                finally:
                    io.close()
            except Exception as e:      # noqa: BLE001 — 전압 읽기 실패로 막지는 않습니다 (모터 응답은 따로 점검)
                warns.append(f"{tag}: 전압을 읽지 못했습니다 ({type(e).__name__}) — 어댑터를 눈으로 확인하세요")
                continue
            level, msgs = _power_judge(role, arm, p)
            (bad if level == "block" else warns).extend(f"{tag}: {m}" for m in msgs)
    return bad, warns


@app.post("/api/record")
async def api_record(req: Request):
    busy = exclusive_busy()   # control 포함 — 제어 탭이 팔을 잡고 있으면 차단
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    b = await req.json()
    task = (b.get("task") or CFG["default_task"]).strip()
    neps = _clamp_int(b.get("num_episodes"), 50, 1, 100000)
    ept = _clamp_int(b.get("episode_time_s"), 30, 1, 3600)
    try:
        _need_ports("follower")
        _need_ports("leader")
        if BIMANUAL:
            bimanual_base_id("follower")
            bimanual_base_id("leader")
    except (NotImplementedError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    missing = [f"{side}/{role}" for side, roles in calib_status().items()
               for role, c in roles.items() if not c["ok"]]
    if missing:
        return JSONResponse({"error": "캘리브레이션 없음: " + ", ".join(missing) + " — 캘리브레이션 탭에서 먼저"},
                            status_code=400)
    if not CAM_SPECS:
        return JSONResponse({"error": "카메라가 등록되지 않았습니다 — 셋업 탭에서 추가하세요"}, status_code=400)
    # 워커를 띄우기 전에 포트를 먼저 열어 봅니다. 여기서 안 걸러내면 워커가
    # connect 단계에서 막히고, 화면에는 "준비 중…" 만 남습니다.
    # probe_port 는 시리얼을 실제로 여는 블로킹 호출이라 반드시 스레드로 —
    # async 핸들러에서 그냥 부르면 이벤트 루프가 멈춰 웹 전체가 먹통이 됩니다.
    bad = await asyncio.to_thread(_preflight_ports)
    if bad:
        return JSONResponse({"error": "포트 점검 실패\n\n" + "\n".join(bad)}, status_code=400)
    pbad, pwarn = await asyncio.to_thread(power_preflight)
    if pbad:
        return JSONResponse({"error": "전원 점검 실패 — 시작하지 않았습니다\n\n" + "\n".join(pbad + pwarn)}, status_code=400)
    busy = exclusive_busy()      # 점검하는 몇 초 사이에 Control 등이 포트를 잡았을 수 있습니다
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    if b.get("mode") == "resume":
        ds = (b.get("resume_ds") or "").strip()
        if not safe_name(ds):
            return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
        root = DATA_ROOT / ds
        if not (root / "meta/info.json").exists():
            return JSONResponse({"error": "데이터셋 없음"}, status_code=400)
        rt = load_json(root / "meta/info.json", {}).get("robot_type", "")
        if not robot_type_ok(rt):
            return JSONResponse({"error": f"데이터셋은 {rt} 로 수집됨 — 현재 모드({robot_name()})와 다릅니다"},
                                status_code=400)
        j = dataset_busy(ds)
        if j:
            return JSONResponse({"error": f"실행 중인 작업({j['id']})이 이 데이터셋을 사용 중"}, status_code=400)
        resume, name = True, ds
    else:
        name = (b.get("name") or "").strip()
        if not safe_name(name):
            return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
        if (DATA_ROOT / name).exists():
            return JSONResponse({"error": f"이미 있는 데이터셋: {name} — 다른 이름을 쓰거나 '기존에 이어서' 선택"},
                                status_code=400)
        resume = False
    spec = {"robot": robot_key(), "mode": CFG["mode"], "arms": json.loads(json.dumps(CFG["arms"])),
            "cameras": json.loads(json.dumps(CFG["cameras"])), "fps": int(CFG["fps"]),
            "task": task, "num_episodes": neps, "episode_time_s": ept,
            "repo_id": f"local/{name}", "root": str(DATA_ROOT / name), "resume": resume,
            "streaming_encoding": bool(CFG.get("streaming_encoding", False)),
            "max_relative_target": mrt_value(CFG.get("max_relative_target"))}
    try:
        jid = start_job("record", [sys.executable, str(Path(__file__).resolve()), "--worker", "record", "{jid}"],
                        spec=spec)
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not resume:
        meta = load_dsmeta()
        meta[name] = {"env": env_name(), "created": time.strftime("%Y-%m-%d %H:%M")}
        save_dsmeta(meta)
        pname, _ = active_project()
        if pname:
            assign_to_project("datasets", name, pname)
    return {"ok": True, "job": jid, "warn": pwarn}


@app.get("/api/record_status/{jid}")
def api_record_status(jid: str):
    if not safe_name(jid):
        return JSONResponse({"error": "잘못된 작업 id"}, status_code=400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    return JSONResponse({"status": record_status(jid), "tail": log_tail(jid),
                         "alive": job_alive(j)})


@app.post("/api/sendkey/{jid}/{key}")
def api_sendkey(jid: str, key: str):
    if key not in ("s", "n", "r", "q"):
        return JSONResponse({"error": "허용되지 않은 키"}, status_code=400)
    ok = send_cmd(jid, key)
    return {"ok": ok} if ok else JSONResponse({"error": "키 전달 실패"}, status_code=400)


@app.get("/api/joblog/{jid}")
def api_joblog(jid: str):
    if not safe_name(jid):
        return JSONResponse({"error": "잘못된 작업 id"}, status_code=400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    return JSONResponse({"tail": log_tail(jid), "alive": job_alive(j)})


# ----------------------------- OpenVINO (Intel NPU · GPU · CPU) --------------
# 변환·검증·롤아웃 본체는 openmimic_ov.py 입니다. 여기는 화면과 API 만 둡니다.
OV_PY = Path(__file__).resolve().parent / "openmimic_ov.py"
OV_DEVICES = ("NPU", "GPU", "CPU")
_OV_DEV = {"t": 0.0, "v": None}


def ov_devices(refresh=False):
    """{"ok", "devices": [{id, name}], "error"} — openvino 는 별도 프로세스에서 조회합니다
    (OpenMimic 프로세스가 NPU 를 붙잡고 있지 않게). 2분 캐시."""
    now = time.monotonic()
    if not refresh and _OV_DEV["v"] is not None and now - _OV_DEV["t"] < 120:
        return _OV_DEV["v"]
    try:
        r = subprocess.run([sys.executable, str(OV_PY), "devices"], capture_output=True, text=True, timeout=60)
        v = json.loads((r.stdout.strip().splitlines() or ["{}"])[-1])
        if "ok" not in v:
            v = {"ok": False, "error": (r.stderr.strip().splitlines() or ["조회 실패"])[-1][:200]}
    except Exception as e:       # noqa: BLE001 — 화면은 떠야 합니다
        v = {"ok": False, "error": f"조회 실패: {type(e).__name__}"}
    _OV_DEV.update(t=now, v=v)
    return v


def ov_families():
    """{"NPU": "Intel(R) AI Boost", ...} — 장치 계열별 이름"""
    d = ov_devices()
    out = {}
    for x in d.get("devices", []) if d.get("ok") else []:
        out.setdefault(x["id"].split(".")[0], x["name"])
    return out


def ov_ckpt_ok(rel):
    """요청의 체크포인트 상대경로 → 절대경로 (검증 실패 시 None)"""
    ck = (OUT_ROOT / (rel or "")).resolve()
    if not rel or not _within(ck, OUT_ROOT) or not (ck / "config.json").is_file():
        return None
    return ck


LANG_POLICIES = {"smolvla", "pi0", "pi0_fast", "pi05", "groot", "multi_task_dit"}
# 입력 영상을 정책이 스스로 resize 하는 종류 — 카메라 해상도가 학습 때와 달라도 됩니다
RESIZING_POLICIES = {"smolvla", "pi0", "pi0_fast", "pi05", "groot", "multi_task_dit"}
GPU_POLICIES = {"pi0", "pi0_fast", "pi05", "groot"}


def rollout_rename_map(rel):
    """롤아웃용 카메라 이름 매핑 {내 카메라 키: 정책 카메라 키} 와 설명.
    1) 학습 때 --rename_map 을 썼으면 train_config.json 의 것을 그대로 씁니다.
    2) 정책 카메라 이름이 셋업에 없고 개수가 충분하면(허브에서 받은 모델 등) 전경 → 손목 순으로 연결합니다."""
    ck = OUT_ROOT / rel
    cfg = load_json(ck / "config.json", {})
    pre = "observation.images."
    mine = [pre + c for c in CAM_SPECS]
    tc = load_json(ck / "train_config.json", {})
    rmap = {k: v for k, v in (tc.get("rename_map") or {}).items() if k.startswith(pre) and k in mine}
    if rmap:
        return rmap, ""
    want = [k for k in (cfg.get("image_keys") or []) if k.startswith(pre)] or \
        [k for k, f in (cfg.get("input_features") or {}).items() if k.startswith(pre) and (f or {}).get("type") == "VISUAL"]
    if not want or not mine or all(k in mine for k in want) or len(mine) < len(want):
        return {}, ""
    rmap = dict(zip(cam_order(mine), want))
    note = "카메라 연결: " + ", ".join(f"{a[len(pre):]} → {b[len(pre):]}" for a, b in rmap.items())
    return rmap, note


def policy_needs_task(rel):
    return load_json(OUT_ROOT / rel / "config.json", {}).get("type") in LANG_POLICIES


def policy_fit(rel):
    """체크포인트가 기대하는 입력(카메라 이름·해상도, 관절 수)과 지금 셋업을 대조 — 팔이 움직이기 전에.
    (errors, warnings). 가져온 모델처럼 학습 데이터셋 정보가 없어도 config.json 만으로 판단합니다."""
    cfg = load_json(OUT_ROOT / rel / "config.json", {})
    ptype = cfg.get("type")
    feats = cfg.get("input_features") or {}
    errs, warns = [], []
    pre = "observation.images."
    rmap, note = rollout_rename_map(rel)
    inv = {v: k[len(pre):] for k, v in rmap.items()}          # 정책 카메라 키 → 내 카메라 이름
    if note:
        warns.append(note)
    for key, f in feats.items():
        shape = list(f.get("shape") or [])
        if key.startswith(pre):
            cam = inv.get(key, key[len(pre):])
            spec = CAM_SPECS.get(cam)
            if spec is None:
                errs.append(f"정책이 카메라 '{cam}' 를 씁니다 — 지금 셋업에 없습니다 (있는 것: {', '.join(CAM_SPECS) or '없음'})")
            elif ptype not in RESIZING_POLICIES and len(shape) == 3 and [int(spec["height"]), int(spec["width"])] != shape[1:]:
                warns.append(f"카메라 '{cam}' 해상도 {spec['width']}x{spec['height']} ≠ 학습 {shape[2]}x{shape[1]}")
        elif key == "observation.state" and shape:
            want = len(CTL_JOINTS) * len(SIDES)
            if shape[0] != want:
                errs.append(f"정책의 관절 수 {shape[0]}개 ≠ 지금 구성 {want}개 ({'양팔' if BIMANUAL else '한팔'}) — "
                            "한팔/양팔 또는 기종이 학습 때와 다릅니다")
    if ptype in GPU_POLICIES and not local_cuda().get("ok"):
        warns.append(f"{ptype} 은 CUDA GPU 용입니다 — 이 기기에서 GPU 를 찾지 못해 매우 느리거나 메모리가 부족할 수 있습니다")
    if ptype == "diffusion" and (cfg.get("num_inference_steps") or cfg.get("num_train_timesteps") or 100) > 20 \
            and not (local_cuda().get("ok") or local_cuda().get("xpu")):
        warns.append(f"Diffusion 디노이징 {cfg.get('num_inference_steps') or cfg.get('num_train_timesteps') or 100}회 — GPU 없이 CPU 로는 "
                     "동작 묶음 하나에 수 초~수십 초 걸립니다. 학습 때 '빠른 추론 (DDIM 10회)' 를 켜세요")
    return errs, warns


# 롤아웃 실패 로그 → 사람이 읽을 원인 (위에서부터 처음 맞는 것)
FAILURE_HINTS = [
    (r"Overload|overload", "모터 과부하 — 팔이 막혔거나 너무 무거운 걸 들었습니다. 전원을 껐다 켜고 제어에서 토크를 끈 뒤 확인하세요."),
    (r"motor check failed|Missing motor|missing motors|There is no status packet",
     "모터가 응답하지 않습니다 — 케이블·전원·모터 ID 를 확인하세요 (Setup → 팔 점검)."),
    (r"Permission denied.*tty|PermissionError.*tty", "포트 권한이 없습니다 — dialout 그룹에 추가 후 다시 로그인하세요."),
    (r"could not open port|No such file or directory: '/dev|Failed to open port|SerialException",
     "시리얼 포트를 열지 못했습니다 — 포트 경로(Setup), USB 연결, 다른 프로그램(제어 탭 등)이 잡고 있는지 확인하세요."),
    (r"failed to set capture_(width|height|fps)", "카메라가 그 해상도/fps 를 지원하지 않습니다 — Setup 카메라 설정을 바꾸세요."),
    (r"OpenCVCamera.*(read failed|Timed out|timeout|not connected)|frames? too old|Failed to capture",
     "카메라 프레임을 못 받았습니다 — 카메라 연결, USB 대역폭(허브에 여러 대), 다른 프로그램 점유를 확인하세요."),
    (r"Mismatch between calibration|EOFError", "캘리브레이션 파일과 모터 값이 다릅니다 — 캘리브레이션 탭에서 다시 캘리브레이션하세요."),
    (r"CUDA out of memory|OutOfMemoryError", "GPU 메모리 부족 — 학습 등 다른 GPU 작업을 멈추세요."),
    (r"shape .* ≠ 변환 시", "카메라 해상도·관절 수가 OpenVINO 변환 때와 다릅니다 — 맞추거나 다시 변환하세요."),
    (r"ConnectionError|Connection refused|Tunnel connection failed|URLError",
     "네트워크 접속이 필요했지만 실패했습니다 (모델 구성요소 다운로드 등)."),
]


def failure_hint(text):
    for rx, hint in FAILURE_HINTS:
        if re.search(rx, text or ""):
            return hint
    return ""


def ov_shape_problem(rel):
    """변환 때 고정한 카메라 입력과 지금 셋업의 카메라(이름·해상도)가 맞는지 — 팔이 움직이기 전에 거릅니다.
    NPU 는 정적 shape 라 해상도가 다르면 첫 추론에서 멈춥니다."""
    meta = openmimic_ov.load_meta(OUT_ROOT / rel) or {}
    pre = "observation.images."
    have = {pre + n: [1, 3, int(s["height"]), int(s["width"])] for n, s in CAM_SPECS.items()}
    for i in meta.get("inputs", []):
        if not i["name"].startswith(pre):
            continue
        cam = i["name"][len(pre):]
        if i["name"] not in have:
            return f"카메라 '{cam}' 가 지금 셋업에 없습니다 (학습 데이터에는 있던 카메라)"
        if have[i["name"]] != i["shape"]:
            h, w = have[i["name"]][2:]
            return (f"카메라 '{cam}' 해상도 {w}x{h} ≠ 학습 {i['shape'][3]}x{i['shape'][2]} — "
                    "셋업에서 해상도를 맞추거나 다시 변환하세요")
    return None


def ov_label(rel):
    st = openmimic_ov.status(OUT_ROOT / rel)
    return {"ok": " · OV✓", "stale": " · OV(다시 변환 필요)"}.get(st["state"], "")


def ov_last_job(rel):
    j = next((j for j in jobs_index() if j["kind"] == "ovconvert" and (j.get("spec") or {}).get("ckpt") == rel), None)
    return {"id": j["id"], "alive": j["alive"], "tail": log_tail(j["id"])} if j else None


@app.get("/api/ov/status")
def api_ov_status(ckpt: str = "", refresh: int = 0):
    ck = ov_ckpt_ok(ckpt)
    if ck is None:
        return JSONResponse({"error": "체크포인트 없음"}, status_code=400)
    st = openmimic_ov.status(ck)
    return {"status": st, "devices": ov_devices(bool(refresh)), "job": ov_last_job(ckpt),
            "fps": CFG["fps"], "shape_problem": ov_shape_problem(ckpt) if st["state"] != "none" else None}


@app.post("/api/ov/convert")
async def api_ov_convert(req: Request):
    b = await req.json()
    rel = b.get("ckpt", "")
    ck = ov_ckpt_ok(rel)
    if ck is None:
        return JSONResponse({"error": "체크포인트 없음 — 목록에서 고르세요"}, status_code=400)
    ptype = load_json(ck / "config.json", {}).get("type")
    if ptype not in ("act", "act2"):
        return JSONResponse({"error": f"OpenVINO 변환은 ACT·ACT2 만 지원합니다 (이 체크포인트: {ptype})"}, status_code=400)
    d = ov_devices(refresh=True)
    if not d.get("ok"):
        return JSONResponse({"error": f"OpenVINO 를 쓸 수 없습니다 ({d.get('error')}) — Intel 기기에서 "
                                      "OPENMIMIC_PLATFORM=intel ./lerobot_conda.sh 로 설치하거나 "
                                      "pip install openvino nncf"}, status_code=400)
    # 추론 중엔 NPU·CPU 를 같이 써서 지연 측정이 틀어지고, 같은 IR 을 덮어쓰게 됩니다
    busy = busy_with(("rollout", "ovconvert"))
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 변환하세요"}, status_code=400)
    argv = [sys.executable, str(OV_PY), "convert", f"--ckpt={ck}", f"--fps={CFG['fps']}"]
    if b.get("int8"):
        argv.append("--int8")
    try:
        jid = start_job("ovconvert", argv, spec={"ckpt": rel})
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid}


# 두 화면(Training·Rollout)이 같이 쓰는 결과 표
OV_JS = r"""
function ovTable(d){
  const st=d.status||{}, m=st.meta||{}, res=m.results||{};
  if(st.state==='none') return '<p class=muted>OpenVINO 변환 없음</p>';
  let h='<p class=muted>'+(st.state==='stale'
      ? '<span class="badge b-warn">체크포인트가 변환 후 바뀜 — 다시 변환 필요</span> '
      : '<span class="badge b-ok">변환됨</span> ')
    +'정밀도 '+(st.precisions||[]).join(', ')+' · '+(m.created||'')+' · 검증 입력: '
    +(m.parity_source==='dataset'?'학습 데이터셋 프레임':'임의 입력')
    +' · PyTorch CPU '+(m.torch_cpu_ms??'?')+' ms · 프레임 예산 '+(m.budget_ms??'?')+' ms</p>';
  h+='<table><tr><th>정밀도</th><th>장치</th><th class=num>컴파일 s</th><th class=num>평균 ms</th>'
    +'<th class=num>p95 ms</th><th class=num>최대오차(관절 단위)</th><th>판정</th></tr>';
  for(const p of Object.keys(res)) for(const dev of Object.keys(res[p])){
    const r=res[p][dev];
    if(!r.ok){ h+='<tr><td>'+p+'</td><td>'+dev+'</td><td colspan=4 class=muted>'+ovEsc(r.error||'실패')
      +'</td><td><span class="badge b-bad">실패</span></td></tr>'; continue; }
    const good=r.parity&&r.realtime;
    h+='<tr><td>'+p+'</td><td>'+dev+'</td><td class=num>'+r.compile_s+'</td><td class=num>'+r.mean_ms
      +'</td><td class=num>'+r.p95_ms+'</td><td class=num>'+r.max_abs_units+'</td><td>'
      +'<span class="badge '+(good?'b-ok':'b-warn')+'">'+(good?'OK':(!r.parity?'오차 큼':'예산 초과'))+'</span></td></tr>';
  }
  return h+'</table>'+(d.shape_problem?'<p class="badge b-warn">'+ovEsc(d.shape_problem)+'</p>':'');
}
function ovEsc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
"""


# ----------------------------- 페이지: 학습 ----------------------------------
# 학습 가능한 정책. lerobot e40b58a 의 정책을 그대로 씁니다 (롤아웃도 lerobot-rollout 이 정책 종류를 알아서 처리).
# OpenVINO 변환·NPU 추론은 ACT·ACT2 만 지원합니다. 정책별로 데이터셋에 맞춰 붙는 인자는 policy_train_args() 에서 만듭니다.
#   steps : 화면 기본 step 수   opts : 화면에 보이는 선택지
TRAIN_POLICIES = {
    "act": {"label": "ACT", "args": ["--policy.type=act"], "needs": [], "extra": "", "steps": 80000,
            "opts": ["nobb"],
            "hint": "ACT — 기본. 데모 50개 안팎으로도 잘 배우고 가볍습니다. OpenVINO(NPU) 변환 가능."},
    "act2": {"label": "ACT2 (개선 ACT · 실험)", "args": ["--policy.type=act2"], "needs": [], "extra": "", "steps": 80000,
             "opts": ["aug", "interp", "dino", "flow", "nobb"],
             "hint": "ACT2 — ACT 에 선택지를 붙인 실험 정책 (plugins/lerobot_policy_act2). 아무것도 안 켜면 ACT 와 같은 모델. "
                     "증강·청크 보간은 비용이 거의 없고, DINOv2 백본은 적은 데이터에서 시각 일반화가 좋아지며 백본을 고정해 학습이 가볍습니다. "
                     "flow 헤드는 L1 회귀 대신 flow matching 으로 동작 묶음을 생성합니다(여러 갈래 동작). OpenVINO 변환 가능 (flow 는 실측 전)."},
    "diffusion": {"label": "Diffusion", "args": ["--policy.type=diffusion"], "needs": ["diffusers"], "extra": "diffusion",
                  "pip": ["diffusers>=0.27.2,<0.36.0"], "steps": 80000, "opts": ["ddim", "nobb"],
                  "hint": "Diffusion Policy — 동작이 여러 갈래인 태스크에 강하지만 추론이 느립니다(디노이징 반복). "
                          "카메라 해상도가 모두 같아야 합니다. OpenVINO 변환은 ACT·ACT2 만 됩니다."},
    "smolvla": {"label": "SmolVLA (사전학습 미세조정)", "args": ["--policy.path=lerobot/smolvla_base"],
                "needs": ["transformers", "num2words"], "extra": "smolvla", "steps": 20000,
                "pip": ["transformers>=5.4.0,<5.6.0", "num2words>=0.5.14,<0.6.0", "accelerate>=1.14.0,<2.0.0"],
                "hint": "SmolVLA — 450M 비전-언어-행동 모델을 미세조정합니다. 태스크 설명(언어)을 씁니다. "
                        "처음 한 번 HuggingFace 에서 lerobot/smolvla_base 를 내려받고(인터넷 필요), GPU 메모리를 많이 씁니다. "
                        "롤아웃 때도 SmolVLM 설정을 HuggingFace 캐시에서 읽으므로 처음 한 번은 인터넷이 필요합니다."},
}
TRAIN_OPT_LABELS = {
    "ddim": ("빠른 추론 (DDIM 10회)", "디노이징을 100회 대신 10회만 합니다. GPU 없는 기기에서 롤아웃하려면 켜 두세요."),
    "nobb": ("ImageNet 백본 없이 (오프라인)", "ResNet18 사전학습 가중치를 받지 않고 처음부터 학습합니다. "
                                         "인터넷이 없는 기기용 — 데이터가 적으면 성능이 떨어질 수 있습니다."),
    "aug": ("영상 증강 + 상태 드롭아웃", "학습 때 카메라 영상을 무작위로 조금 밀고(±6%) 밝기·대비를 흔들며, 10% 확률로 관절 상태를 가립니다. "
                                   "카메라 위치·조명이 조금 달라져도 버티게 하고 영상 의존도를 높입니다."),
    "interp": ("청크 경계 보간", "예측 100 스텝 중 앞 30 스텝만 실행하고 다시 예측하며, 새 청크의 앞 10 스텝을 직전 동작에서 선형으로 이어 "
                              "서보가 청크 경계에서 튀지 않게 합니다."),
    "dino": ("DINOv2 백본 (고정)", "ResNet18 대신 DINOv2-small(22M, HuggingFace facebook/dinov2-small) 을 고정 백본으로 씁니다. "
                                "영상은 224x224 로 줄여 넣습니다. 처음 한 번 인터넷이 필요하고(약 90MB), 적은 데이터에서 일반화가 좋아집니다."),
    "flow": ("flow matching 헤드", "L1 회귀 대신 flow matching(5 스텝) 으로 동작 묶음을 생성합니다. 동작이 여러 갈래인 태스크용. "
                                "VAE 는 끕니다. 추론이 조금 느려집니다."),
}
# 롤아웃 때 정책 카메라 이름이 Setup 과 다르면 전경 카메라부터 연결합니다 (rollout_rename_map)
FRONT_CAMS = ("top", "overview", "front", "scene", "side")


def policy_missing(pol):
    """정책에 필요한 파이썬 패키지 중 없는 것 — lerobot 이 학습 시작 수십 초 뒤에야 ImportError 로 죽기 전에 알려 줍니다."""
    import importlib
    import importlib.util
    importlib.invalidate_caches()        # 설치 작업 직후에도 OpenMimic 재시작 없이 보이게
    return [m for m in TRAIN_POLICIES[pol]["needs"] if importlib.util.find_spec(m) is None]


def cam_order(keys):
    """카메라 키 정렬: 전경(top 등) → 나머지(손목). 미세조정형 정책의 '주 시점' 이 앞에 오게."""
    short = lambda k: k.rsplit(".", 1)[-1]
    return sorted(keys, key=lambda k: (0 if short(k) in FRONT_CAMS or short(k).endswith(FRONT_CAMS) else 1, short(k)))


_CUDA = {"t": 0.0, "v": None}


def local_cuda(refresh=False):
    """이 기기 학습 장치 — {"ok"(CUDA), "name", "vram_gb", "xpu"(Intel GPU), "xpu_name"} (torch 를 별도 프로세스에서
    한 번 조회해 10분 캐시). OpenMimic 본체는 torch 를 import 하지 않으므로 하위 프로세스로 묻습니다.
    lerobot-train 은 CUDA → XPU → CPU 순으로 알아서 고릅니다 (Accelerate 자동 감지)."""
    if not refresh and _CUDA["v"] is not None and time.time() - _CUDA["t"] < 600:
        return _CUDA["v"]
    code = ("import json,torch;ok=torch.cuda.is_available();p=torch.cuda.get_device_properties(0) if ok else None;"
            "x=(not ok) and hasattr(torch,'xpu') and torch.xpu.is_available();"
            "print(json.dumps({'ok':ok,'name':p.name if p else '','vram_gb':round(p.total_memory/2**30,1) if p else 0,"
            "'xpu':bool(x),'xpu_name':torch.xpu.get_device_name(0) if x else '','torch':torch.__version__}))")
    try:
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
        v = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:      # noqa: BLE001
        v = {"ok": False, "name": "", "vram_gb": 0, "xpu": False, "xpu_name": "", "error": str(e)[:200]}
    _CUDA.update(t=time.time(), v=v)
    return v


def resnet_cached():
    """torchvision ResNet18 ImageNet 가중치가 이 기기 캐시에 있는지 (ACT·Diffusion 첫 학습 때 받는 파일)."""
    home = os.environ.get("TORCH_HOME") or os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "torch")
    return (Path(home) / "hub" / "checkpoints" / "resnet18-f37072fd.pth").is_file()


def policy_train_args(pol, ds_root, opts=None):
    """정책·데이터셋에 맞춘 lerobot-train 인자 → (args, error, warnings).
    lerobot 이 학습 시작 수십 초~수 분 뒤(모델 다운로드 후)에야 내는 오류를 시작 전에 걸러 냅니다."""
    opts = set(opts or ())
    spec = TRAIN_POLICIES[pol]
    args, warns = list(spec["args"]), []
    info = load_json(Path(ds_root) / "meta" / "info.json", {})
    feats = info.get("features") if isinstance(info.get("features"), dict) else {}
    cams = cam_order([k for k, f in feats.items() if k.startswith("observation.images.")
                      and isinstance(f, dict) and f.get("dtype") in ("video", "image")])
    shapes = {k: tuple(feats[k].get("shape") or ()) for k in cams}
    if not cams:
        return None, "이 데이터셋에 카메라 영상이 없습니다 — 정책 학습에는 카메라가 1대 이상 필요합니다", warns
    if pol == "diffusion" and len(set(shapes.values())) > 1:
        # lerobot DiffusionConfig.validate_features 가 resize 여부와 관계없이 거부합니다
        detail = ", ".join(f"{k.rsplit('.', 1)[-1]} {s[1]}x{s[0]}" for k, s in shapes.items() if len(s) == 3)
        return None, (f"Diffusion 은 카메라 해상도가 모두 같아야 합니다 ({detail}) — 셋업에서 해상도를 맞춰 다시 수집하거나 "
                      "ACT 를 쓰세요"), warns
    if "ddim" in opts and "ddim" in spec.get("opts", ()):
        args += ["--policy.noise_scheduler_type=DDIM", "--policy.num_inference_steps=10"]
    if pol == "act2":
        import importlib.util
        importlib.invalidate_caches()
        if importlib.util.find_spec("lerobot_policy_act2") is None:
            return None, ("ACT2 플러그인이 설치돼 있지 않습니다 — 터미널에서: "
                          "pip install --no-deps -e plugins/lerobot_policy_act2 (lerobot_conda.sh 를 다시 돌려도 됩니다)"), warns
        if "aug" in opts:
            args += ["--policy.aug_shift=0.06", "--policy.aug_color=0.2", "--policy.state_dropout=0.1"]
        if "interp" in opts:
            args += ["--policy.n_action_steps=30", "--policy.interp_steps=10"]
        if "dino" in opts:
            if importlib.util.find_spec("transformers") is None:
                return None, "DINOv2 백본은 transformers 패키지가 필요합니다 — SmolVLA 의 '필요한 패키지 설치' 를 먼저 누르세요", warns
            args += ["--policy.vision_backbone=dinov2_small", "--policy.image_size=224"]
            if "nobb" not in opts:
                warns.append("DINOv2 가중치(facebook/dinov2-small, 약 90MB)를 처음 한 번 HuggingFace 에서 받습니다 — 인터넷 필요")
        if "flow" in opts:
            args += ["--policy.action_head=flow", "--policy.use_vae=false", "--policy.flow_steps=5"]
    if "nobb" in opts and "nobb" in spec.get("opts", ()):
        args.append("--policy.pretrained_backbone_weights=null")
    elif "nobb" in spec.get("opts", ()) and not resnet_cached() and not (pol == "act2" and "dino" in opts):
        warns.append("처음 학습이라 ResNet18 ImageNet 가중치(약 45MB)를 download.pytorch.org 에서 받습니다 — "
                     "인터넷이 없으면 'ImageNet 백본 없이' 를 켜세요")
    return args, "", warns


@app.post("/api/install/{pol}")
def api_install_policy(pol: str):
    """정책 extra 설치 (pip). torch·torchvision 은 지금 버전으로 고정해 CUDA 휠이 CPU 휠로 바뀌지 않게 합니다."""
    if pol not in TRAIN_POLICIES or not TRAIN_POLICIES[pol].get("pip"):
        return JSONResponse({"error": "설치할 것이 없는 정책"}, status_code=400)
    busy = busy_with(("record", "rollout", "train", "install", "ovconvert"))
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 패키지를 바꾸는 동안 돌던 작업이 깨질 수 있어 끝난 뒤 설치하세요"},
                            status_code=400)
    from importlib import metadata
    pins = []
    for pkg in ("torch", "torchvision"):
        try:
            pins.append(f"{pkg}=={metadata.version(pkg).split('+')[0]}")
        except metadata.PackageNotFoundError:
            pass
    cons = PROJ / "openmimic_pip_constraint.txt"
    _atomic_write(cons, "\n".join(pins) + "\n")
    # lerobot[extra] 대신 그 extra 의 패키지만 설치합니다 (lerobot e40b58a pyproject 의 범위 그대로).
    # 'lerobot[...]' 로 설치하면 소스 설치가 아닌 환경에서 PyPI 의 다른 lerobot 버전이 덮어쓸 수 있습니다.
    argv = [sys.executable, "-m", "pip", "install", "-c", str(cons), *TRAIN_POLICIES[pol]["pip"]]
    try:
        jid = start_job("install", argv, spec={"policy": pol})
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid}


@app.get("/train", response_class=HTMLResponse)
def train_page():
    pname, pr = active_project()
    mine = set(pr["datasets"]) if pr else set()
    dsets = sorted(list_datasets(), key=lambda d: (d["name"] not in mine, d["name"]))
    ds_opts = "".join(f'<option value="{esc(d["name"])}">{esc(d["name"])} ({esc(d["episodes"])}ep)'
                      f'{" · 다른 프로젝트/미분류" if pname and d["name"] not in mine else ""}</option>'
                      for d in dsets)
    projline = (f'<p class=muted>프로젝트 <b class=mono>{esc(pname)}</b> — 이 프로젝트의 데이터셋이 위에 오고, '
                f'학습 출력은 이 프로젝트의 모델로 들어갑니다.</p>' if pname else "")
    nods = "" if dsets else need_html("먼저 데이터를 수집하세요", "학습할 데이터셋이 없습니다 — 수집 탭에서 시연을 녹화하거나, "
                                        "데이터셋 탭에서 다른 기기의 데이터셋을 가져오세요.", "/collect", "수집하러 가기")
    running = [j for j in jobs_index() if j["kind"] == "train" and j["alive"]]
    run_html = "".join(
        f'<div class=runbar><span class="badge b-run">running</span> '
        f'<span class=mono>{esc(j["id"])}</span> '
        f'<button class=danger onclick="stopJob({jsattr(j["id"])})">중지</button></div>'
        for j in running)
    return f"""{CSS}{nav_html('tr')}<div class=wrap>
    <p class=eyebrow>정책 학습</p><h2>학습</h2>
    {projline}{run_html}
    {nods}
    <form class="card formgrid trainform" onsubmit="startTrain(event)">
      <label class=f>데이터셋 <select id=ds onchange="polCheck()">{ds_opts}</select></label>
      <label class=f>정책 <select id=policy onchange="polHint()">{''.join(f'<option value="{k}">{esc(v["label"])}{" — 패키지 미설치" if policy_missing(k) else ""}</option>' for k, v in TRAIN_POLICIES.items())}</select></label>
      <label class=f>출력 이름 <input id=name placeholder="비우면 정책_데이터셋" size=22></label>
      <label class=f>step 수 <input id=steps value=80000 size=7></label>
      <label class=f>배치 <input id=batch value=8 size=4></label>
      <label class=f>실행 위치 <select id=target onchange="polCheck()"><option value=local>이 기기</option></select></label>
      <label class=f title="CUDA 에서 메모리·시간 절약. 손실이 튀면 끄세요"><span><input type=checkbox id=amp> AMP</span></label>
      <button class=primary {'' if dsets else 'disabled'}>학습 시작</button>
    </form>
    <p class=muted id=polhint style="margin:-4px 0 6px"></p>
    <div class=row id=polopts style="margin:0 0 6px"></div>
    <p id=polcheck style="margin:0 0 10px"></p>
    <div class="muted mono" id=which style="margin-bottom:8px"></div>
    <p class=muted id=nochart>학습을 시작하면 손실(loss)·기울기·학습률 그래프와 로그가 여기에 나옵니다.</p>
    <div id=charts style="display:none">
    <div class=chartbox><canvas id=chart height=90></canvas></div>
    <div class=chartbox><canvas id=chart2 height=60></canvas></div>
    </div>
    <div id=tailbox style="display:none"><p class=eyebrow>로그</p><pre id=tail>...</pre></div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    const POL={js({k: v["hint"] for k, v in TRAIN_POLICIES.items()})};
    const MISS={js({k: policy_missing(k) for k in TRAIN_POLICIES})};
    const POLOPTS={js({k: v.get("opts", []) for k, v in TRAIN_POLICIES.items()})};
    const POLSTEPS={js({k: v["steps"] for k, v in TRAIN_POLICIES.items()})};
    const OPTLAB={js({k: list(v) for k, v in TRAIN_OPT_LABELS.items()})};
    const OPTDEF={{ddim:true,nobb:false,aug:true,interp:true,dino:false,flow:false}};
    function polOpts(){{ return [...document.querySelectorAll('#polopts input:checked')].map(x=>x.dataset.opt); }}
    let checkSeq=0;
    async function polCheck(){{
      const q=++checkSeq, el=document.getElementById('polcheck');
      const u='/api/train/check?dataset='+encodeURIComponent(document.getElementById('ds').value)
        +'&policy='+document.getElementById('policy').value+'&opts='+polOpts().join(',')
        +'&target='+encodeURIComponent(document.getElementById('target').value);
      let d; try{{ d=await (await fetch(u)).json(); }}catch(e){{ return; }}
      if(q!==checkSeq) return;
      el.innerHTML='';
      if(d.error){{ const b=document.createElement('span'); b.className='badge b-bad'; b.textContent=d.error; el.appendChild(b); }}
      (d.warnings||[]).forEach(w=>{{ const b=document.createElement('span'); b.className='badge b-warn';
        b.style.marginRight='6px'; b.textContent=w; el.appendChild(b); }});
    }}
    async function installPol(){{
      const p=document.getElementById('policy').value;
      if(!confirm(p+' 에 필요한 패키지('+MISS[p].join(', ')+')를 설치합니다. 몇 분 걸릴 수 있습니다. 계속할까요?')) return;
      const r=await fetch('/api/install/'+p,{{method:'POST'}}); const d=await r.json();
      if(d.error) alert(d.error); else location.href='/jobs/'+d.job;
    }}
    (async()=>{{   // HF Jobs 하드웨어(가격 포함) — 오프라인이면 '이 기기' 만
      try{{
        const [f,s]=await Promise.all([fetch('/api/hub/flavors').then(r=>r.json()), fetch('/api/hub/state').then(r=>r.json())]);
        const sel=document.getElementById('target');
        (f.flavors||[]).filter(x=>x.accelerator).forEach(x=>{{
          const o=document.createElement('option'); o.value=x.name; o.disabled=!s.ok;
          o.textContent='HF Jobs · '+x.label+(x.accelerator?' · '+x.accelerator:'')+(x.usd_h!=null?' · $'+x.usd_h.toFixed(2)+'/h':'')+(s.ok?'':' (Hub 로그인 필요)');
          sel.appendChild(o); }});
      }}catch(e){{}}
    }})();
    function polHint(){{
      const p=document.getElementById('policy').value, h=document.getElementById('polhint');
      h.textContent=POL[p]||'';
      document.getElementById('steps').value=POLSTEPS[p]||80000;
      const o=document.getElementById('polopts'); o.innerHTML='';
      (POLOPTS[p]||[]).forEach(k=>{{ const l=document.createElement('label'); l.className='muted'; l.title=OPTLAB[k][1]; l.style.marginRight='16px';
        const c=document.createElement('input'); c.type='checkbox'; c.dataset.opt=k; c.checked=!!OPTDEF[k];
        c.addEventListener('change',polCheck); l.appendChild(c); l.appendChild(document.createTextNode(' '+OPTLAB[k][0])); o.appendChild(l); }});
      polCheck();
      if((MISS[p]||[]).length){{ const b=document.createElement('button'); b.textContent='필요한 패키지 설치 ('+MISS[p].join(', ')+')';
        b.style.marginLeft='8px'; b.onclick=e=>{{ e.preventDefault(); installPol(); }}; h.appendChild(b); }}
    }}
    polHint();
    async function startTrain(e){{
      e.preventDefault();
      const b={{dataset:document.getElementById('ds').value,name:document.getElementById('name').value,
               steps:document.getElementById('steps').value,batch:document.getElementById('batch').value,
               policy:document.getElementById('policy').value,amp:document.getElementById('amp').checked,opts:polOpts(),
               target:document.getElementById('target').value}};
      if(b.target!=='local' && !confirm('HF 작업에서 학습합니다 (유료, 시간당 요금).\\n데이터셋 "'+b.dataset+'" 이 내 계정 비공개 repo 로 먼저 올라갑니다.\\n끝나면 허브 탭에서 모델을 받으세요. 계속할까요?')) return;
      const r=await fetch('/api/train',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
      const d=await r.json();
      if(d.error){{alert(d.error);return;}}
      if(b.name && d.name && d.name!==b.name) alert('출력 폴더가 이미 있어 '+d.name+' 로 저장합니다');
      location.reload();
    }}
    async function stopJob(id){{
      if(!confirm('학습을 중지할까요? (한 번 더 누르면 강제종료)'))return;
      await fetch('/api/kill/'+id,{{method:'POST'}}); setTimeout(()=>location.reload(),1500);
    }}
    let chart, chart2;
    function fmtEta(s){{ const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return h?h+'시간 '+m+'분':m+'분'; }}
    async function refresh(){{
      const r=await fetch('/api/trainlog'); const d=await r.json();
      const p=d.progress;
      document.getElementById('which').textContent=(d.current||'')
        +(p?'  ·  step '+p.step+' / '+p.target+' ('+p.pct+'%)'+(p.eta_s!=null?' · 남은 시간 약 '+fmtEta(p.eta_s):''):'');
      document.getElementById('tail').textContent=d.tail||'';
      const has=(d.points||[]).length>0;
      document.getElementById('charts').style.display=has?'':'none';
      document.getElementById('tailbox').style.display=d.tail?'':'none';
      document.getElementById('nochart').style.display=(has||d.tail)?'none':'';
      if(!has && !chart) return;
      const xs=d.points.map(p=>p[0]),ys=d.points.map(p=>p[1]);
      if(!window.Chart) return;              // CDN 을 못 받으면(오프라인) 그래프만 생략
      if(!chart){{chart=new Chart(document.getElementById('chart'),{{type:'line',
        data:{{labels:xs,datasets:[{{label:'loss',data:ys,borderColor:'#5d9dd6',
          backgroundColor:'rgba(93,157,214,.08)',fill:true,pointRadius:c=>(c.dataset.data.length<20?3:0),borderWidth:1.5}}]}},
        options:{{animation:false,
          scales:{{y:{{type:'logarithmic',grid:{{color:'#28303a'}},ticks:{{color:'#8b98a7',font:{{family:'IBM Plex Mono',size:11}}}}}},
                   x:{{grid:{{display:false}},ticks:{{color:'#5d6a79',font:{{family:'IBM Plex Mono',size:10}},maxTicksLimit:10}}}}}},
          plugins:{{legend:{{display:false}}}}}}}});}}
      else{{chart.data.labels=xs;chart.data.datasets[0].data=ys;chart.update();}}
      // 기울기 크기(grad norm)·학습률 — 손실이 튀거나 정체될 때 원인 확인용
      const ex=d.extra||[], ex_x=ex.map(p=>p[0]), g=ex.map(p=>p[1]), lr=ex.map(p=>p[2]);
      const tick={{color:'#8b98a7',font:{{family:'IBM Plex Mono',size:11}}}};
      if(!chart2){{chart2=new Chart(document.getElementById('chart2'),{{type:'line',
        data:{{labels:ex_x,datasets:[
          {{label:'grad norm',data:g,borderColor:'#e07a3f',pointRadius:c=>(c.dataset.data.length<20?3:0),borderWidth:1.2,yAxisID:'y'}},
          {{label:'lr',data:lr,borderColor:'#6cc070',pointRadius:c=>(c.dataset.data.length<20?3:0),borderWidth:1.2,yAxisID:'y1'}}]}},
        options:{{animation:false,
          scales:{{y:{{grid:{{color:'#28303a'}},ticks:tick,title:{{display:true,text:'grad norm',color:'#8b98a7'}}}},
                   y1:{{position:'right',grid:{{display:false}},ticks:{{...tick,callback:v=>Number(v).toExponential(1)}},title:{{display:true,text:'lr',color:'#8b98a7'}}}},
                   x:{{grid:{{display:false}},ticks:{{color:'#5d6a79',font:{{family:'IBM Plex Mono',size:10}},maxTicksLimit:10}}}}}},
          plugins:{{legend:{{labels:{{color:'#8b98a7'}}}}}}}}}});}}
      else{{chart2.data.labels=ex_x;chart2.data.datasets[0].data=g;chart2.data.datasets[1].data=lr;chart2.update();}}
    }}
    refresh(); setInterval(refresh,5000);
    </script>"""


@app.get("/api/train/check")
def api_train_check(dataset: str = "", policy: str = "act", opts: str = "", target: str = "local"):
    """학습 시작 전 점검 — 화면에서 데이터셋·정책·선택지를 바꿀 때마다 부릅니다."""
    if policy not in TRAIN_POLICIES or not safe_name(dataset) or not (DATA_ROOT / dataset / "meta/info.json").is_file():
        return {"error": "", "warnings": []}
    _, err, warns = policy_train_args(policy, DATA_ROOT / dataset, [o for o in opts.split(",") if o])
    if not err and target == "local":
        cu = local_cuda()
        if cu.get("ok"):
            warns.append(f"GPU: {cu.get('name')} · {cu.get('vram_gb')} GiB")
        elif cu.get("xpu"):
            warns.append(f"Intel GPU(XPU) {cu.get('xpu_name')} 로 학습합니다 — 실험적 기능입니다 (실기 검증 전). "
                         "느리거나 실패하면 실행 위치를 HF 작업으로 고르세요")
        else:
            warns.append("이 기기에는 학습 가속 장치가 없어 CPU 로 학습합니다 — 매우 느립니다. 실행 위치를 HF 작업으로 고르세요")
    return {"error": err, "warnings": warns}


@app.post("/api/train")
async def api_train(req: Request):
    b = await req.json()
    target = b.get("target") or "local"
    if target != "local":
        return await asyncio.get_running_loop().run_in_executor(None, _cloud_train, b, target)
    busy = gpu_or_loop_busy()   # Control 수동 제어는 학습과 동시 가능
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    ds = (b.get("dataset") or "").strip()
    if not safe_name(ds):
        return JSONResponse({"error": "데이터셋 이름은 영문/숫자/._- 만"}, status_code=400)
    pol = b.get("policy") or "act"
    if pol not in TRAIN_POLICIES:
        return JSONResponse({"error": f"정책은 {', '.join(TRAIN_POLICIES)} 중 하나"}, status_code=400)
    name = (b.get("name") or f"{pol}_{ds}").strip()
    if not safe_name(name):
        return JSONResponse({"error": "출력 이름은 영문/숫자/._- 만"}, status_code=400)
    steps = _clamp_int(b.get("steps"), TRAIN_POLICIES[pol]["steps"], 1, 100000000)
    batch = _clamp_int(b.get("batch"), 8, 1, 4096)
    miss = policy_missing(pol)
    if miss:
        return JSONResponse({"error": f"{TRAIN_POLICIES[pol]['label']} 에 필요한 패키지가 없습니다 ({', '.join(miss)}) — 터미널에서: "
                                      f"cd ~/project/openmimic/lerobot-src && pip install -e \".[{TRAIN_POLICIES[pol]['extra']}]\""},
                            status_code=400)
    root = DATA_ROOT / ds
    if not root.exists():
        return JSONResponse({"error": "dataset not found"}, status_code=400)
    pargs, perr, _ = policy_train_args(pol, root, b.get("opts"))
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    # lerobot 는 output_dir 가 이미 있으면 FileExistsError 로 즉사합니다.
    # 같은 데이터셋으로 두 번째 학습을 돌리는 건 흔한 일이라 이름을 자동으로 비켜 줍니다.
    out = OUT_ROOT / name
    if out.exists():
        for i in range(2, 1000):
            cand = OUT_ROOT / f"{name}_{i}"
            if not cand.exists():
                out, name = cand, cand.name
                break
        else:
            return JSONResponse({"error": f"{name}_2 ~ _999 가 모두 존재합니다 — 이름을 바꾸세요"},
                                status_code=400)
    argv = [sys.executable, "-m", "lerobot.scripts.lerobot_train",
            f"--dataset.repo_id=local/{ds}", f"--dataset.root={root}",
            *pargs, f"--output_dir={out}",
            f"--steps={steps}", f"--batch_size={batch}", "--num_workers=4",
            "--save_freq=10000", "--log_freq=50", "--policy.push_to_hub=false"]   # log_freq: 그래프 점 간격 (기본 200)
    if b.get("amp"):
        argv.append("--policy.use_amp=true")
    try:
        jid = start_job("train", argv)
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    pname, _ = active_project()
    if pname:
        assign_to_project("models", name, pname)
    return {"ok": True, "job": jid, "name": name}


def _cloud_train(b, flavor):
    """HF 작업으로 학습. 로컬 데이터셋은 openmimic_hub.py 가 내 계정 비공개 repo 로 먼저 올립니다.
    이 기기 GPU 를 쓰지 않으므로 수집·추론·로컬 학습과 동시에 돌 수 있습니다."""
    ds = (b.get("dataset") or "").strip()
    if not safe_name(ds) or not (DATA_ROOT / ds / "meta/info.json").is_file():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=400)
    pol = b.get("policy") or "act"
    if pol not in TRAIN_POLICIES:
        return JSONResponse({"error": f"정책은 {', '.join(TRAIN_POLICIES)} 중 하나"}, status_code=400)
    pargs, perr, _ = policy_train_args(pol, DATA_ROOT / ds, b.get("opts"))
    if perr:
        return JSONResponse({"error": perr}, status_code=400)
    st = _hub().status(refresh=True)
    if not st.get("ok"):
        return JSONResponse({"error": "HF Jobs 는 Hugging Face 로그인이 필요합니다 — 허브 탭에서 토큰을 넣으세요"},
                            status_code=400)
    try:
        names = {f["name"] for f in _hub().flavors()}
    except Exception as e:     # noqa: BLE001
        return JSONResponse({"error": f"HF Jobs 하드웨어 목록을 못 받았습니다: {e}"}, status_code=400)
    if flavor not in names:
        return JSONResponse({"error": f"알 수 없는 하드웨어: {flavor}"}, status_code=400)
    busy = dataset_busy(ds)
    if busy:
        return JSONResponse({"error": f"{busy['id']} 가 이 데이터셋을 쓰는 중"}, status_code=400)
    steps = _clamp_int(b.get("steps"), TRAIN_POLICIES[pol]["steps"], 1, 100000000)
    batch = _clamp_int(b.get("batch"), 8, 1, 4096)
    name = (b.get("name") or "").strip()
    argv = [sys.executable, str(HUB_PY), "cloud-train", f"--root={DATA_ROOT / ds}", f"--name={ds}",
            f"--flavor={flavor}", "--", *pargs, f"--steps={steps}", f"--batch_size={batch}",
            "--num_workers=4", "--save_freq=10000", "--log_freq=50"]
    if safe_name(name):
        argv.append(f"--job_name={name}")          # Hub 모델 repo 이름의 앞부분
    if b.get("amp"):
        argv.append("--policy.use_amp=true")
    try:
        jid = start_job("cloudtrain", argv, spec={"runner": "hf", "flavor": flavor, "dataset": ds, "policy": pol})
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid, "cloud": True}


_BIG_SUFFIX = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def _parse_step(tok):
    """lerobot 로그의 step 은 format_big_number 출력 — '80K', '1M', '900' 등."""
    m = re.fullmatch(r"([\d.]+)([KMB]?)", tok)
    if not m:
        return None
    try:
        return int(float(m.group(1)) * _BIG_SUFFIX[m.group(2)])
    except (ValueError, KeyError):
        return None


@app.get("/api/trainlog")
def api_trainlog():
    trains = [j for j in jobs_index() if j["kind"] in ("train", "cloudtrain")]
    if not trains:
        return JSONResponse({"points": [], "tail": "", "current": None})
    j = next((t for t in trains if t["alive"]), trains[0])
    pts, extra, sps = [], [], None
    try:
        with open(j["log"], errors="ignore") as f:
            for line in f:
                m = re.search(r"step:(\S+)\s.*?loss:([\d.]+)", line)
                if m:
                    step = _parse_step(m.group(1))
                    if step is None:
                        continue
                    try:
                        pts.append([step, float(m.group(2))])
                    except ValueError:
                        continue
                    g = re.search(r"grdn:([\d.eE+-]+)", line)
                    lr = re.search(r"\blr:([\d.eE+-]+)", line)
                    sp = re.search(r"smp/s:([\d.]+)", line)
                    try:
                        extra.append([step, float(g.group(1)) if g else None, float(lr.group(1)) if lr else None])
                        sps = float(sp.group(1)) if sp else sps
                    except ValueError:
                        pass
    except Exception:
        pass
    status = "running" if job_alive(j) else "finished"
    # 진행률·남은 시간: --steps 목표와 마지막 처리 속도(samples/s ÷ batch)
    args = dict(a.split("=", 1) for a in (j.get("argv") or []) if a.startswith("--") and "=" in a)
    target = _clamp_int(args.get("--steps"), 0, 0, 10 ** 9)
    batch = _clamp_int(args.get("--batch_size"), 0, 0, 10 ** 6)
    prog = None
    tail = log_tail(j["id"])
    # lerobot 은 step 로그를 200 step 마다 남깁니다. 그 사이에는 진행 막대(12/2000 [..<2:08:55, ..])로 진행률·남은 시간을 봅니다
    bar = None
    for bm in re.finditer(r"(\d+)/(\d+) \[[^\]<]*<(?:(\d+):)?(\d+):(\d+)", tail):
        bar = bm
    if bar and status == "running" and (not pts or int(bar.group(1)) > pts[-1][0]):
        step, tot = int(bar.group(1)), int(bar.group(2))
        h, mi, se = int(bar.group(3) or 0), int(bar.group(4)), int(bar.group(5))
        prog = {"step": step, "target": tot, "pct": round(100 * step / tot, 1) if tot else 0, "eta_s": h * 3600 + mi * 60 + se}
    elif target and pts:
        prog = {"step": pts[-1][0], "target": target, "pct": round(100 * pts[-1][0] / target, 1)}
        if sps and batch and status == "running":
            prog["eta_s"] = int(max(0, target - pts[-1][0]) / (sps / batch))
    return JSONResponse({"points": pts[-2000:], "extra": extra[-2000:], "progress": prog,
                         "tail": tail, "current": f'{j["id"]} [{status}]'})


# ----------------------------- 모델 (모델 탭) · 내보내기/가져오기 · 이어서 학습 ---------
# outputs/<run>/checkpoints/<step>/pretrained_model 하나가 '모델' 하나입니다.
# 학습 기기(Thor)에서 내보낸 tar 를 추론 기기(Intel)에서 그대로 가져오는 흐름을 염두에 뒀습니다.
IMPORT_DISK_SHARE = 0.45         # 업로드 파일 + 풀린 내용이 같이 들어가야 하므로 여유 공간의 절반 이하만


def _train_jobs_for_run(run):
    """outputs/<run> 에 쓰는 train 작업들 (최신 먼저) — 처음 학습과 이어서 학습 모두"""
    rp = os.path.realpath(OUT_ROOT / run)
    out = []
    for j in jobs_index():
        if j["kind"] != "train":
            continue
        if any(a.startswith("--output_dir=") and os.path.realpath(a.split("=", 1)[1]) == rp
               for a in j.get("argv") or []):
            out.append(j)
    return out


def _log_last_metrics(log, nbytes=65536):
    """로그 끝부분에서 마지막 (step, loss). 수 MB 짜리 로그를 다 읽지 않습니다."""
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - nbytes))
            txt = f.read().decode(errors="ignore")
    except OSError:
        return None, None
    last = None
    for last in re.finditer(r"step:(\S+)\s.*?loss:([\d.]+)", txt):
        pass
    if not last:
        return None, None
    try:
        return _parse_step(last.group(1)), float(last.group(2))
    except ValueError:
        return None, None


def _fmt_dur(sec):
    sec = int(sec or 0)
    if sec < 60:
        return "1분 미만"
    h, m = sec // 3600, sec % 3600 // 60
    return f"{h}시간 {m}분" if h else f"{m}분"


def run_info(run, trials=None):
    rd = OUT_ROOT / run
    trials = trials if trials is not None else load_trials()
    cks = []
    cdir = rd / "checkpoints"
    for st in sorted(cdir.iterdir()) if cdir.is_dir() else []:
        pm = st / "pretrained_model"
        if st.name == "last" or st.is_symlink() or not (pm / "config.json").is_file():
            continue
        rel = f"{run}/checkpoints/{st.name}/pretrained_model"
        cks.append({"step": st.name, "rel": rel, "ov": openmimic_ov.status(OUT_ROOT / rel)["state"],
                    "trials": trial_summary(rel, trials), "robot_type": checkpoint_robot_type(rel)})
    tc = load_json(OUT_ROOT / cks[-1]["rel"] / "train_config.json", {}) if cks else {}
    pol = (tc.get("policy") or {}).get("type") or load_json(OUT_ROOT / cks[-1]["rel"] / "config.json", {}).get(
        "type", "?") if cks else "?"
    jobs = _train_jobs_for_run(run)
    alive = any(j["alive"] for j in jobs)
    step = loss = None
    dur = 0.0
    for j in reversed(jobs):                 # 처음 학습 → 이어서 학습 순서로 시간 합산
        try:
            t0 = time.mktime(time.strptime(j["started"], "%Y-%m-%d %H:%M:%S"))
            t1 = time.time() if j["alive"] else os.path.getmtime(j["log"])
            dur += max(0.0, t1 - t0)
        except (KeyError, ValueError, OSError):
            pass
    if jobs:
        step, loss = _log_last_metrics(jobs[0]["log"])
    last_state = rd / "checkpoints" / "last" / "training_state"
    done_step = load_json(last_state / "training_step.json", {}).get("step")
    if done_step is None and cks and cks[-1]["step"].isdigit():
        done_step = int(cks[-1]["step"])          # last 링크가 없으면(가져온 모델 등) 마지막 체크포인트 이름으로
    return {"run": run, "policy": pol, "dataset": ((tc.get("dataset") or {}).get("repo_id") or "").split("/", 1)[-1],
            "steps": tc.get("steps"), "batch": tc.get("batch_size"), "checkpoints": cks, "alive": alive,
            "step": step, "loss": loss, "duration": _fmt_dur(dur) if dur else "", "done_step": done_step,
            "resumable": last_state.is_dir() and not alive,
            "imported": load_json(rd / "openmimic_import.json", {}).get("imported", ""),
            "job": jobs[0]["id"] if jobs else ""}


def list_runs():
    return sorted((d.name for d in OUT_ROOT.iterdir()
                   if d.is_dir() and not d.name.startswith(".") and (d / "checkpoints").is_dir()),
                  key=lambda r: -(OUT_ROOT / r).stat().st_mtime) if OUT_ROOT.exists() else []


@app.get("/models", response_class=HTMLResponse)
def models_page():
    pname, pr = active_project()
    mine = set(pr["models"]) if pr else set()
    trials = load_trials()
    runs = sorted(list_runs(), key=lambda r: r not in mine)     # 안정 정렬 — 최근 순서는 유지
    cards = ""
    for r in runs:
        ri = run_info(r, trials)
        badges = (f'<span class=badge>{esc(ri["policy"])}</span> '
                  + ('<span class="badge b-run">학습 중</span> ' if ri["alive"] else '')
                  + (f'<span class=badge title="{esc(ri["imported"])}">가져옴</span> ' if ri["imported"] else '')
                  + ('<span class="badge b-run">이 프로젝트</span> ' if r in mine else ''))
        meta = " · ".join(x for x in [
            f'데이터셋 <span class=mono>{esc(ri["dataset"])}</span>' if ri["dataset"] else "",
            f'step {esc(ri["done_step"] or ri["step"] or "?")} / {esc(ri["steps"] or "?")}',
            f'batch {esc(ri["batch"])}' if ri["batch"] else "",
            f'마지막 loss <span class=mono>{ri["loss"]:.4f}</span>' if ri["loss"] is not None else "",
            f'학습 시간 {esc(ri["duration"])}' if ri["duration"] else ""] if x)
        rows = ""
        for c in reversed(ri["checkpoints"]):
            t = c["trials"]
            ov = {"ok": '<span class="badge b-ok">OV✓</span>', "stale": '<span class="badge b-warn">OV 다시 변환</span>'}.get(c["ov"], '<span class=muted>-</span>')
            bad = not robot_type_ok(c["robot_type"])
            rate = f'{t["rate"]}% ({t["ok"]}/{t["n"]})' if t["n"] else "-"
            rows += (f'<tr><td class=mono>{esc(c["step"])}</td><td>{ov}</td>'
                     f'<td class=num>{rate}</td>'
                     f'<td style="text-align:right;white-space:nowrap">'
                     + (f'<span class="badge b-warn">{esc(c["robot_type"])} — 모드 불일치</span> ' if bad else
                        f'<a class=btnlink href="/rollout?ckpt={esc(c["rel"])}">실행</a> ')
                     + (f'<button onclick="ovPick({jsattr(c["rel"])})">OpenVINO 변환</button> ' if not bad else '')
                     + f'<a class=btnlink href="/api/model/download?ckpt={esc(c["rel"])}" download>내보내기</a> '
                     f'<button class=danger onclick="delCk({jsattr(c["rel"])},\'step\')">삭제</button></td></tr>')
        acts = ""
        if ri["resumable"]:
            acts += (f'<button onclick="resume({jsattr(r)},{jsattr(ri["done_step"] or 0)},{jsattr(ri["steps"] or 0)})">'
                     f'이어서 학습</button> ')
        if ri["alive"]:
            acts += '<a class=btnlink href="/train">학습 화면</a> '
        acts += f'<button class=danger onclick="delCk({jsattr(ri["checkpoints"][-1]["rel"] if ri["checkpoints"] else r + "/checkpoints/x")},\'run\',{jsattr(r)})">전체 삭제</button>'
        cards += (f'<div class=card style="margin-bottom:12px"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
                  f'<b class=mono style="font-size:15px">{esc(r)}</b> {badges}<span style="flex:1"></span>{acts}</div>'
                  f'<p class=muted style="margin:6px 0 8px">{meta}</p>'
                  f'<table><tr><th>체크포인트</th><th>OpenVINO</th><th class=num>실기 성공률</th><th></th></tr>{rows}</table></div>')
    empty = '' if runs else '<p class=muted>학습된 모델이 없습니다 — 학습 탭에서 학습하거나, 다른 기기에서 내보낸 모델을 가져오세요.</p>'
    pmodels = mine
    ov_cks = sorted(list_checkpoints(), key=lambda c: c.split("/checkpoints/")[0] not in pmodels)
    ov_opts = "".join(f'<option value="{esc(c)}">{esc(c)}{esc(ov_label(c))}</option>' for c in ov_cks)
    return f"""{CSS}{nav_html('md')}<div class=wrap>
    <p class=eyebrow>학습한 모델</p><h2>모델</h2>
    <div class=card style="margin-bottom:14px">
      <div class=formgrid>
        <label class=f style="min-width:320px">모델 가져오기 (.tar / .zip)
          <input type=file id=upf accept=".tar,.gz,.tgz,.zip"></label>
        <button class=primary onclick="upload('model')">가져오기</button>
        <span class=muted id=upmsg></span>
      </div>
      <p class=muted style="margin:6px 0 0">다른 기기의 <b>내보내기</b> 파일(예: Intel NPU 기기에서 추론할 모델을 NVIDIA Thor 에서 학습한 경우)을 그대로 넣으면
      같은 폴더 구조로 들어옵니다. OpenVINO 변환본이 들어 있으면 같이 옵니다.</p>
    </div>
    {empty}{cards}
    <p class=eyebrow style="margin-top:22px" id=ov>OpenVINO 변환 · Intel NPU / GPU / CPU</p>
    <div class=card>
      <p class=muted>학습이 끝난 체크포인트를 OpenVINO 로 바꾸고, 이 기기의 장치마다 PyTorch 결과와의 오차·추론 시간을 잽니다.
      변환 결과는 체크포인트 폴더 안 <span class=mono>openvino/</span> 에 저장되고, 실행 탭에서 추론 엔진으로 고를 수 있습니다.
      카메라 해상도는 학습 데이터 기준으로 고정됩니다.</p>
      <div class=formgrid>
        <label class=f style="min-width:380px">체크포인트
          <select id=ovck onchange="ovRefresh()">{ov_opts}</select></label>
        <label class=f><span><input type=checkbox id=ovint8> INT8 도 만들기 (학습 데이터셋으로 보정)</span></label>
        <button class=primary onclick="ovConvert()" {'disabled' if not ov_cks else ''}>OpenVINO 변환</button>
      </div>
      {'' if ov_cks else '<p class=muted>체크포인트가 없습니다 — 학습을 먼저 완료하세요</p>'}
      <div id=ovres style="margin-top:10px"></div>
      <pre id=ovtail style="display:none;margin-top:10px"></pre>
    </div></div>
    <script>
    {UPLOAD_JS}
    {OV_JS}
    let ovTimer=null;
    async function ovRefresh(){{
      const ck=document.getElementById('ovck').value; if(!ck) return;
      const r=await fetch('/api/ov/status?ckpt='+encodeURIComponent(ck)); const d=await r.json();
      if(d.error){{document.getElementById('ovres').textContent=d.error;return;}}
      const dev=d.devices||{{}};
      const devline='<p class=muted>이 기기 OpenVINO 장치: '+(dev.ok?(dev.devices||[]).map(x=>ovEsc(x.id+' ('+x.name+')')).join(', ')||'없음'
        :'<span class="badge b-warn">'+ovEsc(dev.error||'사용 불가')+'</span>')+'</p>';
      document.getElementById('ovres').innerHTML=devline+ovTable(d);
      const t=document.getElementById('ovtail');
      if(d.job){{t.style.display='';t.textContent=(d.job.alive?'[변환 중 '+d.job.id+']\\n':'['+d.job.id+']\\n')+(d.job.tail||'');}}
      else t.style.display='none';
      clearTimeout(ovTimer); if(d.job&&d.job.alive) ovTimer=setTimeout(ovRefresh,2500);
    }}
    async function ovConvert(){{
      const b={{ckpt:document.getElementById('ovck').value,int8:document.getElementById('ovint8').checked}};
      const r=await fetch('/api/ov/convert',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
      const d=await r.json(); if(d.error){{alert(d.error);return;}}
      setTimeout(ovRefresh,800);
    }}
    ovRefresh();
    function ovPick(rel){{ const s=document.getElementById('ovck'); s.value=rel; ovRefresh();
      document.getElementById('ov').scrollIntoView({{behavior:'smooth'}}); }}
    async function delCk(rel,scope,run){{
      if(scope==='run'){{ const t=prompt('모델 "'+run+'" 을 통째로 삭제합니다 (복구 불가).\\n확인을 위해 이름을 그대로 입력하세요:'); if(t!==run) return; }}
      else if(!confirm('체크포인트 삭제:\\n'+rel.replace('/pretrained_model','')+'\\n삭제할까요?')) return;
      const r=await fetch('/api/delete_checkpoint',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{rel:rel,scope:scope}})}});
      const d=await r.json(); if(d.error) alert(d.error); else location.reload();
    }}
    async function resume(run,done,target){{
      const v=prompt('"'+run+'" 를 step '+done+' 에서 이어서 학습합니다.\\n목표 step (지금 '+target+'):', String(Math.max(target, done*2)));
      if(!v) return;
      const r=await fetch('/api/train/resume',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{run:run,steps:v}})}});
      const d=await r.json(); if(d.error) alert(d.error); else location.href='/train';
    }}
    </script>"""


# 업로드 공용 (Models·Datasets): 원본 바이트를 그대로 POST — python-multipart 의존성 없이 스트리밍
UPLOAD_JS = r"""
function upload(what){
  const f=document.getElementById('upf').files[0], msg=document.getElementById('upmsg');
  if(!f){ alert('파일을 고르세요'); return; }
  const x=new XMLHttpRequest();
  x.open('POST','/api/import/'+what+'?filename='+encodeURIComponent(f.name));
  x.setRequestHeader('Content-Type','application/octet-stream');
  x.upload.onprogress=e=>{ if(e.lengthComputable) msg.textContent='올리는 중 '+Math.round(100*e.loaded/e.total)+'%'; };
  x.onload=()=>{ let d={}; try{ d=JSON.parse(x.responseText); }catch(e){}
    if(d.error){ msg.textContent=''; alert(d.error); return; }
    msg.textContent='완료: '+(d.items||[]).join(', '); setTimeout(()=>location.reload(),900); };
  x.onerror=()=>{ msg.textContent=''; alert('업로드 실패 (연결 끊김)'); };
  msg.textContent='올리는 중…'; x.send(f);
}
"""


@app.get("/api/model/download")
def api_model_download(ckpt: str = ""):
    ck = ov_ckpt_ok(ckpt)
    if ck is None:
        return JSONResponse({"error": "체크포인트 없음"}, status_code=400)
    # 받는 쪽에서 같은 폴더 구조(<run>/checkpoints/<step>/pretrained_model)로 풀리도록 tar 안 경로를 맞춥니다.
    # OpenVINO 컴파일 캐시는 장치별이라 뺍니다.
    run, _, rest = ckpt.partition("/checkpoints/")
    step = rest.split("/")[0]
    return StreamingResponse(_tar_stream(ck, ckpt, skip=("openvino/cache",)), media_type="application/x-tar",
                             headers={"Content-Disposition": f'attachment; filename="{run}_{step}.tar"',
                                      "Cache-Control": "no-store"})


def _safe_extract(arc, out):
    """tar/zip 을 out 에 풉니다. 절대경로·'..'·링크·장치 파일은 거부하고, 풀린 총량이 디스크 여유를 넘으면 거부."""
    free = shutil.disk_usage(out).free
    total = 0
    if tarfile.is_tarfile(arc):
        with tarfile.open(arc, "r:*") as t:
            for m in t.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    raise ValueError(f"위험한 경로: {m.name}")
                if not (m.isfile() or m.isdir()):
                    raise ValueError(f"링크·특수 파일은 받지 않습니다: {m.name}")
                total += m.size
            if total > free * 0.9:
                raise ValueError("풀린 크기가 디스크 여유 공간보다 큽니다")
            t.extractall(out, filter="data")
    elif zipfile.is_zipfile(arc):
        with zipfile.ZipFile(arc) as z:
            for i in z.infolist():
                n = i.filename
                if n.startswith("/") or "\\" in n or ".." in Path(n).parts:
                    raise ValueError(f"위험한 경로: {n}")
                if (i.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError(f"링크는 받지 않습니다: {n}")
                total += i.file_size
            if total > free * 0.9:
                raise ValueError("풀린 크기가 디스크 여유 공간보다 큽니다")
            z.extractall(out)
    else:
        raise ValueError("tar 또는 zip 파일이 아닙니다")


def _clean_name(s, fallback):
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s or "").strip("._-")[:80]
    return s if safe_name(s) else fallback


def _free_name(root, name):
    if not (root / name).exists():
        return name
    for i in range(2, 1000):
        if not (root / f"{name}_{i}").exists():
            return f"{name}_{i}"
    raise ValueError(f"{name}_2 ~ _999 가 모두 존재합니다")


def _place_models(tmp, stem, filename):
    found = sorted({p.parent for p in tmp.rglob("model.safetensors") if (p.parent / "config.json").is_file()})
    if not found:
        raise ValueError("모델이 없습니다 — pretrained_model 폴더(config.json + model.safetensors)가 들어 있어야 합니다")
    runs, placed = {}, []
    for pm in found:
        parts = pm.relative_to(tmp).parts
        if len(parts) >= 4 and parts[-3] == "checkpoints":           # <run>/checkpoints/<step>/pretrained_model
            orig_run, step = parts[-4], parts[-2]
        else:
            orig_run, step = stem, ("imported" if len(found) == 1 else _clean_name(pm.name, "imported"))
        orig_run, step = _clean_name(orig_run, stem), _clean_name(step, "imported")
        if orig_run not in runs:
            runs[orig_run] = _free_name(OUT_ROOT, orig_run)
            (OUT_ROOT / runs[orig_run] / "checkpoints").mkdir(parents=True)
            save_json(OUT_ROOT / runs[orig_run] / "openmimic_import.json",
                      {"imported": time.strftime("%F %T"), "from": filename, "original_name": orig_run})
        dest = OUT_ROOT / runs[orig_run] / "checkpoints" / step
        if dest.exists():
            raise ValueError(f"같은 체크포인트가 두 번 들어 있습니다: {orig_run}/{step}")
        dest.mkdir(parents=True)
        shutil.move(str(pm), str(dest / "pretrained_model"))
        if pm.name == "pretrained_model" and (pm.parent / "training_state").is_dir():
            shutil.move(str(pm.parent / "training_state"), str(dest / "training_state"))
        # 묶음 안의 OpenVINO 변환본은 같은 가중치에서 나온 것으로 봅니다 (크기가 같을 때만).
        # 압축을 풀면 파일 시각이 바뀌어 '다시 변환 필요' 로 보이는 것을 막습니다.
        mp = dest / "pretrained_model" / openmimic_ov.OV_SUBDIR / openmimic_ov.META
        meta = load_json(mp, None)
        if meta and (meta.get("source") or {}).get("size") == (dest / "pretrained_model" / "model.safetensors").stat().st_size:
            meta["source"] = openmimic_ov.fingerprint(dest / "pretrained_model")
            save_json(mp, meta)
        placed.append(f"{runs[orig_run]}/{step}")
    pname, _ = active_project()
    if pname:
        for r in runs.values():
            assign_to_project("models", r, pname)
    return placed


def _place_datasets(tmp, stem):
    roots = sorted({p.parent.parent for p in tmp.rglob("meta/info.json")}, key=lambda p: len(p.parts))
    tops = [r for r in roots if not any(o != r and r.is_relative_to(o) for o in roots)]
    if not tops:
        raise ValueError("데이터셋이 없습니다 — meta/info.json 이 들어 있는 LeRobot 데이터셋 폴더여야 합니다")
    placed = []
    for r in tops:
        name = _free_name(DATA_ROOT, _clean_name(r.name if r != tmp else stem, stem))
        shutil.move(str(r), str(DATA_ROOT / name))
        placed.append(name)
    pname, _ = active_project()
    if pname:
        for n in placed:
            assign_to_project("datasets", n, pname)
    return placed


@app.post("/api/import/{what}")
async def api_import(what: str, req: Request, filename: str = ""):
    if what not in ("model", "dataset"):
        return JSONResponse({"error": "model 또는 dataset"}, status_code=400)
    root = OUT_ROOT if what == "model" else DATA_ROOT
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".import_{secrets.token_hex(4)}"
    tmp.mkdir()
    try:
        limit = shutil.disk_usage(root).free * IMPORT_DISK_SHARE
        clen = int(req.headers.get("content-length") or 0)
        if clen > limit:
            return JSONResponse({"error": "파일이 디스크 여유 공간에 비해 너무 큽니다 (압축 해제 공간 포함)"}, status_code=400)
        arc = tmp / "upload.bin"
        n = 0
        with open(arc, "wb") as fh:
            async for chunk in req.stream():
                n += len(chunk)
                if n > limit:
                    return JSONResponse({"error": "파일이 디스크 여유 공간에 비해 너무 큽니다"}, status_code=400)
                fh.write(chunk)
        if n == 0:
            return JSONResponse({"error": "빈 파일"}, status_code=400)
        out = tmp / "x"
        out.mkdir()
        stem = _clean_name(re.sub(r"(\.tar)?\.(tar|gz|tgz|zip)$", "", Path(filename).name),
                           "imported_model" if what == "model" else "imported_dataset")

        def work():
            _safe_extract(arc, out)
            arc.unlink()
            return _place_models(out, stem, Path(filename).name) if what == "model" else _place_datasets(out, stem)

        items = await asyncio.get_running_loop().run_in_executor(None, work)
        return {"ok": True, "items": items}
    except (ValueError, OSError, tarfile.TarError, zipfile.BadZipFile) as e:
        return JSONResponse({"error": f"가져오기 실패: {e}"}, status_code=400)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@app.post("/api/train/resume")
async def api_train_resume(req: Request):
    b = await req.json()
    run = b.get("run", "")
    if not safe_name(run) or not (OUT_ROOT / run / "checkpoints").is_dir():
        return JSONResponse({"error": "모델 없음"}, status_code=400)
    busy = gpu_or_loop_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    last = OUT_ROOT / run / "checkpoints" / "last"
    tc = last / "pretrained_model" / "train_config.json"
    if not tc.is_file() or not (last / "training_state").is_dir():
        return JSONResponse({"error": "이어서 학습할 체크포인트가 없습니다 (last/training_state 없음 — 가져온 모델은 학습 상태가 빠져 있을 수 있습니다)"},
                            status_code=400)
    done = load_json(last / "training_state" / "training_step.json", {}).get("step") or 0
    steps = _clamp_int(b.get("steps"), 0, 1, 100000000)
    if steps <= done:
        return JSONResponse({"error": f"목표 step 은 지금({done})보다 커야 합니다"}, status_code=400)
    argv = [sys.executable, "-m", "lerobot.scripts.lerobot_train", f"--config_path={tc}", "--resume=true",
            f"--steps={steps}", f"--output_dir={OUT_ROOT / run}"]
    try:
        jid = start_job("train", argv)
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid}


# ----------------------------- Hugging Face Hub · HF Jobs 클라우드 학습 -------------
# 본체는 openmimic_hub.py. 업로드·다운로드·클라우드 학습은 작업(job)으로 띄워 로그로 진행을 봅니다.
# 클라우드 학습 작업 종류는 'cloudtrain' — 이 기기의 GPU·팔을 쓰지 않으므로 수집·추론·학습과 배타가 아닙니다.
HUB_PY = Path(__file__).resolve().parent / "openmimic_hub.py"
_HF_JOB_RE = re.compile(r"Job submitted:\s*(\S+)")
_HF_PAGE_RE = re.compile(r"Job page:\s*(\S+)")
_HF_REPO_RE = re.compile(r"Model repo:\s*https://huggingface\.co/(\S+)")


def _hub():
    import openmimic_hub
    return openmimic_hub


def cloud_job_info(j):
    """클라우드 학습 작업 로그에서 HF 작업 id·페이지·모델 repo"""
    out = {"hf_job": None, "page": None, "repo": None}
    try:
        with open(j["log"], errors="ignore") as f:
            head = f.read(200_000)
    except OSError:
        return out
    for key, rx in (("hf_job", _HF_JOB_RE), ("page", _HF_PAGE_RE), ("repo", _HF_REPO_RE)):
        m = rx.search(head)
        if m:
            out[key] = m.group(1)
    return out


def cancel_cloud(j):
    """로컬 제출 프로세스를 멈춰도 원격 작업은 계속 돕니다(lerobot 은 Ctrl-C 를 '분리' 로 처리). 원격도 취소합니다."""
    hid = cloud_job_info(j)["hf_job"]
    if not hid:
        return False
    try:
        _hub().cancel_job(hid)
        return True
    except Exception as e:     # noqa: BLE001 — 이미 끝난 작업이면 404
        print(f"[hub] cancel {hid}: {e}", file=sys.stderr)
        return False


@app.get("/api/hub/state")
def api_hub_state(refresh: int = 0):
    try:
        return _hub().status(bool(refresh))
    except Exception as e:     # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/api/hub/flavors")
def api_hub_flavors():
    try:
        return {"flavors": _hub().flavors()}
    except Exception as e:     # noqa: BLE001 — 오프라인이면 빈 목록 (이 기기 학습만)
        return {"flavors": [], "error": f"하드웨어 목록을 못 받았습니다: {type(e).__name__}"}


@app.post("/api/hub/login")
async def api_hub_login(req: Request):
    b = await req.json()
    try:
        return await asyncio.get_running_loop().run_in_executor(None, _hub().login, b.get("token", ""))
    except Exception as e:     # noqa: BLE001
        return JSONResponse({"error": f"로그인 실패: {str(e)[:200]}"}, status_code=400)


@app.post("/api/hub/logout")
def api_hub_logout():
    _hub().logout()
    return {"ok": True}


@app.get("/api/hub/mine")
def api_hub_mine():
    st = _hub().status()
    if not st.get("ok"):
        return {"datasets": [], "models": []}
    try:
        return {"datasets": _hub().list_mine(st["user"], "datasets"), "models": _hub().list_mine(st["user"], "models")}
    except Exception as e:     # noqa: BLE001
        return {"datasets": [], "models": [], "error": f"{type(e).__name__}: {e}"}


_HUB_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


@app.post("/api/hub/push")
async def api_hub_push(req: Request):
    b = await req.json()
    ds = b.get("dataset", "")
    if not safe_name(ds) or not (DATA_ROOT / ds / "meta/info.json").is_file():
        return JSONResponse({"error": "데이터셋 없음"}, status_code=400)
    busy = dataset_busy(ds)
    if busy:
        return JSONResponse({"error": f"{busy['id']} 가 이 데이터셋을 쓰는 중"}, status_code=400)
    st = _hub().status(refresh=True)
    if not st.get("ok"):
        return JSONResponse({"error": "Hugging Face 로그인이 필요합니다"}, status_code=400)
    owner = b.get("owner") or st["user"]
    if owner != st["user"] and owner not in st.get("orgs", []):
        return JSONResponse({"error": "내 계정이나 소속 조직에만 올릴 수 있습니다"}, status_code=400)
    argv = [sys.executable, str(HUB_PY), "push-dataset", f"--root={DATA_ROOT / ds}", f"--repo={owner}/{ds}"]
    if b.get("public"):
        argv.append("--public")
    try:
        return {"ok": True, "job": start_job("hub", argv, spec={"op": "push", "dataset": ds, "repo": f"{owner}/{ds}"})}
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/hub/pull")
async def api_hub_pull(req: Request):
    b = await req.json()
    what, repo = b.get("kind"), (b.get("repo") or "").strip().removeprefix("https://huggingface.co/")
    repo = repo.removeprefix("datasets/").strip("/")
    if what not in ("dataset", "model") or not _HUB_REPO_RE.fullmatch(repo):
        return JSONResponse({"error": "repo id 는 '조직/이름' 형식입니다 (예: lerobot/svla_so101_pickplace)"}, status_code=400)
    if what == "dataset":
        argv = [sys.executable, str(HUB_PY), "pull-dataset", f"--repo={repo}", f"--dest={DATA_ROOT}"]
    else:
        argv = [sys.executable, str(HUB_PY), "pull-model", f"--repo={repo}", f"--dest={OUT_ROOT}"]
        step = (b.get("step") or "").strip()
        if step:
            if not safe_name(step):
                return JSONResponse({"error": "체크포인트 이름이 잘못됐습니다"}, status_code=400)
            argv.append(f"--step={step}")
    try:
        return {"ok": True, "job": start_job("hub", argv, spec={"op": "pull", "kind": what, "repo": repo})}
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/hub/cancel/{jid}")
def api_hub_cancel(jid: str):
    if not safe_name(jid):
        return JSONResponse({"error": "잘못된 작업 id"}, status_code=400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    if j.get("kind") != "cloudtrain":
        return JSONResponse({"error": "클라우드 학습 작업이 아닙니다"}, status_code=400)
    kill_job(jid)
    ok = cancel_cloud(j)
    return {"ok": True, "remote_cancelled": ok}


@app.get("/hub", response_class=HTMLResponse)
def hub_page():
    dsets = list_datasets()
    ds_opts = "".join(f'<option value="{esc(d["name"])}">{esc(d["name"])} ({esc(d["episodes"])}ep)</option>' for d in dsets)
    hubjobs = [j for j in jobs_index() if j["kind"] in ("hub", "cloudtrain")][:12]
    rows = ""
    for j in hubjobs:
        sp = j.get("spec") or {}
        if j["kind"] == "cloudtrain":
            ci = cloud_job_info(j)
            what = f'클라우드 학습 · {esc(sp.get("flavor", ""))} · {esc(sp.get("dataset", ""))}'
            links = ((f'<a href="{esc(ci["page"])}" target=_blank rel=noopener>HF 작업</a> ' if ci["page"] else '')
                     + (f'<a href="https://huggingface.co/{esc(ci["repo"])}" target=_blank rel=noopener>모델 repo</a> '
                        f'<button onclick="pull(\'model\',{jsattr(ci["repo"])})">모델 받기</button> ' if ci["repo"] else '')
                     + (f'<button class=danger onclick="cancelCloud({jsattr(j["id"])})">원격 취소</button>' if ci["hf_job"] else ''))
        else:
            what = f'{"올리기" if sp.get("op") == "push" else "받기"} · {esc(sp.get("repo", ""))}'
            links = ''
        run = '<span class="badge b-run">진행 중</span>' if j["alive"] else ""
        rows += (f'<tr><td><a class=mono href="/jobs/{esc(j["id"])}">{esc(j["id"])}</a></td><td>{what}</td>'
                 f'<td>{run}</td><td style="text-align:right">{links}</td></tr>')
    jobs_html = (f'<p class=eyebrow style="margin-top:20px">Hub 작업</p><div class=card><table>{rows}</table></div>'
                 if rows else "")
    return f"""{CSS}{nav_html('hb')}<div class=wrap>
    <p class=eyebrow>Hugging Face Hub</p><h2>허브</h2>
    <div class=card>
      <div id=hfstate class=muted>로그인 상태 확인 중…</div>
      <div class=formgrid id=loginbox style="display:none;margin-top:8px">
        <label class=f style="min-width:360px">액세스 토큰 (write 권한)
          <input type=password id=tok placeholder="hf_..." autocomplete=off></label>
        <button class=primary onclick="login()">로그인</button>
        <span class=muted>토큰은 <a href="https://huggingface.co/settings/tokens" target=_blank rel=noopener>huggingface.co/settings/tokens</a> 에서 만듭니다.
        터미널의 <span class=mono>hf auth login</span> 과 같은 곳에 저장됩니다.</span>
      </div>
    </div>
    <div class=card style="margin-top:12px">
      <p class=eyebrow>데이터셋 올리기</p>
      <div class=formgrid>
        <label class=f style="min-width:300px">로컬 데이터셋 <select id=pds>{ds_opts}</select></label>
        <label class=muted><input type=checkbox id=pub> 공개 (기본은 비공개)</label>
        <button onclick="push()">허브에 올리기</button>
      </div>
      <p class=muted style="margin:6px 0 0">내 계정의 <span class=mono>사용자/데이터셋이름</span> 으로 올라갑니다. 같은 이름이 있으면 바뀐 파일만 갱신합니다.</p>
    </div>
    <div class=card style="margin-top:12px">
      <p class=eyebrow>받기</p>
      <div class=formgrid>
        <label class=f style="min-width:360px">Hub repo id
          <input id=prepo placeholder="lerobot/svla_so101_pickplace" list=minelist></label>
        <datalist id=minelist></datalist>
        <label class=f>체크포인트 (모델, 비우면 마지막) <input id=pstep size=10></label>
        <button onclick="pull('dataset')">데이터셋으로 받기</button>
        <button onclick="pull('model')">모델로 받기</button>
      </div>
      <p class=muted style="margin:6px 0 0">커뮤니티 데이터셋으로 학습하거나, 다른 곳(HF Jobs 등)에서 학습한 정책을 이 기기 팔로 돌릴 때 씁니다.
      받은 데이터셋은 Datasets, 모델은 모델 탭에 나옵니다. 데이터셋은 LeRobot v3.0 형식이어야 합니다.</p>
      <div id=mine class=muted style="margin-top:6px"></div>
    </div>
    {jobs_html}
    <p class=muted style="margin-top:14px">클라우드 학습(HF Jobs)은 학습 탭의 <b>실행 위치</b> 에서 고릅니다. 로컬 데이터셋은 시작할 때 내 계정 비공개 repo 로 먼저 올라갑니다.</p>
    </div>
    <script>
    async function post(u,b){{ const r=await fetch(u,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b||{{}})}}); return r.json(); }}
    function ovEsc(s){{return String(s).replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));}}
    async function state(refresh){{
      const d=await (await fetch('/api/hub/state'+(refresh?'?refresh=1':''))).json();
      const box=document.getElementById('hfstate');
      if(d.ok){{
        box.innerHTML='<span class="badge b-ok">로그인됨</span> <b class=mono>'+ovEsc(d.user)+'</b>'
          +(d.orgs&&d.orgs.length?' · 조직 '+d.orgs.map(ovEsc).join(', '):'')+' <button onclick="logout()">로그아웃</button>';
        document.getElementById('loginbox').style.display='none';
        const m=await (await fetch('/api/hub/mine')).json();
        const all=[...(m.datasets||[]).map(x=>[x,'dataset']),...(m.models||[]).map(x=>[x,'model'])];
        document.getElementById('minelist').innerHTML=all.map(x=>'<option value="'+ovEsc(x[0])+'">').join('');
        document.getElementById('mine').textContent=all.length?'내 Hub: 데이터셋 '+(m.datasets||[]).length+'개 · 모델 '+(m.models||[]).length+'개 (위 입력칸에서 고를 수 있습니다)':'';
      }} else {{
        box.innerHTML='<span class="badge b-warn">로그인 안 됨</span> '+(d.error&&d.error!=='로그인 안 됨'?ovEsc(d.error):'');
        document.getElementById('loginbox').style.display='';
      }}
    }}
    async function login(){{
      const d=await post('/api/hub/login',{{token:document.getElementById('tok').value}});
      document.getElementById('tok').value='';
      if(d.error) alert(d.error); else state(true);
    }}
    async function logout(){{ if(!confirm('이 기기에서 Hugging Face 로그아웃할까요?')) return; await post('/api/hub/logout'); state(true); }}
    async function push(){{
      const ds=document.getElementById('pds').value; if(!ds) return;
      const pub=document.getElementById('pub').checked;
      if(pub && !confirm('공개로 올리면 누구나 볼 수 있습니다. 계속할까요?')) return;
      const d=await post('/api/hub/push',{{dataset:ds,public:pub}}); if(d.error) alert(d.error); else location.href='/jobs/'+d.job;
    }}
    async function pull(kind,repo){{
      repo=repo||document.getElementById('prepo').value;
      const d=await post('/api/hub/pull',{{kind:kind,repo:repo,step:kind==='model'?document.getElementById('pstep').value:''}});
      if(d.error) alert(d.error); else location.href='/jobs/'+d.job;
    }}
    async function cancelCloud(jid){{
      if(!confirm('HF Jobs 원격 학습을 취소할까요? (지금까지 올라간 체크포인트는 남습니다)')) return;
      const d=await post('/api/hub/cancel/'+jid); if(d.error) alert(d.error); else location.reload();
    }}
    state();
    </script>"""


# ----------------------------- 페이지: 추론 (Rollout) ------------------------
# ----------------------------- 롤아웃 실시간 모니터 · 시도 기록 -------------------
# 롤아웃은 openmimic_rollout.py 로 띄웁니다. 그 프로세스가 RUN_DIR/<jid>/ 에 status.json · cam_*.jpg 를 씁니다.
# 시도 기록(성공/실패)은 openmimic_trials.json 에 체크포인트별로 쌓습니다 — 실기 성공률로 모델을 비교하려고.
ROLLOUT_PY = Path(__file__).resolve().parent / "openmimic_rollout.py"
TRIALS_FILE = PROJ / "openmimic_trials.json"
TRIAL_RESULTS = ("success", "fail")


def job_ckpt_rel(j):
    """롤아웃 작업 → 체크포인트 상대경로 (outputs 기준). 모르면 ''."""
    for a in j.get("argv") or []:
        if a.startswith("--policy.path="):
            p = Path(a.split("=", 1)[1])
            try:
                return str(p.resolve().relative_to(OUT_ROOT.resolve()))
            except ValueError:
                return ""
    return ""


def load_trials():
    return load_json(TRIALS_FILE, {})


def trial_summary(rel, trials=None):
    t = (trials if trials is not None else load_trials()).get(rel) or []
    ok = sum(1 for x in t if x.get("result") == "success")
    n = len(t)
    return {"n": n, "ok": ok, "fail": n - ok, "rate": round(100 * ok / n) if n else None}


def trial_label(rel, trials=None):
    s = trial_summary(rel, trials)
    return f" · 성공 {s['ok']}/{s['n']}" if s["n"] else ""


@app.get("/api/rollout/check")
def api_rollout_check(ckpt: str = ""):
    ck = ov_ckpt_ok(ckpt)
    if ck is None:
        return JSONResponse({"error": "체크포인트 없음"}, status_code=400)
    errs, warns = policy_fit(ckpt)
    return {"errors": errs, "warnings": warns, "needs_task": policy_needs_task(ckpt),
            "policy": load_json(ck / "config.json", {}).get("type", "?")}


@app.get("/api/rollout/status/{jid}")
def api_rollout_status(jid: str):
    if not safe_name(jid):
        return JSONResponse({"error": "잘못된 작업 id"}, status_code=400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    if j.get("kind") != "rollout":
        return JSONResponse({"error": "롤아웃 작업이 아닙니다"}, status_code=404)
    rel = job_ckpt_rel(j)
    trials = load_trials()
    mine = [x for x in trials.get(rel, []) if x.get("job") == jid]
    alive = job_alive(j)
    st = load_json(run_dir(jid) / "status.json", {})
    if not alive and st.get("phase") not in ("done", "error"):
        st["phase"] = "ended"           # 강제 종료 등으로 마지막 상태를 못 쓴 경우
    tail = log_tail(jid)
    hint = failure_hint(tail) if (not alive and st.get("phase") in ("error", "ended")) or st.get("phase") == "error" else ""
    return {"alive": alive, "status": st, "tail": tail, "ckpt": rel, "hint": hint,
            "engine": rollout_engine_label(j), "started": j.get("started"),
            "trials": {"run": {"n": len(mine), "ok": sum(1 for x in mine if x["result"] == "success")},
                       "ckpt": trial_summary(rel, trials)}}


@app.get("/api/runstream/{jid}/{cam}")
def api_runstream(jid: str, cam: str):
    if not safe_name(jid) or not safe_name(cam):
        return JSONResponse({"error": "잘못된 이름"}, status_code=400)
    return _mjpeg_from_files(jid, cam)


@app.post("/api/rollout/trial")
async def api_rollout_trial(req: Request):
    b = await req.json()
    jid, res = b.get("job", ""), b.get("result", "")
    if not safe_name(jid) or res not in TRIAL_RESULTS + ("undo",):
        return JSONResponse({"error": "잘못된 요청"}, status_code=400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    rel = job_ckpt_rel(j) if j.get("kind") == "rollout" else ""
    if not rel:
        return JSONResponse({"error": "체크포인트를 알 수 없는 작업"}, status_code=400)
    with META_LOCK:
        trials = load_trials()
        lst = trials.setdefault(rel, [])
        if res == "undo":
            idx = next((i for i in range(len(lst) - 1, -1, -1) if lst[i].get("job") == jid), None)
            if idx is None:
                return JSONResponse({"error": "이 실행에서 기록한 시도가 없습니다"}, status_code=400)
            lst.pop(idx)
        else:
            lst.append({"t": time.strftime("%F %T"), "result": res, "job": jid,
                        "engine": rollout_engine_label(j), "env": env_name()})
        save_json(TRIALS_FILE, trials)
    return {"ok": True, "ckpt": trial_summary(rel, trials)}


# IMPORTMAP_HTML · ARM3D_JS 는 파일 뒤쪽에 정의되므로 조립은 rollout_run_page() 에서 합니다
ROLLOUT_RUN_BODY = """
<style>
.rgrid{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:14px;align-items:start}
.rcams{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px}
.rcams figure{margin:0;position:relative;background:#000;border-radius:8px;overflow:hidden}
.rcams img{width:100%;display:block;aspect-ratio:4/3;object-fit:contain}
.rcams figcaption{position:absolute;top:6px;left:10px;font-family:var(--mono);font-size:11px;color:#cfd8e3;text-shadow:0 0 4px #000;text-transform:uppercase}
#r3d{aspect-ratio:4/3;border-radius:8px;overflow:hidden;background:#0a0d10;position:relative}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:13px}
.kv b{font-family:var(--mono);font-weight:500}
.big{font-family:var(--mono);font-size:22px;font-weight:600}
.trial{display:flex;gap:8px;margin:10px 0 6px}
.trial button{flex:1;font-size:15px;padding:12px 0}
.ok-btn{border-color:var(--ok);color:var(--ok)} .ng-btn{border-color:var(--bad);color:var(--bad)}
.jt td,.jt th{padding:3px 6px;font-size:12px} .jt td.num{font-family:var(--mono)}
.jt tr.far td{color:var(--warn)}
.ph{font-family:var(--mono);font-size:13px;letter-spacing:.06em}
@media(max-width:900px){ .rgrid{grid-template-columns:1fr} }
</style>
<div class=wrap>
<p class=eyebrow>자율 실행 · 실시간</p><h2>추론 실행</h2>
<div class=runbar><span class="badge b-run" id=phb>…</span><span class=mono id=jid></span>
  <span class=badge id=engb></span><span class=mono id=el></span>
  <button class=danger id=stopb onclick="stopRo()">중지</button>
  <a class=btnlink id=newb href="/rollout" style="display:none">새 추론 설정</a>
  <span class="badge b-bad" id=hint style="display:none;white-space:normal"></span>
  <span class=muted id=stopnote>중지(SIGINT) 시 시작 자세로 복귀 후 토크 해제됩니다</span></div>
<div class=rgrid>
  <div>
    <div class=rcams><div id=cams style="display:contents"><p class=muted>카메라 대기 중…</p></div>
      <div id=r3d style="display:none"></div></div>
  </div>
  <div>
    <div class=card>
      <p class=eyebrow>시도 결과</p>
      <div class=trial><button class=ok-btn onclick="trial('success')">성공 (S)</button>
        <button class=ng-btn onclick="trial('fail')">실패 (F)</button></div>
      <div class=kv><span>이번 실행</span><b id=trun>0 / 0</b><span>이 체크포인트 누적</span><b id=tck>-</b></div>
      <p class=muted style="margin:8px 0 0">물체를 놓고 한 번 시도할 때마다 결과를 누르세요. 체크포인트별 실기 성공률로 모델을 비교합니다.
        <a href="#" onclick="trial('undo');return false">마지막 기록 취소 (U)</a></p>
    </div>
    <div class=card style="margin-top:10px">
      <p class=eyebrow>성능</p>
      <div class=kv>
        <span>제어 주기</span><b id=hz>-</b>
        <span>추론 (청크 계산)</span><b id=chunk>-</b>
        <span>추론 (보통 틱)</span><b id=tick>-</b>
        <span>프레임 예산</span><b id=budget>-</b>
      </div>
      <p class=muted id=perfwarn style="margin:6px 0 0"></p>
      <p id=health class=badge style="display:none;margin:8px 0 0"></p>
    </div>
    <div class=card style="margin-top:10px">
      <p class=eyebrow>관절 — 실측 / 명령</p>
      <table class=jt id=jt></table>
      <p class=muted style="margin:6px 0 0">차이가 크게 유지되면 막힘·과부하 또는 정책이 학습 범위를 벗어난 것입니다.</p>
    </div>
  </div>
</div>
<details style="margin-top:12px"><summary class=muted>로그</summary><pre id=tail>...</pre></details>
</div>
"""
ROLLOUT_RUN_JS = """
const $=id=>document.getElementById(id);
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const PH={loading:'모델 로드 중',connecting:'로봇 연결 중',running:'추론 중',returning:'시작 자세로 복귀 중',
          done:'종료',error:'오류로 종료',ended:'종료'};
let ARM=null, camsShown='', ALIVE=true;
$('jid').textContent=RO.jid;
window.stopRo=async function(){
  if(!confirm('추론을 중지할까요? (시작 자세로 돌아간 뒤 토크를 끕니다)'))return;
  await fetch('/api/kill/'+RO.jid,{method:'POST'});
};
window.trial=async function(r){
  const res=await fetch('/api/rollout/trial',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({job:RO.jid,result:r})});
  const d=await res.json(); if(d.error){ alert(d.error); return; }
  refresh();
};
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.repeat) return;
  const k=e.key.toLowerCase();
  if(k==='s') trial('success'); else if(k==='f') trial('fail'); else if(k==='u') trial('undo');
});
async function mount3d(){
  if(!RO.urdf_ok) return;
  $('r3d').style.display='';
  const views={}; RO.sides.forEach(s=>{ views[s]=RO.views[s]||{x:0,y:(s==='left'?0.12:s==='right'?-0.12:0),yaw_deg:0}; });
  try{ ARM=await window.mountArm3D($('r3d'), RO.sides, views, RO.k3); }catch(e){ $('r3d').style.display='none'; }
}
function fmtT(t){ t=Math.max(0,t||0); return String(Math.floor(t/60)).padStart(2,'0')+':'+String(Math.floor(t%60)).padStart(2,'0'); }
function joints(st){
  const o=st.obs||{}, a=st.act||{}; let h='<tr><th>관절</th><th class=num>실측</th><th class=num>명령</th><th class=num>차이</th></tr>';
  for(const side of Object.keys(o)){
    for(const j of Object.keys(o[side])){
      const ov=o[side][j], av=(a[side]||{})[j], d=(av==null)?null:av-ov;
      const far=d!=null && Math.abs(d)>(j==='gripper'?15:8);
      h+='<tr class="'+(far?'far':'')+'"><td>'+E((side==='main'?'':side+' ')+j)+'</td><td class=num>'+ov.toFixed(1)
        +'</td><td class=num>'+(av==null?'-':av.toFixed(1))+'</td><td class=num>'+(d==null?'-':d.toFixed(1))+'</td></tr>';
    }
  }
  $('jt').innerHTML=h;
  if(ARM) for(const side of Object.keys(o)) ARM.update(side, o[side]);
}
let SEQ=0, APPLIED=0;
async function refresh(){
  const my=++SEQ;      // 응답이 순서를 바꿔 도착하면 이미 반영한 것보다 옛 응답은 버립니다
  let d; try{ d=await (await fetch('/api/rollout/status/'+RO.jid)).json(); }catch(e){ return; }
  if(my<APPLIED) return; APPLIED=my;
  if(d.error){ $('phb').textContent=d.error; return; }
  const st=d.status||{}; ALIVE=d.alive;
  $('phb').textContent=PH[st.phase]||st.phase||'시작 중';
  $('phb').className='badge '+(st.phase==='running'?'b-run':st.phase==='error'?'b-bad':(!d.alive?'':'b-warn'));
  $('engb').textContent=d.engine+(st.device&&d.engine.indexOf(st.device)<0?' → '+st.device:'');
  $('el').textContent=st.elapsed?fmtT(st.elapsed):'';
  $('stopb').style.display=d.alive?'':'none'; $('stopnote').style.display=d.alive?'':'none';
  $('newb').style.display=d.alive?'none':'';
  const cams=(st.cams||[]).join(',');
  if(cams && cams!==camsShown){
    camsShown=cams;
    $('cams').innerHTML=(st.cams||[]).map(c=>{ const n=c.replace('observation.images.','');
      return '<figure><img src="/api/runstream/'+RO.jid+'/'+encodeURIComponent(c)+'"><figcaption>'+E(n)+'</figcaption></figure>'; }).join('');
  }
  if(!d.alive && camsShown && st.phase!=='running'){ /* 마지막 프레임 유지 */ }
  const b=1000/RO.fps;
  $('hz').textContent=st.hz?st.hz+' Hz (목표 '+RO.fps+')':'-';
  $('chunk').textContent=st.chunk_ms!=null?st.chunk_ms+' ms':'-';
  $('tick').textContent=st.tick_ms!=null?st.tick_ms+' ms':'-';
  $('budget').textContent=b.toFixed(0)+' ms';
  let w='';
  if(st.chunk_ms!=null && st.chunk_ms>b) w+='청크 계산이 프레임 예산보다 깁니다 — 청크가 바뀌는 순간 한 박자 멈출 수 있습니다. ';
  if(st.hz && st.hz<RO.fps*0.85) w+='제어 주기가 목표보다 낮습니다 (카메라·USB 대역폭·CPU 부하 확인).';
  $('perfwarn').textContent=w;
  const hs=st.health, hb=$('health');
  if(st.health_stop){ hb.style.display=''; hb.className='badge b-bad';
    hb.textContent='서보 상태 이상으로 자동 정지했습니다: '+st.health_stop; }
  else if(hs&&hs.msgs&&hs.msgs.length){ hb.style.display=''; hb.className='badge '+(hs.level==='stop'?'b-bad':'b-warn');
    hb.textContent='서보 상태: '+hs.msgs.join(' · ')+(hs.minvolt!=null?' (최저 전압 '+hs.minvolt+' V)':''); }
  else if(hs&&hs.maxtemp!=null){ hb.style.display=''; hb.className='badge';
    hb.textContent='서보 상태 정상 — 최고 온도 '+hs.maxtemp+'°C'+(hs.minvolt!=null?' · 최저 전압 '+hs.minvolt+' V':''); }
  else if(st.health_err){ hb.style.display=''; hb.className='badge b-warn'; hb.textContent='서보 상태 감시를 켜지 못했습니다: '+st.health_err; }
  else hb.style.display='none';
  joints(st);
  const tr=d.trials||{}, run=tr.run||{n:0,ok:0}, ck=tr.ckpt||{};
  $('trun').textContent=run.ok+' 성공 / '+run.n+' 시도';
  $('tck').textContent=ck.n?(ck.ok+' / '+ck.n+' ('+ck.rate+'%)'):'기록 없음';
  $('tail').textContent=(st.err?'!! '+st.err+'\\n\\n':'')+(d.tail||'');
  $('hint').style.display=d.hint?'':'none'; $('hint').textContent=d.hint?'원인 추정: '+d.hint:'';
  if(d.hint) document.querySelector('details').open=true;
}
// 주기 갱신은 앞 요청이 끝난 뒤에 다음을 보냅니다 (서버가 느려도 요청이 쌓이지 않게)
async function loop(){ await refresh(); setTimeout(loop, 500); }
mount3d(); loop();
"""


def rollout_run_page(j):
    k3 = kind3d()
    views = {sd: (a.get("view") or {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}) for sd, a in ARM_CFGS.items()}
    ro = {"jid": j["id"], "fps": CFG["fps"], "sides": SIDES, "views": views, "k3": k3,
          "urdf_ok": (URDF_DIR / k3["urdf"][len("/urdf/"):]).exists()}
    return (CSS + nav_html("ro") + f"<script>const RO={js(ro)};</script>" + ROLLOUT_RUN_BODY + IMPORTMAP_HTML
            + '<script type="module">' + ARM3D_JS + ROLLOUT_RUN_JS + "</script>")


def rollout_engine_label(j):
    for a in j.get("argv") or []:
        if a.startswith("--ov.device="):
            prec = next((x.split("=", 1)[1] for x in j["argv"] if x.startswith("--ov.precision=")), "fp16")
            return f"OpenVINO · {a.split('=', 1)[1]} · {prec}"
    return "PyTorch"


@app.get("/rollout", response_class=HTMLResponse)
def rollout_page(job: str = "", ckpt: str = ""):
    ro = next((j for j in jobs_index() if j["kind"] == "rollout" and j["alive"]), None)
    if ro:
        return rollout_run_page(ro)
    if job and safe_name(job):
        j = load_json(JOB_DIR / f"{job}.json", {})
        if j.get("kind") == "rollout":
            return rollout_run_page(j)
    busy = exclusive_busy()
    busywarn = (f'<p class="badge b-warn">실행 중: {esc(busy["id"])} — 끝나야 추론을 시작할 수 있습니다</p>'
                if busy else "") + setup_needed_html()
    pname, pr = active_project()
    mine = set(pr["models"]) if pr else set()
    ckpts = sorted(list_checkpoints(), key=lambda c: c.split("/checkpoints/")[0] not in mine)   # 안정 정렬
    ck_opts = ""
    trials = load_trials()
    for c in ckpts:
        rt = checkpoint_robot_type(c)
        bad = not robot_type_ok(rt)
        other = pname and c.split("/checkpoints/")[0] not in mine
        ck_opts += (f'<option value="{esc(c)}" {"disabled" if bad else ""} {"selected" if c == ckpt and not bad else ""}>{esc(c)}'
                    f'{" · " + esc(rt) + " — 모드 불일치" if bad else (" · " + esc(rt) if rt else "")}'
                    f'{" · 다른 프로젝트/미분류" if other else ""}{esc(ov_label(c))}{esc(trial_label(c, trials))}</option>')
    recent = [j for j in jobs_index() if j["kind"] == "rollout"][:6]
    recent_html = "".join(
        f'<tr><td><a href="/rollout?job={esc(j["id"])}" class=mono>{esc(j["id"])}</a></td>'
        f'<td class=mono>{esc(job_ckpt_rel(j).replace("/pretrained_model", ""))}</td><td>{esc(rollout_engine_label(j))}</td>'
        f'<td class=num>{sum(1 for x in trials.get(job_ckpt_rel(j), []) if x.get("job") == j["id"] and x["result"] == "success")}'
        f' / {sum(1 for x in trials.get(job_ckpt_rel(j), []) if x.get("job") == j["id"])}</td></tr>'
        for j in recent)
    recent_html = (f'<p class=eyebrow style="margin-top:22px">최근 실행</p><div class=card><table>'
                   f'<tr><th>작업</th><th>체크포인트</th><th>엔진</th><th class=num>성공 / 시도</th></tr>{recent_html}</table></div>'
                   if recent else "")
    ovd = ov_devices()
    fams = ov_families()
    eng_opts = '<option value=torch>PyTorch (기본)</option>'
    if ovd.get("ok"):
        for dev in OV_DEVICES:
            eng_opts += (f'<option value="ov:{dev}" {"" if dev in fams else "disabled"}>OpenVINO · {dev}'
                         f'{" — " + esc(fams[dev]) if dev in fams else " — 없음"}</option>')
    else:
        eng_opts += f'<option disabled>OpenVINO — {esc(ovd.get("error") or "사용 불가")} (Intel 기기 전용)</option>'
    empty = "" if ckpts else need_html("먼저 모델을 학습하세요", "실행할 체크포인트가 없습니다 — 학습 탭에서 학습하거나, "
                                       "모델 탭에서 다른 기기에서 내보낸 모델을 가져오세요.", "/train", "학습하러 가기")
    return f"""{CSS}{nav_html('ro')}<div class=wrap>
    <p class=eyebrow>자율 실행 · {esc(kind()["label"])} · {"양팔 " + kind()["cli"]["bi_follower"] if BIMANUAL else "한팔 " + kind()["cli"]["follower"]}</p><h2>실행</h2>
    {busywarn}{empty}{f'<p class=muted>프로젝트 <b class=mono>{esc(pname)}</b> — 이 프로젝트의 모델이 위에 옵니다.</p>' if pname else ''}
    <div class=card>
    <div class=formgrid>
      <label class=f style="min-width:380px">체크포인트
        <select id=ckpt>{ck_opts}</select></label>
      <label class=f>추론 엔진 <select id=engine onchange="ovInfo()">{eng_opts}</select></label>
      <label class=f>정밀도 <select id=prec onchange="ovInfo()"><option>fp16</option><option>int8</option></select></label>
      <label class=f>실행 시간(초, 0=무한) <input id=dur value=60 size=6></label>
      <label class=f style="flex:1;min-width:260px">태스크 설명
        <input id=task value="{esc(pr["task"] if pr and pr["task"] else CFG['default_task'])}"></label>
      <button class=primary onclick="startRo()" {'disabled' if not ckpts or not ports_configured() or busy else ''}>추론 시작</button>
    </div>
    <div id=fitinfo style="margin-top:8px"></div>
    <div id=ovinfo style="margin-top:8px"></div>
    <p class=muted>시작 즉시 팔이 움직입니다 — 팔 주변을 비우고, 물체를 시연 위치에 놓으세요.
    카메라 배치는 학습 데이터 수집 때와 동일해야 합니다.</p>
    <p class=muted style="margin-top:4px">체크포인트 삭제·내보내기·OpenVINO 변환은 <a href="/models">모델</a> 탭에서 합니다.</p>
    </div>{recent_html}</div>
    <script>
    {OV_JS}
    const ENG=document.getElementById('engine'), PREC=document.getElementById('prec');
    try{{ const v=localStorage.getItem('openmimic_engine');
          if(v && [...ENG.options].some(o=>o.value===v && !o.disabled)) ENG.value=v;
          const p=localStorage.getItem('openmimic_prec'); if(p) PREC.value=p; }}catch(e){{}}
    async function ovInfo(){{
      try{{ localStorage.setItem('openmimic_engine',ENG.value); localStorage.setItem('openmimic_prec',PREC.value); }}catch(e){{}}
      PREC.disabled = ENG.value==='torch';
      const box=document.getElementById('ovinfo'), ck=document.getElementById('ckpt').value;
      if(ENG.value==='torch' || !ck){{ box.innerHTML=''; return; }}
      const r=await fetch('/api/ov/status?ckpt='+encodeURIComponent(ck)); const d=await r.json();
      if(d.error){{ box.textContent=d.error; return; }}
      box.innerHTML = d.status.state==='none'
        ? '<p class="badge b-warn">이 체크포인트는 OpenVINO 변환이 없습니다 — 모델 탭에서 "OpenVINO 변환" 을 먼저 하세요</p>'
        : ovTable(d);
    }}
    async function fitInfo(){{
      const ck=document.getElementById('ckpt').value, box=document.getElementById('fitinfo');
      if(!ck){{ box.innerHTML=''; return; }}
      const d=await (await fetch('/api/rollout/check?ckpt='+encodeURIComponent(ck))).json();
      if(d.error){{ box.textContent=d.error; return; }}
      box.innerHTML='<span class=badge>'+ovEsc(d.policy)+'</span> '
        +(d.errors||[]).map(x=>'<p class="badge b-bad" style="white-space:normal">'+ovEsc(x)+'</p>').join('')
        +(d.warnings||[]).map(x=>'<p class="badge b-warn" style="white-space:normal">'+ovEsc(x)+' — 학습 때와 같게 맞추길 권합니다</p>').join('')
        +(d.needs_task?'<span class=muted> 언어 지시를 쓰는 정책입니다 — 태스크 설명이 동작을 바꿉니다.</span>':'');
    }}
    document.getElementById('ckpt').addEventListener('change', ()=>{{ ovInfo(); fitInfo(); }});
    ovInfo(); fitInfo();
    async function startRo(){{
      if(!confirm('팔이 즉시 자율 구동됩니다. 주변이 안전한가요?'))return;
      const b={{ckpt:document.getElementById('ckpt').value,
               duration:document.getElementById('dur').value,
               task:document.getElementById('task').value,
               engine:ENG.value, precision:PREC.value}};
      const r=await fetch('/api/rollout',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(b)}});
      const d=await r.json(); if(d.error)alert(d.error); else {{ if(d.warn&&d.warn.length) alert('전원 점검 — 확인하세요\\n\\n'+d.warn.join('\\n')); location.reload(); }}
    }}
    </script>"""


@app.post("/api/rollout")
async def api_rollout(req: Request):
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    b = await req.json()
    rel = b.get("ckpt", "")
    ck = (OUT_ROOT / rel).resolve()
    if not rel or not _within(ck, OUT_ROOT) or not (ck / "config.json").is_file():
        return JSONResponse({"error": "체크포인트 없음 — 목록에서 고르세요"}, status_code=400)
    dur = _clamp_int(b.get("duration"), 60, 0, 86400)
    task = (b.get("task") or CFG["default_task"]).strip()
    rt = checkpoint_robot_type(rel)
    if not robot_type_ok(rt):
        return JSONResponse({"error": f"체크포인트는 {rt} 데이터로 학습됨 — 현재 모드({robot_name()})와 다릅니다"},
                            status_code=400)
    errs, _warns = policy_fit(rel)
    if errs:
        return JSONResponse({"error": " / ".join(errs)}, status_code=400)
    if policy_needs_task(rel) and not (b.get("task") or "").strip():
        return JSONResponse({"error": "이 정책은 태스크 설명(언어 지시)을 씁니다 — 태스크 설명을 넣으세요"}, status_code=400)
    miss = plugin_missing() if BIMANUAL else []
    if miss:
        # OpenMimic 자신은 plugins/ 를 직접 읽지만, lerobot CLI 는 설치된 패키지만 찾습니다
        return JSONResponse({"error": "양팔 OMX 플러그인이 설치돼 있지 않습니다 — 터미널에서: "
                                      "pip install --no-deps " + " ".join(f"-e plugins/{m}" for m in miss)},
                            status_code=400)
    engine = b.get("engine") or "torch"
    # 어느 엔진이든 openmimic_rollout.py 로 띄웁니다 — lerobot-rollout 을 그대로 돌리면서 실시간 화면용 상태를 씁니다
    mon = [sys.executable, str(ROLLOUT_PY), f"--openmimic.run_dir={RUN_DIR}/{{jid}}"]
    if engine == "torch":
        head = mon + ["--openmimic.engine=torch"]
    else:
        dev = engine[3:] if engine.startswith("ov:") else ""
        prec = b.get("precision") or "fp16"
        if dev not in OV_DEVICES or prec not in openmimic_ov.PRECISIONS:
            return JSONResponse({"error": "추론 엔진 값이 잘못됐습니다"}, status_code=400)
        st = openmimic_ov.status(ck)
        if st["state"] == "none":
            return JSONResponse({"error": "OpenVINO 변환이 없습니다 — 모델 탭에서 먼저 변환하세요"}, status_code=400)
        if st["state"] == "stale":
            return JSONResponse({"error": "변환 이후 체크포인트가 바뀌었습니다 — 모델 탭에서 다시 변환하세요"},
                                status_code=400)
        if prec not in st["precisions"]:
            return JSONResponse({"error": f"{prec} 변환본이 없습니다 — 변환 시 INT8 을 체크했는지 확인하세요"},
                                status_code=400)
        prob = ov_shape_problem(rel)
        if prob:
            return JSONResponse({"error": prob}, status_code=400)
        conv = busy_with(("ovconvert",))
        if conv:
            return JSONResponse({"error": f"{conv['id']} 변환 중 — 끝난 뒤 시작하세요"}, status_code=400)
        # 요청 장치가 없으면 openmimic_ov 가 NPU → GPU → CPU 순으로 대체하고 로그에 크게 알립니다
        head = mon + ["--openmimic.engine=ov", f"--ov.dir={openmimic_ov.ov_dir(ck)}",
                f"--ov.device={dev}", f"--ov.precision={prec}", f"--ov.fps={CFG['fps']}"]
    try:
        argv = (head + [f"--policy.path={ck}"] + robot_cli_args()
                + ([f"--rename_map={json.dumps(rollout_rename_map(rel)[0])}"] if rollout_rename_map(rel)[0] else [])
                + ["--strategy.type=base", f"--duration={dur}", f"--task={task}",
                   f"--fps={CFG['fps']}"])
    except (NotImplementedError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    pbad, pwarn = await asyncio.to_thread(power_preflight, ("follower",))
    if pbad:
        return JSONResponse({"error": "전원 점검 실패 — 시작하지 않았습니다\n\n" + "\n".join(pbad + pwarn)}, status_code=400)
    try:
        jid = start_job("rollout", argv)
    except JobStartError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "job": jid, "warn": pwarn}


@app.post("/api/delete_checkpoint")
async def api_delete_checkpoint(req: Request):
    b = await req.json()
    rel = b.get("rel", "")
    scope = b.get("scope", "step")
    if "/checkpoints/" not in rel:
        return JSONResponse({"error": "체크포인트 경로 아님"}, status_code=400)
    run = rel.split("/checkpoints/")[0]
    run_path = os.path.realpath(OUT_ROOT / run)

    def uses_run(j):
        # 학습(--output_dir)·추론(--policy.path) 인자를 실제 경로로 비교 (act_x 가 act_x_2 에 걸리지 않게)
        for a in j.get("argv") or (j.get("cmd") or "").split():
            k, sep, v = a.partition("=")
            if k in ("--output_dir", "--policy.path", "--ckpt") and v:
                rv = os.path.realpath(v)
                if rv == run_path or rv.startswith(run_path + os.sep):
                    return True
        return False
    for j in jobs_index():
        if j["alive"] and uses_run(j):
            return JSONResponse({"error": f"실행 중인 작업({j['id']})이 이 출력을 사용 중"}, status_code=400)
    if scope == "run":
        raw = OUT_ROOT / run
    else:
        step = rel.split("/checkpoints/")[1].split("/")[0]
        if step == "last":
            return JSONResponse({"error": "last는 심볼릭 링크 — 숫자 체크포인트를 선택하세요"}, status_code=400)
        raw = OUT_ROOT / run / "checkpoints" / step
    if raw.is_symlink():
        return JSONResponse({"error": "심볼릭 링크는 삭제하지 않음"}, status_code=400)
    target = raw.resolve()
    # rel='/checkpoints/x' 처럼 run 이 비면 target 이 outputs 자체가 됩니다 — 반드시 막습니다
    if (not run.strip("/") or not _within(target, OUT_ROOT) or target == OUT_ROOT.resolve()
            or not target.exists()):
        return JSONResponse({"error": "대상 없음"}, status_code=400)
    await asyncio.to_thread(shutil.rmtree, target)     # 수 GB 면 수 초 — 이벤트 루프(E-STOP 포함)를 막지 않게
    if scope == "run":
        assign_to_project("models", run, "")
    return {"ok": True}


# ----------------------------- 페이지: Control (수동 제어) --------------------
@app.get("/control", response_class=HTMLResponse)
def control_page():
    busy = busy_with(("record", "rollout")) or (
        {"id": "port-watch (셋업 탭)"} if WATCH.on else None) or (
        {"id": "calibration (캘리브레이션 탭)"} if CALIB.active else None) or (
        {"id": "motor-id-setup (셋업 탭)"} if MOTORSETUP.active else None) or (
        {"id": "arm-check (셋업 탭)"} if ARMCHECK.active else None) or (
        {"id": "verify (셋업 마법사)"} if VERIFY.active else None)
    if busy:
        return f"""{CSS}{nav_html('ct')}<div class=wrap>
        <p class=eyebrow>수동 제어</p><h2>제어</h2>
        <p class="badge b-warn">실행 중: {esc(busy["id"])} — 끝나야 수동 제어를 쓸 수 있습니다</p></div>"""
    if not ports_configured():
        return f"""{CSS}{nav_html('ct')}<div class=wrap>
        <p class=eyebrow>수동 제어</p><h2>제어</h2>
        {setup_needed_html()}</div>"""
    missing = [f"{sd} {role}" if BIMANUAL else role
               for sd, roles in calib_status().items() for role, c in roles.items() if not c["ok"]]
    if missing:
        return f"""{CSS}{nav_html('ct')}<div class=wrap>
        <p class=eyebrow>수동 제어</p><h2>제어</h2>
        {need_html("먼저 캘리브레이션을 하세요", f"캘리브레이션 파일이 없는 팔이 있습니다 ({esc(', '.join(missing))}) — "
                   "셋업 마법사에서 이어서 하면 됩니다.", "/setup/wizard", "셋업 마법사로")}</div>"""
    cam_panels = "".join(
        f'<div class=cw><span class=cl>{esc(n)}</span><img id="cam_{esc(n)}"></div>'
        for n in CAM_SPECS)
    if not CAM_SPECS:
        cam_panels = ""
    # f-string 표현식 안에서는 {{ 가 이스케이프가 아니라 실제 중괄호라 집합이 됩니다.
    # dict 리터럴은 f-string 밖에서 만들어야 합니다.
    views_js = js({sd: (a.get("view") or {"x": 0.0, "y": 0.0, "yaw_deg": 0.0})
                   for sd, a in ARM_CFGS.items()})
    arm_panels = "".join(
        f'<div class=armbox data-side="{esc(s)}">'
        f'{"<div class=armhead>" + esc(s) + "</div>" if BIMANUAL else ""}'
        f'<div class=sliders id="sl_{esc(s)}"></div></div>'
        for s in SIDES)
    return f"""{CSS}{nav_html('ct')}
<style>
.cmain{{display:grid;grid-template-columns:360px 1fr;gap:0;height:calc(100vh - 52px)}}
.cpanel{{border-right:1px solid var(--line);padding:18px;overflow-y:auto}}
.armhead{{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
color:var(--dim);margin:14px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.jrow{{margin-bottom:16px}}
.jhead{{display:flex;justify-content:space-between;font-family:var(--mono);font-size:12px;margin-bottom:4px}}
.jhead .n{{color:var(--text)}} .jhead .v{{color:var(--accent)}} .jhead .a{{color:var(--dim)}}
.jhead .a.stuck{{color:var(--bad);font-weight:600}}
.jrow.stuck .n{{color:var(--bad)}}
#warnbox p{{margin:0 0 8px}}
input[type=range]{{width:100%;accent-color:var(--accent)}}
#right{{display:flex;flex-direction:column;min-height:0}}
.cams{{display:none;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:1px;
background:var(--line);border-bottom:1px solid var(--line)}}
.cams .cw{{position:relative;background:#000}}
.cams img{{width:100%;display:block;max-height:220px;object-fit:contain;background:#000}}
.cams .cl{{position:absolute;top:6px;left:10px;font-family:var(--mono);font-size:11px;
letter-spacing:.1em;text-transform:uppercase;color:#cfd8e3;text-shadow:0 0 4px #000}}
#view{{position:relative;background:#0a0d10;min-height:300px;flex:1}}
#view canvas{{display:block}}
#nourdf{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
color:var(--dim);font-family:var(--mono);font-size:12px;text-align:center;line-height:2}}
button.estop{{background:#4a2020;border-color:var(--bad);color:#ffc9c9;font-family:var(--mono);font-weight:600}}
@media(max-width:820px){{
  .cmain{{grid-template-columns:1fr;height:auto}}
  .cpanel{{border-right:none;border-bottom:1px solid var(--line);overflow:visible}}
  #right{{min-height:auto}}
  #view{{min-height:56vh}}
}}
</style>
<div class=cmain>
  <div class=cpanel>
    <div class=toolbar>
      <span id=cst class=badge>연결 중…</span>
      <button id=breconn class=primary onclick="reconnect()" style="display:none">다시 연결</button>
      <button id=btrq onclick="toggleTorque()" disabled>토크 ON</button>
      <button id=bflw onclick="toggleFollow()" disabled>리더 팔로우 ON</button>
      <button class=estop onclick="estop()">E-STOP</button>
      <input type=color id=armcolor value="#ffffff" title="로봇 색상"
             style="width:34px;height:30px;padding:2px;border-radius:7px;border:1px solid var(--line);background:var(--bg);cursor:pointer">
    </div>
    <div id=warnbox></div>
    {arm_panels}
    <p class=muted>실측값 옆에 <b>오차</b>가 빨갛게 뜨면 그 관절이 명령을 못 따라가는 것입니다
    (막힘·과부하 보호). 서보 온도도 1초마다 확인합니다.<br>
    토크 OFF: 손으로 움직이면 값·3D가 따라옵니다.<br>
    토크 ON: 슬라이더가 목표 (스텝당 최대 {MAX_STEP_DEG}° 제한).<br>
    리더 팔로우: 리더 암을 손으로 움직이면 팔로워가 실시간 미러링 (슬라이더 잠금).<br>
    이 탭에 들어오면 <b>자동으로 연결</b>합니다 — 따로 누를 버튼이 없습니다.
    떠나면 자동으로 토크 해제 + 연결 해제됩니다.<br>
    <b>한 번에 한 탭만</b> 팔을 잡을 수 있습니다. 다른 창에 제어 탭이 열려 있으면 그 창을 먼저 닫으세요.
    수집 / 실행 / 캘리브레이션 / 포트 감시가 돌고 있어도 연결되지 않습니다.</p>
  </div>
  <div id=right>
    <div class=cams id=cams>{cam_panels}</div>
    <div id=view><div id=nourdf>{esc(URDF_DIR)}/{esc(kind()["urdf"])} 없음<br>
    URDF와 meshes/ 를 복사하면 3D 표시<br>(슬라이더 제어는 그대로 동작)</div></div>
  </div>
</div>
<script type="importmap">
{{"imports":{{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/",
"three/examples/jsm/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/",
"urdf-loader":"https://cdn.jsdelivr.net/npm/urdf-loader@0.12.6/src/URDFLoader.js"}}}}
</script>
<script type="module">
const JOINTS = {js(CTL_JOINTS)};
const SIDES  = {js(SIDES)};
const VIEWS  = {views_js};
const K3     = {js(kind3d())};   // 기종별 URDF·관절 이름·단위 (SO-ARM101 / OMX)
const CAMS   = {js(list(CAM_SPECS))};
let ws=null, torque=false, follow=false;
const robots={{}};                       // side -> URDF root
const sliders={{}}, valEls={{}}, actEls={{}};   // side -> joint -> el
const cst=document.getElementById('cst'), btrq=document.getElementById('btrq');
const bflw=document.getElementById('bflw'), breconn=document.getElementById('breconn');
let wsErr='';                     // 서버가 보낸 진짜 사유. onclose 가 덮어쓰지 못하게 보관합니다.
function setStatus(t,cls){{ cst.textContent=t; cst.className='badge '+(cls||''); }}

function buildSliders(side, lims, actual){{
  const box=document.getElementById('sl_'+side); box.innerHTML='';
  sliders[side]={{}}; valEls[side]={{}}; actEls[side]={{}};
  JOINTS.forEach(j=>{{
    const [lo,hi]=lims[j];
    const row=document.createElement('div'); row.className='jrow';
    row.innerHTML=`<div class=jhead><span class=n>${{j}}</span>
      <span><span class=v>-</span> <span class=a>(-)</span></span></div>
      <input type=range min=${{lo}} max=${{hi}} step=0.5 value=${{actual[j]??0}}>`;
    box.appendChild(row);
    const s=row.querySelector('input');
    sliders[side][j]=s;
    valEls[side][j]=row.querySelector('.v');
    actEls[side][j]=row.querySelector('.a');
    s.addEventListener('input',()=>{{
      if(ws&&ws.readyState===1){{
        const joints={{}}; JOINTS.forEach(k=>joints[k]=parseFloat(sliders[side][k].value));
        ws.send(JSON.stringify({{type:'target',side:side,joints}}));
      }}
    }});
  }});
}}
window.toggleTorque=()=>{{ if(ws&&ws.readyState===1) ws.send(JSON.stringify({{type:'torque',on:!torque}})); }};
window.toggleFollow=()=>{{ if(ws&&ws.readyState===1) ws.send(JSON.stringify({{type:'follow',on:!follow}})); }};
window.estop=()=>{{ if(ws&&ws.readyState===1) ws.send(JSON.stringify({{type:'estop'}})); }};

window.reconnect=()=>{{ try{{ws&&ws.close();}}catch(e){{}} openWS(); }};

function openWS(){{
  wsErr=''; setStatus('연결 중…',''); breconn.style.display='none';
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws/control');
  ws.onerror=()=>{{ if(!wsErr) wsErr='서버에 닿지 못했습니다 — OpenMimic 이 떠 있는지 확인하세요'; }};
  ws.onmessage=e=>{{
    const d=JSON.parse(e.data);
    if(d.type==='init'){{
      if(d.error){{ wsErr=d.error; setStatus(d.error,'b-bad'); return; }}
      SIDES.forEach(s=>buildSliders(s, d.arms[s].limits, d.arms[s].actual));
      setStatus('연결됨','b-ok');
      btrq.disabled=false; bflw.disabled=false;
      const errs=d.cam_errors||{{}};
      if((d.cams&&d.cams.length)||Object.keys(errs).length){{
        document.getElementById('cams').style.display='grid';
      }}
      (d.cams||[]).forEach(n=>{{
        const img=document.getElementById('cam_'+n);
        if(img) img.src='/stream/'+encodeURIComponent(n);
      }});
      // 못 연 카메라는 검은 화면으로 두지 말고 이유를 적습니다.
      // Setup 에서는 한 대씩 열어 보니 되고 여기서는 안 되는 경우가 많은데,
      // 대개 USB 대역폭이 모자라거나 두 항목이 같은 장치를 가리킨 것입니다.
      Object.keys(errs).forEach(n=>{{
        const img=document.getElementById('cam_'+n);
        if(!img) return;
        const box=img.parentElement;
        img.remove();
        const d2=document.createElement('div');
        d2.style.cssText='padding:26px 12px;color:#e08a8a;font-family:var(--mono);'
          +'font-size:11px;line-height:1.7;text-align:center';
        d2.textContent='열기 실패 — '+errs[n];
        box.appendChild(d2);
      }});
    }}
    if(d.type==='state'){{
      torque=d.torque; follow=!!d.follow;
      btrq.textContent=torque?'토크 OFF':'토크 ON';
      btrq.disabled=follow;
      bflw.textContent=follow?'리더 팔로우 OFF':'리더 팔로우 ON';
      bflw.classList.toggle('primary',follow);
      let warn='';
      const esc=t=>String(t).replace(/[&<>"]/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]));
      SIDES.forEach(side=>{{
        const a=d.arms[side]; if(!a||!sliders[side])return;
        const stuck=a.stuck||[], hot=a.hot||[];
        const tag=SIDES.length>1?(side+' '):'';
        if(stuck.length){{
          const dg=a.diag||{{}};
          const lines=stuck.map(j=>{{
            const d=dg[j]||{{}};
            return '<br>· <b>'+j+'</b>: '+(d.why||'')
              +' <span class=mono style="font-size:11px">(Torque_Enable='+(d.torque_enable==null?'?':d.torque_enable)
              +', load='+(d.load==null?'?':d.load)+', '+(d.temp==null?'?':d.temp+'°C')+')</span>';
          }}).join('');
          warn+='<p class="badge b-bad">'+tag+'명령을 못 따라가는 관절'+lines+'</p>';
        }}
        if(hot.length) warn+='<p class="badge '+(a.maxtemp>=65?'b-bad':'b-warn')+'">'+tag
          +'서보 온도 '+a.maxtemp+'°C: '+hot.join(', ')+(a.maxtemp>=65?' — 즉시 토크를 끄세요':'')+'</p>';
        if(a.health_stop) warn+='<p class="badge b-bad">'+tag+'서보 상태 이상 — 리더 팔로우·슬라이더를 멈췄습니다: '
          +esc(a.health_stop)+'</p>';
        else if(a.health&&a.health.level==='warn'&&a.health.msgs.length)
          warn+='<p class="badge b-warn">'+tag+'서보 상태: '+a.health.msgs.map(esc).join(' · ')+'</p>';
        if(a.power_warn&&a.power_warn.length)
          warn+='<p class="badge b-warn">'+tag+'전원 점검: '+a.power_warn.map(esc).join(' · ')+'</p>';
        JOINTS.forEach(j=>{{
          if(valEls[side][j])valEls[side][j].textContent=(a.target[j]??0).toFixed(1);
          if(actEls[side][j]){{
            const bad=stuck.indexOf(j)>=0;
            actEls[side][j].textContent='('+(a.actual[j]??0).toFixed(1)
              +(bad?', 오차 '+(a.track[j]>0?'+':'')+a.track[j]:'')+')';
            actEls[side][j].classList.toggle('stuck',bad);
            const row=actEls[side][j].closest('.jrow');
            if(row) row.classList.toggle('stuck',bad);
          }}
          const s=sliders[side][j]; if(!s)return;
          s.disabled=follow;
          if(follow){{
            if(a.target[j]!==undefined)s.value=a.target[j];
          }}else{{
            if(!torque&&a.actual[j]!==undefined)s.value=a.actual[j];
            if(torque&&d.synced&&a.actual[j]!==undefined)s.value=a.actual[j];
          }}
        }});
        updateRobot(side,a.actual);
      }});
      document.getElementById('warnbox').innerHTML=warn;
      if(d.err){{ if(cst.textContent!=='bus error') setStatus('bus error','b-bad'); }}
      else if(cst.textContent==='bus error') setStatus('연결됨','b-ok');     // 오류가 풀리면 배지도 되돌림
    }}
  }};
  // 서버는 오류를 보낸 직후 소켓을 닫습니다. 사유를 지우지 말 것 —
  // 여기서 덮어쓰면 화면엔 'disconnected' 만 남아 원인을 알 수 없습니다.
  ws.onclose=()=>{{
    setStatus(wsErr||'연결 끊김 — 다시 연결을 누르세요','b-bad');
    btrq.disabled=true; bflw.disabled=true; breconn.style.display='';
  }};
}}
addEventListener('pagehide',()=>{{ try{{ws&&ws.close();}}catch(e){{}} }});
openWS();

// ---------- Three.js URDF ----------
let updateRobot=()=>{{}};
function showViewMsg(t){{
  const el=document.getElementById('nourdf');
  el.style.display='flex'; el.innerHTML=t;
}}
(async()=>{{
  const st=await (await fetch('/api/ctlstate')).json();
  if(!st.urdf) return;
  try{{
  document.getElementById('nourdf').style.display='none';
  const THREE=await import('three');
  const {{OrbitControls}}=await import('three/addons/controls/OrbitControls.js');
  const URDFLoader=(await import('urdf-loader')).default;
  const view=document.getElementById('view');
  const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0a0d10);
  const cam=new THREE.PerspectiveCamera(50,1,0.01,10);
  const zoom=SIDES.length>1?1.7:1.0;
  cam.position.set(0.4*zoom,0.35*zoom,0.4*zoom);
  const ren=new THREE.WebGLRenderer({{antialias:true}}); view.appendChild(ren.domElement);
  const ctl=new OrbitControls(cam,ren.domElement); ctl.target.set(0,0.12,0);
  scene.add(new THREE.HemisphereLight(0xffffff,0x223344,1.1));
  const dl=new THREE.DirectionalLight(0xffffff,1.2); dl.position.set(1,2,1); scene.add(dl);
  scene.add(new THREE.GridHelper(SIDES.length>1?1.6:1, SIDES.length>1?32:20, 0x28303a,0x1b222a));
  function resize(){{const w=view.clientWidth,h=view.clientHeight;ren.setSize(w,h);cam.aspect=w/h;cam.updateProjectionMatrix();}}
  new ResizeObserver(resize).observe(view); resize();
  const picker=document.getElementById('armcolor');
  try{{ picker.value=localStorage.getItem('armColor2')||'#ffffff'; }}catch(e){{ picker.value='#ffffff'; }}
  function applyColor(hex){{
    Object.values(robots).forEach(r=>r.traverse(o=>{{
      if(!o.isMesh) return;
      if(!o.userData.recolored){{
        // URDF 가 이미 본체(3d_printed)와 서보(sts3215)를 material 이름으로
        // 나눠 놨습니다. 교체하면 이름이 사라지니 먼저 적어 둡니다.
        o.userData.urdfMat = (o.material && o.material.name) || '';
        o.material=new THREE.MeshStandardMaterial({{metalness:0.15,roughness:0.55}});
        o.userData.recolored=true;
      }}
      if(K3.motor_mat && o.userData.urdfMat===K3.motor_mat){{
        // 서보는 URDF 색(0.1,0.1,0.1)을 그대로 둡니다 — 다 같은 색이면 형태가 안 보입니다.
        o.material.color.set('#1a1a1a');
        o.material.roughness=0.35;
      }} else {{
        o.material.color.set(hex);
      }}
    }}));
    try{{ localStorage.setItem('armColor2',hex); }}catch(e){{}}
  }}
  picker.addEventListener('input',()=>applyColor(picker.value));
  // URDFLoader.load 의 콜백은 parse 직후에 불립니다 — STL 은 아직 로딩 중입니다.
  // 그래서 여기서 칠하면 빈 트리를 칠하는 꼴이고, 나중에 도착한 메시가
  // URDF 자체 material(3d_printed = 노랑)을 그대로 들고 옵니다.
  // LoadingManager 가 비는 시점에 다시 칠해야 합니다.
  const mgr=new THREE.LoadingManager();
  mgr.onLoad=()=>applyColor(picker.value);
  SIDES.forEach((side,i)=>{{
    const loader=new URDFLoader(mgr);
    loader.workingPath='/urdf/';
    loader.packages='/urdf';           // package://xxx/ 형태도 /urdf/로 해석
    loader.load(K3.urdf,
      r=>{{
        // URDF 는 Z-up / X-forward. -90° 눕히면 URDF X → three X(앞), URDF Y → three -Z(왼쪽).
        // Euler 'XYZ' 는 R = Rx·Ry·Rz 라 z 성분이 먼저 적용됨 → URDF 기준 yaw 가 됩니다.
        const v=VIEWS[side]||{{x:0,y:0,yaw_deg:0}};
        r.rotation.set(-Math.PI/2, 0, v.yaw_deg*Math.PI/180);
        r.position.set(v.x, 0, -v.y);        // 앞뒤=X, 좌우=−Z
        robots[side]=r; scene.add(r); applyColor(picker.value);
      }},
      undefined,
      e=>{{console.error(e);showViewMsg('URDF 로드 실패<br>'+(e?.message||e));}});
  }});
  (function anim(){{requestAnimationFrame(anim);ctl.update();ren.render(scene,cam);}})();
  updateRobot=(side,actual)=>{{
    const robot=robots[side];
    if(!robot||!actual)return;
    JOINTS.forEach(j=>{{
      const jt=robot.joints?.[K3.map[j]||j]; if(!jt)return;
      const v=actual[j]; if(v===undefined)return;
      if(j==='gripper'){{
        const g=K3.gripper, lo=g?g.lo:(jt.limit?.lower??0), hi=g?g.hi:(jt.limit?.upper??1);
        jt.setJointValue(lo+(hi-lo)*(v/100));
      }}else jt.setJointValue((K3.sign[j]||1)*v*K3.scale);
    }});
  }};
  }}catch(e){{ console.error(e); showViewMsg('3D 초기화 실패<br>'+(e?.message||e)); }}
}})();
</script>"""


@app.get("/api/ctlstate")
def api_ctlstate():
    return {"connected": any_arm_connected(),
            "torque": all(a.torque for a in ARMS.values()) and any_arm_connected(),
            "sides": SIDES,
            "cams": list(CAM_SPECS),
            "urdf": (URDF_DIR / kind()["urdf"]).exists()}


def _arms_state():
    out = {}
    for s, a in ARMS.items():
        hot = [n for n, t in a.temp.items() if t >= TEMP_WARN_C]
        dpu = kind()["deg_per_unit"]          # 오차 기준은 ° — OMX 단위(-100~100)로 환산
        stuck = ([n for n, e in a.track.items()
                  if abs(e) > (TRACK_WARN_DEG if n == "gripper" else TRACK_WARN_DEG / dpu)]
                 if a.torque else [])
        diag = {}
        for n in stuck:
            te, ld = a.ten.get(n), a.load.get(n)
            if te == 0:
                why = "서보가 토크를 뺐습니다 (과부하 보호) — 토크를 껐다 다시 켜 보고, 안 풀리면 전원 재투입"
            elif te == 1 and ld is not None and abs(ld) > 200:
                why = f"토크는 켜져 있는데 부하 {abs(ld)} — 기계적으로 막혀 버티는 중 (과열 위험)"
            elif te == 1:
                why = f"토크 ON, 부하 {abs(ld) if ld is not None else '?'} — 서보가 명령을 안 받음"
            else:
                why = "상태를 읽지 못함"
            diag[n] = {"torque_enable": te, "load": ld, "temp": a.temp.get(n), "why": why}
        out[s] = {"actual": a.actual, "target": a.target, "limits": a.limits,
                  "track": a.track, "temp": a.temp, "hot": hot, "stuck": stuck,
                  "diag": diag, "health": a.guard.state() if a.guard else None, "health_stop": a.health_stop,
                  "power_warn": a.power_warn + LEADERS[s].power_warn if s in LEADERS else a.power_warn,
                  "maxtemp": max(a.temp.values()) if a.temp else None}
    return out


@app.websocket("/ws/control")
async def ws_control(sock: WebSocket):
    global CTL_OWNER
    await sock.accept()
    if lang_of(sock.cookies) == "en":
        _send = sock.send_text

        async def _send_en(t):          # json.dumps 기본값은 한글을 \uXXXX 로 내보내므로 풀어서 바꿉니다
            await _send(to_en_json(t))

        sock.send_text = _send_en
    if not ws_authed(sock):
        msg = ("다른 사이트에서 온 연결은 받지 않습니다" if not _same_origin(sock.headers)
               else "인증 필요 — 페이지를 새로고침하세요")
        await sock.send_text(json.dumps({"type": "init", "error": msg}))
        await sock.close()
        return
    if (busy_with(("record", "rollout")) or WATCH.on or CALIB.active or MOTORSETUP.active
            or ARMCHECK.active or VERIFY.active):
        await sock.send_text(json.dumps({"type": "init", "error": "record/rollout/Setup/Calib 사용 중 — 제어 불가"}))
        await sock.close()
        return
    if CTL_OWNER is not None:
        await sock.send_text(json.dumps({"type": "init", "error": "다른 브라우저가 제어 중입니다"}))
        await sock.close()
        return
    CTL_OWNER = sock
    try:
        try:
            for arm in ARMS.values():
                if not arm.connected:
                    await asyncio.to_thread(arm.connect)
        except Exception as e:
            for arm in ARMS.values():
                await asyncio.to_thread(arm.disconnect)
            await sock.send_text(json.dumps({"type": "init", "error": f"팔 연결 실패: {e}"}))
            return

        await asyncio.to_thread(CAMS.open)
        await sock.send_text(json.dumps({
            "type": "init", "arms": _arms_state(),
            "cams": list(CAMS.cams),      # 실제로 열린 카메라만
            "cam_errors": dict(CAMS.errors),
        }))

        synced = False   # 토크 토글 직후 슬라이더 동기화 신호 1회

        def _set_torque_all(on):
            # 팔마다 따로 — 한 팔이 실패해도 나머지 팔은 반드시 처리합니다
            errs = []
            for a in ARMS.values():
                try:
                    a.set_torque(on)
                except Exception as e:
                    errs.append(f"{a.side}: {e}")
            if errs:
                raise RuntimeError(" / ".join(errs))

        def _estop():
            # 팔로워 토크부터 끕니다. 리더 정리는 그다음 (전원 없는 리더는 응답 대기로 늦어질 수 있음)
            for a in ARMS.values():
                a.follow = False
            err = None
            try:
                _set_torque_all(False)
            except Exception as e:
                err = e
            _leaders_off()
            if err:
                raise err

        def _leaders_off():
            for side, ldr in LEADERS.items():
                ARMS[side].follow = False
                ldr.disconnect()

        def _follow_on():
            for side, ldr in LEADERS.items():
                if not ldr.connected:
                    ldr.connect()
                if not ARMS[side].torque:
                    ARMS[side].set_torque(True)
                ARMS[side].follow = True

        for side, arm in ARMS.items():
            arm.start_loop(LEADERS[side])

        # 토크·팔로우 버튼 실패 문구 — 팔 루프가 매 주기 a.err 를 지우므로 따로 잡아 두고 몇 초 보여 줍니다
        note = {"m": "", "t": -1e9}

        def _note(e):
            note.update(m=str(e), t=time.monotonic())

        async def rx():
            nonlocal synced
            async for msg in sock.iter_text():
                try:
                    d = json.loads(msg)
                except ValueError:
                    continue
                if not isinstance(d, dict):
                    continue
                kind = d.get("type")
                if kind == "target":
                    arm = ARMS.get(d.get("side"))
                    js_ = d.get("joints")
                    if arm and arm.health_stop:
                        continue                   # 서보 상태 이상으로 멈춘 동안은 슬라이더 명령을 받지 않음
                    if arm and isinstance(js_, dict):
                        for k, v in js_.items():
                            if k in CTL_JOINTS:
                                try:
                                    arm.target[k] = float(v)
                                except (TypeError, ValueError):
                                    pass
                elif kind == "torque":
                    try:
                        await asyncio.to_thread(_set_torque_all, bool(d.get("on")))
                        synced = True
                    except Exception as e:
                        _note(e)
                elif kind == "estop":
                    try:
                        await asyncio.to_thread(_estop)
                    except Exception as e:
                        _note(e)
                elif kind == "follow":
                    on = bool(d.get("on"))
                    try:
                        if on:
                            await asyncio.to_thread(_follow_on)
                        else:
                            await asyncio.to_thread(_leaders_off)
                        synced = True
                    except Exception as e:
                        _note(e)
                        try:
                            await asyncio.to_thread(_leaders_off)
                        except Exception:
                            pass

        rx_task = asyncio.create_task(rx())
        try:
            while True:
                t0 = time.monotonic()
                any_arm = _first_arm()
                err = next((a.err for a in ARMS.values() if a.err), "")
                if not err and t0 - note["t"] < NOTE_HOLD_S:
                    err = note["m"]
                await sock.send_text(json.dumps({
                    "type": "state", "arms": _arms_state(),
                    "torque": any_arm.torque, "follow": any_arm.follow,
                    "synced": synced, "err": err,
                }))
                synced = False
                await asyncio.sleep(max(0.0, 1.0 / CONTROL_HZ - (time.monotonic() - t0)))
        finally:
            rx_task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        def _cleanup():
            global CTL_OWNER
            try:
                CAMS.close()              # 탭 이탈 = 카메라 해제
                for arm in ARMS.values():
                    arm.stop_loop()
                for arm in ARMS.values():
                    arm.disconnect()      # 토크 해제 + 시리얼 해제 (팔로워 먼저)
                for ldr in LEADERS.values():
                    ldr.disconnect()
            finally:
                CTL_OWNER = None          # 정리가 끝난 뒤에야 다른 작업이 포트를 잡을 수 있습니다
        # 시리얼 재시도·스레드 join 이 이벤트 루프를 몇 초씩 막지 않도록 스레드에서 돌립니다.
        # run_in_executor 는 부르는 즉시 스레드가 시작되므로, 이 태스크가 취소돼도(서버 종료 등) 정리는 끝까지 갑니다.
        fut = asyncio.get_running_loop().run_in_executor(None, _cleanup)
        await asyncio.shield(fut)


def _first_arm():
    return ARMS[SIDES[0]]


# Control 카메라 MJPEG 스트림
def _mjpeg_from_files(jid, cam):
    """record worker 가 RUN_DIR 에 떨어뜨리는 JPEG 을 mtime 이 바뀔 때마다 흘려보냅니다."""
    boundary = b"--frame"
    f = run_dir(jid) / f"cam_{cam}.jpg"
    jf = JOB_DIR / f"{jid}.json"

    def gen():
        last_m, last_sent = 0.0, 0.0
        idle = 0
        while True:
            try:
                m = f.stat().st_mtime
            except OSError:
                m = 0.0
            now = time.monotonic()
            if m and (m != last_m or now - last_sent > 1.0):
                try:
                    data = f.read_bytes()
                except OSError:
                    data = b""
                if data:
                    last_m, last_sent = m, now
                    yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                           + f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
            idle += 1
            if idle % 20 == 0 and not job_alive(load_json(jf, {})):
                break
            time.sleep(1.0 / PREVIEW_FPS)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/stream/{cam}")
def stream_cam(cam: str):
    if not CAMS.on:
        rec = next((j for j in jobs_index() if j["kind"] == "record" and j["alive"]), None)
        if rec and (cam in CAM_SPECS or safe_name(cam)):
            return _mjpeg_from_files(rec["id"], cam)
        return JSONResponse({"error": "카메라 미가동 (제어 탭 또는 수집 중에만 스트리밍)"}, status_code=503)
    if cam not in CAM_SPECS:
        return JSONResponse({"error": "unknown camera"}, status_code=404)
    boundary = b"--frame"

    def gen():
        last, last_sent = None, 0.0
        while CAMS.on:
            f = CAMS.frames.get(cam)
            now = time.monotonic()
            # 새 프레임이면 보내고, 카메라가 멈춰도 1초마다 한 번은 재전송해
            # 브라우저가 연결을 끊지 않게 합니다.
            if f is not None and (f is not last or now - last_sent > 1.0):
                last, last_sent = f, now
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\n"
                       + f"Content-Length: {len(f)}\r\n\r\n".encode() + f + b"\r\n")
            time.sleep(1.0 / CTL_STREAM_FPS)

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


# URDF 정적 서빙
@app.get("/urdf/{rest:path}")
def serve_urdf(rest: str):
    p = (URDF_DIR / rest).resolve()
    if not _within(p, URDF_DIR) or not p.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p)


# ----------------------------- 페이지: Setup (포트/카메라) --------------------
def calib_status():
    """설정된 id 별 캘리브레이션 파일 존재 여부 (지금 기종 폴더 기준)."""
    out = {}
    for side, arm in ARM_CFGS.items():
        out[side] = {}
        for role in ("follower", "leader"):
            f = calib_file(role, arm[f"{role}_id"])
            out[side][role] = {"id": arm[f"{role}_id"], "path": str(f), "ok": f.is_file()}
    return out


def _validate_cam(name, spec, seen):
    if not safe_name(name):
        return f"카메라 이름은 영문/숫자/._- 만: {name!r}"
    if name in seen:
        return f"카메라 이름 중복: {name}"
    seen.add(name)
    if not isinstance(spec, dict):
        return f"카메라 {name} 형식 오류"
    idx = spec.get("index_or_path")
    if isinstance(idx, str):
        if not idx.startswith("/dev/") and openmimic_simcam.sim_cam_name(idx) is None:
            return f"카메라 {name} 경로는 /dev/ 로 시작해야 합니다"
    elif not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
        return f"카메라 {name} 인덱스가 잘못됨"
    for k, lo, hi in (("width", 32, 8192), ("height", 32, 8192), ("fps", 1, 240)):
        v = spec.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
            return f"카메라 {name} 의 {k} 값이 잘못됨"
    if spec.get("fourcc") not in (None, "", *CAM_FOURCCS):
        return f"카메라 {name} 의 FOURCC 는 {', '.join(CAM_FOURCCS)} 중 하나"
    return None


# 카메라 픽셀 형식. USB 카메라 여러 대를 한 허브에 꽂으면 YUYV(무압축)는 대역폭이 모자라 프레임이 끊깁니다 —
# 그때 MJPG 로 바꾸면 됩니다. 비우면 드라이버 기본값(lerobot 자동).
CAM_FOURCCS = ("MJPG", "YUYV")


def cam_fourcc(spec):
    f = (spec or {}).get("fourcc") or None
    return f if f in CAM_FOURCCS else None


def cam_fourcc_kw(spec):
    """정했을 때만 넘깁니다 — 지정 안 한 카메라는 예전과 똑같이 만들어집니다."""
    f = cam_fourcc(spec)
    return {"fourcc": f} if f else {}


def validate_config(cfg):
    """저장 전 검증. 문제가 있으면 메시지를, 없으면 None 을 반환합니다."""
    if not isinstance(cfg, dict):
        return "설정 형식 오류"
    mode = cfg.get("mode")
    if mode not in ("single", "bimanual"):
        return "mode 는 single 또는 bimanual"
    if cfg.get("robot", DEFAULT_ROBOT) not in ROBOT_KINDS:
        return f"robot 은 {' / '.join(ROBOT_KINDS)} 중 하나"
    arms = cfg.get("arms")
    want = 2 if mode == "bimanual" else 1
    if not isinstance(arms, list) or len(arms) != want:
        return f"mode={mode} 인데 arms 가 {len(arms) if isinstance(arms, list) else '?'}개입니다 (필요: {want})"
    sides = [a.get("side") for a in arms if isinstance(a, dict)]
    expect = {"main"} if want == 1 else {"left", "right"}
    if set(sides) != expect or len(set(sides)) != want:
        return f"mode={mode} 의 side 는 {sorted(expect)} 여야 합니다"
    if mode == "bimanual":
        try:
            by_side = {a.get("side"): a for a in arms}
            bimanual_base_id("follower", by_side)
            bimanual_base_id("leader", by_side)
        except (KeyError, TypeError, AttributeError):
            return "양팔 id 형식 오류"
        except ValueError as e:
            return str(e)
    seen_ports, cam_names, calib_ids, raw_arm_cams = {}, set(), {}, set()
    for arm in arms:
        if not isinstance(arm, dict):
            return "arms 원소 형식 오류"
        side = arm.get("side", "")
        if not safe_name(side):
            return f"side 이름이 잘못됨: {side!r}"
        for role in ("follower", "leader"):
            cid = arm.get(f"{role}_id", "")
            if not safe_name(cid):
                return f"{side}/{role} id 는 영문/숫자/._- 만"
            key = (role, cid)
            if key in calib_ids:
                return f"{role} id 중복: {cid} — 캘리브레이션 파일이 겹칩니다"
            calib_ids[key] = side
            port = (arm.get(f"{role}_port") or "").strip()
            if not port:
                continue
            if not serial_path_ok(port):
                return f"{side}/{role} 포트는 /dev/… (또는 가상 팔 openmimic_sim/…) 경로여야 합니다: {port}"
            real = os.path.realpath(port)
            if real in seen_ports:
                return f"같은 포트를 두 곳에 지정했습니다: {port} ({seen_ports[real]} 와 중복)"
            seen_ports[real] = f"{side}/{role}"
        v = arm.get("view") or {}
        for k, lim in (("x", 2.0), ("y", 2.0), ("yaw_deg", 360.0)):
            val = v.get(k, 0)
            if not isinstance(val, (int, float)) or isinstance(val, bool) or abs(float(val)) > lim:
                return f"{side} 3D 배치 {k} 값이 범위를 벗어났습니다 (±{lim:g})"
        # 양팔에서는 팔 카메라 이름에 side 접두사가 붙으므로 팔끼리 같은 이름(wrist)이 허용됩니다
        arm_cam_names = set()
        for name, spec in (arm.get("cameras") or {}).items():
            err = _validate_cam(name, spec, arm_cam_names)
            if err:
                return err
            raw_arm_cams.add(name)
            full = f"{side}_{name}" if len(arms) > 1 else name
            if full in cam_names:
                return f"카메라 이름 충돌: {full}"
            cam_names.add(full)
    for name, spec in (cfg.get("cameras") or {}).items():
        err = _validate_cam(name, spec, set())
        if err:
            return err
        if name in cam_names:
            return f"공용 카메라 이름이 팔 카메라와 충돌: {name}"
        # BiSOFollower 는 공용 카메라를 왼팔이 같이 엽니다 — 팔 카메라 원래 이름(wrist)과 같으면 거부됩니다
        if mode == "bimanual" and name in raw_arm_cams:
            return f"공용 카메라 이름 {name} 이 팔 카메라 이름과 같습니다 — lerobot 양팔 규칙상 쓸 수 없습니다"
        cam_names.add(name)
    try:
        if not 1 <= int(cfg.get("fps", 30)) <= 120:
            raise ValueError
    except (TypeError, ValueError):
        return "fps 는 1~120"
    mrt = cfg.get("max_relative_target")
    if mrt is not None and not (isinstance(mrt, (int, float)) and not isinstance(mrt, bool)
                                and 0 < float(mrt) <= 180):
        return "max_relative_target 은 비우거나 0~180 사이의 수"
    return None


@app.get("/setup", response_class=HTMLResponse)
def setup_page():
    return CSS + nav_html("st") + SETUP_HTML.replace("<p class=eyebrow id=envcard>",
                                                     sim_card_html() + "<p class=eyebrow id=envcard>", 1)


@app.get("/api/setup/state")
def api_setup_state():
    busy = exclusive_busy()
    return {"config": CFG, "ports": list_serial_ports(), "calib": calib_status(),
            "busy": f"{busy['id']} 실행 중" if busy else ""}


@app.get("/api/setup/ports")
def api_setup_ports():
    return {"ports": list_serial_ports()}


@app.get("/api/setup/cameras")
def api_setup_cameras():
    if CAMS.on or busy_with(("record", "rollout")):
        return JSONResponse({"error": "카메라 사용 중 — Control/수집을 먼저 종료하세요"}, status_code=400)
    return {"cameras": list_video_devices()}


@app.post("/api/setup/probe")
async def api_setup_probe(req: Request):
    b = await req.json()
    port = (b.get("port") or "").strip()
    if not serial_path_ok(port):
        return JSONResponse({"error": "포트는 /dev/… (또는 가상 팔 openmimic_sim/…) 경로여야 합니다"}, status_code=400)
    if WATCH.on:
        return JSONResponse({"error": "포트 감시 중에는 probe 불가 — 감시를 먼저 중지하세요"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중"}, status_code=400)
    try:
        return await asyncio.to_thread(probe_port, port, bool(b.get("full")))
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)


@app.post("/api/setup/watch")
async def api_setup_watch_start(req: Request):
    b = await req.json()
    ports = [p for p in (b.get("ports") or []) if serial_path_ok(p)]
    if not ports:
        return JSONResponse({"error": "감시할 포트가 없습니다"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중"}, status_code=400)
    await asyncio.to_thread(WATCH.start, ports[:8])
    return {"ok": True, "ports": ports[:8]}


@app.get("/api/setup/watch")
def api_setup_watch_state():
    return {"on": WATCH.on, "state": WATCH.state}


@app.post("/api/setup/watch/stop")
async def api_setup_watch_stop():
    await asyncio.to_thread(WATCH.stop)
    return {"ok": True}


@app.get("/api/setup/camsnap")
def api_camsnap(dev: str):
    jpg = CAM_SNAPS.get(_snap_key(dev))
    if not jpg:
        return JSONResponse({"error": "스냅샷 없음 — 카메라 스캔을 먼저 하세요"}, status_code=404)
    return Response(content=jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/api/setup/motors")
def api_motors_state():
    return MOTORSETUP.state()


@app.post("/api/setup/motors/start")
async def api_motors_start(req: Request):
    b = await req.json()
    port = (b.get("port") or "").strip()
    if not serial_path_ok(port):
        return JSONResponse({"error": "포트는 /dev/… (또는 가상 팔 openmimic_sim/…) 경로여야 합니다"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중"}, status_code=400)
    try:
        role = b.get("role") if b.get("role") in ("follower", "leader") else "follower"
        await asyncio.to_thread(MOTORSETUP.start, port, role)
    except Exception as e:
        MOTORSETUP.stage = "error"
        MOTORSETUP.err = f"{type(e).__name__}: {e}"
        return JSONResponse({"error": MOTORSETUP.err}, status_code=400)
    return {"ok": True}


@app.post("/api/setup/motors/write")
async def api_motors_write():
    try:
        await asyncio.to_thread(MOTORSETUP.write_current)
        MOTORSETUP.err = ""
    except Exception as e:
        MOTORSETUP.err = f"{type(e).__name__}: {e}"
        return JSONResponse({"error": MOTORSETUP.err}, status_code=400)
    return {"ok": True, "last": MOTORSETUP.last}


@app.post("/api/setup/motors/cancel")
async def api_motors_cancel():
    await asyncio.to_thread(MOTORSETUP.cancel)
    return {"ok": True}


@app.get("/api/setup/armcheck")
def api_armcheck_state():
    return ARMCHECK.state()


@app.post("/api/setup/armcheck/start")
async def api_armcheck_start(req: Request):
    b = await req.json()
    port = (b.get("port") or "").strip()
    if not serial_path_ok(port):
        return JSONResponse({"error": "포트는 /dev/… (또는 가상 팔 openmimic_sim/…) 경로여야 합니다"}, status_code=400)
    role = b.get("role") if b.get("role") in ("leader", "follower") else ""
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중"}, status_code=400)
    try:
        await asyncio.to_thread(ARMCHECK.start, port, role, bool(b.get("sweep")))
    except Exception as e:
        ARMCHECK.fail(e)
        return JSONResponse({"error": str(e), "state": ARMCHECK.state()}, status_code=400)
    return {"ok": True, "state": ARMCHECK.state()}


@app.post("/api/setup/armcheck/finish")
async def api_armcheck_finish():
    try:
        await asyncio.to_thread(ARMCHECK.finish)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "state": ARMCHECK.state()}


@app.post("/api/setup/armcheck/cancel")
async def api_armcheck_cancel():
    await asyncio.to_thread(ARMCHECK.cancel)
    return {"ok": True, "state": ARMCHECK.state()}


# ----------------------------- 셋업 마법사 ------------------------------------
WIZARD_FILE = PROJ / "openmimic_wizard.json"     # {환경: {"side|role": {verified, verified_ts, port}}}


def _wiz_load():
    d = load_json(WIZARD_FILE, {})
    return d if isinstance(d, dict) else {}


def _stable_port(pinfo):
    """셋업 탭 stableOf 와 같은 규칙 — 보드에 USB 시리얼이 있으면 by-id, 없으면 by-path."""
    has_sn = bool((pinfo.get("usb") or {}).get("serial"))
    return ((pinfo.get("by_id") or pinfo.get("by_path")) if has_sn
            else (pinfo.get("by_path") or pinfo.get("by_id"))) or pinfo["dev"]


def _port_entry(ports, path):
    if not path:
        return None
    real = os.path.realpath(path)
    for pe in ports:
        if path in (pe["dev"], pe.get("by_id"), pe.get("by_path")) or real == pe["dev"]:
            return pe
    return None


def wizard_state():
    ports = list_serial_ports()
    w = _wiz_load().get(env_name(), {})
    cal = calib_status()
    slots = []
    for side, arm in ARM_CFGS.items():
        for role in ("follower", "leader"):
            port = arm.get(f"{role}_port") or ""
            pe = _port_entry(ports, port)
            c = cal[side][role]
            mtime = Path(c["path"]).stat().st_mtime if c["ok"] else 0
            v = w.get(f"{side}|{role}", {})
            fresh = bool(v.get("verified")) and v.get("verified_ts", 0) >= mtime and v.get("port") == port
            dg = ARMCHECK.history.get(port) if port else None
            # 보드 캘리브레이션 판정은 범위 기록형(SO-ARM101)만 — OMX 는 공장값이 곧 캘리브레이션
            ee = (dg or {}).get("eeprom") if kind()["calib"] == "range" else None
            fcmp, fdiff = file_calib_compare(c["path"], ee) if c["ok"] else ("none", [])
            slots.append({
                "side": side, "role": role, "port": port, "dev": pe["dev"] if pe else "",
                "port_ok": pe is not None, "usb": (pe or {}).get("usb") or {},
                "calib": {"ok": c["ok"], "id": c["id"], "path": c["path"],
                          "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime)) if mtime else ""},
                "diag": dg,
                "board": {"calib": board_calib_summary(ee), "file": fcmp, "diff": fdiff},
                "verified": v.get("verified") if fresh else None,
                "verified_stale": bool(v.get("verified")) and not fresh})
    busy = exclusive_busy()
    k = kind()
    return {"robot": robot_key(), "robot_label": k["label"], "calib_kind": k["calib"], "unit": k["unit"],
            "robots": {r: v["label"] for r, v in ROBOT_KINDS.items()},
            "plugin_missing": plugin_missing(k) if CFG["mode"] == "bimanual" else [],
            "mode": CFG["mode"], "env": env_name(), "slots": slots, "ports": ports,
            "cameras": list(CAM_SPECS), "urdf": (URDF_DIR / k["urdf"]).exists(), "kind3d": kind3d(),
            "views": {sd: a.get("view") for sd, a in ARM_CFGS.items()},
            "busy": busy["id"] if busy else ""}


@app.get("/api/wizard/state")
def api_wizard_state():
    return wizard_state()


@app.post("/api/wizard/assign")
async def api_wizard_assign(req: Request):
    b = await req.json()
    side, role, dev = b.get("side"), b.get("role"), (b.get("dev") or "").strip()
    if side not in ARM_CFGS or role not in ("follower", "leader"):
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 지정하세요"}, status_code=400)
    pe = _port_entry(list_serial_ports(), dev)
    if pe is None:
        return JSONResponse({"error": f"포트를 찾을 수 없습니다: {dev} — 다시 스캔하세요"}, status_code=400)
    stable = _stable_port(pe)
    cfg = json.loads(json.dumps(CFG))
    for a in cfg["arms"]:                       # 같은 보드가 다른 칸에 지정돼 있으면 비웁니다
        for r in ("follower", "leader"):
            if a.get(f"{r}_port") and os.path.realpath(a[f"{r}_port"]) == pe["dev"]:
                a[f"{r}_port"] = ""
    next(a for a in cfg["arms"] if a["side"] == side)[f"{role}_port"] = stable
    err = validate_config(cfg)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    _write_active_config(cfg)
    sync_active_env()
    return wizard_state()


@app.post("/api/wizard/verify/start")
async def api_wizard_verify_start(req: Request):
    b = await req.json()
    side, role = b.get("side"), b.get("role")
    if side not in ARM_CFGS or role not in ("follower", "leader"):
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    await asyncio.to_thread(VERIFY.stop)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중"}, status_code=400)
    try:
        await asyncio.to_thread(VERIFY.start, side, role)
    except Exception as e:
        await asyncio.to_thread(VERIFY.stop)
        return JSONResponse({"error": f"{e}"}, status_code=400)
    return VERIFY.state()


@app.get("/api/wizard/verify")
def api_wizard_verify():
    return VERIFY.state()


@app.post("/api/wizard/verify/stop")
async def api_wizard_verify_stop():
    await asyncio.to_thread(VERIFY.stop)
    return VERIFY.state()


@app.post("/api/wizard/verify/torque_off")
async def api_wizard_verify_torque_off():
    try:
        await asyncio.to_thread(VERIFY.torque_off)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return VERIFY.state()


@app.post("/api/wizard/verified")
async def api_wizard_verified(req: Request):
    b = await req.json()
    side, role = b.get("side"), b.get("role")
    if side not in ARM_CFGS or role not in ("follower", "leader"):
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    await asyncio.to_thread(VERIFY.stop)
    d = _wiz_load()
    d.setdefault(env_name(), {})[f"{side}|{role}"] = {
        "verified": time.strftime("%Y-%m-%d %H:%M"), "verified_ts": time.time(),
        "port": ARM_CFGS[side].get(f"{role}_port") or ""}
    save_json(WIZARD_FILE, d)
    return wizard_state()


def board_calib_summary(eeprom):
    """모터 EEPROM 의 Homing_Offset / Min·Max_Position_Limit 로 '보드에 캘리브레이션이 남아 있는지' 판정.
    공장 출고값(오프셋 0, 범위 0~4095)이면 캘리브레이션 안 된 것입니다. wrist_roll 은 원래 0~4095 라 제외.
    반환: calibrated | default | partial | unknown"""
    if not eeprom or len(eeprom) < len(CTL_JOINTS):
        return "unknown"
    states = []
    for j in CTL_JOINTS:
        e = eeprom.get(j) or {}
        h, lo, hi = e.get("homing_offset"), e.get("range_min"), e.get("range_max")
        if h is None or lo is None or hi is None:
            return "unknown"
        if not 0 <= lo < hi <= 4095:
            states.append("bad")
        elif j != FULL_TURN_MOTOR:
            states.append("default" if (h, lo, hi) == (0, 0, 4095) else "cal")
    if all(x == "cal" for x in states):
        return "calibrated"
    if all(x == "default" for x in states):
        return "default"
    return "partial"


def file_calib_compare(path, eeprom):
    """캘리브레이션 파일과 EEPROM 값 비교. 반환 (상태, 다른 관절 목록) — 상태: match | mismatch | none | unknown"""
    try:
        f = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return "none", []
    if not eeprom:
        return "unknown", []
    diff = []
    for j in CTL_JOINTS:
        a, b = f.get(j) or {}, eeprom.get(j) or {}
        if any(a.get(k) != b.get(k) for k in ("homing_offset", "range_min", "range_max")):
            diff.append(j)
    return ("mismatch" if diff else "match"), diff


_IMPORT_LOCK = threading.Lock()


def import_board_calibration(side, role):
    """모터 EEPROM 에 남은 캘리브레이션을 읽어 lerobot 캘리브레이션 파일(JSON)로 씁니다.
    이미 캘리브레이션된 팔을 새 PC 에 꽂았을 때 다시 안 해도 됩니다.
    서보에는 아무것도 쓰지 않습니다 (읽기만). 기존 파일은 .bak 으로 남깁니다."""
    import tools_armcheck as AC
    arm = ARM_CFGS[side]
    port = arm.get(f"{role}_port") or ""
    if not port:
        raise RuntimeError("포트가 지정되지 않았습니다")
    with _IMPORT_LOCK:
        io = AC.BusIO(port)
        try:
            eeprom, missing = {}, []
            for j in CTL_JOINTS:
                vals = {}
                for reg, key in (("Homing_Offset", "homing_offset"), ("Min_Position_Limit", "range_min"),
                                 ("Max_Position_Limit", "range_max")):
                    v, _ = io.read(AC.IDS[j], reg)
                    vals[key] = v
                if None in vals.values():
                    missing.append(j)
                eeprom[j] = vals
        finally:
            io.close()
    if missing:
        raise RuntimeError("응답 없는 모터: " + ", ".join(missing) + " — 진단부터 통과하세요")
    st = board_calib_summary(eeprom)
    if st != "calibrated":
        raise RuntimeError({"default": "보드에 캘리브레이션이 없습니다 (공장 출고값) — 캘리브레이션을 하세요",
                            "partial": "일부 모터만 캘리브레이션돼 있습니다 — 캘리브레이션을 다시 하세요"}.get(st, st))
    out = {j: {"id": AC.IDS[j], "drive_mode": 0, **eeprom[j]} for j in CTL_JOINTS}
    path = calib_file(role, arm[f"{role}_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=4))        # lerobot _save_calibration 과 같은 모양
    os.replace(tmp, path)
    # 진단 기록도 지금 값으로 갱신 (파일 비교가 바로 '일치' 로 보이도록)
    h = ARMCHECK.history.get(port)
    if h is not None:
        h["eeprom"] = eeprom
    return str(path)


@app.post("/api/wizard/import_calib")
async def api_wizard_import_calib(req: Request):
    b = await req.json()
    side, role = b.get("side"), b.get("role")
    if side not in ARM_CFGS or role not in ("follower", "leader"):
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 하세요"}, status_code=400)
    try:
        path = await asyncio.to_thread(import_board_calibration, side, role)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    st = wizard_state()
    st["imported"] = path
    return st


def convert_mode(cfg, mode):
    """한팔 ↔ 양팔 전환 (셋업 탭 setMode 와 같은 규칙). 한팔의 팔은 왼팔이 되고, 양팔 → 한팔은 왼팔을 남깁니다.
    id 가 바뀌어 캘리브레이션 파일을 못 찾게 되므로, 같은 팔(같은 포트)이 지금 쓰던 파일을 새 id 로 복사합니다.
    반환 (새 설정, 복사 계획 [(원본, 대상)]) — 복사는 검증을 통과한 뒤 apply_calib_copies 로 합니다."""
    cfg = json.loads(json.dumps(cfg))
    if cfg["mode"] == mode:
        return cfg, []
    a = cfg["arms"][0]
    old = {r: a[f"{r}_id"] for r in ("follower", "leader")}
    if mode == "bimanual":
        a["side"] = "left"
        for r in ("follower", "leader"):
            if a[f"{r}_id"] == r:
                a[f"{r}_id"] = f"{r}_left"
        a["view"] = {"x": 0.0, "y": 0.12, "yaw_deg": 0.0}
        cfg["arms"] = [a, {"side": "right", "follower_port": "", "follower_id": "follower_right",
                           "leader_port": "", "leader_id": "leader_right", "cameras": {},
                           "view": {"x": 0.0, "y": -0.12, "yaw_deg": 0.0}}]
    else:
        a["side"] = "main"
        for r in ("follower", "leader"):
            if a[f"{r}_id"] == f"{r}_left":
                a[f"{r}_id"] = r
        a["view"] = {"x": 0.0, "y": 0.0, "yaw_deg": 0.0}
        cfg["arms"] = [a]
    cfg["mode"] = mode
    plan = []
    for r in ("follower", "leader"):
        src, dst = calib_file(r, old[r]), calib_file(r, a[f"{r}_id"])
        if src != dst and src.is_file() and a.get(f"{r}_port"):
            if dst.is_file() and dst.read_bytes() == src.read_bytes():
                continue
            plan.append((src, dst))
    return cfg, plan


def apply_calib_copies(plan):
    """대상에 다른 내용의 파일이 있으면 .bak 으로 남기고 덮어씁니다
    (그 파일은 다른 팔 것일 수 있고, 남겨 두면 연결할 때 엉뚱한 값이 보드에 써집니다)."""
    done = []
    for src, dst in plan:
        dst.parent.mkdir(parents=True, exist_ok=True)
        note = ""
        if dst.is_file():
            shutil.copy2(dst, dst.with_suffix(".json.bak"))
            note = " (기존 파일은 .bak)"
        shutil.copy2(src, dst)
        done.append(f"{src.name} → {dst.name}{note}")
    return done


@app.post("/api/wizard/mode")
async def api_wizard_mode(req: Request):
    b = await req.json()
    mode = b.get("mode")
    if mode not in ("single", "bimanual"):
        return JSONResponse({"error": "mode 는 single 또는 bimanual"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 바꾸세요"}, status_code=400)
    cfg, plan = convert_mode(CFG, mode)
    err = validate_config(cfg)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    copied = apply_calib_copies(plan)
    frm, to = ("main", "left") if mode == "bimanual" else ("left", "main")
    _write_active_config(cfg)
    sync_active_env()
    d = _wiz_load()                  # 같은 팔의 '확인됨' 기록도 새 side 이름으로 옮깁니다
    w = d.get(env_name(), {})
    for r in ("follower", "leader"):
        if f"{frm}|{r}" in w:
            w[f"{to}|{r}"] = w.pop(f"{frm}|{r}")
    save_json(WIZARD_FILE, d)
    st = wizard_state()
    st["copied"] = copied
    return st


def convert_robot(cfg, robot):
    """기종 전환. 보드(USB)가 다르므로 포트는 비우고, id·카메라·3D 배치는 둡니다.
    캘리브레이션 파일은 기종마다 폴더가 달라 서로 섞이지 않습니다."""
    cfg = json.loads(json.dumps(cfg))
    if cfg.get("robot", DEFAULT_ROBOT) == robot:
        return cfg
    cfg["robot"] = robot
    for a in cfg["arms"]:
        a["follower_port"] = a["leader_port"] = ""
    return cfg


@app.post("/api/wizard/robot")
async def api_wizard_robot(req: Request):
    b = await req.json()
    robot = b.get("robot")
    if robot not in ROBOT_KINDS:
        return JSONResponse({"error": f"robot 은 {' / '.join(ROBOT_KINDS)} 중 하나"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 바꾸세요"}, status_code=400)
    cfg = convert_robot(CFG, robot)
    err = validate_config(cfg)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    _write_active_config(cfg)
    sync_active_env()
    return wizard_state()


@app.get("/setup/wizard", response_class=HTMLResponse)
def wizard_page():
    r = HTMLResponse(CSS + nav_html("st") + WIZARD_HTML.replace("<div class=modebar id=modebar></div>",
                                                                sim_card_html() + "<div class=modebar id=modebar></div>", 1))
    r.set_cookie(WIZ_COOKIE, "1", max_age=365 * 86400, samesite="lax")
    return r


@app.post("/api/setup/config")
async def api_setup_config(req: Request):
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 저장하세요"}, status_code=400)
    b = await req.json()
    cfg = b.get("config")
    err = validate_config(cfg)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    cfg["env_name"] = env_name()          # Setup 은 언제나 '지금 환경' 을 고칩니다
    copied, kept = calib_rename_copies(CFG, cfg)
    _write_active_config(cfg)
    sync_active_env()
    return {"ok": True, "config": CFG, "copied": copied, "kept": kept}


def calib_rename_copies(old_cfg, new_cfg):
    """셋업에서 캘리브 id 만 바꾼 같은 팔(같은 기종·같은 역할·같은 포트)이면 기존 파일을 새 이름으로 복사합니다.
    id 를 바꾸자마자 '캘리브레이션 없음' 이 되어 다시 캘리브레이션해야 하던 것을 없앱니다.
    새 이름에 이미 파일이 있으면 덮어쓰지 않습니다 — 다른 팔 세트의 파일일 수 있습니다(그 id 로 바꾼 의도).
    반환 (복사한 것, 이미 있어서 그대로 둔 것)."""
    if robot_key(old_cfg) != robot_key(new_cfg):
        return [], []
    k = kind(new_cfg)
    old_by_port = {}
    for a in old_cfg.get("arms") or []:
        for r in ("follower", "leader"):
            if a.get(f"{r}_port") and a.get(f"{r}_id"):
                old_by_port[(r, a[f"{r}_port"])] = a[f"{r}_id"]
    copied, kept = [], []
    for a in new_cfg.get("arms") or []:
        for r in ("follower", "leader"):
            old_id, new_id = old_by_port.get((r, a.get(f"{r}_port"))), a.get(f"{r}_id")
            if not old_id or not new_id or old_id == new_id:
                continue
            src, dst = calib_file(r, old_id, k), calib_file(r, new_id, k)
            if not src.is_file():
                continue
            if dst.is_file():
                kept.append(dst.name)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied.append(f"{src.name} → {dst.name}")
    return copied, kept


# ----------------------------- 환경 API --------------------------------------
@app.get("/api/sim")
def api_sim():
    return sim_state()


@app.post("/api/sim/start")
async def api_sim_start(request: Request):
    b = await request.json()
    robot = b.get("robot") or CFG.get("robot") or "so101"
    mode = b.get("mode") or CFG.get("mode") or "single"
    try:
        return sim_start(robot, mode)
    except (ValueError, RuntimeError, JobStartError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/sim/stop")
def api_sim_stop():
    try:
        return sim_stop()
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.post("/api/sim/cmd")
async def api_sim_cmd(request: Request):
    b = await request.json()
    st = sim_state()
    if not st.get("on"):
        return JSONResponse({"error": "가상 장치가 꺼져 있습니다"}, status_code=400)
    cmd, n = b.get("cmd"), b.get("board")
    nb = len(st.get("ports", []))
    if cmd == "a":
        sim_send("a on" if b.get("on") else "a off")
    elif cmd in ("w", "v", "t") and isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= nb:
        if cmd == "w":
            sim_send(f"w {n}")
        else:
            v = b.get("value")
            lim = (0, 25) if cmd == "v" else (0, 120)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lim[0] <= v <= lim[1]:
                return JSONResponse({"error": "값이 잘못됐습니다"}, status_code=400)
            sim_send(f"{cmd} {n} {v}")
    else:
        return JSONResponse({"error": "명령이 잘못됐습니다"}, status_code=400)
    return {"ok": True}


@app.get("/api/envs")
def api_envs():
    return {"active": env_name(), "envs": list_envs()}


async def _env_op(fn, *args, needs_idle=True):
    if needs_idle:
        busy = exclusive_busy()
        if busy:
            return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 하세요"}, status_code=400)
    try:
        await asyncio.to_thread(fn, *args)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "active": env_name(), "envs": list_envs()}


@app.post("/api/envs/activate")
async def api_env_activate(req: Request):
    b = await req.json()
    return await _env_op(activate_env, (b.get("name") or "").strip())


@app.post("/api/envs/save_as")
async def api_env_save_as(req: Request):
    b = await req.json()
    return await _env_op(save_env_as, (b.get("name") or "").strip())


@app.post("/api/envs/rename")
async def api_env_rename(req: Request):
    b = await req.json()
    return await _env_op(rename_env, (b.get("name") or "").strip(), (b.get("new") or "").strip())


@app.post("/api/envs/delete")
async def api_env_delete(req: Request):
    b = await req.json()
    return await _env_op(delete_env, (b.get("name") or "").strip(), needs_idle=False)


# 영상 한 프레임을 캔버스에 그려 썸네일로 씁니다. 서버에서 디코딩하지 않으므로
# 코덱(AV1 등)과 무관하게 브라우저가 재생할 수 있으면 됩니다. 동시에 2개까지만.
VTHUMB_JS = """
const _TQ=[]; let _TB=0;
function vthumb(cv, url, t){ _TQ.push([cv,url,t||0]); _tnext(); }
function _tnext(){
  if(_TB>=2 || !_TQ.length) return;
  const it=_TQ.shift(), cv=it[0], url=it[1], t=it[2]; _TB++;
  const v=document.createElement('video'); v.muted=true; v.preload='auto'; v.src=url;
  let done=false;
  const fin=()=>{ if(done) return; done=true; _TB--; v.removeAttribute('src'); try{v.load();}catch(e){} _tnext(); };
  v.addEventListener('loadedmetadata',()=>{ try{ v.currentTime=Math.max(0, Math.min(t+0.05, (v.duration||t+1)-0.05)); }catch(e){ fin(); } });
  v.addEventListener('seeked',()=>{ try{ cv.getContext('2d').drawImage(v,0,0,cv.width,cv.height); cv.classList.add('ok'); }catch(e){} fin(); });
  v.addEventListener('error',fin);
  setTimeout(fin,10000);
}
"""


PROJECTS_HTML = """
<style>
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(420px,100%),1fr));gap:14px}
@media(max-width:560px){.pcard canvas{width:110px}}
.pcard{display:flex;background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden;min-height:128px}
.pcard.on{border-color:var(--accent)}
.pcard canvas{width:170px;height:128px;background:#0b0e12;flex:none;display:block}
.pcard .pb{padding:12px 14px;flex:1;min-width:0;display:flex;flex-direction:column;gap:5px}
.pcard h3{margin:0;font-size:16px;display:flex;gap:8px;align-items:center}
.pcard .meta{font-size:12px;color:var(--muted)}
.pcard .acts{margin-top:auto;display:flex;gap:6px;flex-wrap:wrap}
.pcard .acts button{padding:5px 10px;font-size:12px}
.padd{border:1px dashed var(--line);background:transparent;align-items:center;justify-content:center;cursor:pointer;color:var(--muted)}
.padd:hover{border-color:var(--accent);color:var(--text)}
.pform{background:var(--surface);border:1px solid var(--accent);border-radius:10px;padding:16px 18px;margin-bottom:14px}
</style>
<div class=wrap>
<p class=eyebrow>태스크별 묶음</p><h2>프로젝트</h2>
<p class=muted style="max-width:860px">프로젝트는 태스크 하나의 묶음입니다 — 데이터셋, 학습된 모델, 기본 태스크 설명, 기준 환경.
프로젝트를 <b>열면</b> 데이터셋 · 수집 · 학습 · 실행 탭이 그 프로젝트 기준으로 보이고,
새로 수집한 데이터셋과 새로 학습한 모델이 자동으로 들어갑니다. <b>전체</b>를 열면 프로젝트 없이 지금처럼 모두 보입니다.</p>
<div id=pform></div>
<div class=pgrid id=pgrid></div>
<div id=unassigned style="margin-top:22px"></div>
</div>
<script>
""" + VTHUMB_JS + """
const $=id=>document.getElementById(id);
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function jget(u){ return (await fetch(u)).json(); }
async function jpost(u,b){ const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}); return r.json(); }
const NAME_RE=/^[A-Za-z0-9._-]+$/;
let V=null;
function mk(tag,cls,html){ const e=document.createElement(tag); if(cls) e.className=cls; if(html!=null) e.innerHTML=html; return e; }
function b(label,fn,cls){ const x=mk('button',cls||''); x.textContent=label; x.onclick=fn; return x; }
function paint(v){
  if(v.error){ alert(v.error); return; }
  V=v; const g=$('pgrid'); g.innerHTML='';
  const add=mk('div','pcard padd','<div style="text-align:center"><div style="font-size:30px;line-height:1">+</div><div>새 프로젝트</div></div>');
  add.onclick=()=>form(null); g.appendChild(add);
  const all=mk('div','pcard'+(v.active===''?' on':''));
  all.innerHTML='<canvas width=170 height=128></canvas><div class=pb><h3>전체'+(v.active===''?' <span class="badge b-ok">열림</span>':'')+'</h3>'
    +'<div class=meta>프로젝트 구분 없이 모두</div><div class=meta>데이터셋 '+v.all.datasets+' · 에피소드 '+v.all.episodes+' · 학습 출력 '+v.all.models+'</div>'
    +'<div class=acts></div></div>';
  all.querySelector('.acts').appendChild(b('열기',()=>open_(''),'primary'));
  g.appendChild(all);
  v.projects.forEach(p=>{
    const on=v.active===p.name;
    const c=mk('div','pcard'+(on?' on':''));
    const envWarn = p.env && p.env!==v.env ? ' <span class="badge b-warn" title="지금 환경: '+E(v.env)+'">환경 다름</span>' : '';
    c.innerHTML='<canvas width=170 height=128></canvas><div class=pb>'
      +'<h3><span class=mono>'+E(p.name)+'</span>'+(on?' <span class="badge b-ok">열림</span>':'')+'</h3>'
      +'<div class=meta>'+(p.task?E(p.task):'<i>태스크 설명 없음</i>')+'</div>'
      +'<div class=meta>환경 <span class=mono>'+E(p.env||'지정 안 함')+'</span>'+envWarn+'</div>'
      +'<div class=meta>데이터셋 '+p.datasets.length+' · 에피소드 '+p.episodes+' · 모델 '+p.models.length
      +(p.updated?' · 수정 '+E(p.updated):'')+'</div><div class=acts></div></div>';
    const a=c.querySelector('.acts');
    a.appendChild(b('열기',()=>open_(p.name),'primary'));
    a.appendChild(b('편집',()=>form(p)));
    a.appendChild(b('삭제',()=>del(p),'danger'));
    g.appendChild(c);
    if(p.thumb) vthumb(c.querySelector('canvas'), p.thumb.url, p.thumb.t);
  });
  const u=$('unassigned');
  if(!v.unassigned.length || !v.projects.length){ u.innerHTML=''; return; }
  u.innerHTML='<p class=eyebrow>프로젝트에 안 들어간 데이터셋</p><div class=card><table id=utbl></table></div>';
  const t=$('utbl');
  t.innerHTML='<tr><th>데이터셋</th><th class=num>에피소드</th><th>프로젝트로 옮기기</th></tr>';
  v.unassigned.forEach(d=>{
    const tr=mk('tr','','<td class=mono>'+E(d.name)+'</td><td class=num>'+E(d.episodes)+'</td><td></td>');
    const sel=mk('select'); sel.innerHTML='<option value="">— 선택 —</option>'+v.projects.map(p=>'<option>'+E(p.name)+'</option>').join('');
    sel.onchange=async()=>{ if(sel.value) paint(await jpost('/api/projects/assign',{kind:'datasets',item:d.name,project:sel.value})); };
    tr.lastChild.appendChild(sel); t.appendChild(tr);
  });
}
function form(p){
  const f=$('pform');
  const envOpts='<option value="">지정 안 함</option>'+V.envs.map(e=>'<option'+((p?p.env:V.env)===e?' selected':'')+'>'+E(e)+'</option>').join('');
  f.innerHTML='<div class=pform><h3 style="margin:0 0 10px">'+(p?'프로젝트 편집':'새 프로젝트')+'</h3><div class=formgrid>'
    +'<label class=f>이름 <input id=pf_name size=20 placeholder="dice_cleanup"></label>'
    +'<label class=f style="flex:1;min-width:300px">기본 태스크 설명 (수집·추론에 쓰임) <input id=pf_task placeholder="Move the dice into the cup"></label>'
    +'<label class=f>기준 환경 <select id=pf_env>'+envOpts+'</select></label></div>'
    +'<div class=toolbar style="margin-top:12px"><button class=primary id=pf_ok>'+(p?'저장':'만들고 열기')+'</button><button id=pf_no>취소</button></div></div>';
  $('pf_name').value=p?p.name:''; $('pf_task').value=p?p.task:'';
  $('pf_no').onclick=()=>{ f.innerHTML=''; };
  $('pf_ok').onclick=async()=>{
    const name=$('pf_name').value.trim();
    if(!NAME_RE.test(name)){ alert('이름은 영문/숫자/._- 만'); return; }
    const r=await jpost('/api/projects/save',{name:name,old:p?p.name:'',task:$('pf_task').value,env:$('pf_env').value,activate:!p});
    if(r.error){ alert(r.error); return; }
    f.innerHTML=''; paint(r);
  };
  $('pf_name').focus(); window.scrollTo(0,0);
}
async function open_(name){
  const r=await jpost('/api/projects/activate',{name:name});
  if(r.error){ alert(r.error); return; }
  location.href='/';
}
async function del(p){
  if(!confirm('프로젝트 "'+p.name+'" 를 지웁니다. 데이터셋과 모델 파일은 그대로 남고 미분류가 됩니다.')) return;
  paint(await jpost('/api/projects/delete',{name:p.name}));
}
jget('/api/projects').then(paint);
</script>"""


IMPORTMAP_HTML = """<script type="importmap">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
"three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/",
"three/examples/jsm/":"https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/",
"urdf-loader":"https://cdn.jsdelivr.net/npm/urdf-loader@0.12.6/src/URDFLoader.js"}}
</script>"""

# 읽기 전용 3D 뷰어 (리뷰 재생 · 셋업 마법사 확인 단계). 제어 탭과 같은 좌표·색 규칙.
# <script type="module"> 안에 넣어 쓰고 window.mountArm3D(el, sides, views) 로 부릅니다.
ARM3D_JS = """
window.mountArm3D = async function(el, sides, views, K3){
  // K3 = 서버 kind3d(): 기종별 URDF, lerobot 관절 → URDF 관절 이름, 단위 → rad (없으면 SO-ARM101)
  K3=K3||{urdf:'/urdf/so101.urdf', scale:Math.PI/180, map:{}, sign:{}, gripper:null, motor_mat:'sts3215'};
  const THREE=await import('three');
  const {OrbitControls}=await import('three/addons/controls/OrbitControls.js');
  const URDFLoader=(await import('urdf-loader')).default;
  const scene=new THREE.Scene(); scene.background=new THREE.Color(0x0a0d10);
  const cam=new THREE.PerspectiveCamera(50,1,0.01,10);
  const zoom=sides.length>1?1.7:1.0;
  // 팔은 0 자세에서 앞(+X)으로 뻗으므로 한팔이면 시선을 조금 앞으로 (끝이 잘리지 않게)
  const fx=sides.length>1?0:0.12;
  cam.position.set(0.4*zoom+fx,0.38*zoom,0.5*zoom);
  const ren=new THREE.WebGLRenderer({antialias:true}); el.appendChild(ren.domElement);
  const ctl=new OrbitControls(cam,ren.domElement); ctl.target.set(fx,0.1,0);
  scene.add(new THREE.HemisphereLight(0xffffff,0x223344,1.1));
  const dl=new THREE.DirectionalLight(0xffffff,1.2); dl.position.set(1,2,1); scene.add(dl);
  scene.add(new THREE.GridHelper(sides.length>1?1.6:1, sides.length>1?32:20, 0x28303a, 0x1b222a));
  function resize(){ const w=el.clientWidth||320, h=el.clientHeight||240; ren.setSize(w,h); cam.aspect=w/h; cam.updateProjectionMatrix(); }
  new ResizeObserver(resize).observe(el); resize();
  let color='#ffffff'; try{ color=localStorage.getItem('armColor2')||'#ffffff'; }catch(e){}
  const robots={}, hl={};
  // 관절을 움직이는 서보 = 그 관절의 부모 링크에 붙은 sts3215 메시 (URDF 구조)
  const LINK2JOINT={base_link:'shoulder_pan',shoulder_link:'shoulder_lift',upper_arm_link:'elbow_flex',
                    lower_arm_link:'wrist_flex',wrist_link:'wrist_roll',gripper_link:'gripper'};
  function motorJoint(o){ for(let p=o.parent;p;p=p.parent){ if(p.isURDFLink) return LINK2JOINT[p.name]||null; } return null; }
  function paint(r, side){
    r.traverse(o=>{
      if(!o.isMesh) return;
      if(!o.userData.rc){ o.userData.m=(o.material&&o.material.name)||''; o.userData.j=motorJoint(o);
        o.material=new THREE.MeshStandardMaterial({metalness:0.15,roughness:0.55}); o.userData.rc=1; }
      if(K3.motor_mat && o.userData.m===K3.motor_mat){
        const c=(hl[side]||{})[o.userData.j];
        o.material.color.set(c||'#1a1a1a'); o.material.roughness=0.35;
        o.material.emissive.set(c||'#000000'); o.material.emissiveIntensity=c?0.5:0;
      } else o.material.color.set(color);
    });
  }
  const mgr=new THREE.LoadingManager(); mgr.onLoad=()=>Object.keys(robots).forEach(s=>paint(robots[s],s));
  await Promise.all(sides.map(side=>new Promise(res=>{
    const loader=new URDFLoader(mgr); loader.workingPath='/urdf/'; loader.packages='/urdf';
    loader.load(K3.urdf, r=>{
      const v=views[side]||{x:0,y:0,yaw_deg:0};
      r.rotation.set(-Math.PI/2, 0, (v.yaw_deg||0)*Math.PI/180);
      r.position.set(v.x||0, 0, -(v.y||0));
      robots[side]=r; scene.add(r); res();
    }, undefined, e=>{ console.error(e); res(); });
  })));
  // 화면에서 떨어져 나가면(단계 이동·다시 그리기) 렌더 루프를 멈추고 WebGL 컨텍스트를 돌려줍니다.
  // 안 그러면 브라우저 한도(약 16개)를 넘어 오래된 3D 가 검게 죽습니다.
  let alive=false;
  (function anim(){
    const on=ren.domElement.isConnected;
    if(alive && !on){ ren.dispose(); try{ ren.forceContextLoss(); }catch(e){} return; }
    if(on) alive=true;
    requestAnimationFrame(anim); ctl.update(); ren.render(scene,cam);
  })();
  return {
    // 모터 강조: {관절: '#rrggbb' | null} — 진단 결과·캘리브레이션 진행 표시용
    highlight(side, map){ hl[side]=map||{}; if(robots[side]) paint(robots[side], side); },
    update(side, joints){
      const r=robots[side]; if(!r) return;
      for(const j in joints){
        const jt=r.joints&&r.joints[(K3.map&&K3.map[j])||j], v=joints[j];
        if(!jt || v==null) continue;
        if(j==='gripper'){
          const g=K3.gripper, lo=g?g.lo:(jt.limit?jt.limit.lower:0), hi=g?g.hi:(jt.limit?jt.limit.upper:1);
          jt.setJointValue(lo+(hi-lo)*(v/100));
        }
        else jt.setJointValue(((K3.sign&&K3.sign[j])||1)*v*K3.scale);
      }
    }
  };
};
"""


REVIEW_HTML = """
<style>
.rvwrap{padding:14px 18px 30px}
.rvhead{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.rvhead h2{margin:0;font-family:var(--mono);font-size:18px}
.rv{display:grid;grid-template-columns:minmax(0,1fr) 290px;gap:14px;align-items:start}
.player{background:var(--surface);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.pline{display:flex;gap:10px;align-items:center;padding:9px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.eb{font-family:var(--mono);font-size:11px;background:#e5c07b;color:#111;border-radius:4px;padding:1px 6px;font-weight:600}
.vgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1px;background:var(--line)}
.vcell{position:relative;background:#000}
.vcell video{width:100%;display:block;aspect-ratio:4/3;object-fit:contain;background:#000}
.vcell .cl{position:absolute;top:6px;left:10px;font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  text-transform:uppercase;color:#cfd8e3;text-shadow:0 0 4px #000}
#v3d{height:270px;position:relative;background:#0a0d10;border-top:1px solid var(--line)}
#v3d canvas{display:block}
.tl{padding:10px 12px 12px;border-top:1px solid var(--line)}
.tl .ctr{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.tl input[type=range]{flex:1;min-width:160px;accent-color:var(--accent)}
.chart{margin-top:10px}
.chart canvas{width:100%;height:140px;display:block;cursor:crosshair;background:#0d1116;border-radius:6px}
.legend{display:flex;gap:12px;flex-wrap:wrap;font-family:var(--mono);font-size:11px;color:var(--muted);margin:0 0 4px}
.legend i{display:inline-block;width:10px;height:3px;margin-right:4px;vertical-align:middle}
.eplist{display:flex;flex-direction:column;gap:10px;max-height:calc(100vh - 130px);overflow-y:auto;padding-right:4px}
.epc{background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow:hidden;cursor:pointer;flex:none}
.epc.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.epc.bad canvas{opacity:.4}
.epc canvas{width:100%;aspect-ratio:4/3;display:block;background:#0b0e12}
.epc .row{display:flex;gap:8px;align-items:center;padding:6px 8px;font-size:12px}
@media(max-width:900px){ .rv{grid-template-columns:1fr} .eplist{max-height:none;flex-direction:row;overflow-x:auto} .epc{width:190px} }
</style>
<div class=rvwrap>
<div class=rvhead>
  <a href="/" class=tiny>&larr; 데이터셋</a>
  <h2 id=dsname></h2>
  <span id=chips></span>
  <span style="flex:1"></span>
  <button id=b_add title="이 데이터셋에 이어서 수집">에피소드 추가</button>
  <button id=b_ren>이름 변경</button>
  <a class=btnlink id=b_dl title="LeRobotDataset v3.0 폴더를 그대로 tar 로">다운로드</a>
  <button class=danger id=b_delm style="display:none"></button>
</div>
<div class=rv>
  <div class=player>
    <div class=pline>
      <span class=eb id=epb>E-</span><span class=mono id=epdur></span>
      <span id=eptask class=muted></span><span id=epflags></span>
      <label class=tiny style="margin-left:auto;display:flex;gap:6px;align-items:center;cursor:pointer">
        <input type=checkbox id=epbad> 불량 (X)</label>
    </div>
    <div class=vgrid id=vgrid></div>
    <div id=v3d style="display:none"></div>
    <div class=tl>
      <div class=ctr>
        <button id=b_play style="min-width:46px">&#9654;</button>
        <span class=mono id=tnow>0.0 / 0.0 s</span>
        <input type=range id=scrub min=0 max=1000 value=0>
        <select id=rate title="재생 속도">
          <option value=0.25>0.25×</option><option value=0.5>0.5×</option><option value=1 selected>1×</option><option value=2>2×</option></select>
        <label class=tiny style="display:flex;gap:5px;align-items:center"><input type=checkbox id=showact> 명령(action) 겹쳐 보기</label>
      </div>
      <div id=charts></div>
    </div>
  </div>
  <div class=eplist id=eplist></div>
</div>
<p class=muted style="margin-top:12px">&larr; / &rarr; 이전·다음 에피소드 · Space 재생/정지 · <b>,</b> / <b>.</b> 한 프레임 앞뒤 · X 불량 표시.
불량으로 표시한 에피소드는 위의 <b>삭제 실행</b>으로 한 번에 지웁니다 (결과는 새 폴더, 원본 유지).
그래프는 observation.state(실측)입니다 — 한 관절이 평평하게 멈춰 있거나 갑자기 튀면 그 에피소드를 의심하세요.
<b>짧음</b>은 길이가 중앙값의 절반도 안 되는 에피소드입니다.</p>
</div>
""" + IMPORTMAP_HTML + """
<script type="module">
""" + ARM3D_JS + VTHUMB_JS + """
const $=id=>document.getElementById(id);
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const COLORS=['#5d9dd6','#e07a3f','#6cc070','#c792ea','#e5c07b','#56b6c2'];
async function jget(u){ const r=await fetch(u); return r.json(); }
async function jpost(u,b){ const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}); return r.json(); }
const fmt=t=>{ t=Math.max(0,t||0); const m=Math.floor(t/60), s=Math.floor(t%60); return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0'); };
let INFO=null, IDX=-1, DATA=null, VIDS=[], T=0, DUR=0, PLAYING=false, ARM=null, raf=0, clock0=0, scrubbing=false, CH=[];

function isBad(ep){ return INFO.marks.indexOf(ep)>=0; }
function paintDel(){
  const b=$('b_delm'), n=INFO.marks.length;
  b.style.display=n?'':'none'; b.textContent='불량 '+n+'개 삭제 실행';
}
const io=new IntersectionObserver(ents=>ents.forEach(en=>{
  if(!en.isIntersecting) return;
  const i=+en.target.dataset.i, e=INFO.episodes[i];
  vthumb(en.target.querySelector('canvas'), e.segs[0].url, e.segs[0].from);
  io.unobserve(en.target);
}),{rootMargin:'300px'});
function buildList(){
  const L=$('eplist'); L.innerHTML='';
  INFO.episodes.forEach((e,i)=>{
    const c=document.createElement('div'); c.className='epc'+(isBad(e.ep)?' bad':''); c.dataset.i=i;
    c.innerHTML='<canvas width=240 height=180></canvas><div class=row><span class=eb>E'+e.ep+'</span>'
      +'<span class=mono>'+fmt(e.dur)+'</span>'+(e.short?'<span class="badge b-warn">짧음</span>':'')
      +'<span class="tiny badtag" style="margin-left:auto;color:var(--bad)">'+(isBad(e.ep)?'불량':'')+'</span></div>';
    c.onclick=()=>select(i);
    L.appendChild(c);
    if(e.segs.length) io.observe(c);
  });
}
function paintCard(i){
  const c=document.querySelector('.epc[data-i="'+i+'"]'); if(!c) return;
  const e=INFO.episodes[i];
  c.classList.toggle('bad', isBad(e.ep));
  c.querySelector('.badtag').textContent=isBad(e.ep)?'불량':'';
}

async function select(i){
  if(i<0 || i>=INFO.episodes.length) return;
  pause(); IDX=i; const e=INFO.episodes[i];
  history.replaceState(null,'','?ep='+e.ep);
  document.querySelectorAll('.epc.on').forEach(x=>x.classList.remove('on'));
  const card=document.querySelector('.epc[data-i="'+i+'"]');
  if(card){ card.classList.add('on'); card.scrollIntoView({block:'nearest',inline:'nearest'}); }
  $('epb').textContent='E'+e.ep; $('epdur').textContent=fmt(e.dur)+' · '+e.length+' frames';
  $('eptask').textContent=e.task||''; $('epflags').innerHTML=e.short?'<span class="badge b-warn">짧음</span>':'';
  $('epbad').checked=isBad(e.ep);
  const g=$('vgrid'); g.innerHTML=''; VIDS=[];
  e.segs.forEach(sg=>{
    const cell=document.createElement('div'); cell.className='vcell';
    cell.innerHTML='<span class=cl></span><video muted playsinline preload=auto></video>';
    cell.querySelector('.cl').textContent=sg.cam;
    const v=cell.querySelector('video'); v.dataset.from=sg.from; v.src=sg.url;
    v.addEventListener('loadedmetadata',()=>{ v.currentTime=vpos(v,T); });
    v.addEventListener('error',()=>{ const m=document.createElement('p'); m.className='muted'; m.style.cssText='position:absolute;inset:auto 8px 8px 8px;margin:0;color:#e5c07b;font-size:12px';
      m.textContent='영상을 재생할 수 없습니다 (파일 없음 또는 브라우저가 이 코덱을 못 읽음)'; cell.appendChild(m); });
    g.appendChild(cell); VIDS.push(v);
  });
  if(!e.segs.length) g.innerHTML='<p class=muted style="padding:14px;margin:0;background:var(--surface)">영상 없음</p>';
  DUR=e.dur; T=0; DATA=null; $('charts').innerHTML='<p class=muted>관절 데이터 불러오는 중…</p>';
  render();
  const d=await jget('/api/ds/'+encodeURIComponent(DS)+'/ep/'+e.ep+'/data');
  if(IDX!==i) return;
  if(d.error){ $('charts').innerHTML='<p class="badge b-bad"></p>'; $('charts').firstChild.textContent=d.error; return; }
  DATA=d; if(!DUR && d.t.length) DUR=d.t[d.t.length-1];
  buildCharts(); render();
}

/* ---------- 재생 ---------- */
function vfrom(v){ return parseFloat(v.dataset.from)||0; }
/* v3.0 은 에피소드들이 mp4 하나에 이어 붙어 있어서, from+DUR 은 정확히 다음 에피소드의 첫 프레임입니다.
   영상은 끝에서 반 프레임 앞까지만 보냅니다. */
function vpos(v,t){ return vfrom(v)+Math.max(0, Math.min(t, DUR-0.5/(INFO.fps||30))); }
/* 시계: 재생 가능한 첫 영상. 모두 못 읽으면(404·코덱) 벽시계로 — 그래야 그래프·3D 가 멈추지 않습니다 */
function master(){ return VIDS.find(v=>!v.error && v.readyState>=2); }
let RATE=1;
function curT(){ const m=master(); if(m) return m.currentTime-vfrom(m); return PLAYING?(performance.now()-clock0)/1000*RATE:T; }
function setRate(r){ RATE=r; VIDS.forEach(v=>{ v.playbackRate=RATE; }); clock0=performance.now()-T*1000/RATE; $('rate').value=String(r); }
function step(n){ pause(); seek(T+n/(INFO.fps||30)); }      // 한 프레임씩 (, .)
function play(){
  if(T>=DUR-0.05) seek(0);
  PLAYING=true; clock0=performance.now()-T*1000/RATE;
  VIDS.forEach(v=>{ v.playbackRate=RATE; const p=v.play(); if(p&&p.catch) p.catch(()=>{}); });
  $('b_play').innerHTML='&#10074;&#10074;'; tick();
}
function pause(){ PLAYING=false; VIDS.forEach(v=>v.pause()); $('b_play').innerHTML='&#9654;'; cancelAnimationFrame(raf); }
function seek(t){ T=Math.max(0,Math.min(DUR,t)); VIDS.forEach(v=>{ v.currentTime=vpos(v,T); }); clock0=performance.now()-T*1000/RATE; render(); }
function tick(){
  if(!PLAYING) return;
  T=Math.max(0,curT());
  if(T>=DUR-0.5/(INFO.fps||30)){ T=DUR; pause(); VIDS.forEach(v=>{ v.currentTime=vpos(v,T); }); render(); return; }
  const mv=master();
  VIDS.forEach(v=>{ if(v!==mv && !v.error && Math.abs((v.currentTime-vfrom(v))-T)>0.15) v.currentTime=vpos(v,T); });
  render(); raf=requestAnimationFrame(tick);
}
function frameAt(t){
  if(!DATA||!DATA.t.length) return -1;
  let k=Math.min(DATA.t.length-1, Math.max(0, Math.round(t*DATA.fps)));
  while(k>0 && DATA.t[k]>t) k--;
  while(k<DATA.t.length-1 && DATA.t[k+1]<=t) k++;
  return k;
}
function render(){
  $('tnow').textContent=T.toFixed(1)+' / '+(DUR||0).toFixed(1)+' s';
  if(!scrubbing) $('scrub').value=DUR?Math.round(T/DUR*1000):0;
  CH.forEach(drawCursor);
  const k=frameAt(T);
  if(ARM && k>=0){
    groups().forEach(gr=>{
      const j={}; gr.idx.forEach((ix,n)=>{ j[gr.names[n]]=DATA.state[k][ix]; });
      ARM.update(gr.side||'main', j);
    });
  }
}

/* ---------- 관절 그래프 ---------- */
function groups(){
  const names=DATA.state_names.length?DATA.state_names:(DATA.state[0]||[]).map((_,k)=>'j'+k);
  const sides=INFO.bimanual?['left','right']:[''];
  return sides.map(sd=>{
    const idx=[]; names.forEach((n,k)=>{ if(!sd || n.indexOf(sd+'_')===0) idx.push(k); });
    return {side:sd, idx:idx, names:idx.map(k=>names[k].replace(sd?sd+'_':'','').replace(/[.]pos$/,''))};
  });
}
function buildCharts(){
  const box=$('charts'); box.innerHTML=''; CH=[];
  if(!DATA.t.length){ box.innerHTML='<p class=muted>관절 데이터 없음</p>'; return; }
  groups().forEach(gr=>{
    const w=document.createElement('div'); w.className='chart';
    w.innerHTML='<div class=legend>'+(gr.side?'<b style="color:var(--text)">'+E(gr.side)+'</b>':'')
      +gr.names.map((n,k)=>'<span><i style="background:'+COLORS[k%6]+'"></i>'+E(n)+'</span>').join('')+'</div><canvas></canvas>';
    box.appendChild(w);
    const cv=w.querySelector('canvas');
    const c={gr:gr, cv:cv, base:document.createElement('canvas')};
    cv.addEventListener('click',ev=>{ const r=cv.getBoundingClientRect(); seek((ev.clientX-r.left-c.padL)/(r.width-c.padL-4)*DUR); });
    CH.push(c); drawBase(c);
  });
}
function drawBase(c){
  const dpr=window.devicePixelRatio||1, W=c.cv.clientWidth||600, H=c.cv.clientHeight||140;
  c.cv.width=W*dpr; c.cv.height=H*dpr; c.base.width=W*dpr; c.base.height=H*dpr;
  const g=c.base.getContext('2d'); g.scale(dpr,dpr);
  const act=$('showact').checked && DATA.action.length;
  let lo=Infinity, hi=-Infinity;
  const scan=m=>c.gr.idx.forEach(ix=>m.forEach(row=>{ const v=row[ix]; if(v<lo) lo=v; if(v>hi) hi=v; }));
  scan(DATA.state); if(act) scan(DATA.action);
  if(!isFinite(lo)){ lo=-1; hi=1; }
  if(hi-lo<1){ lo-=0.5; hi+=0.5; }
  const pad=(hi-lo)*0.06; lo-=pad; hi+=pad;
  c.padL=40; const X=t=>c.padL+t/(DUR||1)*(W-c.padL-4), Y=v=>4+(hi-v)/(hi-lo)*(H-8);
  c.X=X; c.W=W; c.H=H;
  g.fillStyle='#0d1116'; g.fillRect(0,0,W,H);
  g.strokeStyle='#1f2730'; g.lineWidth=1; g.font='10px IBM Plex Mono, monospace'; g.fillStyle='#6b7785';
  [hi-pad, (hi+lo)/2, lo+pad].forEach(v=>{ const y=Y(v); g.beginPath(); g.moveTo(c.padL,y); g.lineTo(W,y); g.stroke(); g.fillText(v.toFixed(0),2,y+3); });
  const line=(m,ix,col,dash,alpha)=>{
    g.beginPath(); g.setLineDash(dash); g.globalAlpha=alpha; g.strokeStyle=col; g.lineWidth=dash.length?1:1.5;
    DATA.t.forEach((t,k)=>{ const x=X(t), y=Y(m[k][ix]); if(k) g.lineTo(x,y); else g.moveTo(x,y); });
    g.stroke(); g.setLineDash([]); g.globalAlpha=1;
  };
  c.gr.idx.forEach((ix,n)=>{ if(act) line(DATA.action,ix,COLORS[n%6],[4,3],0.55); line(DATA.state,ix,COLORS[n%6],[],1); });
  drawCursor(c);
}
function drawCursor(c){
  if(!c.base.width) return;
  const g=c.cv.getContext('2d'), dpr=window.devicePixelRatio||1;
  g.setTransform(1,0,0,1,0,0); g.drawImage(c.base,0,0);
  g.scale(dpr,dpr); const x=c.X(T);
  g.strokeStyle='#ffffff'; g.globalAlpha=0.85; g.lineWidth=1; g.beginPath(); g.moveTo(x,0); g.lineTo(x,c.H); g.stroke(); g.globalAlpha=1;
  g.setTransform(1,0,0,1,0,0);
}

/* ---------- 동작 ---------- */
async function toggleBad(){
  const e=INFO.episodes[IDX]; if(!e) return;
  const r=await jpost('/api/mark/'+encodeURIComponent(DS)+'/'+e.ep);
  if(r.error){ alert(r.error); return; }
  INFO.marks=r.marks; $('epbad').checked=isBad(e.ep); paintCard(IDX); paintDel();
}
$('epbad').addEventListener('change',toggleBad);
$('b_play').onclick=()=>PLAYING?pause():play();
$('rate').addEventListener('change',e=>setRate(parseFloat(e.target.value)));   // 모듈 스크립트라 인라인 onchange 로는 못 부릅니다
$('showact').onchange=()=>{ if(DATA) CH.forEach(drawBase); };
$('scrub').addEventListener('input',()=>{ scrubbing=true; seek($('scrub').value/1000*DUR); });
$('scrub').addEventListener('change',()=>{ scrubbing=false; });
$('b_add').onclick=()=>{ location.href='/collect?resume='+encodeURIComponent(DS); };
$('b_ren').onclick=async()=>{
  const nw=(prompt('새 데이터셋 이름 (영문/숫자/._-)', DS)||'').trim();
  if(!nw || nw===DS) return;
  if(!/^[A-Za-z0-9._-]+$/.test(nw)){ alert('영문/숫자/._- 만'); return; }
  const r=await jpost('/api/rename_dataset/'+encodeURIComponent(DS),{new:nw});
  if(r.error){ alert(r.error); return; }
  location.href='/ds/'+encodeURIComponent(r.name);
};
$('b_delm').onclick=async()=>{
  const n=INFO.marks.length;
  if(!confirm('불량으로 표시한 '+n+'개 에피소드를 지웁니다. 결과는 새 폴더로 만들어지고 원본은 남습니다. 진행할까요?')) return;
  const r=await jpost('/api/delete/'+encodeURIComponent(DS));
  if(r.error){ alert(r.error); return; }
  location.href='/jobs';
};
window.addEventListener('resize',()=>{ if(DATA) CH.forEach(drawBase); });
document.addEventListener('keydown',ev=>{
  if(!INFO || INFO.error || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if(ev.target.tagName==='INPUT' && ev.target.type!=='checkbox' && ev.target.type!=='range') return;
  if(ev.key==='ArrowLeft'){ ev.preventDefault(); select(IDX-1); }
  else if(ev.key==='ArrowRight'){ ev.preventDefault(); select(IDX+1); }
  else if(ev.key===' '){ ev.preventDefault(); PLAYING?pause():play(); }
  else if(ev.key==='x'||ev.key==='X'){ toggleBad(); }
  else if(ev.key===','){ ev.preventDefault(); step(-1); }
  else if(ev.key==='.'){ ev.preventDefault(); step(1); }
});

(async()=>{
  INFO=await jget('/api/ds/'+encodeURIComponent(DS)+'/info');
  if(INFO.error){ document.querySelector('.rv').innerHTML='<p class="badge b-bad"></p>'; document.querySelector('.rv p').textContent=INFO.error; return; }
  $('dsname').textContent=DS;
  $('b_dl').href='/api/download/'+encodeURIComponent(DS);
  const chip=(t,cls)=>'<span class="badge '+(cls||'')+'" style="margin-right:4px">'+E(t)+'</span>';
  $('chips').innerHTML=chip(INFO.episodes.length+' 에피소드')+chip(INFO.fps+' fps')+chip(INFO.robot_type||'robot ?')
    +(INFO.version?chip(INFO.version, INFO.version==='v3.0'?'':'b-warn'):'')
    +(INFO.env?chip('환경 '+INFO.env):'')+(INFO.project?'<a href="/projects">'+chip('프로젝트 '+INFO.project,'b-run')+'</a>':'');
  paintDel(); buildList();
  if(!INFO.episodes.length){ $('vgrid').innerHTML='<p class=muted style="padding:14px;margin:0">에피소드가 없습니다</p>'; return; }
  const want=parseInt(new URLSearchParams(location.search).get('ep'));
  const i0=Math.max(0, INFO.episodes.findIndex(e=>e.ep===want));
  if(URDF_OK){
    const sides=INFO.bimanual?['left','right']:['main'];
    const views={}; sides.forEach(s=>{ views[s]=VIEWS_CFG[s]||{x:0, y:(s==='left'?0.12:s==='right'?-0.12:0), yaw_deg:0}; });
    $('v3d').style.display='';
    try{ ARM=await window.mountArm3D($('v3d'), sides, views, K3); }
    catch(e){ console.error(e); $('v3d').style.display='none'; ARM=null; }
  }
  select(i0);
})();
</script>"""


WIZARD_HTML = """
<style>
.wgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-bottom:16px}
.wcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px;display:flex;flex-direction:column;gap:8px}
.wcard.on{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.wcard h3{margin:0;font-family:var(--mono);font-size:13px;letter-spacing:.12em;text-transform:uppercase;color:var(--accent)}
.wrow{display:flex;gap:8px;align-items:center;font-size:12.5px;flex-wrap:wrap}
.wrow .k{width:74px;color:var(--muted);font-family:var(--mono);font-size:11.5px}
.modebar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.modebar .tiny,.wpanel .tiny{font-size:11px;color:var(--dim);font-family:var(--mono)}
.stepper{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0 0 14px}
.stepper .st{display:flex;gap:7px;align-items:center;padding:6px 10px;border-radius:8px;cursor:pointer;color:var(--muted);font-size:13px}
.stepper .st.cur{background:var(--surface2);color:var(--text)}
.stepper .st i{font-style:normal;width:22px;height:22px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;
  font-size:12px;background:var(--surface2);border:1px solid var(--line)}
.stepper .st.done i{background:#2e7d4f;border-color:#2e7d4f;color:#fff}
.stepper .st.cur i{background:var(--accent-dim);border-color:var(--accent);color:#fff}
.stepper .sep{width:22px;height:1px;background:var(--line)}
.wbody{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}
.wbody.one{grid-template-columns:1fr}
.wpanel{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.wpanel .note{border-left:3px solid var(--accent);background:rgba(93,157,214,.08);padding:9px 12px;border-radius:0 6px 6px 0;margin:0 0 12px;line-height:1.7}
.wpanel .note.ok{border-color:var(--ok);background:rgba(76,175,110,.08)}
.wpanel .note.bad{border-color:var(--bad);background:rgba(201,96,96,.08)}
.wpanel .note.warn{border-color:var(--warn);background:rgba(217,161,59,.08)}
.wpanel details{margin:6px 0 10px}
.wpanel summary{cursor:pointer;color:var(--muted);font-size:13px}
.stagebox{background:var(--surface2);border:1px solid var(--accent);border-radius:10px;padding:14px 16px}
.stagebox h3{margin:0 0 6px;font-size:15px}
.stagebox .inst{color:var(--muted);margin:0 0 12px;line-height:1.7}
#w3d{height:420px;background:#0a0d10;border:1px solid var(--line);border-radius:10px;position:relative;overflow:hidden}
#w3d canvas{display:block}
.legend{position:absolute;left:10px;bottom:8px;display:flex;gap:10px;font-size:11.5px;color:var(--muted);pointer-events:none}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
.tbar{height:8px;background:var(--surface2);border-radius:4px;overflow:hidden;min-width:120px}
.tbar i{display:block;height:100%;background:var(--warn);width:0}
dl.dev{display:grid;grid-template-columns:auto 1fr;gap:3px 12px;margin:0 0 12px;font-size:12.5px}
dl.dev dt{color:var(--muted);font-family:var(--mono);font-size:11.5px}
dl.dev dd{margin:0;font-family:var(--mono);word-break:break-all}
tr.best td{background:rgba(76,175,110,.10)}
@media(max-width:900px){ .wbody{grid-template-columns:1fr} }
</style>
<div class=wrap>
<p class=eyebrow>처음 설정</p><h2>셋업 마법사</h2>
<p class=muted style="max-width:900px">팔(보드) 하나씩 <b>포트 찾기 → 진단 → 캘리브레이션 → 확인</b> 순서로 안내합니다.
모터를 구동하는 단계는 없습니다 — 손으로 움직여서 확인합니다. 카메라는 <a href="/setup#cameras">셋업</a> 탭에서 합니다.</p>
<div class=modebar id=modebar></div>
<div id=busy></div>
<div class=wgrid id=over></div>
<div id=wiz></div>
<div id=after></div>
</div>
""" + IMPORTMAP_HTML + """
<script type="module">
""" + ARM3D_JS + """
const $=id=>document.getElementById(id);
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function jget(u){ const r=await fetch(u); return r.json(); }
async function jpost(u,b){ const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}); return r.json(); }
const STEPS=[['port','포트 찾기'],['diag','진단'],['calib','캘리브레이션'],['verify','확인']];
const RL={follower:'팔로워',leader:'리더'};
const JOINTS=['shoulder_pan','shoulder_lift','elbow_flex','wrist_flex','wrist_roll','gripper'];
const C_OK='#2dc937', C_WARN='#e5a50a', C_BAD='#d32f2f', C_CUR='#00c7fd';
let W=null, SLOT=null, STEP=null, T1=null, T2=null, T3=null, ARM=null, ARMSIDE=null, WATCHING=false, CALIBING=false, MSING=false;
/* 폴링용 — 서버 재시작·일시 오류에도 폴링 사슬이 끊기지 않게 예외 대신 null */
async function jtry(u){ try{ const r=await fetch(u); return await r.json(); }catch(e){ return null; } }
const slotKey=()=>SLOT?SLOT.side+'|'+SLOT.role:'';
const tail=p=>{ if(!p) return ''; const t=p.split('/').pop(); return t.length>26?'…'+t.slice(-24):t; };
const badge=(t,c)=>'<span class="badge '+(c||'')+'">'+E(t)+'</span>';
function slotOf(side,role){ return W.slots.find(x=>x.side===side&&x.role===role); }
function doneOf(s){ return {port:s.port_ok, diag:!!(s.diag&&s.diag.code<2&&!s.diag.missing.length), calib:s.calib.ok, verify:!!s.verified}; }
function firstTodo(s){ const d=doneOf(s); return (STEPS.find(x=>!d[x[0]])||['verify'])[0]; }
function stopTimers(){ clearTimeout(T1); clearTimeout(T2); clearTimeout(T3); T1=T2=T3=null; }
function boardText(s){
  const b=s.board||{};
  if(b.calib==='unknown') return '';
  if(!s.calib.ok) return b.calib==='calibrated'?badge('보드에 캘리브 있음','b-ok'):'';
  if(b.file==='match') return badge('보드와 일치','b-ok');
  if(b.file==='mismatch') return badge('보드와 다름','b-warn');
  return '';
}

async function load(){ W=await jget('/api/wizard/state'); paintMode(); paintOver(); paintAfter(); }
function paintMode(){
  const bi=W.mode==='bimanual';
  let h='<span class=muted>기종</span>';
  Object.keys(W.robots).forEach(r=>{ h+='<button data-robot="'+E(r)+'" class="'+(W.robot===r?'primary':'')+'">'+E(W.robots[r])+'</button>'; });
  h+='<span style="width:14px"></span><span class=muted>구성</span>'
    +'<button id=wm1 class="'+(bi?'':'primary')+'">한팔</button>'
    +'<button id=wm2 class="'+(bi?'primary':'')+'">양팔 (left / right)</button>'
    +'<span class=tiny>환경 <span class=mono>'+E(W.env)+'</span> · 바꾸면 이 환경 설정이 바뀝니다</span>';
  if(W.plugin_missing && W.plugin_missing.length)
    h+='<p class="badge b-warn" style="flex-basis:100%">양팔 '+E(W.robot_label)+' 롤아웃에 필요한 플러그인 미설치 — 터미널: pip install --no-deps '
      +W.plugin_missing.map(m=>'-e plugins/'+E(m)).join(' ')+'</p>';
  $('modebar').innerHTML=h;
  $('wm1').onclick=()=>setMode('single'); $('wm2').onclick=()=>setMode('bimanual');
  document.querySelectorAll('#modebar button[data-robot]').forEach(b=>b.onclick=()=>setRobot(b.dataset.robot));
}
async function setRobot(r){
  if(W.robot===r) return;
  if(!confirm(W.robots[r]+' 로 바꿉니다. 보드가 달라서 지정된 포트는 비웁니다 (캘리브레이션 파일은 기종별로 따로 보관). 계속할까요?')) return;
  await leave(); SLOT=null; STEP=null; $('wiz').innerHTML='';
  const r2=await jpost('/api/wizard/robot',{robot:r});
  if(r2.error){ alert(r2.error); return; }
  W=r2; paintMode(); paintOver(); paintAfter();
  history.replaceState(null,'',location.pathname);
}
async function setMode(m){
  if(W.mode===m) return;
  if(m==='single' && !confirm('양팔 → 한팔: 왼팔(left)만 남기고 오른팔 설정(포트·카메라)을 지웁니다. 계속할까요?')) return;
  await leave(); SLOT=null; STEP=null; $('wiz').innerHTML='';
  const r=await jpost('/api/wizard/mode',{mode:m});
  if(r.error){ alert(r.error); return; }
  W=r; paintMode(); paintOver(); paintAfter();
  history.replaceState(null,'',location.pathname);
  if(r.copied&&r.copied.length) $('wiz').innerHTML='<div class=wpanel><p class="note ok">같은 팔의 캘리브레이션 파일을 새 이름으로 복사했습니다: <span class=mono>'+E(r.copied.join(', '))+'</span></p></div>';
}
function paintOver(){
  $('busy').innerHTML=W.busy?'<p class="badge b-warn">실행 중: '+E(W.busy)+'</p>':'';
  const g=$('over'); g.innerHTML='';
  W.slots.forEach(s=>{
    const c=document.createElement('div');
    c.className='wcard'+(SLOT&&SLOT.side===s.side&&SLOT.role===s.role?' on':'');
    const port=s.port_ok?badge('연결됨','b-ok')+' <span class=mono>'+E(tail(s.port))+'</span>'
      :(s.port?badge('연결 안 됨','b-bad')+' <span class=mono>'+E(tail(s.port))+'</span>':badge('미지정','b-warn'));
    const dg=s.diag?badge(s.diag.verdict, s.diag.code===0?'b-ok':s.diag.code===1?'b-warn':'b-bad')+(s.diag.power&&s.diag.power.system?' <span class=tiny>'+E(s.diag.power.system)+' · '+E(s.diag.when)+'</span>':''):'<span class=tiny>-</span>';
    const cb=(s.calib.ok?badge('있음','b-ok')+' <span class=tiny>'+E(s.calib.when)+'</span>':badge('없음','b-warn'))+' '+boardText(s);
    const vf=s.verified?badge('확인됨','b-ok')+' <span class=tiny>'+E(s.verified)+'</span>':(s.verified_stale?badge('다시 확인 필요','b-warn'):'<span class=tiny>-</span>');
    const all=doneOf(s), complete=all.port&&all.diag&&all.calib&&all.verify;
    c.innerHTML='<h3>'+E(s.side)+' · '+RL[s.role]+'</h3>'
      +'<div class=wrow><span class=k>포트</span>'+port+'</div>'
      +'<div class=wrow><span class=k>진단</span>'+dg+'</div>'
      +'<div class=wrow><span class=k>캘리브</span>'+cb+'</div>'
      +'<div class=wrow><span class=k>확인</span>'+vf+'</div>';
    const b=document.createElement('button'); b.className=complete?'':'primary'; b.style.marginTop='4px';
    b.textContent=complete?'다시 보기':(all.port||all.calib?'이어서 하기':'설정하기');
    b.onclick=()=>openSlot(s.side,s.role,complete?'port':firstTodo(s));
    c.appendChild(b); g.appendChild(c);
  });
}
function paintAfter(){
  const all=W.slots.every(s=>{ const d=doneOf(s); return d.port&&d.calib&&d.verify; });
  $('after').innerHTML='<div class=card style="margin-top:16px">'
    +(all?'<p class="badge b-ok" style="margin-top:0">모든 팔 준비 완료 — 제어 탭에서 연결해 보세요</p>':'')
    +'<p style="margin-top:0"><b>카메라</b> — '+(W.cameras.length?W.cameras.length+'대 등록 ('+E(W.cameras.join(', '))+')':'아직 없음')
    +' · <a href="/setup#cameras">셋업 › 3 · 카메라</a>에서 화면으로 확인하고 추가하세요.</p>'
    +'<p style="margin-bottom:0"><b>환경</b> — 지금 구성은 <span class=mono>'+E(W.env)+'</span> 환경에 저장돼 있습니다 · '
    +'<a href="/setup#envcard">환경 관리</a> (작업대가 여러 개면 이름을 나눠 두세요)</p></div>';
}

async function openSlot(side,role,step){
  await leave();
  SLOT={side:side,role:role}; paintOver();
  go(step||firstTodo(slotOf(side,role)));
  $('wiz').scrollIntoView({behavior:'smooth',block:'start'});
}
async function leave(){
  stopTimers();
  if(WATCHING){ WATCHING=false; await jpost('/api/setup/watch/stop'); }
  if(STEP==='verify'){ await jpost('/api/wizard/verify/stop'); }
  if(CALIBING){ CALIBING=false; await jpost('/api/calib/cancel'); }      // 저장 안 한 캘리브는 이전 값으로 되돌립니다
  if(MSING){ MSING=false; await jpost('/api/setup/motors/cancel'); }     // 모터 ID 세팅이 포트를 계속 잡고 있지 않게
  ARM=null;
}
async function go(step){
  await leave(); STEP=step;
  const s=slotOf(SLOT.side,SLOT.role), d=doneOf(s);
  history.replaceState(null,'','?slot='+SLOT.side+'|'+SLOT.role+'&step='+step);
  let h='<div class=stepper>';
  STEPS.forEach((x,i)=>{ if(i) h+='<span class=sep></span>';
    h+='<span class="st'+(x[0]===step?' cur':'')+(d[x[0]]?' done':'')+'" data-step="'+x[0]+'"><i>'+(d[x[0]]&&x[0]!==step?'&#10003;':(i+1))+'</i>'+x[1]+'</span>'; });
  h+='<span style="flex:1"></span><span class=mono style="color:var(--accent)">'+E(SLOT.side)+' · '+RL[SLOT.role]+'</span></div><div id=wstep></div>';
  $('wiz').innerHTML=h;
  document.querySelectorAll('.stepper .st').forEach(el=>el.onclick=()=>go(el.dataset.step));
  ({port:stepPort,diag:stepDiag,calib:stepCalib,verify:stepVerify})[step](s);
}
async function refreshSlot(){ W=await jget('/api/wizard/state'); paintOver(); paintAfter(); return slotOf(SLOT.side,SLOT.role); }

/* ---------- 3D (진단·캘리브·확인 공용) ---------- */
function legend(items){ return '<div class=legend>'+items.map(x=>'<span><i style="background:'+x[0]+'"></i>'+E(x[1])+'</span>').join('')+'</div>'; }
async function mount3D(leg){
  const el=$('w3d'); if(!el) return null;
  const my=STEP;
  if(!W.urdf){ el.innerHTML='<p class=muted style="padding:14px">'+E(W.kind3d.urdf)+' 가 없어 3D 를 못 그립니다 — 표의 숫자로 확인하세요.</p>'; ARM=null; return null; }
  el.innerHTML=leg?legend(leg):'';
  try{ const v={}; v[SLOT.side]={x:0,y:0,yaw_deg:0}; const a=await window.mountArm3D(el,[SLOT.side],v,W.kind3d);
       if(STEP!==my || $('w3d')!==el) return null; ARMSIDE=SLOT.side; ARM=a; return a; }
  catch(e){ console.error(e); ARM=null; el.innerHTML='<p class=muted style="padding:14px">3D 를 불러오지 못했습니다 (인터넷 연결 필요) — 표의 숫자로 확인하세요.</p>'; return null; }
}
function hlDiag(levels){
  const m={}; JOINTS.forEach(j=>{ const l=levels[j]; m[j]=l==='MISSING'||l==='FAIL'?C_BAD:l==='WARN'?C_WARN:l?C_OK:null; }); return m;
}

/* ---------- ① 포트 찾기 ---------- */
function stepPort(s){
  const who=E(SLOT.side)+' '+RL[SLOT.role];
  $('wstep').innerHTML='<div class="wbody one"><div class=wpanel>'
    +'<p class=note><b>'+who+'</b> 팔만 손으로 이리저리 움직이세요. 움직인 팔의 포트가 초록으로 표시됩니다.<br>'
    +'감시를 켜면 <b>모든 포트의 토크가 꺼집니다</b> — 팔로워가 들려 있으면 주저앉으니 받치거나 내려놓으세요.</p>'
    +'<div class=toolbar><button class=primary id=wwatch>포트 감시 시작</button>'
    +(s.port_ok?'<button id=wkeep>지금 포트 그대로 — 다음</button>':'')
    +'<span class=muted id=wsug></span></div>'
    +'<table id=wtbl style="margin-top:10px"></table>'
    +'<p class=tiny style="margin-top:8px">travel = 감시 시작 뒤 엔코더가 움직인 폭 (tick, 4096 = 한 바퀴). 보드를 못 찾으면 USB 를 다시 꽂고 새로고침하세요.</p>'
    +'</div></div>';
  $('wwatch').onclick=watchToggle;
  if($('wkeep')) $('wkeep').onclick=()=>go('diag');
  paintPorts(null);
}
function whoHas(dev){
  const s=W.slots.find(x=>x.dev===dev); return s?s.side+' · '+RL[s.role]:'';
}
function paintPorts(st){
  const t=$('wtbl'); if(!t) return;
  const trav={}; let best=null, second=0;
  W.ports.forEach(p=>{ const w=st&&st[p.dev]; const sp=w&&w.span?Object.values(w.span):[]; trav[p.dev]=sp.length?Math.max.apply(null,sp):null; });
  const vals=Object.entries(trav).filter(x=>x[1]!=null).sort((a,b)=>b[1]-a[1]);
  if(vals.length && vals[0][1]>60){ second=vals[1]?vals[1][1]:0; if(vals[0][1]>=second*3) best=vals[0][0]; }
  let h='<tr><th>포트</th><th>보드</th><th>지금 지정</th><th>움직임(travel)</th><th></th></tr>';
  W.ports.forEach(p=>{
    const tv=trav[p.dev], pct=tv==null?0:Math.min(100,tv/400*100);
    h+='<tr'+(best===p.dev?' class=best':'')+'><td class=mono>'+E(p.dev)+'</td>'
      +'<td class=tiny>'+E((p.usb&&p.usb.product)||'')+(p.usb&&p.usb.serial?' · sn '+E(p.usb.serial):'')+'</td>'
      +'<td class=tiny>'+E(whoHas(p.dev))+'</td>'
      +'<td><div style="display:flex;gap:8px;align-items:center"><div class=tbar><i style="width:'+pct+'%;background:'+(best===p.dev?'var(--ok)':'var(--warn)')+'"></i></div><span class=mono>'+(tv==null?'-':tv)+'</span></div>'
      +(st&&st[p.dev]&&st[p.dev].err?'<span class=tiny style="color:var(--bad)">'+E(st[p.dev].err)+'</span>':'')+'</td>'
      +'<td style="text-align:right"><button data-dev="'+E(p.dev)+'" class="'+(best===p.dev?'primary':'')+'">이 포트로 지정</button></td></tr>';
  });
  if(!W.ports.length) h+='<tr><td colspan=5 class=muted>시리얼 포트가 없습니다 — USB 와 dialout 권한을 확인하세요</td></tr>';
  t.innerHTML=h;
  t.querySelectorAll('button[data-dev]').forEach(b=>b.onclick=()=>assign(b.dataset.dev,b));
  $('wsug').innerHTML=best?'<b style="color:var(--ok)">'+E(best)+'</b> 가 움직였습니다 — 맞으면 지정하세요':(WATCHING?'팔을 움직이는 중… ':'');
}
async function watchToggle(){
  if(WATCHING){ WATCHING=false; stopTimers(); await jpost('/api/setup/watch/stop'); $('wwatch').textContent='포트 감시 시작'; return; }
  const r=await jpost('/api/setup/watch',{ports:W.ports.map(p=>p.dev)});
  if(r.error){ alert(r.error); return; }
  WATCHING=true; $('wwatch').textContent='감시 중지';
  const poll=async()=>{ if(!WATCHING) return; const st=await jtry('/api/setup/watch'); if(st&&st.on) paintPorts(st.state); T1=setTimeout(poll,400); };
  poll();
}
async function assign(dev,btn){
  if(btn) btn.disabled=true;
  let r;
  try{
    if(WATCHING){ WATCHING=false; stopTimers(); await jpost('/api/setup/watch/stop'); }
    r=await jpost('/api/wizard/assign',{side:SLOT.side,role:SLOT.role,dev:dev});
  }catch(e){ r={error:'요청 실패: '+e}; }
  if(r.error){ alert(r.error); if(btn) btn.disabled=false; return; }
  W=r; paintOver(); paintAfter(); go('diag');
}

/* ---------- ② 진단 ---------- */
async function stepDiag(s){
  if(!s.port_ok){ $('wstep').innerHTML='<div class=wpanel><p class="note bad">포트가 지정되지 않았거나 연결돼 있지 않습니다.</p><button class=primary id=wback>포트 찾기로</button></div>'; $('wback').onclick=()=>go('port'); return; }
  $('wstep').innerHTML='<div class=wbody><div class=wpanel><p class=muted>진단 중… 서보에 아무것도 쓰지 않습니다 (몇 초)</p></div><div id=w3d></div></div>';
  mount3D(W.kind3d.motor_mat?[[C_OK,'정상'],[C_WARN,'주의'],[C_BAD,'불량·응답 없음']]:null);   // 모터 색 표시는 서보 메시가 따로 있는 기종만
  const key=slotKey();
  let r; try{ r=await jpost('/api/setup/armcheck/start',{port:s.port, role:SLOT.role, sweep:false}); }catch(e){ r={error:String(e)}; }
  if(STEP!=='diag' || slotKey()!==key) return;       // 진단 도중 다른 팔로 옮겼으면 결과를 버립니다
  const st=r.state||{};
  if(r.error || st.stage==='error'){ $('wstep').innerHTML='<div class=wpanel><p class="note bad"></p><button id=wre>다시 진단</button></div>'; $('wstep').querySelector('.note').textContent='진단 실패 — '+(r.error||st.err); $('wre').onclick=()=>go('diag'); return; }
  const rep=st.report; s=await refreshSlot();
  if(STEP!=='diag' || slotKey()!==key) return;
  const missing=Object.keys(rep.motors).filter(j=>rep.motors[j].model==null);
  const powerFail=rep.findings.some(f=>f.check==='전원'&&f.level==='FAIL');
  const happy=rep.code===0 && !missing.length;
  const cls=rep.code===0?'ok':rep.code===2?'bad':'warn';
  const nOk=Object.keys(rep.motors).length-missing.length;
  const volts=Object.values(rep.motors).map(m=>m.voltage_v).filter(v=>v!=null);
  let h='<div class=wpanel>'
    +'<p class="note '+cls+'">판정: <b>'+E(rep.verdict)+'</b> · 모터 '+nOk+'/'+Object.keys(rep.motors).length
    +(rep.power&&rep.power.system?' · 전원 '+E(rep.power.system)+' 계통 (중앙값 '+rep.power.median_v+' V)':(volts.length?' · '+Math.min.apply(null,volts).toFixed(1)+' V':''))+'</p>';
  const b=s.board||{};
  if(W.calib_kind!=='factory') h+='<p style="margin:0 0 10px">보드 캘리브레이션: '+({calibrated:badge('저장돼 있음','b-ok'),default:badge('없음 (공장값)','b-warn'),partial:badge('일부만','b-warn'),unknown:badge('모름')})[b.calib||'unknown']
    +(s.calib.ok?' · 파일 '+({match:badge('보드와 일치','b-ok'),mismatch:badge('보드와 다름: '+(b.diff||[]).join(', '),'b-warn')})[b.file]||'':'')+'</p>';
  let tbl='<table><tr><th>ID</th><th>관절</th><th class=num>전압</th><th class=num>온도</th><th class=num>흔들림</th><th>결과</th></tr>';
  Object.keys(rep.motors).forEach(j=>{ const m=rep.motors[j];
    tbl+='<tr><td class=mono>'+m.id+'</td><td class=mono>'+E(j)+'</td><td class=num>'+(m.voltage_v==null?'-':m.voltage_v.toFixed(1)+' V')+'</td>'
      +'<td class=num>'+(m.temp_c==null?'-':m.temp_c+'°')+'</td><td class=num>'+(m.noise_ticks==null?'-':m.noise_ticks)+'</td>'
      +'<td>'+badge(m.model==null?'응답 없음':({OK:'정상',INFO:'정상',WARN:'주의',FAIL:'불량'})[m.level], m.level==='FAIL'||m.model==null?'b-bad':m.level==='WARN'?'b-warn':'')+'</td></tr>'; });
  tbl+='</table>'+rep.findings.filter(f=>f.level==='WARN'||f.level==='FAIL').map(f=>'<p style="margin:5px 0">'+badge(f.level,f.level==='FAIL'?'b-bad':'b-warn')+' <span class=mono>'+E(f.joint||'팔 전체')+'</span> · '+E(f.message)+'</p>').join('');
  h+=happy?'<details><summary>모터별 자세히</summary>'+tbl+'</details>':tbl;
  if(missing.length){
    h+='<div class=stagebox style="margin-top:14px" id=wms><h3>모터 ID 세팅이 필요합니다</h3>'
      +'<p class=inst>응답하지 않는 모터: <b>'+E(missing.join(', '))+'</b>. 새 모터는 전부 ID 1 이라 한 개씩만 보드에 꽂아 ID 를 써야 합니다. '
      +'오른쪽 3D 에서 <b style="color:'+C_CUR+'">하늘색</b> 모터가 지금 연결할 모터입니다.</p>'
      +'<div id=wmsbody><button class=primary id=wmsgo>모터 ID 세팅 시작</button></div></div>';
  }
  h+='<div class=toolbar style="margin-top:14px"><button id=wre>다시 진단</button>'
    +'<button class=primary id=wnext '+(powerFail||missing.length?'disabled':'')+'>다음: 캘리브레이션</button>'
    +(powerFail?'<span class="badge b-bad">전원을 먼저 바로잡으세요</span>':'')+'</div></div>';
  $('wstep').querySelector('.wpanel').outerHTML=h;
  $('wre').onclick=()=>go('diag'); $('wnext').onclick=()=>go('calib');
  if($('wmsgo')) $('wmsgo').onclick=()=>msStart(s);
  const lv=(s.diag&&s.diag.levels)||{};
  const paint=()=>{ if(ARM) ARM.highlight(SLOT.side,hlDiag(lv)); else if(STEP==='diag') T3=setTimeout(paint,300); };
  paint();
}
async function msStart(s){
  if(!confirm('모터를 한 개씩만 보드에 연결한 상태여야 합니다. 시작할까요?')) return;
  const r=await jpost('/api/setup/motors/start',{port:s.dev||s.port, role:SLOT.role});
  if(r.error){ alert(r.error); return; }
  MSING=true;
  msPoll();
}
async function msPoll(){
  const m=await jtry('/api/setup/motors'); const b=$('wmsbody'); if(!b||STEP!=='diag') return;
  if(!m){ setTimeout(msPoll,1000); return; }
  if(m.stage!=='running') MSING=false;
  if(ARM && m.order){ const hm={}; (m.done||[]).forEach(d=>hm[d.name]=C_OK); if(m.stage==='running'&&m.current) hm[m.current]=C_CUR; ARM.highlight(SLOT.side,hm); }
  if(m.stage==='running'){
    b.innerHTML='<p>지금 연결할 모터: <b class=mono>'+E(m.current)+'</b> → ID <b>'+E(m.current_id)+'</b> · 완료 '+m.done.length+'/'+m.order.length+'</p>'
      +(m.err?'<p class="badge b-bad"></p>':'')+(m.last?'<p class=tiny>'+E(m.last)+'</p>':'')
      +'<div class=toolbar><button class=primary id=wmsw>ID 쓰기</button><button id=wmsc>취소</button></div>';
    if(m.err) b.querySelector('.b-bad').textContent=m.err;
    $('wmsw').onclick=async()=>{ $('wmsw').disabled=true; await jpost('/api/setup/motors/write'); msPoll(); };
    $('wmsc').onclick=async()=>{ MSING=false; await jpost('/api/setup/motors/cancel'); go('diag'); };
  }else if(m.stage==='done'){
    b.innerHTML='<p class="badge b-ok">6개 모두 ID 기록 완료 — 모터를 전부 다시 연결하고 다시 진단하세요</p>';
  }else if(m.stage==='error'){
    b.innerHTML='<p class="badge b-bad"></p>'; b.firstChild.textContent=m.err;
  }
}

/* ---------- ③ 캘리브레이션 ---------- */
function stepFactory(s){
  /* OMX: lerobot 이 공장값(오프셋 0, 범위 0~4095)을 캘리브레이션으로 씁니다 — 범위 기록이 없습니다 */
  const fname='<span class=mono>'+E(s.calib.id)+'.json</span>';
  $('wstep').innerHTML='<div class="wbody one"><div class=wpanel id=wcp>'
    +(s.calib.ok?'<p class="note ok">캘리브레이션 파일 '+fname+' 이 있습니다 ('+E(s.calib.when)+')</p>'
                :'<p class=note>캘리브레이션 파일이 없습니다 — '+fname+'</p>')
    +'<p class=muted>이 기종('+E(W.robot_label)+')은 범위를 기록하지 않고 lerobot 의 공장값(오프셋 0, 범위 0~4095)을 씁니다. '
    +'버튼을 누르면 토크를 끄고 운전 모드·회전 방향·공장값을 모터에 쓰고 파일을 저장합니다. 팔로워는 받치세요.</p>'
    +'<div class=toolbar>'+(s.calib.ok?'<button class=primary id=wnext>그대로 쓰고 다음: 확인</button><button id=wfac>공장값 다시 쓰기</button>'
                                       :'<button class=primary id=wfac>공장값 쓰기</button>')+'</div></div></div>';
  if($('wnext')) $('wnext').onclick=()=>go('verify');
  $('wfac').onclick=async()=>{
    if(!confirm('토크를 끄고 공장값 캘리브레이션을 씁니다. 계속할까요?')) return;
    $('wfac').disabled=true;
    const r=await jpost('/api/calib/factory',{side:SLOT.side,role:SLOT.role});
    if(r.error){ alert(r.error); $('wfac').disabled=false; return; }
    await refreshSlot(); go('verify');
  };
}
function stepCalib(s){
  if(W.calib_kind==='factory') return stepFactory(s);
  const back='/setup/wizard?slot='+SLOT.side+'|'+SLOT.role+'&step=verify';
  const link='/calib?side='+encodeURIComponent(SLOT.side)+'&role='+SLOT.role+'&next='+encodeURIComponent(back);
  const b=s.board||{}, fname='<span class=mono>'+E(s.calib.id)+'.json</span>';
  let h='<div class=wbody><div class=wpanel id=wcp>', btns='';
  if(s.calib.ok && b.file==='match'){
    h+='<p class="note ok">캘리브레이션 파일 '+fname+' ('+E(s.calib.when)+') 이 보드에 저장된 값과 <b>일치</b>합니다.</p>';
    btns='<button class=primary id=wnext>그대로 쓰고 다음: 확인</button><button id=wnew>새로 캘리브레이션</button>';
  }else if(s.calib.ok && b.file==='mismatch'){
    h+='<p class="note warn">파일 '+fname+' 과 보드에 저장된 값이 다릅니다 ('+E((b.diff||[]).join(', '))+').<br>'
      +'다른 팔의 파일이거나, 다른 PC 에서 이 팔을 다시 캘리브레이션한 경우입니다. 녹화·제어를 시작하면 <b>파일 값이 보드에 다시 써집니다.</b></p>';
    btns='<button class=primary id=wnew>새로 캘리브레이션</button><button id=wimp>보드 값으로 파일 맞추기</button><button id=wnext>파일 그대로 — 확인에서 보기</button>';
  }else if(s.calib.ok){
    h+='<p class="note ok">캘리브레이션 파일이 있습니다 — '+fname+' ('+E(s.calib.when)+')</p>'
      +'<p class=muted>같은 팔이면 그대로 쓰고 다음 단계에서 3D 로 확인하세요. 진단을 먼저 하면 보드 값과 비교해 드립니다.</p>';
    btns='<button class=primary id=wnext>그대로 쓰고 다음: 확인</button><button id=wnew>새로 캘리브레이션</button>';
  }else if(b.calib==='calibrated'){
    h+='<p class="note ok">파일 '+fname+' 은 없지만 <b>보드(모터 EEPROM)에 캘리브레이션이 저장돼 있습니다.</b><br>'
      +'이미 캘리브레이션한 팔을 새 PC 에 꽂은 경우입니다 — 가져오면 다시 할 필요가 없습니다. 모터에는 아무것도 쓰지 않습니다.</p>';
    btns='<button class=primary id=wimp>보드에서 가져오기 (추천)</button><button id=wnew>새로 캘리브레이션</button>';
  }else{
    h+='<p class=note>캘리브레이션 파일이 없습니다 — '+fname+(b.calib==='default'?' · 보드도 공장값입니다':'')+'</p>';
    btns='<button class=primary id=wnew>캘리브레이션 시작</button>';
  }
  h+='<div class=toolbar>'+btns+'</div><div id=wcal style="margin-top:14px"></div>'
    +'<p class=tiny style="margin-top:10px">예전 화면이 편하면 <a href="'+E(link)+'">캘리브레이션 탭에서 하기</a> — 저장하면 이 마법사로 돌아옵니다.</p>'
    +'</div><div id=w3d></div></div>';
  $('wstep').innerHTML=h;
  if($('wnext')) $('wnext').onclick=()=>go('verify');
  if($('wnew')) $('wnew').onclick=calStart;
  if($('wimp')) $('wimp').onclick=importCalib;
  mount3D([[C_OK,'범위 기록됨'],[C_WARN,'더 움직이세요'],[C_BAD,'아직 안 움직임'],[C_CUR,'자동 (wrist_roll)']]);
}
async function importCalib(){
  const btn=$('wimp'); if(btn) btn.disabled=true;
  const r=await jpost('/api/wizard/import_calib',{side:SLOT.side,role:SLOT.role});
  if(r.error){ alert(r.error); if(btn) btn.disabled=false; return; }
  W=r; paintOver(); paintAfter();
  go('verify');
}
async function calStart(){
  const who=SLOT.side+' '+RL[SLOT.role];
  if(!confirm(who+' 캘리브레이션을 시작합니다. 토크가 꺼집니다'+(SLOT.role==='follower'?' — 팔로워가 주저앉지 않게 받치세요.':'.')+' 계속할까요?')) return;
  const btns=document.querySelectorAll('#wcp .toolbar button'); btns.forEach(b=>b.disabled=true);
  const r=await jpost('/api/calib/start',{side:SLOT.side,role:SLOT.role});
  if(r.error){ alert(r.error); btns.forEach(b=>b.disabled=false); return; }
  CALIBING=true;
  $('wcal').innerHTML='<div class=stagebox><h3>범위 기록 중</h3>'
    +'<p class=inst>각 관절을 <b>기계적 한계 양 끝까지</b> 한 번씩 천천히 움직이세요 (한 관절씩). 3D 의 모터가 모두 <b style="color:'+C_OK+'">초록</b>이 되면 저장하세요.<br>'
    +'wrist_roll 은 범위를 기록하지 않습니다 — <b>저장할 때의 자세가 0°</b> 이니 그리퍼를 똑바로 두고 저장하세요.</p>'
    +'<div id=wcerr></div><table id=wctbl></table>'
    +'<div class=toolbar style="margin-top:12px"><button class=primary id=wcsave disabled>저장</button><button id=wccancel>취소 (이전 값으로 되돌림)</button></div></div>';
  $('wcsave').onclick=calSave; $('wccancel').onclick=async()=>{ CALIBING=false; stopTimers(); await jpost('/api/calib/cancel'); go('calib'); };
  calPoll();
}
async function calPoll(){
  if(!CALIBING || STEP!=='calib') return;
  const c=await jtry('/api/calib/state');
  if(!CALIBING || STEP!=='calib') return;
  if(!c){ T2=setTimeout(calPoll,1000); return; }
  if(c.stage!=='ranging' || c.side!==SLOT.side || c.role!==SLOT.role){
    CALIBING=false; $('wcal').innerHTML='<p class="note bad"></p>'; $('wcal').firstChild.textContent='캘리브레이션이 중단됐습니다 '+(c.err?'— '+c.err:'(다른 화면에서 취소됨)'); return; }
  const blk=new Set(c.block), wrn=new Set(c.warn), ovr=new Set(c.over), hm={};
  let h='<tr><th>관절</th><th class=num>현재</th><th class=num>min</th><th class=num>max</th><th>범위</th></tr>';
  c.rows.forEach(r=>{
    const col=r.full_turn?C_CUR:(blk.has(r.name)||ovr.has(r.name))?C_BAD:wrn.has(r.name)?C_WARN:C_OK;
    hm[r.name]=col;
    const pct=r.full_turn?100:Math.min(100,(r.span_deg||0)/Math.max(c.span_ok_deg,1)*100);
    h+='<tr><td class=mono>'+E(r.name)+'</td><td class="num mono">'+(r.pos==null?'-':r.pos)+'</td>'
      +'<td class="num mono">'+(r.full_turn?'-':(r.min==null?'-':r.min))+'</td><td class="num mono">'+(r.full_turn?'-':(r.max==null?'-':r.max))+'</td>'
      +'<td><div style="display:flex;gap:8px;align-items:center"><div class=tbar><i style="width:'+pct+'%;background:'+col+'"></i></div>'
      +'<span class=tiny>'+(r.full_turn?'자동':(r.span_deg==null?'-':r.span_deg+'°'))+'</span></div></td></tr>';
  });
  $('wctbl').innerHTML=h;
  let e='';
  if(c.over.length) e+='<p class="note bad">한 바퀴 넘게 돈 관절: '+E(c.over.join(', '))+' — 기계적 한계 안에서만 움직이세요 (취소 후 다시)</p>';
  if(c.err) e+='<p class="note bad">'+E(c.err)+'</p>';
  if($('wcerr').innerHTML!==e) $('wcerr').innerHTML=e;
  $('wcsave').disabled=!!(c.block.length||c.over.length);
  $('wcsave').title=c.block.length?'아직 안 움직인 관절: '+c.block.join(', '):'';
  if(ARM) ARM.highlight(SLOT.side,hm);
  T2=setTimeout(calPoll,200);
}
async function calSave(){
  const c=await jget('/api/calib/state');
  if(c.warn.length && !confirm('범위가 좁은 관절이 있습니다: '+c.warn.join(', ')+' — 그래도 저장할까요?')) return;
  $('wcsave').disabled=true;
  const r=await jpost('/api/calib/finish');
  if(r.error){ alert(r.error); $('wcsave').disabled=false; return; }
  CALIBING=false; stopTimers();
  await refreshSlot();
  go('verify');
}

/* ---------- ④ 확인 ---------- */
async function stepVerify(s){
  if(!s.calib.ok){ $('wstep').innerHTML='<div class=wpanel><p class=note>캘리브레이션이 먼저 필요합니다.</p><button class=primary id=wb>캘리브레이션으로</button></div>'; $('wb').onclick=()=>go('calib'); return; }
  const usb=s.usb||{};
  $('wstep').innerHTML='<div class=wbody><div class=wpanel>'
    +'<p class=note>팔을 손으로 움직여 보세요. <b>오른쪽 3D 가 실물과 같은 방향·같은 각도로 움직이면</b> 완료를 누르세요.</p>'
    +'<dl class=dev><dt>포트</dt><dd>'+E(s.port)+(s.dev&&s.dev!==s.port?' → '+E(s.dev):'')+'</dd>'
    +'<dt>보드</dt><dd>'+E(usb.product||'-')+(usb.serial?' · sn '+E(usb.serial):'')+'</dd>'
    +'<dt>캘리브</dt><dd>'+E(s.calib.path)+' ('+E(s.calib.when)+')</dd></dl>'
    +'<div id=wvwarn></div><table id=wvtbl></table>'
    +'<div class=toolbar style="margin-top:14px"><button class=primary id=wok disabled title="관절값이 들어와야 누를 수 있습니다">일치함 — 완료</button>'
    +'<button id=wcal>다시 캘리브레이션</button></div>'
    +'<p class=tiny style="margin-top:8px">읽기만 합니다. 방향이 반대거나 각도가 어긋나면 다른 팔의 캘리브레이션 파일이거나 캘리브 때 자세가 틀린 것입니다.</p>'
    +'</div><div id=w3d><p class=muted style="padding:14px">3D 불러오는 중…</p></div></div>';
  $('wcal').onclick=()=>go('calib');
  $('wok').onclick=async()=>{ const r=await jpost('/api/wizard/verified',{side:SLOT.side,role:SLOT.role}); if(r.error){ alert(r.error); return; } W=r; stopTimers(); STEP=null; ARM=null; paintOver(); paintAfter();
    const nx=W.slots.find(x=>{ const d=doneOf(x); return !(d.port&&d.calib&&d.verify); });
    $('wiz').innerHTML='<div class=wpanel><p class="note ok"><b>'+E(SLOT.side)+' · '+RL[SLOT.role]+'</b> 확인 완료</p>'
      +(nx?'<button class=primary id=wnx>다음 팔: '+E(nx.side)+' · '+RL[nx.role]+'</button>':'<p>모든 팔이 준비됐습니다. 아래에서 카메라와 환경을 확인하세요.</p>')+'</div>';
    if(nx) $('wnx').onclick=()=>openSlot(nx.side,nx.role); };
  const r=await jpost('/api/wizard/verify/start',{side:SLOT.side,role:SLOT.role});
  if(STEP!=='verify') { jpost('/api/wizard/verify/stop'); return; }
  if(r.error){ $('wvwarn').innerHTML='<p class="note bad"></p>'; $('wvwarn').firstChild.textContent='읽기 시작 실패 — '+r.error;
    $('w3d').innerHTML='<p class=muted style="padding:14px">관절값을 못 읽어 3D 를 표시하지 않습니다.</p>'; return; }
  mount3D(null);
  vpoll();
}
async function vpoll(){
  if(STEP!=='verify') return;
  const v=await jtry('/api/wizard/verify');
  if(STEP!=='verify') return;
  if(!v){ T2=setTimeout(vpoll,1000); return; }
  const t=$('wvtbl');
  if(t){
    let h='<tr><th>관절</th><th class=num>값</th></tr>';
    Object.keys(v.joints).forEach(j=>{ h+='<tr><td class=mono>'+E(j)+'</td><td class="num mono">'+v.joints[j].toFixed(1)+(j==='gripper'?' %':(W.unit||''))+'</td></tr>'; });
    t.innerHTML=h;
  }
  if($('wok')) $('wok').disabled=!(v.on && Object.keys(v.joints).length && !v.err);
  let w='';
  if(v.torque_on.length) w+='<p class="note bad">토크가 켜져 있어 손으로 안 움직입니다 ('+E(v.torque_on.join(', '))+') '
    +'<button id=wtq class=danger style="margin-left:8px">토크 끄기 — 팔을 받치세요</button></p>';
  v.warn.forEach(x=>{ w+='<p class=note>'+E(x)+'</p>'; });
  if(v.err) w+='<p class="note bad">'+E(v.err)+'</p>';
  if($('wvwarn') && $('wvwarn').innerHTML!==w){ $('wvwarn').innerHTML=w;
    if($('wtq')) $('wtq').onclick=async()=>{ const r=await jpost('/api/wizard/verify/torque_off'); if(r.error) alert(r.error); }; }
  if(ARM) ARM.update(ARMSIDE, v.joints);
  T2=setTimeout(vpoll,100);
}

addEventListener('pagehide',()=>{
  if(STEP==='verify') navigator.sendBeacon('/api/wizard/verify/stop');
  if(WATCHING) navigator.sendBeacon('/api/setup/watch/stop');
  if(CALIBING) navigator.sendBeacon('/api/calib/cancel');
  if(MSING) navigator.sendBeacon('/api/setup/motors/cancel');
});
(async()=>{
  await load();
  const q=new URLSearchParams(location.search), slot=(q.get('slot')||'').split('|');
  if(slot.length===2 && slotOf(slot[0],slot[1])) openSlot(slot[0],slot[1],q.get('step')||null);
})();
</script>"""

SETUP_HTML = """
<style>
.armcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:14px}
.armcard h3{margin:0 0 12px;font-family:var(--mono);font-size:12px;letter-spacing:.16em;
  text-transform:uppercase;color:var(--accent)}
.slot{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px}
.slot .role{font-family:var(--mono);font-size:12px;width:74px;color:var(--muted)}
.slot input.port{flex:1;min-width:240px;font-family:var(--mono);font-size:12.5px}
.slot input.cid{width:130px;font-family:var(--mono);font-size:12.5px}
.tiny{font-size:11px;color:var(--dim);font-family:var(--mono)}
.camthumb{width:160px;height:120px;object-fit:cover;background:#000;border-radius:6px;display:block;
  border:1px solid var(--line)}
.camthumb.noimg{display:flex;align-items:center;justify-content:center;color:var(--dim);
  font-family:var(--mono);font-size:11px}
.stagebox{background:var(--surface);border:1px solid var(--accent);border-radius:10px;padding:16px 18px}
.stagebox h3{margin:0 0 6px;font-size:15px}
.stagebox .inst{color:var(--muted);margin:0 0 12px;line-height:1.7}
</style>
<div class=wrap>
<p class=eyebrow>하드웨어 설정</p><h2>셋업</h2>
<div id=busywarn></div>
<div class="card needcard wizcard"><div class=needtxt><b>처음이거나 팔을 새로 연결했다면 — 셋업 마법사</b>
  <span class=muted>팔 하나씩 포트 찾기 → 진단 → 캘리브레이션 → 3D 확인 순서로 안내합니다.
  이 화면은 카메라·환경 등록과 세부 설정용입니다.</span></div>
  <a class="btnlink primary" href="/setup/wizard">셋업 마법사 시작</a></div>

<p class=eyebrow id=envcard>0 · 환경</p>
<div class=card>
  <p class=muted style="margin-top:0">환경은 하드웨어 구성 한 벌입니다 — 한팔/양팔, 포트, 캘리브 id, 카메라, fps.
  아래에서 <b>설정 저장</b>을 누르면 <b>사용 중인 환경</b>에 반영됩니다. 작업대나 팔 세트가 여러 개면 환경을 나눠 두고 전환하세요.
  수집한 데이터셋에는 어느 환경으로 찍었는지 기록됩니다.</p>
  <table id=envtbl></table>
  <div class=toolbar style="margin-top:10px">
    <button onclick="envSaveAs()">저장된 지금 구성을 새 환경으로 복사</button>
    <span class=muted id=envmsg></span>
  </div>
</div>

<p class=eyebrow>1 · 기종 · 모드</p>
<div class=toolbar>
  <span class=muted>기종</span>
  <button id=r_so101 onclick="setRobot('so101')">SO-ARM101</button>
  <button id=r_omx onclick="setRobot('omx')">OMX</button>
  <span style="width:14px"></span><span class=muted>구성</span>
  <button id=b1 onclick="setMode('single')">한팔</button>
  <button id=b2 onclick="setMode('bimanual')">양팔 (left / right)</button>
  <span class=muted id=modehint></span>
</div>
<div id=arms></div>

<p class=eyebrow>2 · USB 시리얼 포트</p>
<div class=card>
  <div class=toolbar>
    <button onclick="withBusy(this,loadPorts)">다시 스캔</button>
    <button id=bwatch class=primary onclick="withBusy(this,toggleWatch)">포트 감시 시작 (팔 판별)</button>
    <span class=muted>감시를 켜고 팔 하나를 손으로 움직이면 그 포트의 travel 이 올라갑니다.
      전기적으로는 leader/follower 를 구분할 수 없어서, 이게 확실한 판별 방법입니다.</span>
    <p class="badge b-warn" style="margin:8px 0 0">감시는 모든 포트의 토크를 끕니다 —
      팔로워가 들려 있으면 그대로 주저앉습니다. 팔을 받치거나 내려놓고 시작하세요.</p>
  </div>
  <table id=porttbl></table>
  <details class=more style="margin-top:10px"><summary>자세히 — travel · probe · 포트 경로(by-id / by-path)</summary>
  <p class=muted style="margin-top:6px">
    travel = 감시 시작 이후 엔코더가 움직인 최대 폭(tick, 4096 = 1바퀴).
    probe 는 1 Mbps 기본 보드레이트만 봅니다 — 응답이 없으면 <b>전체 스캔</b>으로 보드레이트를 찾으세요.
    <b>1,2,3,4,5,6</b> 이 다 뜨면 모터 ID 세팅은 건너뛰어도 됩니다.<br>
    지정 경로는 <b>보드에 USB 시리얼 번호가 있으면</b>(sn 표시) <span class=mono>/dev/serial/by-id</span> —
    보드 자체를 따라가므로 <b>어느 USB 구멍에 꽂아도</b> 됩니다. 대신 보드를 다른 팔로 옮겨 달면 설정이 어긋나니
    보드에 sn 뒷자리를 적어 붙여 두세요.<br>
    <b>sn 이 없으면</b> 같은 모델끼리 by-id 가 겹치므로 <span class=mono>/dev/serial/by-path</span> 를 씁니다 —
    꽂은 USB 물리 포트에 고정되니 이 경우엔 <b>항상 같은 USB 구멍에</b> 꽂아야 합니다.
  </p></details>
</div>

<details class=adv><summary>2b · 모터 ID 세팅 — 새 팔을 조립했을 때만</summary>
<div class=card>
  <p class=muted style="margin-top:0">새 STS3215 는 전부 ID 1 입니다. probe 에서 <b>1,2,3,4,5,6</b> 이 다 뜨면 이 단계는 건너뛰세요.
  하나만 뜨거나 응답이 없으면 여기서 ID 를 씁니다 — <b>모터를 한 개씩만 보드에 꽂아</b> 순서대로 진행합니다
  (여러 개가 같은 ID 1 로 붙어 있으면 응답이 충돌합니다).</p>
  <div class=toolbar>
    <select id=msport></select> <select id=msrole title="OMX 는 팔로워(ID 11~16)와 리더(ID 1~6) 모터 구성이 다릅니다"><option value=follower>팔로워</option><option value=leader>리더</option></select>
    <button id=msstart class=primary onclick="withBusy(this,msStart)">모터 ID 세팅 시작</button>
  </div>
  <div id=msbox></div>
</div></details>

<details class=adv><summary>2c · 팔 불량 점검 — 팔이 이상할 때</summary>
<div class=card>
  <p class=muted style="margin-top:0">팔 하나(보드 하나)씩 점검합니다. 기본 점검은 서보에 <b>아무것도 쓰지 않습니다</b> —
  모터 응답·모델, 보호 플래그(과열·과부하·과전류·전압·각도센서), 전원 계통(5V/12V)과 전압, 온도,
  가만히 있을 때 엔코더 흔들림, 통신 누락을 봅니다.
  <b>손으로 쓸기</b>를 켜면 토크를 끄고(팔이 처짐) 관절을 손으로 끝까지 움직여 엔코더 튐과 걸림을 봅니다.
  모터를 구동하는 시험은 없습니다.</p>
  <div class=toolbar>
    <select id=acport onchange="acRoleFromPort()"></select>
    <select id=acrole>
      <option value="">역할 모름 (전원 계통 판정 생략)</option>
      <option value=follower>팔로워</option>
      <option value=leader>리더 — 12V 로 읽히면 즉시 경고</option>
    </select>
    <label class=tiny style="display:flex;gap:6px;align-items:center">
      <input type=checkbox id=acsweep> 손으로 쓸기 포함</label>
    <button id=acstart class=primary onclick="withBusy(this,acStart)">점검 시작</button>
  </div>
  <div id=acbox></div>
</div></details>

<p class=eyebrow id=cameras>3 · 카메라</p>
<div class=card>
  <div class=toolbar>
    <button onclick="withBusy(this,loadCams)">카메라 스캔</button>
    <span class=muted>연결된 카메라를 전부 열어 한 장씩 찍습니다 — <b>어느 장치가 어느 카메라인지 화면으로 확인</b>하세요.
      카메라 수에 따라 몇 초 걸리고, 제어·수집 중에는 막힙니다.
      카메라도 같은 규칙입니다 — 시리얼 번호가 있으면 by-id, 없으면(같은 모델 2개가 겹침) by-path 라 그때는 같은 USB 구멍에 꽂아야 합니다.</span>
  </div>
  <table id=camtbl></table>
  <div style="margin-top:16px">
    <p class=eyebrow>등록된 카메라</p>
    <table id=curcamtbl></table>
    <p class=muted style="margin-top:8px">양팔이면 팔 카메라 키에 <span class=mono>left_</span> /
    <span class=mono>right_</span> 접두사가 붙고, 공용 카메라(top)는 접두사 없이 그대로 갑니다 —
    lerobot <span class=mono>bi_so_follower</span> 규칙과 같습니다.</p>
  </div>
</div>

<p class=eyebrow>4 · 기타</p>
<div class=card>
  <div class=formgrid>
    <label class=f>로봇 id (robot_id) <input id=robot_id size=12></label>
    <label class=f>초당 프레임 (fps) <input id=fps size=5></label>
    <label class=f title="SO-ARM101 은 °, OMX 는 -100~100 단위 (1 ≈ 1.8°)">한 번에 움직일 최대 크기 (max_relative_target · SO: ° / OMX: 1≈1.8°, 비우면 미사용) <input id=mrt size=6></label>
    <label class=f style="flex:1;min-width:240px">기본 태스크 설명 <input id=task></label>
  </div>
  <div id=calib></div>
</div>

<div class=toolbar>
  <button class=primary onclick="save()">설정 저장 &amp; 적용</button>
  <span class=muted id=savemsg></span>
</div>
<details class=more><summary>현재 설정 파일 보기 (openmimic_config.json)</summary><pre id=cfgdump></pre></details>
</div>
<script>
let CFG=null, PORTS=[], VCAMS=[], WATCHING=false, timer=null, LASTWATCH=null;
const $=id=>document.getElementById(id);
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dirty=m=>{ $('savemsg').innerHTML='<span class=b-warn>'+E(m)+' — 저장 버튼을 눌러야 적용됩니다</span>'; };

async function jget(u){ return (await fetch(u)).json(); }
async function jpost(u,b){
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},
                        body:JSON.stringify(b||{})});
  return r.json();
}

/* 보드에 USB 시리얼이 없으면 by-id 가 같은 모델끼리 겹칩니다 → by-path 우선 */
function stableOf(p){
  const hasSn = p.usb && p.usb.serial;
  return (hasSn ? (p.by_id || p.by_path) : (p.by_path || p.by_id)) || p.dev;
}
function sameDev(a,b){
  if(!a||!b) return false;
  if(a===b) return true;
  const f=x=>{ const p=PORTS.find(q=>q.dev===x||q.by_id===x||q.by_path===x); return p?p.dev:x; };
  return f(a)===f(b);
}

async function boot(){
  const s=await jget('/api/setup/state');
  CFG=s.config; PORTS=s.ports;
  $('busywarn').innerHTML = s.busy ? '<p class="badge b-warn">'+E(s.busy)+' — 저장/감시가 막힙니다</p>' : '';
  $('robot_id').value=CFG.robot_id||''; $('fps').value=CFG.fps||30;
  $('mrt').value=(CFG.max_relative_target==null?'':CFG.max_relative_target);
  $('task').value=CFG.default_task||'';
  renderArms(); renderPorts(); renderCurCams(); renderCalib(s.calib); dump();
  fillMsPorts(); msRefresh(); fillAcPorts(); acLoad(); envLoad();
}
function dump(){ $('cfgdump').textContent=JSON.stringify(CFG,null,2); }

/* ---------- 팔 ---------- */
async function setRobot(r){
  // 기종 전환 — 보드가 달라 포트는 비웁니다. 캘리브레이션 파일은 기종별 폴더라 섞이지 않습니다
  if((CFG.robot||'so101')===r) return;
  const unsaved=$('savemsg').textContent.indexOf('저장 버튼')>=0;
  if(!confirm((r==='omx'?'OMX':'SO-ARM101')+' 로 바꿉니다. 지정된 포트는 비웁니다. '+(unsaved?'저장하지 않은 변경은 버려집니다. ':'')+'바로 적용할까요?')) return;
  let x;
  try{ x=await (await fetch('/api/wizard/robot',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({robot:r})})).json(); }
  catch(e){ alert('기종 변경 실패: '+e); return; }
  if(x.error){ alert(x.error); return; }
  location.reload();
}
async function setMode(m){
  // 셋업 마법사와 같은 서버 규칙으로 바로 적용합니다 (id 변경 + 같은 팔 캘리브레이션 파일 복사)
  if(CFG.mode===m) return;
  const unsaved=$('savemsg').textContent.indexOf('저장 버튼')>=0;
  const msg=(m==='single'?'양팔 → 한팔: 왼팔(left)만 남기고 오른팔 설정(포트·카메라)을 지웁니다. '
                         :'한팔 → 양팔: 지금 팔이 left 가 되고 right 칸이 새로 생깁니다. ')
            +(unsaved?'저장하지 않은 변경은 버려집니다. ':'')+'바로 적용할까요?';
  if(!confirm(msg)) return;
  let r;
  try{ r=await (await fetch('/api/wizard/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:m})})).json(); }
  catch(e){ alert('모드 변경 실패: '+e); return; }
  if(r.error){ alert(r.error); return; }
  if(r.copied&&r.copied.length) alert('같은 팔의 캘리브레이션 파일을 새 이름으로 복사했습니다: '+r.copied.join(', '));
  location.reload();
}

function renderArms(){
  const bi=CFG.mode==='bimanual';
  $('b1').className=bi?'':'primary';
  $('b2').className=bi?'primary':'';
  const omx=(CFG.robot||'so101')==='omx';
  $('r_so101').className=omx?'':'primary'; $('r_omx').className=omx?'primary':'';
  const T=omx?{f:'omx_follower',l:'omx_leader',bf:'bi_omx_follower',bl:'bi_omx_leader'}
             :{f:'so101_follower',l:'so101_leader',bf:'bi_so_follower',bl:'bi_so_leader'};
  $('modehint').innerHTML = bi
    ? '양팔 = lerobot <span class=mono>'+T.bf+'</span> / <span class=mono>'+T.bl+'</span>. '
      +'calib id 는 같은 이름에 _left / _right 를 붙여야 합니다 (lerobot 이 {id}_left 로 파일을 찾습니다).'
    : '한팔 = lerobot <span class=mono>'+T.f+'</span> / <span class=mono>'+T.l+'</span>.';
  const box=$('arms'); box.innerHTML='';
  CFG.arms.forEach(a=>{
    const d=document.createElement('div'); d.className='armcard';
    d.innerHTML='<h3>'+E(a.side)+'</h3>';
    ['follower','leader'].forEach(role=>{
      const row=document.createElement('div'); row.className='slot';
      row.innerHTML='<span class=role>'+role+'</span>';
      const ip=document.createElement('input'); ip.className='port';
      ip.placeholder='비어 있음 — 아래 2번 표의 역할 드롭다운으로 지정하면 자동으로 채워집니다 (직접 입력도 가능)';
      ip.value=a[role+'_port']||'';
      ip.style.borderColor = ip.value ? 'var(--ok)' : 'var(--line)';
      ip.onchange=()=>{ a[role+'_port']=ip.value.trim(); paintRoles(); renderArms(); dump(); dirty('포트 변경'); };
      const lb=document.createElement('span'); lb.className='tiny';
      lb.textContent = CFG.mode==='bimanual' ? 'calib id (X_left / X_right)' : 'calib id';
      const id=document.createElement('input'); id.className='cid'; id.value=a[role+'_id']||'';
      id.onchange=()=>{ a[role+'_id']=id.value.trim(); dump(); dirty('캘리브 id 변경'); };
      row.appendChild(ip); row.appendChild(lb); row.appendChild(id);
      d.appendChild(row);
    });
    /* 3D 뷰 전용 배치 — 제어·수집 데이터와 무관합니다 */
    a.view = a.view || {x:0, y:(a.side==='left'?0.12:a.side==='right'?-0.12:0), yaw_deg:0};
    const vr=document.createElement('div'); vr.className='slot';
    vr.innerHTML='<span class=role>3D 배치</span>';
    [['x','앞뒤(m, + 앞)'],['y','좌우(m, + 왼쪽)'],['yaw_deg','회전(°, + 좌회전)']].forEach(f=>{
      const k=f[0];
      const lb=document.createElement('span'); lb.className='tiny'; lb.textContent=f[1];
      const inp=document.createElement('input'); inp.size=6; inp.value=a.view[k];
      inp.style.width='80px'; inp.style.fontFamily='var(--mono)';
      inp.onchange=()=>{ a.view[k]=parseFloat(inp.value)||0; dump(); dirty('3D 배치 변경'); };
      vr.appendChild(lb); vr.appendChild(inp);
    });
    const note=document.createElement('span'); note.className='tiny';
    note.textContent='3D 화면 표시용 — 제어·데이터에는 영향 없음';
    vr.appendChild(note);
    d.appendChild(vr);
    box.appendChild(d);
  });
}

/* ---------- 포트 ---------- */
function slotOf(dev){
  for(const a of CFG.arms)
    for(const role of ['follower','leader'])
      if(sameDev(a[role+'_port'],dev)) return a.side+'|'+role;
  return '';
}
function assign(dev,val){
  const stable=stableOf(PORTS.find(p=>p.dev===dev)||{dev});
  CFG.arms.forEach(a=>['follower','leader'].forEach(r=>{
    if(sameDev(a[r+'_port'],dev)) a[r+'_port']='';
  }));
  if(val){
    const [side,role]=val.split('|');
    const a=CFG.arms.find(x=>x.side===side);
    if(a) a[role+'_port']=stable;
  }
  renderArms(); paintRoles(); dump(); dirty('포트 지정');
}

/* 표는 포트 목록·모드가 바뀔 때만 다시 만들고, 감시 중에는 값만 덮어씁니다.
   매번 다시 그리면 열어 둔 <select> 가 닫혀서 역할을 고를 수 없습니다. */
const ROWS={};          // dev -> {idCell, travelCell, sel}
let PROBE={};           // dev -> probe 결과 HTML (다시 그려도 유지)

function renderPorts(){
  const t=$('porttbl');
  t.innerHTML='<tr><th>장치</th><th>USB</th><th class=num>모터 ID</th>'
             +'<th class=num>움직임(travel)</th><th>역할</th><th></th></tr>';
  Object.keys(ROWS).forEach(k=>delete ROWS[k]);
  if(!PORTS.length){
    t.innerHTML+='<tr><td colspan=6 class=muted>시리얼 장치가 없습니다 — '
                +'USB 를 꽂고 다시 스캔하세요 (권한 문제면 dialout 그룹 확인)</td></tr>';
    return;
  }
  PORTS.forEach(p=>{
    const stable=stableOf(p);
    const sn=p.usb&&p.usb.serial;
    const usb=(p.usb&&p.usb.vid)
      ? E(p.label)+'<br><span class="muted mono" style="font-size:11px">'+E(p.usb.vid)+':'+E(p.usb.pid)
        +(sn?' sn='+E(p.usb.serial):' <b class=b-warn>sn 없음</b>')+'</span>'
      : '<span class=muted>-</span>';

    const tr=document.createElement('tr');
    const c1=document.createElement('td'); c1.className='mono';
    c1.innerHTML=E(p.dev)+(stable!==p.dev?'<br><span class="tiny">'+E(stable)+'</span>':'');
    const c2=document.createElement('td'); c2.innerHTML=usb;
    const c3=document.createElement('td'); c3.className='num';
    c3.innerHTML=PROBE[p.dev]||'-';
    const c4=document.createElement('td'); c4.className='num'; c4.innerHTML='-';

    const c5=document.createElement('td');
    const sel=document.createElement('select');
    const opts=[['','미지정']];
    CFG.arms.forEach(a=>{
      opts.push([a.side+'|follower', (CFG.arms.length>1?a.side+' ':'')+'follower']);
      opts.push([a.side+'|leader',   (CFG.arms.length>1?a.side+' ':'')+'leader']);
    });
    opts.forEach(([v,l])=>{
      const o=document.createElement('option'); o.value=v; o.textContent=l; sel.appendChild(o);
    });
    sel.value=slotOf(p.dev);
    sel.onchange=()=>assign(p.dev,sel.value);
    c5.appendChild(sel);

    const c6=document.createElement('td'); c6.style.textAlign='right';
    c6.appendChild(btn('probe',()=>doProbe(p.dev,false)));
    c6.appendChild(btn('전체 스캔',()=>doProbe(p.dev,true)));

    [c1,c2,c3,c4,c5,c6].forEach(c=>tr.appendChild(c));
    t.appendChild(tr);
    ROWS[p.dev]={idCell:c3, travelCell:c4, sel:sel};
  });
  paintWatch();
}

/* 감시 상태만 갱신 — DOM 구조는 건드리지 않음 */
function paintWatch(){
  const st=LASTWATCH;
  PORTS.forEach(p=>{
    const r=ROWS[p.dev]; if(!r) return;
    const w=st?st[p.dev]:null;
    const spans=(w&&w.span)?Object.values(w.span):[];
    const travel=spans.length?Math.max.apply(null,spans):null;
    r.travelCell.innerHTML=(travel==null)?'-':'<b>'+travel+'</b>';
    r.travelCell.style.color=(travel!=null&&travel>60)?'var(--ok)':'';
    if(w&&w.err) r.idCell.innerHTML='<span class=b-bad title="'+E(w.err)+'">err</span>';
    else r.idCell.innerHTML=PROBE[p.dev]||'-';
  });
}

/* 역할 select 값만 갱신 — 다시 그리지 않음 */
function paintRoles(){
  PORTS.forEach(p=>{ const r=ROWS[p.dev]; if(r) r.sel.value=slotOf(p.dev); });
}

/* 클릭 즉시 잠그고 라벨을 바꿉니다 — 스캔·probe 는 몇 초 걸려서
   피드백이 없으면 "눌러도 아무 일 없다" 로 보입니다. */
async function withBusy(el, fn){
  if(el){ el.disabled=true; el.dataset.t=el.textContent; el.textContent='처리 중…'; }
  try{ return await fn(); }
  catch(e){ alert('실패: '+(e&&e.message||e)); }
  finally{
    // 콜백이 라벨을 바꿨으면(예: 감시 시작 → 감시 중지) 그대로 둡니다.
    if(el && el.isConnected){
      el.disabled=false;
      if(el.dataset.t && el.textContent==='처리 중…') el.textContent=el.dataset.t;
    }
  }
}
function btn(label,fn,cls){
  const b=document.createElement('button'); b.textContent=label;
  if(cls)b.className=cls; b.style.marginLeft='6px';
  b.onclick=()=>withBusy(b, ()=>Promise.resolve(fn()));
  return b;
}
function noimg(text){
  const d=document.createElement('div'); d.className='camthumb noimg'; d.textContent=text; return d;
}
/* 인라인 onerror 는 따옴표 escape 가 꼬이기 쉬워 DOM 으로 만듭니다 */
function thumb(dev, ts, failText){
  const img=document.createElement('img');
  img.className='camthumb';
  img.src='/api/setup/camsnap?dev='+encodeURIComponent(dev)+'&t='+ts;
  img.onerror=function(){ img.replaceWith(noimg(failText)); };
  return img;
}

async function loadPorts(){ PORTS=(await jget('/api/setup/ports')).ports; PROBE={}; renderPorts(); fillMsPorts(); fillAcPorts(); }

async function doProbe(dev,full){
  const r=ROWS[dev]; if(!r) return;
  r.idCell.textContent='...';
  const d=await jpost('/api/setup/probe',{port:dev,full:full});
  let html;
  if(d.error){
    html='<span class=b-bad title="'+E(d.error)+'">실패</span>';
  }else if(full){
    const parts=Object.keys(d.baudrates).map(b=>b+': ['+d.baudrates[b].join(',')+']');
    html=parts.length?parts.map(E).join('<br>'):'<span class=muted>없음</span>';
  }else{
    html=d.ids.length
      ? '<span class="badge '+(d.ids.length===6?'b-ok':'b-warn')+'">'+d.ids.join(',')+'</span>'
      : '<span class=muted>응답 없음</span>';
  }
  PROBE[dev]=html; r.idCell.innerHTML=html;
}

async function toggleWatch(){
  if(WATCHING){ await stopWatch(); return; }
  const d=await jpost('/api/setup/watch',{ports:PORTS.map(p=>p.dev)});
  if(d.error){ alert(d.error); return; }
  WATCHING=true; clearWatch();
  $('bwatch').textContent='감시 중지'; $('bwatch').className='danger';
  timer=setInterval(async()=>{
    const s=await jget('/api/setup/watch');
    if(s.on){ LASTWATCH=s.state; paintWatch(); }
  },400);
}
async function stopWatch(){
  clearInterval(timer); timer=null; WATCHING=false;
  $('bwatch').textContent='포트 감시 시작 (팔 판별)'; $('bwatch').className='primary';
  await jpost('/api/setup/watch/stop');
}
/* 감시 재시작 시 이전 travel 이 남지 않게 */
function clearWatch(){ LASTWATCH=null; paintWatch(); }
addEventListener('pagehide',()=>{ if(WATCHING) navigator.sendBeacon('/api/setup/watch/stop'); });

/* ---------- 모터 ID 세팅 ---------- */
let MS=null, mstimer=null;
function fillMsPorts(){
  const sel=$('msport'); const cur=sel.value; sel.innerHTML='';
  PORTS.forEach(p=>{ const o=document.createElement('option'); o.value=p.dev; o.textContent=p.dev+(p.usb&&p.usb.product?'  ('+p.usb.product+')':''); sel.appendChild(o); });
  if(cur) sel.value=cur;
}
async function msStart(){
  const port=$('msport').value; if(!port){ alert('포트를 고르세요'); return; }
  if(!confirm('모터를 한 개씩만 보드에 연결한 상태여야 합니다. 시작할까요?')) return;
  const r=await jpost('/api/setup/motors/start',{port:port, role:$('msrole').value});
  if(r.error){ alert(r.error); }
  msRefresh(); if(!mstimer) mstimer=setInterval(msRefresh,1000);
}
async function msWrite(){
  const b=document.querySelector('#msbox button.primary'); if(b) b.disabled=true;
  const r=await jpost('/api/setup/motors/write'); msRefresh();
}
async function msCancel(){ await jpost('/api/setup/motors/cancel'); msRefresh(); }
async function msRefresh(){
  MS=await jget('/api/setup/motors');
  const box=$('msbox');
  if(MS.stage==='idle'){ box.innerHTML=''; if(mstimer){clearInterval(mstimer); mstimer=null;} return; }
  const doneList=MS.done.map(d=>'<span class="badge b-ok">'+E(d.name)+' = ID '+d.id+'</span>').join(' ');
  let h='<div style="margin-top:6px">'+(doneList||'<span class=muted>아직 기록된 모터 없음</span>')+'</div>';
  if(MS.stage==='running'){
    const n=MS.idx+1, total=MS.order.length, targetId=total-MS.idx;
    h+='<div class=stagebox style="margin-top:12px"><h3>'+n+' / '+total+' · <span class=mono>'+E(MS.current)+'</span> → ID '+targetId+'</h3>'
      +'<p class=inst>보드에 <b>'+E(MS.current)+'</b> 모터 <b>하나만</b> 연결하고 전원이 들어온 상태에서 아래 버튼을 누르세요. '
      +'다른 모터는 케이블을 빼 두세요. 이미 ID 를 쓴 모터도 아직 붙이지 마세요.</p>'
      +(MS.err?'<p class="badge b-bad">'+E(MS.err)+'</p>':'')
      +(MS.last?'<p class="mono muted" style="font-size:12px">'+E(MS.last)+'</p>':'')
      +'<div class=toolbar><button class="primary big" onclick="msWrite()">ID '+targetId+' 쓰기</button>'
      +'<button class=danger onclick="msCancel()">중단</button></div></div>';
  }else if(MS.stage==='done'){
    h+='<p class="badge b-ok" style="margin-top:10px">6개 모터 ID 세팅 완료 — 이제 모터를 전부 데이지체인으로 연결하고 probe 로 1~6 확인 → 캘리브레이션 탭</p>'
      +'<div class=toolbar><button onclick="msCancel()">닫기</button></div>';
    if(mstimer){clearInterval(mstimer); mstimer=null;}
  }else if(MS.stage==='error'){
    h+='<p class="badge b-bad" style="margin-top:10px">'+E(MS.err)+'</p><div class=toolbar><button onclick="msCancel()">닫기</button></div>';
    if(mstimer){clearInterval(mstimer); mstimer=null;}
  }
  box.innerHTML=h;
}
addEventListener('pagehide',()=>{ if(MS&&MS.stage==='running') navigator.sendBeacon('/api/setup/motors/cancel'); });

/* ---------- 환경 ---------- */
function portTail(p){ if(!p) return '-'; const t=p.split('/').pop(); return t.length>24?'…'+t.slice(-22):t; }
async function envLoad(){ envPaint(await jget('/api/envs')); }
function envPaint(d){
  const t=$('envtbl'); if(!t||!d||!d.envs) return;
  let h='<tr><th>환경</th><th>기종 · 모드</th><th>팔 — 팔로워 / 리더 포트</th><th>카메라</th><th>수정</th><th></th></tr>';
  d.envs.forEach((e,i)=>{
    const arms=e.arms.map(a=>'<span class=mono>'+E(a.side)+'</span> '+E(portTail(a.follower_port))+' / '+E(portTail(a.leader_port))).join('<br>');
    const cams=[].concat.apply([], e.arms.map(a=>a.cameras.map(c=>(e.mode==='bimanual'?a.side+'_':'')+c))).concat(e.cameras);
    h+='<tr><td class=mono>'+E(e.name)+(e.active?' <span class="badge b-ok">사용 중</span>':'')+'</td>'
      +'<td style="white-space:nowrap">'+(e.robot==='omx'?'OMX':'SO-ARM101')+' · '+(e.mode==='bimanual'?'양팔':'한팔')+'</td><td style="font-size:12px">'+arms+'</td>'
      +'<td class=mono style="font-size:12px">'+E(cams.join(', ')||'-')+'</td><td class=tiny>'+E(e.updated)+'</td>'
      +'<td style="text-align:right;white-space:nowrap" id="envact'+i+'"></td></tr>';
  });
  t.innerHTML=h;
  d.envs.forEach((e,i)=>{
    const td=$('envact'+i);
    if(!e.active) td.appendChild(btn('전환',()=>envActivate(e.name),'primary'));
    td.appendChild(btn('이름 변경',()=>envRename(e.name)));
    if(!e.active) td.appendChild(btn('삭제',()=>envDelete(e.name),'danger'));
  });
}
const ENV_RE=/^[A-Za-z0-9._-]+$/;
async function envActivate(name){
  if(!confirm('환경을 "'+name+'" 로 전환합니다. 저장하지 않은 Setup 변경은 사라집니다.')) return;
  const r=await jpost('/api/envs/activate',{name:name});
  if(r.error){ alert(r.error); return; }
  location.reload();
}
async function envSaveAs(){
  const name=(prompt('새 환경 이름 (영문/숫자/._-)\\n저장된 지금 구성이 복사되고, 새 환경이 사용 중이 됩니다.')||'').trim();
  if(!name) return;
  if(!ENV_RE.test(name)){ alert('영문/숫자/._- 만 쓸 수 있습니다'); return; }
  const r=await jpost('/api/envs/save_as',{name:name});
  if(r.error){ alert(r.error); return; }
  location.reload();
}
async function envRename(name){
  const nw=(prompt('"'+name+'" 의 새 이름',name)||'').trim();
  if(!nw||nw===name) return;
  if(!ENV_RE.test(nw)){ alert('영문/숫자/._- 만 쓸 수 있습니다'); return; }
  const r=await jpost('/api/envs/rename',{name:name,new:nw});
  if(r.error){ alert(r.error); return; }
  location.reload();
}
async function envDelete(name){
  if(!confirm('환경 "'+name+'" 를 지웁니다. 데이터셋과 캘리브레이션 파일은 그대로입니다.')) return;
  const r=await jpost('/api/envs/delete',{name:name});
  if(r.error){ alert(r.error); return; }
  envPaint(r);
}

/* ---------- 팔 불량 점검 ---------- */
let AC=null, acTimer=null;
const AC_LV={OK:'',INFO:'',WARN:'b-warn',FAIL:'b-bad'};
const AC_MARK={OK:'정상',INFO:'정상',WARN:'주의',FAIL:'불량'};
function fillAcPorts(){
  const sel=$('acport'); if(!sel) return;
  const cur=sel.value; sel.innerHTML='';
  PORTS.forEach(p=>{
    const o=document.createElement('option'); o.value=p.dev;
    const slot=slotOf(p.dev);
    o.textContent=(slot?slot.replace('|',' / ')+' — ':'')+p.dev+(p.usb&&p.usb.product?'  ('+p.usb.product+')':'');
    sel.appendChild(o);
  });
  if(cur) sel.value=cur;
  acRoleFromPort();
}
function acRoleFromPort(){
  const slot=slotOf($('acport').value||'');
  if(slot) $('acrole').value=slot.split('|')[1];
}
async function acStart(){
  const port=$('acport').value;
  if(!port){ alert('포트가 없습니다 — 위 2번에서 다시 스캔하세요'); return; }
  const sweep=$('acsweep').checked;
  if(sweep && !confirm('쓸기를 하면 토크가 꺼져 팔이 처집니다. 팔을 받치거나 내려놓았나요?')) return;
  $('acbox').innerHTML='<p class=muted>점검 중… 몇 초 걸립니다</p>';
  const r=await jpost('/api/setup/armcheck/start',{port:port, role:$('acrole').value, sweep:sweep});
  acPaint(r.state||{stage:'error', err:r.error});
}
async function acFinish(el){
  await withBusy(el, async()=>{ const r=await jpost('/api/setup/armcheck/finish'); acPaint(r.state||{stage:'error',err:r.error}); });
}
async function acCancel(){
  const r=await jpost('/api/setup/armcheck/cancel'); acPaint(r.state);
}
async function acLoad(){ acPaint(await jget('/api/setup/armcheck')); }
function acTable(rep, sweeping){
  const J=Object.keys(rep.motors);
  let h='<table><tr><th>ID</th><th>관절</th><th>모델</th><th>FW</th><th class=num>전압</th><th class=num>온도</th>'
       +'<th class=num>흔들림</th><th class=num>통신</th>'+(sweeping?'':'<th class=num>쓸기</th><th>보호</th>')
       +'<th>결과</th></tr>';
  J.forEach(j=>{
    const m=rep.motors[j];
    const f=v=>v==null?'-':E(v);
    h+='<tr><td class=mono>'+m.id+'</td><td class=mono>'+E(j)+'</td><td class=mono>'+f(m.model)+'</td><td class=mono>'+f(m.fw)+'</td>'
      +'<td class=num>'+(m.voltage_v==null?'-':m.voltage_v.toFixed(1)+' V')+'</td>'
      +'<td class=num>'+(m.temp_c==null?'-':m.temp_c+'°')+'</td>'
      +'<td class=num>'+f(m.noise_ticks)+'</td><td class=num>'+f(m.comm)+'</td>'
      +(sweeping?'':'<td class=num>'+(m.sweep_deg==null?'-':m.sweep_deg+'°')+'</td><td>'+E((m.protect||[]).join(', ')||'-')+'</td>')
      +'<td><span class="badge '+(AC_LV[m.level]||'')+'">'+(sweeping&&m.level!=='FAIL'&&m.level!=='WARN'?'…':AC_MARK[m.level])+'</span></td></tr>';
  });
  return h+'</table>';
}
function acFindings(rep){
  if(!rep.findings.length) return '';
  return '<div style="margin-top:10px">'+rep.findings.filter(x=>x.level!=='OK').map(x=>
    '<p style="margin:4px 0"><span class="badge '+(AC_LV[x.level]||'')+'">'+E(x.level)+'</span> '
    +'<span class=mono>'+E(x.joint||'팔 전체')+'</span> · '+E(x.check)+' — '+E(x.message)+'</p>').join('')+'</div>';
}
function acPaint(st){
  AC=st; const box=$('acbox'); if(!box) return;
  clearTimeout(acTimer); acTimer=null;
  if(!st || st.stage==='idle'){ box.innerHTML=''; return; }
  if(st.stage==='error'){
    box.innerHTML='<p class="badge b-bad" style="margin-top:10px"></p>';
    box.firstChild.textContent='점검 실패 — '+(st.err||'알 수 없는 오류'); return;
  }
  if(st.stage==='checking'){
    box.innerHTML='<p class=muted>점검 중… ('+E(st.port)+')</p>';
    acTimer=setTimeout(acLoad,500); return;
  }
  const rep=st.report;
  const pw=rep&&rep.power&&rep.power.system
    ? '<p class=mono style="margin:10px 0 6px">전원: '+E(rep.power.system)+' 계통 · 중앙값 '+rep.power.median_v+' V (정상 '
      +rep.power.range_v[0]+'~'+rep.power.range_v[1]+' V)</p>' : '';
  if(st.stage==='sweeping'){
    if(!document.getElementById('acsw')){
      const J=Object.keys(st.live);
      box.innerHTML='<div class=stagebox style="margin-top:12px" id=acsw>'
        +'<h3>손으로 쓸기 — '+E(st.port)+'</h3>'
        +'<p class=inst>토크가 꺼졌습니다. 관절을 하나씩 <b>천천히</b> 양 끝까지 움직이세요 (순서 무관). '
        +'막대가 초록이 되면 충분합니다. 끝나면 <b>완료</b>.</p>'
        +'<table><tr><th>관절</th><th>움직인 범위</th><th class=num>°</th><th class=num>튐</th><th class=num>읽기 실패</th></tr>'
        +J.map(j=>'<tr><td class=mono>'+E(j)+'</td><td style="min-width:180px"><div class=pbar style="height:8px;background:var(--surface2);border-radius:4px;overflow:hidden">'
          +'<i id="acb_'+j+'" style="display:block;height:100%;width:0;background:var(--warn)"></i></div></td>'
          +'<td class=num id="acd_'+j+'">0</td><td class=num id="acj_'+j+'">0</td><td class=num id="acf_'+j+'">0</td></tr>').join('')
        +'</table><div class=toolbar style="margin-top:12px">'
        +'<button class=primary onclick="acFinish(this)">완료 — 판정 보기</button>'
        +'<button onclick="acCancel()">취소</button></div>'
        +'<div id=acpart></div></div>';
    }
    Object.keys(st.live).forEach(j=>{
      const l=st.live[j], pct=Math.min(100, l.deg/l.need*100);
      const b=document.getElementById('acb_'+j);
      if(b){ b.style.width=pct+'%'; b.style.background=l.jumps?'var(--bad)':(pct>=100?'var(--ok)':'var(--warn)'); }
      const set=(id,v)=>{ const e=document.getElementById(id); if(e) e.textContent=v; };
      set('acd_'+j, l.deg.toFixed(1)); set('acj_'+j, l.jumps); set('acf_'+j, l.fails);
    });
    const part=document.getElementById('acpart');
    if(part && rep) part.innerHTML='<p class=eyebrow style="margin-top:14px">기본 점검 결과 (쓸기 전)</p>'+pw+acTable(rep,true)+acFindings(rep);
    acTimer=setTimeout(acLoad,300);
    return;
  }
  // done
  const cls={'정상':'b-ok','주의':'b-warn','불량 의심':'b-bad'}[rep.verdict]||'';
  box.innerHTML='<div style="margin-top:12px"><span class="badge '+cls+'" style="font-size:15px;padding:6px 14px">판정: '
    +E(rep.verdict)+'</span> <span class="mono muted">'+E(st.port)+(st.role?' · '+E(st.role):'')+'</span></div>'
    +pw+acTable(rep,false)+acFindings(rep);
}
addEventListener('pagehide',()=>{ if(AC&&AC.stage==='sweeping') navigator.sendBeacon('/api/setup/armcheck/cancel'); });

/* ---------- 카메라 ---------- */
async function loadCams(){
  const d=await jget('/api/setup/cameras');
  if(d.error){ alert(d.error); return; }
  VCAMS=d.cameras; renderVCams();
}
function renderVCams(){
  const t=$('camtbl');
  t.innerHTML='<tr><th>화면</th><th>장치</th><th>기본 해상도</th><th>대상</th><th>이름</th><th></th></tr>';
  if(!VCAMS.length){
    t.innerHTML+='<tr><td colspan=6 class=muted>스캔된 카메라 없음</td></tr>'; return;
  }
  const ts=Date.now();
  VCAMS.forEach(c=>{
    const stable=stableOf(c);      /* 시리얼 번호 있으면 by-id, 없으면 by-path (포트와 같은 규칙) */
    const sn=c.usb&&c.usb.serial;
    const tr=document.createElement('tr');
    const c0=document.createElement('td');
    c0.appendChild(c.snap ? thumb(c.dev, ts, '영상 없음') : noimg('영상 없음'));
    const c1=document.createElement('td'); c1.className='mono';
    c1.innerHTML=E(c.dev)+(stable!==c.dev?'<br><span class=tiny>'+E(stable)+'</span>':'')
      +(c.usb&&c.usb.vid?'<br><span class=tiny>'+E(c.usb.vid)+':'+E(c.usb.pid)+(sn?' sn='+E(sn):' <b class=b-warn>sn 없음 → by-path</b>')+'</span>':'');
    const c2=document.createElement('td'); c2.className='mono';
    c2.textContent=(c.width||'?')+'x'+(c.height||'?')+' @'+Math.round(c.fps||0);
    const c3=document.createElement('td');
    const sel=document.createElement('select');
    CFG.arms.forEach(a=>{
      const o=document.createElement('option');
      o.value='arm:'+a.side; o.textContent=CFG.arms.length>1?(a.side+' 팔'):'팔';
      sel.appendChild(o);
    });
    const o=document.createElement('option'); o.value='shared'; o.textContent='공용 (top 등)';
    sel.appendChild(o);
    c3.appendChild(sel);
    const c4=document.createElement('td');
    const nm=document.createElement('input'); nm.size=8; nm.value='wrist';
    sel.onchange=()=>{ nm.value = sel.value==='shared' ? 'top' : 'wrist'; };
    c4.appendChild(nm);
    const c5=document.createElement('td'); c5.style.textAlign='right';
    c5.appendChild(btn('추가',()=>addCam(stable,sel.value,nm.value.trim()),'primary'));
    [c0,c1,c2,c3,c4,c5].forEach(x=>tr.appendChild(x));
    t.appendChild(tr);
  });
}
function addCam(dev,target,name){
  if(!/^[A-Za-z0-9._-]+$/.test(name)){ alert('이름은 영문/숫자/._- 만'); return; }
  const fps=parseInt($('fps').value)||30;
  const spec={index_or_path:dev,width:640,height:480,fps:fps};
  if(target==='shared'){ CFG.cameras=CFG.cameras||{}; CFG.cameras[name]=spec; }
  else{
    const a=CFG.arms.find(x=>x.side===target.slice(4));
    a.cameras=a.cameras||{}; a.cameras[name]=spec;
  }
  renderCurCams(); dump(); dirty('카메라 추가');
}
function renderCurCams(){
  const t=$('curcamtbl');
  t.innerHTML='<tr><th>화면</th><th>키</th><th>장치</th><th class=num>해상도</th><th class=num>fps</th>'
    +'<th title="USB 카메라 여러 대가 한 허브에서 끊기면 MJPG">형식</th><th></th></tr>';
  const ts=Date.now();
  let n=0;
  const groups=[];
  CFG.arms.forEach(a=>groups.push([a.cameras||{}, CFG.arms.length>1?a.side+'_':'']));
  groups.push([CFG.cameras||{}, '']);
  groups.forEach(g=>{
    const obj=g[0], prefix=g[1];
    Object.keys(obj).forEach(name=>{
      n++;
      const s=obj[name];
      const tr=document.createElement('tr');
      const c0=document.createElement('td');
      c0.appendChild(thumb(s.index_or_path, ts, '스캔 필요'));
      const c1=document.createElement('td'); c1.className='mono'; c1.textContent=prefix+name;
      const c2=document.createElement('td'); c2.className='mono';
      c2.style.fontSize='11px'; c2.textContent=s.index_or_path;
      const c3=document.createElement('td'); c3.className='num';
      c3.innerHTML='<input size=4> x <input size=4>';
      const c4=document.createElement('td'); c4.className='num'; c4.innerHTML='<input size=3>';
      const ins=[].concat([].slice.call(c3.querySelectorAll('input')),
                          [].slice.call(c4.querySelectorAll('input')));
      ins[0].value=s.width; ins[1].value=s.height; ins[2].value=s.fps;
      ins.forEach(i=>i.onchange=()=>{
        s.width=parseInt(ins[0].value)||640; s.height=parseInt(ins[1].value)||480;
        s.fps=parseInt(ins[2].value)||30; dump(); dirty('카메라 변경');
      });
      const cf=document.createElement('td');
      const fs=document.createElement('select');
      [['','자동'],['MJPG','MJPG'],['YUYV','YUYV']].forEach(o=>{ const op=document.createElement('option'); op.value=o[0]; op.textContent=o[1]; fs.appendChild(op); });
      fs.value=s.fourcc||'';
      fs.onchange=()=>{ if(fs.value) s.fourcc=fs.value; else delete s.fourcc; dump(); dirty('카메라 형식 변경'); };
      cf.appendChild(fs);
      const c5=document.createElement('td'); c5.style.textAlign='right';
      c5.appendChild(btn('삭제',()=>{ delete obj[name]; renderCurCams(); dump(); dirty('카메라 삭제'); },'danger'));
      [c0,c1,c2,c3,c4,cf,c5].forEach(x=>tr.appendChild(x));
      t.appendChild(tr);
    });
  });
  if(!n) t.innerHTML+='<tr><td colspan=7 class=muted>등록된 카메라 없음 — 위에서 스캔 후 추가하세요</td></tr>';
}

/* ---------- 캘리브레이션 상태 ---------- */
function renderCalib(cal){
  let h='<p class=eyebrow>캘리브레이션 파일</p>';
  Object.keys(cal).forEach(side=>{
    Object.keys(cal[side]).forEach(role=>{
      const c=cal[side][role];
      h+='<div class=mono style="font-size:12px;margin-bottom:5px">'
        +'<span class="badge '+(c.ok?'b-ok':'b-bad')+'">'+(c.ok?'있음':'없음')+'</span> '
        +E(side)+' / '+E(role)+' · '+E(c.id)+'.json'
        +'<br><span class=tiny>'+E(c.path)+'</span></div>';
    });
  });
  h+='<p class=muted>없으면 제어 탭이 연결되지 않습니다 — <a href="/calib">캘리브레이션 탭</a>에서 만드세요.</p>';
  $('calib').innerHTML=h;
}

/* ---------- 저장 ---------- */
async function save(){
  CFG.robot_id=$('robot_id').value.trim();
  CFG.fps=parseInt($('fps').value)||30;
  const m=$('mrt').value.trim();
  CFG.max_relative_target = m===''? null : parseFloat(m);
  CFG.default_task=$('task').value;
  if(WATCHING) await stopWatch();
  const d=await jpost('/api/setup/config',{config:CFG});
  if(d.error){ $('savemsg').innerHTML='<span class=b-bad>'+E(d.error)+'</span>'; return; }
  $('savemsg').innerHTML='<span class=b-ok>저장·적용됨</span>';
  let note='';
  if(d.copied&&d.copied.length) note+='캘리브 id 를 바꾼 팔의 캘리브레이션 파일을 새 이름으로 복사했습니다: '+d.copied.join(', ')+'\\n';
  if(d.kept&&d.kept.length) note+='새 이름의 캘리브레이션 파일이 이미 있어 그대로 씁니다 (덮어쓰지 않음): '+d.kept.join(', ')
    +'\\n다른 팔의 파일이면 캘리브레이션 탭에서 다시 캘리브레이션하세요.';
  if(note) alert(note);
  setTimeout(function(){ location.reload(); },700);
}
boot();
</script>"""



# ----------------------------- 페이지: Calibration ----------------------------
def _signed_mod(v):
    """Homing_Offset 은 ±2048 범위라 한 바퀴 단위로 접어 넣습니다."""
    return ((int(v) + RES_HALF) % RES) - RES_HALF


FULL_TURN_MOTOR = "wrist_roll"       # lerobot 과 동일: 0~4095 고정
RES = 4096                           # sts3215 엔코더 해상도
RES_HALF = RES // 2
SPAN_OK_DEG = 30.0                   # 이보다 좁으면 "덜 움직임" 경고


class CalibSession:
    """lerobot SOFollower/SOLeader.calibrate() 를 웹용 상태 머신으로 풀어 쓴 것.

    원본은 input() 두 번(중앙 자세 → 범위 기록)과 터미널 Enter 대기로 블로킹됩니다.
    여기서는 **중앙 자세 단계를 없앴습니다.** 중앙을 찾으려면 어차피 한 번 쓸어봐야 하고,
    사용자가 고른 '중앙' 이 실제로는 가동범위의 한쪽 끝이면 반대편에서 엔코더가
    0/4095 를 넘어가 기록이 망가집니다 (lerobot CLI 도 같은 함정이 있습니다).

    대신 이렇게 합니다:
      connect(calibrate=False) → disable_torque → Operating_Mode=POSITION
      → bus.reset_calibration()   (Homing_Offset=0 → Present == 원시 엔코더값)
      → 관절을 양 끝까지 쓸기. 연속 표본의 차이로 언랩해 진짜 min/max 누적
      → [버튼] 기록된 범위의 **중심**에서 homing_offset 을 역산해 저장

    결과 파일의 의미는 lerobot 과 동일합니다: Present = Actual - Homing_Offset 이므로
    중심이 2047(반 바퀴)로 오고 range 는 2047 ± span/2 라 항상 0~4095 안에 들어옵니다.
    경로·포맷도 lerobot 객체의 calibration_fpath / _save_calibration 을 그대로 씁니다.
    """

    def __init__(self):
        self.lock = threading.Lock()      # 시리얼 포트는 스레드 안전하지 않음 — 모든 버스 접근을 직렬화
        self._reset()

    def _reset(self):
        self.device = None
        self.side = self.role = None
        self.stage = "idle"     # idle | ranging | done | error
        self.pos, self.lo, self.hi = {}, {}, {}
        # 엔코더는 0~4095 단일 회전이라 경계를 넘으면 값이 튑니다.
        # 연속 표본의 차이로 언랩해서 '진짜 이동량' 을 누적합니다.
        self.prev_raw, self.unw = {}, {}
        self.err = ""
        self.old_calib = None   # 취소 시 모터에 되돌려 놓을 이전 캘리브레이션
        self.reset_done = False
        self.on = False
        self.thread = None
        self.saved_path = ""
        self.saved = {}

    @property
    def active(self):
        return self.stage == "ranging"

    # ---- 시작 / 종료 -----------------------------------------------------------
    def start(self, side, role):
        if self.active:
            raise RuntimeError("이미 캘리브레이션 진행 중")
        arm = ARM_CFGS.get(side)
        if not arm:
            raise RuntimeError(f"알 수 없는 팔: {side}")
        port = arm.get(f"{role}_port")
        if not port:
            raise RuntimeError(f"{side}/{role} 포트가 지정되지 않았습니다 — 셋업 탭에서 먼저 설정하세요")
        self._reset()
        self.side, self.role = side, role
        if kind()["calib"] != "range":
            raise RuntimeError(f"{kind()['label']} 은 범위 기록 캘리브레이션이 없습니다 — '공장값 쓰기' 를 쓰세요")
        dev = make_arm(role, arm)
        self.old_calib = dict(dev.calibration) if dev.calibration else None
        # calibrate=True 면 input() 에서 멈춥니다. 팔로워는 Goal=현재 위치로 맞춘 뒤 configure 해서
        # 연결 순간 이전 목표로 튀지 않게 합니다 (connect_follower 참고)
        if role == "follower":
            connect_follower(dev)
        else:
            try:
                dev.connect(calibrate=False)
            except Exception:
                close_arm(dev, disable_torque=False)
                raise
        try:
            from lerobot.motors.feetech import OperatingMode
            # configure() 의 torque_disabled() 가 끝나며 토크를 다시 켜므로 여기서 확실히 끕니다
            failed = torque_off_all(dev.bus)
            if failed:
                raise RuntimeError(f"토크 해제 실패: {', '.join(failed)} — 전원·케이블 확인")
            for m in dev.bus.motors:
                dev.bus.write("Operating_Mode", m, OperatingMode.POSITION.value)
            # Homing_Offset=0, 위치 제한 전체 개방 → 이제 읽는 값이 곧 원시 엔코더값
            dev.bus.reset_calibration()
            self.reset_done = True
            pos = dev.bus.sync_read("Present_Position", normalize=False)
        except Exception:
            if self.reset_done and self.old_calib:
                try:
                    dev.bus.write_calibration(self.old_calib)
                except Exception:
                    pass
            close_arm(dev)
            raise
        self.pos = {k: int(v) for k, v in pos.items()}
        self.prev_raw = dict(self.pos)
        self.unw = dict(self.pos)
        self.lo = dict(self.pos)
        self.hi = dict(self.pos)
        self.device = dev
        self.stage = "ranging"
        self.on = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while self.on:
            try:
                with self.lock:
                    if self.device is None:
                        break
                    pos = self.device.bus.sync_read("Present_Position", normalize=False)
                self.pos = {k: int(v) for k, v in pos.items()}
                if self.stage == "ranging":
                    for k, v in self.pos.items():
                        prev = self.prev_raw.get(k, v)
                        d = v - prev
                        if d > RES_HALF:            # 4095 → 0 방향으로 넘어감
                            d -= RES
                        elif d < -RES_HALF:         # 0 → 4095 방향으로 넘어감
                            d += RES
                        self.prev_raw[k] = v
                        u = self.unw.get(k, v) + d
                        self.unw[k] = u
                        self.lo[k] = min(self.lo.get(k, u), u)
                        self.hi[k] = max(self.hi.get(k, u), u)
                self.err = ""
            except Exception as e:
                self.err = str(e)
            time.sleep(0.1)

    def _close(self):
        self.on = False
        t, self.thread = self.thread, None
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.5)
        dev, self.device = self.device, None
        if dev is not None:
            with self.lock:
                close_arm(dev)

    # ---- 판정 ----------------------------------------------------------------
    def problems(self):
        """(안 움직인 관절, 너무 좁게 움직인 관절, 한 바퀴를 넘은 관절)."""
        block, warn, over = [], [], []
        for m in CTL_JOINTS:
            if m == FULL_TURN_MOTOR:
                continue
            span = self.hi.get(m, 0) - self.lo.get(m, 0)
            if span >= RES - 2:
                # 한 바퀴 이상은 단일 회전 엔코더로 표현할 수 없습니다
                over.append(m)
            elif span <= 0:
                block.append(m)
            elif span * 360 / (RES - 1) < SPAN_OK_DEG:
                warn.append(m)
        return block, warn, over

    def _calib_values(self, m):
        """기록된 범위의 중심이 2047(반 바퀴)에 오도록 homing_offset 을 역산.
        Present = Actual - Homing_Offset 이므로 중심을 빼 주면 됩니다."""
        half = (RES - 1) // 2                       # 2047 — lerobot _get_half_turn_homings 와 동일
        if m == FULL_TURN_MOTOR:
            # 전체 회전 관절: 지금 자세를 0° 기준으로, 범위는 한 바퀴 전체
            cur = int(self.pos.get(m, half))
            off = _signed_mod(cur - half)
            # Homing_Offset 은 부호-크기(11비트) 인코딩이라 ±2047 까지만 됩니다. -2048 은 2047 로 (1 tick 차이)
            return (2047 if off == -RES_HALF else off), 0, RES - 1
        lo, hi = float(self.lo[m]), float(self.hi[m])
        center = (lo + hi) / 2
        half_span = (hi - lo) / 2
        off = _signed_mod(int(round(center)) - half)
        rmin, rmax = int(round(half - half_span)), int(round(half + half_span))
        if off == -RES_HALF:
            # -2048 대신 2047 을 쓰면 Present 가 +1 tick 밀리므로 범위도 같이 +1
            off, rmin, rmax = 2047, rmin + 1, rmax + 1
        return off, rmin, rmax

    def finish(self):
        if self.stage != "ranging":
            raise RuntimeError("범위 기록 단계가 아닙니다")
        block, _, over = self.problems()
        if over:
            raise RuntimeError(
                "한 바퀴(360°) 이상 움직인 관절: " + ", ".join(over)
                + " — 단일 회전 엔코더로는 표현할 수 없습니다. 해당 관절을 기계적 한계 안에서만 움직이세요.")
        if block:
            raise RuntimeError("아직 움직이지 않은 관절: " + ", ".join(block))
        from lerobot.motors import MotorCalibration
        dev = self.device
        calib = {}
        for m, motor in dev.bus.motors.items():
            off, rmin, rmax = self._calib_values(m)
            if not (0 <= rmin < rmax <= RES - 1):
                raise RuntimeError(f"{m}: 계산된 범위가 잘못됨 ({rmin}~{rmax}) — 다시 기록하세요")
            calib[m] = MotorCalibration(id=motor.id, drive_mode=0,
                                        homing_offset=off, range_min=rmin, range_max=rmax)
        with self.lock:
            dev.bus.write_calibration(calib)     # Homing_Offset + Min/Max_Position_Limit 기록
            dev.calibration = calib
            dev._save_calibration()
        self.saved_path = str(dev.calibration_fpath)
        self.saved = {m: {"homing_offset": c.homing_offset,
                          "range_min": c.range_min, "range_max": c.range_max}
                      for m, c in calib.items()}
        self.stage = "done"
        self._close()

    def cancel(self):
        if self.device is not None and self.reset_done and self.old_calib:
            # reset_calibration() 이 모터 EEPROM 을 이미 바꿨으므로 이전 값을 되돌려 놓습니다
            with self.lock:
                try:
                    self.device.bus.write_calibration(self.old_calib)
                except Exception as e:
                    self.err = f"이전 캘리브레이션 복원 실패: {e}"
        self._close()
        self.stage = "idle"       # done/error 화면의 '닫기' 도 여기로 옵니다

    def state(self):
        rows = []
        for m in CTL_JOINTS:
            r = {"name": m, "pos": self.pos.get(m), "full_turn": m == FULL_TURN_MOTOR}
            if self.stage in ("ranging", "done"):
                lo, hi = self.lo.get(m), self.hi.get(m)
                r.update({"min": lo, "max": hi,
                          "span_deg": round((hi - lo) * 360 / (RES - 1), 1) if lo is not None else None})
                if lo is not None and self.stage == "ranging":
                    # 저장하면 이 범위가 됩니다 (중심이 2047 로 이동)
                    _, rmin, rmax = self._calib_values(m)
                    r.update({"out_min": rmin, "out_max": rmax})
            rows.append(r)
        block, warn, over = self.problems() if self.stage == "ranging" else ([], [], [])
        return {"stage": self.stage, "side": self.side, "role": self.role,
                "rows": rows, "err": self.err, "block": block, "warn": warn, "over": over,
                "saved_path": self.saved_path, "saved": self.saved,
                "span_ok_deg": SPAN_OK_DEG}


CALIB = CalibSession()


@app.get("/calib", response_class=HTMLResponse)
def calib_page(side: str = "", role: str = "", next: str = ""):
    wiz = {"side": side if side in ARM_CFGS else "",
           "role": role if role in ("follower", "leader") else "",
           "next": next if next.startswith("/setup/wizard") and "//" not in next else ""}
    return CSS + nav_html("cb") + f"<script>const WIZ={js(wiz)};</script>" + CALIB_HTML


# ----------------------------- 캘리브레이션 파일 모음 (여러 팔 세트 · 다른 PC 로 옮기기) --------------
CALIB_DIRS = sorted({d for k in ROBOT_KINDS.values() for d in k["calib_dir"].values()})
CALIB_MAX_FILE = 64 * 1024
CALIB_MAX_ZIP = 4 * 1024 * 1024


def calib_files():
    """이 PC 의 모든 캘리브레이션 파일 (기종·역할 폴더별) — 지금 셋업에서 쓰는 것 표시."""
    using = {str(calib_file(r, a[f"{r}_id"])) for a in ARM_CFGS.values() for r in ("follower", "leader")}
    out = []
    for d in CALIB_DIRS:
        for f in sorted((CALIB_ROOT / d).glob("*.json")):
            st = f.stat()
            out.append({"dir": d, "id": f.stem, "mtime": time.strftime("%F %H:%M", time.localtime(st.st_mtime)),
                        "using": str(f) in using})
    return out


def _calib_json_ok(raw):
    """lerobot MotorCalibration 모양인지: {모터이름: {id, drive_mode, homing_offset, range_min, range_max}}"""
    try:
        d = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    need = {"id", "drive_mode", "homing_offset", "range_min", "range_max"}
    return (isinstance(d, dict) and 0 < len(d) <= 64 and
            all(isinstance(v, dict) and need <= set(v) and all(isinstance(v[x], int) and not isinstance(v[x], bool)
                                                              for x in need) for v in d.values()))


@app.get("/api/calib/files")
def api_calib_files():
    return {"files": calib_files(), "root": str(CALIB_ROOT)}


@app.get("/api/calib/export")
def api_calib_export():
    """모든 캘리브레이션 파일을 zip 하나로 — 안의 경로는 calibration/ 아래 상대경로 그대로."""
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in calib_files():
            z.write(CALIB_ROOT / f["dir"] / f"{f['id']}.json", f"{f['dir']}/{f['id']}.json")
    name = f"openmimic_calib_{os.uname().nodename}_{time.strftime('%Y%m%d_%H%M')}.zip"
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@app.post("/api/calib/import")
async def api_calib_import(req: Request, overwrite: int = 0):
    """내보낸 zip 을 받아 같은 폴더에 넣습니다. 알려진 폴더(robots/so_follower 등)·안전한 이름·캘리브레이션 형식만 받고,
    이미 있는 다른 내용의 파일은 overwrite=1 일 때만 .bak 을 남기고 덮어씁니다."""
    import io
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 가져오세요"}, status_code=400)
    raw = b""
    async for chunk in req.stream():
        raw += chunk
        if len(raw) > CALIB_MAX_ZIP:
            return JSONResponse({"error": "파일이 너무 큽니다 (캘리브레이션 zip 은 4MB 이하)"}, status_code=400)
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return JSONResponse({"error": "zip 파일이 아닙니다 — 캘리브레이션 탭의 '내보내기' 로 만든 zip 을 고르세요"}, status_code=400)
    items, bad, conflict = [], [], []
    for info in z.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        parts = name.split("/")
        if parts and parts[0] == "calibration":       # 폴더째 묶은 zip 도 받습니다
            parts = parts[1:]
        d, fname = "/".join(parts[:-1]), parts[-1] if parts else ""
        if d not in CALIB_DIRS or not fname.endswith(".json") or not safe_name(fname[:-5]) or info.file_size > CALIB_MAX_FILE:
            bad.append(name)
            continue
        data = z.read(info)
        if not _calib_json_ok(data):
            bad.append(name)
            continue
        dst = CALIB_ROOT / d / fname
        if dst.is_file() and dst.read_bytes() != data:
            conflict.append(f"{d}/{fname}")
        items.append((dst, data))
    if not items:
        return JSONResponse({"error": "가져올 캘리브레이션 파일이 없습니다" + (f" (형식이 맞지 않는 파일: {', '.join(bad[:5])})" if bad else "")},
                            status_code=400)
    if conflict and not overwrite:
        return {"ok": False, "conflict": conflict, "bad": bad}
    done = []
    for dst, data in items:
        if dst.is_file() and dst.read_bytes() == data:
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file():
            shutil.copy2(dst, dst.with_suffix(".json.bak"))
        tmp = dst.with_suffix(".json.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dst)
        done.append(f"{dst.parent.name}/{dst.name}")
    return {"ok": True, "imported": done, "same": len(items) - len(done), "bad": bad}


@app.get("/api/calib/state")
def api_calib_state():
    st = CALIB.state()
    st["devices"] = calib_status()
    busy = exclusive_busy() if not CALIB.active else None
    st["busy"] = f"{busy['id']} 실행 중" if busy else ""
    st["ports_configured"] = ports_configured()
    st["calib_kind"] = kind()["calib"]
    st["robot_label"] = kind()["label"]
    return st


@app.post("/api/calib/start")
async def api_calib_start(req: Request):
    b = await req.json()
    side, role = b.get("side"), b.get("role")
    if role not in ("follower", "leader") or side not in ARM_CFGS:
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 종료 후 시작하세요"}, status_code=400)
    try:
        await asyncio.to_thread(CALIB.start, side, role)
    except Exception as e:
        CALIB.stage = "error"
        CALIB.err = f"{type(e).__name__}: {e}"
        return JSONResponse({"error": CALIB.err}, status_code=400)
    return {"ok": True}


@app.post("/api/calib/finish")
async def api_calib_finish():
    try:
        await asyncio.to_thread(CALIB.finish)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "path": CALIB.saved_path}


def factory_calibrate(side, role):
    """OMX 처럼 범위 기록 없이 공장값을 쓰는 기종의 캘리브레이션.
    lerobot 장치의 calibrate() 를 그대로 부릅니다 (토크 OFF → Operating_Mode·Drive_Mode → 공장값
    오프셋 0·범위 0~4095 기록 → 파일 저장). 입력 대기가 없어 웹에서 그대로 돌릴 수 있습니다."""
    k = kind()
    if k["calib"] != "factory":
        raise RuntimeError(f"{k['label']} 은 범위를 기록하는 캘리브레이션을 씁니다")
    arm = ARM_CFGS[side]
    if not arm.get(f"{role}_port"):
        raise RuntimeError("포트가 지정되지 않았습니다")
    dev = make_arm(role, arm)
    f = Path(dev.calibration_fpath)
    try:
        dev.bus.connect()                 # 핸드셰이크 = 모터 6개 확인
        failed = torque_off_all(dev.bus)
        if failed:
            raise RuntimeError(f"토크 해제 실패: {', '.join(failed)}")
        if f.exists():
            shutil.copy2(f, f.with_suffix(".json.bak"))
        dev.calibrate()
    finally:
        close_arm(dev)
    return str(f)


@app.post("/api/calib/factory")
async def api_calib_factory(req: Request):
    b = await req.json()
    side, role = b.get("side"), b.get("role")
    if side not in ARM_CFGS or role not in ("follower", "leader"):
        return JSONResponse({"error": "side/role 이 잘못됨"}, status_code=400)
    busy = exclusive_busy()
    if busy:
        return JSONResponse({"error": f"{busy['id']} 실행 중 — 끝난 뒤 하세요"}, status_code=400)
    try:
        path = await asyncio.to_thread(factory_calibrate, side, role)
    except Exception as e:
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)
    return {"ok": True, "path": path}


@app.post("/api/calib/cancel")
async def api_calib_cancel():
    await asyncio.to_thread(CALIB.cancel)
    return {"ok": True}


CALIB_HTML = """
<style>
.devgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin-bottom:18px}
.dev{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.dev h3{margin:0 0 6px;font-family:var(--mono);font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--accent)}
.dev .p{font-family:var(--mono);font-size:11px;color:var(--dim);word-break:break-all;margin-bottom:10px}
.stagebox{background:var(--surface);border:1px solid var(--accent);border-radius:10px;padding:18px 20px;margin-bottom:18px}
.stagebox h3{margin:0 0 6px;font-size:16px}
.stagebox .inst{color:var(--muted);margin:0 0 14px;line-height:1.7}
.bar{height:6px;background:var(--surface2);border-radius:3px;overflow:hidden;min-width:120px}
.bar i{display:block;height:100%;background:var(--accent)}
.bar.ok i{background:var(--ok)} .bar.warn i{background:var(--warn)} .bar.bad i{background:var(--bad)}
td.mono{font-variant-numeric:tabular-nums}
.stepdots{display:flex;gap:6px;align-items:center;font-family:var(--mono);font-size:11px;color:var(--dim);margin-bottom:14px}
.stepdots b{color:var(--text)}
.stepdots span.on{color:var(--accent)}
</style>
<div class=wrap>
<p class=eyebrow>모터 캘리브레이션</p><h2>캘리브레이션</h2>
<div id=busywarn></div>
<div id=picker></div>
<div id=stage></div>
<p class=eyebrow style="margin-top:26px">캘리브레이션 파일 모음</p>
<div class=dev>
  <p class=muted style="margin-top:0">이 PC 에 있는 모든 팔의 캘리브레이션 파일입니다. 팔 세트가 여러 개면 셋업에서 캘리브 id 를 세트마다 다르게
  (예: <span class=mono>b2_follower_left</span>) 두고 환경으로 저장해 바꿔 쓰세요. <b>내보내기</b> 한 zip 을 다른 PC 의 <b>가져오기</b> 로 넣으면 그대로 옮겨집니다.</p>
  <div id=cfiles class=muted>…</div>
  <div class=row style="margin-top:10px;align-items:center">
    <a class=btnlink href="/api/calib/export" download><button type=button>내보내기 (zip)</button></a>
    <input type=file id=calf accept=".zip">
    <button type=button id=calimp>가져오기</button>
    <span id=calmsg class=muted></span>
  </div>
</div>
</div>
<script>
const $=id=>document.getElementById(id);
async function loadCalibFiles(){
  let d; try{ d=await (await fetch('/api/calib/files')).json(); }catch(e){ return; }
  const fs=d.files||[];
  if(!fs.length){ $('cfiles').textContent='캘리브레이션 파일이 아직 없습니다.'; return; }
  let h='<table><tr><th>폴더</th><th>캘리브 id</th><th>수정</th><th></th></tr>';
  fs.forEach(f=>{ h+='<tr><td class=mono>'+E(f.dir)+'</td><td class=mono>'+E(f.id)+'</td><td class=mono>'+E(f.mtime)+'</td><td>'
    +(f.using?'<span class="badge b-ok">지금 셋업에서 사용</span>':'')+'</td></tr>'; });
  $('cfiles').innerHTML=h+'</table>';
}
async function importCalib(overwrite){
  const f=$('calf').files[0];
  if(!f){ alert('내보내기로 만든 zip 파일을 고르세요'); return; }
  $('calmsg').textContent='가져오는 중…';
  let d;
  try{ d=await (await fetch('/api/calib/import?overwrite='+(overwrite?1:0),{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:f})).json(); }
  catch(e){ $('calmsg').textContent=''; alert('가져오기 실패: '+e); return; }
  $('calmsg').textContent='';
  if(d.error){ alert(d.error); return; }
  if(d.conflict){
    if(confirm('이 PC 에 같은 이름의 다른 캘리브레이션 파일이 있습니다:\\n  '+d.conflict.join('\\n  ')
      +'\\n\\n덮어쓸까요? (기존 파일은 .bak 으로 남깁니다)')) return importCalib(true);
    return;
  }
  let m='가져옴: '+(d.imported.length?d.imported.join(', '):'없음')+(d.same?' · 이미 같은 파일 '+d.same+'개':'');
  if(d.bad&&d.bad.length) m+='\\n형식이 맞지 않아 건너뜀: '+d.bad.join(', ');
  alert(m); loadCalibFiles(); refresh();
}
$('calimp').addEventListener('click',()=>importCalib(false));
loadCalibFiles();
const E=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function jget(u){ return (await fetch(u)).json(); }
async function jpost(u,b){
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})});
  return r.json();
}
let ST=null, timer=null;

function renderPicker(s){
  let h='';
  if(!s.ports_configured)
    h+='<div class="card needcard"><div class=needtxt><b>먼저 팔을 연결하세요</b><span class=muted>USB 포트가 아직 지정되지 않았습니다 — 셋업 마법사에서 팔(보드)을 하나씩 연결합니다.</span></div><a class="btnlink primary" href="/setup/wizard">셋업 마법사 시작</a></div>';
  if(s.calib_kind==='factory')
    h+='<p class=muted>이 기종('+E(s.robot_label)+')은 범위를 기록하지 않고 lerobot 의 공장값(오프셋 0, 범위 0~4095)을 캘리브레이션으로 씁니다. '
      +'버튼을 누르면 토크를 끄고 운전 모드·회전 방향·공장값을 모터에 쓰고 파일을 저장합니다.</p>';
  h+='<div class=devgrid>';
  Object.keys(s.devices).forEach(side=>{
    ['follower','leader'].forEach(role=>{
      const d=s.devices[side][role];
      const busy=s.stage==='homing'||s.stage==='ranging';
      h+='<div class=dev><h3>'+E(side)+' · '+role+'</h3>'
        +'<span class="badge '+(d.ok?'b-ok':'b-bad')+'">'+(d.ok?'캘리브레이션 있음':'없음')+'</span> '
        +'<span class=mono style="font-size:12px">'+E(d.id)+'.json</span>'
        +'<div class=p>'+E(d.path)+'</div>'
        +(s.calib_kind==='factory'
          ? '<button class=primary '+(busy||!s.ports_configured?'disabled':'')
            +' onclick="factory(\\''+E(side)+'\\',\\''+role+'\\',this)">'+(d.ok?'공장값 다시 쓰기':'공장값 쓰기')+'</button>'
          : '<button class=primary '+(busy||!s.ports_configured?'disabled':'')
            +' onclick="start(\\''+E(side)+'\\',\\''+role+'\\',this)">'+(d.ok?'다시 캘리브레이션':'캘리브레이션 시작')+'</button>')
        +'</div>';
    });
  });
  h+='</div>';
  if(WIZ.side && WIZ.role && s.stage!=='homing' && s.stage!=='ranging'){
    h='<div class=stagebox style="margin-bottom:14px"><h3>셋업 마법사 — '+E(WIZ.side)+' · '+E(WIZ.role)+'</h3>'
      +'<p class=inst>아래 <b>'+E(WIZ.side)+' · '+E(WIZ.role)+'</b> 카드에서 캘리브레이션을 하세요. 저장하면 마법사로 돌아가는 버튼이 나옵니다.</p>'
      +(WIZ.next?'<a href="'+E(WIZ.next)+'"><button>캘리브레이션 없이 마법사로 돌아가기</button></a>':'')+'</div>'+h;
  }
  $('picker').innerHTML=h;
}

function rows(s, withRange){
  let h='<table><tr><th>관절</th><th class=num>현재값</th>'
    +(withRange?'<th class=num>min</th><th class=num>max</th><th class=num>폭</th>'
               +'<th class=num>저장될 범위</th><th style="width:150px"></th>':'')
    +'</tr>';
  s.rows.forEach(r=>{
    const n=r.name;
    h+='<tr><td class=mono>'+n+(r.full_turn?' <span class=muted style="font-size:11px">(전체 회전 · 지금 자세가 0°)</span>':'')+'</td>'
      +'<td class="num mono" id="c_pos_'+n+'"></td>';
    if(withRange){
      if(r.full_turn){ h+='<td class=num>-</td><td class=num>-</td><td class=num>-</td>'
                        +'<td class="num mono">0 ~ 4095</td><td></td>'; }
      else{
        h+='<td class="num mono" id="c_min_'+n+'"></td><td class="num mono" id="c_max_'+n+'"></td>'
          +'<td class="num mono" id="c_span_'+n+'"></td>'
          +'<td class="num mono" id="c_out_'+n+'"></td>'
          +'<td><div class="bar" id="c_bar_'+n+'"><i style="width:0%"></i></div></td>';
      }
    }
    h+='</tr>';
  });
  return h+'</table>';
}

/* 값만 갱신 — DOM 을 다시 만들지 않습니다.
   250ms 마다 통째로 다시 그리면 버튼/셀이 교체되며 클릭이 씹힙니다. */
function patchRows(s){
  s.rows.forEach(r=>{
    const n=r.name;
    const pos=$('c_pos_'+n); if(pos) pos.textContent = r.pos==null?'-':r.pos;
    if(r.full_turn) return;
    const mn=$('c_min_'+n); if(mn) mn.textContent = r.min==null?'-':r.min;
    const mx=$('c_max_'+n); if(mx) mx.textContent = r.max==null?'-':r.max;
    const sp=r.span_deg||0;
    const over=(s.over||[]).indexOf(n)>=0;
    const spc=$('c_span_'+n);
    if(spc) spc.innerHTML = over?'<span class=b-bad>'+sp.toFixed(1)+'° ⚠</span>':sp.toFixed(1)+'°';
    const out=$('c_out_'+n);
    if(out) out.textContent = (r.out_min==null?'-':r.out_min+' ~ '+r.out_max);
    const bar=$('c_bar_'+n);
    if(bar){
      bar.className='bar '+(over||sp<=0?'bad':(sp<s.span_ok_deg?'warn':'ok'));
      bar.firstElementChild.style.width=Math.min(100, sp/180*100)+'%';
    }
  });
}

function dots(n){
  const names=['연결','범위 기록','저장'];
  return '<div class=stepdots>'+names.map((x,i)=>
    '<span class="'+(i===n?'on':'')+'">'+(i<n?'✓ ':'')+(i===n?'<b>'+x+'</b>':x)+'</span>'+(i<names.length-1?' › ':'')).join('')+'</div>';
}

function noteHtml(s){
  const ovr=(s.over||[]).length, blk=s.block.length, wrn=s.warn.length;
  if(ovr) return '<p class="badge b-bad">한 바퀴(360°) 이상 움직였습니다: '+s.over.join(', ')
    +'<br>단일 회전 엔코더로는 표현할 수 없습니다. 기계적 한계 안에서만 움직이세요.</p>';
  if(blk) return '<p class="badge b-bad">아직 움직이지 않은 관절: '+s.block.join(', ')+'</p>';
  if(wrn) return '<p class="badge b-warn">'+s.span_ok_deg+'° 미만으로만 움직인 관절: '+s.warn.join(', ')+' — 의도한 게 아니면 더 움직이세요</p>';
  return '<p class="badge b-ok">모든 관절 기록됨 — 저장할 수 있습니다</p>';
}

/* 구조를 다시 만들지 않고 살아 있는 값만 덮어씁니다 */
function patchStage(s){
  patchRows(s);
  const n=$('cnote'); if(n) n.innerHTML=noteHtml(s);
  const e=$('cerr');
  if(e){ e.innerHTML = s.err?'<p class="badge b-bad">'+E(s.err)+'</p>':''; }
  const fb=$('cfinish');
  if(fb) fb.disabled = s.block.length>0 || (s.over||[]).length>0;
}

function renderStage(s){
  const box=$('stage');
  const head='<h3>'+E(s.side)+' · '+E(s.role)+'</h3>';
  const err='<div id=cerr></div>';
  if(s.stage==='ranging'){
    const follower = s.role==='follower';
    box.innerHTML='<div class=stagebox>'+head+dots(1)
      +'<p class=inst>토크가 꺼져 있습니다'+(follower?' — <b>팔로워가 주저앉을 수 있으니 손으로 받치세요.</b>':'.')+'<br>'
      +'<b>wrist_roll 을 뺀 모든 관절</b>을 한 개씩, 한쪽 끝에서 반대쪽 끝까지 <b>천천히</b> 움직이세요. '
      +'그리퍼도 완전히 열고 완전히 닫으세요.<br>'
      +'기계적 스톱에 <b>살짝 닿기 직전</b>까지만 — 여기서 기록되는 범위가 그대로 관절 한계가 됩니다.<br>'
      +'<b>중앙을 맞출 필요는 없습니다.</b> 쓸어본 범위의 중심으로 기준점을 자동 계산합니다. '
      +'<span class=mono>wrist_roll</span> 은 전체 회전이라 범위를 재지 않고, <b>완료·저장을 누르는 순간의 자세가 0° 기준</b>이 됩니다 — '
      +'<b>리더와 팔로워를 같은 방향</b>(예: 그리퍼 턱이 수평)으로 두고 저장하세요. 어긋나면 팔로우할 때 손목이 그만큼 돌아간 채로 따라갑니다.<br>'
      +'각 줄의 막대가 초록이 되면 충분합니다. 끝나면 <b>완료·저장</b>.</p>'
      +err+'<div id=cnote></div>'+rows(s,true)
      +'<div class=toolbar style="margin-top:14px">'
      +'<button class="primary big" id=cfinish onclick="finish(this)">완료·저장</button>'
      +'<button class=danger onclick="cancel()">취소 (이전 값 복원)</button></div></div>';
  }else if(s.stage==='done'){
    box.innerHTML='<div class=stagebox>'+head+dots(2)
      +'<p class="badge b-ok">저장됨</p><p class="mono" style="font-size:12px;color:var(--muted)">'+E(s.saved_path)+'</p>'
      +rows(s,true)
      +'<pre style="margin-top:12px">'+E(JSON.stringify(s.saved,null,2))+'</pre>'
      +'<div class=toolbar style="margin-top:14px">'
      +(WIZ.next?'<a href="'+E(WIZ.next)+'"><button class=primary>셋업 마법사로 돌아가기</button></a>':'')
      +'<button class=primary onclick="closeDone()">닫기</button>'
      +'<a href="/control"><button>제어 탭에서 확인</button></a></div></div>';
  }else if(s.stage==='error'){
    box.innerHTML='<div class=stagebox>'+head+'<p class="badge b-bad">'+E(s.err)+'</p>'
      +'<div class=toolbar><button onclick="closeDone()">닫기</button></div></div>';
  }else{
    box.innerHTML='';
  }
}

let LAST_KEY=null, LAST_PICK=null, polling=false;

/* 구조가 바뀌었을 때만 다시 그리고, 평소엔 값만 갱신 */
function apply(s){
  ST=s;
  const busy=s.busy?'<p class="badge b-warn">'+E(s.busy)+' — 끝나야 캘리브레이션을 시작할 수 있습니다</p>':'';
  if($('busywarn').innerHTML!==busy) $('busywarn').innerHTML=busy;
  const pick=JSON.stringify([s.devices, s.stage, s.ports_configured]);
  if(pick!==LAST_PICK){ LAST_PICK=pick; renderPicker(s); }
  const key=[s.stage, s.side, s.role].join('|');
  if(key!==LAST_KEY){ LAST_KEY=key; renderStage(s); }
  patchStage(s);
}

async function refresh(){
  if(polling) return;          /* 응답이 느려도 요청이 쌓이지 않게 */
  polling=true;
  try{ apply(await jget('/api/calib/state')); }
  catch(e){ /* 일시적 네트워크 오류는 무시하고 다음 주기에 */ }
  finally{ polling=false; }
}

/* setInterval 은 느린 응답에서 요청이 겹칩니다 — 끝난 뒤 다음을 예약 */
async function poll(){
  await refresh();
  timer=setTimeout(poll, 250);
}

/* 클릭 즉시 잠가서 중복 클릭·먹통 체감을 없앰 */
async function withBusy(el, fn){
  if(el){ el.disabled=true; el.dataset.t=el.textContent; el.textContent='처리 중…'; }
  try{ return await fn(); }
  finally{
    // 콜백이 라벨을 바꿨으면(예: 감시 시작 → 감시 중지) 그대로 둡니다.
    if(el && el.isConnected){
      el.disabled=false;
      if(el.dataset.t && el.textContent==='처리 중…') el.textContent=el.dataset.t;
    }
  }
}
async function factory(side,role,el){
  if(!confirm(side+' · '+role+': 토크를 끄고 공장값 캘리브레이션을 씁니다'+(role==='follower'?' — 팔로워는 받치세요':'')+'. 계속할까요?')) return;
  await withBusy(el, async()=>{
    const r=await jpost('/api/calib/factory',{side:side,role:role});
    if(r.error) alert(r.error);
    else if(WIZ.next) location.href=WIZ.next;
    await refresh();
  });
}
async function start(side,role,el){
  await withBusy(el, async()=>{
    const r=await jpost('/api/calib/start',{side:side,role:role});
    if(r.error) alert(r.error);
    await refresh();
  });
}
async function finish(el){
  if(ST&&ST.warn&&ST.warn.length&&!confirm('일부 관절이 좁게만 움직였습니다:\\n'+ST.warn.join(', ')+'\\n이대로 저장할까요?')) return;
  await withBusy(el, async()=>{
    const r=await jpost('/api/calib/finish');
    if(r.error) alert(r.error);
    await refresh();
  });
}
async function cancel(){
  if(!confirm('취소하면 지금까지 기록이 버려집니다.')) return;
  await jpost('/api/calib/cancel'); await refresh();
}
async function closeDone(){ await jpost('/api/calib/cancel'); await refresh(); }
addEventListener('pagehide',()=>{ if(ST&&(ST.stage==='homing'||ST.stage==='ranging')) navigator.sendBeacon('/api/calib/cancel'); });
poll();
</script>"""



# ----------------------------- Record worker (별도 프로세스, 5단계) ----------------
# lerobot-record 를 셸로 띄우고 PTY 로 키를 넣던 것을 없앴습니다. 대신 이 파일 자체를
#   python main.py --worker record <jid>
# 로 띄워 lerobot 의 record_loop() 를 직접 부릅니다. events 딕트가 곧 n/r/q 입니다.
#   상태   : RUN_DIR/<jid>/status.json       (worker → 웹, PREVIEW_FPS 로 갱신)
#   미리보기: RUN_DIR/<jid>/cam_<name>.jpg   (worker → 웹, 원자적 교체)
#   명령   : RUN_DIR/<jid>/cmd               (웹 → worker, n/r/q 문자를 append)
# 전부 파일이라 OpenMimic 을 재시작해도 세션을 잃지 않습니다.

def _cam_configs(specs):
    from lerobot.cameras.opencv import OpenCVCameraConfig
    out = {}
    for name, sp in specs.items():
        idx = sp["index_or_path"]
        idx = idx if isinstance(idx, int) else Path(str(idx))
        out[name] = OpenCVCameraConfig(index_or_path=idx, fps=int(sp["fps"]), **cam_fourcc_kw(sp),
                                       width=int(sp["width"]), height=int(sp["height"]))
    return out


def make_devices(spec):
    """spec = 시작 시점의 설정 스냅샷. (robot, teleop, 하위 팔 객체 목록) — 기종 × 한팔/양팔 분기.
    하위 팔 객체 목록은 캘리브레이션 파일 확인·기록용입니다."""
    arms = {a["side"]: a for a in spec["arms"]}
    mrt = mrt_value(spec.get("max_relative_target"))
    k = kind(spec)                       # spec 의 robot (예전 spec 에는 없음 → so101)
    if spec["mode"] == "bimanual":
        robot = make_bi("follower", arms, k=k, mrt=mrt, cameras=_cam_configs(spec["cameras"]))
        teleop = make_bi("leader", arms, k=k)
        subs = [robot.left_arm, robot.right_arm, teleop.left_arm, teleop.right_arm]
    else:
        arm = spec["arms"][0]
        cams = dict(arm["cameras"])
        cams.update(spec["cameras"])
        robot = make_arm("follower", arm, k=k, mrt=mrt, cameras=_cam_configs(cams))
        teleop = make_arm("leader", arm, k=k)
        subs = [robot, teleop]
    return robot, teleop, subs


class _Preview:
    """robot.get_observation() 을 감싸 최신 카메라 프레임을 잡아두고, 별도 스레드가
    PREVIEW_FPS 로 JPEG + status.json 을 씁니다. record 루프에는 인코딩 비용을 얹지 않습니다."""

    def __init__(self, robot, rd, status):
        self.rd = rd
        self.status = status
        self.latest = {}
        self.on = True
        orig = robot.get_observation

        def tee():
            obs = orig()
            for k, v in obs.items():
                if getattr(v, "ndim", 0) == 3:
                    self.latest[k] = v
            return obs

        robot.get_observation = tee     # 인스턴스 속성이 클래스 메서드를 가림
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def write_status(self):
        st = dict(self.status)
        if st.get("t0"):
            st["elapsed"] = round(time.time() - st["t0"], 1)
        tmp = self.rd / "status.json.preview.tmp"     # put() 과 다른 임시 파일 (동시 쓰기 섞임 방지)
        tmp.write_text(json.dumps(st))
        os.replace(tmp, self.rd / "status.json")

    def _loop(self):
        try:
            import cv2
        except ImportError:
            cv2 = None
        while self.on:
            t0 = time.monotonic()
            try:
                self.write_status()
                if cv2 is not None:
                    for k, frame in list(self.latest.items()):
                        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                                               [cv2.IMWRITE_JPEG_QUALITY, 60])
                        if ok:
                            tmp = self.rd / f"cam_{k}.jpg.tmp"
                            tmp.write_bytes(buf.tobytes())
                            os.replace(tmp, self.rd / f"cam_{k}.jpg")
            except Exception:
                pass
            time.sleep(max(0.0, 1.0 / PREVIEW_FPS - (time.monotonic() - t0)))

    def stop(self):
        self.on = False
        self.thread.join(timeout=2)     # 늦게 끝난 os.replace 가 최종 상태를 덮어쓰지 않게


def worker_record(jid):
    import logging
    import signal as _sig
    j = load_json(JOB_DIR / f"{jid}.json", {})
    spec = j.get("spec") or {}
    rd = run_dir(jid)
    rd.mkdir(parents=True, exist_ok=True)
    status = {"phase": "starting", "episode": None, "recorded": 0,
              "num_episodes": int(spec.get("num_episodes", 0)), "t0": None, "phase_len": 0,
              "elapsed": 0.0, "err": "", "repo_id": spec.get("repo_id", ""), "cams": [],
              "temp": {}, "maxtemp": None, "last": ""}
    events = {"exit_early": False, "rerecord_episode": False, "stop_recording": False}
    ui = {"start": False}        # 대기 상태에서 '녹화 시작' 을 눌렀는지

    def on_key(k):
        # exit_early 는 record_loop 안에서 소비되므로, 어느 단계에 있든 현재 루프를 깨웁니다.
        # 단계에 맞지 않는 키는 무시합니다 (대기 중 'r' 이 다음 에피소드를 버리거나, 녹화 중 's' 가 저장처럼 동작하는 것 방지)
        ph = status.get("phase")
        if k == "s":
            if ph != "ready":
                return
            ui["start"] = True
            events["exit_early"] = True
        elif k == "n":
            if ph != "record":
                return
            events["exit_early"] = True
        elif k == "r":
            if ph != "record":
                return
            events["rerecord_episode"] = True
            events["exit_early"] = True
        elif k == "q":
            events["stop_recording"] = True
            events["exit_early"] = True

    alive = {"on": True}

    def poll_cmd():
        f = rd / "cmd"
        while alive["on"]:
            try:
                if f.exists():
                    txt = f.read_text()
                    f.unlink()
                    for ch in txt:
                        on_key(ch)
                        print(f"[openmimic-worker] key {ch}", flush=True)
            except OSError:
                pass
            time.sleep(0.05)

    def put(**kw):
        status.update(kw)
        try:
            tmp = rd / "status.json.tmp"
            tmp.write_text(json.dumps(status))
            os.replace(tmp, rd / "status.json")
        except OSError:
            pass

    # 준비 단계가 길어도 화면이 멈춰 보이지 않도록, 첫 줄부터 status.json 을 씁니다.
    put(phase="starting", t0=time.time())

    threading.Thread(target=poll_cmd, daemon=True).start()
    _sig.signal(_sig.SIGINT, lambda *_: on_key("q"))     # 작업 탭 '중지' = q 와 동일
    _sig.signal(_sig.SIGTERM, lambda *_: on_key("q"))

    dataset = robot = teleop = preview = None
    rc = 0
    try:
        # torch 까지 끌려와서 첫 실행은 수십 초 걸립니다 — 단계를 화면에 알려 줍니다.
        put(phase="importing")
        from lerobot.utils.utils import init_logging
        init_logging()
        from lerobot.common.control_utils import sanity_check_dataset_robot_compatibility
        from lerobot.configs.dataset import DatasetRecordConfig
        from lerobot.datasets import (LeRobotDataset, VideoEncodingManager,
                                      aggregate_pipeline_dataset_features, create_initial_features)
        from lerobot.processor import make_default_processors
        from lerobot.scripts.lerobot_record import record_loop
        from lerobot.utils.feature_utils import combine_feature_dicts

        put(phase="devices")
        robot, teleop, subs = make_devices(spec)
        for d in subs:
            if not d.calibration:
                raise RuntimeError(f"캘리브레이션 파일이 없습니다: {d.calibration_fpath} — 캘리브레이션 탭에서 만드세요")

        tap, rap, rop = make_default_processors()
        features = combine_feature_dicts(
            aggregate_pipeline_dataset_features(
                pipeline=tap, initial_features=create_initial_features(action=robot.action_features),
                use_videos=True),
            aggregate_pipeline_dataset_features(
                pipeline=rop, initial_features=create_initial_features(observation=robot.observation_features),
                use_videos=True))
        # 인코더/이미지라이터 기본값은 lerobot-record 와 동일하게 DatasetRecordConfig 에서 가져옵니다
        dcfg = DatasetRecordConfig(repo_id=spec["repo_id"], single_task=spec["task"], root=spec["root"],
                                   fps=int(spec["fps"]), episode_time_s=spec["episode_time_s"],
                                   num_episodes=int(spec["num_episodes"]),
                                   push_to_hub=False, streaming_encoding=bool(spec.get("streaming_encoding", False)))
        put(phase="dataset")
        # 양팔은 robot.cameras 가 이름 충돌(wrist)로 줄어드므로 관측 키에서 셉니다
        ncam = sum(1 for v in robot.observation_features.values() if isinstance(v, tuple))
        iw_p = dcfg.num_image_writer_processes if ncam else 0
        iw_t = dcfg.num_image_writer_threads_per_camera * ncam if ncam else 0
        if spec.get("resume"):
            dataset = LeRobotDataset.resume(
                dcfg.repo_id, root=dcfg.root, batch_encoding_size=dcfg.video_encoding_batch_size,
                rgb_encoder=dcfg.rgb_encoder, depth_encoder=dcfg.depth_encoder,
                encoder_threads=dcfg.encoder_threads, streaming_encoding=dcfg.streaming_encoding,
                encoder_queue_maxsize=dcfg.encoder_queue_maxsize,
                image_writer_processes=iw_p, image_writer_threads=iw_t)
            sanity_check_dataset_robot_compatibility(dataset, robot, dcfg.fps, features)
        else:
            dataset = LeRobotDataset.create(
                dcfg.repo_id, dcfg.fps, root=dcfg.root, robot_type=robot.name, features=features,
                use_videos=True, image_writer_processes=iw_p, image_writer_threads=iw_t,
                batch_encoding_size=dcfg.video_encoding_batch_size,
                rgb_encoder=dcfg.rgb_encoder, depth_encoder=dcfg.depth_encoder,
                encoder_threads=dcfg.encoder_threads, streaming_encoding=dcfg.streaming_encoding,
                encoder_queue_maxsize=dcfg.encoder_queue_maxsize)

        put(phase="connecting")            # 시리얼 + 카메라 오픈. 카메라가 말썽이면 여기서 오래 걸립니다
        # calibrate=True 면 input() → 파이프에서 EOFError. 팔 하나씩 안전 순서로 연결합니다
        # (토크 끈 채 캘리브레이션 기록 → Goal=현재 위치 → configure). 양팔도 lerobot 이 팔별 connect 를 부릅니다.
        followers = [robot.left_arm, robot.right_arm] if hasattr(robot, "left_arm") else [robot]
        leaders = [teleop.left_arm, teleop.right_arm] if hasattr(teleop, "left_arm") else [teleop]
        for d in followers:
            connect_follower(d)
        for d in leaders:
            connect_leader(d)
        # 미리보기 이름은 관측 키 기준 — 양팔이면 left_wrist / right_wrist / top 처럼 접두사가 붙습니다
        # (robot.cameras 는 호환용이라 양팔에서 이름이 겹칩니다)
        status["cams"] = [k for k, v in robot.observation_features.items() if isinstance(v, tuple)]
        preview = _Preview(robot, rd, status)

        fps, task = int(spec["fps"]), spec["task"]
        ept = spec["episode_time_s"]

        # 서보 상태 감시 — 팔로워 버스의 온도·전압·보호 플래그를 record_loop 안(get_observation 직후)에서
        # 1초마다 읽습니다. 반이중 버스라 다른 스레드에서 읽으면 패킷이 섞이므로 반드시 루프와 같은 스레드에서.
        if hasattr(robot, "left_arm"):
            fbuses = [("left", robot.left_arm.bus), ("right", robot.right_arm.bus)]
        else:
            fbuses = [("", robot.bus)]
        guard = openmimic_health.HealthGuard(fbuses)
        try:
            guard.start()
        except Exception as e:      # noqa: BLE001 — 감시를 못 켜도 수집은 계속 (화면에 표시)
            logging.warning(f"서보 상태 감시를 켜지 못했습니다: {e}")
            guard = None
        obs_fn = robot.get_observation

        def guarded_obs():
            o = obs_fn()
            if guard is not None:
                why = guard.poll()
                if why:
                    # 녹화 중이던 에피소드는 버리고 수집을 끝냅니다 ('수집 끝내기' 와 같은 경로) — 이상 구간을 남기지 않음
                    logging.warning(f"서보 상태 이상으로 수집을 멈춥니다: {why}")
                    status["health_stop"] = why
                    on_key("q")
                hs = guard.state()
                status["health"] = hs
                if hs["temp"]:
                    status["temp"], status["maxtemp"] = hs["temp"], hs["maxtemp"]
            return o

        robot.get_observation = guarded_obs

        def idle(last=""):
            """대기(READY). 기록하지 않고 리더 팔로우만 유지합니다.
            dataset=None 이면 record_loop 은 add_frame 을 건너뜁니다."""
            ui["start"] = False
            events["rerecord_episode"] = False    # 대기 중에 누른 '버리고 다시' 가 다음 에피소드를 버리지 않게
            put(phase="ready", t0=None, phase_len=0, episode=dataset.num_episodes, last=last)
            while not ui["start"] and not events["stop_recording"]:
                record_loop(robot=robot, events=events, fps=fps,
                            teleop_action_processor=tap, robot_action_processor=rap,
                            robot_observation_processor=rop, teleop=teleop,
                            control_time_s=READY_CHUNK_S, single_task=task)
                put()
            events["exit_early"] = False
            events["rerecord_episode"] = False
            ui["start"] = False

        with VideoEncodingManager(dataset):
            recorded = 0
            last = ""
            while not events["stop_recording"]:
                idle(last)                       # 사람이 '녹화 시작' 을 누를 때까지 기다립니다
                if events["stop_recording"]:
                    break
                put(phase="record", episode=dataset.num_episodes, recorded=recorded,
                    t0=time.time(), phase_len=ept, last="")
                logging.info(f"Recording episode {dataset.num_episodes}")
                record_loop(robot=robot, events=events, fps=fps,
                            teleop_action_processor=tap, robot_action_processor=rap,
                            robot_observation_processor=rop, teleop=teleop, dataset=dataset,
                            control_time_s=ept, single_task=task)
                # 녹화 중 '수집 끝내기' → 진행 중이던 에피소드는 버립니다.
                # 살리고 싶으면 '저장하고 다음' 을 먼저 누르면 됩니다.
                if events["rerecord_episode"] or events["stop_recording"]:
                    logging.info("Discard episode")
                    events["rerecord_episode"] = False
                    events["exit_early"] = False
                    dataset.clear_episode_buffer()
                    last = "버림"
                    continue
                # 시작 직후 바로 '저장하고 다음' → 프레임 0개. save_episode 가 예외를 내므로 건너뜁니다
                if not dataset.has_pending_frames():
                    dataset.clear_episode_buffer()
                    last = "빈 에피소드 — 저장 안 함"
                    continue
                put(phase="saving", t0=None)
                dataset.save_episode()
                recorded += 1
                last = f"episode {dataset.num_episodes - 1} 저장"
                put(recorded=recorded)
    except Exception as e:
        logging.exception("record worker failed")
        put(phase="error", err=f"{type(e).__name__}: {e}")
        rc = 1
    finally:
        alive["on"] = False
        # 팔부터 놓습니다 — finalize 가 길어지는 동안 두 번째 '중지'(SIGKILL)가 와도 토크가 남지 않게.
        # 양팔에서 한쪽만 연결된 채 실패해도 is_connected 가 False 라 disconnect() 를 건너뛰므로 팔별로 닫습니다.
        if robot is not None:
            for d in ([robot.left_arm, robot.right_arm] if hasattr(robot, "left_arm") else [robot]):
                close_arm(d)
        if teleop is not None:
            for d in ([teleop.left_arm, teleop.right_arm] if hasattr(teleop, "left_arm") else [teleop]):
                close_arm(d, disable_torque=kind(spec)["leader_torque"])   # OMX 리더는 그리퍼 토크를 끔
        if preview is not None:
            preview.stop()
        put(phase="finalizing", t0=None)
        if dataset is not None:
            try:
                dataset.finalize()
            except Exception as e:
                logging.exception("finalize failed")
                put(err=f"finalize: {e}")
        put(phase="error" if rc else "done")
    return rc


def worker_main(kind, jid):
    if not safe_name(jid):
        print("bad jid", file=sys.stderr)
        return 2
    if kind == "record":
        return worker_record(jid)
    print(f"unknown worker kind: {kind}", file=sys.stderr)
    return 2


COLLECT_RUN_HTML = """
<style>
.rec{display:grid;grid-template-columns:1fr;gap:14px}
.phase{font-family:var(--mono);font-size:26px;font-weight:600;letter-spacing:.04em}
.phase.record{color:var(--bad)} .phase.ready{color:var(--warn)} .phase.saving{color:var(--accent)}
.pbar{height:10px;background:var(--surface2);border-radius:5px;overflow:hidden;margin:8px 0 4px}
.pbar i{display:block;height:100%;background:var(--accent);transition:width .2s linear}
.pbar.record i{background:var(--bad)} .pbar.reset i{background:var(--warn)}
.cams{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px}
.cams .cw{position:relative;background:#000;border-radius:8px;overflow:hidden}
.cams img{width:100%;display:block;aspect-ratio:4/3;object-fit:contain;background:#000}
.cams .cl{position:absolute;top:6px;left:10px;font-family:var(--mono);font-size:11px;
  letter-spacing:.1em;text-transform:uppercase;color:#cfd8e3;text-shadow:0 0 4px #000}
.bigkeys{display:flex;gap:12px;flex-wrap:wrap}
.bigkeys button{flex:1;min-width:150px;padding:18px 10px;font-size:17px}
.bigkeys button.go{flex:2;background:var(--accent-dim);border-color:var(--accent);color:#dceafe}
.tchip{font-family:var(--mono);font-size:12px;padding:2px 7px;border-radius:5px;
  background:var(--surface2);border:1px solid var(--line)}
.tchip.warn{color:var(--warn);border-color:var(--warn)}
.tchip.hot{color:var(--bad);border-color:var(--bad)}
.statline{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--mono);font-size:13px;color:var(--muted)}
.statline b{color:var(--text)}
</style>
<div class=wrap>
<p class=eyebrow>녹화</p><h2>수집 진행 중</h2>
<div class=runbar><span class="badge b-run" id=rbadge>session</span>
  <span class=mono id=jid></span>
  <span class=mono id=repo style="color:var(--muted)"></span>
  <button id=mute onclick="toggleMute()" style="margin-left:auto" title="녹화 시작·저장·마지막 3초 신호음">소리 켜짐</button>
  <button class=danger onclick="stopRec()"
          title="워커 프로세스를 죽입니다. 정상 종료는 아래 '수집 끝내기'">강제 종료</button></div>
<div class=rec>
  <div class=card>
    <div class=phase id=phase>…</div>
    <div class=pbar id=pbar><i id=pfill style="width:0%"></i></div>
    <div class=statline>
      <span>다음 에피소드 <b id=ep>-</b></span>
      <span>저장됨 <b id=rec>0</b> / 목표 <b id=nep>-</b></span>
      <span>경과 <b id=el>0.0</b>s / <b id=plen>-</b>s</span>
      <span id=lastbox style="display:none">직전 <b id=last></b></span>
    </div>
    <div class=statline id=temps style="margin-top:6px"></div>
    <p id=health class=badge style="display:none;margin-top:8px"></p>
    <p id=err class="badge b-bad" style="display:none;margin-top:10px"></p>
  </div>
  <div class=cams id=cams></div>
  <div class=bigkeys id=keys_ready style="display:none">
    <button class=go onclick="key('s')">&#9679;&nbsp; 녹화 시작 <span class=muted>(Space · s)</span></button>
    <button class=danger onclick="endRec()">&#9632;&nbsp; 수집 끝내기 <span class=muted>(Esc)</span></button>
  </div>
  <div class=bigkeys id=keys_rec style="display:none">
    <button class=go onclick="key('n')">&#10003;&nbsp; 저장하고 다음 <span class=muted>(Space · n)</span></button>
    <button onclick="key('r')">&#10007;&nbsp; 버리고 다시 <span class=muted>(&larr; · r)</span></button>
  </div>
  <p class=muted id=hint></p>
  <p class=eyebrow>로그</p><pre id=tail>...</pre>
</div></div>
<script>
const $=id=>document.getElementById(id);
$('jid').textContent=JID;
let camsBuilt=false, PH='', lastBeep=-1;
async function key(k){ await fetch('/api/sendkey/'+JID+'/'+k,{method:'POST'}); }
/* 신호음 — 화면을 안 보고 팔을 움직이는 동안에도 단계를 알 수 있게 (WebAudio, 파일 없음) */
let AC=null, MUTE=false;
try{ MUTE=localStorage.getItem('openmimic_mute')==='1'; }catch(e){}
function paintMute(){ $('mute').textContent=MUTE?'소리 꺼짐':'소리 켜짐'; }
function toggleMute(){ MUTE=!MUTE; try{ localStorage.setItem('openmimic_mute',MUTE?'1':'0'); }catch(e){} paintMute(); if(!MUTE) tone([880],0.08); }
function tone(freqs, dur){
  if(MUTE) return;
  try{
    AC=AC||new (window.AudioContext||window.webkitAudioContext)();
    if(AC.state==='suspended') AC.resume();
    let t=AC.currentTime;
    freqs.forEach(f=>{ const o=AC.createOscillator(), g=AC.createGain(); o.frequency.value=f; o.type='sine';
      g.gain.setValueAtTime(0.0001,t); g.gain.exponentialRampToValueAtTime(0.25,t+0.01); g.gain.exponentialRampToValueAtTime(0.0001,t+dur);
      o.connect(g); g.connect(AC.destination); o.start(t); o.stop(t+dur+0.02); t+=dur; });
  }catch(e){}
}
document.addEventListener('click',()=>{ if(AC&&AC.state==='suspended') AC.resume(); });   // 브라우저 자동재생 정책
async function endRec(){
  if(!confirm('수집을 끝낼까요? 지금까지 저장된 에피소드는 그대로 남습니다.'))return;
  await key('q');
}
async function stopRec(){
  const L=[
   '워커를 SIGKILL 로 즉시 죽입니다.','',
   '• 녹화 중이던 에피소드와 아직 인코딩되지 않은 영상이 사라집니다.',
   '• 메타가 마무리되지 않아 데이터셋이 안 열릴 수 있습니다 — 데이터셋 탭의 복구로 살릴 수 있습니다.',
   '• 팔 토크가 켜진 채 남습니다 — 제어 탭에서 토크 OFF 하거나 전원을 내리세요.','',
   '응답 없는 워커를 끊을 때만 쓰세요.',
   '정상 종료는 대기 상태의 [수집 끝내기] 입니다.','',
   '그래도 강제 종료할까요?'];
  if(!confirm(L.join('\\n')))return;
  const d = await (await fetch('/api/kill/'+JID+'?force=1',{method:'POST'})).json();
  if(!d.ok) alert('종료 실패 — 이미 죽었거나 pid 를 못 찾았습니다');
  setTimeout(()=>location.reload(),1500);
}
function buildCams(names){
  const box=$('cams'); box.innerHTML='';
  names.forEach(n=>{
    const d=document.createElement('div'); d.className='cw';
    d.innerHTML='<span class=cl></span><img>';
    d.querySelector('.cl').textContent=n;
    d.querySelector('img').src='/stream/'+encodeURIComponent(n);
    box.appendChild(d);
  });
  camsBuilt=names.length>0;
}
/* 폴링이 실패하면 조용히 죽지 않고 무엇이 막혔는지 화면에 적고 다음 주기에 다시 시도합니다. */
let failN=0, polling=false;
function paint(d){
  const s=d.status||{};
  const ph=s.phase||'starting';
  const label={starting:'준비 중…',importing:'lerobot 불러오는 중…',devices:'팔 객체 만드는 중…',
               dataset:'데이터셋 여는 중…',connecting:'팔·카메라 연결 중…',
               ready:'대기 — 녹화 시작을 누르세요',
               record:'● RECORD',saving:'저장 중…',finalizing:'마무리 중…',done:'완료',error:'오류'}[ph]||ph;
  const PREP={starting:1,importing:1,devices:1,dataset:1,connecting:1};
  $('phase').textContent=label; $('phase').className='phase '+ph;
  $('pbar').className='pbar '+ph;
  const pct = (s.phase_len&&s.elapsed!=null)? Math.min(100, s.elapsed/s.phase_len*100)
            : PREP[ph] ? 0 : (ph==='record'?0:100);
  $('pfill').style.width=pct+'%';
  $('ep').textContent = s.episode==null?'-':s.episode;
  $('nep').textContent = s.num_episodes==null?'-':s.num_episodes;
  $('rec').textContent = s.recorded==null?'0':s.recorded;
  $('el').textContent = s.elapsed==null?'0.0':Number(s.elapsed).toFixed(1);
  $('plen').textContent = s.phase_len||'-';
  $('repo').textContent = s.repo_id||'';
  $('rbadge').textContent = ph==='record' ? 'recording' : ph;
  $('rbadge').className = 'badge '+(ph==='record'?'b-bad':ph==='ready'?'b-warn':'b-run');
  $('keys_ready').style.display = ph==='ready' ? '' : 'none';
  $('keys_rec').style.display   = ph==='record' ? '' : 'none';
  if(PREP[ph]){
    const el = s.elapsed||0;
    $('hint').innerHTML = (ph==='importing'
        ? '첫 실행은 torch 까지 끌어오느라 <b>수십 초</b> 걸립니다. 멈춘 게 아닙니다.<br>'
        : ph==='connecting'
        ? '시리얼 포트와 카메라를 엽니다. 여기서 오래 걸리면 <b>카메라가 다른 프로그램에 잡혀 있거나</b> 포트 경로가 바뀐 것입니다.<br>'
        : '')
      + '경과 ' + el.toFixed(0) + '초'
      + (el > 90 ? ' — 아래 로그를 보세요. 안 풀리면 오른쪽 위 <b>강제 종료</b>.' : '');
  } else {
    $('hint').textContent = ph==='ready'
      ? '기록하지 않습니다. 팔은 리더를 계속 따라가니 물체와 자세를 제자리에 놓고, 준비되면 녹화 시작을 누르세요.'
      : ph==='record'
      ? '기록 중입니다. 에피소드 최대(초)가 지나면 자동으로 저장됩니다.'
      : '';
  }
  if(ph!==PH){
    if(ph==='record') tone([660,880],0.12);          // 녹화 시작 ↑
    else if(PH==='record' && ph==='saving') tone([660,440],0.12);   // 저장 ↓
    PH=ph; lastBeep=-1;
  }
  if(ph==='record' && s.phase_len && s.elapsed!=null){
    const left=Math.ceil(s.phase_len-s.elapsed);
    if(left<=3 && left>=1 && left!==lastBeep){ lastBeep=left; tone([880],0.07); }   // 마지막 3초
  }
  if(s.last){ $('lastbox').style.display=''; $('last').textContent=s.last; }
  else { $('lastbox').style.display='none'; }
  const tb=$('temps'), t=s.temp||{};
  const names=Object.keys(t);
  tb.innerHTML = names.length
    ? '<span>모터 온도</span>'+names.map(n=>{
        const v=t[n], c = v>=65?'hot':v>=55?'warn':'';
        return '<span class="tchip '+c+'">'+n+' '+v+'&deg;</span>';
      }).join('') + (s.health&&s.health.minvolt!=null?'<span>최저 전압 '+s.health.minvolt+' V</span>':'')
    : '';
  const hs=s.health, hb=$('health');
  if(s.health_stop){ hb.style.display=''; hb.className='badge b-bad';
    hb.textContent='서보 상태 이상으로 수집을 멈췄습니다 (녹화 중이던 에피소드는 버림): '+s.health_stop; }
  else if(hs&&hs.msgs&&hs.msgs.length){ hb.style.display=''; hb.className='badge '+(hs.level==='stop'?'b-bad':'b-warn');
    hb.textContent='서보 상태: '+hs.msgs.join(' · '); }
  else hb.style.display='none';
  if(s.err){ $('err').style.display=''; $('err').textContent=s.err; } else { $('err').style.display='none'; }
  if(!camsBuilt && s.cams && s.cams.length) buildCams(s.cams);
  $('tail').textContent=d.tail||'';
  if(!d.alive){ setTimeout(()=>location.reload(),1200); }
}
async function refresh(){
  if(polling) return;                 /* 느려도 요청이 쌓이지 않게 */
  polling=true;
  let d;
  try{
    const ac=new AbortController();
    const to=setTimeout(()=>ac.abort(), 5000);
    const r=await fetch('/api/record_status/'+JID,{signal:ac.signal,cache:'no-store'});
    clearTimeout(to);
    if(!r.ok) throw new Error('HTTP '+r.status);
    d=await r.json();
  }catch(e){
    failN++;
    $('phase').textContent='서버 응답 없음';
    $('phase').className='phase error';
    $('err').style.display='';
    $('err').textContent='OpenMimic 서버가 응답하지 않습니다 ('+failN+'회) — '
      +(e.name==='AbortError'?'5초 초과':String(e&&e.message||e))
      +'. 터미널에서 openmimic.log 를 확인하세요.';
    polling=false; return;
  }
  failN=0;
  try{ paint(d); }
  catch(e){
    $('err').style.display='';
    $('err').textContent='화면 갱신 오류: '+(e&&e.message||e);
    console.error(e);
  }
  finally{ polling=false; }
}
refresh(); setInterval(refresh,500);
paintMute();
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'||e.target.tagName==='TEXTAREA')return;
  if(e.ctrlKey||e.metaKey||e.altKey||e.repeat)return;      // Ctrl+R(새로고침) 이 '버리고 다시' 로 가지 않게
  if(AC&&AC.state==='suspended') AC.resume();
  if(['s','n','r'].includes(e.key)){ key(e.key); return; } // 단계에 맞지 않는 키는 워커가 무시합니다
  // 큰 키 한 벌: Space/→ = 다음 단계(대기→녹화, 녹화→저장), ←/Backspace = 버리고 다시, Esc = 수집 끝내기
  if(e.key===' '||e.key==='ArrowRight'){ e.preventDefault(); if(PH==='ready') key('s'); else if(PH==='record') key('n'); }
  else if(e.key==='ArrowLeft'||e.key==='Backspace'){ e.preventDefault(); if(PH==='record') key('r'); }
  else if(e.key==='Escape'){ if(PH==='ready') endRec(); }
});
</script>"""



# ----------------------------- 페이지: Jobs ----------------------------------
@app.get("/jobs", response_class=HTMLResponse)
def jobs_page():
    rows = ""
    for j in jobs_index():
        if j["alive"]:
            st = '<span class="badge b-run">running</span>'
            act = f'<button class=danger onclick="kill({jsattr(j["id"])})">중지</button>'
        else:
            st = '<span class="badge b-ok">done</span>'
            act = f'<button onclick="delJob({jsattr(j["id"])})">삭제</button>'
        rows += (f'<tr><td class=mono>{esc(j["id"])}</td><td>{esc(j["kind"])}</td><td>{st}</td>'
                 f'<td class=mono style="color:var(--muted)">{esc(j.get("started", ""))}</td>'
                 f'<td style="text-align:right"><a href="/jobs/{esc(j["id"])}">log</a> &nbsp;{act}</td></tr>')
    empty = '' if rows else '<tr><td colspan=5 class=muted>작업 기록 없음</td></tr>'
    return f"""{CSS}{nav_html('jb')}<div class=wrap>
    <p class=eyebrow>백그라운드 작업</p><h2>작업</h2>
    <div class=card><table>
    <tr><th>ID</th><th>종류</th><th>상태</th><th>시작</th><th></th></tr>{rows}{empty}</table></div>
    <p class=muted>중지 1회 = 정상 종료(SIGINT) · 한 번 더 = 강제종료 · 삭제 = 기록·로그 제거 (끝난 작업만)</p></div>
    <script>
    async function kill(id){{ await fetch('/api/kill/'+id,{{method:'POST'}}); setTimeout(()=>location.reload(),1500); }}
    async function delJob(id){{ await fetch('/api/deljob/'+id,{{method:'POST'}}); location.reload(); }}
    setTimeout(()=>location.reload(), 10000);
    </script>"""


@app.get("/jobs/{jid}", response_class=HTMLResponse)
def job_log(jid: str):
    if not safe_name(jid):
        return HTMLResponse(f"{CSS}{nav_html('jb')}<div class=wrap>잘못된 작업 id</div>", 400)
    j = load_json(JOB_DIR / f"{jid}.json", {})
    txt = ""
    try:
        txt = Path(j["log"]).read_text(errors="ignore")[-8000:]
    except Exception:
        pass
    return f"""{CSS}{nav_html('jb')}<div class=wrap>
    <p class=eyebrow>작업 로그</p><h2 class=mono style="font-size:17px">{esc(jid)}</h2>
    <pre style="max-height:none">{esc(txt) or '(로그 없음)'}</pre>
    <p class="muted mono">cmd: {esc(j.get('cmd', ''))}</p>
    <script>setTimeout(()=>location.reload(),5000)</script></div>"""


@app.post("/api/kill/{jid}")
def api_kill(jid: str, force: int = 0):
    ok = kill_job(jid, force=bool(force))
    return {"ok": ok, "signal": "SIGKILL" if force else "SIGINT"}


@app.post("/api/deljob/{jid}")
def api_deljob(jid: str):
    ok = delete_job(jid)
    return {"ok": ok} if ok else JSONResponse({"error": "실행 중인 작업"}, status_code=400)


# ----------------------------- 비디오 서빙 -----------------------------------
@app.get("/videos/{ds}/{rest:path}")
def serve_video(ds: str, rest: str):
    if not safe_name(ds):
        return JSONResponse({"error": "not found"}, status_code=404)
    p = (DATA_ROOT / ds / "videos" / rest).resolve()
    if not _within(p, DATA_ROOT / ds / "videos") or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(p, media_type="video/mp4")


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        sys.exit(worker_main(sys.argv[2], sys.argv[3]))
    print(f"data : {DATA_ROOT}\nouts : {OUT_ROOT}\njobs : {JOB_DIR}\nrun  : {RUN_DIR}")
    print(f"conf : {CONFIG_FILE}  (mode={CFG['mode']}, arms={SIDES}, cams={list(CAM_SPECS)})")
    for u in lan_urls(PORT):
        print(f"open : {u}?token={AUTH_TOKEN}   (token: {TOKEN_FILE})" if AUTH_TOKEN
              else f"open : {u}   (인증 없음 — 켜려면 OPENMIMIC_AUTH=on)")
    print(f"calib: {CALIB_ROOT}")
    if calib_root_warning():
        print("!! " + calib_root_warning())
    import importlib.util
    if not any(importlib.util.find_spec(m) for m in ("websockets", "wsproto")):
        # uvicorn 은 WebSocket 라이브러리가 없으면 경고만 하고 /ws/* 를 404 로 거부합니다 → 제어 탭이 연결되지 않음
        print("!! WebSocket 라이브러리가 없습니다 — 제어 탭이 동작하지 않습니다. "
              "openmimic 환경에서: pip install \"websockets>=12\" 후 다시 실행하세요")
    uvicorn.run(app, host="0.0.0.0", port=PORT)
