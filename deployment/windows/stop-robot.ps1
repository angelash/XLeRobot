param([switch]$Apply)
$ErrorActionPreference = 'Stop'
if (-not $Apply) { Write-Output 'PLAN: require takeover offline and recording stopped, then stop the two robot services. Pass -Apply.'; exit }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$state = Invoke-RestMethod 'http://127.0.0.1:8765/api/status' -TimeoutSec 5
if ($state.controller.controller -eq 'online') { throw 'Takeover is still online (including paused). End it yourself in the Web first.' }
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$($state.server_pid)"
if (-not $process -or $process.CommandLine -notlike '*debug_console.app:app*') { throw 'Controller process identity is unverified.' }
& (Join-Path $repo 'fabrication\assembly\debug_console\stop-platform.ps1')
Stop-Process -Id $state.server_pid
Write-Output 'Robot services stopped; no calibration, torque, or mode change requested.'
