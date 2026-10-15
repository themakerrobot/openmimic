# lerobot bi_so_leader 와 같은 구조 (Apache-2.0). 팔 하나는 lerobot 의 OmxLeader 그대로입니다.
import logging
from functools import cached_property

from lerobot.teleoperators.omx_leader import OmxLeader, OmxLeaderConfig
from lerobot.teleoperators.teleoperator import Teleoperator
from lerobot.utils.bimanual import BimanualMixin
from lerobot.utils.decorators import check_if_not_connected

from .config_bi_omx_leader import BiOmxLeaderConfig

logger = logging.getLogger(__name__)


class BiOmxLeader(BimanualMixin, Teleoperator):
    """양팔 ROBOTIS OMX 리더. 팔별 캘리브레이션 id '{id}_left' / '{id}_right', 행동 키 'left_' / 'right_'."""

    config_class = BiOmxLeaderConfig
    name = "bi_omx_leader"

    def __init__(self, config: BiOmxLeaderConfig):
        super().__init__(config)
        self.config = config

        def arm(cfg, side):
            return OmxLeader(OmxLeaderConfig(
                id=f"{config.id}_{side}" if config.id else None,
                calibration_dir=config.calibration_dir,
                port=cfg.port,
                gripper_open_pos=cfg.gripper_open_pos,
            ))

        self.left_arm = arm(config.left_arm_config, "left")
        self.right_arm = arm(config.right_arm_config, "right")

    @cached_property
    def action_features(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm.action_features.items()},
            **{f"right_{k}": v for k, v in self.right_arm.action_features.items()},
        }

    @cached_property
    def feedback_features(self) -> dict[str, type]:
        return {}

    def setup_motors(self) -> None:
        self.left_arm.setup_motors()
        self.right_arm.setup_motors()

    @check_if_not_connected
    def get_action(self):
        out = {f"left_{k}": v for k, v in self.left_arm.get_action().items()}
        out.update({f"right_{k}": v for k, v in self.right_arm.get_action().items()})
        return out

    def send_feedback(self, feedback: dict[str, float]) -> None:
        raise NotImplementedError
