#!/usr/bin/env python
"""OpenMimic 롤아웃 실행기 — lerobot-rollout 을 같은 프로세스에서 그대로 돌리면서
웹 화면용 실시간 상태(카메라·관절 실측/명령·추론 시간·제어 주기)를 파일로 내보냅니다.

  python openmimic_rollout.py --openmimic.run_dir=<RUN_DIR/jid> [--openmimic.engine=torch|ov]
                          [--ov.device=NPU --ov.precision=fp16 --ov.fps=30]
                          <lerobot-rollout 인자 그대로...>

  출력 (OpenMimic 수집 worker 와 같은 규칙, 원자적 교체):
    <run_dir>/status.json     phase · 경과 · 관절 실측(obs)/명령(act) · 추론 ms · 제어 Hz
    <run_dir>/cam_<이름>.jpg  카메라 미리보기

로봇 연결·max_relative_target·중지(SIGINT) 시 시작 자세 복귀·토크 해제는 lerobot 코드 그대로입니다.
이 파일은 관찰만 합니다 — 로봇에 보내는 명령을 바꾸지 않습니다.
서보 상태 감시(openmimic_health)가 정지 판정을 내면 중지 버튼과 같은 SIGINT 를 자기 자신에게 보냅니다.
"""
import json
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import openmimic_platform as P

PREVIEW_FPS = 10


def say(*a):
    print(*a, flush=True)


def _split_key(k):
    """'left_shoulder_pan.pos' → ('left', 'shoulder_pan'), 'gripper.pos' → ('main', 'gripper')"""
    j = k[:-4] if k.endswith(".pos") else k
    for side in ("left", "right"):
        if j.startswith(side + "_"):
            return side, j[len(side) + 1:]
    return "main", j


def _joints(d):
    out = {}
    for k, v in (d or {}).items():
        if not k.endswith(".pos"):
            continue
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        side, j = _split_key(k)
        out.setdefault(side, {})[j] = round(fv, 2)
    return out


