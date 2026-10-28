#!/usr/bin/env python
"""OpenMimic ↔ Hugging Face Hub — 로그인 상태, 데이터셋 올리기/받기, 모델 받기, HF Jobs 클라우드 학습.

OpenMimic 이 오래 걸리는 일(업로드·다운로드·클라우드 학습)을 작업(job)으로 띄울 때 쓰는 명령:

  python openmimic_hub.py push-dataset  --root <로컬 데이터셋 폴더> --repo <user/name> [--public]
  python openmimic_hub.py pull-dataset  --repo <org/name> --dest <DATA_ROOT> [--name <로컬 이름>]
  python openmimic_hub.py pull-model    --repo <org/name> --dest <OUT_ROOT> [--step <체크포인트>] [--name <로컬 이름>]
  python openmimic_hub.py cloud-train   --root <로컬 데이터셋 폴더> --name <데이터셋 이름> --flavor <HF Jobs 하드웨어>
                                    -- <lerobot-train 인자...>

토큰은 huggingface_hub 표준 위치(HF_HOME/token, `hf auth login` 과 같은 곳)에 둡니다.
OpenMimic 프로세스 안에서 쓰는 가벼운 함수(whoami 캐시·로그인·하드웨어 목록)도 여기 있습니다.
"""
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import openmimic_platform as P

OPENMIMIC_TAG = "openmimic"
_WHOAMI = {"token": None, "t": 0.0, "v": None}
_FLAVORS = {"t": 0.0, "v": None}


def say(*a):
    print(*a, flush=True)


# ----------------------------------------------------------------- OpenMimic 프로세스용
def status(refresh=False):
    """{"ok": 로그인 여부, "user", "orgs", "error"} — whoami 는 /whoami-v2 사용량 제한이 있어 5분 캐시."""
    try:
        from huggingface_hub import HfApi, get_token
    except ImportError:
        return {"ok": False, "error": "huggingface_hub 미설치"}
    token = get_token()
    if not token:
        return {"ok": False, "error": "로그인 안 됨"}
    now = time.monotonic()
    if not refresh and _WHOAMI["token"] == token and _WHOAMI["v"] and now - _WHOAMI["t"] < 300:
        return _WHOAMI["v"]
    try:
        info = HfApi().whoami(token=token)
        v = {"ok": True, "user": info["name"], "orgs": [o["name"] for o in info.get("orgs", [])]}
    except Exception as e:     # noqa: BLE001 — 네트워크·만료 토큰 모두 '로그인 안 됨' 으로
        v = {"ok": False, "error": f"토큰 확인 실패: {type(e).__name__}: {str(e)[:160]}"}
    _WHOAMI.update(token=token, t=now, v=v)
    return v


def login(token):
    from huggingface_hub import HfApi
    from huggingface_hub import login as hf_login
    token = (token or "").strip()
    if not token.startswith("hf_"):
        raise ValueError("Hugging Face 토큰은 hf_ 로 시작합니다 (https://huggingface.co/settings/tokens)")
    info = HfApi().whoami(token=token)          # 잘못된 토큰이면 여기서 예외
    hf_login(token=token, add_to_git_credential=False)
    _WHOAMI.update(token=None, v=None)
    return {"ok": True, "user": info["name"], "orgs": [o["name"] for o in info.get("orgs", [])]}


def logout():
    from huggingface_hub import logout as hf_logout
    try:
        hf_logout()
    except Exception:          # noqa: BLE001 — 이미 없으면 그만
        pass
    _WHOAMI.update(token=None, v=None)


def flavors():
    """HF Jobs 하드웨어 목록 + 시간당 가격(USD). 10분 캐시."""
    now = time.monotonic()
    if _FLAVORS["v"] is not None and now - _FLAVORS["t"] < 600:
        return _FLAVORS["v"]
    from huggingface_hub import HfApi
    out = []
    for h in HfApi().list_jobs_hardware():
        cost = getattr(h, "unit_cost_usd", None)
        unit = (getattr(h, "unit_label", "") or "").lower()
        per_h = None
        if cost is not None:
            per_h = cost * 60 if "min" in unit else cost * 3600 if "sec" in unit else cost
        acc = getattr(h, "accelerator", None)
        acc_s = f"{getattr(acc, 'quantity', '') or ''}x {getattr(acc, 'model', '')} {getattr(acc, 'vram', '') or ''}".strip() if acc else ""
        out.append({"name": h.name, "label": getattr(h, "pretty_name", h.name) or h.name,
                    "accelerator": acc_s.removeprefix("1x ").strip(), "usd_h": round(per_h, 2) if per_h else None})
    out.sort(key=lambda f: (not f["accelerator"], f["usd_h"] or 0))
    _FLAVORS.update(t=now, v=out)
    return out


