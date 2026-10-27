# ============================================================================
#  OpenMimic 환경 켜기 (Windows) — activate.sh 와 같은 일
#     . .\activate.ps1          # 점(.) 을 붙여 실행해야 지금 PowerShell 창에 적용됩니다
#
#  conda init 없이 conda 환경 'openmimic' 을 이 창에서만 켭니다 (PATH 맨 앞에 환경 폴더를 붙임 — conda activate 와 같은 순서).
#  데이터·HF 캐시·torch 캐시를 레포 data\ 아래로 두고, 파이썬을 UTF-8 모드로 돌립니다
#  (Windows 기본 인코딩으로는 한국어가 든 설정·로그를 읽고 쓰다 깨집니다).
# ============================================================================
$OpenMimicDir = $PSScriptRoot
$CondaDir = Join-Path $env:USERPROFILE 'miniforge3'
$EnvDir = Join-Path $CondaDir 'envs\openmimic'

if (-not (Test-Path (Join-Path $EnvDir 'python.exe'))) {
    Write-Host "conda 환경이 없습니다: $EnvDir — 먼저 설치하세요: powershell -ExecutionPolicy Bypass -File `"$OpenMimicDir\install.ps1`""
    return
}
if ($env:CONDA_PREFIX -ne $EnvDir) {
    $add = @($EnvDir,
             (Join-Path $EnvDir 'Library\mingw-w64\bin'),
             (Join-Path $EnvDir 'Library\usr\bin'),
             (Join-Path $EnvDir 'Library\bin'),
             (Join-Path $EnvDir 'Scripts'),
             (Join-Path $EnvDir 'bin'),
             (Join-Path $CondaDir 'condabin'))
    $env:PATH = (($add -join ';') + ';' + $env:PATH)
}
$env:CONDA_PREFIX = $EnvDir
$env:CONDA_DEFAULT_ENV = 'openmimic'
$env:HF_HOME = Join-Path $OpenMimicDir 'data\hf'
$env:TORCH_HOME = Join-Path $OpenMimicDir 'data\torch'
$env:PYTHONUTF8 = '1'
Set-Location $OpenMimicDir
