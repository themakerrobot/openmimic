# lerobot bi_so_follower 와 같은 구조 (Apache-2.0). 팔 하나는 lerobot 의 OmxFollower 그대로입니다.
import logging
from functools import cached_property

from lerobot.robots.omx_follower import OmxFollower, OmxFollowerConfig
from lerobot.robots.robot import Robot
from lerobot.utils.bimanual import BimanualMixin
from lerobot.utils.decorators import check_if_not_connected

from .config_bi_omx_follower import BiOmxFollowerConfig

logger = logging.getLogger(__name__)


class BiOmxFollower(BimanualMixin, Robot):
    """양팔 ROBOTIS OMX 팔로워.

    - 팔별 캘리브레이션 id: '{id}_left' / '{id}_right' (bi_so_follower 와 같은 규칙)
    - 관측·행동 키: 'left_' / 'right_' 접두사. 공용 카메라는 접두사 없음 (왼팔이 같이 엶)
    """

    config_class = BiOmxFollowerConfig
    name = "bi_omx_follower"

    def __init__(self, config: BiOmxFollowerConfig):
        super().__init__(config)
        self.config = config

        self._top_level_cam_keys = set(config.cameras)
        collisions = (self._top_level_cam_keys & set(config.left_arm_config.cameras)) | (
            self._top_level_cam_keys & set(config.right_arm_config.cameras)
        )
        if collisions:
            raise ValueError(f"Top-level camera names collide with per-arm camera names: {sorted(collisions)}")

        def arm(cfg, side, cameras):
            return OmxFollower(OmxFollowerConfig(
                id=f"{config.id}_{side}" if config.id else None,
                calibration_dir=config.calibration_dir,
                port=cfg.port,
                disable_torque_on_disconnect=cfg.disable_torque_on_disconnect,
                max_relative_target=cfg.max_relative_target,
                use_degrees=cfg.use_degrees,
                cameras=cameras,
            ))

        self.left_arm = arm(config.left_arm_config, "left",
                            {**config.left_arm_config.cameras, **config.cameras})
        self.right_arm = arm(config.right_arm_config, "right", config.right_arm_config.cameras)
        # 다른 코드 호환용 (양쪽 카메라 이름이 같으면 겹칩니다 — 개수는 observation_features 로 세세요)
        self.cameras = {**self.left_arm.cameras, **self.right_arm.cameras}

    @property
    def _motors_ft(self) -> dict[str, type]:
        return {
            **{f"left_{k}": v for k, v in self.left_arm._motors_ft.items()},
            **{f"right_{k}": v for k, v in self.right_arm._motors_ft.items()},
        }

    @property
    def _cameras_ft(self) -> dict[str, tuple]:
        out: dict[str, tuple] = {}
        for k, v in self.left_arm._cameras_ft.items():
            out[k if k in self._top_level_cam_keys else f"left_{k}"] = v
        for k, v in self.right_arm._cameras_ft.items():
            out[f"right_{k}"] = v
        return out

    @cached_property
    def observation_features(self) -> dict[str, type | tuple]:
        return {**self._motors_ft, **self._cameras_ft}

    @cached_property
    def action_features(self) -> dict[str, type]:
        return self._motors_ft

    def setup_motors(self) -> None:
        self.left_arm.setup_motors()
        self.right_arm.setup_motors()

    @check_if_not_connected
    def get_observation(self):
        obs = {}
        for k, v in self.left_arm.get_observation().items():
            obs[k if k in self._top_level_cam_keys else f"left_{k}"] = v
        for k, v in self.right_arm.get_observation().items():
            obs[f"right_{k}"] = v
        return obs

    @check_if_not_connected
    def send_action(self, action):
        left = {k.removeprefix("left_"): v for k, v in action.items() if k.startswith("left_")}
        right = {k.removeprefix("right_"): v for k, v in action.items() if k.startswith("right_")}
        sent_l = self.left_arm.send_action(left)
        sent_r = self.right_arm.send_action(right)
        return {**{f"left_{k}": v for k, v in sent_l.items()},
                **{f"right_{k}": v for k, v in sent_r.items()}}
