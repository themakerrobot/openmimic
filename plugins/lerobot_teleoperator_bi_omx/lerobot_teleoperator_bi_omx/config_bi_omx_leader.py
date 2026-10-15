# lerobot bi_so_leader 와 같은 구조 (Apache-2.0)
from dataclasses import dataclass

from lerobot.teleoperators.config import TeleoperatorConfig


@dataclass
class OmxLeaderArmConfig:
    """팔 하나 설정. OmxLeaderConfig 와 같은 항목."""

    port: str
    gripper_open_pos: float = 60.0


@TeleoperatorConfig.register_subclass("bi_omx_leader")
@dataclass
class BiOmxLeaderConfig(TeleoperatorConfig):
    left_arm_config: OmxLeaderArmConfig
    right_arm_config: OmxLeaderArmConfig
