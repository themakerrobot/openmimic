#!/usr/bin/env python
# Copyright 2024 Tony Z. Zhao and The HuggingFace Inc. team. All rights reserved.
# Copyright 2026 Circulus Inc. (ACT2 additions)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""ACT2 configuration.

ACT2 = lerobot ACT (e40b58a) plus switches that are each off/plain by default so that
``--policy.type=act2`` with no other flags trains the same network as ``--policy.type=act``:

* ``vision_backbone``: ``resnet18`` (default) or ``dinov2_small`` / ``dinov2_base`` (HF transformers
  ``Dinov2Model``, frozen by default).
* ``image_size``: resize camera images to a square before the backbone (``None`` = keep).
  DINOv2 uses patch 14, so 224 → 16x16 tokens per camera. Resize is part of the model, so it is also
  exported to OpenVINO.
* ``aug_shift`` / ``aug_color``: train-time random shift + brightness/contrast jitter on each camera image.
* ``state_dropout``: zero the robot-state token with this probability during training (forces image use).
* ``interp_steps``: at a chunk boundary, linearly blend the first steps of the new chunk from the last
  executed action (removes the servo jump between chunks).
* ``action_head``: ``l1`` (ACT regression) or ``flow`` (flow-matching action expert on top of the decoder
  tokens, ``flow_steps`` Euler steps at inference). ``flow_fixed_noise`` makes inference deterministic by
  starting every chunk from the same seeded noise (needed for OpenVINO output checks; set False to sample).
"""
from dataclasses import dataclass, field

from lerobot.configs import NormalizationMode, PreTrainedConfig
from lerobot.optim import AdamWConfig

DINOV2_BACKBONES = {
    # name: (HF hub id, hidden size, patch)
    "dinov2_small": ("facebook/dinov2-small", 384, 14),
    "dinov2_base": ("facebook/dinov2-base", 768, 14),
}


@PreTrainedConfig.register_subclass("act2")
@dataclass
class ACT2Config(PreTrainedConfig):
    # ---- identical to ACTConfig -------------------------------------------------------------
    n_obs_steps: int = 1
    chunk_size: int = 100
    n_action_steps: int = 100

    normalization_mapping: dict[str, NormalizationMode] = field(
        default_factory=lambda: {
            "VISUAL": NormalizationMode.MEAN_STD,
            "STATE": NormalizationMode.MEAN_STD,
            "ACTION": NormalizationMode.MEAN_STD,
        }
    )

    vision_backbone: str = "resnet18"
    pretrained_backbone_weights: str | None = "ResNet18_Weights.IMAGENET1K_V1"
    replace_final_stride_with_dilation: int = False
    pre_norm: bool = False
    dim_model: int = 512
    n_heads: int = 8
    dim_feedforward: int = 3200
    feedforward_activation: str = "relu"
    n_encoder_layers: int = 4
    n_decoder_layers: int = 1
    use_vae: bool = True
    latent_dim: int = 32
    n_vae_encoder_layers: int = 4
    temporal_ensemble_coeff: float | None = None
    dropout: float = 0.1
    kl_weight: float = 10.0
    optimizer_lr: float = 1e-5
    optimizer_weight_decay: float = 1e-4
    optimizer_lr_backbone: float = 1e-5

    # ---- ACT2 additions -----------------------------------------------------------------------
    # DINOv2: HF hub id or local path (None → random init, offline smoke tests only)
    dinov2_pretrained_path: str | None = None
    freeze_backbone: bool | None = None  # None → True for dinov2, False for resnet
    image_size: int | None = None  # square resize before the backbone (224 recommended for dinov2)

    aug_shift: float = 0.0  # fraction of image size, e.g. 0.06
    aug_color: float = 0.0  # brightness/contrast jitter in normalized units, e.g. 0.2
    state_dropout: float = 0.0

    interp_steps: int = 0  # linear blend length at chunk boundaries (0 = off)

    action_head: str = "l1"  # "l1" | "flow"
    flow_steps: int = 5  # Euler steps at inference
    flow_layers: int = 2  # transformer layers in the flow action expert
    flow_time_sigma: float = 1.0  # t ~ sigmoid(N(0, sigma)) (logit-normal, as in pi0)
    flow_fixed_noise: bool = True  # inference starts from one fixed noise chunk (deterministic; OpenVINO-checkable)

    def __post_init__(self):
        super().__post_init__()
        if not (self.vision_backbone.startswith("resnet") or self.vision_backbone in DINOV2_BACKBONES):
            raise ValueError(
                f"`vision_backbone` must be a torchvision ResNet or one of {sorted(DINOV2_BACKBONES)}. "
                f"Got {self.vision_backbone}."
            )
        if self.is_dinov2:
            if self.dinov2_pretrained_path is None and self.pretrained_backbone_weights:
                # default HF id when the user did not pass one and did not ask for "no pretrained"
                self.dinov2_pretrained_path = DINOV2_BACKBONES[self.vision_backbone][0]
            if self.image_size is None:
                self.image_size = 224
            if self.freeze_backbone is None:
                self.freeze_backbone = True
        elif self.freeze_backbone is None:
            self.freeze_backbone = False
        if self.action_head not in ("l1", "flow"):
            raise ValueError(f"`action_head` must be 'l1' or 'flow'. Got {self.action_head}.")
        if self.action_head == "flow" and self.flow_steps < 1:
            raise ValueError("`flow_steps` must be >= 1.")
        if self.temporal_ensemble_coeff is not None and self.n_action_steps > 1:
            raise NotImplementedError("`n_action_steps` must be 1 when using temporal ensembling.")
        if self.temporal_ensemble_coeff is not None and self.interp_steps > 0:
            raise ValueError("`interp_steps` and temporal ensembling are exclusive (ensembling already smooths).")
        if self.n_action_steps > self.chunk_size:
            raise ValueError(
                f"The chunk size is the upper bound for the number of action steps per model invocation. Got "
                f"{self.n_action_steps} for `n_action_steps` and {self.chunk_size} for `chunk_size`."
            )
        if self.interp_steps > self.n_action_steps:
            raise ValueError("`interp_steps` must be <= `n_action_steps`.")
        if self.n_obs_steps != 1:
            raise ValueError(f"Multiple observation steps not handled yet. Got `nobs_steps={self.n_obs_steps}`")

    @property
    def is_dinov2(self) -> bool:
        return self.vision_backbone in DINOV2_BACKBONES

    def get_optimizer_preset(self) -> AdamWConfig:
        return AdamWConfig(lr=self.optimizer_lr, weight_decay=self.optimizer_weight_decay)

    def get_scheduler_preset(self) -> None:
        return None

    def validate_features(self) -> None:
        if not self.image_features and not self.env_state_feature:
            raise ValueError("You must provide at least one image or the environment state among the inputs.")

    @property
    def observation_delta_indices(self) -> None:
        return None

    @property
    def action_delta_indices(self) -> list:
        return list(range(self.chunk_size))

    @property
    def reward_delta_indices(self) -> None:
        return None
