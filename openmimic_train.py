"""lerobot-train 을 그대로 돌리되, Windows 에서 막히는 부분만 바꿔 끼웁니다 (python openmimic_train.py <lerobot-train 인자>).

lerobot 은 체크포인트를 저장할 때마다 checkpoints/last 를 심볼릭 링크로 만듭니다. Windows 는 관리자 권한이나 개발자 모드가 없으면
심볼릭 링크를 만들 수 없어(WinError 1314) 첫 체크포인트 저장 직후 학습이 통째로 죽습니다.
여기서는 그 함수만 openmimic_platform.link_dir(되면 심볼릭 링크, 안 되면 디렉터리 정션)로 바꾼 뒤 lerobot_train 을 그대로 실행합니다.
OpenMimic 은 Windows 에서만 이 파일로 학습을 띄웁니다 (openmimic_platform.LEROBOT_TRAIN).
"""
import runpy
import sys
from pathlib import Path

import openmimic_platform as P


def _update_last_checkpoint(checkpoint_dir: Path) -> None:
    from lerobot.utils.constants import LAST_CHECKPOINT_LINK
    P.link_dir(Path(checkpoint_dir).parent / LAST_CHECKPOINT_LINK, Path(checkpoint_dir).name)


def main():
    import lerobot.common.train_utils as tu
    tu.update_last_checkpoint = _update_last_checkpoint      # lerobot_train 이 import 하기 전에 바꿔야 그쪽 이름도 이것을 가리킵니다
    sys.argv = ["lerobot_train", *sys.argv[1:]]
    runpy.run_module("lerobot.scripts.lerobot_train", run_name="__main__", alter_sys=True)


if __name__ == "__main__":
    main()
