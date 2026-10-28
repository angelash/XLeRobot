param(
    [Parameter(Mandatory=$true)][string]$PublicKeyPath,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$UserName,
    [Parameter(Mandatory=$true)][string[]]$ClientAddress,
    [switch]$Apply
)
$ErrorActionPreference = 'Stop'
$PublicKeyPath = (Resolve-Path -LiteralPath $PublicKeyPath).Path
$key = ([System.IO.File]::ReadAllText($PublicKeyPath)).Trim()
if ($key -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?$') { throw 'Expected one Ed25519 PUBLIC key. Never provide a private key.' }
foreach ($address in $ClientAddress) {
    $parsed = $null
    if (-not [System.Net.IPAddress]::TryParse($address, [ref]$parsed) -or $address -in @('0.0.0.0','::')) { throw 'ClientAddress must contain exact client IPs, not networks or wildcards.' }
}
if (-not $Apply) {
    Write-Output "PLAN: enable Windows OpenSSH Server for local account $UserName; permit TCP 22 from $($ClientAddress -join ', ') only."
    Write-Output 'Append the supplied public key and require public-key authentication for that account. No robot services are started.'
    exit
}
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script from an elevated PowerShell on the NEW robot laptop.' }
$target = Get-LocalUser -Name $UserName
if (-not $target.Enabled) { throw 'The target local user must be enabled and have logged in locally once.' }
$existing = Get-Service sshd -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -eq 'Running') { throw 'An SSH server is already running. Inspect its current setup before using this first-time provisioning script.' }
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($capability.State -ne 'Installed') { Add-WindowsCapability -Online -Name $capability.Name | Out-Null }
$bin = Join-Path $env:WINDIR 'System32\OpenSSH'
$sshRoot = Join-Path $env:ProgramData 'ssh'
New-Item -ItemType Directory -Path $sshRoot -Force | Out-Null
$config = Join-Path $sshRoot 'sshd_config'
if (-not (Test-Path -LiteralPath $config)) { Copy-Item -LiteralPath (Join-Path $bin 'sshd_config_default') -Destination $config }
$original = [System.IO.File]::ReadAllText($config)
if ($original -match '(?m)^\s*Port\s+(?!22\s*$)\d+') { throw 'Existing SSH config uses a non-default port; inspect manually.' }
$adminSids = @(Get-LocalGroupMember -SID 'S-1-5-32-544' | ForEach-Object {$_.SID.Value})
if ($target.SID.Value -in $adminSids) {
    $keyFile = Join-Path $sshRoot 'administrators_authorized_keys'
    $accessSid = 'S-1-5-32-544'
} else {
    $profile = Get-CimInstance Win32_UserProfile | Where-Object {$_.SID -eq $target.SID.Value} | Select-Object -First 1
    if (-not $profile) { throw 'Log in to the target account locally once so its profile exists.' }
    $keyFolder = Join-Path $profile.LocalPath '.ssh'
    New-Item -ItemType Directory -Path $keyFolder -Force | Out-Null
    $keyFile = Join-Path $keyFolder 'authorized_keys'
    $accessSid = $target.SID.Value
}
$lines = @()
if (Test-Path -LiteralPath $keyFile) { $lines = [System.IO.File]::ReadAllLines($keyFile) }
$keyBody = (($key -split ' ')[0..1] -join ' ')
if (-not ($lines | Where-Object {$_ -like ($keyBody+'*')})) {
    [System.IO.File]::WriteAllText($keyFile, (($lines + $key) -join "`n")+"`n", (New-Object System.Text.UTF8Encoding($false)))
}
& icacls.exe $keyFile /inheritance:r /grant:r ('*'+$accessSid+':(F)') '*S-1-5-18:(F)' '*S-1-5-32-544:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Public-key file ACL configuration failed' }
$marker = '# XLeRobot remote account'
if (-not $original.Contains($marker)) {
    $backup = $config + '.before-xlerobot-' + (Get-Date -Format 'yyyyMMddHHmmss')
    Copy-Item -LiteralPath $config -Destination $backup
    $block = "`r`n$marker`r`nMatch User $($UserName.ToLowerInvariant())`r`n    PubkeyAuthentication yes`r`n    AuthenticationMethods publickey`r`n    PasswordAuthentication no`r`n"
    [System.IO.File]::WriteAllText($config, $original+$block, (New-Object System.Text.UTF8Encoding($false)))
}
& (Join-Path $bin 'ssh-keygen.exe') -A
if ($LASTEXITCODE -ne 0) { throw 'Host key generation failed' }
& (Join-Path $bin 'sshd.exe') -t -f $config
if ($LASTEXITCODE -ne 0) { throw 'SSH configuration validation failed; service was not started. Restore the saved config if needed.' }
$defaultRule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if ($defaultRule) { $defaultRule | Disable-NetFirewallRule | Out-Null }
$rule = Get-NetFirewallRule -Name 'XLeRobot-SSH' -ErrorAction SilentlyContinue
if ($rule) {
    $rule | Set-NetFirewallRule -Enabled True -Profile Any -Action Allow | Out-Null
    $rule | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $ClientAddress | Out-Null
} else {
    New-NetFirewallRule -Name 'XLeRobot-SSH' -DisplayName 'XLeRobot SSH - approved controller IP only' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 22 -RemoteAddress $ClientAddress -Profile Any | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
Write-Output "Remote server prepared for local user: $UserName"
Write-Output 'Provide the following PUBLIC host fingerprint to the controller operator:'
& (Join-Path $bin 'ssh-keygen.exe') -lf (Join-Path $sshRoot 'ssh_host_ed25519_key.pub')
Write-Output 'No Windows password, private host key, or Tailscale login token should be sent.'
