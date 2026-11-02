param([string]$Repo='D:\Robot\XLeRobot',[string]$DataDir='D:\Robot\FieldMap',[string]$UserName='Ash',[string]$ClientAddress='192.168.137.1')
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Path $DataDir -Force|Out-Null
$address=$null
if(-not [Net.IPAddress]::TryParse($ClientAddress,[ref]$address) -or $address.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork){throw 'ClientAddress must be one IPv4 address'}
$boot=Join-Path $Repo 'deployment\fieldmap\start-fieldmap.ps1'
if(-not(Test-Path -LiteralPath $boot)){throw 'Field map startup script is missing'}
$allowName='XLeRobot-FieldMap-8782'
$allow=Get-NetFirewallRule -Name $allowName -ErrorAction SilentlyContinue
if($allow){$allow|Get-NetFirewallAddressFilter|Set-NetFirewallAddressFilter -RemoteAddress $ClientAddress|Out-Null}
else{New-NetFirewallRule -Name $allowName -DisplayName 'XLeRobot field map - approved workstation' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8782 -RemoteAddress $ClientAddress -Profile Any|Out-Null}

# Existing Studio deployment may have narrowed a declined Python prompt's
# application-wide Block to exclude8770. Keep its other ports, subtract8782,
# and retain a dedicated Block for all other clients at the new map port.
$pyConfig=Get-Content -LiteralPath (Join-Path $Repo '.venv-debug\pyvenv.cfg')
$pythonHome=(($pyConfig|Where-Object {$_ -match '^home\s*='}|Select-Object -First 1)-split '=',2)[1].Trim()
$basePython=Join-Path $pythonHome 'python.exe'
function ConvertTo-IP([uint64]$n){'{0}.{1}.{2}.{3}' -f (($n-shr 24)-band 255),(($n-shr 16)-band 255),(($n-shr 8)-band 255),($n-band 255)}
$values=@(@($ClientAddress,'127.0.0.1')|ForEach-Object {$b=[Net.IPAddress]::Parse($_).GetAddressBytes();[uint64]$b[0]*16777216+[uint64]$b[1]*65536+[uint64]$b[2]*256+[uint64]$b[3]}|Sort-Object -Unique)
$others=@();$rangeStart=[uint64]0
foreach($v in $values){
    if($v -gt $rangeStart){$others+=((ConvertTo-IP $rangeStart)+'-'+(ConvertTo-IP ($v-1)))}
    $rangeStart=[uint64]$v+1
}
if($rangeStart -le 4294967295){$others+=((ConvertTo-IP $rangeStart)+'-'+(ConvertTo-IP 4294967295))}
$others+=@('::/1','8000::/1')
$blockName='XLeRobot-FieldMap-Other-Clients'
$block=Get-NetFirewallRule -Name $blockName -ErrorAction SilentlyContinue
if($block){$block|Get-NetFirewallAddressFilter|Set-NetFirewallAddressFilter -RemoteAddress $others|Out-Null}
else{New-NetFirewallRule -Name $blockName -DisplayName 'XLeRobot field map - block other clients' -Direction Inbound -Action Block -Protocol TCP -LocalPort 8782 -RemoteAddress $others -Profile Any|Out-Null}
$backup=@()
Get-NetFirewallRule -Enabled True -Direction Inbound -Action Block|ForEach-Object {
    $rule=$_;$appFilter=$rule|Get-NetFirewallApplicationFilter;$portFilter=$rule|Get-NetFirewallPortFilter
    if($appFilter.Program -ieq $basePython -and $portFilter.Protocol -in @('TCP','6') -and $rule.Name -ne $blockName){
        $ports=@($portFilter.LocalPort);$newPorts=@();$changed=$false
        foreach($p in $ports){
            if($p -eq 'Any'){$lo=0;$hi=65535}
            elseif($p -match '^(\d+)-(\d+)$'){$lo=[int]$Matches[1];$hi=[int]$Matches[2]}
            elseif($p -match '^\d+$'){$lo=[int]$p;$hi=$lo}
            else{throw "Unknown block port expression: $p"}
            if($lo -le 8782 -and $hi -ge 8782){
                $changed=$true
                if($lo -lt 8782){$newPorts+=if($lo -eq 8781){'8781'}else{"$lo-8781"}}
                if($hi -gt 8782){$newPorts+=if($hi -eq 8783){'8783'}else{"8783-$hi"}}
            }else{$newPorts+=$p}
        }
        if($changed){
            if(-not $newPorts.Count){throw 'Dedicated block on8782 requires manual review; not removed'}
            $entry=[ordered]@{name=$rule.Name;before=$ports;after=$newPorts};$backup+=$entry
            $backup|ConvertTo-Json -Depth 8|Set-Content -LiteralPath (Join-Path $DataDir ('firewall-before-register-'+(Get-Date -Format yyyyMMddHHmmss)+'.json')) -Encoding UTF8
            $portFilter|Set-NetFirewallPortFilter -LocalPort $newPorts|Out-Null
        }
    }
}
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$boot`" -Repo `"$Repo`" -DataDir `"$DataDir`"" -WorkingDirectory $Repo
$principal=New-ScheduledTaskPrincipal -UserId $UserName -LogonType S4U -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 6 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName XLeRobot-FieldMap -Action $action -Principal $principal -Trigger (New-ScheduledTaskTrigger -AtStartup) -Settings $settings -Force|Out-Null
Start-ScheduledTask -TaskName XLeRobot-FieldMap
Write-Output 'Field map boot task registered. Check HTTP and client access independently.'
