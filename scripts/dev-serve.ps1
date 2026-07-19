#Requires -Version 5.1
<#
.SYNOPSIS
  Start Flow Agent backend from THIS workspace source (never global flow.exe).

.DESCRIPTION
  Stops any process listening on the target port if it is NOT from this workspace,
  then launches:  python -m flow_cli serve  from the workspace flow-agent/ directory.

.EXAMPLE
  .\scripts\dev-serve.ps1
  .\scripts\dev-serve.ps1 -Port 8001 -Reload
  .\scripts\dev-serve.ps1 -StopOnly
  .\scripts\dev-serve.ps1 -ForceKillPort
#>
param(
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8001,
    [switch]$Reload,
    [switch]$StopOnly,
    [switch]$ForceKillPort
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$AgentRoot = Join-Path $RepoRoot "flow-agent"
$WorkspaceMarker = (Resolve-Path $AgentRoot).Path

function Get-ListenersOnPort([int]$PortNum) {
    Get-NetTCPConnection -LocalPort $PortNum -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Get-ProcessCommandLine([int]$ProcessId) {
    try {
        (Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop).CommandLine
    } catch {
        $null
    }
}


function Test-HealthIsWorkspace([int]$PortNum) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$PortNum/health" -TimeoutSec 3
        if (-not $h.code_root) { return $false }
        $got = ($h.code_root -replace '/', '\')
        $marker = $WorkspaceMarker -replace '/', '\'
        return $got.Equals($marker, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}
function Test-IsWorkspaceBackend([string]$CommandLine) {
    if (-not $CommandLine) { return $false }
    $norm = $CommandLine -replace '/', '\'
    $marker = $WorkspaceMarker -replace '/', '\'
    return $norm.IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

Write-Host ""
Write-Host "Flow Agent — workspace backend" -ForegroundColor Cyan
Write-Host "  workspace: $WorkspaceMarker"
Write-Host "  target:    http://${HostAddress}:${Port}"
Write-Host ""

$pids = @(Get-ListenersOnPort $Port)
foreach ($procId in $pids) {
    $cmd = Get-ProcessCommandLine $procId
    $fromHere = (Test-IsWorkspaceBackend $cmd) -or (Test-HealthIsWorkspace $Port)
    if ($fromHere -and -not $ForceKillPort -and -not $StopOnly) {
        Write-Host "Already running from this workspace (PID $procId)" -ForegroundColor Green
        Write-Host "  $cmd"
        Write-Host "Use -ForceKillPort to restart, or -StopOnly to stop." -ForegroundColor Yellow
        exit 0
    }
    if (-not $fromHere -or $ForceKillPort -or $StopOnly) {
        $label = if ($fromHere) { "workspace" } else { "FOREIGN/old" }
        Write-Host "Stopping $label backend PID $procId" -ForegroundColor Yellow
        if ($cmd) { Write-Host "  $cmd" -ForegroundColor DarkGray }
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Milliseconds 600
$left = @(Get-ListenersOnPort $Port)
if ($left.Count -gt 0) {
    Write-Host "Port $Port still in use by: $($left -join ', ')" -ForegroundColor Red
    exit 1
}

if ($StopOnly) {
    Write-Host "Port $Port is free." -ForegroundColor Green
    exit 0
}

Push-Location $AgentRoot
try {
    $env:PYTHONPATH = $AgentRoot
    $env:OPENAI_API_HOST = $HostAddress
    $env:OPENAI_API_PORT = "$Port"
    $py = (Get-Command python -ErrorAction Stop).Source
    Write-Host "Starting with:" -ForegroundColor Cyan
    $reloadNote = if ($Reload) { " --reload" } else { "" }
    Write-Host "  $py -m flow_cli serve --host $HostAddress --port $Port$reloadNote"
    Write-Host "  PYTHONPATH=$AgentRoot"
    Write-Host ""

    & $py -c @"
import sys
sys.path.insert(0, r'$AgentRoot')
import cli.api, omniflash.bridge
print('code_root:', r'$AgentRoot')
print('cli.api:  ', cli.api.__file__)
print('bridge:   ', omniflash.bridge.__file__)
print('python:   ', sys.executable)
"@
    if ($LASTEXITCODE -ne 0) { throw "Failed to import workspace packages" }

    $argList = @("-m", "flow_cli", "serve", "--host", $HostAddress, "--port", "$Port")
    if ($Reload) { $argList += "--reload" }
    & $py @argList
} finally {
    Pop-Location
}

