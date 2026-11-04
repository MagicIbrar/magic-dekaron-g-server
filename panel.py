"""Dekaron G server management panel (2026-11-04): a small web admin for the stub, in the spirit of Magic Dekaron's admin
panel. Served in-process by stub-server.py on its own port (ADMIN_PORT, default 8010, bound to 127.0.0.1 by default).

Auth: one admin password, scrypt-hashed in panel-admin.json next to this file. On first start a random password is written to
PANEL-FIRST-LOGIN.txt (printed once in the log). A cookie session (random token, kept in memory) is required on every /api
route except /api/login. Writes are audited through the same stubdb audit log.

What it can do now: dashboard (online players, accounts, heroes, regions, config), online players (broadcast, kick), accounts
(search, view, ban/unban, role player|gm|operator, reset/set password, create, delete, heroes), offline character JSON edit,
registered community servers, live toggles (guests, region name), and a log tail (stub.log + audit). Full server lifecycle
(start/stop/restart as a manager process) is a follow-up; see HUB.md.

Usage from stub-server.py:
    import panel
    await panel.serve(ctx, ip, port)        # ctx = a dict of hooks (see serve())
"""
import asyncio, json, os, secrets, hashlib, hmac, time, html
from urllib.parse import urlsplit, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
ADMIN_FILE = os.environ.get('STUB_PANEL_ADMIN', os.path.join(HERE, 'panel-admin.json'))
FIRST_LOGIN = os.environ.get('STUB_PANEL_FIRST', os.path.join(HERE, 'PANEL-FIRST-LOGIN.txt'))
SESSIONS = {}                 # cookie token -> created time
SESSION_TTL = 8 * 3600
CTX = {}                      # hooks from stub-server.py


