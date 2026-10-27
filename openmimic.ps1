# ============================================================================
#  OpenMimic 실행 도구 (Windows) — 켜기 · 끄기 · 상태 · 업데이트 · 로그 · 로그인 시 자동 시작 (openmimic.sh 의 Windows 판)
#
#    .\openmimic.ps1 start          켜기 (창 없이 백그라운드, 로그는 openmimic.log)
#    .\openmimic.ps1 stop           끄기
#    .\openmimic.ps1 restart        다시 켜기
#    .\openmimic.ps1 status         켜져 있는지 · 접속 주소
#    .\openmimic.ps1 log            로그 보기 (Ctrl+C 로 나감 — OpenMimic 은 계속 돎)
#    .\openmimic.ps1 update         git pull 후 다시 켜기
#    .\openmimic.ps1 autostart on   로그인 때 자동으로 켜기 — 이 계정만 (작업 스케줄러)
#    .\openmimic.ps1 autostart off  자동 시작 끄기
#
#  실행 정책 때문에 막히면: powershell -ExecutionPolicy Bypass -File .\openmimic.ps1 start
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
$Task = 'OpenMimic'
$StartupCmd = Join-Path ([Environment]::GetFolderPath('Startup')) 'OpenMimic.cmd'

function Use-Env {
    if (-not (Test-Path (Join-Path $Dir 'activate.ps1'))) {
        Write-Host "activate.ps1 이 없습니다 — 레포 폴더를 확인하세요: $Dir"; exit 1
    }
    . (Join-Path $Dir 'activate.ps1')
    if ($env:CONDA_DEFAULT_ENV -ne 'openmimic') {
        Write-Host "환경을 켜지 못했습니다 — 먼저 설치하세요: powershell -ExecutionPolicy Bypass -File `"$Dir\install.ps1`""; exit 1
    }
}

# pid 파일의 프로세스가 살아 있고 python main.py 이면 그 pid
function Get-RunningPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $p = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $p) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$p)" -ErrorAction SilentlyContinue
    if ($proc -and $proc.CommandLine -match 'main\.py') { return [int]$p }
    return $null
}

function Show-Urls {
    $ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notmatch '^(127\.|169\.254\.)' } | Select-Object -ExpandProperty IPAddress
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

function Start-OpenMimic {
    $p = Get-RunningPid
    if ($p) { Write-Host "이미 켜져 있습니다 (pid $p)"; Show-Urls; return }
    Use-Env
    $py = (Get-Command python -ErrorAction Stop).Source
    # cmd 가 로그 파일에 이어 쓰고(>>), 파이썬은 UTF-8 모드(-X utf8). 창은 숨깁니다.
    # 작업(수집·학습·실행)은 python 의 자식으로 같은 숨은 콘솔에 붙어, 작업 탭 '중지'(CTRL_BREAK)가 닿습니다.
    $cmdLine = "`"`"$py`" -X utf8 main.py >> `"$LogFile`" 2>&1`""
    $sh = Start-Process -FilePath $env:ComSpec -ArgumentList @('/c', $cmdLine) -WorkingDirectory $Dir -WindowStyle Hidden -PassThru
    $serverPid = $null
    for ($i = 0; $i -lt 40; $i++) {
        if (-not $serverPid) {
            $child = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($sh.Id)" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -match 'main\.py' } | Select-Object -First 1
            if ($child) {
                $serverPid = [int]$child.ProcessId
                Set-Content -LiteralPath $PidFile -Value $serverPid -Encoding ASCII
            }
        }
        if ($serverPid -and (Test-Web)) { break }
        if ($sh.HasExited -and -not (Get-RunningPid)) {
            Write-Host "켜다가 멈췄습니다 — 로그 끝부분:"
            Get-Content -LiteralPath $LogFile -Tail 20 -Encoding UTF8 -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    Write-Host "켰습니다 (pid $serverPid, 로그 $LogFile). 브라우저에서:"
    Show-Urls
    Get-Content -LiteralPath $LogFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -like '!! *' } | Select-Object -Last 3
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
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-Host "껐습니다 (수집·학습 같은 작업은 각자 계속 돌고, 다시 켜면 작업 탭에 그대로 보입니다)"
}

function Show-Status {
    $p = Get-RunningPid
    if ($p) { Write-Host "켜져 있음 (pid $p)"; Show-Urls }
    else { Write-Host "꺼져 있음 — .\openmimic.ps1 start" }
    $onTask = $false
    try { schtasks /Query /TN $Task 2>$null | Out-Null; $onTask = ($LASTEXITCODE -eq 0) } catch { $onTask = $false }
    if ($onTask -or (Test-Path $StartupCmd)) { Write-Host "자동 시작: 켜짐" } else { Write-Host "자동 시작: 꺼짐" }
}

function Update-OpenMimic {
    Set-Location $Dir
    git diff --quiet; $a = $LASTEXITCODE
    git diff --cached --quiet; $b = $LASTEXITCODE
    if ($a -ne 0 -or $b -ne 0) { Write-Host "고친 파일이 있어 업데이트를 멈춥니다 — git status 로 확인하세요"; exit 1 }
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "git pull 실패 — 위 메시지를 확인하세요"; exit 1 }
    Stop-OpenMimic
    Start-OpenMimic
    Write-Host "브라우저는 강력 새로고침(Ctrl+Shift+R) 하세요"
}

function Set-Autostart([string]$Mode) {
    $runLine = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\openmimic.ps1`" start"
    switch ($Mode) {
        'on' {
            # TODO(win-verify): 관리자 권한 없는 계정에서 ONLOGON 작업 만들기가 되는지 — 안 되면 시작프로그램 폴더로 대신합니다
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            schtasks /Create /SC ONLOGON /TN $Task /TR $runLine /F 2>&1 | Out-Null
            $code = $LASTEXITCODE
            $ErrorActionPreference = $old
            if ($code -eq 0) {
                Write-Host "자동 시작을 켰습니다 — 이 계정으로 로그인하면 OpenMimic 이 켜집니다 (작업 스케줄러: $Task)"
            } else {
                Set-Content -LiteralPath $StartupCmd -Encoding ASCII -Value "@echo off`r`nstart `"`" /min $runLine"
                Write-Host "작업 스케줄러에 등록하지 못해(권한) 시작프로그램 폴더에 넣었습니다: $StartupCmd"
            }
        }
        'off' {
            $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
            schtasks /Delete /TN $Task /F 2>&1 | Out-Null
            $ErrorActionPreference = $old
            Remove-Item -LiteralPath $StartupCmd -ErrorAction SilentlyContinue
            Write-Host "자동 시작을 껐습니다"
        }
        default { Write-Host "사용법: .\openmimic.ps1 autostart on | off"; exit 1 }
    }
}

switch ($Command) {
    'start' { Start-OpenMimic }
    'stop' { Stop-OpenMimic }
    'restart' { Stop-OpenMimic; Start-OpenMimic }
    'status' { Show-Status }
    'log' { Get-Content -LiteralPath $LogFile -Tail 50 -Wait -Encoding UTF8 }
    'update' { Update-OpenMimic }
    'autostart' { Set-Autostart $Arg }
    default {
        Get-Content -LiteralPath $PSCommandPath -Encoding UTF8 | Select-Object -Skip 2 -First 9 | ForEach-Object { $_ -replace '^#', '' }
        exit 1
    }
}
