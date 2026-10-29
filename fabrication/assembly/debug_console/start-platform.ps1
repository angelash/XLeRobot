param([switch]$OpenBrowser, [string]$ListenHost)
$ErrorActionPreference = 'Stop'
$consoleDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $consoleDir '..\..\..')).Path
$python = Join-Path $repo '.venv-debug\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少 .venv-debug，请按调试台README初始化环境。' }
$networkPath = Join-Path $repo '.local-deploy\studio-network.json'
$network = if (Test-Path -LiteralPath $networkPath) { Get-Content -LiteralPath $networkPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
if (-not $ListenHost) { $ListenHost = if ($network.listen_host) { [string]$network.listen_host } else { '127.0.0.1' } }
$listenAddress = $null
if (-not [System.Net.IPAddress]::TryParse($ListenHost,[ref]$listenAddress) -or $listenAddress.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) { throw 'Studio ListenHost must be an IPv4 address.' }
$publicUrl = if ($network.public_origin) { [string]$network.public_origin } else { 'http://127.0.0.1:8770' }
$listener = Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    try { $health = Invoke-RestMethod 'http://127.0.0.1:8770/api/platform/status' -TimeoutSec 6 }
    catch {
        # A just-stopped listener may still appear in the Windows TCP snapshot.
        Start-Sleep -Milliseconds 300
        $listener = Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue
        if ($listener) { throw '8770仍有监听但HTTP未响应；不会强制释放。' }
    }
    if ($listener -and $health.version -ne '1.0-studio') { throw '8770被其他服务占用；不会强制释放。' }
}
if (-not $listener) {
    $process = Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','fabrication.assembly.debug_console.platform_app:app','--host',$ListenHost,'--port','8770','--log-level','warning') -WorkingDirectory $repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $consoleDir 'platform.stdout.log') -RedirectStandardError (Join-Path $consoleDir 'platform.stderr.log') -PassThru
    Set-Content -LiteralPath (Join-Path $consoleDir 'platform.pid') -Value $process.Id -Encoding ascii
    $healthy = $false
    foreach ($attempt in 1..20) {
        Start-Sleep -Milliseconds 300
        try { Invoke-RestMethod 'http://127.0.0.1:8770/api/platform/status' -TimeoutSec 4 | Out-Null; $healthy = $true; break } catch { }
    }
    if (-not $healthy) { throw '平台未能启动，请检查 platform.stderr.log。原控制服务未被修改。' }
}
Write-Output $publicUrl
if ($OpenBrowser) { Start-Process $publicUrl }
