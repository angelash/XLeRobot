param([switch]$OpenBrowser)
$ErrorActionPreference = 'Stop'
$consoleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $consoleDir '..\..\..')).Path
$python = Join-Path $repo '.venv-debug\Scripts\python.exe'
$listener = Get-NetTCPConnection -LocalPort 8766 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $status = Invoke-RestMethod 'http://127.0.0.1:8766/api/original/status' -TimeoutSec 3
    if (-not $status.preview_only) { throw '8766由其他服务占用，不会强制停止。' }
} else {
    $process = Start-Process -FilePath $python -ArgumentList @('-m','fabrication.assembly.debug_console.preview_server') -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $consoleDir 'original-preview.stdout.log') -RedirectStandardError (Join-Path $consoleDir 'original-preview.stderr.log') -PassThru
    Set-Content -LiteralPath (Join-Path $consoleDir 'original-preview.pid') -Value $process.Id -Encoding ascii
    $healthy = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 250
        try {
            $status = Invoke-RestMethod 'http://127.0.0.1:8766/api/original/status' -TimeoutSec 2
            $healthy = $status.preview_only
            if ($healthy) { break }
        } catch { }
    }
    if (-not $healthy) { throw '隔离预览未启动，请检查original-preview.stderr.log。' }
}
$url = 'http://127.0.0.1:8766/original'
Write-Output $url
if ($OpenBrowser) { Start-Process $url }
