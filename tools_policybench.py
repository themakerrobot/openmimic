#!/usr/bin/env python
"""정책별 학습 속도·메모리 측정 — 데이터셋 없이 같은 크기의 임의 입력으로 이 기기에서 잽니다.

  source activate.sh            (Windows: . .\\activate.ps1)
  python tools_policybench.py [--policies act,act2-dino,act2-flow,diffusion,smolvla] [--cams 2] [--dim 6]
                              [--batch 8] [--steps 20] [--size 480x640] [--device auto|cuda|xpu|cpu]

하는 일
  정책마다 새 파이썬 프로세스를 띄워(메모리 측정이 섞이지 않게, 메모리 부족으로 하나가 죽어도 나머지는 계속)
  lerobot 정책을 만들고 forward → backward → 옵티마이저 step 을 --steps 번 돌립니다. 앞 3 step 은 준비(컴파일·캐시)로 빼고
  나머지의 중앙값을 step 시간으로 씁니다. GPU(CUDA·XPU)는 torch 의 최대 할당량(드라이버 몫 제외), CPU 는 모델을 만들기 전 대비
  프로세스 최대 메모리 증가분을 적습니다.
  결과는 표로 출력하고 outputs/policybench_<시각>.md 로 저장합니다.

읽을 때 주의
  - 모델 계산만 잽니다. 실제 학습은 여기에 데이터 읽기(영상 디코딩)가 더해져, 데이터 읽기가 느린 기기에서는 실제 step 이 더 깁니다.
  - '기본 step 예상 시간' 은 학습 탭의 기본 step 수(ACT·Diffusion 80000, SmolVLA 20000) × step 시간입니다.
  - SmolVLA 는 처음 한 번 HuggingFace 에서 lerobot/smolvla_base 를, DINOv2 는 facebook/dinov2-small 을 받습니다(인터넷 필요).
  - 다른 학습·실행을 돌리는 중에 재면 숫자가 틀어집니다.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = Path(os.environ.get("OPENMIMIC_HOME") or HERE)
DEFAULT_STEPS = {"act": 80000, "act2": 80000, "diffusion": 80000, "smolvla": 20000}
NEEDS = {"diffusion": ["diffusers"], "smolvla": ["transformers", "num2words"], "act2": ["lerobot_policy_act2"]}


def parse_variant(v):
    """'act2-dino-flow' → ('act2', {'dino', 'flow'})"""
    parts = v.replace("+", "-").split("-")
    return parts[0], set(parts[1:])


# ----------------------------------------------------------------------------- 측정 하나 (자식 프로세스)
def child(a):
    import statistics

    import torch
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.configs.types import FeatureType, PolicyFeature
    from lerobot.policies.factory import get_policy_class
    from lerobot.utils.import_utils import register_third_party_plugins

    register_third_party_plugins()
    pol, opts = parse_variant(a.one)
    H, W = (int(x) for x in a.size.lower().split("x"))
    dev = a.device
    if dev == "auto":
        dev = "cuda" if torch.cuda.is_available() else ("xpu" if hasattr(torch, "xpu") and torch.xpu.is_available() else "cpu")
    device = torch.device(dev)
    torch.manual_seed(0)

    def sync():
        if dev == "cuda":
            torch.cuda.synchronize()
        elif dev == "xpu":
            torch.xpu.synchronize()

    def rss_mb():
        try:
            import psutil
            return psutil.Process().memory_info().rss / 2**20
        except ImportError:                 # Linux 설치에는 psutil 이 없을 수 있음
            try:
                with open("/proc/self/status") as f:
                    return next(int(x.split()[1]) / 1024 for x in f if x.startswith("VmRSS:"))
            except (OSError, StopIteration):
                return 0.0

    names = ["top", "wrist", "wrist2", "side"][: a.cams]
    if pol == "smolvla":
        cfg = PreTrainedConfig.from_pretrained("lerobot/smolvla_base")
        keys = [f"observation.images.camera{i + 1}" for i in range(a.cams)]
    else:
        kw = {}
        if pol == "diffusion":
            kw = {"noise_scheduler_type": "DDIM", "num_inference_steps": 10}
        if pol == "act2":
            if "dino" in opts:
                kw.update(vision_backbone="dinov2_small", image_size=224)
            if "flow" in opts:
                kw.update(action_head="flow", use_vae=False, flow_steps=5)
            if "aug" in opts:
                kw.update(aug_shift=0.06, aug_color=0.2, state_dropout=0.1)
        cfg = PreTrainedConfig.get_choice_class(pol)(**kw)
        keys = [f"observation.images.{c}" for c in names]
    cfg.input_features = {"observation.state": PolicyFeature(FeatureType.STATE, (a.dim,)),
                          **{k: PolicyFeature(FeatureType.VISUAL, (3, H, W)) for k in keys}}
    cfg.output_features = {"action": PolicyFeature(FeatureType.ACTION, (a.dim,))}
    cfg.device = dev

    def peak_mb():
        """프로세스가 지금까지 쓴 최대 메모리 (OS 기록 — step 안의 순간 최고치까지)"""
        if sys.platform == "win32":
            import psutil
            return psutil.Process().memory_info().peak_wset / 2**20
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

    base_rss = rss_mb()
    cls = get_policy_class(pol)
    policy = cls.from_pretrained("lerobot/smolvla_base", config=cfg) if pol == "smolvla" else cls(cfg)
    policy.to(device)
    policy.train()
    optim = cfg.get_optimizer_preset().build(policy.get_optim_params())
    n_tr = sum(p.numel() for p in policy.parameters() if p.requires_grad)

    def batch():
        b = a.batch
        d = {}
        if pol == "diffusion":
            T = cfg.n_obs_steps
            d["observation.state"] = torch.randn(b, T, a.dim)
            for k in keys:
                d[k] = torch.rand(b, T, 3, H, W)
            d["action"] = torch.randn(b, cfg.horizon, a.dim)
            d["action_is_pad"] = torch.zeros(b, cfg.horizon, dtype=torch.bool)
        else:
            d["observation.state"] = torch.randn(b, a.dim)
            for k in keys:
                d[k] = torch.rand(b, 3, H, W)
            if pol == "smolvla":
                n = cfg.tokenizer_max_length
                d["observation.language.tokens"] = torch.randint(0, 1000, (b, n))
                d["observation.language.attention_mask"] = torch.ones(b, n, dtype=torch.bool)
            d["action"] = torch.randn(b, cfg.chunk_size, a.dim)
            d["action_is_pad"] = torch.zeros(b, cfg.chunk_size, dtype=torch.bool)
        return {k: v.to(device) for k, v in d.items()}

    data = batch()          # 같은 입력을 되풀이 — 데이터 읽기는 재지 않습니다
    if dev == "cuda":
        torch.cuda.reset_peak_memory_stats()
    elif dev == "xpu":
        torch.xpu.reset_peak_memory_stats()
    times = []
    for i in range(a.steps):
        sync()
        t = time.perf_counter()
        out = policy.forward(data)
        loss = out[0] if isinstance(out, tuple) else out
        loss.backward()
        optim.step()
        optim.zero_grad(set_to_none=True)
        sync()
        times.append(time.perf_counter() - t)
    if dev == "cuda":
        mem = torch.cuda.max_memory_allocated() / 2**30
    elif dev == "xpu":
        mem = torch.xpu.max_memory_allocated() / 2**30
    else:
        mem = (peak_mb() - base_rss) / 2**10
    warm = times[3:] if len(times) > 4 else times
    name = ""
    if dev == "cuda":
        name = torch.cuda.get_device_name(0)
    elif dev == "xpu":
        name = torch.xpu.get_device_name(0)
    print("RESULT " + json.dumps({"variant": a.one, "device": dev, "device_name": name, "step_s": statistics.median(warm),
                                  "mem_gb": round(mem, 2), "trainable_m": round(n_tr / 1e6, 1)}), flush=True)


# ----------------------------------------------------------------------------- 전체 (부모 프로세스)
def main():
    sys.path.insert(0, str(HERE))
    import openmimic_platform as P
    P.utf8_stdio()                          # Windows 에서 activate 없이 돌려도 한국어 출력이 깨지지 않게
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--policies", default="act,act2-dino,act2-flow,diffusion,smolvla")
    ap.add_argument("--cams", type=int, default=2, help="카메라 수 (양팔은 보통 3)")
    ap.add_argument("--dim", type=int, default=6, help="관절 수 (한팔 6, 양팔 12)")
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--steps", type=int, default=20)
    ap.add_argument("--size", default="480x640", help="카메라 해상도 HxW")
    ap.add_argument("--device", default="auto", choices=["auto", "cuda", "xpu", "cpu"])
    ap.add_argument("--one", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.one:
        return child(a)

    import importlib.util
    rows = []
    for v in [x.strip() for x in a.policies.split(",") if x.strip()]:
        pol, _ = parse_variant(v)
        miss = [m for m in NEEDS.get(pol, []) if importlib.util.find_spec(m) is None]
        if miss:
            print(f"[skip] {v}: 패키지 없음 ({', '.join(miss)}) — 학습 탭의 '필요한 패키지 설치' 를 먼저")
            rows.append({"variant": v, "error": "패키지 없음: " + ", ".join(miss)})
            continue
        print(f"[run] {v} ...", flush=True)
        argv = [sys.executable, str(Path(__file__).resolve()), "--one", v, "--cams", str(a.cams), "--dim", str(a.dim),
                "--batch", str(a.batch), "--steps", str(a.steps), "--size", a.size, "--device", a.device]
        t0 = time.time()
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
        res = next((json.loads(line[7:]) for line in r.stdout.splitlines() if line.startswith("RESULT ")), None)
        if res is None:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["?"]
            print(f"  !! 실패: {tail[0][:200]}")
            rows.append({"variant": v, "error": tail[0][:200]})
            continue
        res["wall_s"] = round(time.time() - t0)
        rows.append(res)
        print(f"  {res['step_s']:.3f} s/step · {res['mem_gb']:.2f} GB · {res['device']} {res['device_name']}")

    dev = next((f"{r['device']} {r['device_name']}".strip() for r in rows if "device" in r), a.device)
    head = (f"# 정책별 학습 속도·메모리 — {dev}\n\n카메라 {a.cams}대 {a.size} · 관절 {a.dim} · 배치 {a.batch} · "
            f"fp32 · {a.steps} step 중 앞 3 step 제외 중앙값 · 임의 입력(데이터 읽기 제외)\n\n")
    lines = ["| 정책 | 학습 대상 파라미터 | s/step | 메모리 | 기본 step 예상 시간 |", "|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['variant']} | - | 실패 | - | {r['error']} |")
            continue
        pol, _ = parse_variant(r["variant"])
        n = DEFAULT_STEPS.get(pol, 80000)
        lines.append(f"| {r['variant']} | {r['trainable_m']}M | {r['step_s']:.3f} | {r['mem_gb']:.2f} GB | "
                     f"{r['step_s'] * n / 3600:.1f} h ({n} step) |")
    md = head + "\n".join(lines) + "\n"
    print("\n" + md)
    out = PROJ / "outputs" / f"policybench_{time.strftime('%Y%m%d_%H%M%S')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"저장: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
