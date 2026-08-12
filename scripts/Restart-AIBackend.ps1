param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$python = (Resolve-Path -LiteralPath (Join-Path $root "runtime\python\python.exe")).Path
$runner = (Resolve-Path -LiteralPath (Join-Path $root "scripts\run_backend.py")).Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

$listener = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort 8000 -State Listen `
    -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $backendPid = [int]$listener.OwningProcess
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$backendPid"
    if (-not $process -or $process.Name -ne "python.exe") {
        throw "拒绝停止：127.0.0.1:8000 的进程不是 python.exe"
    }
    if ($process.ExecutablePath -and
            -not $process.ExecutablePath.Equals($python, [StringComparison]::OrdinalIgnoreCase)) {
        throw "拒绝停止：127.0.0.1:8000 不是本项目 runtime Python"
    }
    Stop-Process -Id $backendPid -ErrorAction Stop
    Wait-Process -Id $backendPid -Timeout 10 -ErrorAction SilentlyContinue
}

$stdout = Join-Path $logs "backend.deepseek.out.log"
$stderr = Join-Path $logs "backend.deepseek.err.log"
$arguments = @($runner, "--host", "127.0.0.1", "--port", "8000")
$started = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
    -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru

$health = $null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($health.status -eq "ok") {
            break
        }
    } catch {
        Start-Sleep -Milliseconds 500
    }
}
if (-not $health -or $health.status -ne "ok") {
    throw "后端未恢复健康，请查看 $stderr"
}
Push-Location $root
try {
    $expected = (& $python $runner --print-runtime |
        Select-Object -Last 1).Trim()
} finally {
    Pop-Location
}
if ([string]$health.llm -ne $expected) {
    throw "后端已启动但未加载当前代码/配置：expected=$expected actual=$($health.llm)"
}

Write-Output "backend_restart=ok pid=$($started.Id)"
Write-Output "health=$($health.status) $($health.llm)"
