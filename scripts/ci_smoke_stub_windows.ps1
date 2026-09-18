# Shared smoke test: stub launches runtime; wait for QApplication OK in boot logs.
# IMPORTANT: keep this file ASCII-only (Windows PowerShell 5.x + UTF-8 without BOM).

$ErrorActionPreference = "Stop"

$work = Join-Path $env:RUNNER_TEMP "hammer_smoke"
if (Test-Path $work) { Remove-Item $work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $work | Out-Null
Copy-Item "dist/HammerPackage/*" $work -Recurse -Force

$stubPath = Join-Path $work "HammerCandleBacktestDashboard.exe"
$runtimePath = Join-Path $work "app\HammerRuntime.exe"
if (-not (Test-Path $stubPath)) { throw "Smoke package missing stub at $stubPath" }
if (-not (Test-Path $runtimePath)) { throw "Smoke package missing runtime at $runtimePath" }

Unblock-File -LiteralPath $stubPath -ErrorAction SilentlyContinue
Unblock-File -LiteralPath $runtimePath -ErrorAction SilentlyContinue

Push-Location $work
try {
  $sys32 = Join-Path $env:SystemRoot "System32"
  $env:PATH = "$sys32;$env:SystemRoot"
  Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
  Remove-Item Env:HAMMER_INSTALL_ROOT -ErrorAction SilentlyContinue

  Start-Process -FilePath ".\HammerCandleBacktestDashboard.exe" -WorkingDirectory $work -WindowStyle Normal | Out-Null

  $deadline = (Get-Date).AddSeconds(120)
  $boot = ""
  $crash = ""

  function Read-BootLog {
    $chunks = @()
    foreach ($rel in @("logs\boot.log", "hammer_boot.log", "logs\stub.log")) {
      if (Test-Path $rel) {
        $chunks += Get-Content $rel -Raw -ErrorAction SilentlyContinue
      }
    }
    return ($chunks -join "`n")
  }
  function Read-CrashLog {
    foreach ($rel in @("logs\crash.log", "hammer_crash.log")) {
      if (Test-Path $rel) {
        return Get-Content $rel -Raw -ErrorAction SilentlyContinue
      }
    }
    return ""
  }

  while ((Get-Date) -lt $deadline) {
    $boot = Read-BootLog
    $crash = Read-CrashLog
    if ($boot -match "QApplication OK") { break }
    if ($crash -and $crash.Trim().Length -gt 0) { break }
    # Stub exits quickly; runtime must appear
    Start-Sleep -Seconds 2
  }

  Get-Process -Name "HammerRuntime","HammerCandleBacktestDashboard" -ErrorAction SilentlyContinue |
    Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1

  $boot = Read-BootLog
  $crash = Read-CrashLog
  Write-Host "==== boot / stub log ===="
  if ($boot) { Write-Host $boot } else { Write-Host "(missing)" }
  Write-Host "==== crash log ===="
  if ($crash) { Write-Host $crash } else { Write-Host "(none)" }

  if ($crash -and $crash.Trim().Length -gt 0) {
    throw "EXE wrote crash log - will not open on user PCs:`n$crash"
  }
  if (-not $boot) {
    throw "EXE never wrote boot log - stub/runtime died before Python (DLL/OpenSSL)."
  }
  if ($boot -notmatch "stub:") {
    Write-Warning "stub.log markers missing - stub may not have run (direct runtime?)"
  }
  if ($boot -notmatch "QApplication OK") {
    throw "boot log missing QApplication OK - stub/runtime failed:`n$boot"
  }
  Write-Host "Smoke OK - stub launched runtime; QApplication works."
}
finally {
  Pop-Location
}
