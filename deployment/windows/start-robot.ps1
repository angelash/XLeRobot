param([ValidateSet('Preview','Monitor')][string]$Mode='Preview', [switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$tools = Join-Path $repo '.local-deploy\tools'
if (Test-Path -LiteralPath (Join-Path $tools 'ffmpeg.exe')) { $env:PATH = $tools + ';' + $env:PATH }
if (-not (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue)) { Write-Warning 'FFmpeg not found. The Web can run, but camera streams need FFmpeg.' }
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $state = Invoke-RestMethod 'http://127.0.0.1:8765/api/original/status' -TimeoutSec 4
    if ([bool]$state.preview_only -ne ($Mode -eq 'Preview')) {
        throw 'Existing controller uses another mode. End takeover yourself, then stop-robot.ps1 before changing mode.'
    }
}
if ($Mode -eq 'Preview') { $env:XLEROBOT_PREVIEW_ONLY = '1' }
else { Remove-Item Env:\XLEROBOT_PREVIEW_ONLY -ErrorAction SilentlyContinue }
& (Join-Path $repo 'fabrication\assembly\debug_console\start.ps1')
$platformUrl = (& (Join-Path $repo 'fabrication\assembly\debug_console\start-platform.ps1') | Select-Object -Last 1)
Write-Output "Mode=$Mode. Startup does not start takeover or enable torque. Web: $platformUrl"
if ($OpenBrowser) { Start-Process $platformUrl }
