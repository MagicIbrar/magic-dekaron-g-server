# Dekaron G stub servers: logs everything the real client sends, answers only what is needed to move it forward.
#   HTTP :50241  (the client's LoginServerIP)  GET /RegionList?memberId=...  GET /Login?memberId=...&token=...
#   TCP  :50242  auth server (authAddress in the region list)     TCP :50243  game server (serverGameIP:port)
# Usage: python stub-server.py [host-ip-as-seen-by-the-client]   (default 10.0.2.2 = BlueStacks' host)
# Log: stub.log next to this file (hex dumps of every TCP byte + every HTTP request).
import asyncio, json, struct, sys, time, os, ssl, re, random, base64, hmac, hashlib, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import autoreply   # zero-filled default answers from protocol/protocol.json
import stubdb      # accounts + passwords + game state in tools/stub/stub.sqlite
import clandb      # clan tables + error codes (attached to stubdb.Db)
import tradedb     # marketplace listings (attached to stubdb.Db)
from urllib.parse import urlsplit, parse_qs, unquote

HOST_IP = sys.argv[1] if len(sys.argv) > 1 else '10.0.2.2'
HTTP_PORT, AUTH_PORT, GAME_PORT = 17001, 17002, 17003
HTTPS_PORT = 8002   # the real login host's port (hosts file maps live-web.dkmgservice.com to the PC)
ADV_GAME_HOST = os.environ.get('STUB_ADVERTISE_GAME_HOST', '')   # advertise a different game-server address (e.g. a relay entry)
ADV_GAME_PORT = int(os.environ.get('STUB_ADVERTISE_GAME_PORT', '0'))
# ---- hub: service info + region list of every online community server (2026-11-04) ---------------------------------
# The client asks get_service_info_v2 (the original AWS endpoint, hosts-mapped here or patched in the APK) for the login
# host (Url "host|flag|flag") and the content CDN root (ext); bundles are ext + "/AssetBundles/" + name. The region list
# then shows one row per server: this stub itself plus every server that registered (POST /hub/register with the shared
# key) and sent a heartbeat within HUB_TTL seconds. The region screen therefore lists only servers that are online.
CDN_ROOT = os.environ.get('STUB_CDN_ROOT', 'https://cdn-gb.dkmgservice.com/AndroidW_0.1.356')
CDN_DIR = os.environ.get('STUB_CDN_DIR', '')          # when set: serve <CDN_DIR>/<file> at https://<this host>:CDN_PORT/<prefix>/AssetBundles/<file>
CDN_PORT = int(os.environ.get('STUB_CDN_PORT', '443'))
HUB_KEY = os.environ.get('STUB_HUB_KEY', '')          # shared secret of the register call; empty = registration off
HUB_TTL = int(os.environ.get('STUB_HUB_TTL', '90'))
REGION_NAME = os.environ.get('STUB_REGION_NAME', 'Global@Stub')
HUB_SERVERS = {}   # name -> {'authAddress', 'seen', 'players'}
HUB_URL = os.environ.get('STUB_HUB_URL', '')          # e.g. https://hub.example.com:8002 : this server then appears in that hub's region list while it runs
HUB_INSECURE = os.environ.get('STUB_HUB_INSECURE', '0') == '1'   # accept the hub's self-signed test certificate

# ---- hub account authority (2026-11-04): the base accounts live on the HUB; a player's identity is one account that works
# on every community server. Tokens are HMAC-signed, so any server sharing STUB_HUB_AUTH_SECRET validates them offline (no
# per-login call home). A community server with STUB_HUB_AUTH_URL delegates an unknown login to the hub to mint the account.
# Set STUB_HUB_AUTH_SECRET on the hub AND on every community server in the network; set STUB_IS_HUB=1 on the hub only. -----
HUB_AUTH_SECRET = os.environ.get('STUB_HUB_AUTH_SECRET', '')     # shared across the whole network; empty = standalone (local accounts only, today's behaviour)
IS_HUB = os.environ.get('STUB_IS_HUB', '0') == '1'              # this instance is the central account authority (serves /hub/auth, /hub/validate, /account)
HUB_AUTH_URL = os.environ.get('STUB_HUB_AUTH_URL', '')          # a community server's hub base URL to delegate logins to (e.g. https://hub.example.com:8002)
HUB_TOKEN_TTL = int(os.environ.get('STUB_HUB_TOKEN_TTL', '86400'))   # s a hub token stays valid
DEEPLINK = os.environ.get('STUB_DEEPLINK', 'magicdekarong://login')  # the scheme the web account page returns the token to the APK on
ADMIN_PORT = int(os.environ.get('STUB_ADMIN_PORT', '8010'))     # the management panel's port (0 = off)
ADMIN_IP = os.environ.get('STUB_ADMIN_IP', '127.0.0.1')        # bind the panel to localhost by default (open it over SSH or a tunnel, never the public internet)


def hub_sign(login, account_no, exp=None):
    """A compact signed token: b64url(login).accountNo.exp.hmac  — anyone with HUB_AUTH_SECRET can verify it offline."""
    exp = int(exp or time.time() + HUB_TOKEN_TTL)
    b = base64.urlsafe_b64encode(login.encode()).decode().rstrip('=')
    msg = '%s.%d.%d' % (b, account_no, exp)
    return msg + '.' + hmac.new(HUB_AUTH_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]


def hub_verify(token):
    """(login, accountNo) if `token` is a valid, unexpired hub token signed with our secret, else None."""
    if not HUB_AUTH_SECRET or not token or token.count('.') != 3:
        return None
    b, no, exp, sig = token.split('.')
    good = hmac.new(HUB_AUTH_SECRET.encode(), ('%s.%s.%s' % (b, no, exp)).encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(good, sig):
        return None
    try:
        exp, no = int(exp), int(no)
    except ValueError:
        return None
    if exp < time.time():
        return None
    try:
        login = base64.urlsafe_b64decode(b + '=' * (-len(b) % 4)).decode('utf-8')
    except Exception:
        return None
    return login, no


async def hub_heartbeat_loop():
    import urllib.request
    ctx = ssl.create_default_context()
    if HUB_INSECURE: ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    while True:
        try:
            players = len([x for x in SESSIONS.values() if x.get('hero')])
            body = json.dumps({'name': REGION_NAME, 'authAddress': '%s:%d' % (HOST_IP, AUTH_PORT), 'key': HUB_KEY, 'players': players}).encode()
            req = urllib.request.Request(HUB_URL.rstrip('/') + '/hub/register', data=body, headers={'Content-Type': 'application/json'}, method='POST')
            r = await asyncio.get_event_loop().run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10, context=ctx).read())
            if b'"error": 0' not in r: log('[hub] registration refused: %s' % r[:100])
        except Exception as e:
            log('[hub] heartbeat failed: %r' % e)
        await asyncio.sleep(30)

LOG = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'stub.log'), 'a', encoding='utf-8')


def log(*a):
    s = time.strftime('%H:%M:%S ') + ' '.join(str(x) for x in a)
    print(s, flush=True); LOG.write(s + '\n'); LOG.flush()


def hexdump(b, limit=512):
    b = b[:limit]
    return ' '.join('%02x' % x for x in b) + (' ...' if len(b) == limit else '') + '   |' + ''.join(chr(x) if 32 <= x < 127 else '.' for x in b) + '|'


# ---- encoding helpers (from PROTOCOL.md: little endian, string = u16 len + utf8, bool = 1 byte) ----
def w_str(s):
    b = s.encode('utf-8'); return struct.pack('<H', len(b)) + b


def w_i8(v): return struct.pack('<B', v & 0xFF)
def w_i16(v): return struct.pack('<h', v)
def w_i32(v): return struct.pack('<i', v)
def w_i64(v): return struct.pack('<q', v)
def w_bool(v): return b'\x01' if v else b'\x00'

def w_u32(v): return struct.pack('<I', v & 0xFFFFFFFF)
def w_u16(v): return struct.pack('<H', v & 0xFFFF)
def w_f32(v): return struct.pack('<f', v)
def w_f64(v): return struct.pack('<d', v)

# Server clock (HDateTime.SyncTime): BaseTime = new DateTime(serverBaseTimeTicks, Utc); now = BaseTime + serverTick ms.
# So serverBaseTimeTicks = the server's start time in .NET ticks and serverTick = milliseconds since that start.
SERVER_START = time.time()
def server_tick(): return int((time.time() - SERVER_START) * 1000)
def base_ticks(): return int(SERVER_START * 10000000) + 621355968000000000
def w_dt(sec=None):
    # DateTime (AWriter.WriteDateTime / AReader.ReadDateTime): int64 seconds since 1970 UTC + int32 sub-second part (100 ns units / 100)
    return w_i64(int(time.time() if sec is None else sec)) + w_i32(0)
