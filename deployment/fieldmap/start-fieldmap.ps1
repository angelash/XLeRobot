param([string]$Repo='D:\Robot\XLeRobot',[string]$DataDir='D:\Robot\FieldMap',[string]$HostAddress='0.0.0.0',[int]$Port=8782)
$ErrorActionPreference='Stop'
Set-Location -LiteralPath $Repo
$python=Join-Path $Repo '.venv-debug\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $python)){throw 'Robot Python environment is missing'}
New-Item -ItemType Directory -Path $DataDir -Force|Out-Null
& $python -X utf8 -m deployment.fieldmap.server --data-dir $DataDir --host $HostAddress --port $Port --parent-pid $PID
exit $LASTEXITCODE
