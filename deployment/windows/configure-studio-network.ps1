param(
    [Parameter(Mandatory=$true)][string]$ListenAddress,
    [Parameter(Mandatory=$true)][string[]]$ClientAddress,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
foreach ($textAddress in @($ListenAddress)+$ClientAddress) {
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($textAddress,[ref]$parsed) -or
        $parsed.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork -or
        $parsed.Equals([System.Net.IPAddress]::Any) -or [System.Net.IPAddress]::IsLoopback($parsed)) {
        throw 'Provide exact non-loopback IPv4 addresses; networks and wildcards are not allowed.'
    }
}
$origin = 'http://'+$ListenAddress+':8770'
function ConvertTo-IPv4Text([uint64]$Number) {
    return ('{0}.{1}.{2}.{3}' -f (($Number -shr 24) -band 255),(($Number -shr 16) -band 255),(($Number -shr 8) -band 255),($Number -band 255))
}
function Get-OtherClientRanges([string[]]$Addresses) {
    $values = @($Addresses | ForEach-Object {
        $bytes = [System.Net.IPAddress]::Parse($_).GetAddressBytes()
        [uint64]$bytes[0]*16777216 + [uint64]$bytes[1]*65536 + [uint64]$bytes[2]*256 + [uint64]$bytes[3]
    } | Sort-Object -Unique)
    $rangeStart = [uint64]0
    foreach ($value in $values) {
        if ($value -gt $rangeStart) { (ConvertTo-IPv4Text $rangeStart)+'-'+(ConvertTo-IPv4Text ($value-1)) }
        $rangeStart = [uint64]$value+1
    }
    if ($rangeStart -le 4294967295) { (ConvertTo-IPv4Text $rangeStart)+'-'+(ConvertTo-IPv4Text 4294967295) }
    '::/0'
}
$otherClients = @(Get-OtherClientRanges (@($ClientAddress)+'127.0.0.1'))
if (-not $Apply) {
    Write-Output "PLAN: Studio listens on IPv4; permit $($ClientAddress -join ', ') to $origin only."
    Write-Output "Other clients remain blocked on TCP8770: $($otherClients -join ', ')"
    Write-Output 'Persist the local origin/config and a scoped TCP8770 firewall rule. No robot service or takeover is changed.'
    exit
}
$principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as administrator on the robot laptop.' }
if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction SilentlyContinue)) { throw 'ListenAddress is not assigned to this computer.' }
$configFolder = Join-Path $repo '.local-deploy'
New-Item -ItemType Directory -Path $configFolder -Force | Out-Null
$pythonConfig = Get-Content -LiteralPath (Join-Path $repo '.venv-debug\pyvenv.cfg')
$pythonHome = (($pythonConfig | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1) -split '=',2)[1].Trim()
$basePython = Join-Path $pythonHome 'python.exe'
# Windows creates application-wide Block rules when its network prompt is declined.
# An explicit Block overrides an Allow: retain the block on all other TCP ports,
# retain UDP, and block every unapproved client on this one Studio TCP port.
$blockedRuleName = 'XLeRobot-Studio-Other-Clients'
$block = Get-NetFirewallRule -Name $blockedRuleName -ErrorAction SilentlyContinue
if ($block) {
    $block | Set-NetFirewallRule -Enabled True -Profile Any -Direction Inbound -Action Block | Out-Null
    $block | Get-NetFirewallApplicationFilter | Set-NetFirewallApplicationFilter -Program $basePython | Out-Null
    $block | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 8770 -RemotePort Any | Out-Null
    $block | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -LocalAddress Any -RemoteAddress $otherClients | Out-Null
} else {
    New-NetFirewallRule -Name $blockedRuleName -DisplayName 'XLeRobot Studio - block unapproved clients' -Direction Inbound -Action Block -Protocol TCP -LocalPort 8770 -Program $basePython -RemoteAddress $otherClients -Profile Any | Out-Null
}
$narrowed = @()
Get-NetFirewallRule -Enabled True -Direction Inbound -Action Block | ForEach-Object {
    $application = $_ | Get-NetFirewallApplicationFilter
    $portFilter = $_ | Get-NetFirewallPortFilter
    $addressFilter = $_ | Get-NetFirewallAddressFilter
    if ($application.Program -ieq $basePython -and $portFilter.Protocol -in @('TCP','6') -and
        $portFilter.LocalPort -eq 'Any' -and $addressFilter.LocalAddress -eq 'Any' -and $addressFilter.RemoteAddress -eq 'Any') {
        $narrowed += [ordered]@{name=$_.Name;program=$application.Program;profile=$_.Profile.ToString();previous_local_port='Any';new_local_port=@('0-8769','8771-65535')}
    }
}
if ($narrowed.Count -gt 0) {
    $backup = Join-Path $configFolder ('studio-firewall-before-'+(Get-Date -Format 'yyyyMMddHHmmss')+'.json')
    [System.IO.File]::WriteAllText($backup,($narrowed | ConvertTo-Json -Depth 5),(New-Object System.Text.UTF8Encoding($false)))
    foreach ($item in $narrowed) {
        Get-NetFirewallRule -Name $item.name | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -LocalPort @('0-8769','8771-65535') | Out-Null
    }
}
$name = 'XLeRobot-Studio-LAN'
$rule = Get-NetFirewallRule -Name $name -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Set-NetFirewallRule -Enabled True -Profile Any -Direction Inbound -Action Allow | Out-Null
    $rule | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort 8770 -RemotePort Any | Out-Null
    $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -LocalAddress $ListenAddress -RemoteAddress $ClientAddress | Out-Null
} else {
    New-NetFirewallRule -Name $name -DisplayName 'XLeRobot Studio - approved controller addresses' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8770 -LocalAddress $ListenAddress -RemoteAddress $ClientAddress -Profile Any | Out-Null
}
$config = [ordered]@{listen_host='0.0.0.0';public_origin=$origin;allowed_origins=@($origin);client_addresses=@($ClientAddress)}
$path = Join-Path $repo '.local-deploy\studio-network.json'
New-Item -ItemType Directory -Path (Split-Path -Parent $path) -Force | Out-Null
if (Test-Path -LiteralPath $path) { Copy-Item -LiteralPath $path -Destination ($path+'.before-'+(Get-Date -Format 'yyyyMMddHHmmss')) }
[System.IO.File]::WriteAllText($path,($config | ConvertTo-Json -Depth 4),(New-Object System.Text.UTF8Encoding($false)))
Write-Output "Configured Studio URL: $origin"
Write-Output 'Restart only the Studio gateway to apply. Controller port8765 and the existing takeover are unchanged.'
