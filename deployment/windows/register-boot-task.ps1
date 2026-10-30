# Registers XLeRobot-Boot: start the robot services at system startup without anyone
# logging in. S4U runs as the current account without storing its password. The
# login task is disabled so only one process ever owns the serial ports.
param([ValidateSet('Preview','Monitor')][string]$Mode='Monitor', [int]$DelaySeconds=20, [switch]$Replace, [switch]$Apply)
$ErrorActionPreference = 'Stop'
$entry = Join-Path $PSScriptRoot 'start-robot-at-boot.ps1'
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskName = 'XLeRobot-Boot'
$logonTask = 'XLeRobot-Interactive'
if (-not $Apply) {
    Write-Output "PLAN: register $taskName for $identity at system startup (S4U, no stored password, delay $DelaySeconds s), Mode=$Mode; disable $logonTask if present. Startup never starts takeover or changes torque."
    exit
}
$current = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $current.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run from an elevated session; startup tasks need administrator rights.' }
if (-not (Test-Path -LiteralPath $entry)) { throw "Missing $entry" }
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and -not $Replace) { throw 'Task already exists. Inspect it, then pass -Replace to update it.' }
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $entry + '" -Mode ' + $Mode)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType S4U -RunLevel Limited
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT' + $DelaySeconds + 'S'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable
if ($existing) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Trigger $trigger -Settings $settings -Description 'XLeRobot control service and Studio at startup; no login required.' | Out-Null
if (Get-ScheduledTask -TaskName $logonTask -ErrorAction SilentlyContinue) { Disable-ScheduledTask -TaskName $logonTask | Out-Null }
$task = Get-ScheduledTask -TaskName $taskName
Write-Output ("Registered $taskName (" + $task.Principal.LogonType + ", " + $task.State + "). $logonTask disabled. Start now with: Start-ScheduledTask -TaskName $taskName")
