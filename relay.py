"""Dekaron G private-server relay: let a home-hosted server (behind NAT, no port-forwarding, no public IP) be reachable
by clients through the central hub, via an OUTBOUND reverse tunnel the host opens.

  HUB  (public):  python -I relay.py hub                 # on the central machine with a public IP/domain
  AGENT(at home): python -I relay.py agent <hub-host>    # next to the home Dekaron server

How it works:
  - The agent dials OUT to the hub control port (works through NAT; the host opens no inbound ports). It authenticates with
    the shared key and declares the local ports to expose (auth 17002 + game 17003 by default) and a server name.
  - The hub allocates a PUBLIC entry port per exposed port and keeps the agent's connection as a multiplexed tunnel.
  - A game client connects to hub:entry_port; the hub opens a logical stream over the tunnel; the agent connects to the home
    server's matching local port and pipes bytes both ways. Many client connections are multiplexed over the one tunnel.
  - The hub registers the server in the hub region list (POST /hub/register on the stub) with authAddress = <public>:<auth entry>.
    LAN clients can be given the server's LAN IP directly (see --lan) to skip the relay; the relay is the remote fallback.

Mux frame (both directions): u32 stream_id, u8 type (1=OPEN 2=DATA 3=CLOSE), u16 label_len/port, then payload.
Env: RELAY_KEY (shared secret, required), RELAY_CONTROL_PORT (7900), RELAY_PUBLIC_HOST (advertised host for the region list),
     RELAY_HUB_REGISTER (stub hub URL to register into, e.g. https://live-web.dkmgservice.com:8002), RELAY_HUB_KEY.
"""
import asyncio, os, struct, sys, json, ssl, urllib.request

KEY = os.environ.get('RELAY_KEY', '')
CONTROL_PORT = int(os.environ.get('RELAY_CONTROL_PORT', '7900'))
PUBLIC_HOST = os.environ.get('RELAY_PUBLIC_HOST', '127.0.0.1')
HUB_REGISTER = os.environ.get('RELAY_HUB_REGISTER', '')
HUB_KEY = os.environ.get('RELAY_HUB_KEY', '')
OPEN, DATA, CLOSE = 1, 2, 3


def log(*a): print('[relay]', *a, flush=True)


async def read_frame(r):
    hdr = await r.readexactly(7)
    sid, typ, n = struct.unpack('<IBH', hdr)
    payload = await r.readexactly(n) if n else b''
    return sid, typ, payload


def frame(sid, typ, payload=b''):
    return struct.pack('<IBH', sid, typ, len(payload)) + payload


# ---------------------------------------------------------------- HUB ----
class Tunnel:
    def __init__(self, w):
        self.w = w; self.streams = {}; self.lock = asyncio.Lock()
    async def send(self, sid, typ, payload=b''):
        async with self.lock:
            self.w.write(frame(sid, typ, payload)); await self.w.drain()


async def hub_entry_handler(tun, label):
    """Returns a callback serving one public entry port: each client connection becomes a mux stream."""
    counter = [0]
    async def handle(creader, cwriter):
        counter[0] += 1; sid = counter[0] << 8 | hash(label) & 0xff
        tun.streams[sid] = cwriter
        peer = cwriter.get_extra_info('peername')
        log('client', peer, '->', label, 'stream', sid)
        await tun.send(sid, OPEN, label.encode())
        try:
            while True:
                data = await creader.read(65536)
                if not data: break
                await tun.send(sid, DATA, data)
        except Exception:
            pass
        finally:
            await tun.send(sid, CLOSE)
            tun.streams.pop(sid, None)
            cwriter.close()
    return handle


