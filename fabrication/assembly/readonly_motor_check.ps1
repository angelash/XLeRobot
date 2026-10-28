param([string[]]$Ports = @('COM3','COM4'), [string]$OutputPath)
$ErrorActionPreference = 'Stop'
# Standalone Feetech protocol queries only. Never import a robot controller.
# Only instruction 0x01 (PING) and 0x02 (READ) can reach the serial write.
function Invoke-ReadOnlyQuery {
    param($Serial, [int]$MotorId, [int]$Instruction, [byte[]]$Parameters = @())
    if ($Instruction -notin @(1,2)) { throw 'Only PING and READ are allowed' }
    if ($MotorId -lt 1 -or $MotorId -gt 253) { throw 'No broadcast permitted' }
    [byte[]]$packet = @(255,255,$MotorId,($Parameters.Count+2),$Instruction) + $Parameters
    $sum = 0
    for ($n=2; $n -lt $packet.Length; $n++) { $sum += [int]$packet[$n] }
    $packet += [byte](255-($sum -band 255))
    $Serial.DiscardInBuffer()
    $Serial.Write($packet,0,$packet.Length)
    $received = [System.Collections.Generic.List[byte]]::new()
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while ($timer.ElapsedMilliseconds -lt 100) {
        if ($Serial.BytesToRead -gt 0) {
            $received.Add([byte]$Serial.ReadByte())
            for ($offset=0; $offset -le $received.Count-6; $offset++) {
                if ($received[$offset] -ne 255 -or $received[$offset+1] -ne 255 -or $received[$offset+2] -ne $MotorId) { continue }
                $length = [int]$received[$offset+3]
                if ($length -lt 2 -or $received.Count -lt $offset+$length+4) { continue }
                $check = 0
                for ($n=$offset+2; $n -lt $offset+$length+4; $n++) { $check += [int]$received[$n] }
                if (($check -band 255) -ne 255) { continue }
                $data = @()
                if ($length -gt 2) { $data = @($received[($offset+5)..($offset+$length+2)]) }
                return [pscustomobject]@{id=$MotorId; error=[int]$received[$offset+4]; data=$data}
            }
        } else { Start-Sleep -Milliseconds 1 }
    }
    return $null
}
$report = [ordered]@{timestamp=(Get-Date -Format o); safety='Only PING and READ; DTR/RTS disabled; no robot SDK, torque, goal, mode, calibration, reset or ID writes'; ports=@()}
foreach ($portName in $Ports) {
    $portReport = [ordered]@{port=$portName; motors=@(); probes=@(); error=$null}
    $serialPort = [System.IO.Ports.SerialPort]::new()
    $serialPort.PortName=$portName
    $serialPort.DtrEnable=$false
    $serialPort.RtsEnable=$false
    $serialPort.Handshake=[System.IO.Ports.Handshake]::None
    $serialPort.ReadTimeout=100
    $serialPort.WriteTimeout=200
    try {
        $serialPort.BaudRate=1000000
        $serialPort.Open()
        # Do not alter motor baud rates. Fallback changes the PC UART only.
        foreach ($baud in @(1000000,115200)) {
            $serialPort.BaudRate=$baud
            $responding=@()
            foreach ($motorId in 1..20) {
                $reply = Invoke-ReadOnlyQuery $serialPort $motorId 1
                if ($null -ne $reply) { $responding += $motorId }
            }
            $portReport.probes += [pscustomobject]@{baud=$baud; scanned_ids='1-20'; responding_ids=$responding}
            if ($responding.Count -gt 0) {
                foreach ($motorId in $responding) {
                    $registerReply=Invoke-ReadOnlyQuery $serialPort $motorId 2 ([byte[]]@(0,70))
                    $portReport.motors += [pscustomobject]@{id=$motorId; baud=$baud; register_start=0; registers=$registerReply; additional_ping=(Invoke-ReadOnlyQuery $serialPort $motorId 1)}
                }
                break
            }
        }
    } catch { $portReport.error=$_.Exception.Message }
    finally { if ($serialPort.IsOpen) { $serialPort.Close() }; $serialPort.Dispose() }
    $report.ports += [pscustomobject]$portReport
}
$json=$report | ConvertTo-Json -Depth 12
if ($OutputPath) { [IO.File]::WriteAllText($OutputPath,$json,[Text.UTF8Encoding]::new($false)) }
$json
