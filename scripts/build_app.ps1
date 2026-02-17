Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

$pythonBin = "python"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonBin = $venvPython
}

& $pythonBin -m pip install -r requirements.txt pyinstaller
& $pythonBin -m PyInstaller `
    packaging/launcher.py `
    --name SlackClaw `
    --onefile `
    --clean `
    --paths src `
    --hidden-import websocket

$arch = $env:PROCESSOR_ARCHITECTURE
if (-not $arch -and $env:PROCESSOR_ARCHITEW6432) {
    $arch = $env:PROCESSOR_ARCHITEW6432
}
$arch = ($arch | ForEach-Object { $_.ToLowerInvariant() })
switch ($arch) {
    "amd64" { $arch = "x64" }
    "x86_64" { $arch = "x64" }
    "arm64" { $arch = "arm64" }
    default {
        if (-not $arch) { $arch = "x64" }
    }
}

$releaseDir = Join-Path $repoRoot "release"
if (-not (Test-Path $releaseDir)) {
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
} else {
    Get-ChildItem -Path $releaseDir -Force | Remove-Item -Recurse -Force
}

$binPath = Join-Path $repoRoot "dist\SlackClaw.exe"
if (-not (Test-Path $binPath)) {
    throw "Expected binary not found: $binPath"
}

$releaseName = "SlackClaw-windows-$arch.exe"
$releasePath = Join-Path $releaseDir $releaseName
Copy-Item $binPath $releasePath -Force

Write-Host "Build complete."
Write-Host "Binary: $binPath"
Write-Host "Release file: $releasePath"
Write-Host "Run first-time setup with: .\release\$releaseName --setup"
