# Start the workstation compute node in the foreground (Ctrl+C to stop).
# It connects out to the robot Studio; no port is opened on this computer.
param([string]$Server='http://192.168.137.150:8770', [double]$Duration=0, [switch]$CpuSpeech, [string[]]$CudaDllDir=@())
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$token=Join-Path $env:LOCALAPPDATA 'XLeRobotRemote\autonomy-token.txt'
if(-not (Test-Path -LiteralPath $token)){throw "缺少节点令牌 $token；请先从机器人笔记本私有目录复制。"}
Set-Location -LiteralPath $repo
$arguments=@('-X','utf8','-m','deployment.autonomy.worker','--server',$Server,'--token-file',$token)
if($Duration -gt 0){$arguments+=@('--duration',[string]$Duration)}
if($CpuSpeech){$arguments+=@('--whisper-device','cpu')}
foreach($dir in $CudaDllDir){$arguments+=@('--cuda-dll-dir',$dir)}
& python @arguments
