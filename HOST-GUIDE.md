# Dekaron G server — host guide

This is a community server for **Dekaron G** (the mobile game by Thumbage). It lets you run your own world that the game
client connects to. You can run it just for yourself on your PC, or make it public so other people can play on it.

> **What this package is and isn't.** This is *our* server code only. It does **not** contain any of the game's own files —
> no APK, no art, no bundles, no game database. The game's content belongs to Thumbage. The server builds the small amount of
> game data it needs from **your own copy** of the game (the files the official client already downloaded to your device). You
> are responsible for having a legitimate copy of the game.

## What you need
- **Python 3.9+** (`python` / `python3` on your PATH).
- **UnityPy** for the one-time data step: `pip install UnityPy`.
- **openssl** for the certificate (Linux/macOS have it; on Windows it ships with Git for Windows).
- Your own copy of the game's **AssetBundles** folder (see "Game data" below).

## First run
1. **Copy the config:** copy `server.config.example` to `server.config` and edit it. For a local test the defaults are fine.
   To go public, set `HOST_IP` to your public IP or domain.
2. **Build the game data** from your own client files (once, and again whenever the game updates):
   ```
   python prepare_gamedata.py "<path to the client's AssetBundles folder>"
   ```
   This writes `gamedata/` next to the server. It only rewrites files you already have; nothing is downloaded or shared.
3. **Start the server:**
   - Windows: double-click `Start-Server.bat` (or `powershell -File start-server.ps1`)
   - Linux/macOS: `sh start-server.sh`
   The first start makes the TLS certificate automatically. You'll see `stub up: http :17001 auth :17002 game :17003` and
   `admin panel on http://127.0.0.1:8010`.

## How it works
- The client asks a **login host** (TLS, port 8002) for a service-info reply, then a **region list**, then connects over TCP
  to the **auth** port (17002) and the **game** port (17003). This server answers all of that.
- **Content** (the 2.6 GB of bundles and updates) is **not** served by you by default — `STUB_CDN_ROOT` points at Thumbage's
  CDN, which is still online. You can point it at your own mirror later if you want.
- **Accounts** live in `stub.sqlite` (created on first run). A device/guest login makes an account automatically; players can
  set a password later. Manage accounts from the panel or `python accounts.py`.

## The management panel
`http://127.0.0.1:8010` (localhost only — reach it over SSH or a tunnel, never expose the port publicly). The first admin
password is written to `PANEL-FIRST-LOGIN.txt` on first start; sign in, change it under Settings, delete that file. The panel
does: live players (broadcast, kick), accounts (ban, role, password, delete), **character management** (level, stats, rename,
revive, teleport, delete), inventory and gold, mail/gifts to one player or everyone, the community-server list, logs, settings.

## Local vs public
- **Local / LAN:** keep `HOST_IP=10.0.2.2` for an emulator on the same PC, or your LAN IP for other devices on your network.
- **Public:** set `HOST_IP` to your public IP/domain and open the three TCP ports (17002, 17003) and the login port (8002).
- **Behind NAT with no port-forwarding?** Use the **relay** (`relay.py`). A relay hub with a public address accepts an outbound
  tunnel from your home server, so players reach you without any router setup. See the comments at the top of `relay.py`; set
  `STUB_ADVERTISE_GAME_PORT` to the relay's game entry port in `server.config`.

## A network of servers (optional)
One central **hub** can hold the base accounts, and other people run their own **community servers** that authenticate players
against your hub, so one account works everywhere and every online server shows up in the client's Region screen.
- On the hub: set `STUB_HUB_AUTH_SECRET` (a long shared secret) and `STUB_IS_HUB=1`.
- On each community server: set the **same** `STUB_HUB_AUTH_SECRET`, plus `STUB_HUB_AUTH_URL` (the hub's URL) and
  `STUB_HUB_URL` + `STUB_HUB_KEY` to register into the hub's region list.
- Leave all of that blank to run a single standalone server with its own accounts.

## Publishing content edits
The server reads its game data from `gamedata/`. You can edit those `.asset` files (drop rates, shop lists, spawns, text) and
restart to change your world. Distributing *edited game content* to clients is a separate thing with its own rules — see the
project notes; by default clients use Thumbage's unmodified content.

## Keeping it updated
The game client version is tied to the content. For now, the project keeps the client build current; watch the project's
GitHub releases for a new server + client patch when the game updates. Re-run `prepare_gamedata.py` after any content update.
