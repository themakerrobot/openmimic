# ============================================================================
#  OpenMimic 실행 도구 (Windows) — 켜기 · 끄기 · 상태 · 업데이트 · 로그 · 로그인 시 자동 시작 (openmimic.sh 의 Windows 판)
#
#    .\openmimic.ps1 start          켜기 (창 없이 백그라운드, 로그는 openmimic.log)
#    .\openmimic.ps1 stop           끄기
#    .\openmimic.ps1 restart        다시 켜기
#    .\openmimic.ps1 status         켜져 있는지 · 접속 주소
#    .\openmimic.ps1 log            로그 보기 (Ctrl+C 로 나감 — OpenMimic 은 계속 돎)
#    .\openmimic.ps1 update         git pull 후 다시 켜기
#    .\openmimic.ps1 autostart on   로그인 때 자동으로 켜기 — 이 계정만 (시작프로그램 폴더 바로 가기)
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

# 네이티브 명령(git·schtasks)을 'Stop' 상태에서 돌리면 stderr 출력이 종료 오류가 되므로 그동안만 풉니다
function Invoke-Native([scriptblock]$Block) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Block } finally { $ErrorActionPreference = $old }
}

# pid 파일의 프로세스가 살아 있고 python main.py 이면 그 pid
function Get-RunningPid {
    if (-not (Test-Path $PidFile)) { return $null }
    $p = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $p) { return $null }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$([int]$p)" -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    if ($proc.CommandLine -match 'main\.py') { return [int]$p }
    if (-not $proc.CommandLine -and $proc.Name -eq 'python.exe') { return [int]$p }   # 관리자 창에서 켠 경우 명령줄을 못 읽음
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

function Start-OpenMimic {
    $p = Get-RunningPid
    if ($p) { Write-Host "이미 켜져 있습니다 (pid $p)"; Show-Urls; return }
    Use-Env
    $py = Join-Path $env:CONDA_PREFIX 'python.exe'
    # cmd 가 로그 파일에 이어 쓰고(>>), 파이썬은 UTF-8 모드(-X utf8)·버퍼 없이(-u — 시작 안내가 바로 로그에 남게). 창은 숨깁니다.
    # 작업(수집·학습·실행)은 python 의 자식으로 같은 숨은 콘솔에 붙습니다.
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
                # 작업 스케줄러 등 낮은 우선순위로 불린 경우에도 서버·작업이 보통 우선순위로 돌게
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
    Write-Host "켰습니다 (pid $serverPid, 로그 $LogFile). 브라우저에서:"
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
    Start-OpenMimic
    Write-Host "브라우저는 강력 새로고침(Ctrl+Shift+R) 하세요"
}

function Remove-OldAutostart {
    Remove-Item -LiteralPath $StartupCmd -ErrorAction SilentlyContinue
    Invoke-Native { schtasks /Delete /TN $OldTask /F 2>&1 | Out-Null }
}

function Set-Autostart([string]$Mode) {
    switch ($Mode) {
        'on' {
            # 시작프로그램 폴더 바로 가기(.lnk) — 관리자 권한이 필요 없고, 배터리일 때도 켜지며, 보통 우선순위로 돕니다.
            # (작업 스케줄러의 로그온 트리거는 관리자 권한이 필요하고, 기본값이 'AC 전원일 때만'·낮은 우선순위)
            Remove-OldAutostart
            $ws = New-Object -ComObject WScript.Shell
            $l = $ws.CreateShortcut($StartupLnk)
            $l.TargetPath = (Get-Process -Id $PID).Path                   # 지금 실행 중인 powershell.exe 또는 pwsh.exe
            $l.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Dir\openmimic.ps1`" start"
            $l.WorkingDirectory = $Dir
            $l.WindowStyle = 7                                             # 최소화로 시작
            $l.Save()
            Write-Host "자동 시작을 켰습니다 — 이 계정으로 로그인하면 OpenMimic 이 켜집니다 ($StartupLnk)"
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
    'start' { Start-OpenMimic }
    'stop' { Stop-OpenMimic }
    'restart' { Stop-OpenMimic; Start-OpenMimic }
    'status' { Show-Status }
    'log' {
        if (-not (Test-Path $LogFile)) { Write-Host "아직 로그가 없습니다 — .\openmimic.ps1 start"; exit 1 }
        Get-Content -LiteralPath $LogFile -Tail 50 -Wait -Encoding UTF8
    }
    'update' { Update-OpenMimic }
    'autostart' { Set-Autostart $Arg }
    default {
        Get-Content -LiteralPath $PSCommandPath -Encoding UTF8 | Select-Object -Skip 1 -First 11 | ForEach-Object { $_ -replace '^#', '' }
        exit 1
    }
}
