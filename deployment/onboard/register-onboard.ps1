# Register a laptop-local conversation bridge; starts no robot motion.
param(
    [Parameter(Mandatory=$true)][string]$Workspace,
    [Parameter(Mandatory=$true)][string]$PythonExe,
    [Parameter(Mandatory=$true)][string]$Config,
    [switch]$Replace,
    [switch]$Apply
)
$ErrorActionPreference='Stop'
$name='XLeRobot-Onboard'
$entry=Join-Path $PSScriptRoot 'start-onboard.ps1'
if(-not $Apply){Write-Output "PLAN: register $name at boot, use existing laptop profile, loopback only, observation mode";exit}
foreach($path in @($Workspace,$PythonExe,$Config,$entry)){if(-not(Test-Path -LiteralPath $path)){throw "Missing $path"}}
$principal=New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator session required to register the boot task'}
$existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if($existing -and -not $Replace){throw 'Task exists; inspect before passing -Replace'}
$arguments='-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "'+$entry+'" -Workspace "'+$Workspace+'" -PythonExe "'+$PythonExe+'" -Config "'+$Config+'"'
$action=New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$identity=[System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskPrincipal=New-ScheduledTaskPrincipal -UserId $identity -LogonType S4U -RunLevel Limited
$trigger=New-ScheduledTaskTrigger -AtStartup
$trigger.Delay='PT30S'
$settings=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -StartWhenAvailable -RestartCount 99 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Principal $taskPrincipal -Trigger $trigger -Settings $settings -Force -Description 'Laptop-local Xiaojiu mailbox and clean source synchronization; no motion control.' | Out-Null
Get-ScheduledTask -TaskName $name | Select-Object TaskName,State
