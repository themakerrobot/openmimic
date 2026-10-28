"""OpenMimic 서보 상태 감시 — 시작 전 전원 점검과 실행 중 온도·전압·보호 플래그 감시.

1) 시작 전 전원 점검 (power_check)
   제어·수집·실행을 시작하기 전에 팔(보드)마다 서보 전압을 읽어, 위험한 조합이면 시작을 막습니다.
   - SO-ARM101 리더가 12V 계통이면 막음 (리더 모터는 저전압 판 — 어댑터가 바뀐 것)
   - 서보 EEPROM 의 전압 한계(Min/Max_Voltage_Limit) 밖이면 막음 — 서보가 스스로 보호에 걸리는 값
   - SO-ARM101 팔로워가 5V 계통, 계통 범위 밖, 지난번과 크게 다름 → 경고만

2) 실행 중 감시 (HealthGuard)
   제어 루프와 같은 스레드에서 PERIOD_S 마다 팔로워 버스의 온도·전압·보호 플래그를 읽습니다.
   반이중 버스라 다른 스레드에서 읽으면 패킷이 섞이므로, 호출하는 쪽이 루프 안에서 poll() 을 부릅니다.
   정지 판정이 나오면 호출한 쪽이 멈춥니다 (실행 = 중지 버튼과 같은 SIGINT, 수집 = 끝내기).

판정 근거 — 출처가 있는 값과 서보 자신의 설정값(EEPROM)만 씁니다
  [EEPROM]  서보의 Max_Temperature_Limit(Feetech) / Temperature_Limit(Dynamixel), Min/Max_Voltage_Limit.
            값이 0 이면(읽지 못함·설정 없음) 그 항목은 판정하지 않습니다.
  [Seeed]   tools_armcheck 와 같은 출처: 5V 계통 4.5~5.5 V, 12V 계통 10.5~13.5 V, 7.0 V 미만이면 5V 계통
  [SDK]     Feetech Status 비트 / [ROBOTIS] Dynamixel Hardware_Error_Status 비트
  [경험값]  온도 경고·정지 여유, 전압 강하 비율 — 데이터시트 값이 아닙니다. 아래 상수로 조정.
"""
import json
import os
import statistics
import threading
import time
from pathlib import Path

import openmimic_platform as P

PERIOD_S = 1.0               # 실행 중 읽기 주기 (s) — 레지스터 3개 × 팔 수만큼 버스 왕복이 늘어납니다
TEMP_WARN_MARGIN = 10        # [경험값] 온도 한계보다 이만큼(°C) 아래부터 경고 (STS3215 공장 한계 70 → 60 = Seeed 권장 상한)
TEMP_STOP_MARGIN = 5         # [경험값] 온도 한계보다 이만큼 아래에서 멈춤 — 서보가 스스로 토크를 빼기 전에
SAG_WARN, SAG_STOP = 0.10, 0.20   # [경험값] 시작 때보다 전압이 이 비율 이상 떨어지면 경고 / 멈춤
REMEMBER_DIFF = 0.20         # [경험값] 지난번 기록과 이 비율 이상 다르면 '어댑터가 바뀌었을 수 있음' 경고
VOLT_SYSTEM_SPLIT = 7.0      # [Seeed]
VOLT_RANGE = {"5V": (4.5, 5.5), "12V": (10.5, 13.5)}   # [Seeed] Feetech(SO-ARM101) 만

REGS = {
    "feetech": {"volt": "Present_Voltage", "temp": "Present_Temperature", "err": "Status",
                "tlim": "Max_Temperature_Limit", "vmin": "Min_Voltage_Limit", "vmax": "Max_Voltage_Limit",
                "bits": {1: "입력 전압 이상", 4: "과열 보호", 8: "과전류 보호", 32: "과부하 보호"}},     # [SDK]
    "dynamixel": {"volt": "Present_Input_Voltage", "temp": "Present_Temperature", "err": "Hardware_Error_Status",
                  "tlim": "Temperature_Limit", "vmin": "Min_Voltage_Limit", "vmax": "Max_Voltage_Limit",
                  "bits": {1: "입력 전압 이상", 4: "과열 보호", 8: "엔코더 이상", 16: "전기 충격 감지",
                           32: "과부하 보호"}},  # [ROBOTIS]
}


def bus_kind(bus):
    return "dynamixel" if "dynamixel" in type(bus).__name__.lower() else "feetech"


def _sread(bus, name):
    return {k: int(v) for k, v in bus.sync_read(name, normalize=False).items()}


def volt_system(v):
    return "5V" if v < VOLT_SYSTEM_SPLIT else "12V"