class Monitor:
    """lerobot 내부 몇 곳을 감싸 최신 값을 잡아 두고, 별도 스레드가 PREVIEW_FPS 로 파일을 씁니다.
    제어 루프에는 JPEG 인코딩·파일 쓰기 비용을 얹지 않습니다."""

    def __init__(self, rd, engine="torch", device="", precision=""):
        self.rd = Path(rd)
        self.rd.mkdir(parents=True, exist_ok=True)
        self.frames = {}
        self.obs, self.act = {}, {}
        self.ticks = deque(maxlen=60)          # get_observation 시각 → 실제 제어 주기
        self.calls = deque(maxlen=300)         # (시각, get_action ms)
        self.n_calls = 0
        self.status = {"phase": "loading", "t0": time.time(), "run_t0": None, "engine": engine,
                       "device": device, "precision": precision, "err": ""}
        self.extra = None                      # OVEngine 같은 곳에서 추가 통계 (callable)
        self.guard = None                      # openmimic_health.HealthGuard — 연결 뒤 생성
        self.on = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # --- lerobot 감싸기 ---
    def wrap_robot(self, robot):
        mon = self
        g, s, c = robot.get_observation, robot.send_action, robot.connect

        def get_observation(*a, **k):
            o = g(*a, **k)
            if mon.guard is not None:          # 제어 루프와 같은 스레드 — 반이중 버스에서 안전
                why = mon.guard.poll()
                if why:
                    say(f"[openmimic] 서보 상태 이상으로 멈춥니다: {why}")
                    mon.set(health_stop=why)
                    P.interrupt_self()     # 작업 탭 '중지' 와 같은 경로 (시작 자세 복귀·토크 해제)
            mon.ticks.append(time.monotonic())
            for key, v in o.items():
                if getattr(v, "ndim", 0) == 3:
                    mon.frames[key] = v
            mon.obs = o
            return o

        def send_action(action, *a, **k):
            mon.act = dict(action)
            return s(action, *a, **k)

        def connect(*a, **k):
            mon.set(phase="connecting")
            r = c(*a, **k)
            try:
                import openmimic_health
                buses = ([("left", robot.left_arm.bus), ("right", robot.right_arm.bus)]
                         if hasattr(robot, "left_arm") else [("", robot.bus)])
                guard = openmimic_health.HealthGuard(buses)
                guard.start()
                mon.guard = guard
            except Exception as e:      # noqa: BLE001 — 감시를 못 켜도 실행은 계속 (화면에 표시)
                say(f"[openmimic] 서보 상태 감시를 켜지 못했습니다: {e}")
                mon.set(health_err=str(e)[:200])
            mon.set(phase="running", run_t0=time.time())
            return r

        robot.get_observation, robot.send_action, robot.connect = get_observation, send_action, connect
        return robot

    def install(self):
        import lerobot.rollout.context as ctx
        orig_make = ctx.make_robot_from_config
        ctx.make_robot_from_config = lambda cfg: self.wrap_robot(orig_make(cfg))

        from lerobot.rollout.inference.sync import SyncInferenceEngine
        orig_get = SyncInferenceEngine.get_action
        mon = self

        def get_action(eng, obs_frame):
            t = time.perf_counter()
            r = orig_get(eng, obs_frame)
            mon.calls.append((time.monotonic(), (time.perf_counter() - t) * 1000))
            mon.n_calls += 1
            return r

        SyncInferenceEngine.get_action = get_action

        # 종료 단계 표시 — 각 전략의 teardown 이 공통으로 부르는 _teardown_hardware 를 감쌉니다
        from lerobot.rollout.strategies.core import RolloutStrategy
        td = RolloutStrategy._teardown_hardware

        def _teardown_hardware(strategy, hw, *a, **k):
            mon.set(phase="returning")
            return td(strategy, hw, *a, **k)

        RolloutStrategy._teardown_hardware = _teardown_hardware

    # --- 상태 ---
    def set(self, **kw):
        self.status.update(kw)

    def snapshot(self):
        st = dict(self.status)
        now = time.monotonic()
        st["elapsed"] = round(time.time() - st["run_t0"], 1) if st.get("run_t0") else 0.0
        tk = list(self.ticks)
        st["hz"] = round((len(tk) - 1) / (tk[-1] - tk[0]), 1) if len(tk) > 5 and tk[-1] > tk[0] else None
        recent = [ms for t, ms in self.calls if now - t < 5.0]
        if recent:
            srt = sorted(recent)
            st["tick_ms"] = round(srt[len(srt) // 2], 1)                  # 보통 틱 (큐에서 꺼내기)
            st["chunk_ms"] = round(srt[-1], 1)                            # 최근 5초 최대 ≈ 청크 계산
            st["p95_ms"] = round(srt[min(len(srt) - 1, int(len(srt) * 0.95))], 1)
        st["n_calls"] = self.n_calls
        st["obs"] = _joints(self.obs)
        st["act"] = _joints(self.act)
        st["cams"] = sorted(self.frames)
        if self.guard is not None:
            st["health"] = self.guard.state()
        if self.extra:
            try:
                st.update(self.extra())
            except Exception:      # noqa: BLE001 — 표시용 통계 실패는 무시
                pass
        return st

    def write(self):
        tmp = self.rd / "status.json.tmp"
        tmp.write_text(json.dumps(self.snapshot()))
        os.replace(tmp, self.rd / "status.json")

    def _loop(self):
        try:
            import cv2
        except ImportError:
            cv2 = None
        while self.on:
            t0 = time.monotonic()
            try:
                self.write()
                if cv2 is not None:
                    for k, frame in list(self.frames.items()):
                        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frame, cv2.COLOR_RGB2BGR),
                                               [cv2.IMWRITE_JPEG_QUALITY, 60])
                        if ok:
                            tmp = self.rd / f"cam_{k}.jpg.tmp"
                            tmp.write_bytes(buf.tobytes())
                            os.replace(tmp, self.rd / f"cam_{k}.jpg")
            except Exception:      # noqa: BLE001 — 미리보기 실패가 롤아웃을 멈추면 안 됩니다
                pass
            time.sleep(max(0.0, 1.0 / PREVIEW_FPS - (time.monotonic() - t0)))

    def stop(self, phase="done", err=""):
        self.set(phase=phase, err=err)
        self.on = False
        self.thread.join(timeout=2)
        try:
            self.write()
        except OSError:
            pass


