# ============================================================================
#  LeRobot + OpenMimic 환경 셋업 (Windows 10/11, conda, docker 미사용) — lerobot_conda.sh 의 Windows 판
#  두 가지 플랫폼을 자동 분기합니다.
#    win-intel : Windows + NVIDIA 없음 (Intel Core Ultra 권장)
#                → PyTorch CPU 휠 + OpenVINO/NNCF. 추론은 NPU/GPU/CPU 를 OpenVINO 로 씁니다.
#                학습은 CPU 휠로는 매우 느립니다 — HF Jobs 클라우드 학습 또는 NVIDIA 기기를 쓰세요.
#                실험적: $env:OPENMIMIC_TORCH='xpu' 면 PyTorch XPU 휠을 시도합니다 (못 받으면 CPU 휠로 계속).
#    win-cuda  : Windows + NVIDIA GPU → PyTorch CUDA 13 휠
#  강제 지정: $env:OPENMIMIC_PLATFORM='win-intel' (win-intel | win-cuda)
#
#  새 기기에서 (PowerShell):
#     git clone https://github.com/themakerrobot/openmimic.git $HOME\project\openmimic
#     cd $HOME\project\openmimic
#     powershell -ExecutionPolicy Bypass -File .\install.ps1
#
#  끝나면:
#     powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start     # 켜기 — 접속 주소 출력 (stop · status · log · update · autostart on)
#
#  다른 사람 환경을 건드리지 않습니다:
#   - conda(miniforge)는 %USERPROFILE%\miniforge3 에 '설치만' 합니다 (conda init 안 함, PATH·레지스트리 등록 안 함).
#   - 파이썬 패키지는 전부 conda 환경 'openmimic' 안에만 들어갑니다. 쓸 때만 . .\activate.ps1 로 그 창에서 켭니다.
#   - Linux 판의 apt·udev·그룹 단계는 없습니다. 시리얼 보드 드라이버(CH340/CH343·CP210x)는 Windows 가 잡거나 장치 관리자에서 확인합니다.
#  로그: 레포 폴더의 lerobot_conda.log (Linux 판과 같은 형식, 끝에 "=== 완료 ===")
# ============================================================================
#Requires -Version 5.1
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$env:PYTHONUTF8 = '1'
# 다른 파이썬 설치가 남긴 사용자 site-packages(%APPDATA%\Python)·PYTHONPATH 가 conda 환경보다 먼저 잡히지 않게
$env:PYTHONNOUSERSITE = '1'
Remove-Item Env:\PYTHONPATH, Env:\PYTHONHOME -ErrorAction SilentlyContinue

# ------------------------------- 설정 ---------------------------------------
$WorkDir = $PSScriptRoot                                   # 이 레포 (main.py 가 있는 곳)
$LogFile = Join-Path $WorkDir 'lerobot_conda.log'
$CondaDir = Join-Path $env:USERPROFILE 'miniforge3'
$EnvName = 'openmimic'
$EnvDir = Join-Path $CondaDir "envs\$EnvName"
$Py = Join-Path $EnvDir 'python.exe'
$PyVer = '3.12'
$LerobotSrc = Join-Path $WorkDir 'lerobot-src'
$LerobotCommit = 'e40b58a8dfa9e7b86918c374791599d070518d11'   # README·lerobot_conda.sh 와 동일
$DataDir = Join-Path $WorkDir 'data'
$TorchPkgs = @('torch<2.12', 'torchvision<0.27')             # lerobot e40b58a 요구 범위
$TorchFlavor = if ($env:OPENMIMIC_TORCH) { $env:OPENMIMIC_TORCH } else { 'cpu' }
$Platform = $env:OPENMIMIC_PLATFORM

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Write-Log([string]$Line) {
    Write-Host $Line
    Add-Content -LiteralPath $LogFile -Value $Line -Encoding UTF8
}
function Now { Get-Date -Format 'yyyy-MM-dd HH:mm:ss' }
function Log([string]$Msg) { Write-Log ''; Write-Log "[$(Now)] === $Msg ===" }
function Warn([string]$Msg) { Write-Log "[$(Now)] !! $Msg" }
function Die([string]$Msg) { Write-Log "[$(Now)] XX 치명적 실패: $Msg"; exit 1 }