# ============================================================================ 1. 시작 전 전원 점검
def read_board_power(bus):
    """연결된 lerobot 버스에서 모터별 전압(V)과 EEPROM 전압 한계(V, 없으면 None)를 읽습니다."""
    R = REGS[bus_kind(bus)]
    volts = {k: v / 10.0 for k, v in _sread(bus, R["volt"]).items()}
    lim = {}
    for key in ("vmin", "vmax"):
        try:
            vals = [v for v in _sread(bus, R[key]).values() if v > 0]
            lim[key] = (max(vals) if key == "vmin" else min(vals)) / 10.0 if vals else None
        except Exception:      # noqa: BLE001 — 한계값을 못 읽으면 그 판정만 건너뜁니다
            lim[key] = None
    return {"volts": volts, "median": round(statistics.median(volts.values()), 2) if volts else None, **lim}


def judge_power(robot, role, p, remembered=None):
    """(수준, 문장 목록) — 수준은 ok | warn | block."""
    msgs, level = [], "ok"

    def add(lv, m):
        nonlocal level
        msgs.append(m)
        if lv == "block" or (lv == "warn" and level == "ok"):
            level = lv

    med = p.get("median")
    if med is None:
        return "warn", ["전압을 읽지 못했습니다"]
    lo_v, hi_v = min(p["volts"].values()), max(p["volts"].values())
    if p.get("vmax") and hi_v > p["vmax"]:
        add("block", f"{hi_v:.1f} V — 서보 전압 한계 {p['vmax']:.1f} V 를 넘습니다. 즉시 전원을 분리하고 어댑터를 확인하세요")
    if p.get("vmin") and lo_v < p["vmin"]:
        add("block", f"{lo_v:.1f} V — 서보 최저 전압 {p['vmin']:.1f} V 보다 낮습니다. 어댑터·케이블을 확인하세요")
    if robot == "so101":
        sysname = volt_system(med)
        if role == "leader" and sysname == "12V":
            add("block", f"리더 팔에 {med:.1f} V (12V 계통) — 리더와 팔로워 어댑터가 바뀐 것 같습니다. 즉시 전원을 분리하세요")
        elif role == "follower" and sysname == "5V":
            add("warn", f"팔로워 팔에 {med:.1f} V (5V 계통) — 12V 팔이면 힘이 모자랍니다. 어댑터를 확인하세요")
        lo, hi = VOLT_RANGE[sysname]
        if not lo <= med <= hi:
            add("warn", f"{med:.1f} V — {sysname} 계통 범위 {lo}~{hi} V 밖 (어댑터·케이블 확인)")
    if remembered and abs(med - remembered) / remembered >= REMEMBER_DIFF:
        add("warn", f"지난번 기록 {remembered:.1f} V, 지금 {med:.1f} V — 어댑터가 바뀌지 않았는지 확인하세요")
    return level, msgs


class PowerMemory:
    """팔(역할·캘리브 id)마다 마지막 정상 전압을 기억합니다 — 다음 시작 때 '지난번과 다름' 경고용."""

    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        try:
            d = json.loads(P.read_text(self.path))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, key):
        v = self.load().get(key)
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    def put(self, key, volt):
        d = self.load()
        d[key] = round(float(volt), 2)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")   # 동시 저장끼리 안 겹치게
        tmp.write_text(json.dumps(d, indent=1))
        P.replace_file(tmp, self.path)


