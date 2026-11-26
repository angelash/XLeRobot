# Install the PICO VR update on the robot laptop.
#
# Default is a read-only plan. -Apply copies the hash-checked payload (with backups),
# installs the Studio HTTPS certificate, enables same-port HTTPS in
# .local-deploy\studio-network.json and restarts only the Studio gateway (8770).
# The VR input route lives in the 8765 controller; it takes effect after the
# controller restarts. -RestartController does that now through the existing
# XLeRobot-Boot task, and only while no takeover, web/VR session, agent run or
# recording is active. Nothing here starts a takeover, enables torque or moves.
# Any failure restores the backed-up files and configuration.
param(
    [string]$Repo = 'D:\Robot\XLeRobot',
    [string]$PicoAddress,
    [switch]$Apply,
    [switch]$RestartController,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path -LiteralPath $Repo).Path
$package = Split-Path -Parent $MyInvocation.MyCommand.Path
$manifest = Get-Content -LiteralPath (Join-Path $package 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$python = Join-Path $Repo '.venv-debug\Scripts\python.exe'
$console = Join-Path $Repo 'fabrication\assembly\debug_console'
$networkPath = Join-Path $Repo '.local-deploy\studio-network.json'
$tlsTarget = Join-Path $Repo '.local-deploy\studio-tls'
if (-not (Test-Path -LiteralPath (Join-Path $console 'platform_app.py'))) { throw "Not an XLeRobot checkout: $Repo" }
if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv-debug' }

function Hash([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Fingerprint([string]$PemPath) {
    $text = Get-Content -LiteralPath $PemPath -Raw
    # -split, not String.Split: Windows PowerShell 5.1 would split on single characters.
    $body = (($text -split '-----BEGIN CERTIFICATE-----')[1] -split '-----END CERTIFICATE-----')[0] -replace '\s', ''
    $sha = [System.Security.Cryptography.SHA256]::Create()
    (($sha.ComputeHash([Convert]::FromBase64String($body)) | ForEach-Object { $_.ToString('X2') }) -join ':')
}
function Get-Json([string]$Url) { try { Invoke-RestMethod $Url -TimeoutSec 5 } catch { $null } }
function Under-Root([string]$Root, [string]$Relative) {
    $prefix = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $resolved = [System.IO.Path]::GetFullPath((Join-Path $Root $Relative))
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Path outside update root: $Relative" }
    $resolved
}

# ---- plan (read-only) ----
$plan = @()
foreach ($entry in $manifest.files) {
    $target = Under-Root $Repo $entry.path
    $source = Under-Root (Join-Path $package 'payload') $entry.path
    if ((Hash $source) -ne $entry.sha256) { throw "Payload checksum mismatch: $($entry.path)" }
    $current = if (Test-Path -LiteralPath $target) { Hash $target } else { $null }
    $state = if ($current -eq $entry.sha256) { 'already' }
             elseif ($null -eq $current) { if (@($entry.accepted).Count -eq 0) { 'new' } else { 'missing' } }
             elseif (@($entry.accepted) -contains $current) { 'update' } else { 'conflict' }
    $plan += [pscustomobject]@{path = $entry.path; state = $state; current = $current}
}
function Get-Activity([bool]$AllowStopped = $false) {
    $controller = Get-Json 'http://127.0.0.1:8765/api/status'
    $studio = Get-Json 'http://127.0.0.1:8770/api/platform/status'
    if ($controller) {
        if ($controller.controller.controller -ne 'offline') { 'controller takeover is online' }
        if ($controller.web_control.active) { 'web/VR control session is active' }
        if ($controller.agent_run.running) { 'assistant run is active' }
        if ($controller.limit_selftest.running) { 'limit test or servo session is active' }
        if ($controller.manual_calibration.active) { 'manual calibration is active' }
    } elseif (-not $AllowStopped) { 'controller 8765 not answering' }
    if (-not $studio -and -not $AllowStopped) { 'Studio 8770 not answering; recording state is unknown' }
    if ($studio -and $studio.recording -and $studio.recording.status -eq 'recording') { 'Studio is recording' }
}
$busy = @(Get-Activity)
$network = if (Test-Path -LiteralPath $networkPath) { Get-Content -LiteralPath $networkPath -Raw -Encoding UTF8 | ConvertFrom-Json } else { $null }
$ca = Join-Path $package 'studio-tls\ca.crt'
Write-Output '== PICO VR update plan =='
$plan | Format-Table -AutoSize | Out-String -Width 200 | Write-Output
Write-Output ("Studio network config: " + $(if ($network) { $networkPath } else { 'none (Studio listens on 127.0.0.1 only; run configure-studio-network.ps1 first)' }))
Write-Output ("HTTPS on port 8770: " + $(if ($network.tls) { 'already configured' } else { 'will be enabled' }))
Write-Output ("CA SHA-256 (compare on the headset): " + (Fingerprint $ca))
if ($PicoAddress) { Write-Output "PICO address to allow through the Studio firewall: $PicoAddress" }
Write-Output ("Activity blocking a restart: " + $(if ($busy) { $busy -join '; ' } else { 'none' }))
if ($plan | Where-Object { $_.state -in @('conflict', 'missing') }) { Write-Output 'Some files differ from the expected vehicle version; inspect before applying.' }
if (-not $Apply) { Write-Output 'Plan only. Re-run with -Apply (and optionally -RestartController, -PicoAddress).'; exit 0 }

# ---- apply ----
if ($plan | Where-Object { $_.state -in @('conflict', 'missing') }) { throw 'Concurrent source change on the vehicle; nothing was changed.' }
if ($busy) { throw ('Not idle: ' + ($busy -join '; ')) }
if (-not $network) { throw 'Studio network is not configured; run deployment\windows\configure-studio-network.ps1 first.' }
$run = Join-Path $Repo ('.local-deploy\pico-vr-install-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $run | Out-Null
$log = Join-Path $run 'install.log'
function Log([string]$Message) { Add-Content -LiteralPath $log -Value ((Get-Date -Format 's') + ' ' + $Message) -Encoding UTF8; Write-Output $Message }
$copied = @()
$serviceRestart = [pscustomobject]@{studio = $false; controller = $false}
$networkBackup = Join-Path $run 'studio-network.json'
Copy-Item -LiteralPath $networkPath -Destination $networkBackup
$tlsBackup = Join-Path $run 'studio-tls-before'
if (Test-Path -LiteralPath $tlsTarget) { Copy-Item -LiteralPath $tlsTarget -Destination $tlsBackup -Recurse }

function Get-StudioIds {
    $ids = @(Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    $owned = @()
    foreach ($id in $ids) {
        $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $id)
        if (-not $p -or $p.CommandLine -notlike '*debug_console.platform_app:app*') { throw 'Port 8770 owner is not Studio; not stopping it.' }
        $owned += [int]$id
        $parent = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $p.ParentProcessId)
        if ($parent -and $parent.Name -eq 'python.exe' -and $parent.CommandLine -like '*debug_console.platform_app:app*') { $owned += [int]$parent.ProcessId }
    }
    $owned
}
function Wait-Http([string]$Url, [int]$Seconds) {
    for ($i = 0; $i -lt $Seconds * 2; $i++) { if (Get-Json $Url) { return $true }; Start-Sleep -Milliseconds 500 }
    $false
}
function Restart-Services([bool]$Controller, [bool]$Restoring = $false) {
    # Regression tests take time. A takeover begun since the plan must still win.
    $blocking = @(Get-Activity $Restoring)
    if ($blocking) { throw ('Not idle before restart: ' + ($blocking -join '; ')) }
    # Track attempts before side effects: a failed restart may already have
    # stopped a service, whereas a failed file copy must not restart hardware.
    $serviceRestart.studio = $true
    foreach ($id in Get-StudioIds) { Stop-Process -Id $id -ErrorAction SilentlyContinue }
    for ($i = 0; $i -lt 20 -and (Get-NetTCPConnection -LocalPort 8770 -State Listen -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 300 }
    if ($Controller) {
        $serviceRestart.controller = $true
        & (Join-Path $console 'stop.ps1') | ForEach-Object { Log $_ }
        Start-ScheduledTask -TaskName 'XLeRobot-Boot'
        if (-not (Wait-Http 'http://127.0.0.1:8765/api/status' 90)) { throw 'Controller did not come back through XLeRobot-Boot' }
    } else {
        # Detached, like earlier deployments: the gateway must outlive this SSH session.
        $launch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = 'powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $console 'start-platform.ps1') + '"'
            CurrentDirectory = $Repo}
        if ($launch.ReturnValue -ne 0) { throw 'Studio launch failed' }
    }
    if (-not (Wait-Http 'http://127.0.0.1:8770/api/platform/status' 90)) { throw 'Studio did not come back on 8770' }
}
try {
    foreach ($entry in $manifest.files) {
        $target = Join-Path $Repo $entry.path
        $source = Join-Path $package ('payload\' + $entry.path)
        if ((Hash $source) -ne $entry.sha256) { throw "Payload changed since plan: $($entry.path)" }
        $current = if (Test-Path -LiteralPath $target) { Hash $target } else { $null }
        if ($current -eq $entry.sha256) { continue }
        $planned = ($plan | Where-Object { $_.path -eq $entry.path }).current
        if ($current -ne $planned) { throw "Vehicle file changed since plan: $($entry.path)" }
        $backup = Join-Path $run ('before\' + $entry.path)
        if (Test-Path -LiteralPath $target) {
            New-Item -ItemType Directory -Path (Split-Path -Parent $backup) -Force | Out-Null
            Copy-Item -LiteralPath $target -Destination $backup
        }
        New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
        # A copy may fail after truncating a destination. Include it in rollback
        # before the first write, including brand-new files.
        $copied += $entry
        Copy-Item -LiteralPath $source -Destination $target
        if ((Hash $target) -ne $entry.sha256) { throw "Copy verification failed: $($entry.path)" }
    }
    Log ("Copied $($copied.Count) file(s); backups in $run")
    New-Item -ItemType Directory -Path $tlsTarget -Force | Out-Null
    foreach ($name in @('server.crt', 'server.key', 'ca.crt')) { Copy-Item -LiteralPath (Join-Path $package ('studio-tls\' + $name)) -Destination (Join-Path $tlsTarget $name) -Force }
    $config = Get-Content -LiteralPath $networkPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $origins = @($config.allowed_origins)
    foreach ($origin in @($config.allowed_origins)) {
        if ($origin -like 'http://*:8770') { $secure = 'https://' + $origin.Substring(7); if ($origins -notcontains $secure) { $origins += $secure } }
    }
    $config | Add-Member -NotePropertyName allowed_origins -NotePropertyValue $origins -Force
    $config | Add-Member -NotePropertyName tls -NotePropertyValue ([ordered]@{cert = '.local-deploy/studio-tls/server.crt'; key = '.local-deploy/studio-tls/server.key'; ca = '.local-deploy/studio-tls/ca.crt'}) -Force
    [System.IO.File]::WriteAllText($networkPath, ($config | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
    Log 'Enabled same-port HTTPS in studio-network.json'
    if (-not $SkipTests) {
        Push-Location $Repo
        try {
            $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
            & $python -m pytest -q -p no:cacheprovider `
                fabrication/assembly/debug_console/test_vr_teleop.py fabrication/assembly/debug_console/test_web_teleop.py `
                fabrication/assembly/debug_console/test_studio_tls.py fabrication/assembly/debug_console/test_platform.py `
                fabrication/assembly/debug_console/test_original_teleop.py *>&1 | Tee-Object -FilePath (Join-Path $run 'tests.txt') | Select-Object -Last 3 | ForEach-Object { Log $_ }
            if ($LASTEXITCODE -ne 0) { throw 'Vehicle regression tests failed' }
        } finally { Pop-Location }
    }
    Restart-Services $RestartController.IsPresent
    $check = & $python -c "import ssl,sys,urllib.request as u;c=ssl.create_default_context(cafile=sys.argv[1]);print(u.urlopen('https://127.0.0.1:8770/vr/',context=c,timeout=5).status)" (Join-Path $tlsTarget 'ca.crt')
    if ($LASTEXITCODE -ne 0 -or "$check".Trim() -ne '200') { throw "HTTPS check failed: $check" }
    Log 'HTTPS /vr/ answers 200 with the local CA'
    $web = Get-Json 'http://127.0.0.1:8765/api/web/status'
    if ($web -and $null -ne $web.mode) { Log 'Controller exposes the VR session (mode field present)' }
    else { Log 'Controller still runs the previous code: VR input becomes available after its next restart (-RestartController or reboot).' }
    if ($PicoAddress) {
        $principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
        $listen = ([uri]$config.public_origin).Host
        $clients = @(@($config.client_addresses) + $PicoAddress | Where-Object { $_ } | Sort-Object -Unique)
        $command = "deployment\windows\configure-studio-network.ps1 -ListenAddress $listen -ClientAddress " + ($clients -join ',') + ' -Apply'
        if ($principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
            & (Join-Path $Repo 'deployment\windows\configure-studio-network.ps1') -ListenAddress $listen -ClientAddress $clients -Apply | ForEach-Object { Log $_ }
            Log 'Firewall updated; restart Studio once more to load the new origin list.'
            Restart-Services $false
        } else { Log "Run as administrator to allow the PICO: $command" }
    }
    Log ('Done. Open https://' + ([uri]$config.public_origin).Host + ':8770/vr/ on the PICO. CA SHA-256: ' + (Fingerprint (Join-Path $tlsTarget 'ca.crt')))
} catch {
    $reason = $_.Exception.Message
    Log "FAILED: $reason. Restoring."
    foreach ($entry in $copied) {
        $target = Join-Path $Repo $entry.path; $backup = Join-Path $run ('before\' + $entry.path)
        if (Test-Path -LiteralPath $backup) { Copy-Item -LiteralPath $backup -Destination $target -Force } else { Remove-Item -LiteralPath $target -ErrorAction SilentlyContinue }
    }
    Copy-Item -LiteralPath $networkBackup -Destination $networkPath -Force
    if (Test-Path -LiteralPath $tlsBackup) { Remove-Item -LiteralPath $tlsTarget -Recurse -Force; Copy-Item -LiteralPath $tlsBackup -Destination $tlsTarget -Recurse }
    elseif (Test-Path -LiteralPath $tlsTarget) { Remove-Item -LiteralPath $tlsTarget -Recurse -Force }
    # A controller that was restarted on the new code goes back through the same boot task.
    try {
        if ($serviceRestart.studio) {
            Restart-Services $serviceRestart.controller $true
            Log 'Restored files; services restarted on the previous code.'
        } else { Log 'Restored files; running services were not restarted.' }
    }
    catch { Log ('Restart after restore failed: ' + $_.Exception.Message + '. Check boot-start.log before any control.') }
    throw
}
