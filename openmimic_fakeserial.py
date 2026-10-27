"""가상 팔 시리얼 포트 (sim://<이름>) — Linux·Windows 공통.

가상 팔 도구(tools_simarms.py)는 보드(Feetech STS3215 · Dynamixel X 흉내)와 MuJoCo 장면을 한 프로세스에 둡니다.
서버·수집/실행 worker·점검 도구 등 여러 프로세스가 같은 가상 팔을 쓰므로(캘리브레이션 레지스터·물리 상태를 공유)
보드를 프로세스마다 따로 만들지 않고, 각 프로세스의 FakeSerial 이 127.0.0.1 소켓으로 그 보드에 바이트를 주고받습니다.
PTY·com0com 같은 OS 기능이나 드라이버가 필요 없습니다.

    import openmimic_fakeserial; openmimic_fakeserial.install()   # 시리얼을 여는 프로세스마다 한 번

install() 뒤에는 serial.Serial(port="sim://main_follower", …) 만 FakeSerial 이 되고,
실제 포트(/dev/ttyACM0 · COM5 …)는 원래 pyserial 객체가 그대로 만들어집니다.
scservo_sdk · dynamixel_sdk 의 PortHandler 가 serial.Serial(…) 을 만들므로 lerobot 코드는 손대지 않습니다.

연결 절차: 클라이언트가 "OMSIM1 <토큰> <보드 이름>\\n" 을 보내면 서버가 "OK\\n" — 그 뒤로는 시리얼과 같은 바이트 스트림.
주소·토큰은 가상 팔 도구가 openmimic_sim.json 의 "endpoint" 에 씁니다.
"""
from __future__ import annotations

import json
import secrets
import select
import socket
import threading
import time
from pathlib import Path

import openmimic_platform as P

SIM_FILE = Path(__file__).resolve().parent / "openmimic_sim.json"     # tools_simarms.py 가 켜져 있을 때만 있음
PREFIX = P.SIM_PREFIX
HELLO = "OMSIM1"
_installed = False


def is_sim_port(port) -> bool:
    return P.is_sim_port(port)


def port_name(port: str) -> str:
    """sim://main_follower → main_follower"""
    return port[len(PREFIX):]


