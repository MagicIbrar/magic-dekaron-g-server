"""Live test of the management panel: starts panel.serve on a scratch port with a fake online session, exercises login,
dashboard, players, broadcast, kick, account ban/role/password/create/delete, offline character edit, servers, config,
logs, and the admin-password change."""
import os, sys, importlib.util, asyncio, json, http.cookies, urllib.request

STUB = r'D:\claudetest\dekaron-g\tools\stub'
sys.path.insert(0, STUB)
SCR = os.environ.get('SCRATCH', os.path.dirname(os.path.abspath(__file__)))
os.environ['STUB_DB'] = os.path.join(SCR, 'panel.sqlite')
os.environ['STUB_PANEL_ADMIN'] = os.path.join(SCR, 'panel-admin.json')
os.environ['STUB_PANEL_FIRST'] = os.path.join(SCR, 'PANEL-FIRST.txt')
for p in (os.environ['STUB_DB'], os.environ['STUB_DB'] + '-wal', os.environ['STUB_DB'] + '-shm', os.environ['STUB_PANEL_ADMIN'], os.environ['STUB_PANEL_FIRST']):
    if os.path.exists(p): os.remove(p)
os.environ['STUB_HUB_AUTH_SECRET'] = 'test-network-secret'; os.environ['STUB_IS_HUB'] = '1'

spec = importlib.util.spec_from_file_location('ss3', os.path.join(STUB, 'stub-server.py')); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import panel

PORT = 18077; ok = True; COOKIE = {'v': ''}
def check(n, c, x=''):
    global ok; print(('PASS' if c else 'FAIL'), n, x); ok = ok and c

class FakeWriter:
    def __init__(self): self.closed = False; self.sent = b''
    def is_closing(self): return self.closed
    def close(self): self.closed = True
    def write(self, b): self.sent += b
    def get_extra_info(self, k): return ('10.0.0.5', 5555)

def req(method, path, obj=None):
    headers = {'Content-Type': 'application/json'}
    if COOKIE['v']: headers['Cookie'] = 'dkg_admin=' + COOKIE['v']
    data = json.dumps(obj).encode() if obj is not None else None
    r = urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:%d%s' % (PORT, path), data=data, headers=headers, method=method), timeout=5)
    sc = r.headers.get('Set-Cookie')
    if sc and 'dkg_admin=' in sc: COOKIE['v'] = sc.split('dkg_admin=', 1)[1].split(';')[0]
    return json.loads(r.read())

