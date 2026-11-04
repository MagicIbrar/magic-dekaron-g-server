# Magic Dekaron G — server (proof of concept)

An experimental, hobby **proof of concept**: a community/private **server** for *Dekaron G*, the mobile MMO by Thumbage that
was discontinued and whose official servers are offline. It's a test to see whether the game could be stood back up on a
server you run yourself. It is early, incomplete, and provided as-is for people who want to tinker.

This repository is the **server side only**.

## What it is
- A server that speaks the game's login/world protocol: service-info, region list, accounts, and a running world (movement,
  chat, NPCs, monsters, items, shops, quests, clans, mail, a marketplace, instance dungeons, world bosses).
- A **web management panel** (players, accounts, character editing, inventory, gold, mail/gifts, logs, settings).
- A small **hub** so several community servers can share accounts and show up in one Region list.
- A **relay** so a server behind a home router can be reached without port-forwarding.
- A converter, `prepare_gamedata.py`, that builds the little bit of game data the server reads **from your own copy of the
  game** (the files the official client already downloaded to your device).

## What it is NOT
- It does **not** contain the game: no APK, no game code, no art, no sounds, no bundles, no game database. Those belong to
  Thumbage. You supply your own copy of the game.
- It does **not** include a game client or any way to obtain the game.
- It is not affiliated with, endorsed by, or connected to Thumbage or any rights holder.

## Quick start (host)
1. Install **Python 3.9+** and `pip install UnityPy`; have **openssl** available.
2. `cp server.config.example server.config` and edit it (defaults are fine for a local test).
3. Build the game data from your own game files: `python prepare_gamedata.py "<your client's AssetBundles folder>"`.
4. Start it: `Start-Server.bat` (Windows) or `sh start-server.sh` (Linux/macOS).

Full details are in **[HOST-GUIDE.md](HOST-GUIDE.md)**; the player side is in **[PLAYER-GUIDE.md](PLAYER-GUIDE.md)**.

## Status
Proof of concept. The server reaches live gameplay in testing, but expect rough edges and gaps. The data converter is
validated: every table the server reads is produced identically from the game bundles versus the development reference.

## Legal / disclaimer
*Dekaron G*, *Dekaron*, their trademarks and all game content are the property of Thumbage and their respective owners. This
project is an independent, non-commercial, fan-made experiment for preservation and learning. It ships none of the game's
files. Use only a copy of the game you are entitled to, and run it only where you are permitted to. You are responsible for
how you use this. See [NOTICE.md](NOTICE.md).

The code in this repository (the server, panel, relay and tools) is released under the MIT License — see [LICENSE](LICENSE).
That license covers **our code only**, not any game content, trademark, or protocol.
