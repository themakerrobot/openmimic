#!/usr/bin/env python3
"""OpenMimic 의 모든 페이지에서 인라인 <script> 를 뽑아 node --check 로 파싱 검증.

Python 문자열 안에 JS 가 들어 있어 따옴표·역슬래시 escape 가 한 번 더 벗겨질 수 있습니다.
파이썬이 평가한 뒤의 JS 를 검사하므로 그런 문제를 그대로 잡습니다.

    python tools_jscheck.py            # node 를 PATH 에서 찾음
    NODE=/opt/node22/bin/node python tools_jscheck.py
    python tools_jscheck.py --en       # 영어 화면(사전 치환 후)
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PAGES = ("/projects", "/", "/collect", "/train", "/models", "/rollout", "/hub", "/control", "/calib", "/setup", "/setup/wizard", "/jobs")


def main():
    node = os.environ.get("NODE") or shutil.which("node") or shutil.which("nodejs")
    if not node:
        print("node 를 찾을 수 없습니다 — NODE=/path/to/node 로 지정하세요", file=sys.stderr)
        return 2
    try:
        import main as openmimic
        from fastapi.testclient import TestClient
    except ImportError as e:
        print(f"의존성 없음: {e}", file=sys.stderr)
        return 2

    # 포트가 비어 있으면 Control 탭이 '포트 미설정' 안내만 그려서 정작 검사해야 할
    # 모듈 스크립트가 빠집니다. 메모리상으로만 더미 포트를 넣어 전체 페이지를 렌더합니다.
    if not openmimic.ports_configured():
        for i, arm in enumerate(openmimic.ARM_CFGS.values()):
            arm["follower_port"] = f"/dev/null_f{i}"
            arm["leader_port"] = f"/dev/null_l{i}"
        print("  (포트 미설정 → 더미 포트 주입, 전체 페이지 렌더)")

    client = TestClient(openmimic.app)
    en = "--en" in sys.argv          # 영어 화면(사전 치환 후)도 문법이 깨지지 않는지
    if en:
        client.cookies.set(openmimic.LANG_COOKIE, "en")
        print("  (영어 모드 — openmimic_i18n_en.json 치환 후 검사)")
    bad = 0
    seen = {}
    with tempfile.TemporaryDirectory() as tmp:
        chk = Path(tmp) / "chk.mjs"
        for path in PAGES:
            resp = client.get(path)
            if resp.status_code != 200:
                print(f"  {path:9s} HTTP {resp.status_code} — 건너뜀")
                continue
            for i, js in enumerate(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", resp.text, re.S)):
                js = js.strip()
                if not js or js.startswith("{"):      # importmap(JSON)
                    continue
                chk.write_text(js)
                r = subprocess.run([node, "--check", str(chk)], capture_output=True, text=True)
                if r.returncode:
                    bad += 1
                    print(f"  {path} script#{i}: SYNTAX ERROR\n{r.stderr}")
                else:
                    print(f"  {path:9s} script#{i}  {len(js):6d} chars  OK")
                seen[path] = seen.get(path, 0) + 1
        # 진행 중 작업이 있을 때만 그려지는 블록은 페이지 요청으로는 안 나옵니다.
        # 모듈 문자열을 직접 검사합니다.
        # 롤아웃 실행 화면은 조립 함수로 만들어지므로 같은 방식으로 감싸 검사합니다
        ro_run = "<script>" + openmimic.ARM3D_JS + openmimic.ROLLOUT_RUN_JS + "</script>"
        for name in ("COLLECT_RUN_HTML", "REVIEW_HTML", "WIZARD_HTML", "ROLLOUT_RUN"):
            blk = ro_run if name == "ROLLOUT_RUN" else getattr(openmimic, name, "")
            if en:
                blk = openmimic.to_en_html(blk)
            for i, js in enumerate(re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", blk, re.S)):
                js = js.strip()
                if not js:
                    continue
                if js.startswith("{"):          # importmap
                    continue
                chk.write_text("const JID='x', DS='x', URDF_OK=false, VIEWS_CFG={}, K3=null, RO={};\n" + js)
                r = subprocess.run([node, "--check", str(chk)], capture_output=True, text=True)
                if r.returncode:
                    bad += 1
                    print(f"  {name} script#{i}: SYNTAX ERROR\n{r.stderr}")
                else:
                    print(f"  {name:9s} script#{i}  {len(js):6d} chars  OK")
                seen[name] = seen.get(name, 0) + 2
    # 페이지마다 최소 2개(공통 + 페이지 스크립트) 는 나와야 합니다.
    # 하나뿐이면 조기 반환 페이지를 검사한 것이라 정작 볼 코드를 못 봤다는 뜻입니다.
    thin = [pg for pg, n in seen.items() if n < 2]
    if thin:
        print(f"\n주의: 스크립트가 1개뿐인 페이지 {thin} — 조기 반환 페이지를 검사했을 수 있습니다")
    print(f"\n문법 오류 {bad}건")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
