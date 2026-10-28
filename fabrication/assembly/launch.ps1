param([ValidateSet('status','calibrate','move')][string]$Mode = 'status')
$ErrorActionPreference = 'Stop'
$assemblyDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = (Resolve-Path (Join-Path $assemblyDir '..\..')).Path
$python = Join-Path $repo '.venv-robot\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '缺少 .venv-robot，请先阅读 ROBOT_SOFTWARE_READY_ZH.md。' }
Set-Location $repo

switch ($Mode) {
    'status' {
        Write-Host '检查 CH343，不打开串口数据通道...'
        Get-PnpDevice -PresentOnly -Class Ports -ErrorAction SilentlyContinue |
            Where-Object FriendlyName -match 'CH343' |
            Select-Object Status,FriendlyName,InstanceId
        & $python -X utf8 -c "import pygame; pygame.init(); pygame.joystick.init(); print('Gamepads:', [pygame.joystick.Joystick(i).get_name() for i in range(pygame.joystick.get_count())]); pygame.quit()"
    }
    'calibrate' {
        & $python -X utf8 'fabrication/assembly/calibrate_first_motion.py'
    }
    'move' {
        Write-Host '旧独立动作进程已停用；启动 v2 Web 控制台，按页面说明做小步测试。'
        & (Join-Path $assemblyDir 'debug_console\start.ps1') -OpenBrowser
    }
}
