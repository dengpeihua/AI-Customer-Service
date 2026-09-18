$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
. (Join-Path $ScriptDir "Runtime.ps1")
$Python = Resolve-AcsPython -ProjectRoot $Root
$Pythonw = Resolve-AcsPython -ProjectRoot $Root -Windowless
$MemPython = Join-Path $Root "mem\.venv\Scripts\python.exe"
$LogDir = Join-Path $Root "logs"
$createdLauncherMutex = $false
$script:LauncherMutex = [System.Threading.Mutex]::new(
    $true,
    "Local\AI.Customer.Service.Douyin.Launcher",
    [ref]$createdLauncherMutex
)
if (-not $createdLauncherMutex) {
    Write-Host "AI customer service startup/login is already in progress." -ForegroundColor Yellow
    exit 0
}

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Ensure-File([string]$Path, [string]$Template) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Copy-Item -LiteralPath $Template -Destination $Path
        Write-Host "created: $Path"
    }
}

function Wait-Http([string]$Url, [int]$Attempts = 40) {
    for ($i = 0; $i -lt $Attempts; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return $true }
        } catch {}
        if ($i -lt ($Attempts - 1)) { Start-Sleep -Seconds 1 }
    }
    return $false
}

function Stop-ProductPython([string]$Needle) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($Root) -and $_.CommandLine.Contains($Needle) } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Start-MemoryEngine {
    if (-not (Test-Path -LiteralPath $MemPython)) {
        Write-Host "Memory engine environment missing; skipping optional memory service" -ForegroundColor Yellow
        return
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8888/health" -TimeoutSec 2
        $expectedData = [IO.Path]::GetFullPath((Join-Path $Root "mem\data"))
        $actualData = [IO.Path]::GetFullPath([string]$health.data_dir)
        if ($health.status -eq "ok" -and $health.mode -eq "local-oss" -and
            $health.collection -eq "memories_minimax_embo_01_1536" -and
            $actualData.Equals($expectedData, [StringComparison]::OrdinalIgnoreCase) -and
            $health.telemetry -eq $false -and
            $health.service_revision -eq "acs-mem0-companion-v5-resilient-dedupe") {
            Write-Host "Memory engine already running"
            return
        }
    } catch {}
    Stop-ProductPython "scripts\run_mem0.py"
    Start-Process -FilePath $MemPython `
        -ArgumentList @((Join-Path $Root "scripts\run_mem0.py"), "--host", "127.0.0.1", "--port", "8888") `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "mem0.out.log") `
        -RedirectStandardError (Join-Path $LogDir "mem0.err.log") | Out-Null
    if (-not (Wait-Http "http://127.0.0.1:8888/health" 60)) {
        Write-Host "Memory engine did not become healthy; see logs\mem0.err.log" -ForegroundColor Yellow
    }
}

function Backup-Database {
    $database = Join-Path $Root "acs.db"
    if (Test-Path -LiteralPath $database) {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $backup = Join-Path $Root "acs.db.before-douyin-$stamp.bak"
        Copy-Item -LiteralPath $database -Destination $backup
        Write-Host "database backup: $backup"
    }
}

function Start-Backend {
    if (Wait-Http "http://127.0.0.1:8000/health" 1) {
        & (Join-Path $ScriptDir "Restart-AIBackend.ps1") -ProjectRoot $Root
        return
    }
    Backup-Database
    Start-Process -FilePath $Python -ArgumentList @((Join-Path $Root "scripts\run_backend.py")) `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "backend.out.log") `
        -RedirectStandardError (Join-Path $LogDir "backend.err.log") | Out-Null
    if (-not (Wait-Http "http://127.0.0.1:8000/health" 40)) {
        Stop-ProductPython "run_backend.py"
        Write-Host "Backend did not become healthy. The workbench remains open in degraded mode; see logs\backend.err.log" -ForegroundColor Red
    }
}

function Start-Widget {
    Stop-ProductPython "run_widget.py"
    $environment = [System.Environment]::GetEnvironmentVariables()
    $environment["ACS_WIDGET_LOG_DIR"] = $LogDir
    $environment["ACS_MANAGE_SERVICES"] = "1"
    $environment["PYTHONDONTWRITEBYTECODE"] = "1"
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Pythonw
    $startInfo.Arguments = '"' + (Join-Path $Root "run_widget.py") + '"'
    $startInfo.WorkingDirectory = $Root
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    foreach ($key in $environment.Keys) {
        $startInfo.EnvironmentVariables[[string]$key] = [string]$environment[$key]
    }
    $script:WidgetProcess = [System.Diagnostics.Process]::Start($startInfo)
}

function Test-WidgetRunning {
    if (-not $script:WidgetProcess) { return $false }
    try {
        $script:WidgetProcess.Refresh()
        return -not $script:WidgetProcess.HasExited
    } catch {
        return $false
    }
}

Set-Location -LiteralPath $Root
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
Ensure-File (Join-Path $Root ".env") (Join-Path $Root ".env.example")
Ensure-File (Join-Path $Root "widget_config.yaml") (Join-Path $Root "widget_config.example.yaml")
Ensure-File (Join-Path $Root "douyin_accounts.yaml") (Join-Path $Root "douyin_accounts.example.yaml")

Write-Step "Close existing workbench"
Stop-ProductPython "run_widget.py"

Write-Step "Start widget"
Start-Widget
Start-Sleep -Milliseconds 800
if (-not (Test-WidgetRunning)) {
    throw "The AI customer service window failed to start. See logs\widget.err.log"
}

Write-Step "Start backend"
Start-Backend
Write-Step "Start memory engine"
Start-MemoryEngine
Write-Host "Douyin AI customer service started. Complete login in each opened Chrome profile." -ForegroundColor Green
