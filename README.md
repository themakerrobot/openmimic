<h1 align="center">OpenMimic</h1>

<p align="center">
  <b>한국어</b> · <a href="README.en.md">English</a>
</p>

<p align="center">
  <b>로봇팔 모방학습, 브라우저 하나로.</b><br>
  셋업부터 데이터 수집 · 학습 · NPU 실행까지 — 명령어 없이<br>
  <b>기본은 전부 온디바이스 · 오프라인</b> — 학습이 무거울 때만 클라우드 GPU 를 골라 씁니다
</p>

<p align="center">
  SO-ARM101 · ROBOTIS OMX · 한팔 / 양팔 &nbsp;·&nbsp; Ubuntu · Windows 네이티브 &nbsp;·&nbsp; Intel NPU · NVIDIA · Jetson Thor
</p>

<p align="center">
  <img alt="Windows native" src="https://img.shields.io/badge/Windows-native-0078D6">
  <img alt="Single and bimanual" src="https://img.shields.io/badge/arms-single%20%7C%20bimanual-2ea44f">
  <img alt="Intel NPU" src="https://img.shields.io/badge/Intel%20NPU-OpenVINO%20FP16%20%7C%20INT8-0071C5">
</p>

<p align="center">
  <img src="docs/img/arms.gif" alt="SO-ARM101 과 OMX" width="880">
</p>

---

