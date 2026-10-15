# lerobot bi_so_follower 와 같은 구조 (Apache-2.0)
from dataclasses import dataclass, field

from lerobot.cameras import CameraConfig
from lerobot.robots.config import RobotConfig


@dataclass
class OmxArmConfig:
    """팔 하나 설정. OmxFollowerConfig 와 같은 항목 (id·calibration_dir 은 양팔 쪽에서 내려줌)."""

    port: str
    disable_torque_on_disconnect: bool = True
    max_relative_target: float | dict[str, float] | None = None
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
    # omx_leader 는 항상 -100~100 이라 팔로워도 기본 False 로 맞춥니다 (omx_follower 기본값과 동일)
    use_degrees: bool = False


@RobotConfig.register_subclass("bi_omx_follower")
@dataclass
class BiOmxFollowerConfig(RobotConfig):
    left_arm_config: OmxArmConfig
    right_arm_config: OmxArmConfig
    # 특정 팔에 속하지 않는 카메라. 관측 키에 left_/right_ 접두사가 붙지 않습니다.
    cameras: dict[str, CameraConfig] = field(default_factory=dict)
