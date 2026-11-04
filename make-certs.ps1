# Dekaron G server: generate the TLS certificate the client expects for the login host (Windows; needs openssl on PATH,
# e.g. from Git for Windows). Players install the resulting CA on their device. Run once.
#   powershell -File make-certs.ps1
#   $env:HOST_IP='203.0.113.10'; $env:EXTRA_DNS='play.example.com'; powershell -File make-certs.ps1
$ErrorActionPreference = 'Stop'
$certs = Join-Path $PSScriptRoot 'certs'
New-Item -ItemType Directory -Force -Path $certs | Out-Null
Push-Location $certs
try {
  $hostIp = if ($env:HOST_IP) { $env:HOST_IP } else { '10.0.2.2' }
  $sans = "DNS:live-web.dkmgservice.com,DNS:cdn-gb.dkmgservice.com,DNS:7m4l13gkx9.execute-api.ap-northeast-2.amazonaws.com,IP:$hostIp"
  if ($env:EXTRA_DNS) { $sans += ",DNS:$($env:EXTRA_DNS)" }
  if ($env:EXTRA_IP) { $sans += ",IP:$($env:EXTRA_IP)" }
  if (-not (Test-Path ca.pem)) {
    & openssl genrsa -out ca.key 2048
    & openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -subj "/CN=Dekaron G Stub CA" -out ca.pem
    Write-Host "made a new CA (ca.pem)"
  }
  "subjectAltName=$sans`nextendedKeyUsage=serverAuth" | Out-File -Encoding ascii ext.cnf
  & openssl genrsa -out live-web.key 2048
  & openssl req -new -key live-web.key -subj "/CN=live-web.dkmgservice.com" -out live-web.csr
  & openssl x509 -req -in live-web.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 3650 -sha256 -extfile ext.cnf -out live-web.pem
  $hash = (& openssl x509 -in ca.pem -noout -subject_hash).Trim()
  Copy-Item ca.pem "$hash.0" -Force
  Write-Host "certificate ready: certs/live-web.pem (SAN: $sans)"
  Write-Host "players install certs/$hash.0 as a trusted CA (see install-ca.sh)"
} finally { Pop-Location }
