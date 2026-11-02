# One-time migration of the former independent observation server into Studio.
param([string]$Repo='D:\Robot\XLeRobot',[string]$DataDir='D:\Robot\FieldMap')
$ErrorActionPreference='Stop'
$repoPath=(Resolve-Path -LiteralPath $Repo).Path
$dataPath=(Resolve-Path -LiteralPath $DataDir).Path
if(-not(Test-Path -LiteralPath (Join-Path $repoPath 'deployment\fieldmap\integration.py'))){throw 'Install the Studio fieldmap module first'}
$state=Invoke-RestMethod http://127.0.0.1:8765/api/status -TimeoutSec 10
$studio=Invoke-RestMethod http://127.0.0.1:8770/api/platform/status -TimeoutSec 10
if($state.controller.controller -ne 'offline' -or $state.agent_run.running -or $state.limit_selftest.running -or $state.manual_calibration.active -or $studio.recording){throw 'Preserve active control, calibration or recording; Studio was not restarted'}
$controllerPid=$state.server_pid
$studioOwners=@(Get-NetTCPConnection -State Listen -LocalPort 8770|Select-Object -ExpandProperty OwningProcess -Unique)
foreach($ownerId in $studioOwners){if((Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId").CommandLine -notlike '*debug_console.platform_app:app*'){throw 'Unexpected8770 owner'}}
$oldOwners=@(Get-NetTCPConnection -State Listen -LocalPort 8782 -ErrorAction SilentlyContinue|Select-Object -ExpandProperty OwningProcess -Unique)
foreach($ownerId in $oldOwners){if((Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId").CommandLine -notlike '*deployment.fieldmap.server*'){throw 'Unexpected8782 owner'}}
$backup=Join-Path $repoPath ('.local-deploy\fieldmap-studio-'+(Get-Date -Format yyyyMMdd-HHmmss))
New-Item -ItemType Directory -Path $backup -Force|Out-Null
$network=Join-Path $repoPath '.local-deploy\studio-network.json'
$hadNetwork=Test-Path -LiteralPath $network
if($hadNetwork){Copy-Item -LiteralPath $network -Destination (Join-Path $backup 'studio-network.json');$config=Get-Content -LiteralPath $network -Raw -Encoding UTF8|ConvertFrom-Json}
else{$config=[pscustomobject]@{}}
$oldTask=Get-ScheduledTask -TaskName XLeRobot-FieldMap -ErrorAction SilentlyContinue
$resumeOldTask=$oldTask -and $oldTask.State -ne 'Disabled'
function Start-Studio {
    Start-ScheduledTask -TaskName XLeRobot-Boot
    foreach($attempt in 1..30){Start-Sleep -Milliseconds 500;try{return Invoke-RestMethod http://127.0.0.1:8770/api/platform/status -TimeoutSec 3}catch{}}
    throw 'Studio did not restart'
}
try{
    if($oldTask){Stop-ScheduledTask -TaskName XLeRobot-FieldMap;Disable-ScheduledTask -TaskName XLeRobot-FieldMap|Out-Null}
    foreach($attempt in 1..12){if(-not(Get-NetTCPConnection -State Listen -LocalPort 8782 -ErrorAction SilentlyContinue)){break};Start-Sleep -Milliseconds 250}
    foreach($ownerId in $oldOwners){$p=Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId";if($p -and $p.CommandLine -like '*deployment.fieldmap.server*'){Stop-Process -Id $ownerId}}
    $mapPath=Join-Path $dataPath 'map.json'
    $map=Get-Content -LiteralPath $mapPath -Raw -Encoding UTF8|ConvertFrom-Json
    $before=[ordered]@{map=(Get-FileHash -LiteralPath $mapPath).Hash;images=@{};review=$null}
    foreach($image in $map.observations){$p=Join-Path (Join-Path $dataPath 'images') $image.image;$before.images[$image.image]=(Get-FileHash -LiteralPath $p).Hash}
    $reviewPath=Join-Path $dataPath 'reviews.json'
    if(Test-Path -LiteralPath $reviewPath){$before.review=(Get-FileHash -LiteralPath $reviewPath).Hash}
    $config|Add-Member -NotePropertyName fieldmap_data_dir -NotePropertyValue $dataPath -Force
    $text=$config|ConvertTo-Json -Depth 10
    [IO.File]::WriteAllText($network,$text,(New-Object System.Text.UTF8Encoding($false)))
    foreach($ownerId in $studioOwners){Stop-Process -Id $ownerId}
    $after=Start-Studio
    $loaded=Invoke-RestMethod http://127.0.0.1:8770/api/platform/fieldmap/map -TimeoutSec 8
    $current=Invoke-RestMethod http://127.0.0.1:8765/api/status -TimeoutSec 10
    if($current.server_pid -ne $controllerPid){throw 'Controller process changed unexpectedly'}
    if($loaded.id -ne $map.id -or $loaded.observations.Count -ne $map.observations.Count -or $loaded.motion_enabled){throw 'Unexpected integrated map'}
    if((Get-FileHash -LiteralPath $mapPath).Hash -ne $before.map){throw 'Map file changed during migration'}
    foreach($name in $before.images.Keys){if((Get-FileHash -LiteralPath (Join-Path (Join-Path $dataPath 'images') $name)).Hash -ne $before.images[$name]){throw 'Photo changed during migration'}}
    if($before.review -and (Get-FileHash -LiteralPath $reviewPath).Hash -ne $before.review){throw 'Review data changed during migration'}
}catch{
    if($hadNetwork){Copy-Item -LiteralPath (Join-Path $backup 'studio-network.json') -Destination $network}
    if($resumeOldTask){Enable-ScheduledTask -TaskName XLeRobot-FieldMap|Out-Null;Start-ScheduledTask -TaskName XLeRobot-FieldMap}
    throw
}

# The integrated page is live and all survey files have been checked before
# retiring only the old standalone entry points. Preserve recovery copies.
foreach($name in @('server.py','start-fieldmap.ps1','register-fieldmap.ps1')){
    $legacy=Join-Path (Join-Path $repoPath 'deployment\fieldmap') $name
    if(Test-Path -LiteralPath $legacy){
        $resolved=(Resolve-Path -LiteralPath $legacy).Path
        if((Split-Path -Parent $resolved) -ne (Join-Path $repoPath 'deployment\fieldmap')){throw 'Legacy path escaped expected module directory'}
        Move-Item -LiteralPath $resolved -Destination (Join-Path $backup $name)
    }
}
$firewallNames=@('XLeRobot-FieldMap-8782','XLeRobot-FieldMap-Other-Clients')
$rules=@($firewallNames|ForEach-Object {Get-NetFirewallRule -Name $_ -ErrorAction SilentlyContinue})
if($rules.Count){$rules|Select-Object Name,Enabled,Direction,Action,Profile|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $backup 'retired-firewall.json') -Encoding UTF8}
foreach($name in $firewallNames){Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue|Remove-NetFirewallRule}
# Restore the old Python Block only when its ranges still match our exact hole.
Get-NetFirewallRule -Enabled True -Direction Inbound -Action Block|ForEach-Object {
    $rule=$_;$ports=$rule|Get-NetFirewallPortFilter
    if($rule.Name -like 'TCP Query User*' -and ((@($ports.LocalPort)|Sort-Object)-join ',') -eq '0-8769,8771-8781,8783-65535'){
        [pscustomobject]@{name=$rule.Name;ports=@($ports.LocalPort)}|ConvertTo-Json -Depth 5|Set-Content -LiteralPath (Join-Path $backup 'python-block-before.json') -Encoding UTF8
        $ports|Set-NetFirewallPortFilter -LocalPort @('0-8769','8771-65535')|Out-Null
    }
}
if(Get-NetTCPConnection -State Listen -LocalPort 8782 -ErrorAction SilentlyContinue){throw 'Legacy listener still exists'}
$receipt=[pscustomobject]@{url='http://192.168.137.150:8770/#fieldmap';controller_pid=$controllerPid;data_dir=$dataPath;images=$loaded.observations.Count;features=$loaded.features.Count;map_sha256=$before.map.ToLowerInvariant();legacy_port_closed=$true;legacy_task_disabled=([bool]$oldTask);backup=$backup;motion_enabled=$loaded.motion_enabled}
$receipt|ConvertTo-Json -Depth 6|Set-Content -LiteralPath (Join-Path $backup 'receipt.json') -Encoding UTF8
$receipt|ConvertTo-Json -Depth 6
