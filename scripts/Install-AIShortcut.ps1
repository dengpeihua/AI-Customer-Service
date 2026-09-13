param(
    [string]$ProjectRoot = ""
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
} else {
    $ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
}

# Keep this Windows PowerShell 5.1 script ASCII-only. Build Chinese filenames from
# Unicode code points so the script remains valid even when it has no UTF-8 BOM.
$customerService = [string]([char]0x5BA2) + [char]0x670D
$launcherName = [string]([char]0x542F) + [char]0x52A8 + "AI" + $customerService + ".bat"
$shortcutName = "AI" + $customerService + ".lnk"
$launcher = Join-Path $ProjectRoot $launcherName
$icon = Join-Path $ProjectRoot "assets\branding\ai-customer-service.ico"

if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher"
}
if (-not (Test-Path -LiteralPath $icon)) {
    throw "Shortcut icon not found: $icon"
}

$shell = New-Object -ComObject WScript.Shell
$desktop = [string]$shell.SpecialFolders.Item("Desktop")
$shortcutPath = Join-Path $desktop $shortcutName
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $ProjectRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "Douyin AI customer service"
$shortcut.WindowStyle = 1
$shortcut.Save()

Write-Output $shortcutPath
