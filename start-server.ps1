# Dekaron G server launcher (Windows). Reads server.config, makes a certificate if needed, checks the game data, and
# starts the server. First copy server.config.example to server.config and edit it.
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (Test-Path server.config) {
  foreach ($line in Get-Content server.config) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $k, $v = $line -split '=', 2
    if ($k.Trim()) { Set-Item -Path "Env:$($k.Trim())" -Value $v }
  }
}
$hostIp = if ($env:HOST_IP) { $env:HOST_IP } else { '10.0.2.2' }

if (-not (Test-Path certs/live-web.pem)) {
  Write-Host "No certificate yet - generating one (certs/)..."
  & powershell -ExecutionPolicy Bypass -File make-certs.ps1
}

$gdb = if ($env:STUB_GDB) { $env:STUB_GDB } else { 'gamedata/gamedatabaseasset' }
if (-not (Test-Path $gdb)) {
  Write-Host "ERROR: game data not found at $gdb" -ForegroundColor Red
  Write-Host "Build it from your own copy of the game first:"
  Write-Host "    python prepare_gamedata.py `"<path to the client's AssetBundles folder>`""
  exit 1
}

$port = if ($env:STUB_ADMIN_PORT) { $env:STUB_ADMIN_PORT } else { '8010' }
Write-Host "Starting Dekaron G server (advertise $hostIp, panel on 127.0.0.1:$port)..."
& python stub-server.py $hostIp
