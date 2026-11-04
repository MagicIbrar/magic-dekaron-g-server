# Dekaron G stub: the account database (SQLite, tools/stub/stub.sqlite or env STUB_DB).
#
# Tables
#   accounts  one row per account: login (the memberId the client sends), kind 'password' | 'device' (guest / Google id
#             accounts the real APK makes, no password), scrypt password hash, ban, and `state` = the account's game data as
#             one JSON document (heroes, bag, account bag, gold, friends, letters ...: the same shape save.json had).
#   heroes    index of every hero (number, name, class, level, map, account) kept in step with `state` on every save
#             (hero numbers are fid(name): the old test accounts share 'UnityHero', so the key is (account, no)):
#             new hero names must be unique across accounts (hero_name_taken; the old test accounts share 'UnityHero').
#   sessions  login keys handed out by GET/POST /Login and server tokens by CQ_RegionLogin (the real client's path).
#
# The server keeps the states in memory (STORE: login -> state dict) and calls save(): only changed rows are written.
# First start with an old save.json and an empty database imports every save.json account as a 'device' account.
import hashlib, hmac, json, os, secrets, sqlite3, time

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get('STUB_DB', os.path.join(HERE, 'stub.sqlite'))
MIN_PASSWORD = 8
SESSION_TTL = 600          # s a login key / server token stays valid before CQ_Login uses it

ERR_OK, ERR_PASSWORD, ERR_NO_ACCOUNT, ERR_BANNED, ERR_TAKEN, ERR_WEAK, ERR_BAD_LOGIN, ERR_NEEDS_PASSWORD = 0, 1, 2, 3, 4, 5, 6, 7
ERROR_TEXT = {ERR_PASSWORD: 'wrong password', ERR_NO_ACCOUNT: 'unknown account', ERR_BANNED: 'account banned', ERR_TAKEN: 'name already taken',
              ERR_WEAK: 'password too short (%d+ characters)' % MIN_PASSWORD, ERR_BAD_LOGIN: 'account name: 3-32 letters, digits, - _ .',
              ERR_NEEDS_PASSWORD: 'this account has a password'}

SCHEMA = '''
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  login TEXT NOT NULL UNIQUE COLLATE NOCASE,
  kind TEXT NOT NULL DEFAULT 'device',
  role TEXT NOT NULL DEFAULT 'player',   -- 'player' | 'gm' | 'operator' (the hub operator; see HUB.md "operator override")
  email TEXT,                            -- optional, for web / linked accounts (password reset later)
  salt BLOB, pass_hash BLOB,
  created REAL NOT NULL, last_login REAL,
  banned_until REAL, ban_reason TEXT,
  state TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS heroes (
  no INTEGER NOT NULL,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  name TEXT NOT NULL COLLATE NOCASE,
  class_index INTEGER, level INTEGER, map INTEGER,
  PRIMARY KEY (account_id, no),
  UNIQUE (account_id, name)
);
CREATE INDEX IF NOT EXISTS heroes_name ON heroes(name);
CREATE TABLE IF NOT EXISTS sessions (
  key INTEGER PRIMARY KEY,
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  token TEXT NOT NULL,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  at REAL NOT NULL, login TEXT, what TEXT NOT NULL, detail TEXT
);
'''


def _hash(password, salt):
    return hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)


def valid_login(login):
    return 3 <= len(login) <= 64 and all(c.isalnum() or c in '-_.' for c in login)


