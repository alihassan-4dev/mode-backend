Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$keyFile = Join-Path $scriptDir "localhost-key.pem"
$certFile = Join-Path $scriptDir "localhost.pem"
$targetPort = 8000

function Test-CommandAvailable {
    param([Parameter(Mandatory = $true)][string]$CommandName)
    return $null -ne (Get-Command $CommandName -ErrorAction SilentlyContinue)
}

if (-not (Test-CommandAvailable -CommandName "uv")) {
    Write-Error "uv is not installed or not on PATH. Install uv first, then run again."
}

if ((-not (Test-Path $keyFile)) -or (-not (Test-Path $certFile))) {
    Write-Host "SSL certificate files not found. Generating localhost certs..." -ForegroundColor Yellow
    if (-not (Test-CommandAvailable -CommandName "mkcert")) {
        Write-Error "mkcert is required to generate localhost certs. Install mkcert, then run again."
    }
    mkcert -install
    mkcert localhost
}

# If port is already bound, avoid a confusing WinError and provide a clean message.
$existingListener = Get-NetTCPConnection -LocalPort $targetPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existingListener) {
    $ownerPid = $existingListener.OwningProcess
    $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
    $procName = if ($proc) { $proc.ProcessName } else { "unknown" }
    Write-Host "Port $targetPort is already in use by PID $ownerPid ($procName)." -ForegroundColor Yellow
    Write-Host "Backend may already be running at https://localhost:$targetPort" -ForegroundColor Yellow
    Write-Host "If you want to restart it, stop that process first and run this command again." -ForegroundColor Yellow
    return
}

Write-Host ""
Write-Host "Starting backend with HTTPS on https://localhost:8000" -ForegroundColor Green
Write-Host "Using certificate: $certFile"
Write-Host "Using key:         $keyFile"
Write-Host ""

uv run uvicorn main:app --host localhost --port $targetPort --reload --ssl-keyfile=localhost-key.pem --ssl-certfile=localhost.pem
