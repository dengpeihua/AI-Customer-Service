$ErrorActionPreference = "Stop"

$LaunchStopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
$Python = Join-Path $Root "runtime\python\python.exe"
$Pythonw = Join-Path $Root "runtime\python\pythonw.exe"
$Mem0Python = Join-Path $Root "mem\.venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$ExpectedWeChatVersion = "4.1.10.27"

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Is-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Relaunch-AsAdmin {
    $args = "-NoProfile -ExecutionPolicy Bypass -NoExit -File `"$PSCommandPath`""
    Start-Process -FilePath "powershell.exe" -ArgumentList $args -Verb RunAs | Out-Null
}

function Ensure-File([string]$Path, [string]$Template) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Copy-Item -LiteralPath $Template -Destination $Path -Force
        Write-Host "created: $Path"
    }
}

function Get-WeChatInstallDir {
    foreach ($key in @(
        "HKCU:\Software\Tencent\Weixin",
        "HKLM:\SOFTWARE\WOW6432Node\Tencent\Weixin",
        "HKLM:\SOFTWARE\Tencent\Weixin"
    )) {
        $installPath = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath
        if ($installPath -and (Test-Path -LiteralPath (Join-Path $installPath "Weixin.exe"))) {
            return $installPath
        }
    }
    foreach ($candidate in @(
        "C:\Program Files\Tencent\Weixin",
        "C:\Program Files (x86)\Tencent\Weixin",
        "D:\WX41027"
    )) {
        if (Test-Path -LiteralPath (Join-Path $candidate "Weixin.exe")) {
            return $candidate
        }
    }
    return $null
}

function Install-WeChatIfNeeded {
    $installer = Join-Path $Root "assets\wechat\WeChatSetup_4.1.10.27.exe"
    $dir = Get-WeChatInstallDir
    $needsInstall = $true
    if ($dir) {
        $exe = Join-Path $dir "Weixin.exe"
        $version = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
        if ($version -like "$ExpectedWeChatVersion*") {
            Write-Host "WeChat found: $dir ($version)"
            $needsInstall = $false
        } else {
            Write-Host "WeChat version is $version, reinstalling $ExpectedWeChatVersion..."
        }
    }

    if ($needsInstall) {
        if (-not (Test-Path -LiteralPath $installer)) {
            throw "Missing WeChat installer: $installer"
        }
        Get-Process Weixin -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Process -FilePath $installer -ArgumentList "/S" -Wait
        Start-Sleep -Seconds 6
        $dir = Get-WeChatInstallDir
        if (-not $dir) {
            throw "WeChat installation finished, but Weixin.exe was not detected."
        }
        Write-Host "WeChat installed: $dir"
    }
    return $dir
}

function Assert-WeChatVersion([string]$InstallDir) {
    $exe = Join-Path $InstallDir "Weixin.exe"
    if (-not (Test-Path -LiteralPath $exe)) {
        throw "Weixin.exe was not found: $exe"
    }
    $version = (Get-Item -LiteralPath $exe).VersionInfo.FileVersion
    if ($version -notlike "$ExpectedWeChatVersion*") {
        throw "Unsupported WeChat version: $version. Expected $ExpectedWeChatVersion. Uninstall the newer WeChat, then rerun this launcher."
    }
}

function Stop-WeChatUpdaterProcesses {
    Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $_.ProcessName -like "*WeixinUpdate*" -or $_.ProcessName -like "*WeChatUpdate*" } |
        Stop-Process -Force -ErrorAction SilentlyContinue
}

function Disable-WeChatUpdaterPath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Length -eq 0 -and $item.IsReadOnly) {
        return
    }
    $disabled = "$Path.disabled"
    Set-ItemProperty -LiteralPath $Path -Name IsReadOnly -Value $false -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $disabled) {
        Set-ItemProperty -LiteralPath $disabled -Name IsReadOnly -Value $false -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $disabled -Force -ErrorAction SilentlyContinue
    }
    Move-Item -LiteralPath $Path -Destination $disabled -Force
    New-Item -ItemType File -Path $Path -Force | Out-Null
    Set-ItemProperty -LiteralPath $Path -Name IsReadOnly -Value $true
    Write-Host "disabled: $Path"
}

function Get-WeChatUpdaterFiles([string]$InstallDir) {
    $roots = @($InstallDir)
    foreach ($root in @(
        (Join-Path $env:APPDATA "Tencent\xwechat"),
        (Join-Path $env:LOCALAPPDATA "Tencent\xwechat"),
        (Join-Path $env:PROGRAMDATA "Tencent\xwechat")
    )) {
        if ($root -and (Test-Path -LiteralPath $root)) {
            $roots += $root
        }
    }

    $files = @()
    foreach ($root in ($roots | Where-Object { $_ } | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $root)) {
            continue
        }
        $files += Get-ChildItem -LiteralPath $root -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object {
                $_.Name -notlike "*.disabled" -and (
                    $_.Name -in @("WeixinUpdate.exe", "WeChatUpdate.exe", "WeixinUpdate.exe.zip", "WeChatUpdate.exe.zip") -or
                    ($_.FullName -match "\\xplugin\\.*\\WeixinUpdate\\") -or
                    ($_.FullName -match "\\update\\download\\.*WeixinUpdate")
                )
            }
    }
    return $files
}

function Ensure-WeChatUpdateBlocked([string]$InstallDir) {
    Stop-WeChatUpdaterProcesses
    Get-WeChatUpdaterFiles $InstallDir | ForEach-Object {
        Disable-WeChatUpdaterPath $_.FullName
    }
    $remaining = Get-WeChatUpdaterFiles $InstallDir |
        Where-Object { $_.Name -match "\.exe(\.zip)?$" -and -not ($_.Length -eq 0 -and $_.IsReadOnly) }
    if ($remaining) {
        throw "WeChat updater is still executable: $($remaining.FullName -join ', ')"
    }
}

function Block-WeChatUpdateHosts {
    try {
        $hosts = Join-Path $env:windir "System32\drivers\etc\hosts"
        $current = [System.IO.File]::ReadAllText($hosts)
        $append = ""
        foreach ($domain in @("dldir1.qq.com", "dldir6.qq.com", "dldir1v6.qq.com", "update.wechat.com")) {
            if ($current -notmatch [regex]::Escape($domain)) {
                $append += "`r`n0.0.0.0 $domain  # ACS-block-wxupdate"
            }
        }
        if ($append) {
            [System.IO.File]::AppendAllText($hosts, $append)
            Write-Host "hosts updated"
        }
    } catch {
        Write-Host "hosts update skipped: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Install-Hook([string]$InstallDir) {
    $hookDll = Join-Path $Root "assets\hook\version_hook.dll"
    if (-not (Test-Path -LiteralPath $hookDll)) {
        throw "Missing hook DLL: $hookDll"
    }
    $target = Join-Path $InstallDir "version.dll"
    $backup = Join-Path $InstallDir "version.dll.acs_backup"
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $sourceHash = (Get-FileHash -LiteralPath $hookDll -Algorithm SHA256).Hash
        $targetHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        if ($sourceHash -eq $targetHash) {
            # Re-running the launcher must not force-kill a healthy WeChat just
            # to copy the exact same DLL. WeChat records that as an abnormal exit.
            Write-Host "hook already deployed: $target"
            return
        }
    }
    Get-Process Weixin -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    if ((Test-Path -LiteralPath $target) -and -not (Test-Path -LiteralPath $backup)) {
        Copy-Item -LiteralPath $target -Destination $backup -Force
    }
    Copy-Item -LiteralPath $hookDll -Destination $target -Force
    Write-Host "hook deployed: $target"
}

function Wait-Backend([int]$Attempts = 40) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 2
            if ($r.StatusCode -eq 200) {
                return $true
            }
        } catch {}
        if ($i -lt ($Attempts - 1)) {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Wait-Mem0([int]$Attempts = 60) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $r = Invoke-RestMethod -Uri "http://127.0.0.1:8888/health" -TimeoutSec 2
            if ($r.status -eq "ok" -and $r.mode -eq "local-oss") {
                return $true
            }
        } catch {}
        if ($i -lt ($Attempts - 1)) {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Stop-VerifiedMem0Listener {
    $listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8888 `
        -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $listener) {
        throw "The stale memory engine answered health checks but its listener PID could not be resolved."
    }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)" `
        -ErrorAction SilentlyContinue
    $commandLine = [string]$process.CommandLine
    $isProjectMem0 = $process -and
        $commandLine.IndexOf("run_mem0.py", [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $commandLine.IndexOf($Root, [StringComparison]::OrdinalIgnoreCase) -ge 0
    if (-not $isProjectMem0) {
        throw "Refusing to stop port 8888 because the listener is not the verified project memory engine process."
    }
    Stop-Process -Id $listener.OwningProcess -Force -ErrorAction Stop
    Write-Host "stopped outdated project memory engine process: $($listener.OwningProcess)"
}

function Start-Mem0 {
    $existing = $null
    try { $existing = Invoke-RestMethod -Uri "http://127.0.0.1:8888/health" -TimeoutSec 2 } catch {}
    if ($existing -and $existing.status -eq "ok") {
        # ``mem\data`` is ASCII-only, so Windows PowerShell 5.1 can now compare the
        # complete canonical path without any UTF-8 path-decoding workaround.
        $actualData = [IO.Path]::GetFullPath([string]$existing.data_dir).TrimEnd('\')
        $expectedData = [IO.Path]::GetFullPath((Join-Path $Root "mem\data")).TrimEnd('\')
        $isProjectData = $actualData.Equals(
            $expectedData, [StringComparison]::OrdinalIgnoreCase
        )
        $isExpectedRuntime = (
            $existing.mode -eq "local-oss" -and
            $existing.collection -eq "memories_dashscope_1024" -and
            $existing.telemetry -eq $false -and
            $existing.service_auth -eq "token" -and
            $existing.service_revision -eq "acs-mem0-companion-v4-structured-retry"
        )
        if ($isProjectData -and -not $isExpectedRuntime) {
            Stop-VerifiedMem0Listener
            $existing = $null
        } elseif (-not $isProjectData) {
            throw "Port 8888 is already used by another memory engine data directory: $actualData"
        } else {
            Write-Host "Mem engine already running: http://127.0.0.1:8888"
            return
        }
    }
    if (-not (Test-Path -LiteralPath $Mem0Python)) {
        throw "Bundled memory engine Python is missing: $Mem0Python"
    }
    $runner = Join-Path $Root "scripts\run_mem0.py"
    $stdout = Join-Path $LogDir "mem0.out.log"
    $stderr = Join-Path $LogDir "mem0.err.log"
    Start-Process -FilePath $Mem0Python -ArgumentList @($runner, "--host", "127.0.0.1", "--port", "8888") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
    if (-not (Wait-Mem0)) {
        throw "Memory engine did not become healthy. Check logs\mem0.err.log"
    }
    Write-Host "Mem engine started: http://127.0.0.1:8888"
}

function Wait-HookAfterLogin([int]$MaxWaitSeconds = 15) {
    Write-Host "Please scan QR code in WeChat. This window will continue automatically after login."
    Push-Location $Root
    try {
        $deadline = (Get-Date).AddSeconds($MaxWaitSeconds)
        $windowReadySince = $null
        while ((Get-Date) -lt $deadline) {
            $mainWindowReady = [bool](Get-Process Weixin -ErrorAction SilentlyContinue |
                Where-Object { $_.MainWindowHandle -ne 0 -and $_.Responding })
            if (-not $mainWindowReady) {
                $windowReadySince = $null
                Start-Sleep -Seconds 2
                continue
            }
            if ($null -eq $windowReadySince) {
                $windowReadySince = Get-Date
            }
            if (((Get-Date) - $windowReadySince).TotalSeconds -lt 5) {
                Start-Sleep -Seconds 1
                continue
            }
            $output = (& $Python -m widget.hook_patch 2>&1 | Out-String).Trim()
            if ($output) {
                Write-Host "[hook] $output"
            }
            if ($output -match "QueryDB=([1-9][0-9]*)") {
                return $true
            }
            Start-Sleep -Seconds 3
        }
        return $false
    } finally {
        Pop-Location
    }
}

function Start-Backend {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    $runner = Join-Path $Root "scripts\run_backend.py"
    # 冷启动只探一次；旧实现会先空等 40 秒，确认后端不存在后才启动。
    $health = Wait-Backend -Attempts 1
    if ($health) {
        # 健康摘要只能证明模型配置相同，不能证明运行中的 Python 已加载最新代码。
        # 完整启动器本来就拥有管理员权限，因此每次都做一次受控重启，避免旧代码长期驻留。
        Write-Host "backend already running; reloading current code/config..." -ForegroundColor Yellow
        & (Join-Path $ScriptDir "Restart-AIBackend.ps1") -ProjectRoot $Root
        return
    }
    $stdout = Join-Path $LogDir "backend.out.log"
    $stderr = Join-Path $LogDir "backend.err.log"
    $args = @($runner, "--host", "127.0.0.1", "--port", "8000")
    Start-Process -FilePath $Python -ArgumentList $args -WorkingDirectory $Root `
        -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr | Out-Null
    if (-not (Wait-Backend)) {
        throw "Backend did not become healthy. Check logs\backend.err.log"
    }
    Write-Host "backend started: http://127.0.0.1:8000"
}

function Stop-StaleWidgetProcesses {
    $runtimeRoot = Join-Path $Root "runtime\python"
    $processes = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $cmd = [string]$_.CommandLine
        $exe = [string]$_.ExecutablePath
        $isWidget = $cmd.IndexOf("run_widget.py", [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        $isThisPackage = ($exe -and $exe.StartsWith($runtimeRoot, [System.StringComparison]::OrdinalIgnoreCase)) -or
            ($cmd.IndexOf($Root, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
        ($_.Name -in @("python.exe", "pythonw.exe")) -and $isWidget -and $isThisPackage
    }
    foreach ($proc in $processes) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Host "stopped old widget process: $($proc.ProcessId)"
    }
}

function Start-Widget {
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    Stop-StaleWidgetProcesses
    $pythonGui = Join-Path $Root "runtime\python\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pythonGui)) {
        $pythonGui = $Python
    }
    $cmd = 'set "PYTHONUNBUFFERED=1"&& set "PYTHONUTF8=1"&& set "ACS_WIDGET_LOG_DIR=' +
        $LogDir + '"&& start "" /D "' + $Root + '" "' + $pythonGui + '" run_widget.py'
    & $env:ComSpec /d /c $cmd
    Write-Host "widget started"
}

# ================= 企业微信（WXWork）自动接入 =================
# 目标：和个人微信一样——自动安装/起企业微信、自动生成配置，客户零手动步骤。
# self_id 不再让用户填：挂件运行时从本地库会话 id 自动识别（widget/wecom 已实现）。
$ExpectedWeComVersion = "5.0.3.6005"

function Get-WeComInstallDir {
    foreach ($key in @(
        "HKCU:\Software\Tencent\WXWork",
        "HKLM:\SOFTWARE\WOW6432Node\Tencent\WXWork",
        "HKLM:\SOFTWARE\Tencent\WXWork"
    )) {
        $ip = (Get-ItemProperty $key -ErrorAction SilentlyContinue).InstallPath
        if ($ip -and (Test-Path -LiteralPath (Join-Path $ip "WXWork.exe"))) { return $ip }
    }
    foreach ($c in @("C:\Program Files (x86)\WXWork", "C:\Program Files\WXWork")) {
        if (Test-Path -LiteralPath (Join-Path $c "WXWork.exe")) { return $c }
    }
    return $null
}

function Install-WeComIfNeeded {
    $installer = Join-Path $Root "assets\wecom\WeCom_5.0.3.6005.exe"
    $dir = Get-WeComInstallDir
    if ($dir) {
        $ver = (Get-Item -LiteralPath (Join-Path $dir "WXWork.exe")).VersionInfo.FileVersion
        if ($ver -like "$ExpectedWeComVersion*") { Write-Host "WeCom found: $dir ($ver)"; return $dir }
        # 版本不符：不强行降级企业微信（企业号自动更新/多用户，强降风险高）。挂件会 strict_version
        # 拒绝注入不符版本、只跑个人微信。客户要用企微需手动装 5.0.3.6005（包内已带安装包）。
        Write-Host "WeCom 版本为 $ver（期望 $ExpectedWeComVersion）：跳过自动降级，本次企微不接入。" -ForegroundColor Yellow
        return $null
    }
    if (-not (Test-Path -LiteralPath $installer)) {
        Write-Host "WeCom 安装包缺失：$installer" -ForegroundColor Yellow; return $null
    }
    Write-Host "安装企业微信 $ExpectedWeComVersion（静默，尽力而为）..."
    try { Start-Process -FilePath $installer -ArgumentList "/S" -Wait -ErrorAction Stop } catch {}
    Start-Sleep -Seconds 6
    $dir = Get-WeComInstallDir
    if (-not $dir) {
        Write-Host "静默安装未完成，改为打开安装向导（装完后重跑本启动器即可自动接入企微）。" -ForegroundColor Yellow
        Start-Process -FilePath $installer | Out-Null
        return $null
    }
    Write-Host "WeCom installed: $dir"
    return $dir
}

function Ensure-WeComConfig([string]$WeComDir) {
    # 自动生成 wecom_hook_config.yaml —— 客户【无需改文件】。self_id 留空 = 运行时自动识别。
    $cfg = Join-Path $Root "wecom_hook_config.yaml"
    if (Test-Path -LiteralPath $cfg) { return }
    $exe = ""
    if ($WeComDir) { $exe = (Join-Path $WeComDir "WXWork.exe").Replace('\', '/') }
    $lines = @(
        "# auto-generated by launcher - do not edit. self_id is auto-detected at runtime.",
        "bridge_port: 8752",
        'token: ""',
        'self_id: ""',
        'process_name: "WXWork.exe"',
        'required_version: "5.0.3.6005"',
        "strict_version: true",
        "wework_exe_path: `"$exe`"",
        "auto_launch: false",
        "wait_login: false",
        "harden_disable_autostart: false",
        "auto_history_sync: false"
    )
    # 纯 ASCII 内容 + 无 BOM（PyYAML 读取零意外）
    [System.IO.File]::WriteAllText($cfg, ($lines -join "`r`n"), (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "generated: wecom_hook_config.yaml (self_id 运行时自动识别)"
}

function Setup-WeCom {
    # 整个企微接入过程 best-effort、绝不阻断个人微信主路径。
    try {
        $dir = Install-WeComIfNeeded
        if (-not $dir) {
            Write-Host "本次仅个人微信渠道。企业微信 5.0.3.6005 装好并登录后重跑启动器即自动接入。" -ForegroundColor Yellow
            return
        }
        Ensure-WeComConfig $dir
        if (-not (Get-Process WXWork -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath (Join-Path $dir "WXWork.exe") | Out-Null
            Write-Host "已启动企业微信；扫码登录后挂件会自动注入 hook 并识别 self_id。"
        } else {
            Write-Host "企业微信已在运行。"
        }
    } catch {
        Write-Host "企微接入步骤出错（忽略，不影响个人微信）：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

if (-not (Is-Administrator)) {
    Write-Host "Administrator permission is required to install WeChat/hook. Please approve the UAC prompt."
    Relaunch-AsAdmin
    exit
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Portable Python missing: $Python"
}

Write-Step "Prepare config"
Ensure-File (Join-Path $Root ".env") (Join-Path $Root ".env.example")
Ensure-File (Join-Path $Root "widget_config.yaml") (Join-Path $Root "widget_config.example.yaml")
if (-not (Test-Path -LiteralPath (Join-Path $Root "broadcast_tasks.json"))) {
    Set-Content -LiteralPath (Join-Path $Root "broadcast_tasks.json") -Value "[]" -Encoding UTF8
}

Write-Step "Install WeChat and hook"
$wxDir = Install-WeChatIfNeeded
Ensure-WeChatUpdateBlocked $wxDir
Block-WeChatUpdateHosts
Assert-WeChatVersion $wxDir
Install-Hook $wxDir

Write-Step "Start WeChat"
Ensure-WeChatUpdateBlocked $wxDir
Block-WeChatUpdateHosts
Assert-WeChatVersion $wxDir
if (Get-Process Weixin -ErrorAction SilentlyContinue) {
    Write-Host "WeChat is already running; keeping the current process."
} else {
    Start-Process -FilePath (Join-Path $wxDir "Weixin.exe") | Out-Null
}

Write-Step "Setup WeCom (企业微信，自动接入)"
Setup-WeCom

Write-Step "Start backend"
Start-Backend

Write-Step "Start widget"
Start-Widget
Write-Host "Hook readiness is handled by the widget supervisor; startup no longer waits up to five minutes."
Write-Host ("workbench launch requested after {0:N1} seconds" -f $LaunchStopwatch.Elapsed.TotalSeconds)

# Mem0 只服务记忆增强，不是工作台首屏和微信接管的启动前置。放到 GUI 拉起之后初始化，
# 避免首次构建索引/加载依赖时让用户长时间看不到任何软件窗口。
Write-Step "Start memory engine"
Start-Mem0

Write-Host ""
Write-Host "AI customer service is running. Admin page: http://127.0.0.1:8000/admin" -ForegroundColor Green
Write-Host "Logs directory: $LogDir"
