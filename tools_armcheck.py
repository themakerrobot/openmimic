#!/usr/bin/env python3
"""SO-101 팔 한 개(서보 드라이버 보드 1개 = 포트 1개)의 불량 여부 점검.

    python tools_armcheck.py                                  # 포트가 하나면 자동 선택
    python tools_armcheck.py --port /dev/serial/by-id/usb-... --role follower      (Windows: --port COM5)
    python tools_armcheck.py --port ... --role leader --sweep # + 손으로 관절 쓸기 (엔코더)
    python tools_armcheck.py --port ... --json                # 결과를 JSON 으로

OpenMimic 의 Setup 탭 '팔 불량 점검' 도 이 모듈을 그대로 씁니다.

종료 코드: 0 정상 / 1 주의 / 2 불량 의심 / 3 실행 실패 — 여러 대를 연달아 검사할 때 씁니다.

단계
  기본(항상)  서보에 아무것도 쓰지 않습니다. 토크도 안 건드립니다.
              ID 1~6 응답 · 모델(STS3215) · 보호 플래그(Status 레지스터 + 응답 에러 바이트) ·
              전원 계통(5V/12V) 과 전압 범위 · 모터 간 전압 편차 · 온도 · 펌웨어 ·
              정지 상태 엔코더 흔들림 · 통신 누락
  --sweep     토크를 끄고(Torque_Enable=0), 손으로 관절을 양 끝까지 움직이게 합니다.
              움직인 범위 · 엔코더 튐(한 샘플 사이 비정상 점프) · 읽기 실패

모터를 구동하는 시험은 넣지 않습니다. 쓰는 레지스터는 --sweep 의 Torque_Enable=0 하나뿐이고,
EEPROM 은 어떤 경우에도 쓰지 않습니다.

판정 근거 — 출처가 있는 값만 씁니다
  [SDK]   Feetech scservo_sdk/protocol_packet_handler.py 의 ERRBIT_* (보호 비트 정의)
  [Seeed] Seeed-Projects/Seeed_RoboController (Seeed 공식 SoARM 캘리브레이션 도구)
            src/tools/servo_middle_calibration.py
              전압 = Present_Voltage / 10 (V)
              5V 계통 4.5~5.5 V, 12V 계통 10.5~13.5 V, 7.0 V 미만이면 5V 계통으로 판정
              SAFE_TEMPERATURE_MAX = 60 °C
            src/gui/factory_calibration_tool.py
              Status 레지스터(주소 65)를 ERRBIT 로 해석, 과열 보호 70 °C 초과 시 토크 차단,
              과부하·과전류 보호는 위치 명령을 다시 보내면 해제
  [lerobot] motors/feetech/tables.py — 레지스터 주소, STS3215 모델 번호 777
  [경험값]  엔코더 흔들림·점프·온도 편차·쓸기 최소 범위 — 데이터시트 값이 아닙니다. 옵션으로 조정.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

import openmimic_platform as P

JOINTS = ("shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll", "gripper")
IDS = {n: i for i, n in enumerate(JOINTS, 1)}
STS3215_MODEL = 777                     # [lerobot]
RES = 4096

ERRBITS = {1: "입력 전압 이상", 2: "각도 센서 이상", 4: "과열", 8: "과전류", 32: "과부하"}   # [SDK]
ERR_HINT = {                                                                          # [Seeed]
    1: "전원 전압이 서보 허용 범위 밖 — 어댑터 전압·극성 확인",
    2: "각도 센서(엔코더) 이상 — 서보 교체 대상",
    4: "70 °C 초과로 토크 차단됨 — 식힌 뒤 원인(막힘·과부하) 확인",
    8: "과전류 보호 — 위치 명령을 다시 보내면 해제. 반복되면 기어 걸림·모터 불량",
    32: "과부하 보호(지속 스톨) — 위치 명령을 다시 보내면 해제. 반복되면 기어 걸림·모터 불량",
}

VOLT_SYSTEM_SPLIT = 7.0                              # [Seeed] V
VOLT_RANGE = {"5V": (4.5, 5.5), "12V": (10.5, 13.5)}  # [Seeed] V
TEMP_WARN_C = 60.0                                   # [Seeed] SAFE_TEMPERATURE_MAX

TEMP_REL_WARN = 8            # [경험값] 같은 팔 중앙값보다 이만큼(°C) 높으면
VOLT_SPREAD_WARN = 0.5       # [경험값] 같은 팔 모터 간 전압 편차 (V)
NOISE_WARN, NOISE_FAIL = 3, 10   # [경험값] 정지 중 엔코더 흔들림 (tick p-p)
JUMP_TICKS = 400             # [경험값] 쓸기 중 한 샘플 사이 허용 최대 이동 (≈35°)
SWEEP_MIN_DEG = {j: 60 for j in JOINTS} | {"gripper": 30, "wrist_roll": 90}   # [경험값]

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
RANK = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}


def errbit_names(err: int) -> list[str]:
    return [name for bit, name in ERRBITS.items() if err & bit]


def unwrap_step(prev: int, cur: int) -> int:
    """단일 회전 엔코더의 연속 두 샘플 차이를 ±2048 안으로 접습니다."""
    d = cur - prev
    if d > RES // 2:
        d -= RES
    elif d < -RES // 2:
        d += RES
    return d


# --------------------------------------------------------------------------- 버스
class BusIO:
    """lerobot FeetechMotorsBus 를 얇게 감쌉니다. 모든 호출이 (값, 에러바이트) 를 돌려주고
    예외를 던지지 않습니다 — 불량 팔을 검사하는 도구라 실패도 결과입니다."""

    def __init__(self, port: str):
        from lerobot.motors import Motor, MotorNormMode
        from lerobot.motors.feetech import FeetechMotorsBus
        from lerobot.motors.motors_bus import get_address

        self._get_address = get_address
        self.bus = FeetechMotorsBus(
            P.resolve_serial_port(port), {n: Motor(IDS[n], "sts3215", MotorNormMode.DEGREES) for n in JOINTS})
        self.bus.connect(handshake=False)

    def close(self):
        try:
            self.bus.port_handler.closePort()
        except Exception:
            pass

    def _addr(self, name):
        return self._get_address(self.bus.model_ctrl_table, "sts3215", name)

    def ping(self, id_):
        model, comm, err = self.bus.packet_handler.ping(self.bus.port_handler, id_)
        return (model if self.bus._is_comm_success(comm) else None), err

    def scan(self):
        """broadcast ping — 1~6 밖의 ID 가 섞여 있는지 확인용. 실패하면 None."""
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


# --------------------------------------------------------------------------- 결과
class Report:
    def __init__(self):
        self.findings = []            # (level, joint|None, check, message)
        self.motors = {n: {"id": IDS[n]} for n in JOINTS}
        self.err = {n: 0 for n in JOINTS}
        self.power = {}

    def add(self, level, joint, check, msg):
        self.findings.append((level, joint, check, msg))

    def note_err(self, joint, err):
        if err:
            self.err[joint] |= err

    def verdict(self):
        worst = max((RANK[f[0]] for f in self.findings), default=0)
        return {0: "정상", 1: "주의", 2: "불량 의심"}[worst], worst

    def joint_level(self, joint):
        levels = [f[0] for f in self.findings if f[1] == joint]
        return max(levels, key=lambda l: RANK[l], default=OK)

    def as_dict(self):
        verdict, worst = self.verdict()
        motors = {}
        for j, m in self.motors.items():
            motors[j] = dict(m)
            motors[j]["level"] = self.joint_level(j)
        return {"verdict": verdict, "code": worst, "power": self.power, "motors": motors,
                "findings": [{"level": l, "joint": j, "check": c, "message": msg}
                             for l, j, c, msg in sorted(self.findings, key=lambda f: -RANK[f[0]])]}


def _read(io, rep, joint, name):
    v, err = io.read(IDS[joint], name)
    rep.note_err(joint, err)
    return v


# --------------------------------------------------------------------------- 기본 점검
def check_presence(io, rep):
    present = []
    for j in JOINTS:
        model = None
        for _ in range(3):
            model, err = io.ping(IDS[j])
            rep.note_err(j, err)
            if model is not None:
                break
        rep.motors[j]["model"] = model
        if model is None:
            rep.add(FAIL, j, "응답", f"ID {IDS[j]} 응답 없음 — 모터·케이블·데이지체인 확인")
            continue
        if model != STS3215_MODEL:
            rep.add(FAIL, j, "모델", f"ID {IDS[j]} 모델 번호 {model} (STS3215={STS3215_MODEL} 아님)")
        present.append(j)
    found = io.scan()
    if found:
        extra = sorted(i for i in found if i not in IDS.values())
        if extra:
            rep.add(WARN, None, "ID", f"1~6 밖의 ID 가 응답합니다: {extra} — 모터 ID 세팅을 다시 확인")
    return present


def check_power(rep, present, role):
    volts = {j: rep.motors[j]["voltage_v"] for j in present if rep.motors[j].get("voltage_v") is not None}
    if not volts:
        return
    med = statistics.median(volts.values())
    system = "5V" if med < VOLT_SYSTEM_SPLIT else "12V"
    lo, hi = VOLT_RANGE[system]
    rep.power = {"system": system, "median_v": round(med, 1), "range_v": [lo, hi]}
    # 5V/12V 혼용 — 가장 먼저 짚어야 하는 지점
    if role == "leader" and system == "12V":
        rep.add(FAIL, None, "전원", f"리더인데 {med:.1f} V (12V 계통) — 리더 모터는 7.4V 급입니다. 즉시 전원을 분리하세요")
    elif role == "follower" and system == "5V":
        rep.add(WARN, None, "전원", f"팔로워인데 {med:.1f} V (5V 계통) — 12V 모터 팔이면 힘이 모자랍니다. 어댑터 확인")
    for j, v in volts.items():
        if not (lo <= v <= hi):
            rep.add(WARN, j, "전원", f"{v:.1f} V — {system} 계통 범위 {lo}~{hi} V 밖 "
                    "(팔 불량이 아니라 어댑터·케이블일 수 있음)")
    spread = max(volts.values()) - min(volts.values())
    if len(volts) >= 2 and spread > VOLT_SPREAD_WARN:
        lowest = min(volts, key=volts.get)
        rep.add(WARN, lowest, "전원", f"같은 팔 안에서 전압 편차 {spread:.1f} V — {lowest} 가 가장 낮음. "
                "데이지체인 커넥터 접촉 확인")


def check_static(io, rep, present, *, role=None, samples=50, noise_warn=NOISE_WARN, noise_fail=NOISE_FAIL):
    fw = {}
    for j in present:
        m = rep.motors[j]
        maj, mi = _read(io, rep, j, "Firmware_Major_Version"), _read(io, rep, j, "Firmware_Minor_Version")
        if maj is not None and mi is not None:
            fw[j] = m["fw"] = f"{maj}.{mi}"
        raw_v = _read(io, rep, j, "Present_Voltage")
        m["voltage_v"] = None if raw_v is None else raw_v / 10.0
        for reg, key in (("Present_Temperature", "temp_c"), ("Status", "status"),
                         ("Torque_Enable", "torque"), ("Operating_Mode", "mode"),
                         ("Min_Position_Limit", "min_lim"), ("Max_Position_Limit", "max_lim"),
                         ("Homing_Offset", "homing")):
            m[key] = _read(io, rep, j, reg)
        if m.get("status"):
            rep.note_err(j, m["status"])       # 래치된 보호 플래그 — 응답 에러 바이트와 같은 비트

        # 정지 상태 엔코더 흔들림 + 통신 누락 (같은 표본으로)
        pos, fails = [], 0
        for _ in range(samples):
            v = _read(io, rep, j, "Present_Position")
            if v is None:
                fails += 1
            else:
                pos.append(v)
            time.sleep(0.005)
        m["comm"] = f"{samples - fails}/{samples}"
        if fails:
            # 한 번 끊김은 기록만 — 멀쩡한 버스에서도 드물게 생깁니다 (lerobot 도 재시도를 둡니다)
            lvl = FAIL if fails > samples * 0.1 else WARN if fails >= 2 else INFO
            rep.add(lvl, j, "통신", f"읽기 {fails}/{samples} 회 실패 — 커넥터·케이블 접촉 또는 ID 충돌")
        if len(pos) >= 2:
            acc = lo = hi = 0
            for a, b in zip(pos, pos[1:]):
                acc += unwrap_step(a, b)
                lo, hi = min(lo, acc), max(hi, acc)
            pp = m["noise_ticks"] = hi - lo
            torq = " (토크 ON 상태라 서보가 떨고 있을 수도 있음)" if m.get("torque") else ""
            if pp > noise_fail:
                rep.add(FAIL, j, "엔코더", f"정지 중 위치가 {pp} tick 흔들림 — 각도 센서 불량 의심{torq}")
            elif pp > noise_warn:
                rep.add(WARN, j, "엔코더", f"정지 중 위치가 {pp} tick 흔들림"
                        + (torq or " — 팔이 완전히 멈춰 있었는지 확인"))

        t = m.get("temp_c")
        if t is not None and t > TEMP_WARN_C:
            rep.add(WARN, j, "온도", f"{t}°C — Seeed 권장 {TEMP_WARN_C:g}°C 초과. 쉬는 중인데 뜨거우면 원인 확인")

        lo_l, hi_l = m.get("min_lim"), m.get("max_lim")
        if lo_l is not None and hi_l is not None:
            if lo_l >= hi_l:
                rep.add(WARN, j, "EEPROM", f"Min_Position_Limit({lo_l}) ≥ Max({hi_l}) — 캘리브레이션을 다시 하세요")
            elif lo_l == 0 and hi_l == RES - 1 and not m.get("homing") and j != "wrist_roll":
                rep.add(INFO, j, "EEPROM", "캘리브레이션 안 된 상태 (불량 아님)")
        if m.get("mode") not in (None, 0):
            rep.add(WARN, j, "EEPROM", f"Operating_Mode={m['mode']} — 위치 모드(0)가 아님")

    if len(set(fw.values())) > 1:
        rep.add(INFO, None, "펌웨어", "모터마다 펌웨어가 다릅니다: " + ", ".join(f"{j}={v}" for j, v in fw.items()))

    check_power(rep, present, role)

    temps = {j: rep.motors[j]["temp_c"] for j in present if rep.motors[j].get("temp_c") is not None}
    if len(temps) >= 3:
        med = statistics.median(temps.values())
        for j, t in temps.items():
            if t - med >= TEMP_REL_WARN:
                rep.add(WARN, j, "온도", f"{t}°C — 같은 팔 다른 모터(중앙값 {med:g}°C)보다 {t - med:g}°C 높음. "
                        "쉬는 중인데 혼자 뜨거우면 내부 쇼트·기어 걸림 의심")


def finalize(rep):
    """누적된 보호 비트를 판정에 넣습니다. 모든 점검이 끝난 뒤 한 번만 부르세요."""
    for j in JOINTS:
        e = rep.err[j]
        rep.motors[j]["protect"] = errbit_names(e)
        for bit in ERRBITS:
            if e & bit:
                rep.add(FAIL, j, "보호", f"{ERRBITS[bit]} — {ERR_HINT[bit]}")


# --------------------------------------------------------------------------- 쓸기
class SweepTracker:
    """손으로 쓸 때의 위치 표본을 관절별로 누적합니다. CLI 와 OpenMimic 이 같이 씁니다."""

    def __init__(self, joints, jump_ticks=JUMP_TICKS):
        self.jump = jump_ticks
        self.s = {j: {"prev": None, "acc": 0, "lo": 0, "hi": 0, "jumps": 0, "fails": 0, "n": 0}
                  for j in joints}

    def feed(self, joint, value):
        s = self.s[joint]
        s["n"] += 1
        if value is None:
            s["fails"] += 1
            return
        if s["prev"] is not None:
            d = unwrap_step(s["prev"], value)
            if abs(d) > self.jump:
                s["jumps"] += 1          # 사람 손으로는 한 샘플 사이에 못 가는 거리
            else:
                s["acc"] += d
                s["lo"], s["hi"] = min(s["lo"], s["acc"]), max(s["hi"], s["acc"])
        s["prev"] = value

    def deg(self, joint):
        s = self.s[joint]
        return round((s["hi"] - s["lo"]) * 360 / RES, 1)

    def live(self, min_deg=SWEEP_MIN_DEG):
        return {j: {"deg": self.deg(j), "need": min_deg.get(j, 60), "jumps": s["jumps"],
                    "fails": s["fails"], "n": s["n"]} for j, s in self.s.items()}

    def judge(self, rep, min_deg=SWEEP_MIN_DEG):
        for j, s in self.s.items():
            deg = rep.motors[j]["sweep_deg"] = self.deg(j)
            need = min_deg.get(j, 60)
            if s["jumps"]:
                rep.add(FAIL, j, "쓸기", f"엔코더 값이 {s['jumps']} 번 튐 (한 샘플에 {self.jump} tick 초과) "
                        "— 각도 센서·자석 불량 의심")
            if s["fails"] > max(2, s["n"] * 0.05):
                rep.add(WARN, j, "쓸기", f"쓰는 동안 읽기 {s['fails']}/{s['n']} 회 실패 — 움직일 때 끊기면 케이블 단선 의심")
            if deg < need:
                rep.add(WARN, j, "쓸기", f"{deg}° 만 움직였습니다 (기준 {need}°) — 덜 움직였거나 걸림/뻑뻑함")


def torque_off(io, joints):
    for j in joints:
        try:
            io.write(IDS[j], "Torque_Enable", 0)
        except Exception:
            pass


def _stdin_enter():
    return P.stdin_line_ready()


def check_sweep(io, rep, present, *, min_deg=SWEEP_MIN_DEG, jump_ticks=JUMP_TICKS, max_s=25.0,
                ask=input, enter_pressed=None):
    """CLI 용 — 관절 하나씩 Enter 로 시작/끝. enter_pressed() 가 True 면 그 관절을 끝냅니다."""
    enter_pressed = enter_pressed or _stdin_enter
    if any(rep.motors[j].get("torque") for j in present):
        ask("\n  토크가 켜진 모터가 있습니다. 끄면 팔이 처집니다 — 팔을 받치거나 내려놓고 Enter ")
    torque_off(io, present)
    tr = SweepTracker(present, jump_ticks)
    for j in present:
        ask(f"\n  [{j}] Enter 를 누른 뒤 양 끝까지 천천히 움직이고, 다 했으면 다시 Enter ")
        t0 = time.monotonic()
        while time.monotonic() - t0 < max_s:
            tr.feed(j, _read(io, rep, j, "Present_Position"))
            s = tr.s[j]
            print(f"\r    움직인 범위 {tr.deg(j):6.1f}°   튐 {s['jumps']}   실패 {s['fails']}   ", end="", flush=True)
            if enter_pressed():
                break
            time.sleep(0.02)
        print()
    tr.judge(rep, min_deg)


# --------------------------------------------------------------------------- 실행
def run(io, *, role=None, sweep=False, opts=None, ask=input):
    o = opts or {}
    rep = Report()
    present = check_presence(io, rep)
    check_static(io, rep, present, role=role,
                 noise_warn=o.get("noise_warn", NOISE_WARN), noise_fail=o.get("noise_fail", NOISE_FAIL))
    if sweep and present:
        check_sweep(io, rep, present, min_deg=o.get("min_deg", SWEEP_MIN_DEG),
                    jump_ticks=o.get("jump_ticks", JUMP_TICKS), ask=ask, enter_pressed=o.get("enter_pressed"))
    finalize(rep)
    return rep


def print_report(rep, port):
    print(f"\n포트: {port}")
    if rep.power:
        p = rep.power
        print(f"전원: {p['system']} 계통 (중앙값 {p['median_v']} V, 정상 범위 {p['range_v'][0]}~{p['range_v'][1]} V)")
    print(f"{'ID':>2}  {'관절':12s} {'모델':>5} {'FW':>6} {'전압':>6} {'온도':>5} {'토크':>4} "
          f"{'흔들림':>5} {'통신':>7}  결과")
    print("-" * 78)
    for j in JOINTS:
        m = rep.motors[j]

        def f(k, w, fmt="{}"):
            v = m.get(k)
            return f"{'-' if v is None else fmt.format(v):>{w}}"
        mark = {OK: "정상", INFO: "정상", WARN: "주의", FAIL: "불량"}[rep.joint_level(j)]
        print(f"{m['id']:>2}  {j:14s} {f('model', 5)} {f('fw', 6)} {f('voltage_v', 6, '{:.1f}V')} "
              f"{f('temp_c', 5, '{}°')} {f('torque', 4)} {f('noise_ticks', 6)} {f('comm', 7)}  {mark}")
    if rep.findings:
        print()
        for lvl, j, chk, msg in sorted(rep.findings, key=lambda f: -RANK[f[0]]):
            print(f"  [{lvl:4s}] {(j or '팔 전체'):14s} {chk:6s} {msg}")
    verdict, _ = rep.verdict()
    print(f"\n판정: {verdict}")


def list_ports():
    """USB 시리얼 포트 — 보드 시리얼 번호 기반 이름(by-id)이 있으면 그것들, 없으면 장치 이름."""
    ports = P.list_serial_ports()
    by_id = sorted(e["by_id"] for e in ports if e["by_id"])
    return by_id or sorted(e["dev"] for e in ports)


def main(argv=None):
    ap = argparse.ArgumentParser(description="SO-101 팔(보드 1개) 불량 점검")
    ap.add_argument("--port", help="서보 드라이버 보드 포트 (생략 시 하나뿐이면 자동)")
    ap.add_argument("--role", choices=("leader", "follower"),
                    help="리더/팔로워 — 주면 5V/12V 계통이 맞는지 같이 판정합니다")
    ap.add_argument("--sweep", action="store_true", help="손으로 관절 쓸기 시험 (토크 OFF)")
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    ap.add_argument("--noise-warn", type=int, default=NOISE_WARN, help="정지 흔들림 주의 기준 (tick)")
    ap.add_argument("--noise-fail", type=int, default=NOISE_FAIL, help="정지 흔들림 불량 기준 (tick)")
    a = ap.parse_args(argv)

    port = a.port
    if not port:
        ports = list_ports()
        if len(ports) != 1:
            print("포트를 지정하세요 (--port). 보이는 포트:\n  " + ("\n  ".join(ports) or "(없음)"), file=sys.stderr)
            return 3
        port = ports[0]
    if not (P.port_exists(port) or P.is_sim_port(port)):
        print(f"포트가 없습니다: {port}", file=sys.stderr)
        return 3
    try:
        io = BusIO(port)
    except Exception as e:
        print(f"포트를 열 수 없습니다: {port}\n  {type(e).__name__}: {e}\n"
              "  OpenMimic 의 Control / Collect / Setup 포트 감시가 잡고 있으면 먼저 끄세요.", file=sys.stderr)
        return 3
    try:
        if not a.json:
            print(f"점검 중… ({port})" + ("" if a.sweep else "  — 서보에 아무것도 쓰지 않습니다"))
        rep = run(io, role=a.role, sweep=a.sweep,
                  opts={"noise_warn": a.noise_warn, "noise_fail": a.noise_fail})
    except KeyboardInterrupt:
        print("\n중단됨", file=sys.stderr)
        return 3
    finally:
        io.close()
    if a.json:
        print(json.dumps({"port": port, "role": a.role, **rep.as_dict()}, ensure_ascii=False, indent=1))
    else:
        print_report(rep, port)
    return rep.verdict()[1]


if __name__ == "__main__":
    sys.exit(main())
