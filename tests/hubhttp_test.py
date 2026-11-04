"""Live HTTP test of the hub account routes: starts stub-server's http_handler on a scratch port and exercises
/account (page), /account/register, /account/login, /hub/auth, /hub/validate, and the region client /Login."""
import os, sys, importlib.util, asyncio, json, urllib.request

STUB = r'D:\claudetest\dekaron-g\tools\stub'
sys.path.insert(0, STUB)
SCR = os.environ.get('SCRATCH', os.path.dirname(os.path.abspath(__file__)))
os.environ['STUB_DB'] = os.path.join(SCR, 'hubhttp.sqlite')
for ext in ('', '-wal', '-shm'):
    p = os.environ['STUB_DB'] + ext
    if os.path.exists(p): os.remove(p)
os.environ['STUB_HUB_AUTH_SECRET'] = 'test-network-secret'
os.environ['STUB_IS_HUB'] = '1'
os.environ['STUB_GUESTS'] = '1'
spec = importlib.util.spec_from_file_location('stubsrv2', os.path.join(STUB, 'stub-server.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

PORT = 18099
ok = True
def check(name, cond, extra=''):
    global ok; print(('PASS' if cond else 'FAIL'), name, extra); ok = ok and cond

def post(path, obj):
    r = urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:%d%s' % (PORT, path),
        data=json.dumps(obj).encode(), headers={'Content-Type': 'application/json'}, method='POST'), timeout=5)
    return json.loads(r.read())

def get(path):
    return urllib.request.urlopen('http://127.0.0.1:%d%s' % (PORT, path), timeout=5).read()

async def run():
    srv = await asyncio.start_server(m.http_handler, '127.0.0.1', PORT)
    loop = asyncio.get_event_loop()
    try:
        # account page
        page = await loop.run_in_executor(None, get, '/account')
        check('account page served', b'Magic Dekaron G' in page and b"'/account/'" in page)
        check('deeplink substituted', b'magicdekarong://login' in page and b'__DEEPLINK__' not in page)
        # register a web account
        j = await loop.run_in_executor(None, lambda: post('/account/register', {'login': 'webbie', 'password': 'secret12345', 'email': 'a@b.co'}))
        check('register ok + token', j['error'] == 0 and j['token'] and j['deeplink'] == 'magicdekarong://login', j)
        check('email stored', m.DB.account('webbie')['email'] == 'a@b.co')
        # login that account
        j2 = await loop.run_in_executor(None, lambda: post('/account/login', {'login': 'webbie', 'password': 'secret12345'}))
        check('web login ok', j2['error'] == 0 and j2['token'])
        j3 = await loop.run_in_executor(None, lambda: post('/account/login', {'login': 'webbie', 'password': 'nope'}))
        check('web login wrong pw refused', j3['error'] == m.stubdb.ERR_PASSWORD)
        # hub/auth with server key (community-server delegation), guest device
        j4 = await loop.run_in_executor(None, lambda: post('/hub/auth', {'memberId': 'dev-guest-7', 'token': '', 'serverKey': 'test-network-secret'}))
        check('hub/auth guest mint', j4['error'] == 0 and j4['token'] and j4['login'] == 'dev-guest-7', j4)
        # hub/auth wrong server key refused
        j5 = await loop.run_in_executor(None, lambda: post('/hub/auth', {'memberId': 'x', 'serverKey': 'WRONG'}))
        check('hub/auth bad server key', j5['error'] == 1)
        # hub/validate
        v = await loop.run_in_executor(None, get, '/hub/validate?token=' + j4['token'])
        vj = json.loads(v)
        check('hub/validate ok', vj['error'] == 0 and vj['login'] == 'dev-guest-7')
        # region client /Login (hub/standalone path) with the web token
        j6 = await loop.run_in_executor(None, lambda: post('/Login', {'memberId': 'webbie', 'token': j2['token']}))
        check('region /Login with hub token -> loginKey', j6['error'] == 0 and j6['loginKey'] > 0, {'error': j6['error'], 'hasKey': j6['loginKey'] > 0})
    finally:
        srv.close(); await srv.wait_closed()
    print('\nALL PASS' if ok else '\nSOME FAILED')
    return ok

sys.exit(0 if asyncio.run(run()) else 1)
