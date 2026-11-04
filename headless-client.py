# Headless Dekaron G client against the stub (tools/stub/stub-server.py): login -> hero list (create if none) -> select ->
# map loading complete -> moves / chat, logging every row it receives. Used to check the multi-player broadcast with two
# clients at once:  python -I headless-client.py <account> [seconds] [--move x,z] [--chat text]
import asyncio, importlib.util, os, struct, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location('stub', os.path.join(HERE, 'stub-server.py'))
stub = importlib.util.module_from_spec(spec); spec.loader.exec_module(stub)
w_i8, w_i32, w_i64, w_str, w_bool, w_f32, w_i16, w_u32 = stub.w_i8, stub.w_i32, stub.w_i64, stub.w_str, stub.w_bool, stub.w_f32, stub.w_i16, stub.w_u32

NAMES = {0x91111: 'SN_Enter hero', 0x91401: 'SN_Enter npc', 0x91201: 'SN_Enter monster', 0x92101: 'SN_Action', 0x92105: 'SN_Leave',
         0x91802: 'SN_Chat', 0x90133: 'SN_HeroData', 0x92715: 'SA_MapLoadingComplete', 0x92732: 'SN_MapMove', 0x91102: 'SN_Shape'}


async def run(account, seconds, move, chat, invite=None, letter=None, mail=None, password='', register=False, clan_create=None, clan_join=None, sell=None, buy=None):
    reader, writer = await asyncio.open_connection('127.0.0.1', stub.GAME_PORT)
    c = stub.SwiftCipher(); buf = b''; t0 = time.time(); tag = account[:10]
    seen = {}
    def send(code, body):
        writer.write(c.encrypt(code, body))
    state = {'stage': 'login', 'hero': None, 'map': 0, 'pos': None, 'moved': False, 'chatted': False, 'invited': False, 'mailed': False, 'clan': 0, 'bag': {}}
    if register:
        send(0x9F001, w_str(account) + w_str(password))   # ours: CQ_Register -> 0x9F002 { int error }
    send(0x90101, w_str(account) + w_i64(int(time.time() * 1000)) + w_i8(0) + w_str(password) + w_i32(1) + w_i32(1) + w_str('AndroidW_0.1.356') + w_str('headless'))
    while time.time() - t0 < seconds:
        try:
            data = await asyncio.wait_for(reader.read(65536), 0.5)
        except asyncio.TimeoutError:
            data = b''
        if data == b'' and reader.at_eof():
            print('[%s] closed by server' % tag); break
        buf += data
        rows, buf = c.decrypt_rows(buf)
        for code, body, fr, ok in rows:
            if code is None:
                continue
            seen[code] = seen.get(code, 0) + 1
            if code == 0x9F002:
                print('[%s] register -> error %d' % (tag, struct.unpack_from('<i', body)[0]))
            if code == 0x90102 and struct.unpack_from('<i', body)[0] != 0:
                print('[%s] LOGIN REFUSED error %d' % (tag, struct.unpack_from('<i', body)[0])); writer.close(); return
            if code == 0x90102:   # SA_Login
                send(0x90111, b'')
            elif code == 0x90112:   # SA_HeroList { int error; HeroInfo[]; byte }
                n = struct.unpack_from('<i', body, 4)[0]
                if n == 0:
                    send(0x90113, w_i32(910101) + w_str('Bot' + account[-3:]))
                else:
                    no = struct.unpack_from('<q', body, 8)[0]
                    send(0x90131, w_i64(no) + w_i64(int(time.time() * 1000)))
            elif code == 0x90114:   # SA_CreateHero
                no = struct.unpack_from('<q', body, 5)[0]
                send(0x90131, w_i64(no) + w_i64(int(time.time() * 1000)))
            elif code == 0x92732:   # SN_MapMove { MapEnterInfo { int map; int hp; int mp; float x,y,z ... } }
                state['map'] = struct.unpack_from('<i', body)[0]; state['pos'] = struct.unpack_from('<fff', body, 12)
                send(0x92714, b'')
            elif code == 0x91111:
                m, cls, no = struct.unpack_from('<IiQ', body); nl = struct.unpack_from('<H', body, 16)[0]; nm = body[18:18 + nl].decode()
                print('[%s] %.1fs SN_Enter hero member %d %s (class %d)' % (tag, time.time() - t0, m, nm, cls))
                if mail and nm == mail.split('|')[0]: state['target_seen'] = True
            elif code == 0x92101:
                kind = body[4]; m = struct.unpack_from('<I', body, 27)[0]
                if m >= stub.OTHER_BASE:
                    print('[%s] %.1fs SN_Action kind %d from member %d' % (tag, time.time() - t0, kind, m))
            elif code == 0x92105:
                n = struct.unpack_from('<h', body, 1)[0]; ms = [struct.unpack_from('<I', body, 3 + 4 * i)[0] for i in range(n)]
                if any(m >= stub.OTHER_BASE for m in ms):
                    print('[%s] %.1fs SN_Leave %r' % (tag, time.time() - t0, ms))
            elif code == 0x91802:
                o = 13; nl = struct.unpack_from('<H', body, o)[0]; nm = body[o + 2:o + 2 + nl].decode(); o += 2 + nl
                ml = struct.unpack_from('<H', body, o)[0]; msg = body[o + 2:o + 2 + ml].decode(); o += 2 + ml
                member = struct.unpack_from('<i', body, o)[0]
                print('[%s] %.1fs SN_Chat from %s (member %d): %s' % (tag, time.time() - t0, nm, member, msg))
            elif code == 0xE0124:   # SN_AddInvited { InviteInfo[] { long heroNo; string name; DateTime } }: accept every friend invitation
                n = struct.unpack_from('<i', body)[0]; o = 4
                for _ in range(n):
                    hn = struct.unpack_from('<q', body, o)[0]; nl = struct.unpack_from('<H', body, o + 8)[0]; nm = body[o + 10:o + 10 + nl].decode(); o += 10 + nl + 12
                    print('[%s] %.1fs friend invite from %s: accepting' % (tag, time.time() - t0, nm)); send(0xE0105, w_i64(hn))
                    if letter: send(0xC0123, w_str(nm) + w_str(letter.split('|')[0]) + w_str(letter.split('|')[-1]))
            elif code in (0xE0142, 0xE0121, 0xE0131, 0xE0106, 0xC0124, 0xC0122):
                print('[%s] %.1fs %s %d bytes' % (tag, time.time() - t0, {0xE0142: 'SA_FriendLogin', 0xE0121: 'SN_AddFriend', 0xE0131: 'SN_Online', 0xE0106: 'SA_InvitedAccept', 0xC0124: 'SA_SendLetter', 0xC0122: 'SA_LetterList'}[code], len(body)))
            elif code in (0x90136, 0xA0413):   # bag listings: remember serial -> (index, count) for --sell
                try:
                    off = 0; n = struct.unpack_from('<i', body, off)[0]; off += 4
                    for _ in range(n):
                        serial, idx, cnt = struct.unpack_from('<qii', body, off); off += 20; has = body[off]; off += 1 + (8 if has else 0); off += 4; off += 4 * 3   # ItemInfo + 3 empty arrays
                        if cnt > 0: state['bag'][serial] = (idx, cnt)
                except struct.error:
                    pass
            elif code in (0xD0202, 0xD0102, 0xD0104, 0xD0108, 0xD020D, 0xD0206):
                err = struct.unpack_from('<i', body)[0]; cn = {0xD0202: 'SA_Sell', 0xD0102: 'SA_Buy', 0xD0104: 'SA_ViewItemsItemIndex', 0xD0108: 'SA_ViewItemsDetail', 0xD020D: 'SA_ReceiveComplete', 0xD0206: 'SA_RegistList'}[code]
                extra = ' error %d' % err
                if code == 0xD0104 and err == 0:
                    n = struct.unpack_from('<i', body, 4)[0]; extra += ' groups %d' % n
                    if n: idx = struct.unpack_from('<i', body, 8)[0]; send(0xD0107, w_i32(idx) + w_i8(255) + w_i16(0) + w_i8(0) + w_bool(True) + w_i32(0))
                if code == 0xD0108 and err == 0:
                    n = struct.unpack_from('<i', body, 4)[0]; extra += ' listings %d' % n
                    if n and buy and not state.get('bought'):
                        tno = struct.unpack_from('<q', body, 8)[0]; price = struct.unpack_from('<i', body, 8 + 8 + 4 + 4 + 1 + 4 + 1 + 4 + 1 + 4)[0]
                        extra += ': buying %d for %d' % (tno, price); send(0xD0101, w_i64(tno) + w_i32(price)); state['bought'] = True
                print('[%s] %.1fs %s%s' % (tag, time.time() - t0, cn, extra))
            elif code in (0xB0102, 0xB0104, 0xB010A, 0xB0112, 0xB0113, 0xB0114, 0xB0115, 0xB0116, 0xB0129, 0xB0133, 0xB0138, 0xB013B, 0xB0143, 0xB0163, 0xB0166):
                cn = {0xB0102: 'SA_ClanCreate', 0xB0104: 'SA_ClannerLogin', 0xB010A: 'SA_ClanJoin', 0xB0112: 'SN_ClannerLogin', 0xB0113: 'SN_ClannerKick', 0xB0114: 'SN_ClanJoin', 0xB0115: 'SN_ClannerLogout',
                      0xB0116: 'SN_ClannerLeave', 0xB0129: 'SA_ClanSearch', 0xB0133: 'SN_ApplyApplication', 0xB0138: 'SN_AcceptToApplication', 0xB013B: 'SN_RejectToApplication', 0xB0143: 'SN_ChangeClanInfo', 0xB0163: 'SN_ChangeGrade', 0xB0166: 'SN_ChangeMaster'}[code]
                extra = ''
                if code in (0xB0102, 0xB010A, 0xB0129):
                    err = struct.unpack_from('<i', body)[0]; extra = ' error %d' % err
                    if code == 0xB0129 and err == 0:
                        cno = struct.unpack_from('<q', body, 4)[0]; extra += ' clan %d: joining' % cno; send(0xB0109, w_i64(cno))
                    if code == 0xB0102 and err == 0: state['clan'] = struct.unpack_from('<q', body, 4)[0]
                    if code == 0xB010A and err == 0: state['clan'] = struct.unpack_from('<q', body, 4)[0]
                if code == 0xB0114:
                    nl = struct.unpack_from('<H', body, 16)[0]; extra = ' ' + body[18:18 + nl].decode('utf-8', 'replace')
                print('[%s] %.1fs %s%s' % (tag, time.time() - t0, cn, extra))
                if code in (0xB0102, 0xB010A) and state['clan']: send(0xB0103, b'')   # CQ_ClannerLogin for the member list
            elif code == 0x93823:   # SN_InvitePartyRequest { long partyIndex; PartyInviteInfo { long charNo; string nick; int heroIndex; byte } }
                pi = struct.unpack_from('<q', body)[0]; nl = struct.unpack_from('<H', body, 16)[0]; nm = body[18:18 + nl].decode()
                print('[%s] %.1fs party invite from %s (party %d): accepting' % (tag, time.time() - t0, nm, pi))
                send(0x93824, w_i64(pi) + w_i8(0))
            elif code in (0x93802, 0x93822, 0x93825, 0x93826, 0x93833, 0x93861):
                names = {0x93802: 'SN_CreateParty', 0x93822: 'SN_InviteParty', 0x93825: 'SN_InvitePartyResponse', 0x93826: 'SN_InvitePartyNotify', 0x93833: 'SN_LeavePartyNotify', 0x93861: 'SN_UpdateMemberInfo'}
                if code != 0x93861 or seen[code] <= 2: print('[%s] %.1fs %s %d bytes' % (tag, time.time() - t0, names[code], len(body)))
            elif code == 0x92715:
                print('[%s] %.1fs in map %d at %r' % (tag, time.time() - t0, state['map'], state['pos']))
                send(0xE0141, w_i32(0))   # NFriend CQ_Login
        el = time.time() - t0
        if state['map'] and move and not state['moved'] and el > 6:
            x, y, z = state['pos']; mx, mz = move
            send(0x92102, stub.action_move(1, x, y, z, mx, y, mz, 600)); state['moved'] = True
        if state['map'] and invite and not state['invited'] and el > 7:
            send(0x93821, w_i8(2) + w_u32(0) + w_i64(0) + w_str(invite)); state['invited'] = True
        if state['map'] and mail and not state['mailed'] and (el > 110 or state.get('target_seen')):
            to, title, msg = (mail.split('|') + ['', ''])[:3]; send(0xC0123, w_str(to) + w_str(title) + w_str(msg)); state['mailed'] = True
        if state['map'] and clan_create and not state.get('clan_sent') and el > 5:
            send(0xB0101, w_str(clan_create) + w_i16(3) + w_i32(1) + w_str('Bot clan') + w_str('Welcome')); state['clan_sent'] = True
        if state['map'] and clan_join and not state['clan'] and el > 5 and el - state.get('clan_t', -9) > 5:
            send(0xB0128, w_str(clan_join)); state['clan_t'] = el   # CQ_ClanSearch every 5 s until the clan exists -> join on the answer
        if state['map'] and sell and not state.get('sold') and el > 4:
            want, price = sell
            for serial, (idx, cnt) in state['bag'].items():
                if idx == want:
                    print('[%s] %.1fs listing serial %d (item %d x%d) for %d' % (tag, el, serial, idx, cnt, price)); send(0xD0201, w_i64(serial) + w_i32(cnt) + w_i32(price)); break
            else:
                print('[%s] %.1fs nothing of item %d in the bag: %r' % (tag, el, want, list(state['bag'].values())[:6]))
            state['sold'] = True
        if state['map'] and buy and not state.get('bought') and el > 4 and el - state.get('ask_t', -9) > 5:
            send(0xD0103, w_i16(1) + w_i32(buy) + w_i32(7)); state['ask_t'] = el   # CQ_ViewItemsItemIndex { int[] {buy}; cacheKey 7 } every 5 s until bought
        if state['map'] and chat and not state['chatted'] and el > 8:
            send(0x91801, w_i32(1) + w_i8(0) + w_str('') + w_str(chat)); state['chatted'] = True
        if int(el) % 5 == 0 and el - int(el) < 0.5 and state['map']:
            send(0x10008, w_i64(int(time.time() * 1000)))
    print('[%s] rows: %s' % (tag, ', '.join('%s x%d' % (NAMES.get(k, hex(k)), v) for k, v in sorted(seen.items()))))
    writer.close()


