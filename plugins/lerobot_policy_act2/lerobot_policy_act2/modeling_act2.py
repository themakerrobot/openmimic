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
"""ACT2 policy — lerobot ACT (e40b58a) with switchable backbone, augmentation, chunk interpolation and an
optional flow-matching action head. With all switches at their defaults it is numerically the same network
as ``act``. The transformer blocks are imported from lerobot's ACT so they stay in sync with the base model.
"""

import logging
import math
from collections import deque
from itertools import chain

import einops
import torch
import torch.nn.functional as F  # noqa: N812
import torchvision
from torch import Tensor, nn
from torchvision.models._utils import IntermediateLayerGetter
from torchvision.ops.misc import FrozenBatchNorm2d

from lerobot.policies.act.modeling_act import (
    ACTDecoder,
    ACTEncoder,
    ACTEncoderLayer,
    ACTSinusoidalPositionEmbedding2d,
    ACTTemporalEnsembler,
    create_sinusoidal_pos_embedding,
)
from lerobot.policies.pretrained import PreTrainedPolicy
from lerobot.utils.constants import ACTION, OBS_ENV_STATE, OBS_IMAGES, OBS_STATE

from .configuration_act2 import DINOV2_BACKBONES, ACT2Config


class ACT2Policy(PreTrainedPolicy):
    """ACT2: Action Chunking Transformer with optional DINOv2 backbone / augmentation / flow head."""

    config_class = ACT2Config
    name = "act2"

    def __init__(self, config: ACT2Config, **kwargs):
        super().__init__(config)
        config.validate_features()
        self.config = config
        self.model = ACT2(config)
        if config.temporal_ensemble_coeff is not None:
            self.temporal_ensembler = ACTTemporalEnsembler(config.temporal_ensemble_coeff, config.chunk_size)
        self.reset()

    def get_optim_params(self) -> dict:
        return [
            {
                "params": [
                    p for n, p in self.named_parameters() if not n.startswith("model.backbone") and p.requires_grad
                ]
            },
            {
                "params": [
                    p for n, p in self.named_parameters() if n.startswith("model.backbone") and p.requires_grad
                ],
                "lr": self.config.optimizer_lr_backbone,
            },
        ]

    def reset(self):
        if self.config.temporal_ensemble_coeff is not None:
            self.temporal_ensembler.reset()
        else:
            self._action_queue = deque([], maxlen=self.config.n_action_steps)
        self._last_action: Tensor | None = None

    @torch.no_grad()
    def select_action(self, batch: dict[str, Tensor]) -> Tensor:
        self.eval()
        if self.config.temporal_ensemble_coeff is not None:
            actions = self.predict_action_chunk(batch)
            return self.temporal_ensembler.update(actions)

        if len(self._action_queue) == 0:
            actions = self.predict_action_chunk(batch)[:, : self.config.n_action_steps].clone()
            k = self.config.interp_steps
            if k > 0 and self._last_action is not None:
                # Linear blend from the last executed action into the new chunk (removes the servo jump).
                # Works on normalized actions; unnormalization is affine so the blend is preserved.
                w = torch.arange(1, k + 1, device=actions.device, dtype=actions.dtype) / k  # (k,) ends at 1
                w = w.view(1, k, 1)
                actions[:, :k] = (1 - w) * self._last_action.unsqueeze(1) + w * actions[:, :k]
            self._action_queue.extend(actions.transpose(0, 1))
        action = self._action_queue.popleft()
        self._last_action = action
        return action

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor]) -> Tensor:
        self.eval()
        if self.config.image_features:
            batch = dict(batch)
            batch[OBS_IMAGES] = [batch[key] for key in self.config.image_features]
        return self.model(batch)[0]

    def forward(self, batch: dict[str, Tensor]) -> tuple[Tensor, dict]:
        if self.config.image_features:
            batch = dict(batch)
            batch[OBS_IMAGES] = [batch[key] for key in self.config.image_features]

        valid_mask = ~batch["action_is_pad"].unsqueeze(-1)
        loss_dict = {}

        if self.config.action_head == "flow":
            pred_v, target_v, (mu_hat, log_sigma_x2_hat) = self.model.flow_training_step(batch)
            sq_err = F.mse_loss(pred_v, target_v, reduction="none")
            num_valid = valid_mask.sum() * sq_err.shape[-1]
            main_loss = (sq_err * valid_mask).sum() / num_valid.clamp_min(1)
            loss_dict["flow_loss"] = main_loss.item()
        else:
            actions_hat, (mu_hat, log_sigma_x2_hat) = self.model(batch)
            abs_err = F.l1_loss(batch[ACTION], actions_hat, reduction="none")
            num_valid = valid_mask.sum() * abs_err.shape[-1]
            main_loss = (abs_err * valid_mask).sum() / num_valid.clamp_min(1)
            loss_dict["l1_loss"] = main_loss.item()

        if self.config.use_vae and log_sigma_x2_hat is not None:
            mean_kld = (-0.5 * (1 + log_sigma_x2_hat - mu_hat.pow(2) - (log_sigma_x2_hat).exp())).sum(-1).mean()
            loss_dict["kld_loss"] = mean_kld.item()
            loss = main_loss + mean_kld * self.config.kl_weight
        else:
            loss = main_loss
        return loss, loss_dict


