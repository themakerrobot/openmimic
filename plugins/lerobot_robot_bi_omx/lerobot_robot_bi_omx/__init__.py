"""양팔 ROBOTIS OMX 팔로워 — lerobot 플러그인.

lerobot 은 'lerobot_robot_' 으로 시작하는 설치된 패키지를 찾아 import 합니다
(lerobot.utils.import_utils.register_third_party_plugins). import 되는 순간
'bi_omx_follower' 가 RobotConfig 에 등록되어 --robot.type=bi_omx_follower 로 쓸 수 있습니다.
"""

from .bi_omx_follower import BiOmxFollower
from .config_bi_omx_follower import BiOmxFollowerConfig, OmxArmConfig

__all__ = ["BiOmxFollower", "BiOmxFollowerConfig", "OmxArmConfig"]