def _pop_opts(argv, prefix):
    opts, rest = {}, []
    for a in argv:
        if a.startswith(prefix):
            k, _, v = a[len(prefix):].partition("=")
            opts[k] = v
        else:
            rest.append(a)
    return opts, rest


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    import openmimic_fakeserial
    import openmimic_simcam
    openmimic_simcam.install()          # 가상 카메라 경로(openmimic_sim/cam_*)면 합성 영상
    openmimic_fakeserial.install()      # 가상 팔 포트(sim://…)면 가상 팔 도구의 보드로
    P.forward_break_to_sigint()         # Windows 작업 탭 '중지'(CTRL_BREAK) = SIGINT 와 같은 정리 경로
    lw, rest = _pop_opts(argv, "--openmimic.")
    ov_opts, rest = _pop_opts(rest, "--ov.")
    engine = (lw.get("engine") or ("ov" if ov_opts else "torch")).lower()
    run_dir = lw.get("run_dir")
    mon = ov_eng = None
    if run_dir:
        mon = Monitor(run_dir, engine=engine, device=(ov_opts.get("device") or "").upper(),
                      precision=ov_opts.get("precision") or "")
    try:
        if engine == "ov":
            import openmimic_ov
            ov_eng, rest = openmimic_ov.prepare_rollout(ov_opts, rest)
            if mon:
                mon.set(device=ov_eng.device)
                mon.extra = lambda: {"ov_last_ms": round(ov_eng.ts[-1], 1) if ov_eng.ts else None,
                                     "ov_n": ov_eng.n, "ov_over": ov_eng.over}
        elif engine != "torch":
            raise SystemExit(f"--openmimic.engine 은 torch | ov: {engine}")
        rest = _skip_backbone_download(rest)
        _answer_calibration_prompt()
        if mon:
            mon.install()
        sys.argv = ["lerobot-rollout"] + rest
        from lerobot.scripts.lerobot_rollout import main as lr_main
        lr_main()
    except BaseException as e:
        _ov_summary(ov_eng)
        if mon:
            # SystemExit(0) / KeyboardInterrupt 는 정상 종료로 봅니다
            normal = isinstance(e, KeyboardInterrupt) or (isinstance(e, SystemExit) and not e.code)
            mon.stop("done" if normal else "error", "" if normal else f"{type(e).__name__}: {e}"[:300])
        raise
    _ov_summary(ov_eng)
    if mon:
        mon.stop("done")
    return 0


def _answer_calibration_prompt():
    """lerobot 은 모터 값이 캘리브레이션 파일과 다르면 input() 으로 묻습니다. 웹 작업은 stdin 이 없어 EOFError 로 죽습니다.
    'ENTER = 파일을 모터에 쓰기' 만 자동으로 답합니다 — OpenMimic 제어 탭의 연결(토크 OFF → 파일 캘리브레이션 쓰기)과
    같은 동작입니다. 처음부터 하는 캘리브레이션(팔을 움직여야 함)이 필요하면 멈추고 캘리브레이션 탭으로 안내합니다."""
    import builtins

    def _input(prompt=""):
        say(prompt)
        if "use provided calibration file" in str(prompt):
            say("[openmimic] 모터 값이 캘리브레이션 파일과 달라 파일 값을 모터에 씁니다 (제어 탭 연결과 같은 동작)")
            return ""
        raise EOFError("캘리브레이션 파일이 없습니다 — 캘리브레이션 탭에서 먼저 캘리브레이션하세요")

    builtins.input = _input


def _skip_backbone_download(rest):
    """ACT·Diffusion 은 모델을 만들 때 torchvision 이 ImageNet ResNet 가중치를 내려받습니다. 곧바로 체크포인트
    가중치로 덮어써지므로 쓸모가 없고, 인터넷이 없는 기기에서는 이 다운로드에서 죽습니다.
    (BatchNorm/GroupNorm 구조는 별도 설정(use_group_norm)이 정하므로 구조는 그대로입니다)"""
    pol = next((a.split("=", 1)[1] for a in rest if a.startswith("--policy.path=")), "")
    if not pol or any(a.startswith("--policy.pretrained_backbone_weights=") for a in rest):
        return rest
    try:
        cfg = json.loads((Path(pol) / "config.json").read_text())
    except (OSError, ValueError):
        return rest
    if cfg.get("pretrained_backbone_weights"):
        return rest + ["--policy.pretrained_backbone_weights=null"]
    return rest


def _ov_summary(eng):
    if eng is None or not eng.ts:
        return
    ts = sorted(eng.ts)
    say(f"[ov] 종료: 추론 {eng.n}회, 평균 {sum(ts) / len(ts):.1f} ms, "
        f"p95 {ts[min(len(ts) - 1, int(len(ts) * 0.95))]:.1f} ms, 예산 초과 {eng.over}회 @ {eng.device}")


if __name__ == "__main__":
    sys.exit(main())
