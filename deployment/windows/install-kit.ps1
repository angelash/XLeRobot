param([string]$Destination=(Join-Path $env:USERPROFILE 'Robot\XLeRobot'), [switch]$Apply)
$ErrorActionPreference = 'Stop'
$kit = $PSScriptRoot
if (-not $Apply) { Write-Output "PLAN: verify kit SHA-256 manifest, extract source to $Destination, then install core dependencies. Pass -Apply."; exit }
$manifest = Get-Content -LiteralPath (Join-Path $kit 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($file in $manifest.files) {
    $path = [System.IO.Path]::GetFullPath((Join-Path $kit $file.path))
    $prefix = $kit.TrimEnd('\') + '\'
    if (-not $path.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escapes the kit directory.' }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) { throw "Checksum mismatch: $($file.path)" }
}
if (Test-Path -LiteralPath $Destination) { throw 'Destination already exists. Choose an empty directory; no existing robot install will be overwritten.' }
Expand-Archive -LiteralPath (Join-Path $kit 'project.zip') -DestinationPath $Destination
& (Join-Path $Destination 'deployment\windows\bootstrap.ps1') -KitRoot $kit -Apply
Write-Output 'Source and core installed. Historical calibration/library remain in runtime-reference.zip; no robot service was started.'
