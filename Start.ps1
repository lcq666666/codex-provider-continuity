param(
    [string]$Action = "",
    [string]$Target = "shared",
    [switch]$Apply
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$entry = Join-Path $PSScriptRoot "scripts\portable.py"
$runtime = $null
$runtimePrefix = @()
foreach ($candidate in @(
    @{ Name = 'py'; Prefix = @('-3.14') },
    @{ Name = 'py'; Prefix = @('-3') },
    @{ Name = 'python'; Prefix = @() },
    @{ Name = 'python3'; Prefix = @() }
)) {
    $command = Get-Command $candidate.Name -ErrorAction SilentlyContinue
    if (-not $command) { continue }
    $probe = @($candidate.Prefix) + @('-c', 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)')
    $probeStatus = 1
    try {
        $ErrorActionPreference = "Continue"
        & $command.Source @probe 2>$null | Out-Null
        $probeStatus = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = "Stop"
    }
    if ($probeStatus -eq 0) {
        $runtime = $command.Source
        $runtimePrefix = @($candidate.Prefix)
        break
    }
}
if (-not $runtime) {
    Write-Host 'Install Python 3.14 from https://www.python.org/downloads/ then run START.cmd again.'
    exit 1
}
$invokeArgs = @($runtimePrefix) + @($entry, '--target', $Target)
if ($Action) { $invokeArgs += @('--action', $Action) } else { $invokeArgs += '--menu' }
if ($Apply) { $invokeArgs += '--apply' }
& $runtime @invokeArgs
exit $LASTEXITCODE