def sim_info() -> dict:
    """가상 팔 도구가 켜져 있으면 상태 파일 내용, 아니면 {}."""
    try:
        d = json.loads(SIM_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(d, dict) or not P.pid_exists(d.get("pid")):
        return {}
    return d


def sim_port_alive(port: str) -> bool:
    """그 가상 포트가 지금 열 수 있는지 (가상 팔 도구가 켜져 있고 그 보드가 있음)."""
    if not is_sim_port(port):
        return False
    names = {p.get("name") for p in sim_info().get("ports", [])}
    return port_name(port) in names


# ============================================================================ 클라이언트 (serial.Serial 대역)
class FakeSerial:
    """pyserial Serial 과 같은 이름의 속성·메서드 (servo SDK 가 쓰는 범위 + 흔히 쓰는 것).
    timeout: None = size 만큼 올 때까지 기다림, 0 = 있는 만큼 바로, 양수 = 그 시간까지 기다림 (pyserial 과 같음)."""

    def __init__(self, port=None, baudrate=9600, bytesize=8, parity="N", stopbits=1, timeout=None,
                 xonxoff=False, rtscts=False, write_timeout=None, dsrdtr=False, inter_byte_timeout=None,
                 exclusive=None, **_kw):
        self._port = port
        self.baudrate = baudrate
        self.bytesize, self.parity, self.stopbits = bytesize, parity, stopbits
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.xonxoff, self.rtscts, self.dsrdtr = xonxoff, rtscts, dsrdtr
        self.is_open = False
        self._sock = None
        self._buf = bytearray()
        if port is not None:
            self.open()

    # ---------------------------------------------------------------- 열기·닫기
    @property
    def port(self):
        return self._port

    @port.setter
    def port(self, value):
        was = self.is_open
        if was:
            self.close()
        self._port = value
        if was:
            self.open()

    @property
    def name(self):
        return self._port

    def open(self):
        import serial
        if self.is_open:
            raise serial.SerialException("Port is already open.")
        if not is_sim_port(self._port):
            raise serial.SerialException(f"가상 포트가 아닙니다: {self._port}")
        info = sim_info()
        ep = info.get("endpoint") or {}
        if not ep.get("port"):
            raise serial.SerialException(f"could not open port {self._port}: 가상 장치가 꺼져 있습니다")
        try:
            s = socket.create_connection((ep.get("host", "127.0.0.1"), int(ep["port"])), timeout=3)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.sendall(f"{HELLO} {ep.get('token', '')} {port_name(self._port)}\n".encode())
            reply = b""
            while not reply.endswith(b"\n") and len(reply) < 128:
                chunk = s.recv(128 - len(reply))
                if not chunk:
                    break
                reply += chunk
        except OSError as e:
            raise serial.SerialException(f"could not open port {self._port}: {e}") from e
        if reply.strip() != b"OK":
            s.close()
            raise serial.SerialException(f"could not open port {self._port}: {reply.decode(errors='ignore').strip()}")
        s.setblocking(False)
        self._sock = s
        self._buf.clear()
        self.is_open = True

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.is_open = False

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    # ---------------------------------------------------------------- 읽기·쓰기
    def _check(self):
        if not self.is_open:
            import serial
            raise serial.PortNotOpenError()

    def _pump(self, wait):
        """소켓에 온 바이트를 버퍼로. wait 초까지 기다립니다 (0 = 기다리지 않음, None = 올 때까지)."""
        import serial
        r, _, _ = select.select([self._sock], [], [], wait)
        if not r:
            return
        try:
            data = self._sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as e:
            self.close()
            raise serial.SerialException(f"{self._port}: 가상 장치 연결이 끊겼습니다 ({e})") from e
        if not data:
            self.close()
            raise serial.SerialException(f"{self._port}: 가상 장치가 꺼졌습니다")
        self._buf += data

    @property
    def in_waiting(self):
        self._check()
        self._pump(0)
        return len(self._buf)

    def inWaiting(self):        # pyserial 옛 이름
        return self.in_waiting

    @property
    def out_waiting(self):
        return 0

    def read(self, size=1):
        self._check()
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        self._pump(0)
        while len(self._buf) < size:
            if deadline is None:
                self._pump(None)
                continue
            left = deadline - time.monotonic()
            if left <= 0:
                break
            self._pump(left)
        out = bytes(self._buf[:size])
        del self._buf[:size]
        return out

    def read_all(self):
        self._check()
        self._pump(0)
        out = bytes(self._buf)
        self._buf.clear()
        return out

    def read_until(self, expected=b"\n", size=None):
        out = bytearray()
        while True:
            c = self.read(1)
            if not c:
                break
            out += c
            if out.endswith(expected) or (size is not None and len(out) >= size):
                break
        return bytes(out)

    readline = read_until

    def write(self, data):
        self._check()
        data = bytes(data)
        try:
            self._sock.setblocking(True)
            self._sock.sendall(data)
        except OSError as e:
            import serial
            self.close()
            raise serial.SerialException(f"{self._port}: 가상 장치 연결이 끊겼습니다 ({e})") from e
        finally:
            if self._sock is not None:
                self._sock.setblocking(False)
        return len(data)

    def flush(self):
        self._check()

    def reset_input_buffer(self):
        self._check()
        self._pump(0)
        self._buf.clear()

    def reset_output_buffer(self):
        self._check()

    flushInput = reset_input_buffer
    flushOutput = reset_output_buffer

    def fileno(self):
        self._check()
        return self._sock.fileno()

    # 제어선은 가상 보드에 의미가 없습니다 — 설정만 받아 둡니다
    dtr = rts = True
    break_condition = False
    cts = dsr = ri = cd = False

    def send_break(self, duration=0.25):
        self._check()

    def __repr__(self):
        return f"FakeSerial(port={self._port!r}, open={self.is_open})"


def install():
    """serial.Serial(port="sim://…") 만 FakeSerial 로. 실제 포트는 원래 클래스 그대로 (isinstance 도 둘 다 통과)."""
    global _installed
    if _installed:
        return
    try:
        import serial
    except ImportError:
        return
    real = serial.Serial

    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, (real, FakeSerial))

        def __subclasscheck__(cls, sub):
            return issubclass(sub, (real, FakeSerial))

    class Serial(metaclass=_Meta):
        """serial.Serial(…) — sim:// 포트면 FakeSerial, 아니면 원래 pyserial Serial 객체를 그대로 돌려줍니다."""

        def __new__(cls, *a, **k):
            port = k.get("port", a[0] if a else None)
            if is_sim_port(port):
                return FakeSerial(*a, **k)
            return real(*a, **k)

    Serial.real = real
    serial.Serial = Serial
    _installed = True


# ============================================================================ 서버 (tools_simarms 쪽)
class SimPortServer:
    """가상 보드들을 127.0.0.1 의 TCP 포트 하나로 내놓습니다. 연결마다 스레드 하나.
    handlers = {보드 이름: handle(buf: bytearray) -> list[bytes]} — buf 에서 완성된 패킷을 꺼내 응답들을 돌려줌."""

    def __init__(self, handlers):
        self.handlers = handlers
        self.token = secrets.token_hex(12)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.host, self.port = self.sock.getsockname()[:2]
        threading.Thread(target=self._accept, daemon=True).start()

    def endpoint(self):
        return {"host": self.host, "port": self.port, "token": self.token}

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(3)
            line = b""
            while not line.endswith(b"\n") and len(line) < 256:
                chunk = conn.recv(256 - len(line))
                if not chunk:
                    return
                line += chunk
            parts = line.decode(errors="ignore").split()
            if len(parts) != 3 or parts[0] != HELLO or not secrets.compare_digest(parts[1], self.token):
                conn.sendall(b"NO bad hello\n")
                return
            handle = self.handlers.get(parts[2])
            if handle is None:
                conn.sendall(f"NO 없는 보드 {parts[2]}\n".encode())
                return
            conn.sendall(b"OK\n")
            conn.settimeout(None)
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            buf = bytearray()
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
                out = handle(buf)
                if out:
                    conn.sendall(b"".join(out))
        except OSError:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