class Db:
    def __init__(self, path=DB_FILE):
        self.path = path
        self.con = sqlite3.connect(path)
        self.con.execute('PRAGMA journal_mode=WAL'); self.con.execute('PRAGMA foreign_keys=ON')
        self.con.executescript(SCHEMA)
        cols = {r[1] for r in self.con.execute('PRAGMA table_info(accounts)')}   # migrate older databases in place
        if 'role' not in cols: self.con.execute("ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'player'")
        if 'email' not in cols: self.con.execute('ALTER TABLE accounts ADD COLUMN email TEXT')
        self.con.commit()
        self.written = {}   # login -> last JSON text written (dirty check)

    # ---- accounts ------------------------------------------------------------------------------------------------
    _ACC_COLS = ('id', 'login', 'kind', 'role', 'email', 'banned_until', 'ban_reason', 'salt', 'pass_hash')

    def account(self, login):
        r = self.con.execute('SELECT id, login, kind, role, email, banned_until, ban_reason, salt, pass_hash FROM accounts WHERE login = ?', (login,)).fetchone()
        return None if r is None else dict(zip(self._ACC_COLS, r))

    def account_by_id(self, aid):
        r = self.con.execute('SELECT id, login, kind, role, email, banned_until, ban_reason, salt, pass_hash FROM accounts WHERE id = ?', (aid,)).fetchone()
        return None if r is None else dict(zip(self._ACC_COLS, r))

    def set_role(self, login, role):
        if role not in ('player', 'gm', 'operator'):
            return False
        self.con.execute('UPDATE accounts SET role = ? WHERE login = ?', (role, login))
        self.audit(login, 'set_role', role); self.con.commit(); return True

    def set_email(self, login, email):
        self.con.execute('UPDATE accounts SET email = ? WHERE login = ?', (email or None, login)); self.con.commit()

    def ensure(self, login, kind='device', role='player'):
        """Get the account, creating a device/hub one if it does not exist. Returns the account row (or None on a bad login)."""
        a = self.account(login)
        if a is not None:
            return a
        if not valid_login(login):
            return None
        self.con.execute('INSERT INTO accounts (login, kind, role, created, state) VALUES (?, ?, ?, ?, ?)', (login, kind, role, time.time(), '{}'))
        self.audit(login, 'create', kind); self.con.commit()
        return self.account(login)

    def create(self, login, password=None, kind=None):
        """Create an account. password None = 'device' account (guest / Google id). Returns an ERR_ code."""
        if not valid_login(login):
            return ERR_BAD_LOGIN
        if self.account(login) is not None:
            return ERR_TAKEN
        if password is not None and len(password) < MIN_PASSWORD:
            return ERR_WEAK
        salt = pass_hash = None
        if password is not None:
            salt = secrets.token_bytes(16); pass_hash = _hash(password, salt)
        self.con.execute('INSERT INTO accounts (login, kind, salt, pass_hash, created, state) VALUES (?, ?, ?, ?, ?, ?)',
                         (login, kind or ('password' if password is not None else 'device'), salt, pass_hash, time.time(), '{}'))
        self.audit(login, 'create', kind or ('password' if password is not None else 'device'))
        self.con.commit(); return ERR_OK

    def set_password(self, login, password):
        if len(password) < MIN_PASSWORD:
            return ERR_WEAK
        if self.account(login) is None:
            return ERR_NO_ACCOUNT
        salt = secrets.token_bytes(16)
        self.con.execute('UPDATE accounts SET kind = ?, salt = ?, pass_hash = ? WHERE login = ?', ('password', salt, _hash(password, salt), login))
        self.audit(login, 'set_password'); self.con.commit(); return ERR_OK

    def check(self, login, password, allow_device=True):
        """Sign-in check. Password accounts need the right password; device accounts sign in with any token when allowed.
        Returns (ERR_ code, account row or None)."""
        a = self.account(login)
        if a is None:
            return ERR_NO_ACCOUNT, None
        if a['banned_until'] is not None and a['banned_until'] > time.time():
            return ERR_BANNED, a
        if a['kind'] == 'password':
            if not password or not hmac.compare_digest(_hash(password, a['salt']), a['pass_hash']):
                return ERR_PASSWORD, a
        elif not allow_device:
            return ERR_NEEDS_PASSWORD, a
        return ERR_OK, a

    def touch(self, login):
        self.con.execute('UPDATE accounts SET last_login = ? WHERE login = ?', (time.time(), login)); self.con.commit()

    def ban(self, login, seconds, reason=''):
        until = None if seconds <= 0 else time.time() + seconds
        self.con.execute('UPDATE accounts SET banned_until = ?, ban_reason = ? WHERE login = ?', (until, reason if until else None, login))
        self.audit(login, 'ban' if until else 'unban', reason); self.con.commit()

    def delete(self, login):
        self.con.execute('DELETE FROM accounts WHERE login = ?', (login,)); self.audit(login, 'delete'); self.con.commit()
        self.written.pop(login, None)

    def list(self, search=None):
        sql = ('SELECT a.id, a.login, a.kind, a.role, a.email, a.created, a.last_login, a.banned_until, '
               '(SELECT count(*) FROM heroes h WHERE h.account_id = a.id) FROM accounts a')
        args = ()
        if search:
            sql += ' WHERE a.login LIKE ? OR a.email LIKE ?'; args = ('%' + search + '%', '%' + search + '%')
        sql += ' ORDER BY a.id'
        return [dict(zip(('id', 'login', 'kind', 'role', 'email', 'created', 'last_login', 'banned_until', 'heroes'), r))
                for r in self.con.execute(sql, args)]

    # ---- sessions (the real client's HTTP login -> auth server -> game server path) ------------------------------
    def new_session(self, login):
        a = self.account(login)
        key = secrets.randbelow(2 ** 31 - 2) + 1; token = secrets.token_hex(16)
        self.con.execute('DELETE FROM sessions WHERE created < ?', (time.time() - SESSION_TTL,))
        self.con.execute('INSERT INTO sessions (key, account_id, token, created) VALUES (?, ?, ?, ?)', (key, a['id'], token, time.time()))
        self.con.commit(); return key, token

    def session(self, login, key=None, token=None):
        """The session row of `login` matching the login key or server token (None when there is none / expired)."""
        a = self.account(login)
        if a is None:
            return None
        if key is not None:
            r = self.con.execute('SELECT key, token FROM sessions WHERE account_id = ? AND key = ? AND created >= ?', (a['id'], key, time.time() - SESSION_TTL)).fetchone()
        else:
            r = self.con.execute('SELECT key, token FROM sessions WHERE account_id = ? AND token = ? AND created >= ?', (a['id'], token, time.time() - SESSION_TTL)).fetchone()
        return None if r is None else {'key': r[0], 'token': r[1]}

    # ---- game state -----------------------------------------------------------------------------------------------
    def load_states(self):
        """login -> state dict for every account (what the server keeps in memory)."""
        out = {}
        for login, text in self.con.execute('SELECT login, state FROM accounts'):
            try:
                out[login] = json.loads(text)
            except ValueError:
                out[login] = {}
            self.written[login] = text
        return out

    def save(self, store):
        """Write every changed account state + refresh the heroes index. Returns the number of rows written."""
        n = 0
        for login, state in store.items():
            text = json.dumps(state, separators=(',', ':'))
            if self.written.get(login) == text:
                continue
            a = self.account(login)
            if a is None:   # a brand-new device account the server made on the fly
                self.con.execute('INSERT INTO accounts (login, kind, created, state) VALUES (?, ?, ?, ?)', (login, 'device', time.time(), text))
                a = self.account(login)
            else:
                self.con.execute('UPDATE accounts SET state = ? WHERE id = ?', (text, a['id']))
            self.con.execute('DELETE FROM heroes WHERE account_id = ?', (a['id'],))
            for h in state.get('heroes', []):
                if h.get('name'):
                    self.con.execute('INSERT OR REPLACE INTO heroes (no, account_id, name, class_index, level, map) VALUES (?, ?, ?, ?, ?, ?)',
                                     (h.get('no'), a['id'], h['name'], h.get('index'), h.get('level'), h.get('map')))
            self.written[login] = text; n += 1
        if n:
            self.con.commit()
        return n

    def hero_name_taken(self, name):
        return self.con.execute('SELECT 1 FROM heroes WHERE name = ?', (name,)).fetchone() is not None

    def hero_owner(self, name):
        r = self.con.execute('SELECT a.login FROM heroes h JOIN accounts a ON a.id = h.account_id WHERE h.name = ?', (name,)).fetchone()
        return None if r is None else r[0]

    def heroes(self):
        return [dict(zip(('no', 'name', 'class_index', 'level', 'map', 'login'), r)) for r in self.con.execute(
            'SELECT h.no, h.name, h.class_index, h.level, h.map, a.login FROM heroes h JOIN accounts a ON a.id = h.account_id ORDER BY a.login, h.name')]

    def audit(self, login, what, detail=''):
        self.con.execute('INSERT INTO audit (at, login, what, detail) VALUES (?, ?, ?, ?)', (time.time(), login, what, detail))

    def audit_rows(self, last=50):
        return list(self.con.execute('SELECT at, login, what, detail FROM audit ORDER BY at DESC LIMIT ?', (last,)))[::-1]

    # ---- migration ------------------------------------------------------------------------------------------------
    def import_json(self, path):
        """One-time import of the old save.json ({ memberId: state }) into device accounts. Returns the count."""
        try:
            old = json.load(open(path, encoding='utf-8'))
        except (OSError, ValueError):
            return 0
        n = 0
        for login, state in old.items():
            if self.account(login) is None:
                self.con.execute('INSERT INTO accounts (login, kind, created, state) VALUES (?, ?, ?, ?)', (login, 'device', time.time(), '{}')); n += 1
        self.con.commit()
        if n:
            self.save(old); self.audit(None, 'import', '%d accounts from %s' % (n, os.path.basename(path)))
            self.con.commit()
        return n


import clandb   # clans / clanners / applications (tools/stub/clandb.py)
clandb.attach(Db)

import tradedb   # marketplace listings (tools/stub/tradedb.py)
tradedb.attach(Db)
