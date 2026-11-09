# Run on the robot laptop only when Apple USB support is missing.
# Never runs from Studio or during package preparation.
$ErrorActionPreference = 'Stop'
$kit = Split-Path -Parent $MyInvocation.MyCommand.Path
$driver = Join-Path $kit 'drivers\AppleMobileDeviceSupport64.msi'
if (-not (Test-Path -LiteralPath $driver)) { throw 'Missing signed Apple USB driver package.' }
$manifest = Get-Content -LiteralPath (Join-Path $kit 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$expected = @($manifest.files | Where-Object { $_.path -eq 'drivers/AppleMobileDeviceSupport64.msi' })
if ($expected.Count -ne 1) { throw 'Apple package is not listed in the kit manifest.' }
$actual = (Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash.ToLower()
if ($actual -ne $expected[0].sha256) { throw 'Apple package checksum mismatch.' }
$signature = Get-AuthenticodeSignature -LiteralPath $driver
if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Apple Inc') { throw 'Apple package signature is not valid.' }
if (Get-Service -Name 'Apple Mobile Device Service' -ErrorAction SilentlyContinue) {
    Write-Output 'Apple Mobile Device support already exists; no changes made.'
    exit 0
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this driver setup as administrator on the robot laptop.' }
$installLog = Join-Path $kit 'apple-driver-install.log'
$arguments = '/i "' + $driver + '" /passive /norestart /L*v "' + $installLog + '"'
$process = Start-Process -FilePath 'msiexec.exe' -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
if ($process.ExitCode -notin @(0,3010)) { throw ('Apple USB support installation failed with code ' + $process.ExitCode) }
Write-Output ('Apple USB support installed. Reboot required: ' + ($process.ExitCode -eq 3010))
