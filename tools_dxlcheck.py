#!/usr/bin/env python3
"""ROBOTIS OMX 팔 한 개(보드 1개 = 포트 1개)의 불량 여부 점검 — Dynamixel(Protocol 2.0) 판.

    python tools_dxlcheck.py --port /dev/serial/by-id/usb-... --role follower      (Windows: --port COM5)
    python tools_dxlcheck.py --port ... --role leader --json

OpenMimic 의 Setup 탭 '팔 불량 점검'·셋업 마법사 '진단' 이 OMX 일 때 이 모듈을 씁니다.
함수 이름·결과 형식은 tools_armcheck(SO-ARM101, STS3215)와 같습니다.

종료 코드: 0 정상 / 1 주의 / 2 불량 의심 / 3 실행 실패

점검 항목 (서보에 아무것도 쓰지 않습니다. 토크도 안 건드립니다)
  모터 ID 응답 · 모델 번호 · Hardware_Error_Status · 응답 Alert 비트 · 온도 ·
  정지 상태 엔코더 흔들림 · 통신 누락. 전압은 표시만 하고 판정하지 않습니다.
  --sweep (OpenMimic 에서만) 은 Torque_Enable=0 만 씁니다.

판정 근거
  [lerobot] robots/omx_follower, teleoperators/omx_leader — 모터 ID·모델
            motors/dynamixel/tables.py — 레지스터 주소, 모델 번호
  [ROBOTIS] DYNAMIXEL X 시리즈 e-Manual, Hardware Error Status(70)
            bit0 입력 전압 / bit2 과열 / bit3 모터 엔코더 / bit4 전기 충격 / bit5 과부하.
            Protocol 2.0 상태 패킷 Error 의 bit7 = Alert (하드웨어 에러가 있다는 표시)
  [경험값]  엔코더 흔들림·온도 — tools_armcheck 와 같은 값
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import openmimic_platform as P
import tools_armcheck as AC
from tools_armcheck import FAIL, INFO, NOISE_FAIL, NOISE_WARN, TEMP_REL_WARN, TEMP_WARN_C, WARN, JOINTS

# [lerobot] omx_follower.py / omx_leader.py 의 모터 정의 그대로
MOTORS = {
    "follower": {"shoulder_pan": (11, "xl430-w250"), "shoulder_lift": (12, "xl430-w250"),
                 "elbow_flex": (13, "xl430-w250"), "wrist_flex": (14, "xl330-m288"),
                 "wrist_roll": (15, "xl330-m288"), "gripper": (16, "xl330-m288")},
    "leader": {"shoulder_pan": (1, "xl330-m288"), "shoulder_lift": (2, "xl330-m288"),
               "elbow_flex": (3, "xl330-m288"), "wrist_flex": (4, "xl330-m288"),
               "wrist_roll": (5, "xl330-m288"), "gripper": (6, "xl330-m077")},
}
HW_ERR = {1: "입력 전압 이상", 4: "과열", 8: "모터 엔코더 이상", 16: "전기 충격", 32: "과부하"}   # [ROBOTIS]
HW_HINT = {
    1: "전원 전압이 모터 허용 범위 밖 — 어댑터·배선 확인",
    4: "과열로 토크가 차단됨 — 식힌 뒤 원인(막힘·과부하) 확인",
    8: "엔코더 이상 — 모터 교체 대상",
    16: "전기적 이상 — 모터 교체 대상",
    32: "과부하로 토크가 차단됨 — 걸림·과부하 원인 제거 후 재부팅(전원 재투입)",
}
ALERT_BIT = 0x80


class BusIO:
    """lerobot DynamixelMotorsBus 를 얇게 감쌉니다. 모든 호출이 (값, 에러바이트)를 돌려주고 예외를 던지지 않습니다."""

    def __init__(self, port: str, role: str = "follower"):
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.dynamixel import DynamixelMotorsBus
        from lerobot.motors.motors_bus import get_address

        self.role = role if role in MOTORS else "follower"
        spec = MOTORS[self.role]
        self.ids = {n: spec[n][0] for n in JOINTS}
        self.models = {n: spec[n][1] for n in JOINTS}
        self._get_address = get_address
        self.bus = DynamixelMotorsBus(
            P.resolve_serial_port(port), {n: Motor(i, m, MotorNormMode.RANGE_M100_100) for n, (i, m) in spec.items()})
        self.bus.connect(handshake=False)
        self.model_numbers = {n: self.bus.model_number_table[m] for n, m in self.models.items()}

    def close(self):
        try:
            self.bus.port_handler.closePort()
        except Exception:
            pass

    def _addr(self, name):
        return self._get_address(self.bus.model_ctrl_table, "xl330-m288", name)

    def ping(self, id_):
        model, comm, err = self.bus.packet_handler.ping(self.bus.port_handler, id_)
        return (model if self.bus._is_comm_success(comm) else None), err

    def scan(self):
        try:
            return self.bus.broadcast_ping() or {}
        except Exception:
            return None

    def read(self, id_, name):
        addr, n = self._addr(name)
        v, comm, err = self.bus._read(addr, n, id_, raise_on_error=False)
        if not self.bus._is_comm_success(comm):
            return None, 0
        return self.bus._decode_sign(name, {id_: v})[id_], err

    def write(self, id_, name, value):
        addr, n = self._addr(name)
        v = self.bus._encode_sign(name, {id_: int(value)})[id_]
        comm, err = self.bus._write(addr, n, id_, v, raise_on_error=False)
        return self.bus._is_comm_success(comm), err


def new_report(io):
    rep = AC.Report()
    for j in JOINTS:
        rep.motors[j]["id"] = io.ids[j]
    rep.alert = {j: False for j in JOINTS}
    return rep


def _read(io, rep, joint, name):
    v, err = io.read(io.ids[joint], name)
    # Protocol 2.0 의 응답 Error 바이트는 Feetech 와 뜻이 다릅니다 (bit7 = Alert, 나머지는 명령 오류 번호).
    # 보호 비트는 Hardware_Error_Status 레지스터에서 따로 읽고, 여기서는 Alert 만 기록합니다.
    if err & ALERT_BIT:
        rep.alert[joint] = True
    return v


def check_presence(io, rep):
    present = []
    for j in JOINTS:
        model = None
        for _ in range(3):
            model, err = io.ping(io.ids[j])
            if err & ALERT_BIT:
                rep.alert[j] = True
            if model is not None:
                break
        rep.motors[j]["model"] = model
        if model is None:
            rep.add(FAIL, j, "응답", f"ID {io.ids[j]} 응답 없음 — 모터·케이블·데이지체인 확인 (새 모터면 ID 세팅 필요)")
            continue
        want = io.model_numbers[j]
        if model != want:
            rep.add(FAIL, j, "모델", f"ID {io.ids[j]} 모델 번호 {model} ({io.models[j]}={want} 아님)")
        present.append(j)
    found = io.scan()
    if found:
        extra = sorted(i for i in found if i not in io.ids.values())
        if extra:
            rep.add(WARN, None, "ID", f"이 팔({io.role}) 구성 밖의 ID 가 응답합니다: {extra} — 모터 ID 세팅을 다시 확인")
    return present


def check_static(io, rep, present, *, role=None, samples=50, noise_warn=NOISE_WARN, noise_fail=NOISE_FAIL):
    fw = {}
    for j in present:
        m = rep.motors[j]
        v = _read(io, rep, j, "Firmware_Version")
        if v is not None:
            fw[j] = m["fw"] = str(v)
        raw_v = _read(io, rep, j, "Present_Input_Voltage")
        m["voltage_v"] = None if raw_v is None else raw_v / 10.0        # 표시만 (0.1 V 단위)
        for reg, key in (("Present_Temperature", "temp_c"), ("Hardware_Error_Status", "hw_err"),
                         ("Torque_Enable", "torque"), ("Operating_Mode", "mode"),
                         ("Min_Position_Limit", "min_lim"), ("Max_Position_Limit", "max_lim"),
                         ("Homing_Offset", "homing")):
            m[key] = _read(io, rep, j, reg)
        if m.get("hw_err"):
            rep.err[j] |= int(m["hw_err"])

        pos, fails = [], 0
        for _ in range(samples):
            p = _read(io, rep, j, "Present_Position")
            if p is None:
                fails += 1
            else:
                pos.append(p)
            time.sleep(0.005)
        m["comm"] = f"{samples - fails}/{samples}"
        if fails:
            lvl = FAIL if fails > samples * 0.1 else WARN if fails >= 2 else INFO
            rep.add(lvl, j, "통신", f"읽기 {fails}/{samples} 회 실패 — 커넥터·케이블 접촉 또는 ID 충돌")
        if len(pos) >= 2:
            acc = lo = hi = 0
            for a, b in zip(pos, pos[1:]):
                acc += AC.unwrap_step(a, b)
                lo, hi = min(lo, acc), max(hi, acc)
            pp = m["noise_ticks"] = hi - lo
            torq = " (토크 ON 상태라 서보가 떨고 있을 수도 있음)" if m.get("torque") else ""
            if pp > noise_fail:
                rep.add(FAIL, j, "엔코더", f"정지 중 위치가 {pp} tick 흔들림 — 각도 센서 불량 의심{torq}")
            elif pp > noise_warn:
                rep.add(WARN, j, "엔코더", f"정지 중 위치가 {pp} tick 흔들림" + (torq or " — 팔이 완전히 멈춰 있었는지 확인"))

        t = m.get("temp_c")
        if t is not None and t > TEMP_WARN_C:
            rep.add(WARN, j, "온도", f"{t}°C — 쉬는 중인데 뜨거우면 원인 확인")

    if len(set(fw.values())) > 1:
        rep.add(INFO, None, "펌웨어", "모터마다 펌웨어가 다릅니다: " + ", ".join(f"{j}={v}" for j, v in fw.items()))
    temps = {j: rep.motors[j]["temp_c"] for j in present if rep.motors[j].get("temp_c") is not None}
    if len(temps) >= 3:
        med = statistics.median(temps.values())
        for j, t in temps.items():
            if t - med >= TEMP_REL_WARN:
                rep.add(WARN, j, "온도", f"{t}°C — 같은 팔 다른 모터(중앙값 {med:g}°C)보다 {t - med:g}°C 높음")


def finalize(rep):
    """Hardware_Error_Status 비트를 판정에 넣습니다. 모든 점검이 끝난 뒤 한 번만."""
    for j in JOINTS:
        e = rep.err[j]
        rep.motors[j]["protect"] = [name for bit, name in HW_ERR.items() if e & bit]
        for bit, name in HW_ERR.items():
            if e & bit:
                rep.add(FAIL, j, "보호", f"{name} — {HW_HINT[bit]}")
        if rep.alert.get(j) and not e:
            rep.add(WARN, j, "보호", "응답에 Alert 비트가 있었습니다 — 하드웨어 에러가 잠깐 났다가 풀렸을 수 있음")


SweepTracker = AC.SweepTracker


def torque_off(io, joints):
    for j in joints:
        try:
            io.write(io.ids[j], "Torque_Enable", 0)
        except Exception:
            pass


def run(io, *, role=None):
    rep = new_report(io)
    present = check_presence(io, rep)
    check_static(io, rep, present, role=role)
    finalize(rep)
    return rep


def main(argv=None):
    import openmimic_fakeserial
    openmimic_fakeserial.install()          # --port sim://… 면 가상 팔 도구의 보드
    P.utf8_stdio()                          # Windows 에서 activate 없이 돌려도 한국어 출력이 깨지지 않게
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", required=True)
    ap.add_argument("--role", choices=("follower", "leader"), default="follower")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    try:
        io = BusIO(a.port, a.role)
    except Exception as e:
        print(f"포트를 열 수 없습니다: {e}", file=sys.stderr)
        return 3
    try:
        rep = run(io, role=a.role)
    finally:
        io.close()
    d = rep.as_dict()
    if a.json:
        print(json.dumps(d, ensure_ascii=False, indent=1))
    else:
        print(f"\n포트: {a.port} ({a.role})")
        for j, m in d["motors"].items():
            v = m.get("voltage_v")
            print(f"  {j:14s} ID {m['id']:>2}  {'응답 없음' if m.get('model') is None else m['level']:6s}"
                  f"  {'' if v is None else f'{v:.1f} V'}  {'' if m.get('temp_c') is None else str(m['temp_c']) + '°C'}")
        for f in d["findings"]:
            if f["level"] in (WARN, FAIL):
                print(f"  [{f['level']}] {f['joint'] or '팔 전체'} · {f['check']} · {f['message']}")
        print(f"\n판정: {d['verdict']}")
    return d["code"]


if __name__ == "__main__":
    sys.exit(main())