# ----------------------------------------------------------------------------------------------------- backbones
class DinoV2Backbone(nn.Module):
    """HF transformers Dinov2Model → (B, D, h, w) feature map (CLS/register tokens dropped)."""

    def __init__(self, config: ACT2Config):
        super().__init__()
        from transformers import Dinov2Config, Dinov2Model

        hub_id, hidden, patch = DINOV2_BACKBONES[config.vision_backbone]
        self.patch = patch
        self.hidden = hidden
        path = config.dinov2_pretrained_path
        model = None
        if path:
            try:
                model = Dinov2Model.from_pretrained(path)
            except Exception as e:  # noqa: BLE001 — offline device: weights come from the checkpoint anyway
                logging.warning("DINOv2 weights '%s' not loadable (%s) — random init; checkpoint weights override.", path, e)
        if model is None:
            heads = {384: 6, 768: 12}[hidden]
            model = Dinov2Model(Dinov2Config(hidden_size=hidden, num_hidden_layers=12, num_attention_heads=heads,
                                             patch_size=patch, image_size=config.image_size or 224))
        self.model = model
        self.out_channels = hidden
        if config.freeze_backbone:
            for p in self.model.parameters():
                p.requires_grad_(False)

    def train(self, mode: bool = True):
        super().train(mode)
        # frozen → keep deterministic (no dropout / drop-path) regardless of outer train()
        if not any(p.requires_grad for p in self.model.parameters()):
            self.model.eval()
        return self

    def forward(self, img: Tensor) -> dict[str, Tensor]:
        b, _, h, w = img.shape
        out = self.model(pixel_values=img).last_hidden_state  # (B, 1 + R + N, D)
        n = (h // self.patch) * (w // self.patch)
        tokens = out[:, -n:]  # drop CLS and any register tokens
        fmap = tokens.reshape(b, h // self.patch, w // self.patch, self.hidden).permute(0, 3, 1, 2)
        return {"feature_map": fmap}


def build_backbone(config: ACT2Config) -> tuple[nn.Module, int]:
    if config.is_dinov2:
        bb = DinoV2Backbone(config)
        return bb, bb.out_channels
    backbone_model = getattr(torchvision.models, config.vision_backbone)(
        replace_stride_with_dilation=[False, False, config.replace_final_stride_with_dilation],
        weights=config.pretrained_backbone_weights,
        norm_layer=FrozenBatchNorm2d,
    )
    bb = IntermediateLayerGetter(backbone_model, return_layers={"layer4": "feature_map"})
    if config.freeze_backbone:
        for p in bb.parameters():
            p.requires_grad_(False)
    return bb, backbone_model.fc.in_features


# ----------------------------------------------------------------------------------------------------- flow head
def timestep_embedding(t: Tensor, dim: int) -> Tensor:
    """Sinusoidal embedding of t in [0, 1] → (B, dim)."""
    half = dim // 2
    freqs = torch.exp(-math.log(10000.0) * torch.arange(half, device=t.device, dtype=t.dtype) / half)
    args = t[:, None] * 1000.0 * freqs[None]
    return torch.cat([torch.sin(args), torch.cos(args)], dim=-1)


class FlowActionExpert(nn.Module):
    """Predicts the flow velocity v(x_t, t | decoder tokens) over the action chunk.

    tokens = decoder_out + proj(x_t) + time_emb  →  self-attention over the chunk  →  Linear → velocity.
    """

    def __init__(self, config: ACT2Config, action_dim: int):
        super().__init__()
        d = config.dim_model
        self.action_in = nn.Linear(action_dim, d)
        self.time_mlp = nn.Sequential(nn.Linear(d, d), nn.SiLU(), nn.Linear(d, d))
        self.layers = nn.ModuleList([ACTEncoderLayer(config) for _ in range(config.flow_layers)])
        self.norm = nn.LayerNorm(d)
        self.out = nn.Linear(d, action_dim)
        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, cond: Tensor, x_t: Tensor, t: Tensor, pos_embed: Tensor) -> Tensor:
        # cond: (S, B, D) decoder output ; x_t: (B, S, A) ; t: (B,) ; pos_embed: (S, 1, D)
        temb = self.time_mlp(timestep_embedding(t, cond.shape[-1]))  # (B, D)
        h = cond + self.action_in(x_t).transpose(0, 1) + temb.unsqueeze(0)
        for layer in self.layers:
            h = layer(h, pos_embed=pos_embed)
        return self.out(self.norm(h)).transpose(0, 1)  # (B, S, A)


# ----------------------------------------------------------------------------------------------------- model
class ACT2(nn.Module):
    def __init__(self, config: ACT2Config):
        super().__init__()
        self.config = config
        action_dim = config.action_feature.shape[0]

        if config.use_vae:
            self.vae_encoder = ACTEncoder(config, is_vae_encoder=True)
            self.vae_encoder_cls_embed = nn.Embedding(1, config.dim_model)
            if config.robot_state_feature:
                self.vae_encoder_robot_state_input_proj = nn.Linear(config.robot_state_feature.shape[0], config.dim_model)
            self.vae_encoder_action_input_proj = nn.Linear(action_dim, config.dim_model)
            self.vae_encoder_latent_output_proj = nn.Linear(config.dim_model, config.latent_dim * 2)
            num_input_token_encoder = 1 + config.chunk_size + (1 if config.robot_state_feature else 0)
            self.register_buffer(
                "vae_encoder_pos_enc",
                create_sinusoidal_pos_embedding(num_input_token_encoder, config.dim_model).unsqueeze(0),
            )

        if config.image_features:
            self.backbone, feat_ch = build_backbone(config)

        self.encoder = ACTEncoder(config)
        self.decoder = ACTDecoder(config)

        if config.robot_state_feature:
            self.encoder_robot_state_input_proj = nn.Linear(config.robot_state_feature.shape[0], config.dim_model)
        if config.env_state_feature:
            self.encoder_env_state_input_proj = nn.Linear(config.env_state_feature.shape[0], config.dim_model)
        self.encoder_latent_input_proj = nn.Linear(config.latent_dim, config.dim_model)
        if config.image_features:
            self.encoder_img_feat_input_proj = nn.Conv2d(feat_ch, config.dim_model, kernel_size=1)
        n_1d_tokens = 1 + (1 if config.robot_state_feature else 0) + (1 if config.env_state_feature else 0)
        self.encoder_1d_feature_pos_embed = nn.Embedding(n_1d_tokens, config.dim_model)
        if config.image_features:
            self.encoder_cam_feat_pos_embed = ACTSinusoidalPositionEmbedding2d(config.dim_model // 2)

        self.decoder_pos_embed = nn.Embedding(config.chunk_size, config.dim_model)

        if config.action_head == "flow":
            self.flow_expert = FlowActionExpert(config, action_dim)
            g = torch.Generator().manual_seed(0)
            self.register_buffer("flow_noise", torch.randn(1, config.chunk_size, action_dim, generator=g))
        else:
            self.action_head = nn.Linear(config.dim_model, action_dim)

        for p in chain(self.encoder.parameters(), self.decoder.parameters()):
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    # ---- image pipeline ---------------------------------------------------------------------------------
    def _prep_image(self, img: Tensor) -> Tensor:
        if self.config.image_size is not None and tuple(img.shape[-2:]) != (self.config.image_size,) * 2:
            img = F.interpolate(img, size=(self.config.image_size, self.config.image_size), mode="bilinear",
                                align_corners=False)
        if self.training:
            img = self._augment(img)
        return img

    def _augment(self, img: Tensor) -> Tensor:
        b = img.shape[0]
        if self.config.aug_shift > 0:
            # per-sample random translation of ±aug_shift (fraction of size); affine_grid units are [-1, 1]
            shift = (torch.rand(b, 2, device=img.device, dtype=img.dtype) * 2 - 1) * (2 * self.config.aug_shift)
            theta = torch.zeros(b, 2, 3, device=img.device, dtype=img.dtype)
            theta[:, 0, 0] = 1
            theta[:, 1, 1] = 1
            theta[:, :, 2] = shift
            grid = F.affine_grid(theta, list(img.shape), align_corners=False)
            img = F.grid_sample(img, grid, padding_mode="border", align_corners=False)
        if self.config.aug_color > 0:
            j = self.config.aug_color
            c = 1 + (torch.rand(b, 1, 1, 1, device=img.device, dtype=img.dtype) * 2 - 1) * j
            d = (torch.rand(b, 1, 1, 1, device=img.device, dtype=img.dtype) * 2 - 1) * j
            img = img * c + d
        return img

    # ---- shared trunk: observations → decoder tokens (S, B, D) and VAE stats ----------------------------
    def _encode(self, batch: dict[str, Tensor]) -> tuple[Tensor, tuple[Tensor, Tensor] | tuple[None, None]]:
        if self.config.use_vae and self.training:
            assert ACTION in batch, "actions must be provided when using the variational objective in training mode."

        batch_size = batch[OBS_IMAGES][0].shape[0] if OBS_IMAGES in batch else batch[OBS_ENV_STATE].shape[0]
        ref = batch[OBS_STATE] if OBS_STATE in batch else (batch[OBS_IMAGES][0] if OBS_IMAGES in batch else batch[OBS_ENV_STATE])

        if self.config.use_vae and ACTION in batch and self.training:
            cls_embed = einops.repeat(self.vae_encoder_cls_embed.weight, "1 d -> b 1 d", b=batch_size)
            parts = [cls_embed]
            if self.config.robot_state_feature:
                parts.append(self.vae_encoder_robot_state_input_proj(batch[OBS_STATE]).unsqueeze(1))
            parts.append(self.vae_encoder_action_input_proj(batch[ACTION]))
            vae_in = torch.cat(parts, dim=1)
            pos_embed = self.vae_encoder_pos_enc.clone().detach()
            cls_joint_is_pad = torch.full((batch_size, 2 if self.config.robot_state_feature else 1), False,
                                          device=ref.device)
            key_padding_mask = torch.cat([cls_joint_is_pad, batch["action_is_pad"]], dim=1)
            cls_out = self.vae_encoder(vae_in.permute(1, 0, 2), pos_embed=pos_embed.permute(1, 0, 2),
                                       key_padding_mask=key_padding_mask)[0]
            latent_pdf = self.vae_encoder_latent_output_proj(cls_out)
            mu = latent_pdf[:, : self.config.latent_dim]
            log_sigma_x2 = latent_pdf[:, self.config.latent_dim :]
            latent = mu + log_sigma_x2.div(2).exp() * torch.randn_like(mu)
        else:
            mu = log_sigma_x2 = None
            latent = torch.zeros([batch_size, self.config.latent_dim], dtype=torch.float32, device=ref.device)

        tokens = [self.encoder_latent_input_proj(latent)]
        pos = list(self.encoder_1d_feature_pos_embed.weight.unsqueeze(1))
        if self.config.robot_state_feature:
            state = batch[OBS_STATE]
            if self.training and self.config.state_dropout > 0:
                keep = (torch.rand(batch_size, 1, device=state.device) >= self.config.state_dropout).to(state.dtype)
                state = state * keep
            tokens.append(self.encoder_robot_state_input_proj(state))
        if self.config.env_state_feature:
            tokens.append(self.encoder_env_state_input_proj(batch[OBS_ENV_STATE]))

        if self.config.image_features:
            for img in batch[OBS_IMAGES]:
                img = self._prep_image(img)
                cam_features = self.backbone(img)["feature_map"]
                cam_pos = self.encoder_cam_feat_pos_embed(cam_features).to(dtype=cam_features.dtype)
                cam_features = self.encoder_img_feat_input_proj(cam_features)
                tokens.extend(list(einops.rearrange(cam_features, "b c h w -> (h w) b c")))
                pos.extend(list(einops.rearrange(cam_pos, "b c h w -> (h w) b c")))

        tokens = torch.stack(tokens, dim=0)
        pos = torch.stack(pos, dim=0)
        encoder_out = self.encoder(tokens, pos_embed=pos)
        decoder_in = torch.zeros((self.config.chunk_size, batch_size, self.config.dim_model),
                                 dtype=pos.dtype, device=pos.device)
        decoder_out = self.decoder(decoder_in, encoder_out, encoder_pos_embed=pos,
                                   decoder_pos_embed=self.decoder_pos_embed.weight.unsqueeze(1))  # (S, B, D)
        return decoder_out, (mu, log_sigma_x2)

    # ---- heads -----------------------------------------------------------------------------------------
    def forward(self, batch: dict[str, Tensor]) -> tuple[Tensor, tuple[Tensor, Tensor] | tuple[None, None]]:
        """Returns (B, chunk_size, action_dim) actions. For the flow head this runs the Euler sampler."""
        decoder_out, vae_stats = self._encode(batch)
        if self.config.action_head == "flow":
            return self._flow_sample(decoder_out), vae_stats
        return self.action_head(decoder_out.transpose(0, 1)), vae_stats

    def flow_training_step(self, batch: dict[str, Tensor]) -> tuple[Tensor, Tensor, tuple]:
        """One flow-matching training step: returns (predicted velocity, target velocity, vae stats)."""
        decoder_out, vae_stats = self._encode(batch)
        a1 = batch[ACTION]  # (B, S, A) normalized actions
        a0 = torch.randn_like(a1)
        t = torch.sigmoid(torch.randn(a1.shape[0], device=a1.device, dtype=a1.dtype) * self.config.flow_time_sigma)
        x_t = t.view(-1, 1, 1) * a1 + (1 - t.view(-1, 1, 1)) * a0
        pred_v = self.flow_expert(decoder_out, x_t, t, self.decoder_pos_embed.weight.unsqueeze(1))
        return pred_v, a1 - a0, vae_stats

    def _flow_sample(self, decoder_out: Tensor) -> Tensor:
        s, b, _ = decoder_out.shape
        action_dim = self.config.action_feature.shape[0]
        if self.config.flow_fixed_noise:
            x = self.flow_noise.to(dtype=decoder_out.dtype).expand(b, s, action_dim).clone()
        else:
            x = torch.randn(b, s, action_dim, device=decoder_out.device, dtype=decoder_out.dtype)
        k = self.config.flow_steps
        pos = self.decoder_pos_embed.weight.unsqueeze(1)
        for i in range(k):
            t = torch.full((b,), i / k, device=x.device, dtype=x.dtype)
            x = x + self.flow_expert(decoder_out, x, t, pos) / k
        return x
