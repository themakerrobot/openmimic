#!/usr/bin/env python
"""ACT vs ACT2 벤치마크 — 같은 데이터셋·step·시드로 변형들을 차례로 학습하고 한 표로 모읍니다.

  source activate.sh
  python tools_act2bench.py --dataset <DATA_ROOT 안 데이터셋 이름> [--variants act,act2,act2-dino,act2-flow]
                            [--steps 20000] [--batch 8] [--seeds 0] [--ov] [--extra "--policy.dim_model=256"]

하는 일
  1. 변형 × 시드마다 lerobot-train 을 돌리고 (outputs/bench_<데이터셋>_<변형>_s<시드>/) 학습 시간·마지막 loss 를 기록
  2. 체크포인트를 롤아웃 경로(lerobot 정책 로드 + 전/후처리)로 다시 열어 PyTorch 추론 지연(ms/청크)을 잼
  3. --ov 면 openmimic_ov.py convert 로 OpenVINO 변환까지 하고 장치별 지연·정합성을 같이 적음
  4. OpenMimic 실행 탭에서 기록한 성공/실패(openmimic_trials.json)가 있으면 체크포인트별 성공률을 합침
  5. outputs/act2bench_<시각>.csv / .md 로 저장 — 성공률은 실제 팔로 Rollout 을 돌린 뒤 --report-only 로 다시 뽑으면 채워집니다

변형 이름은 '+' 나 '-' 로 선택지를 이어 붙입니다: act2-aug-interp-dino-flow-nobb (aug/interp 는 act2 기본 켬, act2-plain 은 전부 끔).
학습·추론은 lerobot 이 CUDA → XPU → CPU 순으로 고릅니다. 모든 변형이 같은 기기·같은 인자로 돌아야 비교가 됩니다.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJ = Path(os.environ.get("OPENMIMIC_HOME") or HERE)
DATA_ROOT = PROJ / "data/hf/lerobot/local"
OUT_ROOT = PROJ / "outputs"
TRIALS_FILE = PROJ / "openmimic_trials.json"

# 학습 탭(main.py policy_train_args) 과 같은 인자 — 화면에서 고른 것과 동일한 모델이 되게
OPT_ARGS = {
    "aug": ["--policy.aug_shift=0.06", "--policy.aug_color=0.2", "--policy.state_dropout=0.1"],
    "interp": ["--policy.n_action_steps=30", "--policy.interp_steps=10"],
    "dino": ["--policy.vision_backbone=dinov2_small", "--policy.image_size=224"],
    "flow": ["--policy.action_head=flow", "--policy.use_vae=false", "--policy.flow_steps=5"],
    "nobb": ["--policy.pretrained_backbone_weights=null"],
}
ACT2_DEFAULT_OPTS = ("aug", "interp")


def variant_args(name):
    """'act' | 'act2' | 'act2-dino-flow' | 'act2-plain' → (정책 종류, lerobot 인자, 켜진 선택지)."""
    parts = re.split(r"[+\-]", name.strip())
    kind, opts = parts[0], parts[1:]
    if kind == "act":
        args = ["--policy.type=act"]
        if "nobb" in opts:
            args += OPT_ARGS["nobb"]
        return kind, args, [o for o in opts if o == "nobb"]
    if kind != "act2":
        raise SystemExit(f"변형은 act 또는 act2 로 시작해야 합니다: {name}")
    on = [] if "plain" in opts else list(ACT2_DEFAULT_OPTS)
    for o in opts:
        if o == "plain":
            continue
        if o not in OPT_ARGS:
            raise SystemExit(f"모르는 선택지 '{o}' (가능: {', '.join(OPT_ARGS)}, plain)")
        if o not in on:
            on.append(o)
    args = ["--policy.type=act2"]
    for o in on:
        args += OPT_ARGS[o]
    return kind, args, on


def run_dir(ds, variant, seed):
    return OUT_ROOT / f"bench_{ds}_{variant.replace('+', '-')}_s{seed}"


def parse_train_log(text):
    """lerobot 학습 로그에서 (마지막 loss, 뒤쪽 20% 평균 loss, 평균 step/s)."""
    losses, upd = [], []
    for m in re.finditer(r"step:(\d+) .*?loss:([\d.]+) .*?updt_s:([\d.]+)", text):
        losses.append(float(m.group(2)))
        upd.append(float(m.group(3)))
    if not losses:
        return None, None, None
    tail = losses[max(0, int(len(losses) * 0.8)):]
    return losses[-1], sum(tail) / len(tail), (1.0 / (sum(upd) / len(upd)) if upd and sum(upd) > 0 else None)


LATENCY_CODE = r'''
import sys, time, json, torch
from pathlib import Path
from lerobot.utils.import_utils import register_third_party_plugins
register_third_party_plugins()
from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.factory import get_policy_class, make_pre_post_processors
from lerobot.datasets.lerobot_dataset import LeRobotDataset
ck, root, repo, n = Path(sys.argv[1]), sys.argv[2], sys.argv[3], int(sys.argv[4])
cfg = PreTrainedConfig.from_pretrained(ck)
cfg.pretrained_backbone_weights = None
if getattr(cfg, "dinov2_pretrained_path", None): cfg.dinov2_pretrained_path = None
dev = "cuda" if torch.cuda.is_available() else ("xpu" if hasattr(torch, "xpu") and torch.xpu.is_available() else "cpu")
cfg.device = dev
pol = get_policy_class(cfg.type).from_pretrained(ck, config=cfg).to(dev).eval()
pre, post = make_pre_post_processors(policy_cfg=cfg, pretrained_path=str(ck),
    preprocessor_overrides={"device_processor": {"device": dev}}, postprocessor_overrides={"device_processor": {"device": "cpu"}})
ds = LeRobotDataset(repo, root=root)
obs = {k: v for k, v in ds[0].items() if k.startswith("observation.")}
sync = (lambda: torch.cuda.synchronize()) if dev == "cuda" else ((lambda: torch.xpu.synchronize()) if dev == "xpu" else (lambda: None))
with torch.no_grad():
    b = pre(obs)
    for _ in range(3): pol.predict_action_chunk(b); sync()
    ts = []
    for _ in range(n):
        t = time.perf_counter(); pol.predict_action_chunk(b); sync(); ts.append((time.perf_counter() - t) * 1000)
ts.sort()
params = sum(p.numel() for p in pol.parameters()); train = sum(p.numel() for p in pol.parameters() if p.requires_grad)
print(json.dumps({"device": dev, "chunk_ms": round(ts[len(ts)//2], 1), "p95_ms": round(ts[int(len(ts)*0.95)-1], 1),
                  "params_m": round(params/1e6, 2), "trainable_m": round(train/1e6, 2),
                  "chunk": cfg.chunk_size, "n_action_steps": cfg.n_action_steps}))
'''


def measure_latency(ck, root, repo, n=20):
    r = subprocess.run([sys.executable, "-c", LATENCY_CODE, str(ck), str(root), repo, str(n)],
                       capture_output=True, text=True, timeout=1800)
    line = next((ln for ln in r.stdout.strip().splitlines()[::-1] if ln.startswith("{")), None)
    if not line:
        return {"error": (r.stderr.strip().splitlines() or ["?"])[-1][:200]}
    return json.loads(line)


def ov_result(ck):
    """openmimic_ov 변환 결과 메타 (있으면). 장치별 fp16 평균 ms 와 정합성."""
    try:
        sys.path.insert(0, str(HERE))
        import openmimic_ov
        meta = openmimic_ov.load_meta(Path(ck))
    except Exception:  # noqa: BLE001
        return {}
    if not meta:
        return {}
    out = {}
    for prec, devs in (meta.get("results") or {}).items():
        for dev, r in (devs or {}).items():
            if isinstance(r, dict) and r.get("mean_ms") is not None:
                out[f"ov_{prec}_{dev.lower()}_ms"] = f"{r['mean_ms']}{'' if r.get('parity') else '(오차)'}"
    return out


def trial_rate(ck):
    try:
        rel = str(Path(ck).resolve().relative_to(OUT_ROOT.resolve()))
    except ValueError:
        return None, 0
    t = (json.loads(TRIALS_FILE.read_text()) if TRIALS_FILE.is_file() else {}).get(rel) or []
    n = len(t)
    ok = sum(1 for x in t if x.get("result") == "success")
    return (round(100 * ok / n) if n else None), n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", help=f"{DATA_ROOT} 안의 데이터셋 이름")
    ap.add_argument("--root", help="데이터셋 경로를 직접 지정 (--dataset 대신)")
    ap.add_argument("--variants", default="act,act2,act2-dino,act2-flow")
    ap.add_argument("--steps", type=int, default=20000)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--seeds", default="0")
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--extra", default="", help="모든 변형에 공통으로 붙일 lerobot 인자 (예: '--policy.use_amp=true')")
    ap.add_argument("--ov", action="store_true", help="학습 뒤 openmimic_ov.py convert 까지 (Intel 기기)")
    ap.add_argument("--latency-n", type=int, default=20)
    ap.add_argument("--report-only", action="store_true", help="학습 없이 기존 bench_* 체크포인트로 표만 다시 생성")
    a = ap.parse_args()

    if a.root:
        root = Path(a.root).resolve()
        ds = root.name
    elif a.dataset:
        ds, root = a.dataset, DATA_ROOT / a.dataset
    else:
        ap.error("--dataset 또는 --root 가 필요합니다")
    if not (root / "meta/info.json").is_file():
        raise SystemExit(f"데이터셋 없음: {root}")
    repo = f"local/{ds}"
    variants = [v.strip() for v in a.variants.split(",") if v.strip()]
    seeds = [int(s) for s in a.seeds.split(",") if s.strip()]
    extra = a.extra.split()
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    rows = []
    for v in variants:
        kind, pargs, on = variant_args(v)
        for seed in seeds:
            out = run_dir(ds, v, seed)
            ck = out / "checkpoints/last/pretrained_model"
            row = {"variant": v, "policy": kind, "opts": "+".join(on) or "-", "seed": seed, "steps": a.steps, "batch": a.batch}
            log = out / "bench_train.log"
            if not a.report_only:
                if out.exists():
                    print(f"[skip] {out.name} 이미 있음 (다시 돌리려면 폴더를 지우세요)")
                else:
                    argv = [sys.executable, "-m", "lerobot.scripts.lerobot_train",
                            f"--dataset.repo_id={repo}", f"--dataset.root={root}", *pargs, *extra,
                            f"--output_dir={out}", f"--steps={a.steps}", f"--batch_size={a.batch}",
                            f"--num_workers={a.num_workers}", f"--seed={seed}",
                            f"--save_freq={a.steps}", f"--log_freq={max(1, min(50, a.steps // 20))}",
                            "--policy.push_to_hub=false", "--wandb.enable=false"]
                    print(f"[train] {out.name}: {' '.join(pargs)}")
                    t0 = time.time()
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with open(str(out) + ".log", "w") as f:
                        r = subprocess.run(argv, stdout=f, stderr=subprocess.STDOUT, text=True)
                    Path(str(out) + ".log").replace(log) if out.exists() else None
                    row["train_min"] = round((time.time() - t0) / 60, 1)
                    if r.returncode != 0:
                        tail = Path(str(out) + ".log").read_text()[-400:] if Path(str(out) + ".log").exists() else ""
                        row["error"] = f"train rc={r.returncode}: {tail.strip().splitlines()[-1] if tail else ''}"
                        rows.append(row)
                        print("  !! 실패:", row["error"])
                        continue
            if log.is_file():
                last, tail, sps = parse_train_log(log.read_text())
                row.update(loss_last=last, loss_tail=tail, step_per_s=round(sps, 2) if sps else None)
                if "train_min" not in row and sps:
                    row["train_min"] = round(a.steps / sps / 60, 1)
            if not ck.is_dir():
                row.setdefault("error", "체크포인트 없음")
                rows.append(row)
                continue
            lat = measure_latency(ck, root, repo, a.latency_n)
            row.update(lat)
            if a.ov and not a.report_only:
                print(f"[ov] {out.name}")
                subprocess.run([sys.executable, str(HERE / "openmimic_ov.py"), "convert", str(ck)])
            row.update(ov_result(ck))
            rate, n = trial_rate(ck)
            row.update(success_pct=rate, trials=n, checkpoint=str(ck.relative_to(OUT_ROOT)))
            rows.append(row)
            print("  ", {k: row[k] for k in row if k in ("train_min", "loss_tail", "chunk_ms", "params_m", "trainable_m", "success_pct")})

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    cols = ["variant", "policy", "opts", "seed", "steps", "batch", "train_min", "step_per_s", "loss_last", "loss_tail",
            "params_m", "trainable_m", "chunk", "n_action_steps", "device", "chunk_ms", "p95_ms",
            *sorted({k for r in rows for k in r if k.startswith("ov")}), "success_pct", "trials", "checkpoint", "error"]
    csv_path = OUT_ROOT / f"act2bench_{stamp}.csv"
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    md = [f"# ACT vs ACT2 — {ds} ({a.steps} steps, batch {a.batch}, seeds {','.join(map(str, seeds))})", "",
          "| 변형 | 선택지 | 시드 | 학습(분) | loss(뒤 20% 평균) | 파라미터 M (학습 M) | 추론 ms/청크 (장치) | 실행 스텝/청크 | 실기 성공률 | 비고 |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        succ = f"{r['success_pct']}% ({r['trials']}회)" if r.get("success_pct") is not None else (f"0/{r.get('trials', 0)}" if r.get("trials") else "미측정")
        ovs = ", ".join(f"OV {k[3:-3].replace('_', ' ').upper()} {v}ms" for k, v in r.items() if k.startswith("ov_") and v is not None)
        md.append(f"| {r['variant']} | {r['opts']} | {r['seed']} | {r.get('train_min', '')} | "
                  f"{'' if r.get('loss_tail') is None else round(r['loss_tail'], 3)} | "
                  f"{r.get('params_m', '')} ({r.get('trainable_m', '')}) | {r.get('chunk_ms', '')} ({r.get('device', '')}) | "
                  f"{r.get('n_action_steps', '')}/{r.get('chunk', '')} | {succ} | {r.get('error') or ovs or ''} |")
    md += ["", "- loss 는 정규화 공간의 학습 손실이라 flow(MSE)와 L1 변형 사이에는 비교가 안 됩니다. 같은 헤드끼리만 보세요.",
           "- 실기 성공률은 실행 탭에서 각 bench_* 체크포인트로 같은 태스크를 20회 이상 돌리고 성공/실패를 누른 뒤 "
           f"`python tools_act2bench.py --dataset {ds} --variants {a.variants} --report-only` 로 다시 뽑으면 채워집니다."]
    md_path = OUT_ROOT / f"act2bench_{stamp}.md"
    md_path.write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\n저장: {csv_path}\n      {md_path}")


if __name__ == "__main__":
    main()