def w_arr(items): return w_i32(len(items)) + b''.join(items)       # struct arrays: int32 count
def w_parr(data, size=4): return w_i16(len(data) // size) + data   # primitive arrays: int16 ELEMENT count (verified with SN_Leave uint[]:
                                                                     # count 1 + one uint; a byte count made the client read garbage)

# world the stub puts every hero into: Ardeca town (scene 9001001, MapDangerType Safe), a portal end position
MAP_INDEX = int(os.environ.get('STUB_MAP', '9001001'))
SPAWN = (-125.66, 8.78, -45.89)
AREA_TYPE = int(os.environ.get('STUB_AREA', '0'))   # E_AreaType: 0 Battle, 1 Peace (no fighting)
WEAPON = int(os.environ.get('STUB_WEAPON', '111001'))   # herocustomize.asset: class 910101 default weapon


def hero_info(h):
    # HeroInfo: heroNo, baseIndex, heroIndex, level, exp, name, ShapeInfo{weapon int, enchant byte, pet int, class int, areaType byte, wing int},
    # propensityPoint, clanNo, clanName, loginTime, logoutMapIndex, isDeleteWaiting, deleteWaitingEndTime, Title
    shape = w_i32(WEAPON) + w_i8(0) + w_i32(0) + w_i32(h["index"]) + w_i8(AREA_TYPE) + w_i32(0)   # ShapeInfo: weapon, enchant, pet, class, areaType, wing
    return (w_i64(h['no']) + w_u32(h['index']) + w_u32(h['index']) + w_u32(h['level']) + w_i64(0) + w_str(h['name']) + shape
            + w_i32(0) + clan_mini_blob(h['no'])[:-3] + w_dt() + w_i32(MAP_INDEX) + w_bool(False) + w_dt(0) + w_i32(0))   # clanNo + clanName (the mini blob without mark / bool)


# --- world population from the exported game tables (read only) -------------------------------------------------
GDB = os.environ.get('STUB_GDB') or next((p for p in (os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gamedata', 'gamedatabaseasset'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'export', 'ExportedProject', 'Assets', 'gamedatabaseasset')) if os.path.isdir(p)),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gamedata', 'gamedatabaseasset'))   # STUB_GDB, else ./gamedata (prepare_gamedata.py), else the dev export
NPC_BASE, MON_BASE = 1000, 2000          # member index ranges; the hero is 1
OTHER_BASE = 50000                       # other players as seen by a client: OTHER_BASE + their session id (every client is member 1 to itself)
SESSIONS = {}                            # session id -> {'writer', 'cipher', 'map', 'h' (hero record), 'hero' (live state), 'name'}
SESSION_SEQ = [0]


def peer_member(sid):
    return OTHER_BASE + sid


def hero_frames(cipher, member, h, pos):
    """SN_Enter hero 0x91111 + SN_Shape 0x91102 + SN_Status 0x91103 + Stand for hero record h at `member` (as another client sees it)."""
    x, y, z = pos
    out = frame(0x91111, w_u32(member) + w_i32(h['index']) + w_i64(h['no']) + w_str(h['name']) + w_i32(h['level'])
                + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_i8(1), cipher=cipher)
    shape = w_i32(WEAPON) + w_i8(0) + w_i32(0) + w_i32(h['index']) + w_i8(AREA_TYPE) + w_i32(0)
    rel = w_i8(1) + w_i32(1) + w_i8(0) + w_i8(4) + w_i64(h['no']) + w_bool(False) + w_bool(False) + w_bool(True) + w_i8(1)
    clan = clan_mini_blob(h['no'])
    out += frame(0x91102, w_u32(member) + w_i32(h['index']) + shape + rel + clan + w_bool(False) + w_i8(0) + w_i32(0) + w_i8(6), cipher=cipher)
    out += frame(0x91103, w_u32(member) + w_i32(0) + w_i32(0), cipher=cipher)
    stand = w_i8(1) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_u32(member)
    out += frame(0x92101, w_i32(4 + len(stand)) + stand, cipher=cipher)
    return out


def peers_of(sid):
    me = SESSIONS.get(sid)
    if not me:
        return []
    return [(osid, o) for osid, o in SESSIONS.items() if osid != sid and o['map'] == me['map'] and not o['writer'].is_closing()]


def send_peers(sid, build):
    """build(peer_cipher, my_member_as_seen_by_peer) -> bytes, written to every other client in my map."""
    for osid, o in peers_of(sid):
        try:
            data = build(o['cipher'], peer_member(sid))
            if data:
                o['writer'].write(data)
        except Exception as e:
            log('[multi] send to session %d failed: %r' % (osid, e))


def leave_peers(sid):
    """SN_Leave 0x92105 { byte Normal; uint[] members } for me to every client in my map, then unregister."""
    if sid in SESSIONS:
        send_peers(sid, lambda c, m: frame(0x92105, w_i8(0) + w_parr(w_u32(m), 4), cipher=c))
        del SESSIONS[sid]


PARTIES = {}                             # partyIndex -> {'master': sid, 'members': [sid], 'limit', 'loot'}
PARTY_SEQ = [100]


def party_member_info(sid, slot, is_master):
    """PartyMemberInfo { byte slot; long userNo; int heroIndex; string nick; byte isMaster; PartyMemberSyncInfo { long charNo; long serverKey;
    int memberIndex; int mapId; Position; short dir; int hp, maxHP, mp, maxMP; long mapObj; int[] statuses; ExtendedShapeInfo { byte } } }"""
    o = SESSIONS.get(sid)
    if not o:
        return b''
    h, hero = o['h'], o['hero']
    x, y, z = hero['pos']
    sync = (w_i64(h['no']) + w_i64(1) + w_i32(peer_member(sid)) + w_i32(o['map']) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0)
            + w_i32(hero['hp']) + w_i32(hero['maxhp']) + w_i32(hero['mp']) + w_i32(500) + w_i64(0) + w_parr(b'', 4) + w_i8(0))
    return w_i8(slot) + w_i64(h['no']) + w_i32(h['index']) + w_str(h['name']) + w_i8(1 if is_master else 0) + sync


def party_invite_info(sid):
    """PartyInviteInfo { long charNo; string nick; int heroIndex; ExtendedShapeInfo { byte } }"""
    o = SESSIONS.get(sid)
    if not o:
        return w_i64(0) + w_str('') + w_i32(0) + w_i8(0)
    return w_i64(o['h']['no']) + w_str(o['h']['name']) + w_i32(o['h']['index']) + w_i8(0)


def party_of(sid):
    for pi, pt in PARTIES.items():
        if sid in pt['members']:
            return pi, pt
    return None, None


def party_members_blob(pt):
    master = pt['master']
    return w_arr([party_member_info(m, i, m == master) for i, m in enumerate(pt['members'])])


def send_session(sid, code, body):
    o = SESSIONS.get(sid)
    if o and not o['writer'].is_closing():
        o['writer'].write(frame(code, body, cipher=o['cipher']))


# ---- clans (NClan 0xB01xx): DB.clans / clanners / clan_applications; members online = SESSIONS whose hero number is a clanner --
def clan_of_hero(hero_no):
    """(clan row, clanner row) of a hero, or (None, None)."""
    m = DB.clanner(hero_no)
    return (DB.clan(m['clan_no']), m) if m else (None, None)


def clan_blob(c):
    # ClanInfo { long clanNo; string clanName; long masterHeroNo; string masterHeroName; short memberLimit; short memberCount; long allianceNo;
    #            int clanLevel; long clanExp; int joinType; short mark; string introduce; string notice; bool succeedMasterTime }
    if c is None:
        return w_i64(0) + w_str('') + w_i64(0) + w_str('') + w_i16(0) + w_i16(0) + w_i64(0) + w_i32(0) + w_i64(0) + w_i32(0) + w_i16(0) + w_str('') + w_str('') + w_bool(False)
    return (w_i64(c['no']) + w_str(c['name']) + w_i64(c['master_no']) + w_str(c['master_name']) + w_i16(c['member_limit']) + w_i16(DB.clan_member_count(c['no']))
            + w_i64(0) + w_i32(c['level']) + w_i64(c['exp']) + w_i32(c['join_type']) + w_i16(c['mark']) + w_str(c['introduce']) + w_str(c['notice']) + w_bool(False))


def clanner_blob(m):
    # ClannerInfo { long clanNo; long heroNo; string nickName; int level; long entryServerKey (0 = offline); int clannerGrade; long accumulateContribution;
    #               long giveContribution; string message; DateTime attendanceTime; DateTime lastLoginTime; DateTime joinTime; DateTime giveDateTime; int giveCount }
    online = any(o['h'].get('no') == m['hero_no'] and not o['writer'].is_closing() for o in SESSIONS.values())
    return (w_i64(m['clan_no']) + w_i64(m['hero_no']) + w_str(m['name']) + w_i32(m['level']) + w_i64(1 if online else 0) + w_i32(m['grade']) + w_i64(m['contribution'])
            + w_i64(0) + w_str('') + w_dt(m['joined']) + w_dt(m['last_login'] or m['joined']) + w_dt(m['joined']) + w_dt(m['joined']) + w_i32(0))


def clan_mini_blob(hero_no):
    """The ClanInfo_mini of SN_Shape / HeroInfo's clan fields: { long clanNo; string clanName; short mark; bool }."""
    c, m = clan_of_hero(hero_no)
    return (w_i64(c['no']) + w_str(c['name']) + w_i16(c['mark']) + w_bool(m['grade'] == clandb.GRADE_LEADER)) if c else (w_i64(0) + w_str('') + w_i16(0) + w_bool(False))


def clan_sessions(clan_no, exclude_sid=None, officers_only=False):
    out = []
    for osid, o in list(SESSIONS.items()):
        if osid == exclude_sid or o['writer'].is_closing():
            continue
        m = DB.clanner(o['h'].get('no', 0))
        if m and m['clan_no'] == clan_no and (not officers_only or m['grade'] != clandb.GRADE_GENERAL):
            out.append(osid)
    return out


def send_clan(clan_no, code, body, exclude_sid=None, officers_only=False):
    for osid in clan_sessions(clan_no, exclude_sid, officers_only):
        send_session(osid, code, body)


def clan_reshape(hero_no):
    """SN_Shape of an online hero to its whole map (the clan name / mark / master flag changed)."""
    for osid, o in list(SESSIONS.items()):
        hh = o['h']
        if hh.get('no') != hero_no or o['writer'].is_closing():
            continue
        shape = w_i32(WEAPON) + w_i8(0) + w_i32(0) + w_i32(hh['index']) + w_i8(AREA_TYPE) + w_i32(0)
        rel = w_i8(1) + w_i32(1) + w_i8(0) + w_i8(4) + w_i64(hh['no']) + w_bool(False) + w_bool(False) + w_bool(True) + w_i8(1)
        clan = clan_mini_blob(hero_no)
        broadcast(o['map'], lambda ci, v, _s=osid: frame(0x91102, w_u32(member_as_seen(_s, v)) + w_i32(hh['index']) + shape + rel + clan + w_bool(False) + w_i8(0) + w_i32(0) + w_i8(6), cipher=ci))


def application_blob(a):
    # Application { long clanNo; long heroNo; long userNo; string heroName; int level; DateTime dateTime }
    return w_i64(a['clan_no']) + w_i64(a['hero_no']) + w_i64(a['hero_no']) + w_str(a['name']) + w_i32(a['level']) + w_dt(a['at'])


# ---- marketplace (NTrading 0xD0xxx): tradedb.py rows; prices in gold (account goods 2) ----------------------------------
def _item_blobs(enchant):
    """enchant + the four empty ability / socket parts shared by every trading struct."""
    return w_i8(enchant) + w_i32(0) + w_i8(0) + w_i32(0) + w_i8(0) + w_i32(0)


def sell_regist_blob(l):
    # SellRegistItem { long SellWaitNo; long ItemNo; int ItemIndex; int ItemCount; byte EnchantLevel; AbilityInfo[]; byte; AbilityInfo[]; byte; SocketSlotInfo[];
    #                  int SellPrice; SellRegistFlag(byte) Flag; DateTime TradingStartTime; DateTime ExpireTime }
    if l is None:
        return w_i64(0) + w_i64(0) + w_i32(0) + w_i32(0) + _item_blobs(0) + w_i32(0) + w_i8(0) + w_dt(0) + w_dt(0)
    return (w_i64(l['no']) + w_i64(l['serial']) + w_i32(l['item_index']) + w_i32(l['count']) + _item_blobs(l['enchant'])
            + w_i32(l['price']) + w_i8({0: 0, 1: 1, 2: 1, 3: 2}[l['flag']]) + w_dt(l['started']) + w_dt(l['expires']))


def trade_detail_blob(l):
    # TradeItemDetail { long TradeNo; int ItemIndex; int ItemCount; byte EnchantLevel; ...; int SellPrice; double AUnitPrice; DateTime ExpireTime }
    return w_i64(l['no']) + w_i32(l['item_index']) + w_i32(l['count']) + _item_blobs(l['enchant']) + w_i32(l['price']) + w_f64(l['price'] / max(1, l['count'])) + w_dt(l['expires'])


def complete_blob(l):
    # CompleteItem / TradeSellItemHistory { (long CompleteNo;) int ItemIndex; int ItemCount; byte EnchantLevel; ...; int SellPrice; int AdjustedPrice; DateTime IptTime }
    return w_i32(l['item_index']) + w_i32(l['count']) + _item_blobs(l['enchant']) + w_i32(l['price']) + w_i32(l['price'] - l['price'] * tradedb.FEE_PERCENT // 100) + w_dt(l['sold_at'] or l['started'])


def system_line(sid, text):
    """A System chat line (type 32) to one session."""
    send_session(sid, 0x91802, w_i32(32) + w_i8(0) + w_i64(0) + w_str('Market') + w_str(text) + w_i32(0) + w_i32(0) + w_i8(1) + w_i32(0) + w_i8(0) + w_i64(int(time.time() * 1000)))


def party_leave(sid, kicked=False):
    """Remove sid from its party: SN_LeavePartyNotify { long leaveCharNo; long nextMasterCharNo } to the rest (or the party dissolves)."""
    pi, pt = party_of(sid)
    if not pt:
        return
    pt['members'].remove(sid)
    me = SESSIONS.get(sid)
    char_no = me['h']['no'] if me else 0
    if len(pt['members']) < 1:
        del PARTIES[pi]
        return
    if pt['master'] == sid:
        pt['master'] = pt['members'][0]
    nxt = SESSIONS[pt['master']]['h']['no'] if pt['master'] in SESSIONS else 0
    for m in pt['members']:
        send_session(m, 0x93833, w_i64(char_no) + w_i64(nxt))
    if len(pt['members']) == 1:
        send_session(pt['members'][0], 0x93833, w_i64(char_no) + w_i64(nxt))


async def party_sync_loop():
    """SN_UpdateMemberInfo { PartyMemberInfo[] } to every party member every 2 s (positions, HP)."""
    while True:
        await asyncio.sleep(2)
        for pi, pt in list(PARTIES.items()):
            blob = party_members_blob(pt)
            for m in pt['members']:
                send_session(m, 0x93861, blob)


# ---- friends + letters (per account in save.json: friends / invited / letters) -----------------------------------------------
import zlib


def fid(hero_name):
    """A stable id for a hero name (save.json hero numbers are per account, so names identify players across accounts)."""
    return (zlib.crc32(hero_name.encode('utf-8')) & 0x7FFFFFFF) + 1000


def find_hero(hero_name):
    """(member, hero record) of a hero called hero_name: an online one first (names repeat across test accounts), then the store."""
    for osid, o in SESSIONS.items():
        if o['h'].get('name') == hero_name and o.get('name') in STORE and not o['writer'].is_closing():
            return o['name'], o['h']
    for member, acc in STORE.items():
        for h in acc.get('heroes', []):
            if h.get('name') == hero_name:
                return member, h
    return None, None


def session_of_hero(hero_name):
    for osid, o in SESSIONS.items():
        if o['h'].get('name') == hero_name and not o['writer'].is_closing():
            return osid
    return None


def friend_base(hero_name):
    return w_i64(fid(hero_name)) + w_str(hero_name) + w_dt()


def block_info(hero_name):
    # BlockInfo { long userNo; long heroNo; string heroName; byte gunIndex }
    return w_i64(fid(hero_name)) + w_i64(fid(hero_name)) + w_str(hero_name) + w_i8(1)


def hostile_info(hero_name):
    # HostileInfo { long userNo; long heroNo; string heroName; string clanName; byte gunIndex }
    return w_i64(fid(hero_name)) + w_i64(fid(hero_name)) + w_str(hero_name) + w_str('') + w_i8(1)


def mailbox_info(m):
    # MailboxInfo { long no; byte E_GameAsset rewardType (1 Item, 13 AccountGoods); int rewardIndex; int rewardValue; int rewardSubValue; string title; string msg; byte E_MailType (1 System); DateTime ipt; DateTime expire }
    return (w_i64(m['no']) + w_i8(m.get('type', 1)) + w_i32(m.get('index', 0)) + w_i32(m.get('value', 1)) + w_i32(m.get('sub', 0))
            + w_str(m['title']) + w_str(m['msg']) + w_i8(1) + w_dt(m.get('time', 0)) + w_dt(m.get('time', 0) + 30 * 86400))


def seed_mails(account):
    """The welcome mails every account gets once: 5 HP potions and 1,000 gold (the system mailbox, E_MailType System)."""
    if account.get('mails_seeded'):
        return
    now = int(time.time())
    account['mails'] = account.get('mails', []) + [
        {'no': now * 10 + 1, 'type': 1, 'index': 401001, 'value': 5, 'sub': 0, 'title': 'Welcome to Ardeca', 'msg': 'Take these potions, you will need them. Press Receive to put them in your bag.', 'time': now},
        {'no': now * 10 + 2, 'type': 13, 'index': 2, 'value': 1000, 'sub': 0, 'title': 'A small allowance', 'msg': 'The guild sends 1,000 gold for your first equipment.', 'time': now},
    ]
    account['mails_seeded'] = True; save_store(STORE)


def receive_mail(cipher, account, hero, name, mail):
    """Hands the mail's reward out: Item -> bag (SN_AddItem), AccountGoods -> goods update. Returns the extra frames."""
    out = b''
    if mail.get('type', 1) == 1 and mail.get('index'):
        b = bag_add(account, mail['index'], max(1, mail.get('value', 1)))
        out += frame(0xA0413, w_arr([add_item(b['serial'], b['index'], b['count'])]) + w_i32(0) + w_i64(0), cipher=cipher)   # SN_AddItem
    elif mail.get('type') == 13:
        gt = mail.get('index', 2)
        if gt == 2:
            account['gold'] = account.get('gold', 0) + mail.get('value', 0)
            out += goods_update(cipher, 2, mail.get('value', 0), account['gold'])
    account['mails'] = [m for m in account.get('mails', []) if m['no'] != mail['no']]
    save_store(STORE)
    log('[%s] mail %d received: %r' % (name, mail['no'], (mail.get('type'), mail.get('index'), mail.get('value'))))
    return out


def letter_info(l):
    # LetterInfo { long no; string sender; DateTime ipt; DateTime expire; byte read; string title; string msg; byte E_MailType }
    return w_i64(l['no']) + w_str(l['from']) + w_dt(l.get('time', 0)) + w_dt(l.get('time', 0) + 30 * 86400) + w_i8(1 if l.get('read') else 0) + w_str(l['title']) + w_str(l['msg']) + w_i8(0)


def rewrite_member(blob, member):
    """An ActionBase blob (length-prefixed) with its actorMemberIndex (offset 27) replaced."""
    b = bytearray(blob)
    if len(b) >= 31:
        struct.pack_into('<I', b, 27, member)
    return bytes(b)
MON_PER_RECT = int(os.environ.get('STUB_MON_PER_RECT', '3'))


def _yaml_floats(block, key):
    return [float(m) for m in re.findall(r'\b%s: (-?[\d.eE+-]+)' % key, block)]


def load_npcs(map_index):
    """[(npcIndex, x, y, z, dir)] from staticnpc/Npc<map>.asset (first RandPos of every placement)."""
    f = os.path.join(GDB, 'staticnpc', 'Npc%d.asset' % map_index)
    if not os.path.exists(f):
        return []
    out = []
    for blk in re.split(r'\n  - NPCIndex: ', open(f, encoding='utf-8').read())[1:]:
        idx = int(blk.split('\n', 1)[0])
        xs, ys, zs, ds = (_yaml_floats(blk, k) for k in ('X', 'Y', 'Z', 'Direction'))
        if xs:
            out.append((idx, xs[0], ys[0], zs[0], int(ds[0]) if ds else 0))
    return out


def load_monsters(map_index, ground_y):
    """[(monsterIndex, x, y, z)]: MON_PER_RECT monsters spread over every spawn rectangle of mapmonsterinfo_<map>.asset."""
    f = os.path.join(GDB, 'mapmonsterinfo', 'mapmonsterinfo_%d.asset' % map_index)
    if not os.path.exists(f):
        return []
    out = []
    import random
    rnd = random.Random(map_index)
    for blk in re.split(r'\n  - MonsterIndex: ', open(f, encoding='utf-8').read())[1:]:
        idx = int(blk.split('\n', 1)[0])
        l, t, r, b = (_yaml_floats(blk, k)[:1] or [0] for k in ('Left', 'Top', 'Right', 'Bottom'))
        for _ in range(MON_PER_RECT):
            out.append((idx, rnd.uniform(l[0], r[0]), ground_y, rnd.uniform(b[0], t[0])))
    return out


def monster_hp(idx):
    f = os.path.join(GDB, 'monster', 'monster_%d.asset' % idx)
    if os.path.exists(f):
        pairs = re.findall(r'- OptionCode: (\d+)\r?\n    Value: (-?\d+)', open(f, encoding='utf-8').read())
        opts = dict((int(a), int(b)) for a, b in pairs)
        if opts.get(24, 0) > 0:
            return opts[24]          # E_StatOptionAll.HPMax = 24 (10 for the Ardeca town demons)
    return 1000


def action_stand(member, x, y, z, d=0):
    blob = w_i8(1) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(d) + w_u32(member)
    return w_i32(4 + len(blob)) + blob


def relation(group, key, propensity=4, danger=0):
    # RelationSource: byte group; int mapType (1 StaticMap); byte danger; byte propensity; long key; Nullable<long> party; Nullable<long> clan; Nullable<byte> gun
    return w_i8(group) + w_i32(1) + w_i8(danger) + w_i8(propensity) + w_i64(key) + w_bool(False) + w_bool(False) + w_bool(False)


# ---- the shared world: one monster table + one AI loop per map, seen by every session in that map ------------------------------
MAPS = {}   # map_index -> {'monsters': {member: mon}, 'items': {member: {'index', 'count'}}, 'seq': [next member], 'ground': y, 'ai': bool}


# ---- instance dungeons (NInstanceDungeon 0x936xx): one private copy of a dungeon map per run --------------------------------
DUNGEON_MAPS = {5001001: 'Dead Front', 5002001: 'DK Arena', 5003001: 'Lost Horizon', 5004001: 'Nexus Tower 1', 5004002: 'Nexus Tower 2',
                5004003: 'Nexus Tower 3', 5004004: 'Nexus Tower 4', 5004005: 'Nexus Tower 5', 5004006: 'Nexus Tower 6'}
INSTANCE_BASE = 80000000
INSTANCES = {}   # instance key -> {'map': real map, 'owner': sid, 'return': (map, pos), 'kills', 'need', 'limit', 'start', 'started', 'done'}
DUNGEON_KILLS = int(os.environ.get('STUB_DUNGEON_KILLS', '10'))
DUNGEON_LIMIT = int(os.environ.get('STUB_DUNGEON_LIMIT', '600'))
DUNGEON_REWARDS = [(401001, 3)]   # item index, count; plus DUNGEON_GOLD
DUNGEON_GOLD = 500


def real_map(key):
    """The data map behind an instance key (spawn tables, portals, names); plain maps are their own."""
    inst = INSTANCES.get(key)
    return inst['map'] if inst else key


def dungeon_spawn(map_index):
    """Where a hero starts in a dungeon: the middle of the first monster rectangle's left edge, 6 units off it (y 0: the client snaps to the ground)."""
    rows = load_monsters(map_index, 0.0)
    if not rows:
        return (0.0, 0.0, 0.0)
    xs = [r[1] for r in rows]; zs = [r[3] for r in rows]
    return (min(xs) - 6.0, 0.0, (min(zs) + max(zs)) / 2.0)


def mission_frames(cipher, inst):
    # SN_StartMission { byte missionKey; byte missionType (3 KillMonsterCount); uint rewardIndex; byte autoPlayID; int teamIndex;
    #                   TaskInfo[] { int taskType; byte taskIndex; byte groupIndex; int value1; int value2; int limitSec } }
    task = w_i32(0) + w_i8(0) + w_i8(0) + w_i32(inst['need']) + w_i32(0) + w_i32(inst['limit'])
    return frame(0x93613, w_i8(1) + w_i8(3) + w_u32(0) + w_i8(0) + w_i32(0) + w_arr([task]), cipher=cipher)


def dungeon_end(key, victory):
    """SN_EndTask / SN_EndMission / SN_EndInstanceDungeon to everybody in the instance; rewards into every bag on a victory."""
    inst = INSTANCES.get(key)
    if inst is None or inst['done']:
        return
    inst['done'] = True
    rewards = [i for i, _ in DUNGEON_REWARDS] if victory else []
    for osid, o in sessions_in(key):
        c = o['cipher']; out = b''
        if victory:
            out += frame(0x93616, w_i8(1) + w_i8(0) + w_i8(1), cipher=c)        # SN_EndTask { key; taskIndex; endType 1 success }
        out += frame(0x93614, w_i8(1) + w_i8(1 if victory else 2), cipher=c)   # SN_EndMission { key; endType }
        if victory and o.get('account') is not None:
            acc = o['account']; adds = []
            for idx, cnt in DUNGEON_REWARDS:
                b = bag_add(acc, idx, cnt); adds.append(add_item(b['serial'], b['index'], b['count']))
            acc['gold'] = acc.get('gold', 0) + DUNGEON_GOLD
            out += frame(0xA0413, w_arr(adds) + w_i32(0) + w_i64(0), cipher=c) + goods_update(c, 2, DUNGEON_GOLD, acc['gold'])
            save_store(STORE)
        # SN_EndInstanceDungeon { MissionState[] { byte key; byte state }; int[] rewardIndexs; byte victory }
        out += frame(0x93611, w_arr([w_i8(1) + w_i8(1 if victory else 2)]) + w_parr(b''.join(w_i32(i) for i in rewards), 4) + w_i8(1 if victory else 0), cipher=c)
        o['writer'].write(out)
    log('[dungeon] instance %d (%s): %s after %d kills' % (key, DUNGEON_MAPS.get(inst['map'], inst['map']), 'victory' if victory else 'defeat', inst['kills']))


async def dungeon_timer(key):
    inst = INSTANCES.get(key)
    if inst is None:
        return
    await asyncio.sleep(inst['limit'])
    if key in INSTANCES and not INSTANCES[key]['done']:
        dungeon_end(key, False)


def dungeon_kill(key):
    """A monster died inside an instance: mission progress to the instance, the end when the count is reached."""
    inst = INSTANCES.get(key)
    if inst is None or inst['done']:
        return
    inst['kills'] += 1
    for osid, o in sessions_in(key):
        send_session(osid, 0x93615, w_i8(1) + w_i8(0) + w_i32(inst['kills']))   # SN_ProgressMission { key; taskIndex; value }
    if inst['kills'] >= inst['need']:
        dungeon_end(key, True)


# ---- world events (NWorldEvent 0x956xx): world bosses from worldboss.asset on their daily schedule ---------------------------
def load_world_bosses():
    """WorldBossIndex -> {'monster', 'map', 'pos', 'times': [(h, m)], 'span': s} from worldboss/worldboss.asset (3 bosses at 12:00 and 20:00, 4 h)."""
    f = os.path.join(GDB, 'worldboss', 'worldboss.asset')
    out = {}
    if not os.path.exists(f):
        return out
    for blk in re.split(r'\n  - WorldBossIndex: ', open(f, encoding='utf-8').read())[1:]:
        idx = int(blk.split('\n', 1)[0].strip())
        def g(k, conv=float, default=0):
            m = re.search(r'\n    %s: (\S+)' % k, blk); return conv(m.group(1)) if m else default
        times = [(int(a), int(b)) for a, b in re.findall(r'- Hour: (\d+)\r?\n      Minute: (\d+)', blk)]
        out[idx] = {'monster': g('MonsterIndex', int), 'map': g('MapIndex', int), 'pos': (g('xPos'), g('yPos'), g('zPos')), 'times': times, 'span': g('SpawnTimeSpan', int, 14400000) / 1000.0}
    return out


WORLD_BOSSES = load_world_bosses()
BOSS_EVERY = int(os.environ.get('STUB_BOSS_EVERY', '0'))        # test: a boss every N s instead of the daily schedule (0 = schedule)
BOSS_MAP = int(os.environ.get('STUB_BOSS_MAP', '0'))            # test: every boss spawns in this map near the start point
BOSS_HP = int(os.environ.get('STUB_BOSS_HP', '0'))              # test: boss HP override (the real ones have 1,500,000)
BOSS_STATE = {}   # WorldBossIndex -> {'map': map key, 'member': m, 'until': t, 'since': t, 'kills': [(name, t)]}
BOSS_REWARDS = [(401001, 5)]
BOSS_GOLD = 2000


def notice_all(text):
    """A Notice chat line (type 64) to every session."""
    for osid in list(SESSIONS):
        send_session(osid, 0x91802, w_i32(64) + w_i8(0) + w_i64(0) + w_str('Notice') + w_str(text) + w_i32(0) + w_i32(0) + w_i8(1) + w_i32(0) + w_i8(0) + w_i64(int(time.time() * 1000)))


def send_all(code, body):
    for osid in list(SESSIONS):
        send_session(osid, code, body)


def monster_enter_frame(cipher, m, mon):
    x, y, z = mon['pos']
    return frame(0x91201, w_u32(m) + w_u32(mon['index']) + w_i32(mon['max']) + w_i32(mon['hp']) + action_stand(m, x, y, z) + w_i32(0) + w_i32(0) + relation(2, m, propensity=2), cipher=cipher)


def spawn_world_boss(idx):
    wb = WORLD_BOSSES.get(idx)
    if wb is None or idx in BOSS_STATE:
        return
    map_index = BOSS_MAP or wb['map']
    x, y, z = (SPAWN[0] + 10.0, SPAWN[1], SPAWN[2] + 10.0) if BOSS_MAP else wb['pos']
    ms = map_state(map_index, y)
    ms['seq'][0] += 1; m = ms['seq'][0]
    hp = BOSS_HP or monster_hp(wb['monster'])
    mon = {'index': wb['monster'], 'hp': hp, 'max': hp, 'pos': (x, y, z), 'home': (x, y, z), 'next': 0, 'boss': idx}
    ms['monsters'][m] = mon
    BOSS_STATE[idx] = {'map': map_index, 'member': m, 'until': time.time() + wb['span'], 'since': time.time(), 'kills': []}
    broadcast(map_index, lambda c, v: monster_enter_frame(c, m, mon))
    send_all(0x95605, w_i32(idx))                                   # SN_WorldBossSpotOpen
    send_all(0x95609, w_parr(w_i32(idx), 4))                        # SN_WorldBossesOpen { int[] }
    log('[event] world boss %d (monster %d) spawned in map %d as member %d, hp %d' % (idx, wb['monster'], map_index, m, hp))


def close_world_boss(idx, killer=None):
    st = BOSS_STATE.pop(idx, None)
    if st is None:
        return
    ms = MAPS.get(st['map'])
    if ms and st['member'] in ms['monsters'] and killer is None:
        del ms['monsters'][st['member']]
        broadcast(st['map'], lambda c, v, m=st['member']: frame(0x92105, w_i8(0) + w_parr(w_u32(m), 4), cipher=c))   # SN_Leave: the boss vanishes
    send_all(0x95606, w_i32(idx))                                   # SN_WorldBossSpotClose
    send_all(0x95608, w_parr(w_i32(idx), 4))                        # SN_WorldBossesClose { int[] }
    if killer:
        notice_all('The world boss was slain by %s!' % killer)
    log('[event] world boss %d closed (%s)' % (idx, killer or 'timed out'))


def boss_killed(mon, sid, account, hero_name):
    """A session killed a world boss: rewards to the killer, the close to everybody."""
    idx = mon.get('boss')
    out = b''
    if idx in BOSS_STATE:
        BOSS_STATE[idx]['kills'].append((hero_name, time.time()))
        o = SESSIONS.get(sid)
        if o and account is not None:
            adds = []
            for i, cnt in BOSS_REWARDS:
                b = bag_add(account, i, cnt); adds.append(add_item(b['serial'], b['index'], b['count']))
            account['gold'] = account.get('gold', 0) + BOSS_GOLD; save_store(STORE)
            out = frame(0xA0413, w_arr(adds) + w_i32(0) + w_i64(0), cipher=o['cipher']) + goods_update(o['cipher'], 2, BOSS_GOLD, account['gold'])
        close_world_boss(idx, killer=hero_name)
    return out


def boss_due(idx, now):
    wb = WORLD_BOSSES[idx]
    if BOSS_EVERY:
        return (now - SERVER_START) >= BOSS_EVERY and (now - SERVER_START) % BOSS_EVERY < 6
    lt = time.localtime(now)
    return any(h == lt.tm_hour and m == lt.tm_min for h, m in wb['times'])


SERVER_START = time.time()


async def world_event_loop():
    """Every 5 s: spawn the bosses that are due, remove the ones past their time."""
    while True:
        await asyncio.sleep(5)
        try:
            now = time.time()
            for idx in list(WORLD_BOSSES):
                if idx in BOSS_STATE:
                    if now > BOSS_STATE[idx]['until']:
                        close_world_boss(idx)
                elif boss_due(idx, now) and SESSIONS:
                    spawn_world_boss(idx)
        except Exception as e:
            log('[event] loop error %r' % e)


def event_boss_info(cipher):
    """SN_EventBossInfo { int[] WorldBossIndexes; int[] RiftIndexes; FieldBossDeadInfo[] } + SN_SpawnedWorldBosses for a client that just signed in."""
    alive = b''.join(w_i32(i) for i in BOSS_STATE)
    return frame(0x95632, w_parr(alive, 4) + w_parr(b'', 4) + w_i32(0), cipher=cipher) + frame(0x95607, w_parr(alive, 4), cipher=cipher)


def map_state(map_index, ground_y=None):
    """The shared state of a map; created (monsters placed, AI started) the first time somebody enters it."""
    ms = MAPS.get(map_index)
    if ms is None:
        ms = MAPS[map_index] = {'monsters': {}, 'items': {}, 'seq': [MON_BASE + 500], 'ground': ground_y if ground_y is not None else SPAWN[1], 'ai': False}
        for i, (idx, x, y, z) in enumerate(load_monsters(real_map(map_index), ms['ground'])):
            hp = monster_hp(idx)
            ms['monsters'][MON_BASE + i] = {'index': idx, 'hp': hp, 'max': hp, 'pos': (x, y, z), 'home': (x, y, z), 'next': 0}
        if os.environ.get('STUB_MON_AI', '1') != '0' and os.environ.get('STUB_MONSTERS', '1') != '0':
            ms['ai'] = True
            asyncio.ensure_future(map_ai(map_index))
        log('[world] map %d created: %d monsters' % (map_index, len(ms['monsters'])))
    return ms


def sessions_in(map_index):
    return [(osid, o) for osid, o in list(SESSIONS.items()) if o['map'] == map_index and not o['writer'].is_closing()]


def broadcast(map_index, build, exclude=None):
    """build(cipher, viewer_sid) -> bytes to every session in the map (except `exclude`)."""
    for osid, o in sessions_in(map_index):
        if osid == exclude:
            continue
        try:
            data = build(o['cipher'], osid)
            if data:
                o['writer'].write(data)
        except Exception as e:
            log('[world] broadcast to session %d failed: %r' % (osid, e))


def member_as_seen(actor_sid, viewer_sid):
    """A hero's member index as a given viewer sees it (1 for itself, OTHER_BASE + sid for the others)."""
    return 1 if actor_sid == viewer_sid else peer_member(actor_sid)


def world_population(cipher, ground_y, npcs=True, monsters=True, table=None, map_index=None):
    """SN_Enter for every NPC (0x91401) and living monster (0x91201) of the map (the shared table)."""
    map_index = map_index or MAP_INDEX
    out = b''
    for i, (idx, x, y, z, d) in enumerate(load_npcs(real_map(map_index)) if npcs else []):
        m = NPC_BASE + i
        out += frame(0x91401, w_u32(m) + w_u32(idx) + action_stand(m, x, y, z, d) + relation(4, m), cipher=cipher)
    if monsters:
        ms = map_state(map_index, ground_y)
        for m, mon in list(ms['monsters'].items()):
            if mon['hp'] <= 0:
                continue
            x, y, z = mon['pos']
            # { uint member; uint monsterFlyIndex; int maxHP; int currentHP; ActionBase; StatusSyncInfo[]; CrowdControlSyncInfo[]; RelationSource }
            out += frame(0x91201, w_u32(m) + w_u32(mon['index']) + w_i32(mon['max']) + w_i32(mon['hp']) + action_stand(m, x, y, z) + w_i32(0) + w_i32(0)
                         + relation(2, m, propensity=2), cipher=cipher)
        for fm, fi in list(ms['items'].items()):
            x, y, z = fi['pos']
            out += frame(0x91301, action_field_item(fm, x, y, z, fi['index'], fi['count'], fi.get('owner', 0)), cipher=cipher)
    return out


async def respawn_monster(map_index, mon):
    """A dead monster comes back at its home spot after MON_RESPAWN s with a new member index, announced to the whole map."""
    await asyncio.sleep(MON_RESPAWN)
    ms = MAPS.get(map_index)
    if not ms:
        return
    ms['seq'][0] += 1
    m2 = ms['seq'][0]
    x, y, z = mon['home']
    ms['monsters'][m2] = {'index': mon['index'], 'hp': mon['max'], 'max': mon['max'], 'pos': (x, y, z), 'home': (x, y, z), 'next': 0}
    broadcast(map_index, lambda c, v: monster_enter(c, m2, mon['index'], x, y, z, mon['max']))
    log('[world] map %d: respawn %d as member %d' % (map_index, mon['index'], m2))


DEBUFF_CHANCE = float(os.environ.get('STUB_DEBUFF_CHANCE', '0.10'))   # a monster hit that does not stun applies a debuff this often
DEBUFFS = [(7024005, 3, 1.5), (7024004, 0, 0)]   # (status index, poison tick damage, tick seconds): Nunvice's Poison Fang (DoT), Terra's Curse (slow icon)


def apply_debuff(map_index, victim_sid, attacker_member, hero):
    """SN_StatusInsert of a monster debuff on the hero (5 s, status.json duration), poison ticks as SN_Damage, SN_StatusDelete at the end."""
    now = time.time()
    if now < hero.get('debuff_until', 0) or hero.get('dead'):
        return
    status, tick_dmg, tick = random.choice(DEBUFFS)
    dur = status_duration(status) or 5
    hero['debuff_until'] = now + dur
    broadcast(map_index, lambda c, v: frame(0x92221, w_u32(member_as_seen(victim_sid, v)) + w_i32(status) + w_i64(server_tick()) + w_i64(dur * 1000) + w_i8(0) + w_i32(0) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=c))
    log('[world] map %d: session %d debuffed with %d for %d s' % (map_index, victim_sid, status, dur))

    async def run():
        end = time.time() + dur
        while tick_dmg and time.time() + tick < end:
            await asyncio.sleep(tick)
            if hero.get('dead') or victim_sid not in SESSIONS or hero['hp'] <= tick_dmg:
                break
            hero['hp'] -= tick_dmg
            broadcast(map_index, lambda c, v: frame(0x92211, w_u32(attacker_member) + w_u32(member_as_seen(victim_sid, v)) + w_i32(tick_dmg) + w_i32(hero['hp']), cipher=c))
        await asyncio.sleep(max(0.0, end - time.time()))
        if victim_sid in SESSIONS:
            broadcast(map_index, lambda c, v: frame(0x92222, w_u32(member_as_seen(victim_sid, v)) + w_parr(w_i32(status), 4), cipher=c))   # SN_StatusDelete { uint; int[] }
    asyncio.ensure_future(run())


def hero_hit(map_index, victim_sid, attacker_member, dmg, hero, hx, hy, hz, stun=False, debuff=False):
    """SN_Damage / SN_CrowdControl / SN_Dead (+ SN_DeadInfo for the victim) to everybody in the map; the victim sees itself as member 1."""
    now = time.time()
    hero['hit'] = now
    if hero['hp'] > dmg:
        hero['hp'] -= dmg
        if stun and now >= hero.get('cc_until', 0):
            hero['cc_until'] = now + 2.0
            broadcast(map_index, lambda c, v: crowd_control(c, attacker_member, member_as_seen(victim_sid, v), dmg, hero['hp'], hx, hy, hz))
        else:
            broadcast(map_index, lambda c, v: frame(0x92211, w_u32(attacker_member) + w_u32(member_as_seen(victim_sid, v)) + w_i32(dmg) + w_i32(hero['hp']), cipher=c))
            if debuff:
                apply_debuff(map_index, victim_sid, attacker_member, hero)
        return False
    hero['hp'] = 0; hero['dead'] = True
    def dead_frames(c, v):
        me = member_as_seen(victim_sid, v)
        dead = w_i8(4) + w_i64(server_tick()) + w_f32(hx) + w_f32(hy) + w_f32(hz) + w_i16(0) + w_u32(me) + w_i32(0)
        out = frame(0x92212, w_u32(attacker_member) + w_u32(me) + w_i32(dmg) + w_bool(False) + w_i32(4 + len(dead)) + dead + w_f32(hx) + w_f32(hy) + w_f32(hz), cipher=c)
        return out
    broadcast(map_index, dead_frames)
    return True


async def map_ai(map_index):
    """Every 0.5 s: regen for the heroes in the map; each living monster chases / hits the nearest living hero in aggro range."""
    import math
    dt = 0.5
    while True:
        await asyncio.sleep(dt)
        ms = MAPS.get(map_index)
        if not ms:
            return
        now = time.time()
        here = sessions_in(map_index)
        if not here:
            continue
        for sid_, o in here:
            hero = o['hero']; cipher = o['cipher']; out = b''
            if hero['dead']:
                continue
            if hero['mp'] < 500 and now - hero.get('cast', 0) > 3:
                hero['mp'] = min(500, hero['mp'] + 5)
                out += frame(0x92236, w_u32(1) + w_u32(1) + w_u32(0) + w_i8(0) + w_i32(5) + w_i8(2) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=cipher)   # SN_Heal healType 2 = MP
            if hero['hp'] < hero['maxhp'] and now - hero['hit'] > 5:
                add = max(1, int(hero['maxhp'] * REGEN_PCT / 100))
                hero['hp'] = min(hero['maxhp'], hero['hp'] + add)
                out += frame(0x92236, w_u32(1) + w_u32(1) + w_u32(0) + w_i8(0) + w_i32(add) + w_i8(1) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=cipher)
            if out:
                o['writer'].write(out)
        alive = [(sid_, o) for sid_, o in here if not o['hero']['dead']]
        for m, mon in list(ms['monsters'].items()):
            if mon['hp'] <= 0:
                continue
            x, y, z = mon['pos']
            best = None; best_d = MON_AGGRO
            for sid_, o in alive:
                hx, hy, hz = o['hero']['pos']
                d = math.hypot(hx - x, hz - z)
                if d < best_d:
                    best, best_d = (sid_, o), d
            if best is None:
                continue
            vsid, vo = best; hero = vo['hero']; hx, hy, hz = hero['pos']; dist = best_d
            if dist > MON_RANGE:
                step = min(dist - MON_RANGE * 0.8, MON_SPEED / 1000.0 * dt)
                nx, nz = x + (hx - x) / dist * step, z + (hz - z) / dist * step
                mon['pos'] = (nx, hy, nz)
                broadcast(map_index, lambda c, v, m=m, x=x, y=y, z=z, nx=nx, hy=hy, nz=nz: frame(0x92101, action_move(m, x, y, z, nx, hy, nz, MON_SPEED), cipher=c))
            elif now >= mon['next']:
                mon['next'] = now + 2.0
                broadcast(map_index, lambda c, v, m=m, x=x, y=y, z=z, vsid=vsid, hx=hx, hy=hy, hz=hz: frame(0x92101, action_skill(m, x, y, z, MON_SKILL, member_as_seen(vsid, v), hx, hy, hz), cipher=c))
                died = hero_hit(map_index, vsid, m, MON_DAMAGE, hero, hx, hy, hz, stun=random.random() < CC_CHANCE, debuff=random.random() < DEBUFF_CHANCE)
                if died:
                    # SN_DeadInfo 0x92239 to the victim: opens the "You died" panel with the Revive button (reviveTick = when revive is allowed)
                    atk = w_i32(mon['index']) + w_str('') + w_i32(1) + w_i8(0) + w_i8(0)   # AttackerBaseInfo { int index; string name; int level; byte gun; ExtendedShapeInfo{byte} }
                    pen = dead_penalty(vo.get('h'))
                    vo['writer'].write(frame(0x92239, w_i8(2) + w_bool(True) + atk + w_bool(False) + w_arr([penalty_blob(pen)] if pen else []) + w_i32(0)
                                             + w_i64(pen['value'] if pen else 0) + w_i64(0) + w_bool(False) + w_i64(server_tick() + 5000), cipher=vo['cipher']))
                    if pen:
                        vo['writer'].write(frame(0x92225, w_i64(-pen['value']) + w_u32(vo['h']['level']) + w_i64(vo['h']['exp']), cipher=vo['cipher']))   # SN_ExpUp (negative)
                    log('[world] map %d: hero of session %d killed by monster %d' % (map_index, vsid, m))


PENALTY_PERCENT = int(os.environ.get('STUB_PENALTY_PERCENT', '5'))   # EXP lost on death, % of the level's EXP (0 = no penalty)
PENALTY_HOURS = 24


def dead_penalty(h):
    """Takes PENALTY_PERCENT of the level's EXP from hero record h and records it as a DeadPenaltyInfo (type 1 = EXP); None when nothing is lost."""
    if not h or not PENALTY_PERCENT or h.get('level', 1) < 2:
        return None
    need = LEVELS.get(h.get('level', 1), 0)
    loss = min(h.get('exp', 0), max(1, need * PENALTY_PERCENT // 100))
    if loss <= 0:
        return None
    h['exp'] -= loss
    pens = h.setdefault('penalties', [])
    pen = {'no': int(time.time() * 1000) % 10 ** 12, 'type': 1, 'value': loss, 'expire': int(time.time()) + PENALTY_HOURS * 3600}
    pens.append(pen); save_store(STORE)
    log('[world] hero %s lost %d EXP (penalty %d)' % (h.get('name'), loss, pen['no']))
    return pen


def penalty_blob(pen):
    """DeadPenaltyInfo { E_PenaltyType (byte); long penaltyNo; int penaltyValue; DateTime expireTime }"""
    return w_i8(pen['type']) + w_i64(pen['no']) + w_i32(pen['value']) + w_dt(pen['expire'])


def penalty_cost(pen):
    return max(100, pen['value'] // 5)


def penalty_info(cipher, h):
    """SN_DeadPenaltyInfo 0x9016A { DeadPenaltyInfo[]; AddItem[] } with the hero's open penalties (expired ones dropped)."""
    now = int(time.time())
    pens = [x for x in (h.get('penalties') or []) if x['expire'] > now]
    h['penalties'] = pens
    return frame(0x9016A, w_arr([penalty_blob(x) for x in pens]) + w_i32(0), cipher=cipher)


# ---- attendance: the first sign-in of each day mails the day's reward (7-day cycle) and says so in a Notice line
ATTENDANCE = [(13, 2, 500), (1, 401001, 3), (13, 2, 800), (1, 401001, 5), (13, 2, 1000), (1, 401001, 5), (13, 2, 3000)]   # (mail type, index, value)


def notice_frame(cipher, text):
    return frame(0x91802, w_i32(64) + w_i8(0) + w_i64(0) + w_str('Notice') + w_str(text) + w_i32(0) + w_i32(0) + w_i8(1) + w_i32(0) + w_i8(0) + w_i64(int(time.time() * 1000)), cipher=cipher)


def attendance(cipher, account):
    today = time.strftime('%Y-%m-%d')
    at = account.setdefault('attend', {'last': '', 'day': 0})
    if at['last'] == today:
        return b''
    at['day'] = at['day'] % len(ATTENDANCE) + 1; at['last'] = today
    mtype, idx, val = ATTENDANCE[at['day'] - 1]
    now = int(time.time())
    account['mails'] = account.get('mails', []) + [{'no': now * 10 + 3, 'type': mtype, 'index': idx, 'value': val, 'sub': 0, 'title': 'Attendance day %d' % at['day'],
                                                     'msg': 'Thank you for playing today. Day %d of %d: here is your reward.' % (at['day'], len(ATTENDANCE)), 'time': now}]
    save_store(STORE)
    log('[attend] day %d: %s' % (at['day'], 'gold %d' % val if mtype == 13 else 'item %d x%d' % (idx, val)))
    return notice_frame(cipher, 'Attendance day %d: your reward is in the mailbox.' % at['day'])


def map_monster_kinds(map_index):
    """Distinct MonsterIndex values of mapmonsterinfo_<map>.asset (sent in MapEnterInfo.monsterIndex for preloading)."""
    f = os.path.join(GDB, 'mapmonsterinfo', 'mapmonsterinfo_%d.asset' % map_index)
    if not os.path.exists(f):
        return []
    seen = []
    for m in re.findall(r'- MonsterIndex: (\d+)', open(f, encoding='utf-8').read()):
        if int(m) not in seen:
            seen.append(int(m))
    return seen

INJECT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'inject')


def run_injects(cipher, name):
    """Live test hook: every *.py in tools/stub/inject/ is executed once (globals = this module) and its `payload` bytes
    are sent to the client on the next ping; the file is then renamed to .done. Our own scripts only."""
    out = b''
    if not os.path.isdir(INJECT_DIR):
        return out
    for fn in sorted(os.listdir(INJECT_DIR)):
        if not fn.endswith('.py'):
            continue
        path = os.path.join(INJECT_DIR, fn)
        g = dict(globals()); g.update(cipher=cipher, payload=b'')
        try:
            exec(compile(open(path, encoding='utf-8').read(), path, 'exec'), g)
            out += g['payload']
            log('[%s] inject %s: %d bytes' % (name, fn, len(g['payload'])))
        except Exception as e:
            log('[%s] inject %s FAILED: %r' % (name, fn, e))
        os.replace(path, path + '.done')
    return out

# --- persistence: tools/stub/stub.sqlite (stubdb.py): accounts (login, kind, password hash, ban) + one JSON state document per
# account = { heroes: [...], country, bag, abag, gold, friends, letters ... } kept in memory as STORE[login]. An old save.json
# ({ memberId: state }) is imported once into 'device' accounts when the database is still empty.
SAVE_FILE = os.environ.get('STUB_SAVE', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'save.json'))
DB = stubdb.Db()
GUESTS_ALLOWED = os.environ.get('STUB_GUESTS', '1') == '1'   # device / Google-id accounts without a password (the real APK has no password field)
SECRET_CODES = {0x90101, 0x9F001, 0x9F003, 0x80105}         # rows that carry a password / token: never hex-dumped into the log


def load_store():
    if not DB.list():
        n = DB.import_json(SAVE_FILE)
        if n:
            log('[db] imported %d accounts from %s' % (n, SAVE_FILE))
    store = DB.load_states()
    for acc in store.values():   # hero numbers used to be 1, 2 ... per account: make them unique (the party / friend packets carry them)
        for h in acc.get('heroes', []):
            if h.get('name') and h.get('no', 0) < 1000:
                h['no'] = fid(h['name'])
    return store


def save_store(store):
    DB.save(store)


def sign_in(member, secret, via):
    """Account check for CQ_Login / HTTP Login. `secret` = password (our client), session token (the real client after
    CQ_RegionLogin) or the platform token (device accounts). Returns an stubdb.ERR_ code; makes device accounts on the fly
    when STUB_GUESTS=1."""
    a = DB.account(member)
    hv = hub_verify(secret)                                  # a valid hub token = cross-server identity: provision a local shadow account
    if hv and hv[0] == member:
        if a is None:
            a = DB.ensure(member, kind='hub')
        if a is not None and (a['banned_until'] or 0) <= time.time():
            return stubdb.ERR_OK
    if a is None:
        if not GUESTS_ALLOWED or not stubdb.valid_login(member) or (secret and via == 'game-login' and not hv):   # a password for an unknown name is a typo, not a guest
            log('[db] unknown account %s... via %s' % (member[:8], via))
            return stubdb.ERR_NO_ACCOUNT
        err = DB.create(member, None)
        log('[db] new device account %s... via %s (%s)' % (member[:8], via, 'ok' if err == 0 else err))
        return err
    if a['kind'] == 'password' and secret and DB.session(member, token=secret) is not None:
        err = stubdb.ERR_BANNED if (a['banned_until'] or 0) > time.time() else stubdb.ERR_OK
    else:
        err, _ = DB.check(member, secret, allow_device=GUESTS_ALLOWED)
    if err:
        DB.audit(member, 'login_failed', '%s: %s' % (via, stubdb.ERROR_TEXT.get(err, err)))
        log('[db] sign-in refused for %s... via %s: %s' % (member[:8], via, stubdb.ERROR_TEXT.get(err, err)))
    return err


def hub_authenticate(member, secret, password=None):
    """The HUB's account authority. `member` = login or device id; `secret` = a hub token or platform/guest token;
    `password` = a web/registered account's password. Returns (err, login, accountNo, token). An unknown device id gets a
    fresh hub account (guest, upgradeable on the web page) when guests are allowed. Needs HUB_AUTH_SECRET to mint tokens."""
    if not HUB_AUTH_SECRET:
        return stubdb.ERR_NO_ACCOUNT, member, 0, ''
    hv = hub_verify(secret)                                  # already-linked account presenting its token
    a = DB.account(member)
    if hv and hv[0] == member and a is not None and (a['banned_until'] or 0) <= time.time():
        return stubdb.ERR_OK, a['login'], a['id'], hub_sign(a['login'], a['id'])
    if a is not None:
        if (a['banned_until'] or 0) > time.time():
            return stubdb.ERR_BANNED, member, a['id'], ''
        if a['kind'] == 'password':
            err, _ = DB.check(member, password if password is not None else secret, allow_device=False)
            if err:
                DB.audit(member, 'login_failed', 'hub-auth: %s' % stubdb.ERROR_TEXT.get(err, err))
                return err, member, a['id'], ''
        return stubdb.ERR_OK, a['login'], a['id'], hub_sign(a['login'], a['id'])
    if not GUESTS_ALLOWED or not stubdb.valid_login(member):
        return stubdb.ERR_NO_ACCOUNT, member, 0, ''
    a = DB.ensure(member, kind='hub')                        # new guest hub account keyed by the device id
    if a is None:
        return stubdb.ERR_BAD_LOGIN, member, 0, ''
    log('[hub] new hub account %s... (guest)' % member[:8])
    return stubdb.ERR_OK, a['login'], a['id'], hub_sign(a['login'], a['id'])


async def hub_delegate(member, secret):
    """A community server asks its hub to authenticate `member`. Returns (err, login, token)."""
    ctx = ssl.create_default_context()
    if HUB_INSECURE: ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    body = json.dumps({'memberId': member, 'token': secret or '', 'serverKey': HUB_AUTH_SECRET}).encode()
    req = urllib.request.Request(HUB_AUTH_URL.rstrip('/') + '/hub/auth', data=body, headers={'Content-Type': 'application/json'}, method='POST')
    try:
        raw = await asyncio.get_event_loop().run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=10, context=ctx).read())
        j = json.loads(raw)
    except Exception as e:
        log('[hub] delegate to %s failed: %r' % (HUB_AUTH_URL, e))
        return stubdb.ERR_NO_ACCOUNT, member, ''
    return int(j.get('error', 1)), j.get('login', member) or member, j.get('token', '') or ''


STORE = load_store()

def item_info(serial, index, count=1):
    # ItemInfo { long serial; int itemIndex; int count; byte enchant; byte specialAbilityCount; byte socketCount; byte lockFlag; Nullable<DateTime> periodEnd; int nRemain }
    return w_i64(serial) + w_i32(index) + w_i32(count) + w_i8(0) + w_i8(0) + w_i8(0) + w_i8(0) + w_bool(False) + w_i32(0)


def add_item(serial, index, count=1):
    # AddItem { ItemInfo; AbilityInfo[]; AbilityInfo[]; SocketSlotInfo[] }
    return item_info(serial, index, count) + w_i32(0) + w_i32(0) + w_i32(0)


def read_item_infos(body, off=0):
    """ItemInfo[] { long serial; int itemIndex; int count; 4 bytes; Nullable<DateTime>; int nRemain } -> [(serial, index, count)], end offset."""
    n = struct.unpack_from('<i', body, off)[0]; off += 4; out = []
    for _ in range(n):
        serial, index, count = struct.unpack_from('<qii', body, off); off += 20
        has = body[off]; off += 1 + (8 if has else 0)
        off += 4
        out.append((serial, index, count))
    return out, off


_SKILL_CACHE = {}


def load_skill(index):
    """{'mp', 'hp', 'cool'} from skill/<index>.asset (HActiveTypeInfo JSON)."""
    if index in _SKILL_CACHE:
        return _SKILL_CACHE[index]
    f = os.path.join(GDB, 'skill', '%d.asset' % index)
    out = {'mp': 0, 'hp': 0, 'cool': 0.0}
    if os.path.exists(f):
        t = open(f, encoding='utf-8').read()
        out['status'] = 0
    if os.path.exists(f):
        mb = re.search(r'HImpactFormulaBuff.{0,400}?\\"StatusIndex\\" : (\d+)', t, re.S)
        if mb:
            out['status'] = int(mb.group(1))
    for k, key, conv in (('mp', 'MpUse', int), ('hp', 'HpUse', int), ('cool', 'CoolTime', float)):
            m = re.search(r'\\"%s\\" : (-?[\d.]+)' % key, t)
            if m:
                out[k] = conv(m.group(1))
    _SKILL_CACHE[index] = out
    return out


def status_duration(index):
    """RemainSecMax of status/status.asset entry `index` (seconds)."""
    f = os.path.join(GDB, 'status', 'status.asset')
    if not os.path.exists(f):
        return 60
    t = open(f, encoding='utf-8').read()
    m = re.search(r'\n    Index: %d\n(?:.*\n){0,20}?    RemainSecMax: (\d+)' % index, t)
    return int(m.group(1)) if m else 60


SKILL_DAMAGE = int(os.environ.get('STUB_SKILL_DAMAGE', '10'))   # damage of a class skill (basic attack: STUB_DAMAGE)
STAT_POINTS_PER_LEVEL = int(os.environ.get('STUB_STAT_POINTS', '3'))


def hero_stats(h):
    """Persisted stats of hero record h: {'str','dex','int','con','agi','wis','points','levelpt'} (class base 16/15/15/18/16/15)."""
    st = h.setdefault('stats', {'str': 16, 'dex': 15, 'int': 15, 'con': 18, 'agi': 16, 'wis': 15, 'points': 0, 'levelpt': 0})
    return st


def load_quest(index):
    """{'next': int, 'exp': int, 'items': [(itemIndex, count)]} from quest/quest_base_<index>.asset (its JSON Data strings)."""
    f = os.path.join(GDB, 'quest', 'quest_base_%d.asset' % index)
    q = {'next': 0, 'exp': 0, 'items': [], 'task': None}
    if not os.path.exists(f):
        return q
    t = open(f, encoding='utf-8').read()
    m = re.search(r'\\"NextQuestId\\" : (\d+)', t)
    if m:
        q['next'] = int(m.group(1))
    m = re.search(r'\\"Exp\\" : (\d+)', t)
    if m:
        q['exp'] = int(m.group(1))
    k = t.find('CountGoal')
    if k >= 0:
        seg = t[k:k + 400]
        gi = re.search(r'Index\W+(\d+)', seg); gc = re.search(r'Count\W+(\d+)', seg); gt = re.search(r'Type\W+(\w+)', seg)
        if gi and gc and gt:
            q['task'] = {'index': int(gi.group(1)), 'count': int(gc.group(1)), 'type': gt.group(1)}
    for m in re.finditer(r'HGameAssetDataCategoryItem\\",\\r\\n\\t\\t\\"Index\\" : (\d+),\\r\\n\\t\\t\\"Count\\" : (\d+)', t):
        q['items'].append((int(m.group(1)), int(m.group(2))))
    return q

MON_SKILL = 10040101          # monsterbody character500101 BasicSkill (the town demons' melee hit)
MON_SPEED = int(os.environ.get('STUB_MON_SPEED', '3500'))
MON_RANGE = float(os.environ.get('STUB_MON_RANGE', '1.6'))
MON_AGGRO = float(os.environ.get('STUB_MON_AGGRO', '9.0'))
MON_DAMAGE = int(os.environ.get('STUB_MON_DAMAGE', '7'))
MON_RESPAWN = float(os.environ.get('STUB_MON_RESPAWN', '8'))


def action_move(member, x, y, z, dx, dy, dz, speed, d=0):
    blob = w_i8(2) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(d) + w_u32(member) + w_u32(speed) + w_f32(dx) + w_f32(dy) + w_f32(dz)
    return w_i32(4 + len(blob)) + blob


def action_cc(member, x, y, z, key, end_tick, level=0, detail=1, d=0):
    # CrowdControl : ActionBase + int key; byte level; long endTick; byte DetailAction; uint speed; float destX, destY, destZ
    blob = (w_i8(9) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(d) + w_u32(member)
            + w_i32(key) + w_i8(level) + w_i64(end_tick) + w_i8(detail) + w_u32(0) + w_f32(x) + w_f32(y) + w_f32(z))
    return w_i32(4 + len(blob)) + blob


def crowd_control(cipher, attacker, defender, dmg, hp, x, y, z, key=1, ms=2000):
    """SN_CrowdControl 0x92214 { uint attacker; uint defender; int damage; int currentHP; ActionBase (CrowdControl); CrowdControlSyncInfo { int key; byte level; long start; long end } }"""
    st = server_tick(); en = st + ms
    return frame(0x92214, w_u32(attacker) + w_u32(defender) + w_i32(dmg) + w_i32(hp) + action_cc(defender, x, y, z, key, en) + w_i32(key) + w_i8(0) + w_i64(st) + w_i64(en), cipher=cipher)


CC_CHANCE = float(os.environ.get('STUB_CC_CHANCE', '0.15'))   # a monster hit stuns the hero (key 1, 2 s) this often


def action_skill(member, x, y, z, skill, target, tx, ty, tz, d=0):
    # SkillAction: ActionBase + int skillIndex; byte skillLevel; uint skillUid; long endTick; byte byUsePos; float x,y,z; byte impactIndex; uint target; int attackSpeed
    blob = (w_i8(3) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(d) + w_u32(member)
            + w_i32(skill) + w_i8(1) + w_u32(0) + w_i64(0) + w_i8(0) + w_f32(tx) + w_f32(ty) + w_f32(tz) + w_i8(0) + w_u32(target) + w_i32(10000))
    return w_i32(4 + len(blob)) + blob


def monster_enter(cipher, m, idx, x, y, z, hp):
    return frame(0x91201, w_u32(m) + w_u32(idx) + w_i32(hp) + w_i32(hp) + action_stand(m, x, y, z) + w_i32(0) + w_i32(0)
                 + relation(2, m, propensity=2), cipher=cipher)

DROP_ITEM = int(os.environ.get('STUB_DROP_ITEM', '401001'))   # 401001 = auto HP potion (bound), 50 gold in shop 1010301   # what every monster drops (412001 = a speed potion; any item works for the pickup test)
MON_EXP = int(os.environ.get('STUB_MON_EXP', '2'))
REGEN_PCT = float(os.environ.get('STUB_REGEN_PCT', '2'))      # % max HP per AI tick out of combat


def load_levels():
    """[maxExp by level] from characterlist/heroconfig.asset HeroExp (level 1 needs 4, 2 needs 6 ...)."""
    f = os.path.join(GDB, 'characterlist', 'heroconfig.asset')
    out = {}
    if os.path.exists(f):
        for lv, mx in re.findall(r'- Level: (\d+)\r?\n\s+MaxExp: (\d+)', open(f, encoding='utf-8').read()):
            out[int(lv)] = int(mx)
    return out


LEVELS = load_levels()


def gain_exp(cipher, h, hero, amount):
    """SN_ExpUp (+ SN_LevelUp per level gained) for persisted hero h / live state hero; returns the frames."""
    h['exp'] = h.get('exp', 0) + amount
    out = b''
    while LEVELS.get(h['level'], 0) and h['exp'] >= LEVELS[h['level']] and h['level'] < 120:
        h['exp'] -= LEVELS[h['level']]
        h['level'] += 1
        php, pmp = hero['hp'], hero['mp']
        hero['maxhp'] += 50; hero['hp'] = hero['maxhp']; hero['mp'] = 500
        st = hero_stats(h); st['points'] += STAT_POINTS_PER_LEVEL; st['levelpt'] += STAT_POINTS_PER_LEVEL
        # SN_LevelUp { uint member; uint level; int prevHP; int prevMP; int newHP; int newMP; int statPoint }
        out += frame(0x92226, w_u32(1) + w_u32(h['level']) + w_i32(php) + w_i32(pmp) + w_i32(hero['hp']) + w_i32(hero['mp']) + w_i32(st['points']), cipher=cipher)
        # SN_UpdateStatInfo { 6 stats; remain; levelStatPoint; etcStatPoint; int E_Code }
        out += frame(0x93D05, w_i32(st['str']) + w_i32(st['dex']) + w_i32(st['int']) + w_i32(st['con']) + w_i32(st['agi']) + w_i32(st['wis']) + w_i32(st['points']) + w_i32(st['levelpt']) + w_i32(0) + w_i32(0), cipher=cipher)
    # SN_ExpUp { long addExp; uint currentLevel; long currentExp }
    return frame(0x92225, w_i64(amount) + w_u32(h['level']) + w_i64(h['exp']), cipher=cipher) + out


def action_field_item(member, x, y, z, item_index, count, owner_no):
    # FieldItemOwnership (E_Action 110): ActionBase + int ItemIndex; int Count; byte FieldItemOwnerType (1 Character); long OwnerNo
    blob = w_i8(110) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_u32(member) + w_i32(item_index) + w_i32(count) + w_i8(1) + w_i64(owner_no)
    return w_i32(4 + len(blob)) + blob

def load_portals(map_index):
    """{portalIndex: (endMapIndex, (x, y, z, direction))} from staticportal/portal<map>.asset (first end position)."""
    f = os.path.join(GDB, 'staticportal', 'portal%d.asset' % map_index)
    out = {}
    if not os.path.exists(f):
        return out
    for blk in re.split(r'\n  - Index: ', open(f, encoding='utf-8').read())[1:]:
        idx = int(blk.split('\n', 1)[0])
        em = re.search(r'EndMapIndex: (\d+)', blk)
        ep = re.search(r'EndPosX: (-?[\d.eE+-]+)\r?\n\s+EndPosY: (-?[\d.eE+-]+)\r?\n\s+EndPosZ: (-?[\d.eE+-]+)\r?\n\s+EndDirection: (-?\d+)', blk)
        if em and ep:
            out[idx] = (int(em.group(1)), (float(ep.group(1)), float(ep.group(2)), float(ep.group(3)), int(ep.group(4))))
    return out


SKILLS = [int(x) for x in os.environ.get('STUB_SKILLS', '1010000,1012001,1012002,1012003,1012004,1012005').split(',') if x]   # the Swordsman's skill records

TEST_SHOP_NPC = int(os.environ.get('STUB_SHOP_NPC', '900100103'))   # this NPC gets a misc shop job (shop 1010301) for tests
TEST_SHOP = int(os.environ.get('STUB_SHOP', '1010301'))
START_GOLD = int(os.environ.get('STUB_GOLD', '100000'))
POTION_HEAL = int(os.environ.get('STUB_POTION_HEAL', '300'))


def _json_blocks(text):
    """The JSON Data strings of a gamedatabase asset, unescaped, each with the DepthList field name that follows it."""
    out = []
    for m in re.finditer(r'Data: "(\{.*?\}|\[.*?\])"\r?\n\s+DepthList:\r?\n\s+- Field: (\w+)', text, re.S):
        out.append((m.group(2), m.group(1).replace('\\r\\n', '').replace('\\t', '').replace('\\"', '"')))
    return out


def load_npc_jobs(npc_index):
    """[(jobType, index)] from npc/npc_<idx>.asset JobList."""
    f = os.path.join(GDB, 'npc', 'npc_%d.asset' % npc_index)
    if not os.path.exists(f):
        return []
    t = open(f, encoding='utf-8').read()
    m = re.search(r'\n  JobList:\n((?:  - .*\n|    .*\n)*)', t)
    return [(int(a), int(b)) for a, b in re.findall(r'- JobType: (\d+)\r?\n\s+Index: (\d+)', m.group(1))] if m else []


def load_product(pidx):
    """(itemIndex, count, goldPrice) from shop/product_<idx>.asset."""
    f = os.path.join(GDB, 'shop', 'product_%d.asset' % pidx)
    if not os.path.exists(f):
        return None
    item = price = None
    for field, js in _json_blocks(open(f, encoding='utf-8').read()):
        if field == 'PurchasingItem':
            m = re.search(r'"Index" : (\d+),\s*"Count" : (\d+)', js)
            if m:
                item = (int(m.group(1)), int(m.group(2)))
        elif field == 'Price':
            m = re.search(r'"Type" : "Gold",\s*"Count" : (\d+)', js)
            if m:
                price = int(m.group(1))
    return (item[0], item[1], price) if item and price is not None else None


def item_sell_price(item_index):
    f = os.path.join(GDB, 'item', 'item%d.asset' % item_index)
    if not os.path.exists(f):
        return 0
    for field, js in _json_blocks(open(f, encoding='utf-8').read()):
        if field == 'SellPrice':
            m = re.search(r'"Type" : "Gold",\s*"Count" : (\d+)', js)
            if m:
                return int(m.group(1))
    return 0


def goods_update(cipher, gtype, delta, current):
    # SN_UpdateAccountGoods { UpdateAccountGoods[] { int AccountGoodsType; long delta; long current }; int UserLogType; long logGroupSerial }
    return frame(0xA0473, w_arr([w_i32(gtype) + w_i64(delta) + w_i64(current)]) + w_i32(0) + w_i64(0), cipher=cipher)

SKIP_TUTORIAL = os.environ.get('STUB_SKIP_TUTORIAL', '0') == '1'   # mark the Ardeca main quest chain complete (frees the hero from auto-walking)
TUTORIAL_QUESTS = list(range(900100100, 900100112))


def quest_group(index):
    f = os.path.join(GDB, 'quest', 'quest_base_%d.asset' % index)
    if os.path.exists(f):
        m = re.search(r'GroupIndex: (\d+)', open(f, encoding='utf-8').read())
        if m:
            return int(m.group(1))
    return 0

MAX_PACKET = 1024    # the client's receive buffer: "packetLength 1274 > 1024" kills the connection


def bag_add(account, index, count):
    """Put count of item index into the account bag, stacking onto an existing entry of a stackable item; returns the entry."""
    if item_stackable(index):
        for b in account.setdefault('bag', []):
            if b['index'] == index:
                b['count'] += count
                return b
    account['serial'] = account.get('serial', 100) + 1
    b = {'serial': account['serial'], 'index': index, 'count': count}
    account.setdefault('bag', []).append(b)
    return b


_STACK = {}


def item_stackable(index):
    """ItemType 4 Consume / 7 Stack items stack (E_Type); weapons / armour / accessories do not."""
    if index not in _STACK:
        f = os.path.join(GDB, 'item', 'item%d.asset' % index)
        t = open(f, encoding='utf-8').read() if os.path.exists(f) else ''
        m = re.search(r'ItemType: (\d+)', t)
        _STACK[index] = bool(m) and int(m.group(1)) in (4, 7)
    return _STACK[index]


def chunks(items, n):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def map_enter_info(hp=1000, mp=500, map_index=None, pos=None):
    map_index = real_map(map_index or MAP_INDEX)
    # MapEnterInfo: mapIndex, currentHP, currentMP, posX, posY, posZ, direction ushort, mapDangerType byte, areaType byte,
    # monsterIndex uint[], gatherIndex int[], triggerIndexes int[]  + WorldEnvironmentInfo { WorldStatusData[] }
    x, y, z = pos or SPAWN
    mi = (w_i32(map_index) + w_i32(hp) + w_i32(mp) + w_f32(x) + w_f32(y) + w_f32(z) + w_u16(0) + w_i8(0) + w_i8(AREA_TYPE)
          + w_parr(b''.join(w_u32(i) for i in map_monster_kinds(map_index))) + w_parr(b'') + w_parr(b''))
    return mi + w_i32(0)


def sn_hero_data(h):
    # SN_HeroData 0x90133 (field order of protocol.json)
    now = int(time.time() * 1000)
    st = hero_stats(h)
    return (w_i64(h['no']) + w_i32(1) + w_i32(h['index']) + w_str(h['name']) + w_u32(h['level']) + w_i64(h.get('exp', 0)) + w_i32(st['points'])
            + w_i32(st['str']) + w_i32(st['dex']) + w_i32(st['int']) + w_i32(st['con']) + w_i32(st['agi']) + w_i32(st['wis']) + w_i64(0) + w_i32(0) + w_i32(40) + w_i32(0)
            + w_i32(0) + w_dt() + w_i32(40) + w_i32(0) + w_i32(st['levelpt']) + w_i32(0) + w_i32(0) + w_i8(1) + w_i32(0) + w_i32(0) + w_i64(0))


# ---- Swift "Default" cipher (Swift.Infra.Network.Cipher.Default, recovered from libil2cpp.so) ----
# Row = 12-byte header + payload.  header: [0..4] CRC32 (big endian, over the obfuscated bytes 4..size-1),
# [4..8] code (big endian), [8..10] frame counter (big endian; not incremented for protocol codes 0x1xxxx),
# [10..12] total size incl. header (big endian).  Every byte from 4 to size-1 is XORed with
# (crctable[i & 0xff] & 0xff) ^ key, key = low byte of (serverHSKey ^ clientHSKey), 0 before any handshake.
CRCTAB = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ 0xEDB88320 if _c & 1 else _c >> 1
    CRCTAB.append(_c)
HDR = 12


class SwiftCipher:
    def __init__(self):
        self.key = 0x0F; self.frame = 0   # initial key byte: the Default cipher ctor loads a constant (see FINDINGS)

    def encrypt(self, code, body):
        size = HDR + len(body)
        buf = bytearray(size)
        buf[4:8] = struct.pack('>I', code & 0xFFFFFFFF)
        if (code >> 16) != 1:
            self.frame = (self.frame + 1) & 0xFFFF
        buf[8:10] = struct.pack('>H', self.frame)
        buf[10:12] = struct.pack('>H', size)
        buf[12:] = body
        crc = 0xFFFFFFFF
        for i in range(4, size):
            b = buf[i] ^ ((CRCTAB[i & 0xFF] ^ self.key) & 0xFF)
            buf[i] = b
            crc = CRCTAB[(b ^ crc) & 0xFF] ^ (crc >> 8)
        buf[0:4] = struct.pack('>I', (~crc) & 0xFFFFFFFF)
        return bytes(buf)

    def decrypt_rows(self, buf):
        """Returns (rows[(code, body, frame, crc_ok)], rest). Decodes in place semantics on a copy."""
        rows = []
        while len(buf) >= HDR:
            # de-obfuscate the header to learn the size
            hdr = bytearray(buf[:HDR])
            for i in range(4, HDR):
                hdr[i] ^= (CRCTAB[i & 0xFF] ^ self.key) & 0xFF
            size = struct.unpack('>H', hdr[10:12])[0]
            if size < HDR or size > 65535:
                rows.append((None, bytes(buf), None, False)); buf = b''; break
            if len(buf) < size:
                break
            raw = bytearray(buf[:size]); buf = buf[size:]
            crc = 0xFFFFFFFF
            for i in range(4, size):
                ob = raw[i]
                crc = CRCTAB[(ob ^ crc) & 0xFF] ^ (crc >> 8)
                raw[i] = ob ^ ((CRCTAB[i & 0xFF] ^ self.key) & 0xFF)
            want = struct.unpack('>I', raw[0:4])[0]
            code = struct.unpack('>I', raw[4:8])[0]; frame = struct.unpack('>H', raw[8:10])[0]
            rows.append((code, bytes(raw[12:]), frame, want == ((~crc) & 0xFFFFFFFF)))
        return rows, buf


def frame(code, body, mode=None, cipher=None):
    return (cipher or SwiftCipher()).encrypt(code, body)


FRAME_MODE = 'size_total'

# ---- packets the stub answers (codes from protocol.json) ----
CQ_ServerGunList, SA_ServerGunList = 0x80101, 0x80102
CQ_RegionLogin, SA_RegionLogin = 0x80105, 0x80106
SN_TryGameServerLogin = 0x8010A
CQ_Login, SA_Login = 0x90101, 0x90102


def sa_server_gun_list():
    # SA_ServerGunList { ServerGunInfo[] infos }  (array prefix assumed i32 count; alternatives tried from the log)
    info = (w_i8(1) + w_i8(1) + w_str('Stub World') + w_i8(3) + w_i64(1000) + w_i64(1) + w_i64(1000) + w_i64(1)   # gunState 3 = ServerOpen + w_i64(1) + w_i64(1000) + w_i64(1)
            + w_i16(0) + w_str(ADV_GAME_HOST or HOST_IP) + w_i16(ADV_GAME_PORT or GAME_PORT) + w_bool(True) + w_bool(False) + w_i32(0) + w_i8(0))
    return w_i32(1) + info   # ArrayFormatter<T>: int32 count (primitive arrays use int16 byte counts)




async def tcp_handler(reader, writer, name):
    peer = writer.get_extra_info('peername'); log('[%s] connect from %s' % (name, peer))
    buf = b''
    cipher = SwiftCipher()
    heroes = []   # the signed-in account's heroes (STORE[memberId]['heroes'] after CQ_Login; saved to save.json)
    account = {}
    cur_hero = None
    hero = {'pos': SPAWN, 'hp': 1000, 'maxhp': 1453, 'mp': 500, 'dead': False, 'hit': 0.0}
    field_items = {}   # memberIndex -> {'index', 'count'}
    world = {'map': MAP_INDEX, 'spawn': SPAWN, 'ai': False}
    next_member = [MON_BASE + 500]
    quests = {}   # questNo -> {index, count, state} (per connection)
    monsters = {}   # memberIndex -> {'index', 'hp', 'max', 'pos'} of the monsters sent to this client
    SESSION_SEQ[0] += 1; sid = SESSION_SEQ[0]
    # Server speaks first: protocol handshake offer 0x10005 (from ThumbConnection.Update):
    # bool reconnectEnabled, bool useHandshake, int64 serverKey, uint32 serverHSKey, uint32 clientHSKey
    use_hs = os.environ.get('STUB_HS', '0') == '1'
    reconn = os.environ.get('STUB_RECONN', '0') == '1'
    hs = cipher.encrypt(0x10004, w_bool(reconn) + w_bool(use_hs) + w_i64(1) + struct.pack('<II', 0, 0))
    log('[%s] send handshake offer (reconn=%s useHandshake=%s): %s' % (name, reconn, use_hs, hexdump(hs))); writer.write(hs); await writer.drain()
    if use_hs:
        cipher.key = 0   # client calls cipher.Initialize(serverHSKey ^ clientHSKey) = 0 after a handshake offer
    else:
        # no handshake: mark the connection established right away (cancels the client's connect timeout, starts its ping timer)
        beg = cipher.encrypt(0x1000F, b'')
        log('[%s] send begin 0x1000F: %s' % (name, hexdump(beg))); writer.write(beg); await writer.drain()
    async def probe():
        await asyncio.sleep(4)
        try:
            ping = cipher.encrypt(0x10008, w_i64(int(time.time() * 1000)))
            log('[%s] probe: send ping syn 0x10008: %s' % (name, hexdump(ping))); writer.write(ping); await writer.drain()
        except Exception as e:
            log('[%s] probe failed %r' % (name, e))
    if os.environ.get('STUB_PROBE', '0') == '1':
        asyncio.ensure_future(probe())
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            buf += data
            rows, buf = cipher.decrypt_rows(buf)
            if any(r[0] in SECRET_CODES for r in rows):
                log('[%s] recv %d bytes (contains a sign-in row: not dumped)' % (name, len(data)))
            else:
                log('[%s] recv %d bytes: %s' % (name, len(data), hexdump(data)))
            for code, body, fr, ok in rows:
                if code is None:
                    log('[%s] undecodable bytes: %s' % (name, hexdump(body))); continue
                log('[%s] row code=0x%X frame=%s crc_ok=%s body=%s' % (name, code, fr, ok, '<hidden>' if code in SECRET_CODES else hexdump(body, 200)))
                reply = None
                if code == CQ_ServerGunList:
                    reply = frame(SA_ServerGunList, sa_server_gun_list(), cipher=cipher)
                elif code == CQ_RegionLogin:
                    # { string memberId; byte isDev; ... ; int loginKey } -> SA_RegionLogin { int error; long accountNo; string serverToken;
                    # int unregisterRemainSec; DateTime? banExpireTime; byte? lastWorld; byte? lastGun; DeviceResultType; bool isQAAccount }
                    mlen = struct.unpack_from('<H', body)[0]; member = body[2:2 + mlen].decode('utf-8', 'replace')
                    lkey = struct.unpack_from('<i', body, len(body) - 4)[0] if len(body) >= 4 else 0
                    sess = DB.session(member, key=lkey)
                    acc_row = DB.account(member)
                    if sess is None and acc_row is not None and acc_row['kind'] == 'password':
                        err, token = stubdb.ERR_PASSWORD, ''   # a password account must come through HTTP Login with the password
                    else:
                        err, token = (0, sess['token']) if sess else (sign_in(member, None, 'region-login'), 'guest-token')
                    log('[%s] region login %s... key %s -> %s' % (name, member[:8], 'ok' if sess else 'none', err))
                    body = w_i32(err) + w_i64(acc_row['id'] if acc_row else 1) + w_str(token) + w_i32(0) + b'\x00' + b'\x00' + b'\x00' + w_i8(0) + w_bool(False)
                    reply = frame(SA_RegionLogin, body, cipher=cipher)
                elif code == 0x9F001:   # ours: CQ_Register { string login; string password } -> SA 0x9F002 { int error }  (never logged)
                    ln = struct.unpack_from('<H', body)[0]; login_ = body[2:2 + ln].decode('utf-8', 'replace')
                    pl = struct.unpack_from('<H', body, 2 + ln)[0]; pw = body[4 + ln:4 + ln + pl].decode('utf-8', 'replace')
                    err = DB.create(login_, pw)
                    if err == 0:
                        STORE[login_] = {'heroes': [], 'country': 0}; save_store(STORE)
                    log('[%s] register %r -> %s' % (name, login_, stubdb.ERROR_TEXT.get(err, 'ok')))
                    reply = frame(0x9F002, w_i32(err), cipher=cipher)
                elif code == 0x9F003:   # ours: CQ_ChangePassword { string old; string new } -> SA 0x9F004 { int error }  (signed-in account)
                    ln = struct.unpack_from('<H', body)[0]; old = body[2:2 + ln].decode('utf-8', 'replace')
                    pl = struct.unpack_from('<H', body, 2 + ln)[0]; new = body[4 + ln:4 + ln + pl].decode('utf-8', 'replace')
                    member_ = account.get('_login')
                    if not member_:
                        err = stubdb.ERR_NO_ACCOUNT
                    else:
                        row = DB.account(member_)
                        err = DB.check(member_, old)[0] if row and row['kind'] == 'password' else 0   # a device account sets its first password
                        if err == 0:
                            err = DB.set_password(member_, new)
                    log('[%s] change password -> %s' % (name, stubdb.ERROR_TEXT.get(err, 'ok')))
                    reply = frame(0x9F004, w_i32(err), cipher=cipher)
                elif code == 0x10005:
                    # client handshake reply: string clientKey, bool reconnecting, int64 a, int64 b [, int64 serverKey, int64 serial]
                    ln = struct.unpack_from('<H', body)[0]; ck = body[2:2 + ln].decode('utf-8', 'replace'); rest = body[2 + ln:]
                    log('[%s] client handshake reply: clientKey=%r rest=%s' % (name, ck, rest.hex()))
                    a = struct.unpack_from('<q', rest, 1)[0] if len(rest) >= 9 else 0
                    # handshake ack 0x10006: int32 error, int64 serverKey, uint32 serial, int64 serverTick, int64 clientTick(echo)
                    reply = frame(0x10006, w_i32(0) + w_i64(1) + struct.pack('<I', 0) + w_i64(int(time.time() * 1000)) + w_i64(a), cipher=cipher)
                elif code == 0x10008:   # ping syn from client: echo as ping ack 0x10009 with (clientTick, serverTick)
                    reply = run_injects(cipher, name)
                    t = struct.unpack_from('<q', body)[0] if len(body) >= 8 else 0
                    reply += frame(0x10009, w_i64(t) + w_i64(server_tick()), cipher=cipher)
                elif code == 0x1000A:   # client "disconnect" notice (ThumbConnection.Disconnect): the real server closes the socket
                    log('[%s] client sent disconnect notice 0x1000A: closing' % name)
                    await writer.drain(); writer.close(); return
                elif code == 0x80110:   # CQ_CharacterListAll -> SA_CharacterListAll { int error; WorldHeroInfo[] infos }
                    hero_index = int(os.environ.get('STUB_HERO_INDEX', '1010101'))
                    # WorldHeroInfo: byte world, byte gun, long heroNo, long userNo, string name, int heroIndex, int level, Nullable<DateTime> lastLogin
                    hero = w_i8(1) + w_i8(1) + w_i64(1) + w_i64(1) + w_str('StubHero') + w_i32(hero_index) + w_i32(1) + b'\x00'
                    reply = frame(0x80111, w_i32(0) + w_i32(1) + hero, cipher=cipher)
                elif code == 0x80107:   # CQ_RegistLoginWaitQueue {byte world, byte gun} -> SA {int error, long seq, long cur} + SN_TryGameServerLogin
                    reply = frame(0x80108, w_i32(0) + w_i64(1) + w_i64(1), cipher=cipher)
                    reply += frame(0x8010A, w_i8(body[0] if body else 1) + w_i8(body[1] if len(body) > 1 else 1), cipher=cipher)
                elif code == 0x80112:   # CQ_RegistDevice -> SA_RegistDevice {int error, bool isCertified}
                    reply = frame(0x80113, w_i32(0) + w_bool(True), cipher=cipher)
                elif code == 0x8011C:   # CQ_CheckRegistDevice -> SA_CheckRegistDevice (guess: int error, bool)
                    reply = frame(0x8011D, w_i32(0) + w_bool(True), cipher=cipher)
                elif code == 0x90111:   # CQ_HeroList -> SA_HeroList { int nError; HeroInfo[] heroInfos; byte heroCreateLimitCount }
                    reply = frame(0x90112, w_i32(0) + w_arr([hero_info(h) for h in heroes]) + w_i8(3), cipher=cipher)
                elif code == 0x90113:   # CQ_CreateHero { int heroIndex; string heroName } -> SA_CreateHero { int error; Nullable<HeroInfo> }
                    hidx = struct.unpack_from('<i', body)[0]; ln = struct.unpack_from('<H', body, 4)[0]; hname = body[6:6 + ln].decode('utf-8', 'replace')
                    log('[%s] create hero index=%d name=%r' % (name, hidx, hname))
                    if not account or DB.hero_name_taken(hname) or not (2 <= len(hname) <= 16):
                        log('[%s] create hero refused: %s' % (name, 'not signed in' if not account else 'name taken or invalid'))
                        reply = frame(0x90114, w_i32(stubdb.ERR_TAKEN) + w_bool(False), cipher=cipher)
                    else:
                        h = {'no': fid(hname), 'index': hidx, 'name': hname, 'level': 1}; heroes.append(h); save_store(STORE)
                        reply = frame(0x90114, w_i32(0) + w_bool(True) + hero_info(h), cipher=cipher)
                elif code == 0x90117:   # CQ_RegistCountry { byte countryType } -> SA_RegistCountry { int error; byte countryType; DateTime lastUpdateTime }
                    ct = body[0] if body else 0
                    reply = frame(0x90118, w_i32(0) + w_i8(ct) + w_dt(), cipher=cipher)
                    account['country'] = ct; save_store(STORE)
                elif code == 0x91807:   # CQ_ChatOption { byte hideSenderInfo } -> SA_ChatOption { int error; byte hideSenderInfo }
                    reply = frame(0x91808, w_i32(0) + w_i8(body[0] if body else 0), cipher=cipher)
                elif code == 0x90131:   # CQ_SelectHero { long selectHeroNo; long clientTick }
                    sel, ctick = struct.unpack_from('<qq', body)
                    h = next((x for x in heroes if x['no'] == sel), None) or (heroes[0] if heroes else {'no': sel, 'index': 1010101, 'name': 'StubHero', 'level': 1})
                    now = int(time.time() * 1000)
                    log('[%s] select hero no=%d -> %r' % (name, sel, h))
                    cur_hero = h; account.setdefault('bag', []); account.setdefault('serial', 100)
                    if h.get('map') and h.get('pos') and os.environ.get('STUB_MAP') is None:   # the hero continues where it left (STUB_MAP forces the start map)
                        world['map'] = int(h['map']); world['spawn'] = tuple(h['pos']); hero['pos'] = world['spawn']
                    if h.get('hp') and not h.get('dead'):
                        hero['hp'] = min(hero['maxhp'], int(h['hp'])); hero['mp'] = int(h.get('mp', 500))
                    # SA_SelectHero { int error; long userNo; long heroNo; long clientTick; long serverTick; bool isAway; Nullable<DateTime> banExpireTime }
                    reply = frame(0x90132, w_i32(0) + w_i64(1) + w_i64(h['no']) + w_i64(ctick) + w_i64(server_tick()) + w_bool(False) + w_bool(False), cipher=cipher)
                    reply += frame(0x90133, sn_hero_data(h), cipher=cipher)                     # SN_HeroData
                    # starter weapon: bag item serial 1 = WEAPON, worn in place Weapon (1).
                    # AddItem { ItemInfo { long serial; int itemIndex; int count; byte enchant; byte specialAbilityCount; byte socketCount; byte lockFlag; Nullable<DateTime> periodEnd; int nRemain }; AbilityInfo[]; AbilityInfo[]; SocketSlotInfo[] }
                    item = w_i64(1) + w_i32(WEAPON) + w_i32(1) + w_i8(0) + w_i8(0) + w_i8(0) + w_i8(0) + w_bool(False) + w_i32(0) + w_i32(0) + w_i32(0) + w_i32(0)
                    # class item (item910101 = HCharacterClassDataRecord: UnitIndex 910101 = the body prefab, stats, MoveSpeed 10000): the client
                    # takes the hero's body unit from the suit slot CharacterClass (E_Place 26) -> without it no body and move speed 0.
                    cls_item = w_i64(2) + w_i32(h['index']) + w_i32(1) + w_i8(0) + w_i8(0) + w_i8(0) + w_i8(0) + w_bool(False) + w_i32(0) + w_i32(0) + w_i32(0) + w_i32(0)
                    # Class items are E_Type Illustration (6): GItemFactory refuses them in the bag; they are worn through the suit packet's
                    # SuitIllustrationItemInfo[] { byte place; int itemIndex } -> HIllustrationItemContents.ForceEquip(place, itemIndex).
                    bag_frames = [item] + [add_item(b['serial'], b['index'], b['count']) for b in account.get('bag', [])]
                    for part in chunks(bag_frames, 12):
                        reply += frame(0x90136, w_arr(part), cipher=cipher)                        # SN_ItemListInBag
                    suit = {1: 1}
                    suit.update((int(k), v) for k, v in account.get('suit', {}).items())
                    reply += frame(0x90134, w_arr([w_i8(k) + w_i64(v) for k, v in suit.items()]) + w_arr([w_i8(26) + w_i32(h['index'])]), cipher=cipher)   # SN_ItemListInSuit
                    abag = [add_item(b['serial'], b['index'], b['count']) for b in account.get('abag', [])]
                    for part in (chunks(abag, 12) if abag else [[]]):
                        reply += frame(0x90161, w_arr(part), cipher=cipher)                         # SN_ItemListInAccountBag (storage)
                    done = account.setdefault('quests_done', [])
                    if SKIP_TUTORIAL:
                        done += [q for q in TUTORIAL_QUESTS if q not in done]
                    reply += frame(0x90138, w_i64(86400000) + w_i32(0) + w_i32(0) + w_arr([w_i32(quest_group(q)) + w_i32(q) + w_bool(False) for q in done]), cipher=cipher)   # SN_QuestInfo { long dailyRemainMs; int; stProgress[]; stComplete[] }
                    reply += frame(0x90139, w_arr([w_i32(k) + w_i32(0) + w_i8(0) for k in SKILLS]), cipher=cipher)   # SN_SkillInfo { SkillLevelInfo[] { int skill; int autoUseTime; byte autoFlag } }
                    reply += frame(0x9013A, w_i32(0), cipher=cipher)                             # SN_ExpireInfo
                    account.setdefault('gold', START_GOLD)
                    reply += frame(0x9016B, w_parr(b''.join(w_i64(v) for v in (0, 0, account['gold'], 0, 0, 0, 0)), 8), cipher=cipher)   # SN_AccountGoods long[] by AccountGoodsType (2 = Gold)
                    reply += frame(0x90165, w_parr(b''), cipher=cipher)                          # SN_TutorialInfo int[]
                    reply += frame(0x9013F, b'', cipher=cipher)                                  # SN_DataEnd
                    # SN_MapMove 0x92732 { MapEnterInfo; WorldEnvironmentInfo; bool redMoon }: the server moves the hero into its map
                    # (SA_EnterStaticMap 0x92713 only checks its error field in the client: HDungeonReceiver -> ResultCode.Successed)
                    reply += frame(0x92732, map_enter_info(map_index=world['map'], pos=world['spawn']) + w_bool(False), cipher=cipher)
                elif code == 0x92714:   # CQ_MapLoadingComplete -> SA_MapLoadingComplete { int nError; bool redMoon; long serverTick; long serverBaseTimeTicks }
                    now = int(time.time() * 1000)
                    reply = frame(0x92715, w_i32(0) + w_bool(False) + w_i64(server_tick()) + w_i64(base_ticks()), cipher=cipher)
                    h = heroes[0] if heroes else {'no': 1, 'index': 1010101, 'name': 'StubHero', 'level': 1}
                    x, y, z = world['spawn']
                    # SN_Enter (hero) 0x91111 { uint memberIndex; int heroFlyIndex; long heroNo; string nickName; int level; float x,y,z; short direction; byte gunIndex }
                    reply += frame(0x91111, w_u32(1) + w_i32(h['index']) + w_i64(h['no']) + w_str(h['name']) + w_i32(h['level'])
                                   + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_i8(1), cipher=cipher)
                    # SN_Shape 0x91102: the hero's look. Without it the client draws only the name plate (no body).
                    # { uint memberIndex; int heroFlyIndex; ShapeInfo; RelationSource; ClanInfo_mini; bool isPKJustifiable; ExtendedShapeInfo{byte}; int titleIndex; byte countryType }
                    shape = w_i32(WEAPON) + w_i8(0) + w_i32(0) + w_i32(h["index"]) + w_i8(AREA_TYPE) + w_i32(0)   # ShapeInfo: weapon, enchant, pet, class, areaType, wing
                    # RelationSource: byte group (1 Hero), int mapType (1 StaticMap), byte danger, byte propensity (4 Neutral), long key, Nullable<long> party, Nullable<long> clan, Nullable<byte> gun
                    rel = w_i8(1) + w_i32(1) + w_i8(0) + w_i8(4) + w_i64(h['no']) + w_bool(False) + w_bool(False) + w_bool(True) + w_i8(1)
                    clan = clan_mini_blob(h['no'])
                    reply += frame(0x91102, w_u32(1) + w_i32(h['index']) + shape + rel + clan + w_bool(False) + w_i8(0) + w_i32(0) + w_i8(6), cipher=cipher)
                    reply += frame(0x91103, w_u32(1) + w_i32(0) + w_i32(0), cipher=cipher)   # SN_Status: no buffs / crowd control
                    stand = w_i8(1) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_u32(1)
                    reply += frame(0x92101, w_i32(4 + len(stand)) + stand, cipher=cipher)       # SN_Action: Stand (server-side first action)
                    ms = map_state(world['map'], y); monsters = ms['monsters']; field_items = ms['items']
                    if world['map'] in INSTANCES and not INSTANCES[world['map']]['started']:
                        inst = INSTANCES[world['map']]; inst['started'] = True; inst['start'] = time.time(); inst['need'] = min(inst['need'], max(1, len(ms['monsters'])))
                        reply += mission_frames(cipher, inst); asyncio.ensure_future(dungeon_timer(world['map']))
                        log('[dungeon] instance %d started: kill %d of %d monsters within %d s' % (world['map'], inst['need'], len(ms['monsters']), inst['limit']))
                    reply += world_population(cipher, y, monsters=False, map_index=world['map'])
                    # other players: register this session in its map, show the others to me and me to them
                    leave_peers(sid)
                    SESSIONS[sid] = {'writer': writer, 'cipher': cipher, 'map': world['map'], 'h': h, 'hero': hero, 'name': name, 'account': account}
                    if DB.clanner(h['no']):
                        DB.clan_touch(h['no'], h['level']); send_clan(DB.clanner(h['no'])['clan_no'], 0xB0112, w_i64(h['no']) + w_i64(1), exclude_sid=sid)   # SN_ClannerLogin { heroNo; entryServerKey }
                    for osid, o in peers_of(sid):
                        reply += hero_frames(cipher, peer_member(osid), o['h'], o['hero']['pos'])
                    send_peers(sid, lambda c, m: hero_frames(c, m, h, (x, y, z)))
                    log('[%s] session %d in map %d with %d others' % (name, sid, world['map'], len(peers_of(sid))))
                    if os.environ.get('STUB_MONSTERS', '1') != '0':
                        async def later_monsters(delay=float(os.environ.get('STUB_MON_DELAY', '2.5'))):
                            await asyncio.sleep(delay)
                            data = world_population(cipher, y, npcs=False, monsters=True, table=monsters, map_index=world['map'])
                            log('[%s] monsters sent after %.1fs: %d bytes' % (name, delay, len(data)))
                            writer.write(data); await writer.drain()
                        asyncio.ensure_future(later_monsters())
                    log('[%s] world population sent: %d bytes' % (name, len(reply)))
                elif code == 0x92201:   # CN_PCSkill { SkillAction } (E_Action 3)
                    # SkillAction = ActionBase + int skillIndex; byte skillLevel; uint skillUid; long endTick; byte byUsePos; float x,y,z;
                    #               byte impactIndex; uint targetActorIndex; int attackSpeed
                    blob = body        # offsets below include the 4-byte blob length prefix (kind at 4, skill fields from 31)
                    kind = blob[4]
                    skill_idx, skill_lv, skill_uid, end_tick, by_pos = struct.unpack_from('<iBIqB', blob, 31)
                    tx, ty, tz, impact, target, aspd = struct.unpack_from('<fffBIi', blob, 49)
                    log('[%s] PC skill: kind %d skill %d lv %d uid %d target member %d impact %d aspd %d' % (name, kind, skill_idx, skill_lv, skill_uid, target, impact, aspd))
                    sk = load_skill(skill_idx)
                    if sk['mp'] > hero['mp'] or sk['hp'] >= hero['hp']:
                        # SN_UseSkillError { int errorCode; int currentMP; int currentHP; int skillIndex }
                        reply = frame(0x92205, w_i32(1) + w_i32(hero['mp']) + w_i32(hero['hp']) + w_i32(skill_idx), cipher=cipher)
                        log('[%s] skill %d refused: mp %d < %d' % (name, skill_idx, hero['mp'], sk['mp']))
                        writer.write(reply); await writer.drain(); reply = b''
                        continue
                    hero['mp'] -= sk['mp']; hero['hp'] -= sk['hp']; hero['cast'] = time.time()
                    reply = frame(0x92101, body, cipher=cipher)                               # SN_Action: the same SkillAction
                    send_peers(sid, lambda c, m, bl=body: frame(0x92101, rewrite_member(bl, m), cipher=c))
                    reply += frame(0x92204, w_i32(hero['mp']) + w_i32(hero['hp']) + w_arr([]), cipher=cipher)   # SN_PCSkill { currentMP; currentHP; ItemInfo[] delInfos }
                    if sk['status']:
                        dur = status_duration(sk['status'])
                        # SN_StatusInsert { uint defender; int status; long startTick; long remainTick (ms); byte timeless; int deleteStatus; int hp; int mp }
                        reply += frame(0x92221, w_u32(1) + w_i32(sk['status']) + w_i64(server_tick()) + w_i64(dur * 1000) + w_i8(0) + w_i32(0) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=cipher)
                        log('[%s] buff %d for %d s' % (name, sk['status'], dur))
                        async def expire(st=sk['status'], d=dur):
                            await asyncio.sleep(d)
                            if not writer.is_closing():
                                writer.write(frame(0x92222, w_u32(1) + w_parr(w_i32(st), 4), cipher=cipher)); await writer.drain()   # SN_StatusDelete { uint defender; int[] status }
                        asyncio.ensure_future(expire())
                    mon = monsters.get(target)
                    if mon and mon['hp'] > 0:
                        dmg = int(os.environ.get('STUB_DAMAGE', '4')) if skill_idx == 1010000 else SKILL_DAMAGE
                        mon['hp'] = max(0, mon['hp'] - dmg)
                        if mon['hp'] > 0:
                            # SN_Damage { uint attacker; uint defender; int normalDamage; int currentHP } to everybody in the map
                            reply += frame(0x92211, w_u32(1) + w_u32(target) + w_i32(dmg) + w_i32(mon['hp']), cipher=cipher)
                            broadcast(world['map'], lambda c, v, t=target, d=dmg, hp=mon['hp']: frame(0x92211, w_u32(peer_member(sid)) + w_u32(t) + w_i32(d) + w_i32(hp), cipher=c), exclude=sid)
                        else:
                            # SN_Dead { uint attacker; uint defender; int normalDamage; Nullable<int> attackerPropensityPoint; ActionBase stDefenderAction (Dead); float skillTargetPos x,y,z }
                            x, y, z = mon['pos']
                            dead = w_i8(4) + w_i64(server_tick()) + w_f32(x) + w_f32(y) + w_f32(z) + w_i16(0) + w_u32(target) + w_i32(0)   # Dead: E_DeadType Normal (int)
                            reply += frame(0x92212, w_u32(1) + w_u32(target) + w_i32(dmg) + w_bool(False) + w_i32(4 + len(dead)) + dead
                                           + w_f32(x) + w_f32(y) + w_f32(z), cipher=cipher)
                            broadcast(world['map'], lambda c, v, t=target, d=dmg, dead=dead, x=x, y=y, z=z: frame(0x92212, w_u32(peer_member(sid)) + w_u32(t) + w_i32(d) + w_bool(False) + w_i32(4 + len(dead)) + dead
                                                                                                     + w_f32(x) + w_f32(y) + w_f32(z), cipher=c), exclude=sid)
                            if mon.get('boss'):
                                reply += boss_killed(mon, sid, account, cur_hero['name'] if cur_hero else h['name'])
                            elif world['map'] in INSTANCES:
                                dungeon_kill(world['map'])
                            else:
                                asyncio.ensure_future(respawn_monster(world['map'], mon))
                            for qn, qq in quests.items():
                                tk = qq.get('task')
                                if tk and tk['type'] == 'MonsterKill' and tk['index'] == mon['index'] and qq['count'] < tk['count']:
                                    qq['count'] += 1; qq['state'] = 2 if qq['count'] >= tk['count'] else 1
                                    # SN_Update { stTaskUpdate[] { int QuestNo; int QuestIndex; int TaskCount; byte E_STATE } }
                                    reply += frame(0xA0922, w_arr([w_i32(qn) + w_i32(qq['index']) + w_i32(qq['count']) + w_i8(qq['state'])]), cipher=cipher)
                                    log('[%s] kill quest %d: %d/%d' % (name, qn, qq['count'], tk['count']))
                            if cur_hero is not None and MON_EXP:
                                reply += gain_exp(cipher, cur_hero, hero, MON_EXP); save_store(STORE)
                            if DROP_ITEM:
                                ms = MAPS[world['map']]; ms['seq'][0] += 1
                                fm = ms['seq'][0]
                                owner = cur_hero['no'] if cur_hero else 1
                                field_items[fm] = {'index': DROP_ITEM, 'count': 1, 'pos': (x, y, z), 'owner': owner}
                                # SN_Enter NSyncItem { ActionBase FieldItemOwnership }: the drop lies where the monster died, owned by the killer; everybody sees it
                                reply += frame(0x91301, action_field_item(fm, x, y, z, DROP_ITEM, 1, owner), cipher=cipher)
                                broadcast(world['map'], lambda c, v, fm=fm, x=x, y=y, z=z, owner=owner: frame(0x91301, action_field_item(fm, x, y, z, DROP_ITEM, 1, owner), cipher=c), exclude=sid)
                        log('[%s] monster %d hp %d/%d' % (name, target, mon['hp'], mon['max']))
                elif code == 0xA1011:   # CQ_EquipItem { byte E_Place; long serial } -> SA_EquipItem { int error; byte place; long serial } + SN_ChangeSuit
                    place, serial = struct.unpack_from('<Bq', body)
                    account.setdefault('suit', {})[str(place)] = serial
                    save_store(STORE)
                    log('[%s] equip serial %d in place %d' % (name, serial, place))
                    reply = frame(0xA1012, w_i32(0) + w_i8(place) + w_i64(serial), cipher=cipher)
                    if cur_hero is not None:
                        weapon = WEAPON
                        if place == 1:
                            weapon = next((b['index'] for b in account.get('bag', []) if b['serial'] == serial), WEAPON)
                        shape = w_i32(weapon) + w_i8(0) + w_i32(0) + w_i32(cur_hero['index']) + w_i8(AREA_TYPE) + w_i32(0)
                        reply += frame(0xA1003, w_u32(1) + w_i32(cur_hero['index']) + shape + w_i32(0), cipher=cipher)   # SN_ChangeSuit { member; heroFlyIndex; ShapeInfo; titleIndex }
                elif code == 0xA1013:   # CQ_EquipItemRemove { byte E_Place } -> SA_EquipItemRemove { int error; byte place }
                    place = body[0]
                    account.setdefault('suit', {}).pop(str(place), None); save_store(STORE)
                    reply = frame(0xA1014, w_i32(0) + w_i8(place), cipher=cipher)
                elif code == 0x92411:   # CQ_ReviveSelf -> SA_ReviveSelf { int error; float x,y,z; ushort dir; int hp; int mp; long remainTick } + SN_Revive
                    hero['dead'] = False; hero['hp'] = hero['maxhp']; hero['mp'] = 500; hero['pos'] = world['spawn']; hero['hit'] = time.time()
                    if cur_hero is not None:
                        cur_hero['dead'] = False
                    broadcast(world['map'], lambda c, v: frame(0x92414, w_u32(peer_member(sid)) + w_f32(world['spawn'][0]) + w_f32(world['spawn'][1]) + w_f32(world['spawn'][2]) + w_u16(0) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=c), exclude=sid)
                    x, y, z = world['spawn']
                    log('[%s] revive at spawn' % name)
                    reply = frame(0x92412, w_i32(0) + w_f32(x) + w_f32(y) + w_f32(z) + w_u16(0) + w_i32(hero['hp']) + w_i32(hero['mp']) + w_i64(0), cipher=cipher)
                    reply += frame(0x92414, w_u32(1) + w_f32(x) + w_f32(y) + w_f32(z) + w_u16(0) + w_i32(hero['hp']) + w_i32(hero['mp']), cipher=cipher)
                elif code == 0x94401:   # CQ_PickUpItem { uint member; float x,y,z } -> SA_PickUpItem { int error; uint member } + SN_AddItem + SN_Leave
                    fm = struct.unpack_from('<I', body)[0]
                    fi = field_items.pop(fm, None)
                    log('[%s] pick up field item %d: %r' % (name, fm, fi))
                    reply = frame(0x94402, w_i32(0 if fi else 1) + w_u32(fm), cipher=cipher)
                    if not fi:
                        reply += frame(0x92105, w_i8(0) + w_parr(w_u32(fm), 4), cipher=cipher)
                    if fi:
                        b = bag_add(account, fi['index'], fi['count'])
                        save_store(STORE)
                        reply += frame(0xA0413, w_arr([add_item(b['serial'], b['index'], b['count'])]) + w_i32(0) + w_i64(0), cipher=cipher)
                        reply += frame(0x92105, w_i8(0) + w_parr(w_u32(fm), 4), cipher=cipher)   # SN_Leave { byte E_LeaveType Normal; uint[] members }: removes the field item
                        broadcast(world['map'], lambda c, v, fm=fm: frame(0x92105, w_i8(0) + w_parr(w_u32(fm), 4), cipher=c), exclude=sid)
                elif code == 0xE0141:   # NFriend CQ_Login { int check } -> SA_Login { int error; FriendHeroBase } + SN_AddFriend + SN_AddInvited + SN_Online
                    h = heroes[0] if heroes else {'no': 1, 'name': 'StubHero'}
                    friends = account.setdefault('friends', []); invited = account.setdefault('invited', [])
                    base = (w_i64(fid(h['name'])) + w_str(h['name']) + w_i32(len(friends)) + w_i32(0) + w_i32(len(invited)) + w_i32(0) + w_i32(0)
                            + w_i32(50) + w_i32(50) + w_i32(50) + w_i32(50) + w_i32(50) + w_dt())
                    reply = frame(0xE0142, w_i32(0) + base, cipher=cipher)
                    reply += event_boss_info(cipher)
                    reply += penalty_info(cipher, h) + attendance(cipher, account)
                    reply += frame(0xE0121, w_arr([friend_base(n) for n in friends]), cipher=cipher)          # SN_AddFriend { FriendBase[] }
                    reply += frame(0xE0124, w_arr([w_i64(fid(n)) + w_str(n) + w_dt() for n in invited]), cipher=cipher)   # SN_AddInvited { InviteInfo[] }
                    blocked = account.setdefault('blocked', []); hostile = account.setdefault('hostile', [])
                    if blocked:
                        reply += frame(0xE0125, w_arr([block_info(n) for n in blocked]), cipher=cipher)        # SN_AddBlock { BlockInfo[] }
                    if hostile:
                        reply += frame(0xE0134, w_arr([hostile_info(n) for n in hostile]), cipher=cipher)      # SN_AddHostile { HostileInfo[] }
                    for n in friends:
                        if session_of_hero(n) is not None:
                            reply += frame(0xE0131, friend_base(n), cipher=cipher)   # SN_Online { FriendBase }
                    log('[%s] friends: %r invited: %r' % (name, friends, invited))
                elif code == 0xE0109:   # CQ_HeroSearch { string name } -> SA { int error; long heroNo; string name; byte gun }
                    nl = struct.unpack_from('<H', body)[0]; target = body[2:2 + nl].decode('utf-8', 'replace')
                    tm, th = find_hero(target)
                    reply = frame(0xE010A, (w_i32(0) + w_i64(fid(target)) + w_str(target) + w_i8(1)) if th else (w_i32(1) + w_i64(0) + w_str(target) + w_i8(1)), cipher=cipher)
                elif code == 0xE0101:   # CQ_InvitingAdd { string heroName } -> SA_InvitingAdd { int error; InviteInfo } (+ SN_AddInvited to the target)
                    nl = struct.unpack_from('<H', body)[0]; target = body[2:2 + nl].decode('utf-8', 'replace')
                    h = heroes[0] if heroes else {'no': 1, 'name': 'StubHero'}
                    tm, th = find_hero(target)
                    if not th or target == h['name']:
                        reply = frame(0xE0102, w_i32(1) + w_i64(0) + w_str(target) + w_dt(), cipher=cipher)
                    else:
                        tacc = STORE[tm]
                        if h['name'] not in tacc.setdefault('invited', []):
                            tacc['invited'].append(h['name'])
                        save_store(STORE)
                        reply = frame(0xE0102, w_i32(0) + w_i64(fid(target)) + w_str(target) + w_dt(), cipher=cipher)
                        ts = session_of_hero(target)
                        if ts is not None:
                            send_session(ts, 0xE0124, w_arr([w_i64(fid(h['name'])) + w_str(h['name']) + w_dt()]))
                        log('[%s] friend invite %s -> %s' % (name, h['name'], target))
                elif code == 0xE0105:   # CQ_InvitedAccept { long heroNo } -> SA_InvitedAccept { int error; FriendBase } (+ SN_AddFriend / SN_Online to the other)
                    hn = struct.unpack_from('<q', body)[0]
                    h = heroes[0] if heroes else {'no': 1, 'name': 'StubHero'}
                    other = next((n for n in account.get('invited', []) if fid(n) == hn), None)
                    if other is None:
                        reply = frame(0xE0106, w_i32(1) + w_i64(hn) + w_str('') + w_dt(), cipher=cipher)
                    else:
                        account['invited'].remove(other)
                        if other not in account.setdefault('friends', []):
                            account['friends'].append(other)
                        om, oh = find_hero(other)
                        if om and h['name'] not in STORE[om].setdefault('friends', []):
                            STORE[om]['friends'].append(h['name'])
                        save_store(STORE)
                        reply = frame(0xE0106, w_i32(0) + friend_base(other), cipher=cipher)
                        reply += frame(0xE0131, friend_base(other), cipher=cipher) if session_of_hero(other) is not None else b''
                        os_ = session_of_hero(other)
                        if os_ is not None:
                            send_session(os_, 0xE0121, w_arr([friend_base(h['name'])]))
                            send_session(os_, 0xE0131, friend_base(h['name']))
                        log('[%s] %s and %s are friends' % (name, h['name'], other))
                elif code == 0xE0107:   # CQ_InvitedReject { long heroNo } -> SA { int error; long heroNo }
                    hn = struct.unpack_from('<q', body)[0]
                    account['invited'] = [n for n in account.get('invited', []) if fid(n) != hn]; save_store(STORE)
                    reply = frame(0xE0108, w_i32(0) + w_i64(hn), cipher=cipher)
                elif code in (0xE0113, 0xE0150):   # CQ_BlockAdd / CQ_HostileAdd { string heroName } -> SA { int error; BlockInfo / HostileInfo }
                    nl = struct.unpack_from('<H', body)[0]; target = body[2:2 + nl].decode('utf-8', 'replace')
                    tm, th = find_hero(target)
                    key = 'blocked' if code == 0xE0113 else 'hostile'
                    lst = account.setdefault(key, [])
                    err = 0 if th and target != h['name'] else 1
                    if err == 0 and target not in lst:
                        lst.append(target); save_store(STORE)
                    log('[%s] %s add %r -> err %d' % (name, key, target, err))
                    info = (block_info if code == 0xE0113 else hostile_info)(target if err == 0 else '')
                    reply = frame(code + 1, w_i32(err) + info, cipher=cipher)
                elif code in (0xE0115, 0xE0152):   # CQ_BlockDelete / CQ_HostileDelete { long heroNo } -> SA { int error; long heroNo }
                    hn = struct.unpack_from('<q', body)[0]
                    key = 'blocked' if code == 0xE0115 else 'hostile'
                    account[key] = [n for n in account.get(key, []) if fid(n) != hn]; save_store(STORE)
                    reply = frame(code + 1, w_i32(0) + w_i64(hn), cipher=cipher)
                elif code == 0xE0111:   # CQ_FriendDelete { long heroNo } -> SA { int error; long heroNo }
                    hn = struct.unpack_from('<q', body)[0]
                    account['friends'] = [n for n in account.get('friends', []) if fid(n) != hn]; save_store(STORE)
                    reply = frame(0xE0112, w_i32(0) + w_i64(hn), cipher=cipher)
                elif code == 0xC0105:   # CQ_MailCount -> SA { int error; int mailCount; int letterCount; bool newLetter }
                    letters = account.setdefault('letters', []); seed_mails(account)
                    reply = frame(0xC0106, w_i32(0) + w_i32(len(account.get('mails', []))) + w_i32(len(letters)) + w_bool(any(not l.get('read') for l in letters)), cipher=cipher)
                elif code == 0xC0101:   # CQ_MailList -> SA { int error; MailboxInfo[] } (the system mailbox: rewards to receive)
                    seed_mails(account)
                    reply = frame(0xC0102, w_i32(0) + w_arr([mailbox_info(m) for m in account.get('mails', [])[-12:]]), cipher=cipher)
                elif code == 0xC0103:   # CQ_ReceiveMail { long mailNo; byte type; int rewardIndex; int rewardValue; int rewardSubValue } -> SA { int error; long mailNo }
                    mno = struct.unpack_from('<q', body)[0]
                    m = next((x for x in account.get('mails', []) if x['no'] == mno), None)
                    reply = frame(0xC0104, w_i32(0 if m else 1) + w_i64(mno), cipher=cipher)
                    if m:
                        reply += receive_mail(cipher, account, hero, name, m)
                elif code == 0xC0130:   # CQ_MultiReceiveMail { RecvMailInfo[] { long no; byte type; int index; int value } } -> SA { int error; long[] received }
                    n = struct.unpack_from('<i', body)[0]; got = []; extra = b''
                    for i in range(n):
                        mno = struct.unpack_from('<q', body, 4 + 17 * i)[0]
                        m = next((x for x in account.get('mails', []) if x['no'] == mno), None)
                        if m:
                            extra += receive_mail(cipher, account, hero, name, m); got.append(mno)
                    reply = frame(0xC0131, w_i32(0) + w_parr(b''.join(w_i64(x) for x in got), 8), cipher=cipher) + extra
                elif code == 0xC0121:   # CQ_LetterList { byte type; long last } -> SA { int error; LetterInfo[] }
                    letters = account.setdefault('letters', [])
                    reply = frame(0xC0122, w_i32(0) + w_arr([letter_info(l) for l in letters[-12:]]), cipher=cipher)
                elif code == 0xC0123:   # CQ_SendLetter { string target; string title; string msg } -> SA { int error; int sendCount; DateTime }
                    o = 0
                    vals = []
                    for _ in range(3):
                        ln = struct.unpack_from('<H', body, o)[0]; vals.append(body[o + 2:o + 2 + ln].decode('utf-8', 'replace')); o += 2 + ln
                    target, title, msg = vals
                    h = heroes[0] if heroes else {'no': 1, 'name': 'StubHero'}
                    tm, th = find_hero(target)
                    if not th:
                        reply = frame(0xC0124, w_i32(1) + w_i32(0) + w_dt(), cipher=cipher)
                    else:
                        tl = STORE[tm].setdefault('letters', [])
                        tl.append({'no': int(time.time() * 1000) % 1000000000, 'from': h['name'], 'time': int(time.time()), 'title': title, 'msg': msg, 'read': False})
                        save_store(STORE)
                        reply = frame(0xC0124, w_i32(0) + w_i32(1) + w_dt(), cipher=cipher)
                        log('[%s] letter %s -> %s: %r' % (name, h['name'], target, title))
                elif code == 0xC0129:   # CN_UpdateLetterReadFlag { long letterNo }
                    ln_ = struct.unpack_from('<q', body)[0]
                    for l in account.get('letters', []):
                        if l['no'] == ln_:
                            l['read'] = True
                    save_store(STORE)
                elif code == 0xC0125:   # CQ_DeleteLetter { long letterNo } -> SA { int error; long letterNo }
                    ln_ = struct.unpack_from('<q', body)[0]
                    account['letters'] = [l for l in account.get('letters', []) if l['no'] != ln_]; save_store(STORE)
                    reply = frame(0xC0126, w_i32(0) + w_i64(ln_), cipher=cipher)
                elif code == 0x93801:   # CN_CreateParty { byte limit; byte lootOption } -> SN_CreateParty { int error; long partyIndex; byte limit; byte loot; PartyMemberInfo master }
                    limit, loot = struct.unpack_from('<BB', body)
                    pi, pt = party_of(sid)
                    if pt:
                        reply = frame(0x93802, w_i32(1) + w_i64(pi) + w_i8(pt['limit']) + w_i8(pt['loot']) + party_member_info(sid, 0, True), cipher=cipher)
                    else:
                        PARTY_SEQ[0] += 1; pi = PARTY_SEQ[0]
                        PARTIES[pi] = {'master': sid, 'members': [sid], 'limit': limit or 5, 'loot': loot}
                        reply = frame(0x93802, w_i32(0) + w_i64(pi) + w_i8(limit or 5) + w_i8(loot) + party_member_info(sid, 0, True), cipher=cipher)
                        log('[%s] party %d created' % (name, pi))
                elif code == 0x93821:   # CN_InviteParty { byte type (0 member, 1 charNo, 2 nick); uint member; long charNo; string nick }
                    itype, member, char_no = struct.unpack_from('<BIq', body)
                    nl = struct.unpack_from('<H', body, 13)[0]; nick = body[15:15 + nl].decode('utf-8', 'replace')
                    target = None
                    for osid, o in SESSIONS.items():
                        if osid == sid:
                            continue
                        if (itype == 0 and peer_member(osid) == member) or (itype == 1 and o['h']['no'] == char_no) or (itype == 2 and o['h']['name'] == nick) or (itype == 0 and member == 0 and o['h']['name'] == nick):
                            target = osid; break
                    pi, pt = party_of(sid)
                    if not pt:   # the game creates the party with the first invite
                        PARTY_SEQ[0] += 1; pi = PARTY_SEQ[0]
                        PARTIES[pi] = pt = {'master': sid, 'members': [sid], 'limit': 5, 'loot': 0}
                        reply = frame(0x93802, w_i32(0) + w_i64(pi) + w_i8(5) + w_i8(0) + party_member_info(sid, 0, True), cipher=cipher)
                    else:
                        reply = b''
                    if target is None:
                        reply += frame(0x93822, w_i32(1) + party_invite_info(sid), cipher=cipher)   # SN_InviteParty error: no such player
                        log('[%s] party invite: target not found (%d %d %d %r)' % (name, itype, member, char_no, nick))
                    else:
                        reply += frame(0x93822, w_i32(0) + party_invite_info(target), cipher=cipher)
                        send_session(target, 0x93823, w_i64(pi) + party_invite_info(sid))   # SN_InvitePartyRequest { long partyIndex; PartyInviteInfo master }
                        pt.setdefault('invited', set()).add(target)
                        log('[%s] party %d invites session %d' % (name, pi, target))
                elif code == 0x93824:   # CN_InvitePartyResponse { long partyIndex; byte InviteResult (0 accept) }
                    pi, res = struct.unpack_from('<qB', body)
                    pt = PARTIES.get(pi)
                    if pt and res == 0 and sid not in pt['members'] and len(pt['members']) < pt['limit']:
                        party_leave(sid)
                        pt['members'].append(sid)
                        # SN_InvitePartyResponse { int error; long partyIndex; byte limit; byte loot; PartyMemberInfo[] } to me; SN_InvitePartyNotify { PartyMemberInfo } to the rest
                        reply = frame(0x93825, w_i32(0) + w_i64(pi) + w_i8(pt['limit']) + w_i8(pt['loot']) + party_members_blob(pt), cipher=cipher)
                        mine = party_member_info(sid, len(pt['members']) - 1, False)
                        for m in pt['members']:
                            if m != sid:
                                send_session(m, 0x93826, mine)
                        log('[%s] joined party %d (%d members)' % (name, pi, len(pt['members'])))
                    elif pt:
                        reply = frame(0x93825, w_i32(1) + w_i64(pi) + w_i8(pt['limit']) + w_i8(pt['loot']) + w_arr([]), cipher=cipher)
                        me = SESSIONS.get(sid)
                        send_session(pt['master'], 0x9382A, w_i64(me['h']['no'] if me else 0))   # SN_InvitePartyRejectNotify { long targetCharNo }
                    else:
                        reply = frame(0x93825, w_i32(2) + w_i64(pi) + w_i8(0) + w_i8(0) + w_arr([]), cipher=cipher)
                elif code == 0x93811:   # CN_UpdatePartyOption { byte limit; byte LootOption } -> SN_UpdatePartyOption { int error } + SN_UpdatePartyOptionNotify { limit; loot } to all
                    limit, loot = struct.unpack_from('<BB', body)
                    pi, pt = party_of(sid)
                    if pt and pt['master'] == sid and 2 <= limit <= 5 and limit >= len(pt['members']):
                        pt['limit'] = limit; pt['loot'] = loot
                        reply = frame(0x93812, w_i32(0), cipher=cipher)
                        for m in pt['members']:
                            send_session(m, 0x93813, w_i8(limit) + w_i8(loot))
                        log('[%s] party %d options: limit %d loot %d' % (name, pi, limit, loot))
                    else:
                        reply = frame(0x93812, w_i32(1), cipher=cipher)
                elif code == 0x93841:   # CN_KickPartyMember { long charNo } -> SN_KickPartyMember { int error } + SN_KickPartyMemberNotify { long } to all (the kicked one included)
                    kick_no = struct.unpack_from('<q', body)[0]
                    pi, pt = party_of(sid)
                    victim = next((m for m in (pt['members'] if pt else []) if m in SESSIONS and SESSIONS[m]['h']['no'] == kick_no), None)
                    if pt and pt['master'] == sid and victim is not None and victim != sid:
                        for m in pt['members']:
                            send_session(m, 0x93843, w_i64(kick_no))
                        party_leave(victim, kicked=True)
                        reply = frame(0x93842, w_i32(0), cipher=cipher)
                        log('[%s] party %d kicked charNo %d' % (name, pi, kick_no))
                    else:
                        reply = frame(0x93842, w_i32(1), cipher=cipher)
                elif code == 0x93851:   # CN_ChangePartyMaster { long charNo } -> SN_ChangePartyMaster { int error } + SN_ChangePartyMasterNotify { long } to all
                    new_no = struct.unpack_from('<q', body)[0]
                    pi, pt = party_of(sid)
                    new_sid = next((m for m in (pt['members'] if pt else []) if m in SESSIONS and SESSIONS[m]['h']['no'] == new_no), None)
                    if pt and pt['master'] == sid and new_sid is not None:
                        pt['master'] = new_sid
                        reply = frame(0x93852, w_i32(0), cipher=cipher)
                        for m in pt['members']:
                            send_session(m, 0x93853, w_i64(new_no))
                        log('[%s] party %d master -> charNo %d' % (name, pi, new_no))
                    else:
                        reply = frame(0x93852, w_i32(1), cipher=cipher)
                elif code == 0x93831:   # CN_LeaveParty -> SN_LeaveParty { int error } (+ SN_LeavePartyNotify to the others)
                    party_leave(sid)
                    reply = frame(0x93832, w_i32(0), cipher=cipher)
                elif code == 0x91801:   # CN_Chat { int chatType; byte extendInfoType; string targetName; string message }
                    ctype, ext = struct.unpack_from('<iB', body)
                    o = 5
                    tl = struct.unpack_from('<H', body, o)[0]; tname = body[o + 2:o + 2 + tl].decode('utf-8', 'replace'); o += 2 + tl
                    ml = struct.unpack_from('<H', body, o)[0]; msg = body[o + 2:o + 2 + ml].decode('utf-8', 'replace')
                    log('[%s] chat type %d to %r: %r' % (name, ctype, tname, msg))
                    h = heroes[0] if heroes else {'no': 1, 'name': 'StubHero'}
                    # SN_Chat { int chatType; byte extendInfoType; long senderCharNo; string senderName; string message; int senderMemberIndex;
                    #           int pveRank; byte gunIndex; int siegeIndex; ExtendedShapeInfo { byte }; long messageId }
                    def sn_chat(ct, sender_no, sender, text, member):
                        return frame(0x91802, w_i32(ct) + w_i8(0) + w_i64(sender_no) + w_str(sender) + w_str(text) + w_i32(member) + w_i32(0) + w_i8(1) + w_i32(0) + w_i8(0) + w_i64(int(time.time() * 1000)), cipher=cipher)
                    reply = sn_chat(ctype, h['no'], h['name'], msg, 1)
                    if ctype in (1, 2, 4, 8):
                        for osid, o in list(SESSIONS.items()):
                            if osid != sid and not o['writer'].is_closing() and (ctype != 2 or o['map'] == world['map']) and (ctype != 4 or (party_of(sid)[1] and osid in party_of(sid)[1]['members'])) and (ctype != 8 or osid in clan_sessions(DB.clanner(h['no'])['clan_no'] if DB.clanner(h['no']) else -1)):
                                o['writer'].write(frame(0x91802, w_i32(ctype) + w_i8(0) + w_i64(h['no']) + w_str(h['name']) + w_str(msg) + w_i32(peer_member(sid)) + w_i32(0) + w_i8(1) + w_i32(0) + w_i8(0) + w_i64(int(time.time() * 1000)), cipher=o['cipher']))
                    if msg.startswith('/stun'):   # test: stun the hero for 3 s
                        hx, hy, hz = hero['pos']
                        reply += crowd_control(cipher, 0, 1, 0, hero['hp'], hx, hy, hz, ms=3000)
                    elif msg.startswith('/'):
                        reply += sn_chat(32, 0, 'Server', 'stub: unknown command %s' % msg.split()[0], 0)   # System
                    elif ctype == 16:
                        reply += sn_chat(16, 0, tname or 'Nobody', '(%s is not online on the stub)' % (tname or '?'), 0)
                elif code == 0x93D01:   # CQ_StatUp { int str, dex, int, con, agi, wis } (points to add) -> SA_StatUp { int error; 6 stats; remain; levelStatPoint; etcStatPoint }
                    adds = struct.unpack_from('<6i', body)
                    h = heroes[0] if heroes else None
                    st = hero_stats(h) if h else hero_stats({})
                    total = sum(max(0, a) for a in adds)
                    err = 0 if h and total <= st['points'] and all(a >= 0 for a in adds) else 1
                    if err == 0:
                        for k, a in zip(('str', 'dex', 'int', 'con', 'agi', 'wis'), adds):
                            st[k] += a
                        st['points'] -= total
                        hero['maxhp'] += adds[3] * 10; hero['hp'] = min(hero['hp'] + adds[3] * 10, hero['maxhp'])
                        save_store(STORE)
                    log('[%s] stat up %r -> %r (err %d)' % (name, adds, st, err))
                    reply = frame(0x93D02, w_i32(err) + w_i32(st['str']) + w_i32(st['dex']) + w_i32(st['int']) + w_i32(st['con']) + w_i32(st['agi']) + w_i32(st['wis']) + w_i32(st['points']) + w_i32(st['levelpt']) + w_i32(0), cipher=cipher)
                elif code == 0x93605 and cur_hero is not None:   # CQ_CreateInstanceDungeon { int instanceDungeonFlyIndex (= the dungeon map) } -> SA { error; DeniedInfo[]; flyIndex; MapEnterInfo; env } + SN_MapMove
                    dmap = struct.unpack_from('<i', body)[0]
                    if dmap not in DUNGEON_MAPS or world['map'] in INSTANCES:
                        reply = frame(0x93606, w_i32(1) + w_i32(0) + w_i32(dmap) + map_enter_info(hero['hp'], hero['mp'], map_index=real_map(world['map']), pos=hero['pos']), cipher=cipher)
                    else:
                        key = INSTANCE_BASE + sid
                        INSTANCES[key] = {'map': dmap, 'owner': sid, 'return': (world['map'], tuple(hero['pos'])), 'kills': 0, 'need': DUNGEON_KILLS, 'limit': DUNGEON_LIMIT, 'start': 0, 'started': False, 'done': False}
                        px, py, pz = dungeon_spawn(dmap)
                        leave_peers(sid)
                        MAPS.pop(key, None)
                        world['map'] = key; world['spawn'] = (px, py, pz); hero['pos'] = (px, py, pz)
                        reply = frame(0x93606, w_i32(0) + w_i32(0) + w_i32(dmap) + map_enter_info(hero['hp'], hero['mp'], map_index=dmap, pos=(px, py, pz)), cipher=cipher)
                        reply += frame(0x92732, map_enter_info(hero['hp'], hero['mp'], map_index=dmap, pos=(px, py, pz)) + w_bool(False), cipher=cipher)
                        log('[%s] enters dungeon %s as instance %d' % (name, DUNGEON_MAPS[dmap], key))
                elif code == 0x93601 and cur_hero is not None:   # CQ_EnterInstanceDungeon { long matchKey (= instance key); int mapIndex }: join a running instance (party mates)
                    key, dmap = struct.unpack_from('<qi', body)
                    inst = INSTANCES.get(key)
                    if inst is None or inst['done'] or world['map'] in INSTANCES:
                        reply = frame(0x93602, w_i32(1), cipher=cipher)
                    else:
                        px, py, pz = dungeon_spawn(inst['map'])
                        inst.setdefault('returns', {})[sid] = (world['map'], tuple(hero['pos']))
                        leave_peers(sid); world['map'] = key; world['spawn'] = (px, py, pz); hero['pos'] = (px, py, pz)
                        reply = frame(0x93602, w_i32(0), cipher=cipher)
                        reply += frame(0x92732, map_enter_info(hero['hp'], hero['mp'], map_index=inst['map'], pos=(px, py, pz)) + w_bool(False), cipher=cipher)
                elif code in (0x93607, 0x93603) and cur_hero is not None:   # CQ_LeaveToStaticMap / CQ_LeaveInstanceDungeon -> SA { error } + SN_MapMove back to where the hero came from
                    inst = INSTANCES.get(world['map'])
                    sa = 0x93608 if code == 0x93607 else 0x93604
                    if inst is None:
                        reply = frame(sa, w_i32(1), cipher=cipher)
                    else:
                        back_map, back_pos = inst.get('returns', {}).get(sid, inst['return'])
                        key = world['map']
                        leave_peers(sid)
                        world['map'] = back_map; world['spawn'] = back_pos; hero['pos'] = back_pos
                        cur_hero['map'] = back_map; cur_hero['pos'] = list(back_pos); save_store(STORE)
                        reply = frame(sa, w_i32(0), cipher=cipher)
                        reply += frame(0x93622, b'', cipher=cipher)   # SN_LeaveInstanceDungeon
                        reply += frame(0x92732, map_enter_info(hero['hp'], hero['mp'], map_index=back_map, pos=back_pos) + w_bool(False), cipher=cipher)
                        if not sessions_in(key):
                            INSTANCES.pop(key, None); MAPS.pop(key, None)
                        log('[%s] leaves the dungeon back to map %d' % (name, back_map))
                elif code in (0x92701, 0x92718):   # CQ_Portal / CQ_WorldMapPortal { int portalIndex } -> SA { int error; MapEnterInfo; WorldEnvironmentInfo } (+ SN_MapMove for another map)
                    pidx = struct.unpack_from('<i', body)[0]
                    pt = load_portals(real_map(world['map'])).get(pidx)
                    log('[%s] portal %d -> %r' % (name, pidx, pt))
                    sa = 0x92702 if code == 0x92701 else 0x92719
                    if not pt:
                        reply = frame(sa, w_i32(1) + map_enter_info(map_index=world['map'], pos=hero['pos']), cipher=cipher)
                    else:
                        end_map, (px, py, pz, pd) = pt
                        hero['pos'] = (px, py, pz)
                        reply = frame(sa, w_i32(0) + map_enter_info(hero['hp'], hero['mp'], map_index=end_map, pos=(px, py, pz)), cipher=cipher)
                        if cur_hero is not None:
                            cur_hero['map'] = end_map; cur_hero['pos'] = [px, py, pz]; save_store(STORE)
                        if end_map != world['map']:
                            leave_peers(sid)
                            world['map'] = end_map; world['spawn'] = (px, py, pz)
                            reply += frame(0x92732, map_enter_info(hero['hp'], hero['mp'], map_index=end_map, pos=(px, py, pz)) + w_bool(False), cipher=cipher)   # SN_MapMove: the client loads the new map
                elif code == 0x70101:   # NNpcShop CQ_Buy { int npc; int shop; BuyItemInfo[] { int product; int count } } -> SA_Buy { int error; BuyItemInfo[] }
                    npc, shop, n = struct.unpack_from('<iii', body)
                    wants = [struct.unpack_from('<ii', body, 12 + 8 * i) for i in range(n)]
                    total = 0; adds = []
                    for pidx, cnt in wants:
                        pr = load_product(pidx)
                        if pr:
                            total += pr[2] * cnt
                    log('[%s] buy at npc %d shop %d: %r total %d gold (have %d)' % (name, npc, shop, wants, total, account.get('gold', 0)))
                    if total <= account.get('gold', 0) and wants:
                        account['gold'] -= total
                        for pidx, cnt in wants:
                            pr = load_product(pidx)
                            if pr:
                                b = bag_add(account, pr[0], pr[1] * cnt)
                                adds.append(add_item(b['serial'], b['index'], b['count']))
                        save_store(STORE)
                        reply = frame(0x70102, w_i32(0) + w_arr([w_i32(a) + w_i32(b) for a, b in wants]), cipher=cipher)
                        if adds:
                            reply += frame(0xA0413, w_arr(adds) + w_i32(0) + w_i64(0), cipher=cipher)
                        reply += goods_update(cipher, 2, -total, account['gold'])
                    else:
                        reply = frame(0x70102, w_i32(1) + w_arr([]), cipher=cipher)
                elif code == 0x70103:   # CQ_Sell { int npc; int shop; SellItemInfo[] { long serial; int count } } -> SA_Sell { int error; SellItemInfoPlus[] { serial; count; remain }; long logGroupSerial }
                    npc, shop, n = struct.unpack_from('<iii', body)
                    sells = [struct.unpack_from('<qi', body, 12 + 12 * i) for i in range(n)]
                    plus = []; gone = []; earned = 0
                    for serial, cnt in sells:
                        b = next((x for x in account.get('bag', []) if x['serial'] == serial), None)
                        if not b:
                            continue
                        cnt = min(cnt, b['count']); b['count'] -= cnt; earned += item_sell_price(b['index']) * cnt
                        plus.append(w_i64(serial) + w_i32(cnt) + w_i32(b['count']))
                        if b['count'] <= 0:
                            account['bag'].remove(b); gone.append(item_info(serial, b['index'], 0))
                    account['gold'] = account.get('gold', 0) + earned; save_store(STORE)
                    log('[%s] sell %r -> +%d gold' % (name, sells, earned))
                    reply = frame(0x70104, w_i32(0) + w_arr(plus) + w_i64(0), cipher=cipher)
                    if gone:
                        reply += frame(0xA0414, w_arr(gone) + w_i32(0) + w_i64(0), cipher=cipher)   # SN_DeleteItem { ItemInfo[]; int UserLogType; long }
                    if earned:
                        reply += goods_update(cipher, 2, earned, account['gold'])
                elif code == 0xA045B:   # CQ_ItemDelete { ItemInfo[] } -> SA_ItemDelete { int error; DeleteItem[] { long serial; int currentCount } } (+ SN_DeleteItem)
                    infos, _ = read_item_infos(body)
                    worn = set(account.get('suit', {}).values()); dels = []; gone = []; err = 0
                    for serial, index, count in infos:
                        b = next((x for x in account.get('bag', []) if x['serial'] == serial), None)
                        if not b or serial in worn:
                            err = 1; continue
                        cnt = min(max(1, count), b['count']); b['count'] -= cnt
                        dels.append(w_i64(serial) + w_i32(b['count']))
                        if b['count'] <= 0:
                            account['bag'].remove(b); gone.append(item_info(serial, b['index'], 0))
                    save_store(STORE)
                    log('[%s] delete items %r -> err %d' % (name, infos, err))
                    reply = frame(0xA045C, w_i32(err) + w_arr(dels), cipher=cipher)
                    if gone:
                        reply += frame(0xA0414, w_arr(gone) + w_i32(0) + w_i64(0), cipher=cipher)
                elif code in (0xA0476, 0xA0478):   # CQ_ItemBagToAccountBag / CQ_AccountBagToItemBag { ItemInfo[] } -> SA { int error }
                    infos, _ = read_item_infos(body)
                    to_store = code == 0xA0476
                    src, dst = (account.setdefault('bag', []), account.setdefault('abag', [])) if to_store else (account.setdefault('abag', []), account.setdefault('bag', []))
                    worn = set(account.get('suit', {}).values())
                    moved = []; err = 0
                    for serial, index, count in infos:
                        b = next((x for x in src if x['serial'] == serial), None)
                        if not b or (to_store and serial in worn):
                            err = 1; continue
                        src.remove(b); dst.append(b); moved.append(b)
                    save_store(STORE)
                    log('[%s] %s %d item(s): %r' % (name, 'store' if to_store else 'take back', len(moved), [(b['index'], b['count']) for b in moved]))
                    reply = frame(code + 1, w_i32(err), cipher=cipher)
                    if moved:
                        gone = w_arr([item_info(b['serial'], b['index'], 0) for b in moved]) + w_i32(0) + w_i64(0)
                        came = w_arr([add_item(b['serial'], b['index'], b['count']) for b in moved]) + w_i32(0) + w_i64(0)
                        if to_store:
                            reply += frame(0xA0414, gone, cipher=cipher) + frame(0xA0201, came, cipher=cipher)   # SN_DeleteItem + SN_AddAccountItem
                        else:
                            reply += frame(0xA0202, gone, cipher=cipher) + frame(0xA0413, came, cipher=cipher)   # SN_DeleteAccountItem + SN_AddItem
                elif code == 0xA0440:   # CQ_ItemUse { long serial; int packageSelectIndex; bool auto } -> SA_ItemUse { int error; long serial; bool auto; int stackCount; GameAssetForClient[]; int remainCount; int itemIndex; long log } (+ SN_PotionItemUse)
                    serial, pkg, auto = struct.unpack_from('<qi?', body)
                    b = next((x for x in account.get('bag', []) if x['serial'] == serial), None)
                    log('[%s] use item serial %d: %r' % (name, serial, b))
                    if b:
                        b['count'] -= 1
                        hero['hp'] = min(hero['maxhp'], hero['hp'] + POTION_HEAL)
                        reply = frame(0xA0441, w_i32(0) + w_i64(serial) + w_bool(auto) + w_i32(b['count']) + w_arr([]) + w_i32(b['count']) + w_i32(b['index']) + w_i64(0), cipher=cipher)
                        reply += frame(0xA0442, w_i32(hero['hp']) + w_i32(hero['mp']) + w_i32(POTION_HEAL), cipher=cipher)   # SN_PotionItemUse { hp; mp; addPoint }
                        if b['count'] <= 0:
                            account['bag'].remove(b)
                            reply += frame(0xA0414, w_arr([item_info(serial, b['index'], 0)]) + w_i32(0) + w_i64(0), cipher=cipher)
                        save_store(STORE)
                    else:
                        reply = frame(0xA0441, w_i32(1) + w_i64(serial) + w_bool(auto) + w_i32(0) + w_arr([]) + w_i32(0) + w_i32(0) + w_i64(0), cipher=cipher)
                elif code == 0x93501:   # CQ_JobList { int npcIndex } -> SA_JobList { int error; int npcIndex; JobInfo[] }
                    npc = struct.unpack_from('<i', body)[0]
                    log('[%s] NPC tapped: %d' % (name, npc))
                    jobs = load_npc_jobs(npc)
                    if (npc == TEST_SHOP_NPC or TEST_SHOP_NPC == 0) and (1, TEST_SHOP) not in jobs:
                        jobs.append((1, TEST_SHOP))   # E_Job 1 ShopMiscellaneous
                        if (12, 0) not in jobs:
                            jobs.append((12, 0))      # E_Job 12 Storage (the account bag)
                    # JobInfo { int jobType; int index; bool enable; string reason }
                    reply = frame(0x93502, w_i32(0) + w_i32(npc) + w_arr([w_i32(jt) + w_i32(ji) + w_bool(True) + w_str('') for jt, ji in jobs]), cipher=cipher)
                elif code == 0x92736:   # CN_MapEtcInfo { byte dummy } -> SN_MapEtcInfo { MapEtcInfo[] }
                    reply = frame(0x92737, w_arr([w_i32(world['map']) + w_i8(0)]), cipher=cipher)
                elif code == 0x90170:   # CQ_ToLobby -> SA_ToLobby { int nError; long userNo }
                    reply = frame(0x90171, w_i32(0) + w_i64(1), cipher=cipher)
                elif code == 0x60104:   # CQ_ReCaptchaStart { byte dummy } -> SA_ReCaptchaStart { int error }
                    # HreCaptchaReceiver.SA_ReCaptchaStart: error != 0 -> reCaptchaReserveAction() (runs the reserved action at once);
                    # error == 0 -> reCaptchaN() opens the Google reCAPTCHA web view. The stub skips the captcha.
                    reply = frame(0x60105, w_i32(1), cipher=cipher)
                elif code == 0x60101:   # CQ_ServerVerify { string token } -> SA_ServerVerify { int error }
                    reply = frame(0x60102, w_i32(0), cipher=cipher)
                elif code >> 8 == 0xB01 and heroes and cur_hero is not None:   # ---- clans (NClan): see clandb.py ----
                    hno, hname, hlevel = cur_hero['no'], cur_hero['name'], cur_hero.get('level', 1)
                    my_clan, me = clan_of_hero(hno)
                    def rd_str(b, o):
                        n = struct.unpack_from('<H', b, o)[0]; return b[o + 2:o + 2 + n].decode('utf-8', 'replace'), o + 2 + n
                    if code == 0xB0144:   # CQ_IsClanner -> SA_IsClanner { int error; long clanNo; bool aliveClanServer }
                        reply = frame(0xB0145, w_i32(0) + w_i64(my_clan['no'] if my_clan else 0) + w_bool(True), cipher=cipher)
                    elif code == 0xB0101:   # CQ_ClanCreate { string clanName; short clanMark; int joinType; string introduction; string notice } -> SA { int error; long clanNo }
                        cname, o = rd_str(body, 0); mark, jt = struct.unpack_from('<hi', body, o); o += 6
                        intro, o = rd_str(body, o); notice, o = rd_str(body, o)
                        err, c = DB.clan_create(cname, hno, hname, hlevel, mark, jt, intro, notice)
                        log('[%s] clan create %r mark %d join %d -> %s' % (name, cname, mark, jt, clandb.ERROR_TEXT.get(err, 'ok %d' % (c['no'] if c else 0))))
                        reply = frame(0xB0102, w_i32(err) + w_i64(c['no'] if c else 0), cipher=cipher)
                        if c:
                            clan_reshape(hno)   # SN_Shape again with the clan: the map sees the new clan name
                    elif code == 0xB0103:   # CQ_ClannerLogin -> SA_ClannerLogin { error; ClanInfo; ClanAttendanceInfo; ClanHouseInfo; ClannerInfo[]; MiniAllianceApplication[] x2; MiniClanInfo[]; CountLimitInfo[] }
                        if my_clan is None:
                            reply = frame(0xB0104, w_i32(clandb.ERR_NOT_MEMBER) + clan_blob(None) + (w_dt(0) + w_i32(0) + w_dt(0) + w_i32(0)) + (w_i64(0) + w_dt(0)) + w_i32(0) + w_i32(0) + w_i32(0) + w_i32(0) + w_i32(0), cipher=cipher)
                        else:
                            att = w_dt(0) + w_i32(0) + w_dt(0) + w_i32(0); house = w_i64(my_clan['no']) + w_dt(0)
                            reply = frame(0xB0104, w_i32(0) + clan_blob(my_clan) + att + house + w_arr([clanner_blob(m) for m in DB.clanners(my_clan['no'])]) + w_i32(0) + w_i32(0) + w_i32(0) + w_i32(0), cipher=cipher)
                    elif code == 0xB0107:   # CQ_ClanListRead -> SA_ClanListRead { error; long heroNo; ClanInfo[] }
                        reply = frame(0xB0108, w_i32(0) + w_i64(hno) + w_arr([clan_blob(c) for c in DB.clans()]), cipher=cipher)
                    elif code == 0xB0128:   # CQ_ClanSearch { string clanName } -> SA_ClanSearch { error; ClanInfo }
                        cname, _ = rd_str(body, 0); c = DB.clan_by_name(cname)
                        reply = frame(0xB0129, w_i32(0 if c else clandb.ERR_NO_CLAN) + clan_blob(c), cipher=cipher)
                    elif code == 0xB0109:   # CQ_ClanJoin { long clanNo } -> SA_ClanJoin { error; clanNo }: Immediately joins, NeedAccept files an application
                        cno = struct.unpack_from('<q', body)[0]; c = DB.clan(cno)
                        if c is None:
                            err = clandb.ERR_NO_CLAN
                        elif c['join_type'] == clandb.JOIN_IMMEDIATELY:
                            err = DB.clan_add(cno, hno, hname, hlevel)
                            if err == 0:
                                send_clan(cno, 0xB0114, clanner_blob(DB.clanner(hno)) + w_i16(DB.clan_member_count(cno)), exclude_sid=sid)   # SN_ClanJoin to the members
                        else:
                            err = DB.clan_apply(cno, hno, hname, hlevel)
                            if err == 0:
                                send_clan(cno, 0xB0133, w_i8(1), officers_only=True)   # SN_ApplyApplication { byte newData }
                        log('[%s] clan join %d -> %s' % (name, cno, clandb.ERROR_TEXT.get(err, 'ok')))
                        reply = frame(0xB010A, w_i32(err) + w_i64(cno), cipher=cipher)
                        if err == 0 and c['join_type'] == clandb.JOIN_IMMEDIATELY:
                            reply += frame(0xB0114, clanner_blob(DB.clanner(hno)) + w_i16(DB.clan_member_count(cno)), cipher=cipher)
                            clan_reshape(hno)
                    elif code == 0xB0131:   # CQ_ApplyApplication { long clanNo } -> SA { error }
                        cno = struct.unpack_from('<q', body)[0]
                        err = DB.clan_apply(cno, hno, hname, hlevel)
                        if err == 0:
                            send_clan(cno, 0xB0133, w_i8(1), officers_only=True)
                        reply = frame(0xB0132, w_i32(err), cipher=cipher)
                    elif code == 0xB0158:   # CQ_MyApplication -> SA { error; SendApplicationToClan[] }
                        rows = []
                        for a in DB.hero_applications(hno):
                            c = DB.clan(a['clan_no'])
                            if c:   # { long clanNo; string clanName; int clanLevel; short mark; string masterHeroName; short memberCount; short memberLimit; string introduce; DateTime }
                                rows.append(w_i64(c['no']) + w_str(c['name']) + w_i32(c['level']) + w_i16(c['mark']) + w_str(c['master_name']) + w_i16(DB.clan_member_count(c['no'])) + w_i16(c['member_limit']) + w_str(c['introduce']) + w_dt(a['at']))
                        reply = frame(0xB0159, w_i32(0) + w_arr(rows), cipher=cipher)
                    elif code == 0xB015A:   # CQ_MyApplicationDelete { long clanNo } -> SA { error; clanNo }
                        cno = struct.unpack_from('<q', body)[0]; DB.clan_application_delete(cno, hno)
                        send_clan(cno, 0xB013C, w_i64(hno), officers_only=True)   # SN_DeleteApplication
                        reply = frame(0xB015B, w_i32(0) + w_i64(cno), cipher=cipher)
                    elif code == 0xB010B:   # CQ_ClannerLeave -> SA { error }; SN_ClannerLeave { heroNo; short count } to the others (the master must hand over or delete)
                        if my_clan is None:
                            err = clandb.ERR_NOT_MEMBER
                        elif me['grade'] == clandb.GRADE_LEADER and DB.clan_member_count(my_clan['no']) > 1:
                            err = clandb.ERR_RIGHTS
                        else:
                            err = DB.clan_remove(hno)
                            if err == 0 and DB.clan_member_count(my_clan['no']) == 0:
                                DB.clan_delete(my_clan['no'])
                            elif err == 0:
                                send_clan(my_clan['no'], 0xB0116, w_i64(hno) + w_i16(DB.clan_member_count(my_clan['no'])))
                        reply = frame(0xB010C, w_i32(err), cipher=cipher)
                        if err == 0:
                            clan_reshape(hno)
                    elif code == 0xB010D:   # CQ_ClanDelete -> SA { error }: the master disbands; every member gets SN_ClannerKick with its own number
                        if my_clan is None or me['grade'] != clandb.GRADE_LEADER:
                            err = clandb.ERR_RIGHTS
                        else:
                            err = 0
                            for m in DB.clanners(my_clan['no']):
                                for osid in clan_sessions(my_clan['no']):
                                    if SESSIONS[osid]['h'].get('no') == m['hero_no']:
                                        send_session(osid, 0xB0113, w_i64(m['hero_no']) + w_i16(0))
                            members_ = [m['hero_no'] for m in DB.clanners(my_clan['no'])]
                            DB.clan_delete(my_clan['no'])
                            for mno in members_:
                                clan_reshape(mno)
                        reply = frame(0xB010E, w_i32(err), cipher=cipher)
                    elif code == 0xB010F:   # CQ_ClannerKick { long targetHeroNo } -> SA { error; kickedHeroNo; short count }; SN_ClannerKick to everybody incl. the kicked
                        tno = struct.unpack_from('<q', body)[0]; tm = DB.clanner(tno)
                        if my_clan is None or me['grade'] == clandb.GRADE_GENERAL or tm is None or tm['clan_no'] != my_clan['no'] or tm['grade'] <= me['grade']:
                            err = clandb.ERR_RIGHTS
                        else:
                            err = DB.clan_remove(tno)
                            send_clan(my_clan['no'], 0xB0113, w_i64(tno) + w_i16(DB.clan_member_count(my_clan['no'])), exclude_sid=sid)
                            for osid, o in list(SESSIONS.items()):
                                if o['h'].get('no') == tno:
                                    send_session(osid, 0xB0113, w_i64(tno) + w_i16(0))
                        reply = frame(0xB0110, w_i32(err) + w_i64(tno) + w_i16(DB.clan_member_count(my_clan['no']) if my_clan else 0), cipher=cipher)
                        if err == 0:
                            clan_reshape(tno)
                    elif code == 0xB0134:   # CQ_ApplicationList -> SA { error; Application[] }
                        rows = [application_blob(a) for a in DB.clan_applications(my_clan['no'])] if my_clan and me['grade'] != clandb.GRADE_GENERAL else []
                        reply = frame(0xB0135, w_i32(0 if my_clan else clandb.ERR_NOT_MEMBER) + w_arr(rows), cipher=cipher)
                    elif code in (0xB0136, 0xB0139):   # CQ_AcceptToApplication / CQ_RejectToApplication { long heroNo }
                        ano = struct.unpack_from('<q', body)[0]
                        if my_clan is None or me['grade'] == clandb.GRADE_GENERAL:
                            err = clandb.ERR_RIGHTS
                        else:
                            app = next((a for a in DB.clan_applications(my_clan['no']) if a['hero_no'] == ano), None)
                            if app is None:
                                err = clandb.ERR_NOT_MEMBER
                            elif code == 0xB0136:
                                err = DB.clan_add(my_clan['no'], ano, app['name'], app['level'])
                                if err == 0:
                                    send_clan(my_clan['no'], 0xB0114, clanner_blob(DB.clanner(ano)) + w_i16(DB.clan_member_count(my_clan['no'])))   # SN_ClanJoin (the new member too)
                                    for osid, o in list(SESSIONS.items()):
                                        if o['h'].get('no') == ano:
                                            send_session(osid, 0xB0138, w_i64(my_clan['no']))   # SN_AcceptToApplication
                                    clan_reshape(ano)
                                else:
                                    DB.clan_application_delete(my_clan['no'], ano)
                            else:
                                err = 0; DB.clan_application_delete(my_clan['no'], ano)
                                for osid, o in list(SESSIONS.items()):
                                    if o['h'].get('no') == ano:
                                        send_session(osid, 0xB013B, w_str(my_clan['name']) + w_i64(my_clan['no']))   # SN_RejectToApplication
                            if err in (0, clandb.ERR_IN_CLAN):
                                send_clan(my_clan['no'], 0xB013C, w_i64(ano), officers_only=True)   # SN_DeleteApplication
                        reply = frame(0xB0137, w_i32(err) + w_bool(err == 0), cipher=cipher) if code == 0xB0136 else frame(0xB013A, w_i32(err), cipher=cipher)
                    elif code == 0xB0141:   # CQ_ChangeClanInfo { short mark; int joinType; string introduce; string notice } -> SA { error } + SN_ChangeClanInfo (same body) to the clan
                        mark, jt = struct.unpack_from('<hi', body); intro, o = rd_str(body, 6); notice, o = rd_str(body, o)
                        if my_clan is None or me['grade'] == clandb.GRADE_GENERAL:
                            err = clandb.ERR_RIGHTS
                        else:
                            err = 0; DB.clan_update(my_clan['no'], mark, jt, intro, notice)
                            send_clan(my_clan['no'], 0xB0143, body)
                        reply = frame(0xB0142, w_i32(err), cipher=cipher)
                    elif code == 0xB0161:   # CQ_ChangeGrade { long heroNo; int grade } -> SA { error } + SN_ChangeGrade { heroNo; int grade }
                        tno, grade = struct.unpack_from('<qi', body)
                        err = clandb.ERR_RIGHTS if my_clan is None or me['grade'] != clandb.GRADE_LEADER else DB.clan_set_grade(my_clan['no'], tno, grade)
                        if err == 0:
                            send_clan(my_clan['no'], 0xB0163, w_i64(tno) + w_i32(grade))
                        reply = frame(0xB0162, w_i32(err), cipher=cipher)
                    elif code == 0xB0164:   # CQ_ChangeMaster { long toHeroNo } -> SA { error } + SN_ChangeMaster { before; int grade; after; int grade }
                        tno = struct.unpack_from('<q', body)[0]
                        err = clandb.ERR_RIGHTS if my_clan is None or me['grade'] != clandb.GRADE_LEADER or tno == hno else DB.clan_set_master(my_clan['no'], tno)
                        if err == 0:
                            send_clan(my_clan['no'], 0xB0166, w_i64(hno) + w_i32(clandb.GRADE_GENERAL) + w_i64(tno) + w_i32(clandb.GRADE_LEADER))
                            clan_reshape(hno); clan_reshape(tno)   # the master's crown changes hands
                        reply = frame(0xB0165, w_i32(err), cipher=cipher)
                    else:
                        r = autoreply.answer_for(code)
                        if r:
                            log('[%s] clan auto-answer 0x%X for %s' % (name, r[0], autoreply.describe(code)))
                            reply = frame(r[0], r[2], cipher=cipher)
                elif code >> 16 == 0xD and heroes and cur_hero is not None:   # ---- marketplace (NTrading): see tradedb.py ----
                    hno, hname = cur_hero['no'], cur_hero['name']
                    DB.trade_expire()
                    if code == 0xD0201:   # CQ_Sell { long serial; int amount; int price } -> SA_Sell { error; SellRegistItem } (+ SN_DeleteItem / SN_AddItem for a split stack)
                        serial, amount, price = struct.unpack_from('<qii', body)
                        b = next((x for x in account.get('bag', []) if x['serial'] == serial), None)
                        worn = set(account.get('suit', {}).values())
                        if b is None or serial in worn or amount < 1 or amount > b['count']:
                            err, l = tradedb.ERR_ITEM, None
                        else:
                            err, l = DB.trade_sell(hno, hname, account.get('_login', ''), serial, b['index'], amount, b.get('enchant', 0), price)
                        reply = frame(0xD0202, w_i32(err) + sell_regist_blob(l), cipher=cipher)
                        if err == 0:
                            rest = b['count'] - amount
                            account['bag'].remove(b)
                            reply += frame(0xA0414, w_arr([item_info(serial, b['index'], 0)]) + w_i32(0) + w_i64(0), cipher=cipher)   # SN_DeleteItem
                            if rest > 0:
                                nb = bag_add(account, b['index'], rest)
                                reply += frame(0xA0413, w_arr([add_item(nb['serial'], nb['index'], nb['count'])]) + w_i32(0) + w_i64(0), cipher=cipher)   # SN_AddItem (the remainder)
                            save_store(STORE)
                        log('[%s] market sell serial %d x%d for %d -> %s' % (name, serial, amount, price, tradedb.ERROR_TEXT.get(err, 'ok')))
                    elif code == 0xD0205:   # CQ_RegistList -> SA_RegistList { error; SellRegistItem[] }
                        reply = frame(0xD0206, w_i32(0) + w_arr([sell_regist_blob(l) for l in DB.trade_mine(hno)]), cipher=cipher)
                    elif code == 0xD0207:   # CQ_SellCancel { long SellWaitNo } -> SA_SellCancel { error; no } (+ SN_AddItem: the item comes back)
                        no = struct.unpack_from('<q', body)[0]; l = DB.trade_cancel(hno, no)
                        reply = frame(0xD0208, w_i32(0 if l else tradedb.ERR_NO_LISTING) + w_i64(no), cipher=cipher)
                        if l:
                            nb = bag_add(account, l['item_index'], l['count']); nb['enchant'] = l['enchant']
                            reply += frame(0xA0413, w_arr([add_item(nb['serial'], nb['index'], nb['count'])]) + w_i32(0) + w_i64(0), cipher=cipher)
                            save_store(STORE)
                    elif code == 0xD020A:   # CQ_CompleteList { long LastCompleteNo } -> SA_CompleteList { error; CompleteItem[] }
                        reply = frame(0xD020B, w_i32(0) + w_arr([w_i64(l['no']) + complete_blob(l) for l in DB.trade_mine(hno, (tradedb.SOLD,))]), cipher=cipher)
                    elif code == 0xD020C:   # CQ_ReceiveComplete -> SA_ReceiveComplete { error }: the sold listings' gold (minus the fee) lands in the account
                        gross, fee, rows = DB.trade_settle(hno)
                        reply = frame(0xD020D, w_i32(0 if rows else tradedb.ERR_NOTHING), cipher=cipher)
                        if rows:
                            account['gold'] = account.get('gold', 0) + gross - fee; save_store(STORE)
                            reply += goods_update(cipher, 2, gross - fee, account['gold'])
                            log('[%s] market settle %d listings: +%d gold (fee %d)' % (name, len(rows), gross - fee, fee))
                    elif code == 0xD0203:   # CQ_SellHistory -> SA_SellHistory { error; TradeSellItemHistory[] }
                        reply = frame(0xD0204, w_i32(0) + w_arr([complete_blob(l) for l in DB.trade_history(hno)]), cipher=cipher)
                    elif code == 0xD0103:   # CQ_ViewItemsItemIndex { int[] ItemIndexes; int ClientCacheKey } -> SA { error; TradeItemGroupByItemIndex[]; cacheKey }
                        n = struct.unpack_from('<h', body)[0]; wanted = set(struct.unpack_from('<%di' % n, body, 2)) if n > 0 else None
                        ck = struct.unpack_from('<i', body, 2 + 4 * max(0, n))[0] if len(body) >= 6 + 4 * max(0, n) else 0
                        groups = {}
                        for l in DB.trade_list():
                            if wanted is None or l['item_index'] in wanted:
                                g = groups.setdefault(l['item_index'], [l['price'], 0]); g[0] = min(g[0], l['price']); g[1] += 1
                        reply = frame(0xD0104, w_i32(0) + w_arr([w_i32(i) + w_f64(g[0]) + w_i32(g[1]) for i, g in sorted(groups.items())]) + w_i32(ck), cipher=cipher)
                    elif code == 0xD0105:   # CQ_ViewItemsEnchantLevel { int ItemIndex; byte start; byte end } -> SA { error; TradeItemGroupByEnchantLevel[] }
                        idx, lo, hi = struct.unpack_from('<iBB', body); groups = {}
                        for l in DB.trade_list(idx):
                            if lo <= l['enchant'] <= hi:
                                g = groups.setdefault(l['enchant'], [l['price'], 0]); g[0] = min(g[0], l['price']); g[1] += 1
                        reply = frame(0xD0106, w_i32(0) + w_arr([w_i32(idx) + w_i8(e) + w_f64(g[0]) + w_i32(g[1]) for e, g in sorted(groups.items())]), cipher=cipher)
                    elif code == 0xD0107:   # CQ_ViewItemsDetail { int ItemIndex; byte EnchantLevel (255 = any); int[] codes; SortOption; int loaded } -> SA { error; TradeItemDetail[] }
                        idx, ench = struct.unpack_from('<iB', body)
                        rows = DB.trade_list(idx, None if ench == 255 else ench)
                        reply = frame(0xD0108, w_i32(0) + w_arr([trade_detail_blob(l) for l in rows[:100]]), cipher=cipher)
                    elif code == 0xD0101:   # CQ_Buy { long TradeNo; int Price } -> SA_Buy { error; TradeNo }: gold out, item in (SN_AddItem), the seller is told
                        no, price = struct.unpack_from('<qi', body)
                        l = DB.trade_listing(no)
                        if l is None or l['flag'] != tradedb.SELLING:
                            err = tradedb.ERR_NO_LISTING
                        elif account.get('gold', 0) < l['price']:
                            err = tradedb.ERR_GOLD
                        else:
                            err, l = DB.trade_buy(hno, hname, no, price)
                        reply = frame(0xD0102, w_i32(err) + w_i64(no), cipher=cipher)
                        if err == 0:
                            account['gold'] -= l['price']
                            nb = bag_add(account, l['item_index'], l['count']); nb['enchant'] = l['enchant']; save_store(STORE)
                            reply += frame(0xA0413, w_arr([add_item(nb['serial'], nb['index'], nb['count'])]) + w_i32(0) + w_i64(0), cipher=cipher)
                            reply += goods_update(cipher, 2, -l['price'], account['gold'])
                            ss = session_of_hero(l['seller_name'])
                            if ss is not None:
                                system_line(ss, '%s bought your item %d x%d for %d gold (collect it in the marketplace)' % (hname, l['item_index'], l['count'], l['price']))
                        log('[%s] market buy listing %d -> %s' % (name, no, tradedb.ERROR_TEXT.get(err, 'ok')))
                    elif code == 0xD0301:   # CQ_Search { int ItemIndex; byte EnchantLevel } -> SA_Search { error; double min; double max; double avg }
                        idx, ench = struct.unpack_from('<iB', body); lo, hi, avg = DB.trade_prices(idx, None if ench == 255 else ench)
                        reply = frame(0xD0302, w_i32(0) + w_f64(lo) + w_f64(hi) + w_f64(avg), cipher=cipher)
                    else:
                        r = autoreply.answer_for(code)
                        if r:
                            log('[%s] market auto-answer 0x%X for %s' % (name, r[0], autoreply.describe(code)))
                            reply = frame(r[0], r[2], cipher=cipher)
                elif code in (0x92102, 0x92103):   # CN_MoveAction / CN_StandAction { ActionBase }: movement is server-confirmed,
                    # the client only moves once the server echoes the action as SN_Action 0x92101 { ActionBase }.
                    # ActionBase wire (from the client's own packets): int32 blobLength (incl. itself), byte kind (1 Stand, 2 Move),
                    # int64 startLogicTime, float x, y, z, int16 direction, uint32 actorMemberIndex, then Move: uint32 speed, float destX, destY, destZ.
                    blob = bytearray(body)
                    if code == 0x92102 and len(blob) >= 35 and struct.unpack_from('<I', blob, 31)[0] == 0:
                        struct.pack_into('<I', blob, 31, int(os.environ.get('STUB_MOVE_SPEED', '600')))   # the client sends speed 0 (its stat is 0)
                    if len(blob) >= 25:
                        hero['pos'] = struct.unpack_from('<fff', blob, 13) if code == 0x92103 else struct.unpack_from('<fff', blob, 35)   # stand: position, move: destination
                    reply = frame(0x92101, bytes(blob), cipher=cipher)
                    send_peers(sid, lambda c, m, bl=bytes(blob): frame(0x92101, rewrite_member(bl, m), cipher=c))
                elif code == 0x70201:   # CQ_PremiumShopList { MarketType; long uid } -> SA { int error; bool alreadyUpToDate; long uid; products[]; int[] topRank }
                    reply = frame(0x70202, w_i32(0) + w_bool(True) + w_i64(0) + w_i32(0) + w_parr(b''), cipher=cipher)
                elif code == 0x70301:   # CQ_BillingBagRead -> SA_BillingBagRead { int error; BillingBagItem[] }
                    reply = frame(0x70302, w_i32(0) + w_i32(0), cipher=cipher)
                elif code == 0xE0141:   # NFriend CQ_Login { int check } -> SA_Login { int error; FriendHeroBase }
                    h = heroes[0] if heroes else {'no': 1, 'index': 1010101, 'name': 'StubHero', 'level': 1}
                    fhb = w_i64(h['no']) + w_str(h['name']) + w_i32(0) * 5 + w_i32(50) * 5 + w_dt()
                    reply = frame(0xE0142, w_i32(0) + fhb, cipher=cipher)
                elif code == 0xA1101:   # CQ_EventList { long gameEventUid } -> SA_EventList { int error; long uid; EventInfo[] }
                    reply = frame(0xA1102, w_i32(0) + w_i64(0) + w_i32(0), cipher=cipher)
                elif code == 0xA1201:   # NDeadPenalty CQ_RecoveryExpPenalty { long penaltyNo; int recoveryPaymentsIndex } -> SA { int error; long penaltyNo }
                    pno, _pay = struct.unpack_from('<qi', body)
                    hh = cur_hero or (SESSIONS.get(sid) or {}).get("h") or {}
                    pen = next((x for x in (hh.get('penalties') or []) if x['no'] == pno), None)
                    if pen is None:
                        reply = frame(0xA1202, w_i32(2) + w_i64(pno), cipher=cipher)
                    elif account.get('gold', 0) < penalty_cost(pen):
                        reply = frame(0xA1202, w_i32(1) + w_i64(pno), cipher=cipher)
                    else:
                        cost = penalty_cost(pen)
                        account['gold'] -= cost
                        hh['penalties'] = [x for x in hh['penalties'] if x['no'] != pno]
                        reply = frame(0xA1202, w_i32(0) + w_i64(pno), cipher=cipher) + goods_update(cipher, 2, -cost, account['gold'])
                        reply += gain_exp(cipher, hh, hero, pen['value']) + penalty_info(cipher, hh)
                        log('[%s] recovered %d EXP for %d gold' % (name, pen['value'], cost))
                elif code == 0xC0105:   # CQ_MailCount -> SA_MailCount { int error; int mailCount; int letterCount; bool newLetterFlag }
                    reply = frame(0xC0106, w_i32(0) + w_i32(0) + w_i32(0) + w_bool(False), cipher=cipher)
                elif code == 0xB0158:   # CQ_MyApplication -> SA_MyApplication { int error; SendApplicationToClan[] }
                    reply = frame(0xB0159, w_i32(0) + w_i32(0), cipher=cipher)
                elif code == 0xA1C01:   # CQ_DynamicTranslationList { long uid; string locale } -> SA { int error; long uid; string locale; TranslationInfo[] }
                    ln = struct.unpack_from('<H', body, 8)[0]; loc = body[10:10 + ln]
                    reply = frame(0xA1C02, w_i32(0) + w_i64(0) + w_i16(ln) + loc + w_i32(0), cipher=cipher)
                elif code == 0xA0910:   # NQuest CQ_Start { int questNo; int questIndex } -> SA_Start { int error; int reqQuestNo; int reqQuestIndex; stProgress }
                    req_qno, qidx = struct.unpack_from('<ii', body)
                    # the client sends questNo -1: the server assigns the quest instance number (GQuestNo.Invalid = -1 otherwise)
                    qno = len(quests) + 1
                    quests[qno] = {'index': qidx, 'count': 0, 'state': 1, 'task': load_quest(qidx)['task']}
                    log('[%s] quest start: index %d -> no %d' % (name, qidx, qno))
                    prog = w_i32(qno) + w_i32(qidx) + w_i32(0) + w_i8(1) + w_bool(False)   # stProgress: QuestNo, QuestIndex, TaskCount, TaskState NORMAL, Extend null
                    reply = frame(0xA0911, w_i32(0) + w_i32(req_qno) + w_i32(qidx) + prog, cipher=cipher)
                elif code == 0xA0918:   # CQ_Conversation { TaskType; int questNo; int npcIndex } -> SA_Conversation { int error; int questNo; int npcIndex } + SN_Update
                    ttype, qno, npc = struct.unpack_from('<iii', body)
                    log('[%s] quest conversation: type %d quest %d npc %d' % (name, ttype, qno, npc))
                    q = quests.get(qno)
                    reply = frame(0xA0919, w_i32(0) + w_i32(qno) + w_i32(npc), cipher=cipher)
                    if q:
                        q['count'] += 1; q['state'] = 2
                        # SN_Update { stTaskUpdate[] { int QuestNo; int QuestIndex; int TaskCount; E_STATE TaskState (2 ENDING) } }
                        reply += frame(0xA0922, w_arr([w_i32(qno) + w_i32(q['index']) + w_i32(q['count']) + w_i8(2)]), cipher=cipher)
                elif code == 0xA091A:   # CQ_Action { TaskType; int questNo; int taskCount } -> SA_Action { int error; int questNo } + SN_Update
                    ttype, qno, cnt = struct.unpack_from('<iii', body)
                    log('[%s] quest action: type %d quest %d count %d' % (name, ttype, qno, cnt))
                    q = quests.get(qno)
                    reply = frame(0xA091B, w_i32(0) + w_i32(qno), cipher=cipher)
                    if q:
                        q['count'] = cnt; q['state'] = 2
                        reply += frame(0xA0922, w_arr([w_i32(qno) + w_i32(q['index']) + w_i32(q['count']) + w_i8(2)]), cipher=cipher)
                elif code == 0xA0914:   # CQ_Finish { int questNo; int questIndex; int slotIndex } -> SA_Finish { int error; int questNo; int questIndex; int slotIndex; int selectedSlotIndex }
                    qno, qidx, slot = struct.unpack_from('<iii', body)
                    log('[%s] quest finish: quest %d index %d slot %d' % (name, qno, qidx, slot))
                    quests.pop(qno, None)
                    if qidx not in account.setdefault('quests_done', []):
                        account['quests_done'].append(qidx)
                    reply = frame(0xA0915, w_i32(0) + w_i32(qno) + w_i32(qidx) + w_i32(slot) + w_i32(slot), cipher=cipher)
                    q = load_quest(qidx)
                    if cur_hero is not None:
                        if q['exp']:
                            reply += gain_exp(cipher, cur_hero, hero, q['exp'])
                        adds = []
                        for idx, cnt in q['items']:
                            b = bag_add(account, idx, cnt)
                            adds.append(add_item(b['serial'], b['index'], b['count']))
                        if adds:
                            # SN_AddItem { AddItem[] infos; int UserLogType; long logGroupSerial }
                            reply += frame(0xA0413, w_arr(adds) + w_i32(0) + w_i64(0), cipher=cipher)
                        save_store(STORE)
                        log('[%s] quest reward: exp %d items %r (next quest %d)' % (name, q['exp'], q['items'], q['next']))
                elif code == CQ_Login:
                    now = int(time.time() * 1000)
                    mlen = struct.unpack_from('<H', body)[0]
                    member = body[2:2 + mlen].decode('utf-8', 'replace')
                    ctick = struct.unpack_from('<q', body, 2 + mlen)[0] if len(body) > 10 else 0   # clientTick after memberId
                    tl = struct.unpack_from('<H', body, 11 + mlen)[0] if len(body) > 13 + mlen else 0
                    secret = body[13 + mlen:13 + mlen + tl].decode('utf-8', 'replace')   # serverToken: our client's password / the real client's session token
                    err = sign_in(member, secret, 'game-login')
                    if err:
                        reply = frame(SA_Login, w_i32(err) + w_i64(ctick) + w_i64(server_tick()) + w_i64(0) + w_i64(base_ticks()) + w_bool(False) + w_i8(0) + w_i8(0) + w_i8(0) + w_dt(), cipher=cipher)
                        log('[%s] send: %s' % (name, hexdump(reply))); writer.write(reply); await writer.drain()
                        continue
                    DB.touch(member)
                    account = STORE.setdefault(member, {'heroes': [], 'country': 0})
                    account['_login'] = member
                    heroes = account['heroes']
                    log('[%s] account %s...: %d heroes, country %d' % (name, member[:8], len(heroes), account['country']))
                    mode = os.environ.get('STUB_LOGIN', 'new')
                    if mode == 'old':     # the 2026-11-02 layout that reached the lobby: ms everywhere, 8-byte trailing DateTime
                        body = w_i32(0) + w_i64(now) + w_i64(now) + w_i64(1) + w_i64(now) + w_bool(False) + w_i8(0) + w_i8(0) + w_i8(0) + w_i64(now)
                    elif mode == 'dt8':   # new clock, 8-byte trailing DateTime
                        body = w_i32(0) + w_i64(ctick) + w_i64(server_tick()) + w_i64(1) + w_i64(base_ticks()) + w_bool(False) + w_i8(0) + w_i8(0) + w_i8(0) + w_i64(now)
                    elif mode == 'msbase':  # old ms values, 12-byte DateTime
                        body = w_i32(0) + w_i64(now) + w_i64(now) + w_i64(1) + w_i64(now) + w_bool(False) + w_i8(0) + w_i8(0) + w_i8(0) + w_dt()
                    else:   # chatHideSenderInfo, isSupportToken, countryType (saved: the client skips the country popup), countryTypeUpdateTime
                        body = w_i32(0) + w_i64(ctick) + w_i64(server_tick()) + w_i64(1) + w_i64(base_ticks()) + w_bool(False) + w_i8(0) + w_i8(0) + w_i8(account.get('country', 0)) + w_dt()
                    reply = frame(SA_Login, body, cipher=cipher)
                elif code >> 16 != 1:
                    # no handler yet: answer the query with the zero-filled SA_ from the protocol table so the client never
                    # blocks, and log what it asked for (tools/stub/requests-report.py lists these)
                    r = autoreply.answer_for(code)
                    if r:
                        log('[%s] auto-answer 0x%X %s for %s' % (name, r[0], r[1], autoreply.describe(code)))
                        reply = frame(r[0], r[2], cipher=cipher)
                    else:
                        log('[%s] UNANSWERED %s' % (name, autoreply.describe(code)))
                if reply:
                    if len(reply) > MAX_PACKET:
                        log('[%s] WARNING: reply of %d bytes for code 0x%X (single frames must stay <= %d)' % (name, len(reply), code, MAX_PACKET))
                    log('[%s] send: %s' % (name, hexdump(reply))); writer.write(reply); await writer.drain()
                if code == 0x10005:
                    # after the handshake ack, 0x1000F = "begin": fires the client's OnConnected and starts its ping timer
                    beg = frame(0x1000F, b'', cipher=cipher)
                    log('[%s] send begin 0x1000F: %s' % (name, hexdump(beg))); writer.write(beg); await writer.drain()
            if buf:
                log('[%s] unparsed tail %d bytes' % (name, len(buf)))
    except Exception as e:
        log('[%s] error %r' % (name, e))
    party_leave(sid)
    if cur_hero is not None and DB.clanner(cur_hero['no']):
        send_clan(DB.clanner(cur_hero['no'])['clan_no'], 0xB0115, w_i64(cur_hero['no']) + w_i64(0), exclude_sid=sid)   # SN_ClannerLogout
    leave_peers(sid)
    if cur_hero is not None:
        if world['map'] in INSTANCES:
            inst = INSTANCES[world['map']]; back_map, back_pos = inst.get('returns', {}).get(sid, inst['return'])
            if not sessions_in(world['map']):
                INSTANCES.pop(world['map'], None); MAPS.pop(world['map'], None)
            cur_hero['map'] = back_map; cur_hero['pos'] = [float(v) for v in back_pos]
        else:
            cur_hero['map'] = world['map']; cur_hero['pos'] = [float(v) for v in hero['pos']]
        cur_hero['hp'] = hero['hp']; cur_hero['mp'] = hero['mp']; cur_hero['dead'] = hero['dead']
        save_store(STORE)
    log('[%s] closed' % name); writer.close()


ACCOUNT_PAGE = '''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Magic Dekaron G - Account</title>
<style>
:root{color-scheme:dark}
body{margin:0;background:#0d0f14;color:#e7e9ee;font:16px/1.5 system-ui,Segoe UI,Roboto,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}
.card{width:min(92vw,380px);background:#161a22;border:1px solid #262c38;border-radius:14px;padding:26px 24px;box-shadow:0 10px 40px #0008}
h1{font-size:20px;margin:0 0 4px;letter-spacing:.5px}.sub{color:#8b93a7;font-size:13px;margin:0 0 18px}
.tabs{display:flex;gap:6px;margin-bottom:16px}.tabs button{flex:1;background:#1e2430;border:1px solid #2b323f;color:#aab2c5;padding:9px;border-radius:9px;cursor:pointer;font-size:14px}
.tabs button.on{background:#c0392b;border-color:#c0392b;color:#fff}
label{display:block;font-size:12px;color:#8b93a7;margin:12px 0 5px}
input{width:100%;box-sizing:border-box;background:#0f131a;border:1px solid #2b323f;color:#e7e9ee;padding:11px;border-radius:9px;font-size:15px}
.go{width:100%;margin-top:20px;background:#c0392b;border:0;color:#fff;padding:13px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer}
.go:hover{background:#d84030}.msg{margin-top:14px;font-size:13px;min-height:18px}.msg.err{color:#ff7a6b}.msg.ok{color:#67d98b}
.tok{margin-top:14px;word-break:break-all;background:#0f131a;border:1px dashed #2b323f;border-radius:9px;padding:10px;font:12px ui-monospace,monospace;color:#9fb0c9;display:none}
</style></head><body><div class="card">
<h1>Magic Dekaron G</h1><p class="sub">One account for every server on the network.</p>
<div class="tabs"><button id="tL" class="on" onclick="pick('login')">Sign in</button><button id="tR" onclick="pick('register')">Create account</button></div>
<label>Account name</label><input id="login" autocomplete="username" autocapitalize="off">
<label>Password</label><input id="password" type="password" autocomplete="current-password">
<div id="emailRow" style="display:none"><label>Email (optional, for password reset)</label><input id="email" type="email" autocomplete="email"></div>
<button class="go" id="go" onclick="submit()">Sign in</button>
<div class="msg" id="msg"></div><div class="tok" id="tok"></div>
</div><script>
var mode='login',DEEP='__DEEPLINK__';
function pick(m){mode=m;document.getElementById('tL').className=m=='login'?'on':'';document.getElementById('tR').className=m=='register'?'on':'';
document.getElementById('emailRow').style.display=m=='register'?'block':'none';document.getElementById('go').textContent=m=='login'?'Sign in':'Create account';
document.getElementById('msg').textContent='';document.getElementById('tok').style.display='none';}
async function submit(){var msg=document.getElementById('msg');msg.className='msg';msg.textContent='...';
var payload={login:document.getElementById('login').value.trim(),password:document.getElementById('password').value};
if(mode=='register')payload.email=document.getElementById('email').value.trim();
try{var r=await fetch('/account/'+mode,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
var j=await r.json();
if(j.error){msg.className='msg err';msg.textContent=j.message||('error '+j.error);return;}
msg.className='msg ok';msg.textContent='Signed in. Returning to the game...';
var url=(j.deeplink||DEEP)+'?member='+encodeURIComponent(j.login)+'&token='+encodeURIComponent(j.token);
var t=document.getElementById('tok');t.style.display='block';t.textContent=url;
location.href=url;
}catch(e){msg.className='msg err';msg.textContent='network error';}}
</script></body></html>'''


async def http_handler(reader, writer):
    peer = writer.get_extra_info('peername')
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
    except Exception as e:
        log('[http] bad request from %s: %r' % (peer, e)); writer.close(); return
    text = head.decode('latin-1'); line = text.split('\r\n')[0]
    log('[http] %s %s' % (peer, line)); log('[http] headers: ' + text.replace('\r\n', ' | ')[:800])
    cl = 0
    for h in text.split('\r\n')[1:]:
        if h.lower().startswith('content-length:'): cl = int(h.split(':')[1])
    body = await reader.readexactly(cl) if cl else b''
    if body: log('[http] body: ' + re.sub(r'("token"\s*:\s*")[^"]*"', r'\1<hidden>"', body[:800].decode('utf-8', 'replace')))
    try:
        method, target, _ = line.split(' ')
    except ValueError:
        method, target = 'GET', '/'
    u = urlsplit(target); path = u.path.rstrip('/'); q = parse_qs(u.query)
    if CDN_DIR and '/AssetBundles/' not in path and path.lower().endswith('.mp4') and '/' not in path.strip('/'):
        video = os.path.join(os.path.dirname(os.path.normpath(CDN_DIR)), 'Video', path.strip('/'))
        if os.path.isfile(video): path = '/AssetBundles/../Video/' + path.strip('/')   # handled below (the one allowed escape)
    if '/AssetBundles/' in path:   # content: /<prefix>/AssetBundles/<file> -> CDN_DIR/<file> (read only, no path escapes, streamed)
        rel = unquote(path[path.find('/AssetBundles/') + len('/AssetBundles/'):])   # names with spaces arrive %20-encoded
        full = os.path.normpath(os.path.join(CDN_DIR, rel)) if CDN_DIR and rel else ''
        root = os.path.dirname(os.path.normpath(CDN_DIR)) if rel.startswith('../Video/') else os.path.normpath(CDN_DIR)
        if not full or ('..' in rel.split('/') and not rel.startswith('../Video/')) or not full.startswith(root) or not os.path.isfile(full):
            log('[cdn] %s -> 404' % rel); writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'); await writer.drain(); writer.close(); return
        size = os.path.getsize(full); log('[cdn] %s %s (%d bytes)' % (method, rel, size))
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % size)
        if method != 'HEAD':
            with open(full, 'rb') as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk: break
                    writer.write(chunk); await writer.drain()
        writer.close(); return
    if path.lower().endswith('regionlist'):
        regions = [{'regionId': 1, 'regionName': REGION_NAME, 'authAddress': '%s:%d' % (HOST_IP, AUTH_PORT),
                    'serviceTimeOffset': '0', 'state': 0, 'ping': '10'}]
        now = time.time()
        for i, (name, srv) in enumerate(sorted(HUB_SERVERS.items())):
            if now - srv['seen'] > HUB_TTL: continue   # offline servers are not offered
            regions.append({'regionId': 2 + i, 'regionName': name, 'authAddress': srv['authAddress'], 'serviceTimeOffset': '0', 'state': 0, 'ping': '10'})
        resp = {'error': 0, 'isQAAccount': False, 'accounts': [], 'regionInfo': regions}
    elif path.lower().endswith('get_service_info_v2'):
        resp = {'Code': '0', 'Url': 'https://live-web.dkmgservice.com:%d|0|0' % HTTPS_PORT, 'Msg': '', 'ext': CDN_ROOT, 'globalMsg': None, 'data': {'versionStatus': '3'}}
    elif path.lower().endswith('hub/register') and method == 'POST':
        try: j = json.loads(body.decode('utf-8'))
        except ValueError: j = {}
        name = str(j.get('name', ''))[:32].strip(); addr = str(j.get('authAddress', ''))[:64]
        if name and '@' not in name: name = 'Community@' + name   # the client shows only Group@Server names (left column = group)
        ok = HUB_KEY and j.get('key') == HUB_KEY and re.fullmatch(r'[A-Za-z0-9 @._-]{2,32}', name) and re.fullmatch(r'[A-Za-z0-9.-]+:\d{2,5}', addr)
        if ok: HUB_SERVERS[name] = {'authAddress': addr, 'seen': time.time(), 'players': int(j.get('players', 0) or 0)}
        resp = {'error': 0 if ok else 1}
    elif path.lower().endswith('hub/servers'):
        now = time.time()
        resp = {'servers': [{'name': n, 'online': now - v['seen'] <= HUB_TTL, 'players': v['players']} for n, v in sorted(HUB_SERVERS.items())]}
    elif path.lower().endswith('hub/auth') and method == 'POST':
        # A community server (or the APK) asks the hub to authenticate an account. Minting needs the shared server key.
        try: j = json.loads(body.decode('utf-8'))
        except ValueError: j = {}
        member = str(j.get('memberId', ''))[:64]; token = str(j.get('token', '') or ''); password = j.get('password')
        if j.get('serverKey') and not hmac.compare_digest(str(j.get('serverKey')), HUB_AUTH_SECRET or '\0'):
            resp = {'error': 1, 'message': 'bad server key'}
        else:
            err, login, no, tok = hub_authenticate(member, token, password)
            resp = {'error': err, 'message': stubdb.ERROR_TEXT.get(err, ''), 'login': login, 'accountNo': no, 'token': tok}
    elif path.lower().endswith('hub/validate'):
        hv = hub_verify(q.get('token', [''])[0])
        resp = {'error': 0, 'login': hv[0], 'accountNo': hv[1]} if hv else {'error': 1}
    elif path.lower().rstrip('/').endswith('/account/register') and method == 'POST':
        try: j = json.loads(body.decode('utf-8'))
        except ValueError: j = {}
        login = str(j.get('login', ''))[:64].strip(); pw = str(j.get('password', '')); email = str(j.get('email', ''))[:128].strip()
        err = DB.create(login, pw) if HUB_AUTH_SECRET else stubdb.ERR_NO_ACCOUNT
        if err == 0 and email: DB.set_email(login, email)
        a = DB.account(login) if err == 0 else None
        resp = {'error': err, 'message': stubdb.ERROR_TEXT.get(err, ''),
                'login': login, 'token': hub_sign(login, a['id']) if a else '', 'deeplink': DEEPLINK}
    elif path.lower().rstrip('/').endswith('/account/login') and method == 'POST':
        try: j = json.loads(body.decode('utf-8'))
        except ValueError: j = {}
        login = str(j.get('login', ''))[:64].strip()
        err, who, no, tok = hub_authenticate(login, '', str(j.get('password', '')))
        resp = {'error': err, 'message': stubdb.ERROR_TEXT.get(err, ''), 'login': who, 'token': tok, 'deeplink': DEEPLINK}
    elif path.lower().rstrip('/').endswith('/account') and method == 'GET':
        page = ACCOUNT_PAGE.replace('__DEEPLINK__', DEEPLINK)
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nContent-Length: %d\r\nConnection: close\r\n\r\n'
                     % len(page.encode()) + page.encode())
        await writer.drain(); writer.close(); return
    elif path.lower().endswith('login'):
        member = q.get('memberId', [''])[0]; token = ''
        try:
            j = json.loads(body.decode('utf-8')) if body else {}
            member = j.get('memberId', member) or member; token = j.get('token', '') or ''
        except ValueError:
            pass
        if not member:
            err = stubdb.ERR_NO_ACCOUNT
        elif HUB_AUTH_URL:                               # community server: the hub owns the account
            err, hub_login, _ = await hub_delegate(member, token)
            if err == 0:
                member = hub_login; DB.ensure(member, kind='hub')   # local shadow for this server's own world/state
        else:                                            # hub or standalone server
            err = sign_in(member, token, 'http-login')
        lkey = DB.new_session(member)[0] if err == 0 else 0
        resp = {'error': err, 'message': stubdb.ERROR_TEXT.get(err, ''), 'remainSec': None, 'banExpireTime': None, 'loginKey': lkey,
                'wemixWebServerUrl': '', 'xsollaProjectID': '', 'xsollaSandBox': True}
    else:
        resp = {'error': 0}
    payload = json.dumps(resp).encode()
    log('[http] reply: ' + re.sub(r'"loginKey": \d+', '"loginKey": <hidden>', payload.decode()))
    writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(payload) + payload)
    await writer.drain(); writer.close()


