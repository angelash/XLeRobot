# Startup entry used by the XLeRobot-Boot task. Runs without an interactive login
# (session 0); XInput, DirectShow cameras and the CH343 serial boards were verified
# to work there. Retries because USB devices or the network may still be settling.
param([ValidateSet('Preview','Monitor')][string]$Mode='Monitor', [int]$Attempts=6, [int]$RetrySeconds=20)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$log = Join-Path $repo 'fabrication\assembly\debug_console\boot-start.log'
function Write-BootLog([string]$message) {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format 's') + ' ' + $message) -Encoding UTF8
}
$sessionId = (Get-Process -Id $PID).SessionId
Write-BootLog "boot start Mode=$Mode user=$env:USERNAME session=$sessionId"
for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
    try {
        $result = & (Join-Path $PSScriptRoot 'start-robot.ps1') -Mode $Mode
        Write-BootLog ("started on attempt $attempt; " + (($result | Out-String).Trim() -replace '\s+', ' '))
        exit 0
    } catch {
        Write-BootLog ("attempt $attempt failed: " + $_.Exception.Message)
        if ($attempt -lt $Attempts) { Start-Sleep -Seconds $RetrySeconds }
    }
}
exit 1