def _hash(pw, salt):
    return hashlib.scrypt(pw.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def _load_admin():
    """Return the admin record, creating a random first password on first run."""
    if os.path.exists(ADMIN_FILE):
        return json.load(open(ADMIN_FILE, encoding='utf-8'))
    pw = secrets.token_urlsafe(12)
    salt = secrets.token_bytes(16)
    rec = {'salt': salt.hex(), 'hash': _hash(pw, salt).hex()}
    json.dump(rec, open(ADMIN_FILE, 'w', encoding='utf-8'))
    open(FIRST_LOGIN, 'w', encoding='utf-8').write(
        'Magic Dekaron G panel - first admin password:\n\n    %s\n\nSign in at the panel, then change it under Settings. '
        'Delete this file afterwards.\n' % pw)
    CTX.get('log', print)('[panel] first admin password written to %s (set a new one in the panel)' % FIRST_LOGIN)
    return rec


def _check_password(pw):
    rec = _load_admin()
    return hmac.compare_digest(_hash(pw, bytes.fromhex(rec['salt'])).hex(), rec['hash'])


def _set_password(pw):
    salt = secrets.token_bytes(16)
    json.dump({'salt': salt.hex(), 'hash': _hash(pw, salt).hex()}, open(ADMIN_FILE, 'w', encoding='utf-8'))


def _cookie_token(headers):
    for h in headers:
        if h.lower().startswith('cookie:') and 'dkg_admin=' in h:
            return h.split('dkg_admin=', 1)[1].split(';')[0].strip()
    return ''


def _authed(headers):
    t = _cookie_token(headers)
    if t in SESSIONS and time.time() - SESSIONS[t] < SESSION_TTL:
        return True
    SESSIONS.pop(t, None)
    return False


# ---- API handlers (return a JSON-able dict, or raise ValueError(msg) for a 400) --------------------------------------
def api_dashboard(q, body):
    db = CTX['db']; sess = CTX['sessions']
    now = time.time()
    online = [s for s in sess.values() if not s['writer'].is_closing()]
    regions = CTX['hub_servers']
    return {
        'regionName': CTX['config']()['regionName'],
        'onlinePlayers': len([s for s in online if s.get('hero')]),
        'onlineSessions': len(online),
        'accounts': len(db.list()),
        'heroes': len(db.heroes()),
        'communityServers': len([1 for v in regions.values() if now - v['seen'] <= CTX['config']()['hubTtl']]),
        'config': CTX['config'](),
    }


def api_players(q, body):
    sess = CTX['sessions']; out = []
    for sid, s in sess.items():
        if s['writer'].is_closing():
            continue
        peer = s['writer'].get_extra_info('peername')
        hero = s.get('h') or {}
        out.append({'sid': sid, 'account': s.get('name', ''), 'hero': hero.get('name', ''),
                    'level': hero.get('level', 0), 'map': (s.get('hero') or {}).get('map', hero.get('map', 0)),
                    'ip': peer[0] if peer else ''})
    return {'players': out}


def api_notice(q, body):
    text = str(body.get('text', '')).strip()[:200]
    if not text:
        raise ValueError('empty notice')
    n = CTX['notice'](text)
    CTX['db'].audit('admin', 'notice', text); CTX['db'].con.commit()
    return {'sent': n}


def api_kick(q, body):
    sid = int(body.get('sid', 0))
    ok = CTX['kick'](sid)
    CTX['db'].audit('admin', 'kick', 'session %d' % sid); CTX['db'].con.commit()
    return {'ok': ok}


def api_accounts(q, body):
    return {'accounts': CTX['db'].list(q.get('q', [''])[0] or None)}


def api_account(q, body):
    login = q.get('login', [''])[0]
    db = CTX['db']; a = db.account(login)
    if a is None:
        raise ValueError('no such account')
    state = CTX['store'].get(login, {})
    heroes = [{'no': h.get('no'), 'name': h.get('name'), 'index': h.get('index'), 'level': h.get('level'), 'map': h.get('map')}
              for h in state.get('heroes', [])]
    online = any(s.get('name') == login and not s['writer'].is_closing() for s in CTX['sessions'].values())
    return {'login': a['login'], 'kind': a['kind'], 'role': a['role'], 'email': a['email'] or '',
            'banned': (a['banned_until'] or 0) > time.time(), 'banReason': a['ban_reason'] or '',
            'online': online, 'gold': state.get('gold', 0), 'heroes': heroes}


def api_account_ban(q, body):
    login = str(body.get('login', '')); seconds = int(body.get('seconds', 0)); reason = str(body.get('reason', ''))[:120]
    if CTX['db'].account(login) is None:
        raise ValueError('no such account')
    CTX['db'].ban(login, seconds, reason)
    return {'ok': True}


def api_account_role(q, body):
    login = str(body.get('login', '')); role = str(body.get('role', ''))
    if not CTX['db'].set_role(login, role):
        raise ValueError('bad role')
    return {'ok': True}


def api_account_password(q, body):
    login = str(body.get('login', '')); pw = str(body.get('password', ''))
    err = CTX['db'].set_password(login, pw)
    if err:
        raise ValueError(CTX['errtext'].get(err, 'error %d' % err))
    return {'ok': True}


def api_account_create(q, body):
    login = str(body.get('login', '')).strip(); pw = body.get('password') or None
    err = CTX['db'].create(login, pw)
    if err:
        raise ValueError(CTX['errtext'].get(err, 'error %d' % err))
    return {'ok': True}


def api_account_delete(q, body):
    login = str(body.get('login', ''))
    if any(s.get('name') == login and not s['writer'].is_closing() for s in CTX['sessions'].values()):
        raise ValueError('account is online; kick it first')
    CTX['db'].delete(login); CTX['store'].pop(login, None)
    return {'ok': True}


def api_hero_state(q, body):
    """Read an OFFLINE account's full state JSON for editing."""
    login = q.get('login', [''])[0]
    if any(s.get('name') == login and not s['writer'].is_closing() for s in CTX['sessions'].values()):
        raise ValueError('account is online; edits apply only while offline')
    if CTX['db'].account(login) is None:
        raise ValueError('no such account')
    return {'login': login, 'state': CTX['store'].get(login, {})}


def api_hero_save(q, body):
    login = str(body.get('login', ''))
    if any(s.get('name') == login and not s['writer'].is_closing() for s in CTX['sessions'].values()):
        raise ValueError('account is online; edits apply only while offline')
    if CTX['db'].account(login) is None:
        raise ValueError('no such account')
    try:
        state = body['state'] if isinstance(body.get('state'), dict) else json.loads(body.get('state', '{}'))
    except ValueError:
        raise ValueError('state is not valid JSON')
    CTX['store'][login] = state
    CTX['save']()
    CTX['db'].audit(login, 'admin_edit', 'character JSON'); CTX['db'].con.commit()
    return {'ok': True}


def api_servers(q, body):
    now = time.time(); ttl = CTX['config']()['hubTtl']
    return {'servers': [{'name': n, 'authAddress': v['authAddress'], 'players': v['players'],
                         'online': now - v['seen'] <= ttl, 'ageSec': int(now - v['seen'])}
                        for n, v in sorted(CTX['hub_servers'].items())]}


def api_config(q, body):
    if body:   # live-editable subset
        if 'guests' in body:
            CTX['set_guests'](bool(body['guests']))
        if 'regionName' in body:
            CTX['set_region'](str(body['regionName'])[:32])
        CTX['db'].audit('admin', 'config', json.dumps({k: body[k] for k in ('guests', 'regionName') if k in body}))
        CTX['db'].con.commit()
    return {'config': CTX['config']()}


def api_save(q, body):
    n = CTX['save']()
    return {'written': n}


def api_logs(q, body):
    n = int(q.get('n', ['120'])[0])
    lines = []
    try:
        with open(os.path.join(HERE, 'stub.log'), encoding='utf-8', errors='replace') as f:
            lines = f.readlines()[-n:]
    except OSError:
        pass
    audit = [{'at': r[0], 'login': r[1], 'what': r[2], 'detail': r[3]} for r in CTX['db'].audit_rows(80)]
    return {'log': ''.join(lines), 'audit': audit}


def api_set_admin_password(q, body):
    old = str(body.get('old', '')); new = str(body.get('new', ''))
    if not _check_password(old):
        raise ValueError('current password is wrong')
    if len(new) < 8:
        raise ValueError('new password must be 8+ characters')
    _set_password(new)
    try:
        os.remove(FIRST_LOGIN)
    except OSError:
        pass
    return {'ok': True}


# ---- character management (2026-11-04) ---------------------------------------------------------------------------
# Structural edits (level, stats, name, position, bag, gold, quests) need the account OFFLINE: the live session keeps its
# own copy of the hero and would overwrite the edit on its next save. Mail is the online-safe way to give items / gold:
# the mailbox is read live from the account record, so a player sees it the next time they open their mail.
import paneldata

STAT_KEYS = ('str', 'dex', 'int', 'con', 'agi', 'wis', 'points', 'levelpt')


def _online_sid(login):
    for sid, s in CTX['sessions'].items():
        if s.get('name') == login and not s['writer'].is_closing():
            return sid
    return None


def _need_offline(login):
    if CTX['db'].account(login) is None:
        raise ValueError('no such account')
    if _online_sid(login) is not None:
        raise ValueError('account is online - kick it first, then edit')


def _account_state(login):
    if CTX['db'].account(login) is None:
        raise ValueError('no such account')
    return CTX['store'].setdefault(login, {'heroes': [], 'country': 0})


def _hero(login, no):
    for h in _account_state(login).get('heroes', []):
        if int(h.get('no', 0)) == int(no):
            return h
    raise ValueError('no such character')


def _audit(login, what, detail=''):
    CTX['db'].audit(login, what, detail); CTX['db'].con.commit()


def _hero_view(login, h, online):
    st = CTX['hero_stats'](h)
    lv = int(h.get('level', 1)); levels = CTX['levels']
    pos = h.get('pos')
    return {'no': h.get('no'), 'name': h.get('name', ''), 'classIndex': h.get('index'), 'className': paneldata.class_name(h.get('index', 0)),
            'level': lv, 'exp': h.get('exp', 0), 'maxExp': levels.get(lv, 0), 'maxLevel': 120,
            'map': h.get('map') or 0, 'mapName': paneldata.map_name(h.get('map')) or 'Town start (default)',
            'pos': [round(float(v), 2) for v in pos] if pos else None,
            'hp': h.get('hp'), 'mp': h.get('mp'), 'dead': bool(h.get('dead')), 'penalties': len(h.get('penalties', [])),
            'stats': {k: st.get(k, 0) for k in STAT_KEYS}, 'online': online}


def api_hero(q, body):
    login = q.get('login', [''])[0]; no = q.get('no', ['0'])[0]
    h = _hero(login, no)
    return {'login': login, 'hero': _hero_view(login, h, _online_sid(login) is not None), 'destinations': paneldata.destinations()}


def api_hero_edit(q, body):
    login = str(body.get('login', '')); _need_offline(login); h = _hero(login, body.get('no', 0)); changes = []
    if 'level' in body:
        lv = max(1, min(120, int(body['level']))); h['level'] = lv; changes.append('level=%d' % lv)
    if 'exp' in body:
        cap = CTX['levels'].get(int(h.get('level', 1)), 0)
        ex = max(0, int(body['exp'])); h['exp'] = min(ex, cap - 1) if cap else ex; changes.append('exp=%d' % h['exp'])
    if isinstance(body.get('stats'), dict):
        st = CTX['hero_stats'](h)
        for k in STAT_KEYS:
            if k in body['stats']:
                st[k] = max(0, min(65535, int(body['stats'][k])))
        changes.append('stats')
    if body.get('name'):
        name = str(body['name']).strip()
        if not (2 <= len(name) <= 16) or any(c in name for c in '<>"\'\\/ '):
            raise ValueError('name: 2-16 characters, no spaces or < > " \' / \\')
        if name.lower() != str(h.get('name', '')).lower() and CTX['db'].hero_name_taken(name):
            raise ValueError('that character name is taken')
        changes.append('name %s->%s' % (h.get('name'), name)); h['name'] = name
    if body.get('revive'):
        h['dead'] = False; h.pop('hp', None); h.pop('mp', None); changes.append('revived')
    if body.get('clearPenalties'):
        h['penalties'] = []; changes.append('penalties cleared')
    CTX['save'](); _audit(login, 'admin_hero_edit', '%s: %s' % (h.get('name'), ', '.join(changes)))
    return {'ok': True, 'hero': _hero_view(login, h, False)}


def api_hero_teleport(q, body):
    login = str(body.get('login', '')); _need_offline(login); h = _hero(login, body.get('no', 0))
    if body.get('town'):
        h.pop('map', None); h.pop('pos', None); where = 'town start'
    else:
        m = int(body.get('map', 0))
        d = next((x for x in paneldata.destinations() if x['map'] == m), None)
        if d is None:
            raise ValueError('unknown destination map')
        pos = body.get('pos') or d['pos']
        h['map'] = m; h['pos'] = [float(v) for v in pos][:3]; where = '%s %s' % (d['name'], h['pos'])
    h['dead'] = False
    CTX['save'](); _audit(login, 'admin_teleport', '%s -> %s' % (h.get('name'), where))
    return {'ok': True, 'hero': _hero_view(login, h, False)}


def api_hero_delete(q, body):
    login = str(body.get('login', '')); _need_offline(login); h = _hero(login, body.get('no', 0))
    if hasattr(CTX['db'], 'clanner') and CTX['db'].clanner(h.get('no')):
        raise ValueError('character is in a clan - remove it from the clan first')
    st = _account_state(login); st['heroes'] = [x for x in st.get('heroes', []) if x is not h]
    CTX['save'](); _audit(login, 'admin_hero_delete', h.get('name', ''))
    return {'ok': True}


def _item_row(b, equipped_places):
    info = paneldata.item_info(b['index'])
    return {'serial': b['serial'], 'index': b['index'], 'name': paneldata.item_name(b['index']), 'count': b.get('count', 1),
            'type': info['type'], 'rarity': info['rarity'], 'slot': info['slot'], 'equipped': equipped_places.get(b['serial'], '')}


def api_inventory(q, body):
    login = q.get('login', [''])[0]; st = _account_state(login)
    suit = st.get('suit', {}) or {}
    equipped = {int(serial): paneldata.SLOTS.get(int(place), 'Slot %s' % place) for place, serial in suit.items()}
    bag = [_item_row(b, equipped) for b in st.get('bag', [])]
    mails = [{'no': m['no'], 'type': m.get('type'), 'title': m.get('title', ''), 'msg': m.get('msg', ''), 'time': m.get('time'),
              'reward': ('%s x%d' % (paneldata.item_name(m['index']), m.get('value', 1))) if m.get('type') == 1
              else ('%d gold' % m.get('value', 0)) if m.get('type') == 13 and m.get('index') == 2 else ''}
             for m in st.get('mails', [])]
    return {'login': login, 'online': _online_sid(login) is not None, 'gold': st.get('gold', 0), 'bag': bag, 'mails': mails,
            'questsDone': len(st.get('quests_done', [])), 'attendance': st.get('attend', {})}


def api_inventory_add(q, body):
    login = str(body.get('login', '')); _need_offline(login)
    idx = int(body.get('index', 0)); count = max(1, min(9999, int(body.get('count', 1))))
    if not paneldata.item_info(idx)['exists']:
        raise ValueError('no such item index')
    st = _account_state(login)
    if paneldata.item_info(idx)['stackable']:
        CTX['bag_add'](st, idx, count)
    else:
        for _ in range(min(count, 50)):   # equipment never stacks: one entry each (capped at 50 per call)
            CTX['bag_add'](st, idx, 1)
    CTX['save'](); _audit(login, 'admin_item_add', '%s x%d' % (paneldata.item_name(idx), count))
    return {'ok': True}


def api_inventory_remove(q, body):
    login = str(body.get('login', '')); _need_offline(login); st = _account_state(login)
    serial = int(body.get('serial', 0)); n = body.get('count')
    b = next((x for x in st.get('bag', []) if int(x['serial']) == serial), None)
    if b is None:
        raise ValueError('no such item in the bag')
    if n is not None and 0 < int(n) < b.get('count', 1):
        b['count'] -= int(n); what = '%s -%d' % (paneldata.item_name(b['index']), int(n))
    else:
        st['bag'] = [x for x in st['bag'] if x is not b]
        st['suit'] = {p: s for p, s in (st.get('suit') or {}).items() if int(s) != serial}
        what = '%s removed' % paneldata.item_name(b['index'])
    CTX['save'](); _audit(login, 'admin_item_remove', what)
    return {'ok': True}


def api_inventory_unequip(q, body):
    login = str(body.get('login', '')); _need_offline(login); st = _account_state(login)
    serial = int(body.get('serial', 0))
    st['suit'] = {p: s for p, s in (st.get('suit') or {}).items() if int(s) != serial}
    CTX['save'](); _audit(login, 'admin_unequip', 'serial %d' % serial)
    return {'ok': True}


def api_gold(q, body):
    login = str(body.get('login', '')); _need_offline(login); st = _account_state(login)
    if 'set' in body:
        st['gold'] = max(0, min(2_000_000_000, int(body['set'])))
    else:
        st['gold'] = max(0, min(2_000_000_000, st.get('gold', 0) + int(body.get('add', 0))))
    CTX['save'](); _audit(login, 'admin_gold', str(st['gold']))
    return {'ok': True, 'gold': st['gold']}


_MAIL_SEQ = [0]


def _make_mail(kind, index, count, title, msg):
    _MAIL_SEQ[0] = (_MAIL_SEQ[0] + 1) % 100
    no = int(time.time() * 1000) * 100 + _MAIL_SEQ[0]
    if kind == 'gold':
        return {'no': no, 'type': 13, 'index': 2, 'value': max(1, int(count)), 'sub': 0, 'title': title, 'msg': msg, 'time': int(time.time())}
    if not paneldata.item_info(index)['exists']:
        raise ValueError('no such item index')
    return {'no': no, 'type': 1, 'index': int(index), 'value': max(1, min(9999, int(count))), 'sub': 0, 'title': title, 'msg': msg, 'time': int(time.time())}


def api_mail_send(q, body):
    """Online-safe gift: a system mail with an item or gold, to one account or to every account."""
    kind = 'gold' if body.get('kind') == 'gold' else 'item'
    title = str(body.get('title') or 'A gift from the team')[:40]; msg = str(body.get('msg') or '')[:200]
    targets = [str(body.get('login', ''))] if not body.get('all') else [a['login'] for a in CTX['db'].list()]
    sent = 0
    for login in targets:
        if CTX['db'].account(login) is None:
            continue
        st = _account_state(login)
        st['mails'] = st.get('mails', []) + [_make_mail(kind, body.get('index', 0), body.get('count', 1), title, msg)]
        sent += 1
    if not sent:
        raise ValueError('no such account')
    CTX['save']()
    what = ('%d gold' % int(body.get('count', 1))) if kind == 'gold' else ('%s x%s' % (paneldata.item_name(body.get('index', 0)), body.get('count', 1)))
    _audit('ALL' if body.get('all') else targets[0], 'admin_mail', '%s to %d account(s): %s' % (what, sent, title))
    for login in targets:   # tell anyone online that mail arrived
        sid = _online_sid(login)
        if sid is not None:
            try: CTX['message'](sid, 'You have new mail: %s' % title)
            except Exception: pass
    return {'ok': True, 'sent': sent}


def api_mail_delete(q, body):
    login = str(body.get('login', '')); st = _account_state(login); no = int(body.get('no', 0))
    st['mails'] = [m for m in st.get('mails', []) if int(m['no']) != no]
    CTX['save'](); _audit(login, 'admin_mail_delete', str(no))
    return {'ok': True}


def api_account_reset(q, body):
    login = str(body.get('login', '')); _need_offline(login); st = _account_state(login); what = body.get('what')
    if what == 'quests':
        st['quests_done'] = []
        for k in [k for k in st if k.startswith('quest') and k != 'quests_done']:
            st.pop(k, None)
    elif what == 'attendance':
        st['attend'] = {}
    else:
        raise ValueError('unknown reset')
    CTX['save'](); _audit(login, 'admin_reset', what)
    return {'ok': True}


def api_player_message(q, body):
    sid = int(body.get('sid', 0)); text = str(body.get('text', '')).strip()[:200]
    if not text or sid not in CTX['sessions']:
        raise ValueError('no such player or empty message')
    CTX['message'](sid, text); _audit(CTX['sessions'][sid].get('name', ''), 'admin_message', text)
    return {'ok': True}


def api_items_search(q, body):
    return {'items': [dict(i, **{k: paneldata.item_info(i['index'])[k] for k in ('type', 'rarity', 'slot', 'stackable')})
                      for i in paneldata.search_items(q.get('q', [''])[0])]}


def api_gamedata(q, body):
    return {'classes': paneldata.classes(), 'destinations': paneldata.destinations()}


def api_characters(q, body):
    """Every character on the server (search by name / account / class / map) for the Characters page."""
    s = (q.get('q', [''])[0] or '').lower(); out = []
    online = {x.get('name') for x in CTX['sessions'].values() if not x['writer'].is_closing()}
    for login, st in CTX['store'].items():
        for h in st.get('heroes', []):
            row = {'login': login, 'no': h.get('no'), 'name': h.get('name', ''), 'className': paneldata.class_name(h.get('index', 0)),
                   'level': h.get('level', 1), 'mapName': paneldata.map_name(h.get('map')) or 'Town', 'online': login in online,
                   'dead': bool(h.get('dead'))}
            if not s or s in row['name'].lower() or s in login.lower() or s in row['className'].lower() or s in row['mapName'].lower():
                out.append(row)
    out.sort(key=lambda r: (-int(r['level'] or 0), r['name'].lower()))
    return {'characters': out[:500]}


ROUTES = {
    'hero': api_hero, 'hero/edit': api_hero_edit, 'hero/teleport': api_hero_teleport, 'hero/delete': api_hero_delete,
    'inventory': api_inventory, 'inventory/add': api_inventory_add, 'inventory/remove': api_inventory_remove,
    'inventory/unequip': api_inventory_unequip, 'gold': api_gold, 'mail/send': api_mail_send, 'mail/delete': api_mail_delete,
    'account/reset': api_account_reset, 'player/message': api_player_message, 'items/search': api_items_search,
    'gamedata': api_gamedata, 'characters': api_characters,
    'dashboard': api_dashboard, 'players': api_players, 'notice': api_notice, 'kick': api_kick,
    'accounts': api_accounts, 'account': api_account, 'account/ban': api_account_ban, 'account/role': api_account_role,
    'account/password': api_account_password, 'account/create': api_account_create, 'account/delete': api_account_delete,
    'hero/state': api_hero_state, 'hero/save': api_hero_save, 'servers': api_servers, 'config': api_config,
    'save': api_save, 'logs': api_logs, 'admin/password': api_set_admin_password,
}


# ---- HTTP plumbing --------------------------------------------------------------------------------------------------
def _resp(status, ctype, data, cookie=None):
    head = 'HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n' % (status, ctype, len(data))
    if cookie:
        head += 'Set-Cookie: %s\r\n' % cookie
    return head.encode() + b'\r\n' + data


async def handler(reader, writer):
    try:
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
    except Exception:
        writer.close(); return
    text = head.decode('latin-1'); headers = text.split('\r\n')
    try:
        method, target, _ = headers[0].split(' ')
    except ValueError:
        writer.close(); return
    cl = 0
    for h in headers[1:]:
        if h.lower().startswith('content-length:'):
            cl = int(h.split(':')[1])
    raw = await reader.readexactly(cl) if cl else b''
    u = urlsplit(target); path = u.path; q = parse_qs(u.query)

    # static UI
    if method == 'GET' and (path == '/' or not path.startswith('/api/')):
        rel = 'index.html' if path == '/' else path.lstrip('/')
        full = os.path.normpath(os.path.join(HERE, 'panel', rel))
        base = os.path.join(HERE, 'panel')
        if full.startswith(base) and os.path.isfile(full):
            ctype = {'html': 'text/html; charset=utf-8', 'js': 'application/javascript', 'css': 'text/css'}.get(full.rsplit('.', 1)[-1], 'application/octet-stream')
            writer.write(_resp('200 OK', ctype, open(full, 'rb').read())); await writer.drain(); writer.close(); return
        writer.write(_resp('404 Not Found', 'text/plain', b'not found')); await writer.drain(); writer.close(); return

    name = path[len('/api/'):].rstrip('/')
    try:
        body = json.loads(raw.decode('utf-8')) if raw else {}
    except ValueError:
        body = {}

    if name == 'login':
        if _check_password(str(body.get('password', ''))):
            tok = secrets.token_hex(16); SESSIONS[tok] = time.time()
            CTX['db'].audit('admin', 'panel_login'); CTX['db'].con.commit()
            writer.write(_resp('200 OK', 'application/json', b'{"ok":true}',
                               cookie='dkg_admin=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d' % (tok, SESSION_TTL)))
        else:
            writer.write(_resp('200 OK', 'application/json', b'{"ok":false}'))
        await writer.drain(); writer.close(); return

    if name == 'logout':
        SESSIONS.pop(_cookie_token(headers), None)
        writer.write(_resp('200 OK', 'application/json', b'{"ok":true}', cookie='dkg_admin=; Path=/; Max-Age=0'))
        await writer.drain(); writer.close(); return

    if not _authed(headers):
        writer.write(_resp('401 Unauthorized', 'application/json', b'{"error":"auth"}')); await writer.drain(); writer.close(); return

    fn = ROUTES.get(name)
    if fn is None:
        writer.write(_resp('404 Not Found', 'application/json', b'{"error":"route"}')); await writer.drain(); writer.close(); return
    try:
        out = fn(q, body)
        data = json.dumps(out).encode()
        writer.write(_resp('200 OK', 'application/json', data))
    except ValueError as e:
        writer.write(_resp('400 Bad Request', 'application/json', json.dumps({'error': str(e)}).encode()))
    except Exception as e:
        CTX.get('log', print)('[panel] error in %s: %r' % (name, e))
        writer.write(_resp('500 Server Error', 'application/json', json.dumps({'error': 'server error'}).encode()))
    await writer.drain(); writer.close()


async def serve(ctx, ip, port):
    """ctx keys: db, sessions, store, save, notice, kick, config (callable -> dict), set_guests, set_region, hub_servers,
    errtext, log."""
    CTX.update(ctx)
    _load_admin()   # ensure the first password exists before anyone connects
    s = await asyncio.start_server(handler, ip, port)
    ctx.get('log', print)('[panel] admin panel on http://%s:%d' % (ip if ip != '0.0.0.0' else '127.0.0.1', port))
    return s
