# ============================================================
#  Flow Agent — Windows Uninstall Script
#  Run this in PowerShell:
#      .\uninstall-windows.ps1
# ============================================================

$ErrorActionPreference = "Stop"

$StartupFolder = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
$ShortcutPath = [System.IO.Path]::Combine($StartupFolder, "flow-agent.lnk")

if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
    Write-Host "✔ Auto-start shortcut removed. Flow Agent will no longer start on login." -ForegroundColor Green
} else {
    Write-Host "! Auto-start shortcut was not found. Nothing to remove." -ForegroundColor Yellow
}