if __name__ == '__main__':
    acc = sys.argv[1]; secs = float(sys.argv[2]) if len(sys.argv) > 2 and not sys.argv[2].startswith('--') else 20
    move = chat = invite = None
    if '--invite' in sys.argv: invite = sys.argv[sys.argv.index('--invite') + 1]
    letter = sys.argv[sys.argv.index('--letter') + 1] if '--letter' in sys.argv else None
    mail = sys.argv[sys.argv.index('--mail') + 1] if '--mail' in sys.argv else None
    if '--move' in sys.argv: move = tuple(float(v) for v in sys.argv[sys.argv.index('--move') + 1].split(','))
    if '--chat' in sys.argv: chat = sys.argv[sys.argv.index('--chat') + 1]
    password = sys.argv[sys.argv.index('--password') + 1] if '--password' in sys.argv else ''
    cc = sys.argv[sys.argv.index('--clan-create') + 1] if '--clan-create' in sys.argv else None
    cj = sys.argv[sys.argv.index('--clan-join') + 1] if '--clan-join' in sys.argv else None
    sell = tuple(int(v) for v in sys.argv[sys.argv.index('--sell') + 1].split(',')) if '--sell' in sys.argv else None
    buy = int(sys.argv[sys.argv.index('--buy') + 1]) if '--buy' in sys.argv else None
    asyncio.run(run(acc, secs, move, chat, invite, letter, mail, password, '--register' in sys.argv, cc, cj, sell, buy))
