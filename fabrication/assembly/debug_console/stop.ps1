$ErrorActionPreference = 'Stop'
$consoleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $consoleDir 'server.pid'
$matched = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'uvicorn fabrication\.assembly\.debug_console\.app:app' }
if ($matched) {
    $matched | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Output "Stopped $($matched.Count) debug-console process(es)."
} else { Write-Output 'No debug-console process found.' }
if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