HuggingFace [lerobot](https://github.com/huggingface/lerobot) 위에서 돌아갑니다.
팔을 꽂은 PC 에서 OpenMimic 을 켜고, 같은 네트워크의 노트북·태블릿 브라우저로 접속하면 끝입니다.

## 네 단계, 한 화면

| ① 셋업 | ② 수집 | ③ 학습 | ④ 실행 |
|:---:|:---:|:---:|:---:|
| ![](docs/img/wizard_diag.png) | ![](docs/img/collect_run.png) | ![](docs/img/training.png) | ![](docs/img/rollout_live.png) |
| 팔을 손으로 움직이면 포트·캘리브레이션이 끝 | 리더 팔로 시연, 키 하나로 녹화 | ACT · ACT2 · Diffusion · SmolVLA | 카메라 · 3D 를 보며 자율 구동 |

## 무엇이 다른가

- **명령어 0줄** — 포트 찾기, 모터 진단, 캘리브레이션, 녹화, 학습, 실행까지 전부 화면에서. 셋업 마법사가 한 단계씩 안내합니다.
- **온디바이스 · 오프라인** — 셋업 · 수집 · 학습 · 실행이 모두 로봇 옆 PC 한 대에서 돌고, 데이터가 밖으로 나가지 않습니다. 설치가 끝나면 인터넷이 없어도 됩니다.
- **GPU 없는 노트북에서 NPU 실행** — 학습한 모델을 OpenVINO **FP16 · INT8** 로 바꿔 Intel Core Ultra **NPU** 에서 구동합니다.
  변환할 때 PyTorch 대비 오차와 추론 시간을 장치별로 재서 보여 줍니다.
- **Windows 네이티브** — WSL 없이 Ubuntu 와 같은 기능. 데이터셋 · 캘리브레이션 · 모델을 두 OS 사이에 그대로 옮깁니다.
- **한팔도 양팔도** — SO-ARM101 · ROBOTIS OMX 를 한팔 · 양팔로. 전환은 버튼 하나.
- **학습은 원하는 곳에서** — 내 PC 의 NVIDIA GPU · Jetson Thor · Intel 내장 GPU. 오래 걸리면 Hugging Face 클라우드 GPU(HF Jobs)를
  그때만 시간 단위로 빌립니다 (T4 시간당 $0.40 부터, 학습 화면에 요금 표시 — 이때만 데이터셋이 내 계정의 비공개 저장소로 올라갑니다).
- **팔을 지키는 안전장치** — 시작 전 전원 점검으로 어댑터 혼용을 막고, 실행 중 서보 온도 · 전압을 감시해 이상하면 자동 정지.
- **좋은 데이터** — 실제 수집 주기 표시, 카메라 동기 재생 · 3D · 관절 그래프로 에피소드 리뷰, 불량 에피소드 일괄 삭제, 끊긴 데이터셋 복구.
- **실기 성공률** — 실행 중 S / F 키로 기록해 체크포인트별 성공률을 비교합니다.
- **한/EN** — 버튼 하나로 화면 언어 전환.

## 지원

| | |
|---|---|
| 로봇팔 | **SO-ARM101** (Feetech STS3215) · **ROBOTIS OMX** (Dynamixel) — 한팔 / 양팔 |
| OS | **Ubuntu** 22.04 · 24.04 · 26.04 · **Windows** 10 · 11 (네이티브) |
| 학습 | 온디바이스: NVIDIA GPU · Jetson Thor · Intel 내장 GPU(XPU) · 선택: HF Jobs 클라우드 GPU(시간 단위) |
| 실행 (온디바이스) | Intel Core Ultra **NPU** / GPU / CPU (OpenVINO FP16 · INT8) · NVIDIA GPU · Jetson Thor |
| 정책 | ACT · ACT2 · Diffusion · SmolVLA — [비교](docs/POLICIES.md) |

> **실기로 확인** — Lunar Lake 노트북(Core Ultra 200V · RAM 32 GB · Windows 11) 한 대로 SO-ARM101 양팔 · 카메라 3대를
> 셋업 → 수집 → 학습 → OpenVINO FP16 / INT8 변환 → **NPU 실행**까지 한 번에 돌렸습니다.

## 시작하기

**Ubuntu**

```bash
git clone https://github.com/themakerrobot/openmimic.git ~/project/openmimic && cd ~/project/openmimic
./lerobot_conda.sh          # 설치 — 끝나면 한 번 로그아웃 후 다시 로그인
./openmimic.sh start        # 켜기 — 접속 주소가 나옵니다
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/themakerrobot/openmimic.git $HOME\project\openmimic; cd $HOME\project\openmimic
powershell -ExecutionPolicy Bypass -File .\install.ps1     # 설치
.\OpenMimic.bat                                            # 켜기 — 다음부터는 탐색기에서 더블클릭
```

브라우저로 **`http://<PC IP>:8080`** 에 들어가면 셋업 마법사가 나머지를 안내합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [사용 설명서](docs/GUIDE.md) | 셋업 마법사, 화면별 사용법, 팔 여러 세트, 안전장치, 문제 해결 |
| [설치 · 운영](docs/INSTALL.md) | OS 별 설치, 켜기·끄기·자동 시작, 업데이트, 파일 위치, 보안 |
| [Intel NPU](docs/INTEL.md) | OpenVINO 변환 · NPU 실행, Intel 내장 GPU 학습, 실측값 |
| [학습 정책](docs/POLICIES.md) | ACT · ACT2 · Diffusion · SmolVLA 비교, 양팔 지원 |
| [Windows 지원](docs/WINDOWS_PORT.md) | OS 차이를 메운 방법, 실기 확인 기록 |
| [개발 문서](docs/DEVELOPMENT.md) | 구조, 설계 이유, 설정 파일 형식, 판정 근거 |

## License

- 코드: MIT — [LICENSE](LICENSE)
- `urdf/` SO-101 URDF·STL: [TheRobotStudio SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) (Apache 2.0) — [urdf/LICENSE.md](urdf/LICENSE.md)
- `urdf/omx_f.urdf`, `urdf/open_manipulator_description/`: [ROBOTIS open_manipulator](https://github.com/ROBOTIS-GIT/open_manipulator) (Apache 2.0) — [urdf/LICENSE-omx.md](urdf/LICENSE-omx.md)
- `plugins/`: 양팔 OMX 는 lerobot `bi_so_follower` / `bi_so_leader` 구조를 따르고, ACT2 는 lerobot ACT 를 바탕으로 한 코드 (Apache 2.0)
- `tools_dsrepair.py`: [huggingface/leLab](https://github.com/huggingface/leLab) `dataset_repair.py` 를 옮겨 고친 코드 (Apache 2.0, 파일 머리에 표기)
