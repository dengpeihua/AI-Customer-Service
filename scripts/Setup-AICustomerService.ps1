[CmdletBinding()]
param(
    [switch]$SkipMemory,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Resolve-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $knownPaths = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\uv.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\uv.exe")
    )
    foreach ($candidate in $knownPaths) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $wingetPackages = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    $wingetPackage = Get-ChildItem -LiteralPath $wingetPackages -Directory `
        -Filter "astral-sh.uv_*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($wingetPackage) {
        $candidate = Join-Path $wingetPackage.FullName "uv.exe"
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "uv is missing and winget is unavailable. Install uv, then rerun this script."
    }

    Write-Step "Install uv"
    & $winget.Source install --id astral-sh.uv --exact --source winget `
        --accept-package-agreements --accept-source-agreements --disable-interactivity | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install uv (exit code $LASTEXITCODE)."
    }

    $env:Path = @(
        [Environment]::GetEnvironmentVariable("Path", "Process"),
        [Environment]::GetEnvironmentVariable("Path", "User"),
        [Environment]::GetEnvironmentVariable("Path", "Machine")
    ) -join ";"
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    foreach ($candidate in $knownPaths) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $wingetPackage = Get-ChildItem -LiteralPath $wingetPackages -Directory `
        -Filter "astral-sh.uv_*" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($wingetPackage) {
        $candidate = Join-Path $wingetPackage.FullName "uv.exe"
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "uv was installed but cannot be found in the current PowerShell session."
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )
    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "$FilePath failed with exit code $LASTEXITCODE."
    }
}

Set-Location -LiteralPath $Root
$uv = Resolve-Uv

Write-Step "Install managed Python 3.11"
Invoke-Checked $uv @("python", "install", "3.11")

$MainPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $MainPython)) {
    Write-Step "Create application environment"
    Invoke-Checked $uv @("venv", "--python", "3.11", (Join-Path $Root ".venv"))
}

Write-Step "Install application dependencies"
Invoke-Checked $uv @(
    "pip", "install", "--python", $MainPython,
    "--requirements", (Join-Path $Root "requirements.lock.txt")
)

if (-not $SkipMemory) {
    $MemPython = Join-Path $Root "mem\.venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $MemPython)) {
        Write-Step "Create memory service environment"
        Invoke-Checked $uv @("venv", "--python", "3.11", (Join-Path $Root "mem\.venv"))
    }

    Write-Step "Install memory service dependencies"
    Invoke-Checked $uv @(
        "pip", "install", "--python", $MemPython,
        "--requirements", (Join-Path $Root "mem\server\requirements.lock.txt")
    )
}

Write-Step "Create local configuration and database"
$configureArgs = @((Join-Path $Root "scripts\configure_local.py"))
if ($NonInteractive) {
    $configureArgs += "--non-interactive"
}
Invoke-Checked $MainPython $configureArgs

Write-Step "Verify source installation"
Invoke-Checked $MainPython @(
    "-c",
    "import fastapi, sqlalchemy, PySide6, playwright, agents; print('python_imports=ok')"
)

Write-Host ""
Write-Host "Source installation completed." -ForegroundColor Green
Write-Host "Start with: .\scripts\Start-AICustomerService.ps1"
Write-Host "If configuration is incomplete, rerun: .\.venv\Scripts\python.exe .\scripts\configure_local.py"