def list_mine(user, kind="datasets", limit=30):
    """내(+조직) 허브의 LeRobot 데이터셋/모델 repo id 목록"""
    from huggingface_hub import HfApi
    api = HfApi()
    fn = api.list_datasets if kind == "datasets" else api.list_models
    try:
        return [x.id for x in fn(author=user, filter="LeRobot", limit=limit, sort="last_modified")]
    except Exception:          # noqa: BLE001
        return [x.id for x in fn(author=user, limit=limit)]


def cancel_job(job_id):
    from huggingface_hub import HfApi
    HfApi().cancel_job(job_id=job_id)


# ----------------------------------------------------------------- 명령 (작업으로 실행)
def _clean(s, fallback):
    s = re.sub(r"[^A-Za-z0-9._-]", "_", s or "").strip("._-")[:80]
    return s or fallback


def _free(root, name):
    if not (root / name).exists():
        return name
    for i in range(2, 1000):
        if not (root / f"{name}_{i}").exists():
            return f"{name}_{i}"
    raise SystemExit(f"{name}_2 ~ _999 가 모두 존재합니다")


def push_dataset(root, repo, private=True):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    root = Path(root)
    if not (root / "meta/info.json").is_file():
        raise SystemExit(f"데이터셋 폴더가 아닙니다: {root}")
    say(f"[hub] 데이터셋 올리기: {root} → {repo} ({'비공개' if private else '공개'})")
    ds = LeRobotDataset(repo, root=root)
    ds.push_to_hub(private=private, tags=["lerobot", OPENMIMIC_TAG])
    say(f"[hub] 완료: https://huggingface.co/datasets/{repo}")
    return repo


def pull_dataset(repo, dest, name=None):
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = _free(dest, _clean(name or repo.split("/")[-1], "hub_dataset"))
    tmp = dest / f".hub_{name}"
    shutil.rmtree(tmp, ignore_errors=True)
    say(f"[hub] 데이터셋 받기: {repo} → {dest / name}")
    try:
        ds = LeRobotDataset(repo, root=tmp)           # meta·parquet·영상까지 받습니다 (codebase 버전 태그 기준)
        say(f"[hub] 에피소드 {ds.num_episodes}개 · 프레임 {ds.num_frames} · fps {ds.fps}")
        os.replace(tmp, dest / name)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    say(f"[hub] 완료: {name}")
    return name


def _hub_checkpoints(repo):
    """repo 안의 체크포인트: {step: 'checkpoints/<step>/pretrained_model'} (+ 루트에 바로 있으면 {'hub': ''})"""
    from huggingface_hub import HfApi
    files = HfApi().list_repo_files(repo)
    out = {}
    for f in files:
        m = re.fullmatch(r"checkpoints/([^/]+)/pretrained_model/config\.json", f)
        if m and m.group(1) != "last":
            out[m.group(1)] = f"checkpoints/{m.group(1)}/pretrained_model"
    if not out and "config.json" in files and "model.safetensors" in files:
        out["hub"] = ""
    return out


def pull_model(repo, dest, step=None, name=None):
    from huggingface_hub import snapshot_download
    dest = Path(dest)
    cks = _hub_checkpoints(repo)
    if not cks:
        raise SystemExit(f"{repo} 에 정책 체크포인트가 없습니다 (config.json + model.safetensors)")
    step = step or sorted(cks)[-1]
    if step not in cks:
        raise SystemExit(f"체크포인트 {step} 없음 — 있는 것: {', '.join(sorted(cks))}")
    sub = cks[step]
    run = _free(dest, _clean(name or repo.split("/")[-1], "hub_model"))
    tmp = dest / f".hub_{run}"
    shutil.rmtree(tmp, ignore_errors=True)
    say(f"[hub] 모델 받기: {repo} ({step}) → {dest / run}")
    try:
        snapshot_download(repo, local_dir=tmp, allow_patterns=[f"{sub}/*" if sub else "*"],
                          ignore_patterns=None if sub else ["checkpoints/*", "*.md"])
        src = tmp / sub if sub else tmp
        target = dest / run / "checkpoints" / _clean(step, "hub") / "pretrained_model"
        target.parent.mkdir(parents=True)
        shutil.move(str(src), str(target))
        shutil.rmtree(target / ".cache", ignore_errors=True)
        (dest / run / "openmimic_import.json").write_text(json.dumps(
            {"imported": time.strftime("%F %T"), "from": f"hf:{repo}", "original_name": repo, "step": step},
            ensure_ascii=False, indent=1))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    say(f"[hub] 완료: {run}/checkpoints/{step}")
    return run


