# Assemble stub launcher package for Windows CI.
# Expects:
#   dist/HammerCandleBacktestDashboard.exe  (tiny stub)
#   dist/HammerRuntime.exe                  (full app)
# Writes:
#   dist/HammerPackage/...
#   dist/Hammer-windows.zip
#   dist/release-assets/...  (optional bridge copies)
#
# IMPORTANT: keep this file ASCII-only. Windows PowerShell 5.x mis-parses
# UTF-8 em-dashes/arrows in .ps1 scripts (shows as aEURoe / ParserError).

$ErrorActionPreference = "Stop"

$runtime = "dist/HammerRuntime.exe"
$stub = "dist/HammerCandleBacktestDashboard.exe"
if (-not (Test-Path $runtime)) { throw "Missing $runtime - build HammerCandleBacktestDashboard.spec first" }
if (-not (Test-Path $stub)) { throw "Missing $stub - build HammerStub.spec first" }

$rtSize = (Get-Item $runtime).Length
$stubSize = (Get-Item $stub).Length
Write-Host "Runtime $([math]::Round($rtSize/1MB,1)) MB; stub $([math]::Round($stubSize/1MB,1)) MB ($stubSize bytes)"

if ($rtSize -lt 400MB) {
  throw "Runtime too small ($([math]::Round($rtSize/1MB,1)) MB) - expected ~420-450 MB"
}
if ($rtSize -lt 420MB) {
  Write-Warning "Runtime is $([math]::Round($rtSize/1MB,1)) MB (target ~430 MB)"
}
# PyInstaller one-file stub is usually ~5-25 MB, never hundreds
if ($stubSize -lt 2MB) { throw "Stub too small ($stubSize bytes) - build likely failed" }
if ($stubSize -ge 80MB) {
  throw "Stub looks like full app ($([math]::Round($stubSize/1MB,1)) MB) - expected tiny launcher. Check build order / names."
}

$pkg = "dist/HammerPackage"
if (Test-Path $pkg) { Remove-Item $pkg -Recurse -Force }
New-Item -ItemType Directory -Force -Path "$pkg/app" | Out-Null
Copy-Item -LiteralPath $stub -Destination "$pkg/HammerCandleBacktestDashboard.exe" -Force
Copy-Item -LiteralPath $runtime -Destination "$pkg/app/HammerRuntime.exe" -Force

@"
Hammer (Windows) - stub launcher layout

1. Keep this folder together (launcher exe + app\HammerRuntime.exe).
2. Double-click HammerCandleBacktestDashboard.exe (the launcher).
3. Put your data/ folder next to the launcher (same folder as this README).

Check for Updates stages a new runtime under app\ and restarts via the launcher.

Old one-file installs: Check for Updates downloads the large bridge EXE once;
on first open it auto-converts to this stub + app\ layout.
"@ | Set-Content -Encoding ascii "$pkg/README.txt"

$zip = "dist/Hammer-windows.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }

# Compress-Archive is unreliable for ~400MB+ trees; use .NET ZipFile instead
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory(
  (Resolve-Path $pkg).Path,
  (Join-Path (Resolve-Path "dist").Path "Hammer-windows.zip")
)

$zipSize = (Get-Item $zip).Length
Write-Host "Wrote $zip ($([math]::Round($zipSize/1MB,1)) MB)"
if ($zipSize -lt 400MB) { throw "Hammer-windows.zip too small ($([math]::Round($zipSize/1MB,1)) MB)" }

# Release / artifact bridge copies
$assets = "dist/release-assets"
if (Test-Path $assets) { Remove-Item $assets -Recurse -Force }
New-Item -ItemType Directory -Force -Path $assets | Out-Null
Copy-Item -LiteralPath $zip -Destination "$assets/Hammer-windows.zip" -Force
Copy-Item -LiteralPath $runtime -Destination "$assets/HammerRuntime.exe" -Force
# Older clients (pre-stub updater) only accept this exact large filename
Copy-Item -LiteralPath $runtime -Destination "$assets/HammerCandleBacktestDashboard.exe" -Force

Write-Host "Package OK -> dist/HammerPackage + dist/Hammer-windows.zip + dist/release-assets/"
