#Requires -Version 5.1
<#
.SYNOPSIS
  Show which backend is on the port and whether it is this workspace.
#>
param([int]$Port = 8001)

$RepoRoot = Split-Path -Parent $PSScriptRoot
$AgentRoot = (Resolve-Path (Join-Path $RepoRoot "flow-agent")).Path
$marker = $AgentRoot -replace '/', '\'

Write-Host "Expected workspace: $AgentRoot" -ForegroundColor Cyan
Write-Host ""

$pids = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique
if (-not $pids) {
    Write-Host "Port ${Port}: nothing listening" -ForegroundColor Yellow
    exit 1
}

foreach ($procId in $pids) {
    $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$procId").CommandLine
    $fromCmd = $cmd -and (($cmd -replace '/', '\').IndexOf($marker, [StringComparison]::OrdinalIgnoreCase) -ge 0)
    Write-Host "PID $procId  cmdline_has_workspace_path=$fromCmd"
    Write-Host "  $cmd"
}

Write-Host ""
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 8
    $health | ConvertTo-Json -Compress
    if ($health.code_root) {
        $got = ($health.code_root -replace '/', '\')
        $ok = $got.Equals($marker, [StringComparison]::OrdinalIgnoreCase)
        Write-Host ""
        if ($ok) {
            Write-Host "OK: /health.code_root matches this workspace" -ForegroundColor Green
            Write-Host "    transport=$($health.transport) extension_connected=$($health.extension_connected)" -ForegroundColor DarkGray
            exit 0
        } else {
            Write-Host "WARN: /health.code_root is NOT this workspace" -ForegroundColor Red
            Write-Host "  got:  $($health.code_root)"
            Write-Host "  want: $AgentRoot"
            Write-Host "Stop foreign backend:  .\scripts\dev-serve.ps1 -StopOnly -ForceKillPort" -ForegroundColor Yellow
            exit 2
        }
    } else {
        Write-Host ""
        Write-Host "WARN: no code_root in /health — likely OLD backend (global flow.exe)" -ForegroundColor Red
        Write-Host "Stop it then start workspace code:" -ForegroundColor Yellow
        Write-Host "  .\scripts\dev-serve.ps1 -ForceKillPort"
        exit 2
    }
} catch {
    Write-Host "health request failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