# 네이티브 명령을 실행하며 출력(표준오류 포함)을 화면·로그에 같이 남깁니다. 종료 코드를 돌려줍니다.
function Invoke-Logged([string]$Exe, [string[]]$ArgList) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'          # 네이티브 명령의 stderr 를 오류로 보지 않게 (PowerShell 5.1)
    try {
        # 표준오류 줄은 ErrorRecord 로 옵니다 — 빈 줄이면 문자열이 'System.Management.Automation.RemoteException' 이 되므로 메시지만
        & $Exe @ArgList 2>&1 | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) { Write-Log $_.Exception.Message } else { Write-Log ("$_") }
        }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $old
    }
}
# 파이썬 코드를 임시 파일로 실행 — PowerShell 5.1 은 네이티브 명령 인자 속 큰따옴표를 제대로 넘기지 못합니다
function Invoke-Py([string]$Code) {
    $f = Join-Path $env:TEMP ("openmimic_install_{0}.py" -f ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    [System.IO.File]::WriteAllText($f, $Code, (New-Object System.Text.UTF8Encoding($false)))
    try { return (Invoke-Logged $Py @($f)) } finally { Remove-Item -LiteralPath $f -ErrorAction SilentlyContinue }
}
function Pip([string[]]$ArgList) { return (Invoke-Logged $Py (@('-m', 'pip') + $ArgList)) }
# 파이썬 한 줄의 표준출력만 받습니다 (실패하면 빈 문자열). 'Stop' 상태에서 네이티브 stderr 가 종료 오류가 되지 않게 잠시 풉니다
function Get-PyOut([string]$Code) {
    if (-not (Test-Path $Py)) { return '' }
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Py -c $Code 2>$null
        if ($LASTEXITCODE -ne 0) { return '' }
        return (($out | Out-String).Trim())
    } finally {
        $ErrorActionPreference = $old
    }
}
# conda activate 와 같은 순서로 환경 폴더를 PATH 앞에 붙입니다 (conda init·훅 스크립트 없이)
function Enable-CondaEnv {
    $add = @($EnvDir, (Join-Path $EnvDir 'Library\mingw-w64\bin'), (Join-Path $EnvDir 'Library\usr\bin'),
             (Join-Path $EnvDir 'Library\bin'), (Join-Path $EnvDir 'Scripts'), (Join-Path $EnvDir 'bin'))
    $env:PATH = (($add -join ';') + ';' + $env:PATH)
    $env:CONDA_PREFIX = $EnvDir
    $env:CONDA_DEFAULT_ENV = $EnvName
}

# ------------------------------- 플랫폼 -------------------------------------
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { Die "x86_64(AMD64) Windows 전용입니다 (현재 $env:PROCESSOR_ARCHITECTURE)" }
if (-not $Platform) {
    $nvidia = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
    if (-not $nvidia) {
        $nvidia = [bool](Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue | Where-Object { $_.Name -match 'NVIDIA' })
    }
    $Platform = if ($nvidia) { 'win-cuda' } else { 'win-intel' }
}
switch ($Platform) {
    'win-cuda' { $TorchIndexes = @('https://download.pytorch.org/whl/cu130') }
    'win-intel' {
        switch ($TorchFlavor) {
            'cpu' { $TorchIndexes = @('https://download.pytorch.org/whl/cpu', 'https://pypi.org/simple') }
            # Windows XPU 휠로 Intel GPU 학습이 되는 것을 실기로 확인했습니다 (Arc 140V, ACT 5000 step). 못 받으면 CPU 휠로 내려갑니다
            'xpu' { $TorchIndexes = @('https://download.pytorch.org/whl/xpu', 'https://download.pytorch.org/whl/cpu', 'https://pypi.org/simple') }
            default { Die "OPENMIMIC_TORCH 는 cpu | xpu 중 하나: $TorchFlavor" }
        }
    }
    default { Die "OPENMIMIC_PLATFORM 은 win-intel | win-cuda 중 하나: $Platform" }
}

