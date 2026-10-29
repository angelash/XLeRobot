param(
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-zA-Z0-9][a-zA-Z0-9.:-]*$')][string]$RobotHost,
    [Parameter(Mandatory=$true)][ValidatePattern('^SHA256:[a-zA-Z0-9+/]+={0,2}$')][string]$Fingerprint,
    [ValidateRange(1,65535)][int]$Port=22,
    [string]$ProfileDirectory=(Join-Path $env:LOCALAPPDATA 'XLeRobotRemote'),
    [string]$KeyScanExecutable
)
$ErrorActionPreference = 'Stop'
$folder = $ProfileDirectory
New-Item -ItemType Directory -Path $folder -Force | Out-Null
$temporary = Join-Path $folder ('host-'+[Guid]::NewGuid().ToString('N')+'.pub')
try {
    $candidates = @()
    if ($KeyScanExecutable) { $candidates += (Get-Command $KeyScanExecutable -ErrorAction Stop).Source }
    else {
        $candidates += (Get-Command ssh-keyscan.exe -ErrorAction Stop).Source
        # Windows 9.5 keyscan can select an unsupported hybrid KEX with OpenSSH 10.
        # A newer Git for Windows scanner may read the key; fingerprint checks stay mandatory.
        $gitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
        if ($gitCommand) {
            $gitRoot = Split-Path -Parent (Split-Path -Parent $gitCommand.Source)
            $gitScanner = Join-Path $gitRoot 'usr\bin\ssh-keyscan.exe'
            if (Test-Path -LiteralPath $gitScanner) { $candidates += $gitScanner }
        }
    }
    $scan = @()
    foreach ($scanner in @($candidates | Select-Object -Unique)) {
        # Windows PowerShell treats keyscan's informational stderr banner as an error.
        $previousPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $scan = @(& $scanner -T 8 -t ed25519 -p $Port $RobotHost 2>$null | Where-Object {$_ -match '^\S+ ssh-ed25519 [A-Za-z0-9+/=]+(?: .*)?$'})
            $scanExitCode = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previousPreference }
        if ($scanExitCode -eq 0 -and $scan.Count -gt 0) {
            Write-Output "Ed25519 host key read using: $scanner"
            break
        }
        $scan = @()
    }
    if (-not $scan) { throw 'No Ed25519 host key received. Check SSH/IP/firewall; Windows 9.5 keyscan may require a newer Git for Windows scanner via -KeyScanExecutable.' }
    [System.IO.File]::WriteAllText($temporary, ($scan -join "`n")+"`n", (New-Object System.Text.UTF8Encoding($false)))
    $prints = @(& ssh-keygen.exe -lf $temporary)
    if ($LASTEXITCODE -ne 0 -or $prints.Count -eq 0) { throw 'Host fingerprint calculation failed; no trust was saved.' }
    foreach ($line in $prints) {
        $actual = ($line -split '\s+')[1]
        if ($actual -ne $Fingerprint) { throw "Host fingerprint mismatch. Received $actual; no trust was saved." }
    }
    $known = Join-Path $folder 'known_hosts'
    $old = if (Test-Path -LiteralPath $known) {[System.IO.File]::ReadAllLines($known)} else {@()}
    [System.IO.File]::WriteAllText($known, ((@($old)+$scan | Select-Object -Unique) -join "`n")+"`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Output "Verified host fingerprint stored: $RobotHost / $Fingerprint"
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
