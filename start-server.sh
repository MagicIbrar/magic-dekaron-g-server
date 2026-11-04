#!/bin/sh
# Dekaron G server launcher (Linux / macOS). Reads server.config, makes a certificate if needed, checks the game data,
# and starts the server. First copy server.config.example to server.config and edit it.
cd "$(dirname "$0")"

# load server.config (KEY=VALUE, # comments) into the environment
if [ -f server.config ]; then
  while IFS= read -r line; do
    case "$line" in ''|\#*) continue;; esac
    key=${line%%=*}; val=${line#*=}
    [ -n "$key" ] && export "$key=$val"
  done < server.config
fi
HOST_IP="${HOST_IP:-10.0.2.2}"

# a TLS certificate for the login host
if [ ! -f certs/live-web.pem ]; then
  echo "No certificate yet - generating one (certs/)..."
  sh make-certs.sh
fi

# the game data the server reads
GDB_DIR="${STUB_GDB:-gamedata/gamedatabaseasset}"
if [ ! -d "$GDB_DIR" ]; then
  echo "ERROR: game data not found at $GDB_DIR"
  echo "Build it from your own copy of the game first:"
  echo "    python3 prepare_gamedata.py \"<path to the client's AssetBundles folder>\""
  exit 1
fi

echo "Starting Dekaron G server (advertise $HOST_IP, panel on 127.0.0.1:${STUB_ADMIN_PORT:-8010})..."
exec python3 stub-server.py "$HOST_IP"
