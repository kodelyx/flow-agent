# ============================================================
#  Flow Agent — Windows Setup Script
#  Run this in PowerShell:
#      .\setup-windows.ps1
# ============================================================

$ErrorActionPreference = "Stop"

Write-Host "▶ 1/2 Locating Flow Agent Executable" -ForegroundColor Cyan

# 1. Try to find flow on PATH (from python/uv tool)
$FlowBin = Get-Command flow.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source

# 2. Check local directory for prebuilt binaries
if (-not $FlowBin) {
    if (Test-Path "$PSScriptRoot\flow-cli-windows.exe") {
        $FlowBin = Resolve-Path "$PSScriptRoot\flow-cli-windows.exe"
    } elseif (Test-Path "$PSScriptRoot\flow-agent\dist\flow-cli-windows.exe") {
        $FlowBin = Resolve-Path "$PSScriptRoot\flow-agent\dist\flow-cli-windows.exe"
    }
}

if (-not $FlowBin) {
    Write-Host "× Could not find flow.exe on PATH or flow-cli-windows.exe in the current directory." -ForegroundColor Red
    Write-Host "Please download flow-cli-windows.exe and place it in the same folder as this script, or install the CLI via uv." -ForegroundColor Yellow
    Exit 1
}

Write-Host "✔ Found Flow Agent: $FlowBin" -ForegroundColor Green

Write-Host "`n▶ 2/2 Setting up auto-start on login" -ForegroundColor Cyan

$StartupFolder = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
$ShortcutPath = [System.IO.Path]::Combine($StartupFolder, "flow-agent.lnk")

# Create Shortcut
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $FlowBin
$Shortcut.Arguments = "serve"
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Save()

Write-Host "✔ Shortcut created in Startup folder: $ShortcutPath" -ForegroundColor Green
Write-Host "🎉 All done! Flow Agent will now start automatically in the background whenever you log in." -ForegroundColor Green
Write-Host "To test it right now, you can run:" -ForegroundColor Yellow
Write-Host "    & '$FlowBin' serve" -ForegroundColor Yellow
