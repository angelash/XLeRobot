param([Parameter(Mandatory=$true)][string]$Workspace,[Parameter(Mandatory=$true)][string]$PythonExe,[Parameter(Mandatory=$true)][string]$Config)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $Workspace
$settings=Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
New-Item -ItemType Directory -Path $settings.state_dir -Force | Out-Null
$out=Join-Path $settings.state_dir 'service.stdout.log'
$err=Join-Path $settings.state_dir 'service.stderr.log'
$ErrorActionPreference='Continue'
& $PythonExe -m deployment.onboard.service --config $Config --parent-pid $PID 1>> $out 2>> $err
exit $LASTEXITCODE
