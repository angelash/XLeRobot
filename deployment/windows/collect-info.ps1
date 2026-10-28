param([string]$OutputPath, [string]$RepositoryPath)
$ErrorActionPreference = 'Stop'
$repo = if ($RepositoryPath) {(Resolve-Path -LiteralPath $RepositoryPath).Path} else {(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path}
if (-not (Test-Path -LiteralPath (Join-Path $repo 'fabrication\assembly\debug_console'))) { $repo = $null }
if (-not $OutputPath) {
    $OutputPath = if ($repo) {Join-Path $repo '.local-deploy\preflight.json'} else {Join-Path $env:USERPROFILE 'XLeRobot-Preflight\preflight.json'}
}
$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$computer = Get-CimInstance Win32_ComputerSystem
$sshService = Get-Service sshd -ErrorAction SilentlyContinue
$report = [ordered]@{
    collected_at = (Get-Date).ToString('o')
    computer_name = $env:COMPUTERNAME
    username = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
    windows = $os.Caption
    windows_version = $os.Version
    architecture = $os.OSArchitecture
    cpu = $cpu.Name
    cpu_architecture_code = $cpu.Architecture
    memory_gb = [Math]::Round($computer.TotalPhysicalMemory / 1GB, 1)
    gpu = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name)
    disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Select-Object DeviceID, @{n='free_gb';e={[Math]::Round($_.FreeSpace/1GB,1)}})
    ipv4 = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -ne '127.0.0.1'} | Select-Object IPAddress,InterfaceAlias)
    ssh_service = if ($sshService) {$sshService.Status.ToString()} else {'not installed'}
    ssh_client = [bool](Get-Command ssh.exe -ErrorAction SilentlyContinue)
    camera_names = @(Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPClass -in @('Camera','Image') -or $_.Name -like '*USB2.0_CAM*'} | Select-Object Name,Status)
    port_devices = @(Get-CimInstance Win32_PnPEntity | Where-Object {$_.PNPClass -eq 'Ports'} | Select-Object Name,Status,PNPDeviceID)
    repository_path = $repo
    control_read_only = $true
}
$python = if ($repo) {Join-Path $repo '.venv-debug\Scripts\python.exe'} else {$null}
if ($python -and (Test-Path -LiteralPath $python)) {
    Push-Location $repo
    try {
        $probe = & $python -X utf8 -m deployment.windows.probe_devices
        if ($LASTEXITCODE -eq 0) { $report.python_devices = ($probe -join "`n") | ConvertFrom-Json }
        else { $report.python_probe_error = 'Python device inventory failed; inspect environment dependencies.' }
    } finally { Pop-Location }
}
$folder = Split-Path -Parent ([System.IO.Path]::GetFullPath($OutputPath))
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$json = $report | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText([System.IO.Path]::GetFullPath($OutputPath), $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Output "Inventory saved: $OutputPath"
Write-Output 'No serial ports were opened; no camera frames or motor commands were requested.'
