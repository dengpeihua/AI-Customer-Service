param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath $ProjectRoot).Path
$scriptPath = $MyInvocation.MyCommand.Path

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $arguments = @(
        "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $scriptPath,
        "-ProjectRoot", $root
    )
    $elevated = Start-Process -FilePath "powershell.exe" -ArgumentList $arguments `
        -Verb RunAs -WindowStyle Hidden -PassThru -Wait
    exit $elevated.ExitCode
}

$pythonw = (Resolve-Path -LiteralPath (Join-Path $root "runtime\python\pythonw.exe")).Path
$widgetEntry = (Resolve-Path -LiteralPath (Join-Path $root "run_widget.py")).Path
$logs = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logs | Out-Null

# 只关闭本项目的挂件进程；同机其他 Python/客服程序不受影响。打包启动链在部分机器上
# 最终登记成 python.exe（即便入口请求的是 pythonw.exe），因此先枚举两种宿主名，再用
# run_widget.py + 本项目 runtime 路径做双重收窄。
$widgetProcesses = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in @("python.exe", "pythonw.exe") } |
    Where-Object {
        $commandLine = [string]$_.CommandLine
        $executable = [string]$_.ExecutablePath
        # 非管理员父进程读取管理员 pythonw 时，CommandLine/ExecutablePath 可能都是空。
        # 精确窗口标题是本应用自己设置的第二身份证明，可让提升后的重启脚本找到旧挂件，
        # 避免旧实例没关、新实例被单实例锁立即拒绝。
        $process = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        # 同时识别旧标题，保证这次升级后第一次启动也能关闭仍在运行的旧版工作台。
        $isWidgetWindow = $process -and $process.MainWindowTitle -in @("AI客服", "AI 客服工作台")
        $isWidget = $isWidgetWindow -or $commandLine.IndexOf(
            "run_widget.py", [StringComparison]::OrdinalIgnoreCase
        ) -ge 0
        $isThisRuntime = $executable.Equals(
            $pythonw, [StringComparison]::OrdinalIgnoreCase
        ) -or $commandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0
        $isWidget -and ($isThisRuntime -or $isWidgetWindow)
    }
foreach ($item in $widgetProcesses) {
    $process = Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue
    if ($process) {
        [void]$process.CloseMainWindow()
        Wait-Process -Id $item.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
        if (Get-Process -Id $item.ProcessId -ErrorAction SilentlyContinue) {
            Stop-Process -Id $item.ProcessId -ErrorAction Stop
        }
    }
}

& (Join-Path $PSScriptRoot "Restart-AIBackend.ps1") -ProjectRoot $root

$env:ACS_WIDGET_LOG_DIR = $logs
$widget = Start-Process -FilePath $pythonw -ArgumentList @($widgetEntry) `
    -WorkingDirectory $root -PassThru
Start-Sleep -Seconds 3
if (-not (Get-Process -Id $widget.Id -ErrorAction SilentlyContinue)) {
    throw "挂件重启失败，请查看 logs\widget.err.log"
}

$health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 5
if ([string]$health.llm -notlike "*embed_model=embo-01*embed_dim=1536*") {
    throw "后端已启动，但未加载 MiniMax embo-01 / 1536 维配置：$($health.llm)"
}

Write-Output "runtime_restart=ok backend=$($health.llm) widget_pid=$($widget.Id)"