def cloud_train(root, name, flavor, train_args):
    """로컬 데이터셋을 내 계정 비공개 repo 로 올린 뒤(매번 — 이어서 수집한 에피소드까지 반영),
    lerobot 의 원격 학습(--job.target=<flavor>)으로 넘깁니다. 이 프로세스는 원격 로그를 그대로 흘립니다."""
    st = status(refresh=True)
    if not st.get("ok"):
        raise SystemExit("Hugging Face 로그인이 필요합니다 — 허브 탭에서 토큰을 넣으세요")
    repo = f"{st['user']}/{name}"
    push_dataset(root, repo, private=True)
    argv = [a for a in train_args if not a.startswith(("--dataset.repo_id=", "--dataset.root=", "--output_dir="))]
    argv = [sys.executable, "-m", "lerobot.scripts.lerobot_train", f"--dataset.repo_id={repo}", *argv,
            f"--job.target={flavor}", f'--job.tags=["{OPENMIMIC_TAG}"]', "--save_checkpoint_to_hub=true"]
    say("[hub] HF Jobs 제출: " + " ".join(argv[2:]))
    P.exec_replace(argv)        # PID 를 그대로 이어받아 OpenMimic 의 작업 추적·중지가 그대로 동작 (Windows 는 자식으로)


def main(argv=None):
    import argparse
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    extra = []
    if "--" in rest:
        i = rest.index("--")
        rest, extra = rest[:i], rest[i + 1:]
    ap = argparse.ArgumentParser(prog=f"openmimic_hub.py {cmd}")
    if cmd == "push-dataset":
        ap.add_argument("--root", required=True)
        ap.add_argument("--repo", required=True)
        ap.add_argument("--public", action="store_true")
        a = ap.parse_args(rest)
        push_dataset(a.root, a.repo, private=not a.public)
    elif cmd == "pull-dataset":
        ap.add_argument("--repo", required=True)
        ap.add_argument("--dest", required=True)
        ap.add_argument("--name")
        a = ap.parse_args(rest)
        pull_dataset(a.repo, a.dest, a.name)
    elif cmd == "pull-model":
        ap.add_argument("--repo", required=True)
        ap.add_argument("--dest", required=True)
        ap.add_argument("--step")
        ap.add_argument("--name")
        a = ap.parse_args(rest)
        pull_model(a.repo, a.dest, a.step, a.name)
    elif cmd == "cloud-train":
        ap.add_argument("--root", required=True)
        ap.add_argument("--name", required=True)
        ap.add_argument("--flavor", required=True)
        a = ap.parse_args(rest)
        cloud_train(a.root, a.name, a.flavor, extra)
    else:
        print(f"알 수 없는 명령: {cmd}\n{__doc__}")
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:      # noqa: BLE001 — 작업 로그 끝에 사람이 읽을 원인 한 줄
        msg = f"{type(e).__name__}: {e}"
        say(f"[hub] 실패: {msg[:400]}")
        if any(k in msg for k in ("xethub", "Connection", "connect", "Timeout", "resolve", "proxy")):
            say("[hub] 네트워크 문제로 보입니다 — huggingface.co 와 *.xethub.hf.co (대용량 파일 저장소) 에 접속되는지 확인하세요")
        elif "401" in msg or "403" in msg or "token" in msg.lower():
            say("[hub] 권한 문제로 보입니다 — 허브 탭에서 write 권한 토큰으로 다시 로그인하세요 (비공개 repo 는 소유자만 받을 수 있습니다)")
        raise
