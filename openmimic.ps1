# ============================================================================
#  OpenMimic 실행 도구 (Windows) — 켜기 · 끄기 · 상태 · 업데이트 · 로그 · 로그인 시 자동 시작 (openmimic.sh 의 Windows 판)
#
#    .\openmimic.ps1 start          이 창에서 켜기 (로그가 이 창에 보임, Ctrl+C 로 끔)
#    .\openmimic.ps1 stop           끄기 (다른 창에서 켠 것도)
#    .\openmimic.ps1 restart        끄고 이 창에서 다시 켜기
#    .\openmimic.ps1 status         켜져 있는지 · 접속 주소
#    .\openmimic.ps1 update         git pull 후 이 창에서 다시 켜기
#    .\openmimic.ps1 background     창 없이 백그라운드로 켜기 (로그는 openmimic.log)
#    .\openmimic.ps1 log            백그라운드 로그 보기 (Ctrl+C 로 나감 — OpenMimic 은 계속 돎)
#    .\openmimic.ps1 autostart on   로그인 때 최소화된 창으로 켜기 — 이 계정만 (시작프로그램 폴더 바로 가기)
#    .\openmimic.ps1 autostart off  자동 시작 끄기
#
#  실행 정책 때문에 막히면: powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start  (pwsh 도 같음)
#  activate.ps1 로 환경을 켜므로 캘리브레이션·데이터 폴더가 항상 설치 폴더(data\hf)로 맞춰집니다.
# ============================================================================
#Requires -Version 5.1
param([string]$Command = '', [string]$Arg = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Dir = $PSScriptRoot
$PidFile = Join-Path $Dir 'openmimic_server.pid'
$LogFile = Join-Path $Dir 'openmimic.log'
$Port = 8080
$EnvDir = Join-Path $env:USERPROFILE 'miniforge3\envs\openmimic'
$OldTask = 'OpenMimic'                                                     # 예전 판이 만든 작업 스케줄러 항목 (있으면 지움)
$StartupDir = [Environment]::GetFolderPath('Startup')
$StartupLnk = Join-Path $StartupDir 'OpenMimic.lnk'
$StartupCmd = Join-Path $StartupDir 'OpenMimic.cmd'                        # 예전 판이 만든 것 (있으면 지움)

function Use-Env {
    if (-not (Test-Path (Join-Path $Dir 'activate.ps1'))) {
        Write-Host "activate.ps1 이 없습니다 — 레포 폴더를 확인하세요: $Dir"; exit 1
    }
    . (Join-Path $Dir 'activate.ps1')
    if ($env:CONDA_DEFAULT_ENV -ne 'openmimic') {
        Write-Host "환경을 켜지 못했습니다 — 먼저 설치하세요: powershell -ExecutionPolicy Bypass -File `"$Dir\install.ps1`""; exit 1
    }
}

# 네이티브 명령(git·schtasks·python)을 'Stop' 상태에서 돌리면 stderr 출력이 종료 오류가 되므로 그동안만 풉니다
function Invoke-Native([scriptblock]$Block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block } finally { $ErrorActionPreference = $old }
}

# 켜져 있는 서버(python main.py)의 pid. 백그라운드로 켰으면 pid 파일, 창에서 켰으면 프로세스 목록에서 찾습니다
# (작업 worker 인 'main.py --worker …' 는 뺍니다)
function Get-RunningPid {
    if (Test-Path $PidFile) {
        $p = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($p) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$p)" -ErrorAction SilentlyContinue
            if ($proc -and (($proc.CommandLine -match 'main\.py') -or (-not $proc.CommandLine -and $proc.Name -eq 'python.exe'))) {
                return [int]$p
            }
        }
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    }
    $found = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'main\.py' -and $_.CommandLine -notmatch '--worker' -and
                       $_.ExecutablePath -and $_.ExecutablePath.StartsWith($EnvDir, [System.StringComparison]::OrdinalIgnoreCase) } |
        Select-Object -First 1
    if ($found) { return [int]$found.ProcessId }
    return $null
}

function Show-Urls {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -AddressState Preferred -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' -and $_.InterfaceAlias -notmatch '^vEthernet' } |
        Select-Object -ExpandProperty IPAddress
    if (-not $ips) { Write-Host "  http://<이 PC 의 IP>:$Port/"; return }
    foreach ($ip in $ips) { Write-Host "  http://${ip}:$Port/" }
}

function Test-Web {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$Port/jobs" -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

# 이 창에서 켭니다 — 로그가 그대로 보이고 Ctrl+C 로 끕니다.
# 수집·학습 같은 작업은 서버와 다른 프로세스 그룹이라 Ctrl+C 를 받지 않고 계속 돕니다 (다시 켜면 작업 탭에 그대로).
# 창을 닫으면 Windows 가 이 창에 붙은 프로세스를 모두 끄므로 진행 중인 작업도 같이 꺼집니다.
function Start-Foreground {
    $p = Get-RunningPid
    if ($p) { Write-Host "이미 켜져 있습니다 (pid $p) — 이 창에서 켜려면 먼저 .\openmimic.ps1 stop"; Show-Urls; return }
    Use-Env
    $py = Join-Path $env:CONDA_PREFIX 'python.exe'
    try { $Host.UI.RawUI.WindowTitle = 'OpenMimic — Ctrl+C 로 끄기' } catch { }
    Write-Host "OpenMimic 을 이 창에서 켭니다. 브라우저에서:"
    Show-Urls
    Write-Host "끄려면 이 창에서 Ctrl+C — 수집·학습 같은 작업은 계속 돕니다. 창을 닫으면 진행 중인 작업도 같이 꺼집니다."
    Write-Host ""
    Set-Location $Dir
    Invoke-Native { & $py -X utf8 -u main.py }
    Write-Host ""
    Write-Host "OpenMimic 이 꺼졌습니다 (다시 켜기: .\openmimic.ps1 start)"
}

# 창 없이 켭니다 (예전 start). cmd 가 로그 파일에 이어 쓰고(>>), 파이썬은 UTF-8 모드(-X utf8)·버퍼 없이(-u).
function Start-Background {
    $p = Get-RunningPid
    if ($p) { Write-Host "이미 켜져 있습니다 (pid $p)"; Show-Urls; return }
    Use-Env
    $py = Join-Path $env:CONDA_PREFIX 'python.exe'
    $cmdLine = "`"`"$py`" -X utf8 -u main.py >> `"$LogFile`" 2>&1`""
    $sh = Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', $cmdLine) -WorkingDirectory $Dir -WindowStyle Hidden -PassThru
    $serverPid = $null
    for ($i = 0; $i -lt 40; $i++) {
        if (-not $serverPid) {
            $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($sh.Id)" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match 'main\.py' } | Select-Object -First 1
            if ($child) {
                $serverPid = [int]$child.ProcessId
                Set-Content -LiteralPath $PidFile -Value $serverPid -Encoding ASCII
                try { (Get-Process -Id $serverPid).PriorityClass = 'Normal' } catch { }
            }
        }
        if ($serverPid -and (Test-Web)) { break }
        if ($sh.HasExited -and -not (Get-RunningPid)) {
            Write-Host "켜다가 멈췄습니다 — 로그 끝부분:"
            if (Test-Path $LogFile) { Get-Content -LiteralPath $LogFile -Tail 20 -Encoding UTF8 }
            Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "백그라운드로 켰습니다 (pid $serverPid, 로그 $LogFile). 브라우저에서:"
    Show-Urls
    if (Test-Path $LogFile) {
        Get-Content -LiteralPath $LogFile -Tail 200 -Encoding UTF8 | Where-Object { $_ -like '!! *' } | Select-Object -Last 3
    }
}

function Stop-OpenMimic {
    $p = Get-RunningPid
    if (-not $p) { Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue; Write-Host "꺼져 있습니다"; return }
    # 서버 프로세스만 끕니다 (/T 없음) — 수집·학습 같은 작업은 각자 계속 돌고, 다시 켜면 작업 탭에 그대로 보입니다
    Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 20; $i++) {
        if (-not (Get-Process -Id $p -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $p -ErrorAction SilentlyContinue) {
        Write-Host "끄지 못했습니다 (pid $p) — 관리자 창에서 켰다면 관리자 창에서 stop 하세요"; exit 1
    }
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-Host "껐습니다 (수집·학습 같은 작업은 각자 계속 돌고, 다시 켜면 작업 탭에 그대로 보입니다)"
}

function Show-Status {
    $p = Get-RunningPid
    if ($p) { Write-Host "켜져 있음 (pid $p)"; Show-Urls }
    else { Write-Host "꺼져 있음 — .\openmimic.ps1 start" }
    if (Test-Path $StartupLnk) { Write-Host "자동 시작: 켜짐" } else { Write-Host "자동 시작: 꺼짐" }
}

function Update-OpenMimic {
    Set-Location $Dir
    $a = Invoke-Native { git diff --quiet 2>$null; $LASTEXITCODE }
    $b = Invoke-Native { git diff --cached --quiet 2>$null; $LASTEXITCODE }
    if ($a -gt 1 -or $b -gt 1) { Write-Host "git 을 실행하지 못했습니다 — 이 폴더에서 git status 로 확인하세요"; exit 1 }
    if ($a -ne 0 -or $b -ne 0) { Write-Host "고친 파일이 있어 업데이트를 멈춥니다 — git status 로 확인하세요"; exit 1 }
    $code = Invoke-Native { git pull --ff-only 2>&1 | ForEach-Object { Write-Host "$_" }; $LASTEXITCODE }
    if ($code -ne 0) { Write-Host "git pull 실패 — 위 메시지를 확인하세요"; exit 1 }
    Stop-OpenMimic
    Write-Host "브라우저는 강력 새로고침(Ctrl+Shift+R) 하세요"
    Start-Foreground
}

function Remove-OldAutostart {
    Remove-Item -LiteralPath $StartupCmd -ErrorAction SilentlyContinue
    Invoke-Native { schtasks /Delete /TN $OldTask /F 2>&1 | Out-Null }
}

function Set-Autostart([string]$Mode) {
    switch ($Mode) {
        'on' {
            # 시작프로그램 폴더 바로 가기(.lnk) — 관리자 권한이 필요 없고, 배터리일 때도 켜지며, 보통 우선순위로 돕니다.
            # 최소화된 창에서 start 로 켜므로 로그를 창에서 볼 수 있고, 꺼져도(-NoExit) 창이 남아 오류를 볼 수 있습니다.
            Remove-OldAutostart
            $ws = New-Object -ComObject WScript.Shell
            $l = $ws.CreateShortcut($StartupLnk)
            $l.TargetPath = (Get-Process -Id $PID).Path                   # 지금 실행 중인 powershell.exe 또는 pwsh.exe
            $l.Arguments = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$Dir\openmimic.ps1`" start"
            $l.WorkingDirectory = $Dir
            $l.WindowStyle = 7                                             # 최소화로 시작
            $l.Save()
            Write-Host "자동 시작을 켰습니다 — 이 계정으로 로그인하면 최소화된 창에서 OpenMimic 이 켜집니다 ($StartupLnk)"
        }
        'off' {
            Remove-OldAutostart
            Remove-Item -LiteralPath $StartupLnk -ErrorAction SilentlyContinue
            Write-Host "자동 시작을 껐습니다"
        }
        default { Write-Host "사용법: .\openmimic.ps1 autostart on | off"; exit 1 }
    }
}

switch ($Command) {
    'start' { Start-Foreground }
    'background' { Start-Background }
    'stop' { Stop-OpenMimic }
    'restart' { Stop-OpenMimic; Start-Foreground }
    'status' { Show-Status }
    'log' {
        if (-not (Test-Path $LogFile)) { Write-Host "로그 파일이 없습니다 — 창에서 켰다면(start) 그 창에 로그가 보입니다"; exit 1 }
        Get-Content -LiteralPath $LogFile -Tail 50 -Wait -Encoding UTF8
    }
    'update' { Update-OpenMimic }
    'autostart' { Set-Autostart $Arg }
    default {
        Get-Content -LiteralPath $PSCommandPath -Encoding UTF8 | Select-Object -Skip 1 -First 13 | ForEach-Object { $_ -replace '^#', '' }
        exit 1
    }
}
