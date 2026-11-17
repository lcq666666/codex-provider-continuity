param([string]$Target = "shared", [switch]$Apply)
$entry = Join-Path (Split-Path $PSScriptRoot -Parent) 'Start.ps1'
& $entry -Action 'cc-switch' -Target $Target -Apply:$Apply
exit $LASTEXITCODE