# ============================================================================ 2. 실행 중 감시
class HealthGuard:
    """buses = [(접두사, lerobot 버스)] — 한팔은 [("", bus)], 양팔은 [("left", …), ("right", …)].
    호출하는 쪽이 제어 루프 안에서 poll() 을 부릅니다 (PERIOD_S 가 안 지났으면 바로 돌아옴)."""

    def __init__(self, buses, period=PERIOD_S):
        self.buses = list(buses)
        self.period = period
        self.tlim, self.vmin, self.vmax = {}, {}, {}     # 접두사 → 값 (팔 단위, 모터 중 가장 엄격한 값)
        self.base = {}                                    # 접두사 → 시작 전압 중앙값
        self.temp, self.volt = {}, {}                     # 표시 이름 → 값
        self.level, self.msgs, self.stop_reason = "ok", [], ""   # stop_reason 은 처음 멈춘 이유 (계속 남김)
        self.stopping = ""                                       # 이번 측정에서 정지 조건인 문장 (조건이 풀리면 "")
        self.read_fail = 0
        self.err0 = {}                                    # 시작 때 이미 서 있던 보호 비트 — 새로 선 비트만 멈춤
        self._last = 0.0
        self._started = False

    def _key(self, pfx, motor):
        return f"{pfx}_{motor}" if pfx else motor

    def start(self):
        """EEPROM 한계값을 읽고 첫 측정으로 기준 전압을 잡습니다. 연결 직후 한 번."""
        for pfx, bus in self.buses:
            R = REGS[bus_kind(bus)]
            try:
                for m, e in _sread(bus, R["err"]).items():
                    self.err0[self._key(pfx, m)] = e
            except Exception:      # noqa: BLE001
                pass
            for key, dst, pick in (("tlim", self.tlim, min), ("vmin", self.vmin, max), ("vmax", self.vmax, min)):
                try:
                    vals = [v for v in _sread(bus, R[key]).values() if v > 0]
                except Exception:      # noqa: BLE001
                    vals = []
                if vals:
                    dst[pfx] = pick(vals) / (10.0 if key != "tlim" else 1)
        self._started = True
        self.poll(force=True)
        for pfx, _ in self.buses:
            vs = [v for k, v in self.volt.items() if (k.startswith(pfx + "_") if pfx else True)]
            if vs:
                self.base[pfx] = statistics.median(vs)

    def poll(self, force=False):
        """새로 정지 판정이 나면 그 이유(문장), 아니면 ""."""
        now = time.monotonic()
        if not force and (not self._started or now - self._last < self.period):
            return ""
        self._last = now
        msgs = []          # (수준, 문장)
        for pfx, bus in self.buses:
            R = REGS[bus_kind(bus)]
            side = f"{pfx} " if pfx else ""
            try:
                temps = _sread(bus, R["temp"])
                volts = {k: v / 10.0 for k, v in _sread(bus, R["volt"]).items()}
                errs = _sread(bus, R["err"])
            except Exception:      # noqa: BLE001 — 한 번 못 읽은 것으로 멈추지 않습니다 (통신 누락은 드물게 생김)
                self.read_fail += 1
                continue
            for m, t in temps.items():
                self.temp[self._key(pfx, m)] = t
            for m, v in volts.items():
                self.volt[self._key(pfx, m)] = v
            for m, e in errs.items():
                old = self.err0.get(self._key(pfx, m), 0)
                new = [n for b, n in R["bits"].items() if e & b and not old & b]
                kept = [n for b, n in R["bits"].items() if e & b and old & b]
                if new:
                    msgs.append(("stop", f"{side}{m}: 서보 보호 동작 ({', '.join(new)})"))
                if kept:
                    msgs.append(("warn", f"{side}{m}: 시작 때부터 보호 플래그 ({', '.join(kept)}) — 끝나면 팔 불량 점검"))
            tl = self.tlim.get(pfx)
            if tl and temps:
                m, t = max(temps.items(), key=lambda kv: kv[1])
                if t >= tl - TEMP_STOP_MARGIN:
                    msgs.append(("stop", f"{side}{m}: {t}°C — 서보 온도 한계 {tl:g}°C 에 가까움. 식힌 뒤 막힘·과부하 원인 확인"))
                elif t >= tl - TEMP_WARN_MARGIN:
                    msgs.append(("warn", f"{side}{m}: {t}°C — 뜨거워지는 중 (한계 {tl:g}°C)"))
            if volts:
                lo = min(volts.values())
                vmin, vmax = self.vmin.get(pfx), self.vmax.get(pfx)
                if vmin and lo < vmin:
                    msgs.append(("stop", f"{side}공급 전압 {lo:.1f} V — 서보 최저 전압 {vmin:.1f} V 아래. 어댑터 용량·케이블 확인"))
                if vmax and max(volts.values()) > vmax:
                    msgs.append(("stop", f"{side}공급 전압 {max(volts.values()):.1f} V — 서보 전압 한계 {vmax:.1f} V 초과. 전원 분리"))
                base = self.base.get(pfx)
                if base:
                    sag = (base - lo) / base
                    if sag >= SAG_STOP:
                        msgs.append(("stop", f"{side}공급 전압 {lo:.1f} V — 시작 때 {base:.1f} V 에서 {sag * 100:.0f}% 떨어짐. "
                                     "어댑터 전류가 모자라거나 커넥터 접촉 불량"))
                    elif sag >= SAG_WARN:
                        msgs.append(("warn", f"{side}공급 전압 {lo:.1f} V — 시작 때 {base:.1f} V 에서 {sag * 100:.0f}% 떨어짐 "
                                     "(여러 관절이 동시에 힘을 쓸 때 전원 강하)"))
        self.msgs = [m for _, m in msgs]
        self.stopping = next((m for lv, m in msgs if lv == "stop"), "")
        newly = ""
        if self.stopping:
            if not self.stop_reason:
                newly = self.stop_reason = self.stopping
            self.level = "stop"
        else:
            self.level = "warn" if msgs else "ok"
        return newly

    def state(self):
        return {"level": self.level, "msgs": self.msgs[:6], "stop_reason": self.stop_reason,
                "maxtemp": max(self.temp.values()) if self.temp else None,
                "minvolt": round(min(self.volt.values()), 1) if self.volt else None,
                "temp": dict(self.temp), "volt": dict(self.volt)}