async def hub_control(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        line = (await reader.readline()).decode().strip()
        hello = json.loads(line)
    except Exception as e:
        log('bad agent hello from', peer, e); writer.close(); return
    if not KEY or hello.get('key') != KEY:
        log('agent auth failed', peer); writer.write(b'{"error":1}\n'); await writer.drain(); writer.close(); return
    name = str(hello.get('name', 'server'))[:32]
    labels = hello.get('ports', {'auth': 17002, 'game': 17003})  # label -> local port (informational to agent)
    pinned = hello.get('entry', {})   # label -> desired public entry port (for a stable server.csv); else 0 = auto
    tun = Tunnel(writer)
    servers = []
    assigned = {}
    for label in labels:
        want = int(pinned.get(label, 0))
        srv = await asyncio.start_server(await hub_entry_handler(tun, label), '0.0.0.0', want)
        port = srv.sockets[0].getsockname()[1]; assigned[label] = port; servers.append(srv)
        log('server', name, 'label', label, '-> public entry port', port)
    writer.write((json.dumps({'error': 0, 'entry': assigned, 'host': PUBLIC_HOST}) + '\n').encode()); await writer.drain()
    # keep the server in the hub region list while the tunnel lives (re-register every 30s; the hub TTL expires stale ones)
    hb = None
    if HUB_REGISTER and 'auth' in assigned:
        async def heartbeat():
            ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            first = True
            while True:
                try:
                    body = json.dumps({'name': name, 'authAddress': '%s:%d' % (PUBLIC_HOST, assigned['auth']), 'key': HUB_KEY}).encode()
                    req = urllib.request.Request(HUB_REGISTER.rstrip('/') + '/hub/register', data=body, headers={'Content-Type': 'application/json'}, method='POST')
                    await asyncio.get_event_loop().run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10, context=ctx).read())
                    if first: log('registered', name, 'in hub region list (heartbeat 30s)'); first = False
                except Exception as e:
                    log('hub register failed:', e)
                await asyncio.sleep(30)
        hb = asyncio.ensure_future(heartbeat())
    # pump frames from the agent back to the matching client streams
    try:
        while True:
            sid, typ, payload = await read_frame(reader)
            cw = tun.streams.get(sid)
            if typ == DATA and cw:
                cw.write(payload); await cw.drain()
            elif typ == CLOSE and cw:
                cw.close(); tun.streams.pop(sid, None)
    except Exception as e:
        log('tunnel closed for', name, e)
    finally:
        for srv in servers: srv.close()
        if hb: hb.cancel()


async def run_hub():
    assert KEY, 'set RELAY_KEY'
    s = await asyncio.start_server(hub_control, '0.0.0.0', CONTROL_PORT)
    log('hub control on :%d, public host %s%s' % (CONTROL_PORT, PUBLIC_HOST, ', registering into ' + HUB_REGISTER if HUB_REGISTER else ''))
    async with s: await s.serve_forever()


# -------------------------------------------------------------- AGENT ----
async def agent_pump(tun_w, lock, sid, lr, lw, streams):
    try:
        while True:
            data = await lr.read(65536)
            if not data: break
            async with lock: tun_w.write(frame(sid, DATA, data)); await tun_w.drain()
    except Exception:
        pass
    finally:
        async with lock: tun_w.write(frame(sid, CLOSE)); await tun_w.drain()
        streams.pop(sid, None); lw.close()


async def run_agent(hub_host):
    assert KEY, 'set RELAY_KEY'
    name = os.environ.get('RELAY_SERVER_NAME', 'HomeServer')
    ports = {'auth': int(os.environ.get('RELAY_AUTH_PORT', '17002')), 'game': int(os.environ.get('RELAY_GAME_PORT', '17003'))}
    entry = {}
    if os.environ.get('RELAY_AUTH_ENTRY'): entry['auth'] = int(os.environ['RELAY_AUTH_ENTRY'])
    if os.environ.get('RELAY_GAME_ENTRY'): entry['game'] = int(os.environ['RELAY_GAME_ENTRY'])
    r, w = await asyncio.open_connection(hub_host, CONTROL_PORT)
    w.write((json.dumps({'key': KEY, 'name': name, 'ports': ports, 'entry': entry}) + '\n').encode()); await w.drain()
    resp = json.loads((await r.readline()).decode())
    if resp.get('error'): log('hub rejected:', resp); return
    log('connected; public entries:', resp.get('entry'), 'host', resp.get('host'))
    lock = asyncio.Lock(); streams = {}
    while True:
        sid, typ, payload = await read_frame(r)
        if typ == OPEN:
            label = payload.decode(); lp = ports.get(label)
            if not lp:
                async with lock: w.write(frame(sid, CLOSE)); await w.drain()
                continue
            try:
                lr, lw = await asyncio.open_connection('127.0.0.1', lp)   # open BEFORE reading more frames (no DATA race)
            except Exception as e:
                log('agent: cannot reach local', label, lp, e)
                async with lock: w.write(frame(sid, CLOSE)); await w.drain()
                continue
            streams[sid] = lw
            asyncio.ensure_future(agent_pump(w, lock, sid, lr, lw, streams))
        elif typ == DATA:
            lw = streams.get(sid)
            if lw: lw.write(payload); await lw.drain()
        elif typ == CLOSE:
            lw = streams.get(sid)
            if lw: lw.close(); streams.pop(sid, None)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'hub': asyncio.run(run_hub())
    elif mode == 'agent': asyncio.run(run_agent(sys.argv[2] if len(sys.argv) > 2 else '127.0.0.1'))
    else: print(__doc__)


if __name__ == '__main__':
    main()
