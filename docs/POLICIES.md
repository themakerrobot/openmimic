# 학습 정책 상세

학습 탭에서 고를 수 있는 정책과, 고를 때 알아 둘 것입니다. (lerobot `e40b58a` 기준)
설계 이유와 lerobot 내부 동작은 [DEVELOPMENT.md](DEVELOPMENT.md#학습-정책별-처리-train_policies--policy_train_args) 에 있습니다.

## 한눈에

| 정책 | 언제 | 어디서 학습 | 한팔 | 양팔 | 참고 |
|---|---|---|---|---|---|
| **ACT** (기본) | 데모 50개 안팎, 가볍고 빠름 | 이 기기 (CPU·GPU·Intel XPU) | ✅ | ✅ | **OpenVINO(NPU) 변환 가능** |
| **ACT2** (실험) | ACT 의 성능을 올려 보려는 실험 — 증강·청크 보간·DINOv2 백본·flow 헤드를 켜고 끔 | 이 기기 (CPU·GPU·Intel XPU) | ✅ | ✅ | **OpenVINO 변환 가능** (flow 는 실측 전). 이 레포 `plugins/lerobot_policy_act2` |
| **Diffusion** | 동작이 여러 갈래인 태스크 | 이 기기 (GPU 권장) | ✅ | ✅ | 카메라 해상도가 모두 같아야 함. **빠른 추론 (DDIM 10회)** 기본 켜짐 |
| **SmolVLA** | 태스크 설명(언어)을 쓰는 450M 모델 미세조정 | GPU 권장 · HF Jobs | ✅ | ✅ (실측 전) | 처음 한 번 인터넷으로 내려받음 |

정책을 바꾸면 그 정책에 맞는 기본 step 수가 들어가고, 데이터셋·정책 조합을 **시작 전에 점검**합니다
(예: Diffusion 인데 카메라 해상도가 다름, 카메라 없는 데이터셋 → 빨간 표시로 막음).

## 선택지

- **빠른 추론 (DDIM 10회)** — Diffusion. 디노이징을 100회 대신 10회만 합니다. GPU 없는 기기에서 동작 묶음 하나가
  23초 → 2.4초로 줄었습니다(시험 서버 CPU 4코어). GPU 없는 기기에서 롤아웃하려면 켜 두세요.
- **ImageNet 백본 없이 (오프라인)** — ACT·Diffusion 은 처음 학습할 때 ResNet18 가중치(약 45MB)를 `download.pytorch.org` 에서 받습니다.
  인터넷이 없는 기기면 켜세요 (데이터가 적으면 성능이 조금 떨어질 수 있음).
- **AMP** — CUDA 에서 메모리·시간 절약. 손실이 튀면 끄세요.
- 필요한 패키지가 없으면 정책 목록에 "패키지 미설치" 가 붙고 **필요한 패키지 설치** 버튼이 생깁니다
  (torch 는 지금 버전으로 고정한 채 설치 — XPU/CUDA 휠이 바뀌지 않음).

## ACT2 — ACT 개선 실험 (`plugins/lerobot_policy_act2`)

lerobot 에 없는 정책이라 이 레포의 플러그인으로 넣었습니다 (`--policy.type=act2`). lerobot 의 ACT(e40b58a) 트랜스포머 블록을
그대로 import 하고, 아래 선택지를 설정으로 붙였습니다. **아무것도 안 켜면 ACT 와 같은 모델**이므로 같은 데이터·step 으로
ACT 와 나란히 학습해 실행 탭의 성공률을 비교하는 용도입니다.

| 선택지 (학습 탭) | lerobot 인자 | 하는 일 | 비용 |
|---|---|---|---|
| 영상 증강 + 상태 드롭아웃 (기본 켬) | `aug_shift=0.06 aug_color=0.2 state_dropout=0.1` | 학습 때 영상을 ±6% 밀고 밝기·대비를 흔듦, 10% 확률로 관절 상태를 0 으로 | 없음 |
| 청크 경계 보간 (기본 켬) | `n_action_steps=30 interp_steps=10` | 100 스텝 예측 중 30 만 실행 후 재예측, 새 청크 앞 10 스텝을 직전 동작에서 선형 블렌드 | 추론 횟수 3배 (ACT 기본은 100 스텝 전부 실행) |
| DINOv2 백본 (고정) | `vision_backbone=dinov2_small image_size=224` | ResNet18 → DINOv2-small(HF `facebook/dinov2-small`, 22M) 고정, 224×224 로 리사이즈 (리사이즈는 모델 안에 있어 OpenVINO 에도 포함) | 처음 한 번 약 90MB 다운로드. 학습 파라미터는 줄어듦(백본 고정) |
| flow matching 헤드 | `action_head=flow use_vae=false flow_steps=5` | 디코더 토큰 위에 작은 flow 전문가(2층)를 두고 속도장을 학습, 추론은 Euler 5 스텝 | 추론 시간 ~ 1.5~2배 |
| ImageNet 백본 없이 | `pretrained_backbone_weights=null` | ResNet/DINOv2 사전학습 가중치를 받지 않음 (오프라인) | — |

- 선택지 조합은 자유입니다. "ACT vs ACT2(증강+보간)" 처럼 한 가지씩 바꿔 가며 비교하는 게 원인을 알기 쉽습니다.
- `flow_fixed_noise=true`(기본)라 flow 추론은 매번 같은 시작 잡음을 써서 결정적입니다 — OpenVINO 정합성 검증이 가능합니다.
  `--policy.flow_fixed_noise=false` 로 끄면 샘플링이 됩니다.
- Intel XPU·CPU·CUDA 어디서나 학습됩니다. DINOv2 는 `transformers` 가 필요합니다 (SmolVLA 와 같은 패키지).
- 확인한 것 (CPU, 더미 데이터셋): 모든 조합 학습 → 체크포인트 → `lerobot-rollout` 경로 로드 → OpenVINO FP16 변환·정합성
  (최대오차 0.004). 실제 팔 성공률·Intel NPU 지연은 **아직 실측 전**입니다.
- OpenVINO 변환 시 ACT2 의 `predict_action_chunk` 만 바꾸므로 청크 보간·큐는 그대로 동작합니다.
- **비교 벤치마크**: `python tools_act2bench.py --dataset <데이터셋> --variants act,act2,act2-dino,act2-flow --steps 20000 [--ov]`
  — 변형들을 같은 step·시드로 차례로 학습해 `outputs/bench_<데이터셋>_<변형>_s<시드>/` 에 두고, 학습 시간·loss·파라미터·
  추론 ms(PyTorch, --ov 면 OpenVINO 장치별)를 `outputs/act2bench_<시각>.md/.csv` 로 모읍니다. 체크포인트는 실행 탭 목록에
  그대로 보이므로 실기 성공률을 기록한 뒤 `--report-only` 로 다시 뽑으면 표에 성공률이 들어갑니다.

## 허브에서 받은 모델

허브 탭에서 받은 모델의 카메라 이름이 셋업과 다르면 **전경 카메라 → 손목 카메라** 순으로 자동 연결하고,
롤아웃 사전 점검에 노란 표시로 보여 줍니다 (예: `카메라 연결: top → …, wrist → …`).
pi0·GR00T 처럼 CUDA 용 대형 모델은 GPU 없는 기기에서 고르면 "매우 느리거나 메모리가 부족할 수 있음" 경고가 뜹니다.

## 양팔에서 확인한 것

관절 12개·카메라 3대(top·left_wrist·right_wrist) 데이터셋으로 실제 lerobot 학습 → 가상 양팔 롤아웃까지 확인했습니다.

| 정책 | 양팔 학습 | 가상 양팔 롤아웃 |
|---|---|---|
| ACT | ✅ | ✅ 29.9 Hz |
| Diffusion (DDIM 10회) | ✅ | ✅ 5.1 Hz (CPU) |

## 넣지 않은 정책

| 정책 | 이유 |
|---|---|
| X-VLA · MolmoAct2 | CUDA 전용 대형 모델(0.9B · 21.8GB 가중치) — Intel PC 에서 학습·실행 불가라 뺐습니다 |
| pi0 / pi05 / pi0_fast | 3B, VRAM 24~40 GB — 클라우드 전용 |
| GR00T N1.7 | SO-101 레시피가 RTC 롤아웃 필수 — OpenMimic 롤아웃은 아직 sync 방식 |
| TD-MPC | 보상(`next.reward`) 필요 · 정사각형 이미지만 — 원격조작 데이터로 학습 불가 (실측) |
| gaussian_actor | 온라인 강화학습(HIL-SERL) 전용 — `lerobot-train` 으로 학습 불가 (실측) |
| VQ-BeT | 카메라 1대만, 처음 2만 step 은 VQ-VAE 만 학습 |
| lingbot_va · fastwam · wall_x · eo1 · vla_jepa · evo1 | 엔드이펙터 전용·5B 이상·SO-101 안내 없음 등 |

## 학습 장치

학습 탭이 이 기기의 학습 장치를 보여 줍니다 — `GPU: …` / `Intel GPU(XPU) … — 실험적` / `CPU 로 학습 — 매우 느립니다`.
Intel PC 에서의 학습은 [INTEL.md](INTEL.md#intel-기기에서-학습) 를 보세요.
