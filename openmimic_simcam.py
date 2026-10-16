"""OpenMimic 가상 카메라 — 가상 팔(tools_simarms)과 함께 쓰는 합성 영상 카메라.

카메라 경로가 openmimic_sim/cam_<이름> 이면 실제 장치를 열지 않고 합성 영상을 돌려줍니다.
그 밖의 경로·번호는 원래 cv2.VideoCapture 객체가 그대로 만들어집니다 (실제 카메라는 전혀 바뀌지 않음).

    import openmimic_simcam; openmimic_simcam.install()     # cv2 를 쓰는 프로세스마다 한 번

cv2.VideoCapture 를 Python 에서 상속하면 OpenCV 바인딩의 객체 해제와 맞지 않아 메모리가 깨질 수 있습니다.
그래서 상속하지 않고, cv2.VideoCapture 자리에 '생성은 경로에 따라 나눠 주고 isinstance 는 둘 다 통과시키는'
작은 클래스를 둡니다. lerobot OpenCVCamera 는 isinstance(…, cv2.VideoCapture) 로 연결 여부를 봅니다.
"""
import math
import os
import time
import zlib
from pathlib import Path

SIM_DIR = Path(__file__).resolve().parent / "openmimic_sim"
PREFIX = str(SIM_DIR / "cam_")
_installed = False


def sim_cam_name(target):
    """가상 카메라 경로면 이름, 아니면 None."""
    if isinstance(target, (str, os.PathLike)):
        s = os.fspath(target)
        if s.startswith(PREFIX):
            return s[len(PREFIX):] or "cam"
    return None


class SimCapture:
    """cv2.VideoCapture 와 같은 메서드 이름 — 합성 영상을 fps 에 맞춰 돌려줍니다."""

    def __init__(self, name, cv2, np):
        self._cv2, self._np = cv2, np
        self.name = name
        self._p = {cv2.CAP_PROP_FRAME_WIDTH: 640.0, cv2.CAP_PROP_FRAME_HEIGHT: 480.0, cv2.CAP_PROP_FPS: 30.0,
                   cv2.CAP_PROP_FOURCC: float(cv2.VideoWriter_fourcc(*"MJPG"))}
        self._open = True
        self._last = 0.0
        h = zlib.crc32(name.encode())
        self._bg = (40 + h % 60, 40 + (h >> 8) % 60, 40 + (h >> 16) % 60)

    def isOpened(self):
        return self._open

    def set(self, prop, value):
        self._p[prop] = float(value)
        return True

    def get(self, prop):
        return self._p.get(prop, 0.0)

    def getBackendName(self):
        return "OPENMIMIC_SIM"

    def _frame(self):
        cv2, np = self._cv2, self._np
        w = int(self._p[cv2.CAP_PROP_FRAME_WIDTH])
        h = int(self._p[cv2.CAP_PROP_FRAME_HEIGHT])
        img = np.empty((h, w, 3), np.uint8)
        img[:] = self._bg
        t = time.monotonic()
        # 바닥 격자 + 움직이는 물체 두 개 (정지 영상이면 미리보기가 멈춘 것처럼 보입니다)
        for x in range(0, w, max(8, w // 8)):
            cv2.line(img, (x, h // 2), (x, h - 1), (90, 90, 90), 1)
        cx = int(w / 2 + w / 4 * math.sin(t * 0.7))
        cy = int(h * 0.65 + h / 10 * math.cos(t * 0.9))
        cv2.circle(img, (cx, cy), max(4, w // 16), (60, 200, 230), -1)
        bx = int(w / 2 + w / 3 * math.cos(t * 0.4))
        cv2.rectangle(img, (bx - w // 20, int(h * 0.4)), (bx + w // 20, int(h * 0.4) + h // 8), (80, 160, 80), -1)
        fs = max(0.35, w / 900)
        cv2.putText(img, f"SIM {self.name}", (8, int(22 * fs / 0.7)), cv2.FONT_HERSHEY_SIMPLEX, fs,
                    (235, 235, 235), 1, cv2.LINE_AA)
        return img

    def read(self, *a):
        if not self._open:
            return False, None
        fps = self._p.get(self._cv2.CAP_PROP_FPS) or 30.0
        dt = 1.0 / fps - (time.monotonic() - self._last)
        if dt > 0:
            time.sleep(dt)
        self._last = time.monotonic()
        return True, self._frame()

    def grab(self):
        return self._open

    def retrieve(self, *a):
        return self.read()

    def release(self):
        self._open = False


def install():
    global _installed
    if _installed:
        return
    try:
        import cv2
        import numpy as np
    except ImportError:
        return
    real = cv2.VideoCapture

    class _Meta(type):
        def __instancecheck__(cls, obj):
            return isinstance(obj, (real, SimCapture))

    class VideoCapture(metaclass=_Meta):
        """cv2.VideoCapture(…) — 가상 경로면 SimCapture, 아니면 원래 cv2.VideoCapture 객체를 그대로 돌려줍니다."""

        def __new__(cls, *a, **k):
            name = sim_cam_name(a[0]) if a else None
            if name is not None:
                return SimCapture(name, cv2, np)
            return real(*a, **k)

    cv2.VideoCapture = VideoCapture
    _installed = True
