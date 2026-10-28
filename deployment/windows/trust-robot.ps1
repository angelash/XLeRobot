param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.:-]*$')][string]$RobotHost,
    [Parameter(Mandatory=$true)][ValidatePattern('^SHA256:[a-zA-Z0-9+/]+={0,2}$')][string]$Fingerprint,
    [ValidateRange(1,65535)][int]$Port=22,
    [string]$ProfileDirectory=(Join-Path $env:LOCALAPPDATA 'XLeRobotRemote')
)
$ErrorActionPreference = 'Stop'
$folder = $ProfileDirectory
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$temporary = Join-Path $folder ('host-'+[Guid]::NewGuid().ToString('N')+'.pub')
try {
    # Windows PowerShell treats keyscan's informational stderr banner as an error.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $scan = @(& ssh-keyscan.exe -T 8 -t ed25519 -p $Port $RobotHost 2>$null | Where-Object {$_ -match '^[^#].* ssh-ed25519 '})
    } finally { $ErrorActionPreference = $previousPreference }
    if (-not $scan) { throw 'No Ed25519 host key received; check SSH, IP and firewall.' }
    [System.IO.File]::WriteAllText($temporary, ($scan -join "`n")+"`n", (New-Object System.Text.UTF8Encoding($false)))
    $prints = @(& ssh-keygen.exe -lf $temporary)
    foreach ($line in $prints) {
        $actual = ($line -split '\s+')[1]
        if ($actual -ne $Fingerprint) { throw "Host fingerprint mismatch. Received $actual; no trust was saved." }
    }
    $known = Join-Path $folder 'known_hosts'
    $old = if (Test-Path -LiteralPath $known) {[System.IO.File]::ReadAllLines($known)} else {@()}
    [System.IO.File]::WriteAllText($known, ((@($old)+$scan | Select-Object -Unique) -join "`n")+"`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "Verified host fingerprint stored: $RobotHost / $Fingerprint"
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
