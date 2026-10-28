param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.:-]*$')][string]$RobotHost,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$UserName,
    [ValidateRange(1,65535)][int]$Port=22,
    [ValidateSet('Info','Shell','Tunnel')][string]$Action='Info',
    [string]$ProfileDirectory=(Join-Path $env:LOCALAPPDATA 'XLeRobotRemote')
)
$ErrorActionPreference = 'Stop'
$folder = $ProfileDirectory
$key = Join-Path $folder 'controller_ed25519'
$known = Join-Path $folder 'known_hosts'
if (-not (Test-Path -LiteralPath $key)) { throw 'The dedicated controller private key is missing on this controller computer.' }
if (-not (Test-Path -LiteralPath $known)) { throw 'Use trust-robot.ps1 with the laptop host fingerprint before connecting.' }
$arguments = @('-i',$key,'-o','IdentitiesOnly=yes','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o',('UserKnownHostsFile="'+$known+'"'),'-o','ConnectTimeout=10','-o','ServerAliveInterval=15','-o','ServerAliveCountMax=3','-p',[string]$Port)
if ($Action -eq 'Tunnel') {
    if (Get-NetTCPConnection -LocalPort 18770 -State Listen -ErrorAction SilentlyContinue) { throw '18770 is already in use. Inspect the existing tunnel; it will not be stopped.' }
    Write-Output 'Remote Studio tunnel: http://127.0.0.1:18770 ; keep this process alive. No takeover is started.'
    & ssh.exe @arguments -o ExitOnForwardFailure=yes -N -L '127.0.0.1:18770:127.0.0.1:8770' ($UserName+'@'+$RobotHost)
} elseif ($Action -eq 'Shell') {
    & ssh.exe @arguments -t ($UserName+'@'+$RobotHost)
} else {
    $code = '[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;whoami;Get-CimInstance Win32_OperatingSystem | Select-Object Caption,Version,OSArchitecture | ConvertTo-Json'
    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($code))
    & ssh.exe @arguments ($UserName+'@'+$RobotHost) powershell.exe -NoProfile -NonInteractive -EncodedCommand $encoded
}
if ($LASTEXITCODE -ne 0) { throw "SSH exited $LASTEXITCODE. No password or automatic host-key bypass will be attempted." }