Log "시작. arch=$env:PROCESSOR_ARCHITECTURE platform=$Platform 작업경로=$WorkDir"
if (-not (Test-Path (Join-Path $WorkDir 'main.py'))) { Die "$WorkDir\main.py 가 없습니다. 이 레포를 clone 한 폴더에서 실행하세요" }
if ($CondaDir -match '\s') { Die "사용자 폴더 경로에 공백이 있어 conda 를 설치할 수 없습니다: $CondaDir" }
if ($CondaDir -match '[^\x00-\x7F]') {
    # TODO(win-verify): 한글 등 비ASCII 사용자 폴더에서 miniforge·pip 가 정상 동작하는지
    Warn "사용자 폴더 경로에 영문이 아닌 글자가 있습니다 ($CondaDir) — conda/pip 가 실패하면 영문 계정에서 설치하세요"
}

# ------------------------------------------------------------- 0. 필수 도구
Log "0. 필수 도구 확인 (git)"
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Die "git 이 없습니다 — winget install --id Git.Git -e 로 설치한 뒤 새 PowerShell 창에서 다시 실행하세요"
}
Invoke-Logged 'git' @('--version') | Out-Null

# --------------------------------------------------------------- 1. miniforge
# 이 환경의 파이썬이 돌고 있으면(OpenMimic·작업) pip 가 쓰는 중인 .pyd/.dll 을 바꾸지 못해 WinError 5 로 실패합니다
$busyPy = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($EnvDir, [System.StringComparison]::OrdinalIgnoreCase) }
if ($busyPy) { Die "openmimic 환경의 파이썬이 실행 중입니다 (pid $(@($busyPy.ProcessId) -join ',')) — .\openmimic.ps1 stop 과 작업 종료 후 다시 실행하세요" }

Log "1. miniforge 설치"
$CondaExe = Join-Path $CondaDir 'Scripts\conda.exe'
if (-not (Test-Path $CondaExe)) {
    $inst = Join-Path $env:TEMP 'Miniforge3-Windows-x86_64.exe'
    Write-Log "내려받는 중: Miniforge3-Windows-x86_64.exe"
    Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Windows-x86_64.exe' -OutFile $inst
    # /D 는 반드시 마지막·따옴표 없이 (NSIS 규칙). PATH·레지스트리 등록 안 함
    $p = Start-Process -FilePath $inst -ArgumentList @('/InstallationType=JustMe', '/RegisterPython=0', '/AddToPath=0', '/S', "/D=$CondaDir") -Wait -PassThru
    if ($p.ExitCode -ne 0 -or -not (Test-Path $CondaExe)) { Die "miniforge 설치 실패 (종료 코드 $($p.ExitCode))" }
} else {
    Write-Log "이미 설치됨: $CondaDir"
}

# ---------------------------------------------------------------- 2. conda env
Log "2. conda 환경 생성 (python $PyVer)"
if (Test-Path $Py) {
    Write-Log "환경 이미 존재: $EnvName"
} else {
    # -p 로 위치를 고정하고 conda-forge 만 씁니다 — 기존 conda 의 ~\.condarc(envs_dirs·channels)를 따라가지 않게
    if ((Invoke-Logged $CondaExe @('create', '-p', $EnvDir, "python=$PyVer", '-y', '-q', '--override-channels', '-c', 'conda-forge')) -ne 0) { Die "conda 환경 생성 실패" }
}
Enable-CondaEnv
Invoke-Logged $Py @('-V') | Out-Null
# setuptools 는 lerobot(<81)·torch(<82) 요구 범위 안에서만 올립니다 — 최신으로 올리면 다시 설치할 때 의존성 충돌 오류가 찍힙니다
if ((Pip @('install', '--upgrade', 'pip', 'wheel', 'setuptools<81')) -ne 0) { Die "pip 업그레이드 실패" }

