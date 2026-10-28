param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$consoleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $consoleDir '..\..\..')).Path
$python = Join-Path $repo '.venv-debug\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少 .venv-debug，请按 README 初始化只读调试环境。' }
$listener = Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $stdout = Join-Path $consoleDir 'server.stdout.log'
    $stderr = Join-Path $consoleDir 'server.stderr.log'
    $process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','fabrication.assembly.debug_console.app:app','--host','127.0.0.1','--port','8765','--log-level','warning') -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Set-Content -LiteralPath (Join-Path $consoleDir 'server.pid') -Value $process.Id -Encoding ascii
    $healthy = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 250
        try { Invoke-RestMethod 'http://127.0.0.1:8765/api/status' -TimeoutSec 2 | Out-Null; $healthy = $true; break } catch { }
    }
    if (-not $healthy) { throw '只读调试台未能在 8765 端口启动，请查看 server.stderr.log。' }
}
$url = 'http://127.0.0.1:8765'
Write-Output $url
if ($OpenBrowser) { Start-Process $url }
