#!/usr/bin/env python3
"""가상 팔 — 실제 팔 없이 OpenMimic 전체 흐름(셋업 마법사·Calib·Control·Collect)을 시험하는 도구.

    python tools_simarms.py --robot so101 --mode bimanual     # SO-ARM101 양팔 (보드 4개)
    python tools_simarms.py --robot omx   --mode single       # OMX 한팔 (보드 2개)

켜 두는 동안 OpenMimic 의 포트 목록에 '가상 …' 보드로 나타나고, 끄면 사라집니다 (OpenMimic 재시작 불필요).
실제 팔과는 아무 관계가 없습니다 — 실제 팔을 쓸 때는 이 도구를 켜지 않으면 됩니다.

동작
  - 모터는 실제 프로토콜을 그대로 말합니다: SO-ARM101 = Feetech STS3215, OMX = Dynamixel X (Protocol 2.0).
    lerobot 버스가 진짜 서보와 통신하듯 ping·read·write·sync read/write 를 주고받습니다.
  - 모터 ID·모델은 lerobot 정의 그대로 (SO: ID 1~6, OMX 팔로워 11~16 / 리더 1~6).
  - 처음에는 공장 상태(캘리브레이션 없음)입니다. 전압은 팔로워 12 V, 리더 5 V (SO) 로 보고합니다.
  - Homing_Offset 을 실제처럼 반영합니다 (Feetech: Present = Actual - Homing, Dynamixel: Present = Actual + Homing).
  - 토크가 켜지면 목표 위치로 천천히 움직입니다.
  - 손으로 움직이는 대신 콘솔 명령을 씁니다:
        w <번호>   그 보드의 관절을 양 끝까지 한 번 쓸기 (포트 찾기·캘리브레이션·확인 단계용)
        a          리더를 계속 천천히 움직이기 켜기/끄기 (Control 리더 팔로우·Collect 용)
        l          보드 목록
        q          끝내기
  - 모터 ID 세팅(보드레이트·ID 변경)은 흉내내지 않습니다.

포트 경로는 레포 안의 openmimic_sim/<이름> 심볼릭 링크로 고정됩니다 — 껐다 켜도 같은 경로라 다시 지정할 필요가 없습니다.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import pty
import select
import signal
import sys
import threading
import time
import tty
from pathlib import Path

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


# ============================================================================ 보드 = PTY 하나
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
        self.master, slave = pty.openpty()
        tty.setraw(self.master)
        tty.setraw(slave)
        self._slave = slave                   # 열어 두어야 OpenMimic 이 닫아도 PTY 가 유지됩니다
        self.dev = os.ttyname(slave)
        self.link = SIM_DIR / self.name
        self.wiggle_until = 0.0
        self.wiggle_t0 = 0.0
        self.lock = threading.Lock()
        threading.Thread(target=self._io, daemon=True).start()

    def _io(self):
        buf = b""
        while True:
            try:
                buf += os.read(self.master, 1024)
            except OSError:
                return
            pkts, buf = self.board.split(buf)
            for pkt in pkts:
                with self.lock:
                    for rep in self.board.handle(pkt):
                        os.write(self.master, rep)

    def tick(self, now, dt, auto):
        with self.lock:
            servos = list(self.board.servos.values())
            for k, s in enumerate(servos):
                torque = s.mem[40] if isinstance(s, FeetechServo) else s.mem[64]
                if not torque:
                    if now < self.wiggle_until:            # '손으로' 양 끝까지 쓸기
                        ph = (now - self.wiggle_t0) / (self.wiggle_until - self.wiggle_t0)
                        s.actual = 2048 + 900 * math.sin(2 * math.pi * ph)
                    elif auto and self.role == "leader" and k < 5:
                        s.actual = 2048 + 350 * math.sin(now * 0.6 + k)
                s.tick(dt)

    def wiggle(self, sec=4.0):
        self.wiggle_t0 = time.monotonic()
        self.wiggle_until = self.wiggle_t0 + sec


def main(argv=None):
    ap = argparse.ArgumentParser(description="가상 팔 — 실제 팔 없이 OpenMimic 시험")
    ap.add_argument("--robot", choices=("so101", "omx"), default="so101")
    ap.add_argument("--mode", choices=("single", "bimanual"), default="single")
    ap.add_argument("--auto", action="store_true", help="시작부터 리더를 계속 움직임")
    a = ap.parse_args(argv)

    sides = ["main"] if a.mode == "single" else ["left", "right"]
    SIM_DIR.mkdir(exist_ok=True)
    for f in SIM_DIR.iterdir():
        if f.is_symlink():
            f.unlink()
    arms = [SimArm(a.robot, s, r) for s in sides for r in ("follower", "leader")]
    for arm in arms:
        os.symlink(arm.dev, arm.link)
    label = {"so101": "SO-ARM101", "omx": "OMX"}[a.robot]
    SIM_FILE.write_text(json.dumps({
        "pid": os.getpid(), "robot": a.robot, "mode": a.mode,
        "ports": [{"path": str(arm.link), "dev": arm.dev,
                   "label": f"가상 {label} {arm.side} {'팔로워' if arm.role == 'follower' else '리더'}"}
                  for arm in arms]}, ensure_ascii=False, indent=1))

    def cleanup(*_):
        try:
            SIM_FILE.unlink()
        except OSError:
            pass
        for arm in arms:
            try:
                arm.link.unlink()
            except OSError:
                pass
        os._exit(0)

    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGTERM, cleanup)
    state = {"auto": a.auto}

    def loop():
        last = time.monotonic()
        while True:
            now = time.monotonic()
            for arm in arms:
                arm.tick(now, now - last, state["auto"])
            last = now
            time.sleep(0.01)

    threading.Thread(target=loop, daemon=True).start()

    def show():
        print(f"\n가상 {label} {'양팔' if a.mode == 'bimanual' else '한팔'} — OpenMimic 포트 목록에 '가상 …' 으로 나타납니다")
        for i, arm in enumerate(arms, 1):
            print(f"  {i}. {arm.name:16s} {arm.link}  (→ {arm.dev})")
        print("명령: w <번호> 쓸기 · a 리더 자동 움직임 " + ("[켜짐]" if state["auto"] else "[꺼짐]") + " · l 목록 · q 끝")

    show()
    interactive = sys.stdin.isatty()
    while True:
        if not interactive:
            time.sleep(1)
            continue
        r, _, _ = select.select([sys.stdin], [], [], 0.5)
        if not r:
            continue
        cmd = sys.stdin.readline().strip().split()
        if not cmd:
            continue
        if cmd[0] == "q":
            cleanup()
        elif cmd[0] == "l":
            show()
        elif cmd[0] == "a":
            state["auto"] = not state["auto"]
            print("리더 자동 움직임:", "켜짐" if state["auto"] else "꺼짐")
        elif cmd[0] == "w" and len(cmd) > 1 and cmd[1].isdigit() and 1 <= int(cmd[1]) <= len(arms):
            arms[int(cmd[1]) - 1].wiggle()
            print(f"{arms[int(cmd[1]) - 1].name} 쓰는 중 (4초)")
        else:
            print("명령: w <번호> · a · l · q")


if __name__ == "__main__":
    main()
