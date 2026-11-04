# Dekaron G — player guide (private servers)

Play **Dekaron G** on a private or community server instead of the (now offline) official one. You keep using the real game
client; a small patch points it at a server you choose, and the server's Region screen lists the servers that are online.

> **You need your own copy of the game.** This project does not give you the game. You supply the APK and the game's
> downloaded files yourself — they are Thumbage's. The patch kit only edits an APK you already have.

## What you need
- The **Dekaron G APK** (the version these tools target) on your PC.
- An Android device or emulator (e.g. BlueStacks) where you can install apps and add a trusted certificate.
- The server's details from whoever runs it (or run your own — see the host guide).

## How connecting works
1. **The client patch** (from the APK patch kit) changes the game so it talks to the server you choose and logs in by your
   device identity — no third-party account sign-in.
2. **Trust the server's certificate.** The login step uses HTTPS, so your device must trust the server's CA (the host gives
   you a `*.0` file; install it with the steps in `install-ca.sh` / the host guide).
3. **Point your device at the server.** Either the host runs a public server with a real domain, or you add a hosts entry /
   local DNS so the game's login host resolves to the server's address. The host guide covers both.
4. **Pick a server.** Launch the game — it logs in by device and shows the **Region** screen listing every server that is
   online. Choose one and play. One account can work across all servers on the same network (if the host set that up).

## Local vs public servers
- A **public** server just works once your device trusts its certificate and can reach its address.
- A **local** server (your own PC, or a friend's) needs the device to reach that machine: same LAN, a hosts entry, or the
  host's relay if they're behind a home router.

## Updates
For now the project keeps the client patch current with the game. If the game updates and your client stops connecting, get
the newer patch kit from the project's GitHub releases. The long-term goal is to remove the need to track the official
update servers at all.

## Fair play
Use only a copy of the game you are entitled to, and a server you are allowed to play on. Respect each server owner's rules —
it's their world.
