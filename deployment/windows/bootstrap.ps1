param([string]$KitRoot, [string]$PythonExe, [switch]$Apply)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
if (-not $KitRoot) { throw 'Pass -KitRoot with the extracted transfer-kit directory.' }
$KitRoot = (Resolve-Path -LiteralPath $KitRoot).Path
$venv = Join-Path $repo '.venv-debug'
if (-not $Apply) {
    Write-Output "PLAN: install Python 3.11 x64 if missing; create isolated $venv; install core wheels; copy FFmpeg."
    Write-Output 'No services, login tasks, hardware calibration or remote server will be started.'
    exit
}
if (-not [Environment]::Is64BitOperatingSystem -or (Get-CimInstance Win32_Processor | Select-Object -First 1).Architecture -ne 9) {
    throw 'This kit targets native Windows x64. An ARM64 or 32-bit computer needs a rebuilt runtime kit.'
}
if (Test-Path -LiteralPath $venv) { throw 'Existing .venv-debug found. Refusing to overwrite an environment; inspect it first.' }
if (-not $PythonExe) {
    $candidate = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'
    if (Test-Path -LiteralPath $candidate) { $PythonExe = $candidate }
    elseif (Get-Command py.exe -ErrorAction SilentlyContinue) {
        $found = & py.exe -3.11 -c 'import sys;print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0) { $PythonExe = ($found | Select-Object -Last 1) }
    }
    if (-not $PythonExe) {
        $installer = Join-Path $KitRoot 'installers\python-3.11.9-amd64.exe'
        if (-not (Test-Path -LiteralPath $installer)) { throw 'Install 64-bit Python 3.11, or pass -PythonExe; bundled installer is missing.' }
        $install = Start-Process -FilePath $installer -ArgumentList @('/quiet','InstallAllUsers=0','Include_launcher=0','PrependPath=0','Include_test=0','Include_pip=1',('TargetDir="' + (Split-Path -Parent $candidate) + '"')) -Wait -PassThru -WindowStyle Hidden
        if ($install.ExitCode -notin @(0,3010)) { throw "Python installer exited $($install.ExitCode)" }
        $PythonExe = $candidate
    }
}
# Windows PowerShell 5.1 removes embedded double quotes from native arguments.
# Keep these probes free of quoted Python literals so the exact code survives.
$runtime = & $PythonExe -c 'import sys,struct;print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor)+chr(47)+str(struct.calcsize(chr(80))*8))'
if ($LASTEXITCODE -ne 0 -or $runtime -ne '3.11/64') { throw 'The prepared wheelhouse requires Python 3.11 x64. Other architectures need a new kit.' }
& $PythonExe -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
$targetPython = Join-Path $venv 'Scripts\python.exe'
& $targetPython -m pip install --no-index --find-links (Join-Path $KitRoot 'wheelhouse') -r (Join-Path $KitRoot 'requirements-core.lock')
if ($LASTEXITCODE -ne 0) { throw 'Offline wheel install failed; services have not been started.' }
$tools = Join-Path $repo '.local-deploy\tools'
New-Item -ItemType Directory -Path $tools -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $KitRoot 'tools\ffmpeg.exe') -Destination (Join-Path $tools 'ffmpeg.exe')
& $targetPython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Dependency check failed' }
& $targetPython -c 'import fastapi,httpx,cv2,numpy,PIL,serial,pydantic;print(fastapi.__version__,cv2.__version__,numpy.__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Native import failed. Inspect the error; install the bundled Microsoft VC++ runtime if a DLL is missing.' }
& (Join-Path $PSScriptRoot 'collect-info.ps1')
Write-Output 'Core installed. Start with start-robot.ps1 -Mode Preview; hardware motion remains a separate step.'