# ------------------------------------------------------------------ 3. PyTorch
Log "3. PyTorch 설치 ($Platform)"
# 다시 설치할 때 다른 종류의 휠(예: 2.11.0+cpu)이 남아 있으면 pip 가 "이미 만족" 으로 보고 바꾸지 않습니다 → 먼저 지웁니다
$OldLocal = Get-PyOut "import torch;v=torch.__version__;print(v.split('+')[1] if '+' in v else '')"
$want = if ($Platform -eq 'win-cuda') { 'cu130' } else { $TorchFlavor }
if ($OldLocal -and $OldLocal -ne $want) {
    Write-Log "--- 기존 torch(+$OldLocal) 를 지우고 $want 휠로 바꿉니다"
    Pip @('uninstall', '-y', 'torch', 'torchvision') | Out-Null
}
$TorchOk = $false
foreach ($idx in $TorchIndexes) {
    Write-Log "--- 인덱스 시도: $idx"
    if ((Pip (@('install', '--index-url', $idx) + $TorchPkgs)) -eq 0) {
        $TorchOk = $true
        Write-Log "--- 성공: $idx"
        break
    }
    Warn "실패: $idx"
}
if (-not $TorchOk) { Die "PyTorch 설치 실패. 인덱스 경로 확인 후 TorchIndexes 수정" }
$env:TORCH_LOCAL = Get-PyOut "import torch;v=torch.__version__;print(v.split('+')[1] if '+' in v else '')"
if ($Platform -eq 'win-intel' -and $TorchFlavor -eq 'xpu' -and $env:TORCH_LOCAL -ne 'xpu') {
    Warn "XPU 휠을 받지 못해 $(if ($env:TORCH_LOCAL) { $env:TORCH_LOCAL } else { '기본' }) 휠을 설치했습니다 — Intel GPU 학습 없이 계속합니다 (추론은 OpenVINO 로 그대로 가능)"
}

$CheckTorch = @'
import os, sys, torch
plat = os.environ.get("OPENMIMIC_PLATFORM_RESOLVED", "")
local = torch.__version__.split("+")[1] if "+" in torch.__version__ else ""
if plat == "win-intel":
    want = os.environ.get("TORCH_LOCAL", "")
    xpu = hasattr(torch, "xpu") and torch.xpu.is_available()
    print("torch      :", torch.__version__, "| xpu:", xpu, "(추론 가속은 OpenVINO 가 담당)")
    if xpu:
        print("xpu device :", torch.xpu.get_device_name(0))
    elif local == "xpu":
        print("!! XPU 휠이지만 Intel GPU 를 못 잡았습니다 — Intel 그래픽 드라이버 확인. 학습은 CPU 로 돌아갑니다")
    if want == "xpu" and local != "xpu":
        print("!! XPU 휠이 다른 휠로 바뀌었습니다"); sys.exit(1)
    sys.exit(0)
