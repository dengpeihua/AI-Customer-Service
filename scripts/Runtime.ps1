function Resolve-AcsPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ProjectRoot,
        [switch]$Windowless
    )

    $executable = if ($Windowless) { "pythonw.exe" } else { "python.exe" }
    $candidates = @(
        (Join-Path $ProjectRoot "runtime\python\$executable"),
        (Join-Path $ProjectRoot ".venv\Scripts\$executable")
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }

    throw "Python environment missing. Run .\scripts\Setup-AICustomerService.ps1 first."
}