async def run():
    # a fake online session so players/kick have something to act on
    m.SESSIONS[42] = {'writer': FakeWriter(), 'cipher': None, 'name': 'onlineguy', 'h': {'name': 'Hero42', 'level': 7, 'map': 9001001}, 'hero': {'map': 9001001}}
    # an offline account to edit
    m.DB.create('editme', None); m.STORE['editme'] = {'gold': 50, 'heroes': [{'no': 1, 'name': 'EditHero', 'index': 0, 'level': 3, 'map': 9001001}]}
    ctx = {'db': m.DB, 'sessions': m.SESSIONS, 'store': m.STORE, 'save': lambda: m.save_store(m.STORE),
           'notice': m.panel_notice, 'kick': m.panel_kick, 'config': m.panel_config,
           'set_guests': m.set_guests, 'set_region': m.set_region, 'hub_servers': m.HUB_SERVERS,
           'errtext': m.stubdb.ERROR_TEXT, 'log': m.log,
           'hero_stats': m.hero_stats, 'levels': m.LEVELS, 'bag_add': m.bag_add, 'message': m.system_line}
    srv = await panel.serve(ctx, '127.0.0.1', PORT)
    loop = asyncio.get_event_loop(); run = lambda f, *a: loop.run_in_executor(None, lambda: f(*a))
    try:
        pw = open(os.environ['STUB_PANEL_FIRST'], encoding='utf-8').read().split('password:')[1].split('\n\n')[1].strip()
        check('first-login password file', len(pw) > 6)
        # auth gate
        try:
            await run(req, 'GET', '/api/dashboard'); check('auth gate (no cookie)', False)
        except urllib.error.HTTPError as e:
            check('auth gate (no cookie)', e.code == 401)
        j = await run(req, 'POST', '/api/login', {'password': 'wrong'}); check('wrong admin pw', j['ok'] is False)
        j = await run(req, 'POST', '/api/login', {'password': pw}); check('admin login', j['ok'] is True and COOKIE['v'])
        d = await run(req, 'GET', '/api/dashboard'); check('dashboard', d['onlineSessions'] == 1 and d['config']['isHub'], {'sess': d['onlineSessions']})
        pl = await run(req, 'GET', '/api/players'); check('players list', pl['players'][0]['hero'] == 'Hero42')
        nt = await run(req, 'POST', '/api/notice', {'text': 'server test notice'}); check('broadcast notice', nt['sent'] == 1)
        # accounts
        ac = await run(req, 'POST', '/api/account/create', {'login': 'paneltest', 'password': 'createdpw1'}); check('create account', ac['ok'])
        aj = await run(req, 'GET', '/api/account?login=paneltest'); check('account detail', aj['kind'] == 'password')
        rj = await run(req, 'POST', '/api/account/role', {'login': 'paneltest', 'role': 'gm'}); check('set role gm', rj['ok'] and m.DB.account('paneltest')['role'] == 'gm')
        bj = await run(req, 'POST', '/api/account/ban', {'login': 'paneltest', 'seconds': 3600, 'reason': 'x'}); check('ban', bj['ok'] and (m.DB.account('paneltest')['banned_until'] or 0) > 0)
        await run(req, 'POST', '/api/account/ban', {'login': 'paneltest', 'seconds': 0}); check('unban', (m.DB.account('paneltest')['banned_until'] or 0) == 0)
        pj = await run(req, 'POST', '/api/account/password', {'login': 'paneltest', 'password': 'newpw12345'}); check('set password', pj['ok'])
        # offline character edit
        hs = await run(req, 'GET', '/api/hero/state?login=editme'); check('read offline state', hs['state']['gold'] == 50)
        hs['state']['gold'] = 999
        sj = await run(req, 'POST', '/api/hero/save', {'login': 'editme', 'state': hs['state']}); check('save offline state', sj['ok'] and m.STORE['editme']['gold'] == 999)
        # online edit refused
        try:
            await run(req, 'GET', '/api/hero/state?login=onlineguy'); check('online edit refused', False)
        except urllib.error.HTTPError as e:
            check('online edit refused', e.code == 400)
        # ---- character management ----
        cs = await run(req, 'GET', '/api/characters?q=EditHero'); check('characters list', any(c['name'] == 'EditHero' for c in cs['characters']))
        gd = await run(req, 'GET', '/api/gamedata'); check('gamedata', len(gd['destinations']) > 10 and any(c['name'] == 'Swordsman' for c in gd['classes']))
        hv = await run(req, 'GET', '/api/hero?login=editme&no=1'); check('hero view', bool(hv['hero']['className']) and hv['hero']['mapName'] == 'Ardeca Castle')
        he = await run(req, 'POST', '/api/hero/edit', {'login': 'editme', 'no': 1, 'level': 50, 'exp': 0, 'stats': {'str': 99}})
        check('hero edit level/stats', he['hero']['level'] == 50 and m.STORE['editme']['heroes'][0]['stats']['str'] == 99)
        he = await run(req, 'POST', '/api/hero/edit', {'login': 'editme', 'no': 1, 'level': 999}); check('level clamped to 120', he['hero']['level'] == 120)
        m.DB.create('other', None); m.STORE['other'] = {'heroes': [{'no': 2, 'name': 'TakenName', 'index': 910101, 'level': 1}]}; m.save_store(m.STORE)
        try:
            await run(req, 'POST', '/api/hero/edit', {'login': 'editme', 'no': 1, 'name': 'TakenName'}); check('rename taken refused', False)
        except urllib.error.HTTPError as e:
            check('rename taken refused', e.code == 400)
        rn = await run(req, 'POST', '/api/hero/edit', {'login': 'editme', 'no': 1, 'name': 'NewName1'}); check('rename ok', rn['hero']['name'] == 'NewName1')
        dest = [d for d in gd['destinations'] if d['map'] != 9001001][0]
        await run(req, 'POST', '/api/hero/teleport', {'login': 'editme', 'no': 1, 'map': dest['map']}); check('teleport', m.STORE['editme']['heroes'][0]['map'] == dest['map'])
        await run(req, 'POST', '/api/hero/teleport', {'login': 'editme', 'no': 1, 'town': True}); check('send to town', 'map' not in m.STORE['editme']['heroes'][0])
        it = await run(req, 'GET', '/api/items/search?q=potion'); check('item search', len(it['items']) > 0)
        pot = it['items'][0]['index']
        await run(req, 'POST', '/api/inventory/add', {'login': 'editme', 'index': pot, 'count': 7})
        inv = await run(req, 'GET', '/api/inventory?login=editme'); row = [b for b in inv['bag'] if b['index'] == pot]
        check('add item (stacked)', bool(row) and row[0]['count'] == 7 and bool(row[0]['name']))
        await run(req, 'POST', '/api/inventory/remove', {'login': 'editme', 'serial': row[0]['serial'], 'count': 3})
        inv = await run(req, 'GET', '/api/inventory?login=editme'); check('remove some', [b for b in inv['bag'] if b['index'] == pot][0]['count'] == 4)
        await run(req, 'POST', '/api/inventory/add', {'login': 'editme', 'index': 221401, 'count': 1})
        inv = await run(req, 'GET', '/api/inventory?login=editme'); arm = [b for b in inv['bag'] if b['index'] == 221401][0]
        m.STORE['editme']['suit'] = {'4': arm['serial']}
        inv = await run(req, 'GET', '/api/inventory?login=editme'); check('equipped marker', [b for b in inv['bag'] if b['index'] == 221401][0]['equipped'] == 'Chest')
        await run(req, 'POST', '/api/inventory/unequip', {'login': 'editme', 'serial': arm['serial']}); check('unequip', m.STORE['editme']['suit'] == {})
        await run(req, 'POST', '/api/inventory/remove', {'login': 'editme', 'serial': arm['serial']}); check('remove item', not any(b['index'] == 221401 for b in m.STORE['editme']['bag']))
        g0 = m.STORE['editme'].get('gold', 0)
        g = await run(req, 'POST', '/api/gold', {'login': 'editme', 'add': 500}); check('gold add', g['gold'] == g0 + 500)
        g = await run(req, 'POST', '/api/gold', {'login': 'editme', 'set': 7}); check('gold set', g['gold'] == 7)
        m.STORE['editme']['quests_done'] = [1, 2]; await run(req, 'POST', '/api/account/reset', {'login': 'editme', 'what': 'quests'}); check('reset quests', m.STORE['editme']['quests_done'] == [])
        m.DB.ensure('onlineguy'); m.STORE.setdefault('onlineguy', {'heroes': [{'no': 1, 'name': 'Hero42', 'level': 7}]})
        for path, body in (('/api/hero/edit', {'login': 'onlineguy', 'no': 1, 'level': 5}), ('/api/inventory/add', {'login': 'onlineguy', 'index': pot}), ('/api/gold', {'login': 'onlineguy', 'add': 1})):
            try:
                await run(req, 'POST', path, body); check('online refused ' + path, False)
            except urllib.error.HTTPError as e:
                check('online refused ' + path, e.code == 400)
        before = len(m.SESSIONS[42]['writer'].sent)
        ms = await run(req, 'POST', '/api/mail/send', {'login': 'onlineguy', 'kind': 'item', 'index': pot, 'count': 2, 'title': 'Hi'})
        check('mail to online account + notified', ms['sent'] == 1 and m.STORE['onlineguy']['mails'][-1]['index'] == pot and len(m.SESSIONS[42]['writer'].sent) > before)
        ms = await run(req, 'POST', '/api/mail/send', {'all': True, 'kind': 'gold', 'count': 100, 'title': 'Event'}); check('gift everyone', ms['sent'] == len(m.DB.list()))
        mno = m.STORE['editme']['mails'][-1]['no']; await run(req, 'POST', '/api/mail/delete', {'login': 'editme', 'no': mno}); check('mail delete', all(x['no'] != mno for x in m.STORE['editme']['mails']))
        pm = await run(req, 'POST', '/api/player/message', {'sid': 42, 'text': 'hello'}); check('player message', pm['ok'])
        await run(req, 'POST', '/api/hero/delete', {'login': 'editme', 'no': 1}); check('delete hero', m.STORE['editme']['heroes'] == [])
        # kick
        kj = await run(req, 'POST', '/api/kick', {'sid': 42}); check('kick', kj['ok'] and m.SESSIONS[42]['writer'].is_closing())
        # delete account
        dj = await run(req, 'POST', '/api/account/delete', {'login': 'paneltest'}); check('delete account', dj['ok'] and m.DB.account('paneltest') is None)
        # servers + logs + config
        sv = await run(req, 'GET', '/api/servers'); check('servers route', 'servers' in sv)
        lg = await run(req, 'GET', '/api/logs?n=20'); check('logs route', 'log' in lg and 'audit' in lg)
        cf = await run(req, 'POST', '/api/config', {'regionName': 'Test@Panel', 'guests': False}); check('config apply', cf['config']['regionName'] == 'Test@Panel' and cf['config']['guests'] is False)
        # admin password change
        pc = await run(req, 'POST', '/api/admin/password', {'old': pw, 'new': 'brandnewadmin1'}); check('admin pw change', pc['ok'])
        # static page
        idx = await loop.run_in_executor(None, lambda: urllib.request.urlopen('http://127.0.0.1:%d/' % PORT, timeout=5).read())
        check('index.html served', b'Server management panel' in idx)
    finally:
        srv.close(); await srv.wait_closed()
    print('\nALL PASS' if ok else '\nSOME FAILED'); return ok

import urllib.error
sys.exit(0 if asyncio.run(run()) else 1)
