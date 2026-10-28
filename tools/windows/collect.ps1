# Collect ALVR experiment telemetry into captures\<timestamp>\ (client + server).
# Run from this folder after an experiment session:
#     powershell -ExecutionPolicy Bypass -File collect.ps1
#
# Only sessions not collected before are pulled/copied (tracked in captures\.collected.txt).

$ErrorActionPreference = "Stop"

$root = $PSScriptRoot
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$out = Join-Path $root "captures\$stamp"
$clientOut = Join-Path $out "client"
$serverOut = Join-Path $out "server"
New-Item -ItemType Directory -Force -Path $clientOut, $serverOut | Out-Null

$adb = Join-Path $root "platform-tools\adb.exe"
if (-not (Test-Path $adb)) { $adb = "adb" }

$collectedFile = Join-Path $root "captures\.collected.txt"
if (-not (Test-Path $collectedFile)) { New-Item -ItemType File -Force -Path $collectedFile | Out-Null }
$known = @(Get-Content $collectedFile | Where-Object { $_ -ne "" })

$remote = "/sdcard/Android/data/alvr.client.dev/files/alvr_telemetry"
Write-Host "Checking connected Quest 3..."
$devices = & $adb devices | Select-String "device$"
if (-not $devices) {
    Write-Host "WARNING: no adb device found; skipping client telemetry pull"
} else {
    $dirs = & $adb shell "ls $remote" 2>$null
    foreach ($dir in $dirs) {
        $dir = $dir.Trim()
        if ($dir -notmatch "^session_") { continue }
        if ($known -contains "client:$dir") { continue }
        Write-Host "Pulling client session $dir ..."
        & $adb pull "$remote/$dir" $clientOut | Out-Null
        Add-Content $collectedFile "client:$dir"
    }
}

$serverDir = Join-Path $root "alvr_telemetry"
if (Test-Path $serverDir) {
    Get-ChildItem $serverDir -Directory | ForEach-Object {
        $name = $_.Name
        if ($known -contains "server:$name") { return }
        Write-Host "Copying server session $name ..."
        Copy-Item $_.FullName (Join-Path $serverOut $name) -Recurse
        Add-Content $collectedFile "server:$name"
    }
}

Write-Host ""
Write-Host "Collected into: $out"
Write-Host "Reboot to Linux when convenient; analysis reads this from the mounted folder."
