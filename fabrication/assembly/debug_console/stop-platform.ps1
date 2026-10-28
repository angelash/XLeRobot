$ErrorActionPreference = 'Stop'
$consoleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pidFile = Join-Path $consoleDir 'platform.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output '没有平台PID文件；不会停止其他服务。'; exit }
$platformProcessId = [int](Get-Content -LiteralPath $pidFile)
$process = Get-CimInstance Win32_Process -Filter "ProcessId=$platformProcessId"
if ($process -and $process.CommandLine -like '*debug_console.platform_app:app*') {
    try { $state = Invoke-RestMethod 'http://127.0.0.1:8770/api/platform/status' -TimeoutSec 5 } catch { throw '平台状态无法确认；不会停止。' }
    if ($state.recording -and $state.recording.status -eq 'recording') { throw '仍在录制，请先在数据工作室停止并保存。' }
    Stop-Process -Id $platformProcessId
    Remove-Item -LiteralPath $pidFile
    Write-Output '一体化平台已停止；8765控制服务与接管未停止。'
} elseif (-not $process) {
    Remove-Item -LiteralPath $pidFile
    Write-Output '平台进程已退出。'
} else { throw 'PID身份与平台不符；不会停止。' }
