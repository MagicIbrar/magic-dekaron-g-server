"""Unit test of the hub account authority: token sign/verify, hub_authenticate (guest + password), and sign_in with a hub token.
Loads stub-server.py under a scratch DB + a test network secret."""
import os, sys, importlib.util

STUB = r'D:\claudetest\dekaron-g\tools\stub'
sys.path.insert(0, STUB)
os.environ['STUB_DB'] = os.path.join(os.environ.get('SCRATCH', os.path.dirname(os.path.abspath(__file__))), 'hubauth.sqlite')
if os.path.exists(os.environ['STUB_DB']):
    os.remove(os.environ['STUB_DB'])
os.environ['STUB_HUB_AUTH_SECRET'] = 'test-network-secret'
os.environ['STUB_IS_HUB'] = '1'
os.environ['STUB_GUESTS'] = '1'

spec = importlib.util.spec_from_file_location('stubsrv', os.path.join(STUB, 'stub-server.py'))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

ok = True
def check(name, cond):
    global ok
    print(('PASS' if cond else 'FAIL'), name)
    ok = ok and cond

# 1) token round-trip
tok = m.hub_sign('device-abc-123', 7)
v = m.hub_verify(tok)
check('token round-trip', v == ('device-abc-123', 7))
check('tampered token rejected', m.hub_verify(tok[:-1] + ('0' if tok[-1] != '0' else '1')) is None)
check('expired token rejected', m.hub_verify(m.hub_sign('x', 1, exp=1)) is None)

# 2) guest device -> fresh hub account + token
err, login, no, t2 = m.hub_authenticate('device-guest-xyz', '', None)
check('guest provisioned', err == 0 and login == 'device-guest-xyz' and no > 0 and t2)
check('guest token verifies', m.hub_verify(t2) == ('device-guest-xyz', no))
check('guest account kind=hub', m.DB.account('device-guest-xyz')['kind'] == 'hub')

# 3) returning guest presents its token -> same account
err, login, no3, t3 = m.hub_authenticate('device-guest-xyz', t2, None)
check('returning guest same account', err == 0 and no3 == no)

# 4) password account: register, then login right/wrong
assert m.DB.create('webplayer', 'goodpass123') == 0
err, login, no4, t4 = m.hub_authenticate('webplayer', '', 'goodpass123')
check('password login ok', err == 0 and t4 and m.hub_verify(t4) == ('webplayer', no4))
err, *_ = m.hub_authenticate('webplayer', '', 'wrongpass')
check('wrong password refused', err == m.stubdb.ERR_PASSWORD)

# 5) sign_in accepts a hub token (the community-server / game-login path), provisioning a shadow account
err = m.sign_in('device-newserver-01', m.hub_sign('device-newserver-01', 999), 'game-login')
check('sign_in accepts hub token', err == 0 and m.DB.account('device-newserver-01') is not None)

# 6) ban blocks hub auth
m.DB.ban('webplayer', 3600, 'test')
err, *_ = m.hub_authenticate('webplayer', '', 'goodpass123')
check('banned refused', err == m.stubdb.ERR_BANNED)

print('\nALL PASS' if ok else '\nSOME FAILED')
sys.exit(0 if ok else 1)
