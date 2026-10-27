#!/usr/bin/env python3
"""가상 팔 — 실제 팔 없이 OpenMimic 전체 흐름(셋업 마법사·Calib·Control·Collect)을 시험하는 도구.

    python tools_simarms.py --robot so101 --mode bimanual     # SO-ARM101 양팔 (보드 4개)
    python tools_simarms.py --robot omx   --mode single       # OMX 한팔 (보드 2개)

켜 두는 동안 OpenMimic 의 포트 목록에 '가상 …' 보드로 나타나고, 끄면 사라집니다 (OpenMimic 재시작 불필요).
실제 팔과는 아무 관계가 없습니다 — 실제 팔을 쓸 때는 이 도구를 켜지 않으면 됩니다. Linux · Windows 공통.

동작
  - 모터는 실제 프로토콜을 그대로 말합니다: SO-ARM101 = Feetech STS3215, OMX = Dynamixel X (Protocol 2.0).
    lerobot 버스가 진짜 서보와 통신하듯 ping·read·write·sync read/write 를 주고받습니다.
  - 포트 이름은 sim://<side>_<role> (예: sim://main_follower) 입니다. 껐다 켜도 같은 이름이라 다시 지정할 필요가 없습니다.
    OpenMimic 쪽 프로세스는 openmimic_fakeserial 이 serial.Serial 대신 이 도구의 보드에 127.0.0.1 소켓으로 붙습니다
    (보드·물리 상태는 이 프로세스 하나에 있어 서버·수집/실행 worker 가 같은 팔을 봅니다). PTY·드라이버가 필요 없습니다.
  - 모터 ID·모델은 lerobot 정의 그대로 (SO: ID 1~6, OMX 팔로워 11~16 / 리더 1~6).
  - 처음에는 공장 상태(캘리브레이션 없음)입니다. 전압은 팔로워 12 V, 리더 5 V (SO) 로 보고합니다.
  - Homing_Offset 을 실제처럼 반영합니다 (Feetech: Present = Actual - Homing, Dynamixel: Present = Actual + Homing).
  - 토크가 켜지면 목표 위치로 천천히 움직입니다.
  - 손으로 움직이는 대신 콘솔 명령을 씁니다 (OpenMimic 화면의 '가상 장치' 막대도 같은 명령을 openmimic_sim/cmd 로 보냅니다):
        w <번호>          그 보드의 관절을 양 끝까지 한 번 쓸기 (포트 찾기·캘리브레이션·확인 단계용)
        a [on|off]        리더를 계속 천천히 움직이기 켜기/끄기 (제어 리더 팔로우·수집 용)
        v <번호> <V>      그 보드의 전압을 바꿔 보고 (어댑터 혼용·전원 강하 흉내 — 서보 상태 감시 시험용)
        t <번호> <°C>     그 보드의 온도를 바꿔 보고 (과열 흉내)
        l                 보드 목록
        q                 끝내기
  - 가상 카메라도 함께 만듭니다 (openmimic_sim/cam_<이름>). 영상은 openmimic_simcam 이 합성합니다.
  - 물리 시뮬레이션 (--physics mujoco, SO-ARM101 한팔에서 mujoco 가 있으면 기본):
    팔로워가 MuJoCo 안의 SO-101 이 되고, 카메라(top · wrist)가 책상·큐브·목표 칸을 렌더링합니다 (openmimic_mujoco).
        demo     리더가 '큐브 집어 초록 칸에 놓기' 시연을 한 번 (약 14초) — 지금 큐브 위치에서
        demo new 큐브를 새 위치에 놓고 시연
        reset    큐브만 새 위치에 다시 놓기
  - 모터 ID 세팅(보드레이트·ID 변경)은 흉내내지 않습니다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np

import openmimic_platform as P
from openmimic_fakeserial import PREFIX, SimPortServer

ROOT = Path(__file__).resolve().parent
SIM_DIR = ROOT / "openmimic_sim"
SIM_FILE = ROOT / "openmimic_sim.json"
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")




# ============================================================================ Feetech (STS3215)
def _fe_chk(b):
    return (~sum(b)) & 0xFF


class FeetechServo:
    """STS3215 레지스터 일부. 주소는 lerobot motors/feetech/tables.py 와 같습니다."""

    def __init__(self, id_, volt):
        self.id = id_
        self.mem = bytearray(128)
        self.actual = 2048.0              # 실제 엔코더 위치 (Homing 적용 전)
        self.w(3, 777, 2)                 # Model_Number
        self.w(0, 3, 1); self.w(1, 10, 1)
        self.w(5, id_, 1)
        self.w(9, 0, 2); self.w(11, 4095, 2)   # 공장값: 위치 제한 없음
        self.w(13, 70, 1)
        self.w(31, 0, 2)                  # Homing_Offset 0 (부호-크기, bit11)
        self.w(62, volt, 1)               # Present_Voltage (0.1 V)
        self.w(63, 32, 1)                 # 온도
        self.goal_actual = None

    def w(self, a, v, n):
        for i in range(n):
            self.mem[a + i] = (int(v) >> (8 * i)) & 0xFF

    def r(self, a, n):
        return sum(self.mem[a + i] << (8 * i) for i in range(n))

    def homing(self):
        v = self.r(31, 2)
        return -(v & 0x7FF) if v & 0x800 else (v & 0x7FF)

    def sync_present(self):
        self.w(56, int(round(self.actual - self.homing())) % 4096, 2)

    def on_write(self, a):
        if a <= 42 < a + 2 or a == 42:       # Goal_Position (Present 좌표계) → 실제 위치로
            self.goal_actual = (self.r(42, 2) + self.homing()) % 4096
        if a <= 40 < a + 1 and self.mem[40] and self.goal_actual is None:
            self.goal_actual = (self.r(42, 2) + self.homing()) % 4096

    def tick(self, dt):
        if self.mem[40] and self.goal_actual is not None:      # 토크 ON → 목표로 이동
            d = self.goal_actual - self.actual
            if d > 2048: d -= 4096
            if d < -2048: d += 4096
            self.actual = (self.actual + max(-1200 * dt, min(1200 * dt, d))) % 4096
        self.sync_present()


class FeetechBoard:
    def __init__(self, ids, volt):
        self.servos = {i: FeetechServo(i, volt) for i in ids}

    def handle(self, pkt):
        id_, instr, p = pkt[2], pkt[4], pkt[5:-1]
        out = []
        S = self.servos
        if instr == 0x01:                 # PING
            for s in (S.values() if id_ == 0xFE else [S[id_]] if id_ in S else []):
                out.append(self._reply(s))
        elif instr == 0x02 and id_ in S:  # READ
            s = S[id_]
            out.append(self._reply(s, bytes(s.mem[p[0]:p[0] + p[1]])))
        elif instr == 0x03 and id_ in S:  # WRITE
            s = S[id_]
            s.mem[p[0]:p[0] + len(p) - 1] = p[1:]
            s.on_write(p[0])
            out.append(self._reply(s))
        elif instr == 0x82:               # SYNC READ
            a, n = p[0], p[1]
            for i in p[2:]:
                if i in S:
                    out.append(self._reply(S[i], bytes(S[i].mem[a:a + n])))
        elif instr == 0x83:               # SYNC WRITE (응답 없음)
            a, n, rest = p[0], p[1], p[2:]
            for k in range(0, len(rest), n + 1):
                i = rest[k]
                if i in S:
                    S[i].mem[a:a + n] = rest[k + 1:k + 1 + n]
                    S[i].on_write(a)
        return out

    @staticmethod
    def _reply(s, data=b""):
        body = bytes([s.id, len(data) + 2, 0]) + data
        return b"\xff\xff" + body + bytes([_fe_chk(body)])

    @staticmethod
    def split(buf):
        pkts = []
        while True:
            i = buf.find(b"\xff\xff")
            if i < 0 or len(buf) < i + 4:
                break
            end = i + 4 + buf[i + 3]
            if len(buf) < end:
                break
            pkt, buf = buf[i:end], buf[end:]
            if _fe_chk(pkt[2:-1]) == pkt[-1]:
                pkts.append(pkt)
        return pkts, buf


# ============================================================================ Dynamixel X (Protocol 2.0)
_CRC = []
for _i in range(256):
    _c = _i << 8
    for _ in range(8):
        _c = ((_c << 1) ^ 0x8005) if _c & 0x8000 else (_c << 1)
    _CRC.append(_c & 0xFFFF)


def _crc16(data):
    crc = 0
    for b in data:
        crc = ((crc << 8) ^ _CRC[((crc >> 8) ^ b) & 0xFF]) & 0xFFFF
    return crc


def _stuff(b):
    out = bytearray()
    for i, x in enumerate(b):
        out.append(x)
        if i >= 2 and b[i - 2:i + 1] == b"\xff\xff\xfd":
            out.append(0xFD)
    return bytes(out)


def _unstuff(b):
    out, i = bytearray(), 0
    while i < len(b):
        out.append(b[i])
        if len(out) >= 3 and out[-3:] == b"\xff\xff\xfd" and i + 1 < len(b) and b[i + 1] == 0xFD:
            i += 1
        i += 1
    return bytes(out)


DXL_MODEL = {"xl430-w250": 1060, "xl330-m288": 1200, "xl330-m077": 1190}


class DxlServo:
    """Dynamixel X 레지스터 일부. 주소는 lerobot motors/dynamixel/tables.py 와 같습니다."""

    def __init__(self, id_, model, volt):
        self.id = id_
        self.mem = bytearray(256)
        self.actual = 2048.0
        self.goal_actual = None
        self.w(0, DXL_MODEL[model], 2)
        self.w(6, 52, 1)
        self.w(7, id_, 1)
        self.w(11, 3, 1)
        self.w(48, 4095, 4); self.w(52, 0, 4)
        self.w(144, volt, 2)
        self.w(146, 32, 1)
        self.w(31, 70, 1)                 # Temperature_Limit — 가상 값 (실제 모델별 공장값은 e-Manual 확인)

    def w(self, a, v, n):
        v = int(v) & ((1 << (8 * n)) - 1)
        for i in range(n):
            self.mem[a + i] = (v >> (8 * i)) & 0xFF

    def r(self, a, n, signed=False):
        v = sum(self.mem[a + i] << (8 * i) for i in range(n))
        if signed and v >= 1 << (8 * n - 1):
            v -= 1 << (8 * n)
        return v

    def homing(self):
        return self.r(20, 4, signed=True)

    def sync_present(self):
        self.w(132, int(round(self.actual + self.homing())), 4)

    def on_write(self, a, n):
        if a <= 116 < a + n:
            self.goal_actual = self.r(116, 4, signed=True) - self.homing()
        if a <= 64 < a + n and self.mem[64] and self.goal_actual is None:
            self.goal_actual = self.r(116, 4, signed=True) - self.homing()

    def tick(self, dt):
        if self.mem[64] and self.goal_actual is not None:
            d = self.goal_actual - self.actual
            self.actual += max(-1200 * dt, min(1200 * dt, d))
        self.sync_present()


class DxlBoard:
    def __init__(self, spec, volt):
        self.servos = {i: DxlServo(i, m, volt) for i, m in spec}

    def handle(self, pkt):
        id_ = pkt[4]
        body = _unstuff(pkt[7:-2])
        inst, p = body[0], body[1:]
        S, out = self.servos, []
        u16 = lambda b: int.from_bytes(b, "little")
        if inst == 0x01:
            for s in (S.values() if id_ == 0xFE else [S[id_]] if id_ in S else []):
                out.append(self._reply(s, bytes(s.mem[0:2]) + bytes([s.mem[6]])))
        elif inst == 0x02 and id_ in S:
            a, n = u16(p[0:2]), u16(p[2:4])
            out.append(self._reply(S[id_], bytes(S[id_].mem[a:a + n])))
        elif inst == 0x03 and id_ in S:
            a, data = u16(p[0:2]), p[2:]
            S[id_].mem[a:a + len(data)] = data
            S[id_].on_write(a, len(data))
            out.append(self._reply(S[id_]))
        elif inst == 0x82:
            a, n = u16(p[0:2]), u16(p[2:4])
            for i in p[4:]:
                if i in S:
                    out.append(self._reply(S[i], bytes(S[i].mem[a:a + n])))
        elif inst == 0x83:
            a, n, rest = u16(p[0:2]), u16(p[2:4]), p[4:]
            for k in range(0, len(rest), n + 1):
                i = rest[k]
                if i in S:
                    S[i].mem[a:a + n] = rest[k + 1:k + 1 + n]
                    S[i].on_write(a, n)
        return out

    @staticmethod
    def _reply(s, data=b""):
        body = bytes([s.id]) + (len(data) + 4).to_bytes(2, "little") + bytes([0x55, 0]) + data
        pkt = b"\xff\xff\xfd\x00" + _stuff(body)
        return pkt + _crc16(pkt).to_bytes(2, "little")

    @staticmethod
    def split(buf):
        pkts = []
        while True:
            i = buf.find(b"\xff\xff\xfd\x00")
            if i < 0 or len(buf) < i + 7:
                break
            end = i + 7 + int.from_bytes(buf[i + 5:i + 7], "little")
            if len(buf) < end:
                break
            pkt, buf = buf[i:end], buf[end:]
            if _crc16(pkt[:-2]) == int.from_bytes(pkt[-2:], "little"):
                pkts.append(pkt)
        return pkts, buf


# ============================================================================ 보드 = 가상 포트 하나 (sim://<이름>)
OMX_SPEC = {
    "follower": [(11, "xl430-w250"), (12, "xl430-w250"), (13, "xl430-w250"),
                 (14, "xl330-m288"), (15, "xl330-m288"), (16, "xl330-m288")],
    "leader": [(1, "xl330-m288"), (2, "xl330-m288"), (3, "xl330-m288"),
               (4, "xl330-m288"), (5, "xl330-m288"), (6, "xl330-m077")],
}


class SimArm:
    def __init__(self, robot, side, role):
        self.robot, self.side, self.role = robot, side, role
        self.name = f"{side}_{role}"
        volt = 120 if role == "follower" else (50 if robot == "so101" else 50)
        if robot == "so101":
            self.board = FeetechBoard(range(1, 7), volt)
        else:
            self.board = DxlBoard(OMX_SPEC[role], volt)
        self.port = PREFIX + self.name            # 설정에 저장되는 포트 이름 — 껐다 켜도 같음
        self.wiggle_until = 0.0
        self.wiggle_t0 = 0.0
        self.lock = threading.Lock()              # 보드 레지스터 — 시리얼 응답과 서보 갱신이 함께 씀
        self.world = None                         # 물리 시뮬레이션이면 팔로워의 몸 (openmimic_mujoco.World)
        self.ranges = None                        # (lo, hi) 틱 — 쓸기 범위. 없으면 2048 ± 900
        self.demo = None                          # (경유점, 시작 시각) — 리더 자동 시연

    def serve(self, buf):
        """가상 포트로 들어온 바이트(buf, 연결마다 따로)에서 완성된 패킷을 꺼내 응답들을 돌려줍니다.
        보드 잠금만 잡습니다 — 물리 계산·렌더를 기다리지 않아야 lerobot 의 응답 제한 시간(수십 ms) 안에 답합니다."""
        pkts, rest = self.board.split(bytes(buf))
        del buf[:len(buf) - len(rest)]
        out = []
        for pkt in pkts:
            with self.lock:
                out.extend(self.board.handle(pkt))
        return out

    def _wig(self, k, ph):
        if self.ranges is None:
            return 2048 + 900 * math.sin(2 * math.pi * ph)
        lo, hi = self.ranges[0][k], self.ranges[1][k]
        return (lo + hi) / 2 + (hi - lo) / 2 * math.sin(2 * math.pi * ph)

    def tick(self, now, dt, auto):
        wig = now < self.wiggle_until
        ph = (now - self.wiggle_t0) / max(1e-6, self.wiggle_until - self.wiggle_t0)
        if self.world is not None:
            # 물리: 토크가 모두 꺼져 있으면 '손으로' 움직인 것처럼 관절을 놓고, 아니면 목표를 액추에이터로.
            # 보드 잠금 안에서는 레지스터만 읽고, MuJoCo 호출(월드 잠금)은 보드 잠금 밖에서 합니다 —
            # 겹쳐 잡으면 시연 계획·렌더 동안 시리얼 응답이 멈춰 lerobot 이 'no status packet' 으로 끊깁니다.
            with self.lock:
                servos = list(self.board.servos.values())
                torque = [bool(s.mem[40]) for s in servos]
                goals = [s.goal_actual for s in servos]
                tele = [self._wig(k, ph) for k in range(len(servos))] if wig and not any(torque) else None
            if tele is not None:
                self.world.teleport(tele)
            self.world.set_targets(goals, torque)
            return
        demo_q = None
        if self.demo is not None:
            pts, t0 = self.demo
            if now - t0 > pts[-1][0]:
                self.demo = None
            import openmimic_mujoco as om
            demo_q = om.tick_from_rad(om.interp(pts, now - t0), self.robot)
        with self.lock:
            for k, s in enumerate(self.board.servos.values()):
                torque = s.mem[40] if isinstance(s, FeetechServo) else s.mem[64]
                if not torque:
                    if wig:                                # '손으로' 양 끝까지 쓸기
                        s.actual = self._wig(k, ph)
                    elif demo_q is not None:
                        s.actual = float(demo_q[k])
                    elif auto and self.role == "leader" and k < 5:
                        s.actual = 2048 + 350 * math.sin(now * 0.6 + k)
                s.tick(dt)

    def sync_from_world(self):
        """물리 한 스텝 뒤 팔로워 서보의 실제 위치(엔코더)를 MuJoCo 관절각에서 읽어 둡니다."""
        ticks = self.world.joint_ticks()
        with self.lock:
            for k, s in enumerate(self.board.servos.values()):
                s.actual = float(ticks[k])
                s.sync_present()

    def wiggle(self, sec=4.0):
        self.wiggle_t0 = time.monotonic()
        self.wiggle_until = self.wiggle_t0 + sec

    def set_volt(self, volt):
        """보드 전체 전압을 바꿔 보고합니다 (0.1 V 단위 레지스터)."""
        with self.lock:
            for s in self.board.servos.values():
                if isinstance(s, FeetechServo):
                    s.w(62, int(round(volt * 10)), 1)
                else:
                    s.w(144, int(round(volt * 10)), 2)

    def set_temp(self, temp):
        with self.lock:
            for s in self.board.servos.values():
                s.w(63 if isinstance(s, FeetechServo) else 146, int(temp), 1)


def main(argv=None):
    ap = argparse.ArgumentParser(description="가상 팔 — 실제 팔 없이 OpenMimic 시험")
    ap.add_argument("--robot", choices=("so101", "omx"), default="so101")
    ap.add_argument("--mode", choices=("single", "bimanual"), default="single")
    ap.add_argument("--auto", action="store_true", help="시작부터 리더를 계속 움직임")
    ap.add_argument("--physics", choices=("auto", "mujoco", "none"), default="auto",
                    help="auto = SO-ARM101 한팔이고 mujoco 가 있으면 물리 시뮬레이션")
    ap.add_argument("--cam", default="320x240", help="가상 카메라 해상도 (물리 시뮬레이션 렌더)")
    a = ap.parse_args(argv)
    world = None
    stale = P.frame_dir()
    for f in (stale.glob("cam_*.rgb") if stale.is_dir() else []):
        try:
            f.unlink()                   # 전에 죽은 물리 시뮬레이션이 남긴 렌더 프레임 — 남아 있으면 가상 카메라가 옛 화면을 보여 줌
        except OSError:
            pass                         # Windows: 가상 카메라가 아직 매핑 중 — 첫 렌더가 덮어씁니다
    if a.physics != "none" and a.robot == "so101" and a.mode == "single":
        import openmimic_mujoco as om
        if om.available(a.robot) and not om.gl_ready():
            print("물리 시뮬레이션: MuJoCo 렌더 백엔드를 쓸 수 없어 물리 시뮬레이션 없이 켭니다 "
                  f"(MUJOCO_GL={os.environ.get('MUJOCO_GL', '')})", flush=True)
            if a.physics == "mujoco":
                sys.exit("MuJoCo 렌더 백엔드를 쓸 수 없습니다")
        elif om.available(a.robot):
            cw, ch = (int(v) for v in a.cam.lower().split("x"))
            world = om.World(robot=a.robot, cam_size=(cw, ch))
            print(f"물리 시뮬레이션: MuJoCo (렌더 {os.environ.get('MUJOCO_GL')} · {os.environ.get('OPENMIMIC_GL_INFO', '')})", flush=True)
        elif a.physics == "mujoco":
            sys.exit("mujoco 가 없습니다 — pip install mujoco")

    sides = ["main"] if a.mode == "single" else ["left", "right"]
    SIM_DIR.mkdir(exist_ok=True)
    for f in SIM_DIR.iterdir():
        # 옛 판(PTY 심볼릭 링크)이 남긴 것·가상 카메라 경로·명령 파일
        if f.is_symlink() or f.name.startswith("cam_") or f.name == "cmd":
            try:
                f.unlink()
            except OSError:
                pass
    arms = [SimArm(a.robot, s, r) for s in sides for r in ("follower", "leader")]
    server = SimPortServer({arm.name: arm.serve for arm in arms})
    if world is not None:
        rng = world.ranges_ticks(margin=0.005)       # 리더·팔로워 쓸기 범위를 같게 → 캘리브레이션 범위가 같아짐
        for arm in arms:
            arm.ranges = rng
            if arm.role == "follower":
                arm.world = world
                arm.sync_from_world()
    # 가상 카메라 — 경로 표시용 빈 파일. 영상은 이 경로를 연 쪽에서 openmimic_simcam 이 합성합니다
    cam_names = ["top", "wrist"] if a.mode == "single" else ["top", "left_wrist", "right_wrist"]
    cams = []
    for n in cam_names:
        f = SIM_DIR / f"cam_{n}"
        f.write_text("")
        cams.append({"name": n, "path": str(f)})
    label = {"so101": "SO-ARM101", "omx": "OMX"}[a.robot]
    state = {"auto": a.auto, "volt": {}, "temp": {}}
    state_lock = threading.Lock()

    def write_state():
        with state_lock:
            tmp = SIM_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "pid": os.getpid(), "robot": a.robot, "mode": a.mode, "auto": state["auto"],
                "endpoint": server.endpoint(),
                "ports": [{"path": arm.port, "dev": arm.port, "name": arm.name, "side": arm.side, "role": arm.role,
                           "label": f"가상 {label} {arm.side} {'팔로워' if arm.role == 'follower' else '리더'}",
                           "volt": state["volt"].get(arm.name), "temp": state["temp"].get(arm.name)}
                          for arm in arms],
                "cams": cams, "physics": "mujoco" if world is not None else "",
                "task": dict(world.state(), demo=demo_left()) if world is not None else None},
                ensure_ascii=False, indent=1))
            P.replace_file(tmp, SIM_FILE)

    def demo_left():
        ld = next((x for x in arms if x.role == "leader"), None)
        if ld is None or ld.demo is None:
            return 0
        pts, t0 = ld.demo
        return round(max(0.0, pts[-1][0] - (time.monotonic() - t0)), 1)

    write_state()

    def cleanup(*_):
        try:
            SIM_FILE.unlink()
        except OSError:
            pass
        server.close()
        for c in cams:
            try:
                os.unlink(c["path"])
            except OSError:
                pass
        if world is not None:
            world.close()
        os._exit(0)

    P.install_stop_handlers(cleanup)         # SIGINT · SIGTERM · (Windows) CTRL_BREAK

    def loop():
        last = time.monotonic()
        while True:
            now = time.monotonic()
            for arm in arms:
                arm.tick(now, now - last, state["auto"])
            if world is not None:
                world.step(now - last)
                for arm in arms:
                    if arm.world is not None:
                        arm.sync_from_world()
            last = now
            time.sleep(0.01)

    threading.Thread(target=loop, daemon=True).start()

    if world is not None:
        def render_loop():
            # 카메라 렌더 — GPU(EGL)면 30 fps 를 넉넉히, CPU(OSMesa)면 가능한 만큼. 카메라는 마지막 장면을 읽습니다
            while True:
                t0 = time.monotonic()
                try:
                    world.render_all()
                except Exception as e:      # noqa: BLE001 — 렌더 실패가 팔 흉내를 멈추면 안 됩니다
                    print("렌더 실패:", e, flush=True)
                    time.sleep(2)
                time.sleep(max(0.0, 1 / 30 - (time.monotonic() - t0)))

        def state_loop():
            while True:
                time.sleep(0.5)
                try:
                    write_state()
                except OSError:
                    pass

        threading.Thread(target=render_loop, daemon=True).start()
        threading.Thread(target=state_loop, daemon=True).start()

    def show():
        print(f"\n가상 {label} {'양팔' if a.mode == 'bimanual' else '한팔'} — OpenMimic 포트 목록에 '가상 …' 으로 나타납니다")
        for i, arm in enumerate(arms, 1):
            print(f"  {i}. {arm.name:16s} {arm.port}")
        print(f"  (보드 연결: 127.0.0.1:{server.port})")
        print("명령: w <번호> 쓸기 · a 리더 자동 움직임 " + ("[켜짐]" if state["auto"] else "[꺼짐]")
              + " · v <번호> <V> 전압 · t <번호> <°C> 온도 · l 목록 · q 끝", flush=True)

    def board(tok):
        return arms[int(tok) - 1] if tok.isdigit() and 1 <= int(tok) <= len(arms) else None

    def run(cmd):
        if not cmd:
            return
        if cmd[0] == "q":
            cleanup()
        elif cmd[0] == "l":
            show()
        elif cmd[0] == "a":
            state["auto"] = (cmd[1] == "on") if len(cmd) > 1 and cmd[1] in ("on", "off") else not state["auto"]
            print("리더 자동 움직임:", "켜짐" if state["auto"] else "꺼짐", flush=True)
        elif cmd[0] in ("demo", "reset"):
            if world is None:
                print("물리 시뮬레이션이 아닙니다 (--physics mujoco)", flush=True)
                return
            if cmd[0] == "reset" or (len(cmd) > 1 and cmd[1] == "new"):
                world.reset_scene()
            if cmd[0] == "demo":
                import openmimic_mujoco as om
                ld = next(x for x in arms if x.role == "leader")
                with ld.lock:
                    start = om.rad_from_tick(np.array([s.actual for s in ld.board.servos.values()], float), a.robot)
                ld.demo = (world.plan_demo(start_q=start), time.monotonic())
                print("자동 시연 시작 (약 14초)", flush=True)
            else:
                print("큐브를 새 위치에 놓았습니다", flush=True)
        elif cmd[0] == "w" and len(cmd) > 1 and board(cmd[1]):
            board(cmd[1]).wiggle()
            print(f"{board(cmd[1]).name} 쓰는 중 (4초)", flush=True)
        elif cmd[0] in ("v", "t") and len(cmd) > 2 and board(cmd[1]):
            try:
                val = float(cmd[2])
            except ValueError:
                print("값은 숫자", flush=True)
                return
            arm = board(cmd[1])
            if cmd[0] == "v" and 0 <= val <= 25:
                arm.set_volt(val)
                state["volt"][arm.name] = val
                print(f"{arm.name} 전압 {val:g} V 로 보고", flush=True)
            elif cmd[0] == "t" and 0 <= val <= 120:
                arm.set_temp(val)
                state["temp"][arm.name] = val
                print(f"{arm.name} 온도 {val:g} °C 로 보고", flush=True)
        else:
            print("명령: w <번호> · a [on|off] · v <번호> <V> · t <번호> <°C> · l · q", flush=True)
            return
        write_state()

    show()
    # 터미널에서 직접 띄웠으면 키보드 명령도 받습니다 — 별도 스레드가 한 줄씩 넘김 (select 는 Windows 콘솔에 못 씀)
    typed = queue.Queue()
    if sys.stdin is not None and sys.stdin.isatty():
        def read_stdin():
            for line in sys.stdin:
                typed.put(line)
        threading.Thread(target=read_stdin, daemon=True).start()
    cmd_file = SIM_DIR / "cmd"
    while True:
        # 화면(OpenMimic)이 보내는 명령 — 한 줄에 하나
        try:
            for line in (P.take_file(cmd_file) or "").splitlines():
                run(line.strip().split())
        except OSError:
            pass
        try:
            run(typed.get(timeout=0.2).strip().split())
        except queue.Empty:
            pass


if __name__ == "__main__":
    main()
