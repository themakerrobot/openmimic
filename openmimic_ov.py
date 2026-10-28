#!/usr/bin/env python
"""OpenMimic OpenVINO 모듈 — ACT·ACT2 체크포인트를 OpenVINO IR 로 변환·검증하고,
lerobot rollout 의 신경망 호출만 OpenVINO(NPU/GPU/CPU)로 바꿔 실행합니다.

  python openmimic_ov.py devices
  python openmimic_ov.py convert <.../pretrained_model> [--int8] [--fps 30]     (--ckpt=<경로> 도 가능)
  python openmimic_ov.py rollout --ov.dir=<.../pretrained_model/openvino> --ov.device=NPU [--ov.precision=fp16]
                             <lerobot-rollout 인자 그대로...>

설계
  * 정규화/역정규화는 체크포인트 안의 policy_preprocessor / policy_postprocessor (lerobot) 를
    그대로 씁니다. OpenVINO 로 바뀌는 건 ACT 신경망(백본 + 트랜스포머) 한 덩어리뿐입니다.
    → 데이터 형식·관절 순서·단위가 PyTorch 경로와 100% 같습니다.
  * 추론 시 VAE 인코더는 쓰지 않습니다(잠재 = 0). IR 에도 들어가지 않습니다.
  * NPU 는 정적 shape 만 받습니다. 입력 shape 는 학습 데이터의 카메라 해상도로 고정됩니다.
    카메라 해상도를 바꾸면 다시 변환해야 합니다.
  * rollout 은 lerobot-rollout 을 같은 프로세스에서 그대로 실행하고 ACTPolicy.predict_action_chunk
    만 교체합니다. 로봇 연결·max_relative_target·중지(SIGINT) 시 복귀/토크 해제 동작은 PyTorch 경로와 같습니다.

산출물: <pretrained_model>/openvino/
  act_fp16.xml/.bin   FP16 가중치 IR (NPU/GPU/CPU 공용)
  act_int8.xml/.bin   (--int8) NNCF 사후 양자화. 학습 데이터셋 프레임으로 보정합니다
  ov_meta.json        입력 이름·shape, 원본 체크포인트 지문, 장치별 정합성·지연 측정 결과
  cache/              NPU/GPU 컴파일 캐시 (두 번째 실행부터 컴파일이 빨라집니다)
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

import openmimic_platform as P

OV_SUBDIR = "openvino"
META = "ov_meta.json"
DEVICE_ORDER = ("NPU", "GPU", "CPU")
PRECISIONS = ("fp16", "int8")
N_PARITY = 8            # 정합성 비교 샘플 수
N_LAT = 40              # 지연 측정 반복 수
N_CALIB = 64            # INT8 보정 샘플 수
# 정규화 공간(평균 0, 표준편차 1) 기준 최대 오차 허용치 — 넘으면 "주의"
TOL = {"fp16": 0.05, "int8": 0.25}

log = logging.getLogger("openmimic_ov")


def say(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------- 공용 유틸
def ov_dir(ck):
    return Path(ck) / OV_SUBDIR


def _weights_file(ck):
    return Path(ck) / "model.safetensors"


def fingerprint(ck):
    """원본 가중치 파일 지문 — 재학습/덮어쓰기로 바뀌면 IR 이 낡은 것으로 판정됩니다."""
    st = _weights_file(ck).stat()
    return {"file": "model.safetensors", "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def load_meta(ck):
    try:
        return json.loads(P.read_text(ov_dir(ck) / META))
    except (OSError, ValueError):
        return None


def status(ck):
    """OpenMimic 화면용 요약: none | stale | ok (+ meta)"""
    meta = load_meta(ck)
    if not meta:
        return {"state": "none"}
    try:
        fresh = meta.get("source") == fingerprint(ck)
    except OSError:
        fresh = False
    have = [p for p in PRECISIONS if meta.get("ir", {}).get(p) and (ov_dir(ck) / meta["ir"][p]).is_file()]
    if not have:
        return {"state": "none"}
    return {"state": "ok" if fresh else "stale", "precisions": have, "meta": meta}


def devices():
    """[{id, name}] — openvino 미설치면 예외."""
    import openvino as ov
    core = ov.Core()
    out = []
    for d in core.available_devices:
        try:
            name = core.get_property(d, "FULL_DEVICE_NAME")
        except Exception:      # noqa: BLE001 — 드라이버마다 미지원 속성이 다릅니다
            name = d
        out.append({"id": d, "name": str(name)})
    return out


def _core(cache_dir=None):
    import openvino as ov
    core = ov.Core()
    if cache_dir:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        core.set_property({"CACHE_DIR": str(cache_dir)})
    return core


def _family(dev_id):
    return dev_id.split(".")[0]          # GPU.0 → GPU


def pick_devices(available, want):
    """want 를 맨 앞에, 그 뒤로 NPU → GPU → CPU 순 대체 후보 (want 가 CPU 면 CPU 만)."""
    fams = {}
    for d in available:
        fams.setdefault(_family(d), d)
    order = [want] + [d for d in DEVICE_ORDER if DEVICE_ORDER.index(d) > DEVICE_ORDER.index(want)] \
        if want in DEVICE_ORDER else list(DEVICE_ORDER)
    return [fams[f] for f in order if f in fams]


# ----------------------------------------------------------------- 정책 로드
OV_POLICY_TYPES = ("act", "act2")


def load_policy(ck):
    """체크포인트 → (policy, preprocessor, postprocessor). 모두 CPU 에 올립니다."""
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.factory import get_policy_class, make_pre_post_processors

    from lerobot.utils.import_utils import register_third_party_plugins
    register_third_party_plugins()      # act2 (plugins/lerobot_policy_act2) 를 lerobot 에 등록
    cfg = PreTrainedConfig.from_pretrained(ck)
    if cfg.type not in OV_POLICY_TYPES:
        raise SystemExit(f"ACT·ACT2 체크포인트만 변환합니다 (이 체크포인트: {cfg.type})")
    cfg.device = "cpu"
    # 백본 가중치는 체크포인트에 이미 들어 있습니다 — ImageNet/DINOv2 가중치를 다시 내려받지 않게 합니다(오프라인 기기 대비)
    cfg.pretrained_backbone_weights = None
    if getattr(cfg, "dinov2_pretrained_path", None):
        cfg.dinov2_pretrained_path = None
    if getattr(cfg, "action_head", "l1") == "flow" and not getattr(cfg, "flow_fixed_noise", True):
        say("  (ACT2 flow 헤드: flow_fixed_noise=false 라 샘플링에 난수가 들어갑니다 — 정합성 검증은 참고용)")
    policy = get_policy_class(cfg.type).from_pretrained(ck, config=cfg)
    policy.to("cpu").eval()
    pre, post = make_pre_post_processors(
        policy_cfg=cfg, pretrained_path=str(ck),
        preprocessor_overrides={"device_processor": {"device": "cpu"}},
        postprocessor_overrides={"device_processor": {"device": "cpu"}},
    )
    return policy, pre, post


def input_spec(cfg):
    """[(이름, shape)] — 순서가 곧 IR 입력 순서입니다."""
    from lerobot.utils.constants import OBS_ENV_STATE, OBS_STATE
    spec = []
    if cfg.robot_state_feature:
        spec.append((OBS_STATE, [1, *cfg.robot_state_feature.shape]))
    if cfg.env_state_feature:
        spec.append((OBS_ENV_STATE, [1, *cfg.env_state_feature.shape]))
    for k, f in cfg.image_features.items():
        spec.append((k, [1, *f.shape]))
    if not spec or (not cfg.robot_state_feature):
        raise SystemExit("observation.state 가 없는 ACT 는 지원하지 않습니다")
    return spec


def make_core_module(policy, spec):
    """policy.model 을 (state, [env], *images) → actions (1, chunk, A) 함수로 감쌉니다."""
    import torch
    from lerobot.utils.constants import OBS_ENV_STATE, OBS_IMAGES, OBS_STATE
    names = [n for n, _ in spec]
    img_names = [n for n in names if n not in (OBS_STATE, OBS_ENV_STATE)]

    class ACTCore(torch.nn.Module):
        def __init__(self, model):
            super().__init__()
            self.model = model

        def forward(self, *xs):
            d = dict(zip(names, xs))
            batch = {OBS_STATE: d[OBS_STATE]}
            if OBS_ENV_STATE in d:
                batch[OBS_ENV_STATE] = d[OBS_ENV_STATE]
            if img_names:
                batch[OBS_IMAGES] = [d[n] for n in img_names]
            return self.model(batch)[0]

    return ACTCore(policy.model).eval()


# ----------------------------------------------------------------- 샘플 데이터
def dataset_batches(ck, pre, spec, n):
    """학습 데이터셋에서 프레임 n 개를 고르게 뽑아 preprocessor(정규화)까지 통과시킨 입력 목록.
    데이터셋을 못 읽으면 None."""
    try:
        tc = json.loads((Path(ck) / "train_config.json").read_text())
        ds = tc.get("dataset") or {}
        from lerobot.datasets.lerobot_dataset import LeRobotDataset
        # 학습 기기의 절대경로가 그대로 적혀 있습니다. 다른 기기로 체크포인트를 옮겼으면
        # 이 기기의 OpenMimic 데이터 폴더(같은 이름)에서 찾습니다.
        name = ds["repo_id"].split("/", 1)[-1]
        roots = [ds.get("root"), Path(os.environ.get("OPENMIMIC_HOME") or Path(__file__).resolve().parent) / "data/hf/lerobot/local" / name]
        root = next((r for r in roots if r and (Path(r) / "meta/info.json").is_file()), None)
        if root is None:
            raise FileNotFoundError(f"데이터셋 '{name}' 없음 (찾은 곳: {', '.join(str(r) for r in roots if r)})")
        d = LeRobotDataset(ds["repo_id"], root=root)
        total = len(d)
        if total == 0:
            return None
        idx = sorted({int(i * (total - 1) / max(1, n - 1)) for i in range(n)})
        out = []
        for i in idx:
            item = pre(dict(d[i]))
            out.append([item[name].float().reshape(shape).contiguous() for name, shape in spec])
        return out
    except Exception as e:     # noqa: BLE001 — 데이터셋이 없어도 변환 자체는 가능합니다
        say(f"  (데이터셋 프레임 사용 불가 → 임의 입력으로 검증: {type(e).__name__}: {e})")
        return None


def random_batches(spec, n, seed=0):
    import torch
    g = torch.Generator().manual_seed(seed)
    # preprocessor 를 거친 뒤의 값(정규화 공간)을 흉내 냅니다 — 평균 0, 표준편차 1
    return [[torch.randn(*shape, generator=g) for _, shape in spec] for _ in range(n)]


# ----------------------------------------------------------------- 변환
def _set_input_names(model, spec):
    for port, (name, _) in zip(model.inputs, spec):
        port.get_tensor().set_names({name})
    model.outputs[0].get_tensor().set_names({"action"})


def _bench(compiled, batches, n_lat):
    import numpy as np
    req = compiled.create_infer_request()
    outs = [req.infer([b.numpy() for b in bat])[0].copy() for bat in batches]
    feed = [b.numpy() for b in batches[0]]
    for _ in range(3):
        req.infer(feed)
    ts = []
    for _ in range(n_lat):
        t = time.perf_counter()
        req.infer(feed)
        ts.append((time.perf_counter() - t) * 1000)
    ts = np.array(ts)
    return outs, {"mean_ms": round(float(ts.mean()), 2), "p95_ms": round(float(np.percentile(ts, 95)), 2)}


def convert(ck, int8=False, fps=30, n_lat=N_LAT):
    import numpy as np
    import openvino as ov
    import torch

    ck = Path(ck).resolve()
    if not (ck / "config.json").is_file() or not _weights_file(ck).is_file():
        raise SystemExit(f"체크포인트가 아닙니다: {ck}")
    od = ov_dir(ck)
    od.mkdir(parents=True, exist_ok=True)
    budget = 1000.0 / fps
    say(f"[1/5] 체크포인트 로드: {ck}")
    policy, pre, post = load_policy(ck)
    cfg = policy.config
    spec = input_spec(cfg)
    say("  입력: " + ", ".join(f"{n} {s}" for n, s in spec))
    say(f"  chunk_size={cfg.chunk_size}  n_action_steps={cfg.n_action_steps}  "
        f"temporal_ensemble={cfg.temporal_ensemble_coeff}")
    core_mod = make_core_module(policy, spec)

    say("[2/5] 검증용 입력 준비")
    batches = dataset_batches(ck, pre, spec, N_PARITY)
    source = "dataset" if batches else "random"
    if not batches:
        batches = random_batches(spec, N_PARITY)
    with torch.no_grad():
        ref = [core_mod(*b).numpy() for b in batches]
        t0 = time.perf_counter()
        for _ in range(5):
            core_mod(*batches[0])
        torch_ms = (time.perf_counter() - t0) * 1000 / 5
    say(f"  샘플 {len(batches)}개 ({'학습 데이터셋 프레임' if source == 'dataset' else '임의 입력'}), "
        f"출력 {list(ref[0].shape)}, PyTorch CPU {torch_ms:.1f} ms")

    say("[3/5] OpenVINO 변환 (정적 shape, FP16 가중치)")
    t0 = time.perf_counter()
    import warnings
    with torch.no_grad(), warnings.catch_warnings():
        warnings.simplefilter("ignore")      # TracerWarning — shape 고정 변환이라 무관합니다
        m = ov.convert_model(core_mod, example_input=tuple(batches[0]),
                             input=[ov.PartialShape(s) for _, s in spec])
    _set_input_names(m, spec)
    ov.save_model(m, str(od / "act_fp16.xml"), compress_to_fp16=True)
    say(f"  act_fp16.xml 저장 ({time.perf_counter() - t0:.1f}s)")
    ir = {"fp16": "act_fp16.xml"}

    if int8:
        say("[3b] INT8 양자화 (NNCF)")
        calib = dataset_batches(ck, pre, spec, N_CALIB) if source == "dataset" else None
        if not calib:
            say("  !! 학습 데이터셋을 못 읽어 INT8 보정을 할 수 없습니다 — FP16 만 사용하세요")
        else:
            import nncf
            q = nncf.quantize(m, nncf.Dataset(calib, lambda b: tuple(x.numpy() for x in b)),
                              model_type=nncf.ModelType.TRANSFORMER, subset_size=len(calib))
            ov.save_model(q, str(od / "act_int8.xml"))
            ir["int8"] = "act_int8.xml"
            say(f"  act_int8.xml 저장 (보정 {len(calib)} 프레임)")

    say("[4/5] 장치별 정합성·지연 측정")
    core = _core(od / "cache")
    avail = core.available_devices
    say("  사용 가능 장치: " + ", ".join(avail))
    results = {}
    for prec, xml in ir.items():
        model = core.read_model(str(od / xml))
        results[prec] = {}
        for dev in pick_devices(avail, "NPU"):
            r = {}
            try:
                t0 = time.perf_counter()
                compiled = core.compile_model(model, dev, {"PERFORMANCE_HINT": "LATENCY"})
                r["compile_s"] = round(time.perf_counter() - t0, 2)
                outs, lat = _bench(compiled, batches, n_lat)
                r.update(lat)
                diff = max(float(np.abs(o - e).max()) for o, e in zip(outs, ref))
                # 관절 단위(역정규화 후) 오차 — 화면에 보여 줄 값
                with torch.no_grad():
                    du = max(float((post(torch.from_numpy(o[0])) - post(torch.from_numpy(e[0]))).abs().max())
                             for o, e in zip(outs, ref))
                r.update(ok=True, max_abs_norm=round(diff, 5), max_abs_units=round(du, 4),
                         parity=diff <= TOL[prec], realtime=r["p95_ms"] <= budget)
                say(f"  {prec:4s} {dev:5s} 컴파일 {r['compile_s']:.1f}s | 평균 {r['mean_ms']:.1f} ms, "
                    f"p95 {r['p95_ms']:.1f} ms ({'≤' if r['realtime'] else '>'} {budget:.0f} ms) | "
                    f"최대오차 {diff:.4f}(정규화) / {du:.3f}(관절 단위) {'OK' if r['parity'] else '주의'}")
            except Exception as e:     # noqa: BLE001 — NPU 컴파일 실패 등은 결과로 기록합니다
                r.update(ok=False, error=f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}")
                say(f"  {prec:4s} {dev:5s} 실패: {r['error']}")
            results[prec][_family(dev)] = r

    say("[5/5] 메타 저장")
    try:
        import lerobot
        lr_ver = getattr(lerobot, "__version__", "?")
    except Exception:     # noqa: BLE001
        lr_ver = "?"
    meta = {
        "version": 1, "created": time.strftime("%F %T"), "source": fingerprint(ck),
        "inputs": [{"name": n, "shape": s} for n, s in spec],
        "output_shape": list(ref[0].shape), "chunk_size": cfg.chunk_size,
        "n_action_steps": cfg.n_action_steps, "temporal_ensemble_coeff": cfg.temporal_ensemble_coeff,
        "ir": ir, "openvino": ov.__version__, "lerobot": lr_ver, "torch_cpu_ms": round(torch_ms, 2),
        "fps": fps, "budget_ms": round(budget, 2), "parity_source": source, "tolerance": TOL,
        "results": results,
    }
    tmp = od / (META + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    P.replace_file(tmp, od / META)
    best = recommend(meta)
    say("완료. " + (f"권장: {best[1].upper()} · {best[0]}" if best else "실시간 기준을 만족하는 장치가 없습니다"))
    return meta


def recommend(meta):
    """(장치, 정밀도) — 정합성 OK + 실시간 만족 중 NPU > GPU > CPU, fp16 > int8."""
    for dev in DEVICE_ORDER:
        for prec in PRECISIONS:
            r = (meta.get("results") or {}).get(prec, {}).get(dev)
            if r and r.get("ok") and r.get("parity") and r.get("realtime"):
                return dev, prec
    return None


# ----------------------------------------------------------------- 추론 엔진
class OVEngine:
    """IR 하나를 컴파일해 두고 정규화된 배치 dict → actions (1, chunk, A) torch 텐서를 돌려줍니다."""

    def __init__(self, ck, device="NPU", precision="fp16", fps=30):
        ck = Path(ck)
        meta = load_meta(ck)
        if not meta:
            raise SystemExit(f"OpenVINO 변환 결과가 없습니다: {ov_dir(ck)} — 모델 탭에서 먼저 변환하세요")
        if meta.get("source") != fingerprint(ck):
            raise SystemExit("체크포인트가 변환 이후 바뀌었습니다 — 모델 탭에서 다시 변환하세요")
        xml = (meta.get("ir") or {}).get(precision)
        if not xml or not (ov_dir(ck) / xml).is_file():
            raise SystemExit(f"{precision} IR 이 없습니다 — 변환 시 해당 정밀도를 만들었는지 확인하세요")
        self.inputs = [(i["name"], tuple(i["shape"])) for i in meta["inputs"]]
        self.budget_ms = 1000.0 / fps
        core = _core(ov_dir(ck) / "cache")
        model = core.read_model(str(ov_dir(ck) / xml))
        cands = pick_devices(core.available_devices, device)
        if device not in {_family(d) for d in core.available_devices}:
            say(f"[ov] !! {device} 장치가 없습니다 (있음: {', '.join(core.available_devices) or '없음'}) — "
                f"{P.TEXT['ov_driver']}")
        if not cands:
            raise SystemExit(f"OpenVINO 장치를 찾을 수 없습니다 (요청: {device}, 있음: {core.available_devices})")
        self.compiled = None
        for dev in cands:
            try:
                t0 = time.perf_counter()
                self.compiled = core.compile_model(model, dev, {"PERFORMANCE_HINT": "LATENCY"})
                self.device = dev
                say(f"[ov] {precision} IR 을 {dev} 에 컴파일 ({time.perf_counter() - t0:.1f}s)")
                break
            except Exception as e:     # noqa: BLE001
                say(f"[ov] !! {dev} 컴파일 실패: {type(e).__name__}: {str(e).splitlines()[0][:200] if str(e) else ''}")
        if self.compiled is None:
            raise SystemExit("모든 OpenVINO 장치에서 컴파일 실패")
        if _family(self.device) != device:
            say(f"[ov] !! 요청한 {device} 대신 {self.device} 로 실행합니다 (대체 실행)")
        self.req = self.compiled.create_infer_request()
        self.n = 0
        self.ts = []
        self.over = 0
        self._last_log = 0.0

    def run(self, batch):
        import numpy as np
        import torch
        feed = []
        for name, shape in self.inputs:
            x = batch[name]
            if tuple(x.shape) != shape:
                raise RuntimeError(f"{name} shape {tuple(x.shape)} ≠ 변환 시 {shape} — "
                                   "카메라 해상도/관절 수가 학습 때와 다릅니다. 맞추거나 다시 변환하세요")
            feed.append(np.ascontiguousarray(x.detach().to("cpu", torch.float32).numpy()))
        t = time.perf_counter()
        out = self.req.infer(feed)[0]
        ms = (time.perf_counter() - t) * 1000
        self.n += 1
        self.ts.append(ms)
        if ms > self.budget_ms:
            self.over += 1
        now = time.monotonic()
        if self.n <= 3 or now - self._last_log >= 5:
            self._last_log = now
            recent = self.ts[-50:]
            say(f"[ov] 추론 #{self.n} {ms:.1f} ms (최근 평균 {sum(recent) / len(recent):.1f} ms, "
                f"프레임 예산 {self.budget_ms:.0f} ms 초과 {self.over}회) @ {self.device}")
            if ms > self.budget_ms:
                say("[ov] !! 추론이 프레임 주기보다 깁니다 — 청크 경계에서 동작이 한 박자 멈출 수 있습니다")
        return torch.from_numpy(np.array(out, dtype=np.float32, copy=True))


def patch_act(engine):
    """ACTPolicy.predict_action_chunk 를 OpenVINO 로 교체 (select_action·큐·temporal ensemble 은 그대로)."""
    import torch
    from lerobot.policies.act.modeling_act import ACTPolicy

    @torch.no_grad()
    def predict_action_chunk(self, batch):
        self.eval()
        return engine.run(batch)

    ACTPolicy.predict_action_chunk = predict_action_chunk
    try:
        from lerobot_policy_act2 import ACT2Policy
        ACT2Policy.predict_action_chunk = predict_action_chunk     # select_action 의 보간·큐는 그대로
    except ImportError:
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


def prepare_rollout(opts, rest):
    """--ov.* 옵션으로 OVEngine 을 만들고 ACTPolicy 를 패치합니다. (engine, 고친 lerobot 인자) 반환.
    컴파일(특히 NPU 첫 컴파일)은 로봇 연결 전에 끝냅니다 — 연결된 채로 수십 초 멈춰 있지 않게."""
    rest = list(rest)
    pol = next((a.split("=", 1)[1] for a in rest if a.startswith("--policy.path=")), "")
    if not pol:
        raise SystemExit("--policy.path=<.../pretrained_model> 가 필요합니다")
    ck = Path(pol)
    if opts.get("dir") and Path(opts["dir"]).resolve() != ov_dir(ck).resolve():
        raise SystemExit("--ov.dir 와 --policy.path 가 서로 다른 체크포인트입니다")
    device = (opts.get("device") or "NPU").upper()
    if device not in DEVICE_ORDER:
        raise SystemExit(f"--ov.device 는 {'/'.join(DEVICE_ORDER)} 중 하나")
    precision = (opts.get("precision") or "fp16").lower()
    fps = float(opts.get("fps") or 30)
    engine = OVEngine(ck, device, precision, fps)
    patch_act(engine)
    if not any(a.startswith("--device=") for a in rest):
        rest.append("--device=cpu")          # 전/후처리는 CPU torch — OpenVINO 가 numpy 를 받습니다
    if not any(a.startswith("--policy.pretrained_backbone_weights=") for a in rest):
        # 모델 생성 시 torchvision 이 ImageNet ResNet 가중치를 내려받는데, 곧바로 체크포인트 가중치로
        # 덮어써지므로 쓸모가 없습니다. 학습 기기와 다른(오프라인) Intel 기기에서는 이 다운로드에서 죽습니다.
        rest.append("--policy.pretrained_backbone_weights=null")
    return engine, rest


def cmd_rollout(argv):
    """예전 호출 형태 호환 — openmimic_rollout.py 로 넘깁니다 (OV 엔진, 모니터 없음 / --openmimic.run_dir 있으면 모니터)."""
    import openmimic_rollout
    return openmimic_rollout.main(["--openmimic.engine=ov"] + list(argv))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, args = argv[0], argv[1:]
    if cmd == "devices":
        try:
            print(json.dumps({"ok": True, "devices": devices()}, ensure_ascii=False))
        except ImportError:
            print(json.dumps({"ok": False, "error": "openvino 미설치"}, ensure_ascii=False))
        return 0
    if cmd == "convert":
        import argparse
        ap = argparse.ArgumentParser(prog="openmimic_ov.py convert")
        ap.add_argument("ckpt", nargs="?")
        ap.add_argument("--ckpt", dest="ckpt_opt", help="위치 인자 대신 (OpenMimic 이 이 형태로 넘깁니다)")
        ap.add_argument("--int8", action="store_true")
        ap.add_argument("--fps", type=float, default=30)
        a = ap.parse_args(args)
        ck = a.ckpt_opt or a.ckpt
        if not ck:
            ap.error("체크포인트(pretrained_model) 경로가 필요합니다")
        convert(ck, int8=a.int8, fps=a.fps)
        return 0
    if cmd == "rollout":
        return cmd_rollout(args)
    print(f"알 수 없는 명령: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