print("torch      :", torch.__version__)
print("cuda avail :", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device     :", torch.cuda.get_device_name(0))
else:
    print("!! CUDA 미인식 — 잘못된 휠이거나 NVIDIA 드라이버가 CUDA 13 을 지원하지 않습니다.")
    sys.exit(1)
'@
$env:OPENMIMIC_PLATFORM_RESOLVED = $Platform
Log "3-1. torch 확인 (win-cuda 는 CUDA 인식 필수)"
if ((Invoke-Py $CheckTorch) -ne 0) { Die "torch 확인 실패 (위 !! 줄 참고). 여기서 멈춥니다" }

# ------------------------------------------------------------------ 4. LeRobot
Log "4. LeRobot 소스 (commit $($LerobotCommit.Substring(0, 8)))"
if (-not (Test-Path (Join-Path $LerobotSrc '.git'))) {
    if ((Invoke-Logged 'git' @('clone', 'https://github.com/huggingface/lerobot.git', $LerobotSrc)) -ne 0) { Die "lerobot clone 실패" }
}
if ((Invoke-Logged 'git' @('-C', $LerobotSrc, 'fetch', '--all', '--tags')) -ne 0) { Warn "fetch 실패, 로컬 트리 사용" }
if ((Invoke-Logged 'git' @('-C', $LerobotSrc, 'checkout', '-q', $LerobotCommit)) -ne 0) { Die "lerobot commit $LerobotCommit 체크아웃 실패" }

Log "4-1. lerobot[feetech,dynamixel,training,diffusion,smolvla] 설치 (torch 는 위 휠 유지)"
# torch 를 pip 기본 인덱스 것으로 덮어쓰지 않도록 현재 버전으로 핀 (로컬 꼬리표 +cpu/+cu130 은 뗌)
$Constraint = Join-Path $env:TEMP 'openmimic-torch-constraint.txt'
$pins = Get-PyOut "import torch, torchvision; print('torch==' + torch.__version__.split('+')[0]); print('torchvision==' + torchvision.__version__.split('+')[0])"
if (-not $pins) { Die "torch 버전을 읽지 못했습니다" }
Set-Content -LiteralPath $Constraint -Value $pins -Encoding ASCII
Get-Content -LiteralPath $Constraint | ForEach-Object { Write-Log $_ }
$env:PIP_CONSTRAINT = $Constraint
Push-Location $LerobotSrc
try {
    if ((Pip @('install', '-e', '.[feetech,dynamixel,training,diffusion,smolvla]')) -ne 0) { Die "lerobot 설치 실패 (로그 확인)" }
} finally { Pop-Location }

# 영상은 PyAV 로 통일합니다 (Linux 판과 같음 — torchcodec 은 플랫폼별 FFmpeg DLL 의존이 달라 지웁니다)
Pip @('uninstall', '-y', 'torchcodec') | Out-Null
# Windows PyAV 휠의 FFmpeg 에 lerobot 기본 코덱(libsvtav1)이 들어 있음을 실기 수집 로그로 확인했습니다 ('Using video codec: libsvtav1')
if ((Pip @('install', 'av>=15.0.0,<16.0.0')) -ne 0) { Die "av 설치 실패" }

Log "4-2. OpenMimic 의존성"
# websockets: 제어 탭(/ws/control). psutil: 작업 프로세스 확인. pygrabber: 카메라의 안정 식별자(DirectShow 장치 경로)
if ((Pip @('install', 'fastapi<1.0', 'uvicorn', 'websockets>=12', 'psutil')) -ne 0) { Die "OpenMimic 의존성 설치 실패" }
if ((Pip @('install', 'pygrabber')) -ne 0) {
    Warn "pygrabber 설치 실패 — 카메라를 번호로만 지정합니다 (재부팅·재연결 때 순서가 바뀔 수 있음)"
}

Log "4-3. 플러그인 — 양팔 OMX (bi_omx_follower / bi_omx_leader) · ACT2 정책 (act2)"
$plugins = @('lerobot_robot_bi_omx', 'lerobot_teleoperator_bi_omx', 'lerobot_policy_act2') |
    ForEach-Object { @('-e', (Join-Path $WorkDir "plugins\$_")) }
if ((Pip (@('install', '--no-deps') + $plugins)) -ne 0) { Die "플러그인 설치 실패" }

Log "4-4. torch 가 덮어써지지 않았는지 재확인"
if ((Invoke-Py $CheckTorch) -ne 0) { Die "lerobot 설치 과정에서 torch 휠이 바뀌었습니다. pip uninstall -y torch torchvision 후 3단계 인덱스로 재설치" }

if ($Platform -eq 'win-intel') {
    Log "4-5. OpenVINO / NNCF (Intel NPU·GPU·CPU 추론)"
    if ((Pip @('install', 'openvino>=2025.4', 'nncf>=2.19')) -ne 0) { Die "openvino 설치 실패" }

    # Core Ultra Windows 에서 OpenVINO 가 NPU 를 잡고 openmimic_ov 변환이 되는 것은 실기로 확인 — TODO(win-verify): NPU 실행(롤아웃)
    Log "4-6. Intel NPU / GPU 점검 (드라이버는 설치하지 않습니다 — 안내만)"
    $cpu = (Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1).Name
    Write-Log "CPU : $cpu"
    if ($cpu -notmatch 'Core\(TM\) Ultra|Core Ultra') { Warn "Core Ultra 가 아닙니다 — NPU 가 없을 수 있습니다. OpenVINO GPU/CPU 로만 추론합니다" }
    $ovDevs = Get-PyOut "import openvino as ov; print(' '.join(ov.Core().available_devices))"
    Write-Log "OpenVINO 장치: $(if ($ovDevs) { $ovDevs } else { '없음' })"
    if ($ovDevs -match '\bNPU\b') { Write-Log "NPU : OpenVINO 에서 사용 가능" }
    else { Warn "NPU 를 OpenVINO 가 못 잡습니다 — 장치 관리자의 '신경 프로세서'(Intel AI Boost) 드라이버를 확인하세요 (Intel 드라이버 지원 도우미 또는 PC 제조사 드라이버)" }
    if ($ovDevs -match '\bGPU\b') { Write-Log "GPU : OpenVINO 에서 사용 가능" }
    else { Warn "GPU 를 OpenVINO 가 못 잡습니다 — Intel 그래픽 드라이버를 최신으로 설치하세요" }
}
Remove-Item Env:\PIP_CONSTRAINT -ErrorAction SilentlyContinue

# ----------------------------------------------------------------- 5. 임포트 검증
Log "5. 최종 검증"
$Verify = @'
import torch
print("torch   :", torch.__version__, "| cuda:", torch.cuda.is_available(),
      "| xpu:", hasattr(torch, "xpu") and torch.xpu.is_available())
try:
    import openvino as ov
    print("openvino:", ov.__version__, "| devices:", ov.Core().available_devices)
except ImportError:
    print("openvino: (미설치 — win-intel 플랫폼에서만 설치)")
import lerobot
from lerobot.robots.so_follower import SOFollower
from lerobot.robots.bi_so_follower import BiSOFollower
from lerobot.motors.feetech import FeetechMotorsBus
from lerobot.robots.omx_follower import OmxFollower
from lerobot.motors.dynamixel import DynamixelMotorsBus
import dynamixel_sdk  # noqa: F401
import lerobot_robot_bi_omx, lerobot_teleoperator_bi_omx  # noqa: F401
from lerobot.scripts.lerobot_record import record_loop
import fastapi, uvicorn, cv2, av, websockets, psutil  # noqa: F401
print("lerobot :", "OK (so_follower / bi_so_follower / feetech / omx / bi_omx / dynamixel / record_loop)")
print("fastapi :", fastapi.__version__, "| cv2:", cv2.__version__, "| av:", av.__version__)
import serial.tools.list_ports as lp
print("COM 포트:", [p.device for p in lp.comports()] or "없음")
'@
if ((Invoke-Py $Verify) -ne 0) { Die "최종 검증 실패 (위 오류 확인)" }

# ------------------------------------------------------------------- 완료
Log "완료"
$trainNote = if ($env:TORCH_LOCAL -eq 'xpu') { '학습은 이 기기 Intel GPU(XPU)로 됩니다 (실험적) — 느리면 학습 탭 실행 위치에서 HF Jobs 를 고르세요.' } else { '학습은 학습 탭 실행 위치에서 HF Jobs 를 고르세요 (Intel GPU 학습: $env:OPENMIMIC_TORCH=''xpu'' 로 다시 설치 — 실험적).' }
$torchNote = if ($Platform -eq 'win-intel') { " (torch 휠: $(if ($env:TORCH_LOCAL) { $env:TORCH_LOCAL } else { '기본' }))" } else { '' }
@"

  플랫폼    : $Platform$torchNote
  conda env : $EnvName  (python $PyVer, $EnvDir)
  lerobot   : $LerobotSrc @ $($LerobotCommit.Substring(0, 8))
  데이터    : $DataDir   (HF_HOME=$DataDir\hf → 캘리브레이션은 HF_HOME\lerobot\calibration)
  활성화    : powershell -ExecutionPolicy Bypass -NoExit -Command ". '$WorkDir\activate.ps1'"

  --- OpenMimic 켜기 ---
  powershell -ExecutionPolicy Bypass -File "$WorkDir\openmimic.ps1" start     # 접속 주소를 알려 줍니다 (stop · status · log · update · autostart on)

  --- 웹에서 ---
  처음 열면 셋업 마법사가 뜹니다 → 데이터셋 화면의 '시작하기' 순서(셋업 → 캘리브레이션 → 수집 → 학습 → 실행)대로

  * 다른 PC 에서 접속하려면 Windows 방화벽이 8080 포트(python.exe)를 허용해야 합니다 — 처음 켤 때 뜨는 창에서 '허용'.
  * win-intel: 모델 탭 → 체크포인트 "OpenVINO 변환" → 실행 탭에서 추론 엔진 NPU/GPU/CPU 선택.
               $trainNote
  * 시리얼 보드가 COM 포트로 안 보이면 장치 관리자에서 드라이버(CH340/CH343 · CP210x)를 확인하세요.

"@ -split "`n" | ForEach-Object { Write-Log $_.TrimEnd("`r") }
