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
if (-not $Apply) {
    Write-Output "PLAN: Studio listens on IPv4; permit $($ClientAddress -join ', ') to $origin only."
    Write-Output 'Persist the local origin/config and a scoped TCP8770 firewall rule. No robot service or takeover is changed.'
    exit
}
$principal = New-Object System.Security.Principal.WindowsPrincipal([System.Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run as administrator on the robot laptop.' }
if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $ListenAddress -ErrorAction SilentlyContinue)) { throw 'ListenAddress is not assigned to this computer.' }
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
