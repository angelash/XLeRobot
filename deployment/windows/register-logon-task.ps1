param([ValidateSet('Preview','Monitor')][string]$Mode='Preview', [switch]$Apply)
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot 'start-robot.ps1'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if (-not $Apply) { Write-Output "PLAN: register XLeRobot-Interactive for $identity at login, Mode=$Mode. No motion is enabled."; exit }
$taskName = 'XLeRobot-Interactive'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'Task already exists. Inspect it before replacing it.' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $entry + '" -Mode ' + $Mode)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings | Out-Null
Write-Output 'Task registered, not started. Log in locally; a remote operator may then use Start-ScheduledTask -TaskName XLeRobot-Interactive.'
