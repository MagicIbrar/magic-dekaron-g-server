#!/bin/sh
# Dekaron G server: generate the TLS certificate the game client expects for the login host.
# The client talks HTTPS to live-web.dkmgservice.com (and the service-info + CDN hosts). This makes a local CA and a leaf
# certificate for those names, which players install as a trusted CA on their device (see install-ca.sh). Run once.
#   sh make-certs.sh                 # default names (localhost testing / BlueStacks: IP 10.0.2.2)
#   HOST_IP=203.0.113.10 EXTRA_DNS=play.example.com sh make-certs.sh    # add your public IP / domain
set -e
export MSYS_NO_PATHCONV=1   # Git Bash on Windows would otherwise turn /CN=... into a path
cd "$(dirname "$0")/certs" 2>/dev/null || { mkdir -p certs; cd certs; }
HOST_IP="${HOST_IP:-10.0.2.2}"
SANS="DNS:live-web.dkmgservice.com,DNS:cdn-gb.dkmgservice.com,DNS:7m4l13gkx9.execute-api.ap-northeast-2.amazonaws.com,IP:$HOST_IP"
[ -n "$EXTRA_DNS" ] && SANS="$SANS,DNS:$EXTRA_DNS"
[ -n "$EXTRA_IP" ] && SANS="$SANS,IP:$EXTRA_IP"

if [ ! -f ca.pem ]; then
  openssl genrsa -out ca.key 2048
  openssl req -x509 -new -nodes -key ca.key -sha256 -days 3650 -subj "/CN=Dekaron G Stub CA" -out ca.pem
  echo "made a new CA (ca.pem)"
fi
printf "subjectAltName=%s\nextendedKeyUsage=serverAuth\n" "$SANS" > ext.cnf
openssl genrsa -out live-web.key 2048
openssl req -new -key live-web.key -subj "/CN=live-web.dkmgservice.com" -out live-web.csr
openssl x509 -req -in live-web.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 3650 -sha256 -extfile ext.cnf -out live-web.pem
# Android reads user CAs by <subject_hash>.0
HASH=$(openssl x509 -in ca.pem -noout -subject_hash)
cp ca.pem "$HASH.0"
echo "certificate ready: certs/live-web.pem (SAN: $SANS)"
echo "players install certs/$HASH.0 as a trusted CA (see install-ca.sh)"
