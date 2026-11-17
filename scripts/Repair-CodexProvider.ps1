param([string]$Target = "shared", [switch]$Doctor, [switch]$Apply, [switch]$UpdateConfig)
$entry = Join-Path (Split-Path $PSScriptRoot -Parent) 'Start.ps1'
$action = if ($Doctor) { 'doctor' } else { 'repair' }
& $entry -Action $action -Target $Target -Apply:$Apply
exit $LASTEXITCODE