def set_guests(v):
    global GUESTS_ALLOWED; GUESTS_ALLOWED = bool(v); log('[panel] guests %s' % ('allowed' if GUESTS_ALLOWED else 'blocked'))


def set_region(v):
    global REGION_NAME
    if v: REGION_NAME = v; log('[panel] region name = %s' % REGION_NAME)


def panel_config():
    return {'regionName': REGION_NAME, 'host': ADV_GAME_HOST or HOST_IP, 'authPort': AUTH_PORT,
            'gamePort': ADV_GAME_PORT or GAME_PORT, 'httpsPort': HTTPS_PORT, 'guests': GUESTS_ALLOWED,
            'isHub': IS_HUB, 'hubAuthUrl': HUB_AUTH_URL, 'hubAuth': bool(HUB_AUTH_SECRET),
            'cdnRoot': CDN_ROOT, 'hubTtl': HUB_TTL}


def panel_notice(text):
    n = 0
    for sid, s in list(SESSIONS.items()):
        if not s['writer'].is_closing():
            try: system_line(sid, text); n += 1
            except Exception: pass
    return n


def panel_kick(sid):
    s = SESSIONS.get(sid)
    if not s: return False
    try: s['writer'].close()
    except Exception: pass
    return True


async def main():
    s1 = await asyncio.start_server(http_handler, '0.0.0.0', HTTP_PORT)
    s2 = await asyncio.start_server(lambda r, w: tcp_handler(r, w, 'auth'), '0.0.0.0', AUTH_PORT)
    s3 = await asyncio.start_server(lambda r, w: tcp_handler(r, w, 'game'), '0.0.0.0', GAME_PORT)
    here = os.path.dirname(os.path.abspath(__file__))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(os.path.join(here, 'certs', 'live-web.pem'), os.path.join(here, 'certs', 'live-web.key'))
    s4 = await asyncio.start_server(http_handler, '0.0.0.0', HTTPS_PORT, ssl=ctx)
    if CDN_DIR:
        s5 = await asyncio.start_server(http_handler, '0.0.0.0', CDN_PORT, ssl=ctx); asyncio.ensure_future(s5.serve_forever())
        log('cdn files from %s on :%d (service info ext = %s)' % (CDN_DIR, CDN_PORT, CDN_ROOT))
    log('hub: region %s, registration %s, ttl %d s' % (REGION_NAME, 'on' if HUB_KEY else 'off', HUB_TTL))
    if HUB_URL and HUB_KEY: asyncio.ensure_future(hub_heartbeat_loop()); log('hub: registering as %s at %s' % (REGION_NAME, HUB_URL))
    asyncio.ensure_future(party_sync_loop())
    asyncio.ensure_future(world_event_loop())
    log('world bosses: %s (every %s s, map %s)' % (sorted(WORLD_BOSSES), BOSS_EVERY or 'schedule', BOSS_MAP or 'own'))
    log('https on :%d' % HTTPS_PORT)
    if HUB_AUTH_SECRET:
        log('hub accounts: %s, network secret set%s' % ('HUB (authority)' if IS_HUB else ('community -> ' + HUB_AUTH_URL if HUB_AUTH_URL else 'local'), ''))
    if ADMIN_PORT:
        import panel
        ctx = {'db': DB, 'sessions': SESSIONS, 'store': STORE, 'save': lambda: save_store(STORE),
               'notice': panel_notice, 'kick': panel_kick, 'config': panel_config,
               'set_guests': set_guests, 'set_region': set_region, 'hub_servers': HUB_SERVERS,
               'errtext': stubdb.ERROR_TEXT, 'log': log,
               'hero_stats': hero_stats, 'levels': LEVELS, 'bag_add': bag_add, 'message': system_line}
        s6 = await panel.serve(ctx, ADMIN_IP, ADMIN_PORT); asyncio.ensure_future(s6.serve_forever())
    log('stub up: http :%d  auth :%d  game :%d  (advertised host %s, frame mode %s)' % (HTTP_PORT, AUTH_PORT, GAME_PORT, HOST_IP, 'swift-default'))
    async with s1, s2, s3, s4:
        await asyncio.gather(s1.serve_forever(), s2.serve_forever(), s3.serve_forever(), s4.serve_forever())

if __name__ == '__main__':
    asyncio.run(main())
