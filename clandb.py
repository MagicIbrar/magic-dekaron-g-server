# Dekaron G stub: clans (NClan) in the account database. Imported by stubdb.py, which attaches these functions to Db.
#   clans              one row per clan (number from 1001, unique name, master, mark 1..100 = Assets/Sprite/GuildMark<nnn>,
#                      join type 0 Reject / 1 Immediately / 2 NeedAccept, introduce, notice, level / exp, member limit)
#   clanners           hero -> clan, grade 1 Leader / 2 Manager / 3 General, contribution, join / last login times
#   clan_applications  pending requests to join a NeedAccept clan
import time

CLAN_SCHEMA = '''
CREATE TABLE IF NOT EXISTS clans (
  no INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  master_no INTEGER NOT NULL, master_name TEXT NOT NULL,
  mark INTEGER NOT NULL DEFAULT 1, join_type INTEGER NOT NULL DEFAULT 1,
  introduce TEXT NOT NULL DEFAULT '', notice TEXT NOT NULL DEFAULT '',
  level INTEGER NOT NULL DEFAULT 1, exp INTEGER NOT NULL DEFAULT 0, member_limit INTEGER NOT NULL DEFAULT 30,
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS clanners (
  hero_no INTEGER PRIMARY KEY,
  clan_no INTEGER NOT NULL REFERENCES clans(no) ON DELETE CASCADE,
  name TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1,
  grade INTEGER NOT NULL DEFAULT 3, contribution INTEGER NOT NULL DEFAULT 0,
  joined REAL NOT NULL, last_login REAL
);
CREATE TABLE IF NOT EXISTS clan_applications (
  clan_no INTEGER NOT NULL REFERENCES clans(no) ON DELETE CASCADE,
  hero_no INTEGER NOT NULL, name TEXT NOT NULL, level INTEGER NOT NULL DEFAULT 1, at REAL NOT NULL,
  PRIMARY KEY (clan_no, hero_no)
);
'''
CLAN_COLS = ('no', 'name', 'master_no', 'master_name', 'mark', 'join_type', 'introduce', 'notice', 'level', 'exp', 'member_limit', 'created')
CLANNER_COLS = ('hero_no', 'clan_no', 'name', 'level', 'grade', 'contribution', 'joined', 'last_login')
APP_COLS = ('clan_no', 'hero_no', 'name', 'level', 'at')
GRADE_LEADER, GRADE_MANAGER, GRADE_GENERAL = 1, 2, 3
JOIN_REJECT, JOIN_IMMEDIATELY, JOIN_NEED_ACCEPT = 0, 1, 2
ERR_IN_CLAN, ERR_NAME, ERR_NO_CLAN, ERR_FULL, ERR_RIGHTS, ERR_CLOSED, ERR_NOT_MEMBER, ERR_APPLIED = 1, 2, 3, 4, 5, 6, 7, 8
ERROR_TEXT = {ERR_IN_CLAN: 'already in a clan', ERR_NAME: 'clan name taken or invalid (2-16 characters)', ERR_NO_CLAN: 'no such clan',
              ERR_FULL: 'the clan is full', ERR_RIGHTS: 'only the clan master (or a manager) may do that', ERR_CLOSED: 'this clan does not accept members',
              ERR_NOT_MEMBER: 'not a member of this clan', ERR_APPLIED: 'application already sent'}


def _row(cols, r):
    return None if r is None else dict(zip(cols, r))


def clan_init(self):
    self.con.executescript(CLAN_SCHEMA); self.con.commit()
    if self.con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'clans'").fetchone() is None:
        self.con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('clans', 1000)"); self.con.commit()   # clan numbers start at 1001


def clan(self, no):
    return _row(CLAN_COLS, self.con.execute('SELECT %s FROM clans WHERE no = ?' % ', '.join(CLAN_COLS), (no,)).fetchone())


def clan_by_name(self, name):
    return _row(CLAN_COLS, self.con.execute('SELECT %s FROM clans WHERE name = ?' % ', '.join(CLAN_COLS), (name,)).fetchone())


def clans(self):
    return [_row(CLAN_COLS, r) for r in self.con.execute('SELECT %s FROM clans ORDER BY no' % ', '.join(CLAN_COLS))]


def clan_member_count(self, no):
    return self.con.execute('SELECT count(*) FROM clanners WHERE clan_no = ?', (no,)).fetchone()[0]


def clanner(self, hero_no):
    return _row(CLANNER_COLS, self.con.execute('SELECT %s FROM clanners WHERE hero_no = ?' % ', '.join(CLANNER_COLS), (hero_no,)).fetchone())


def clanners(self, no):
    return [_row(CLANNER_COLS, r) for r in self.con.execute('SELECT %s FROM clanners WHERE clan_no = ? ORDER BY grade, joined' % ', '.join(CLANNER_COLS), (no,))]


def clan_create(self, name, master_no, master_name, master_level, mark, join_type, introduce, notice):
    """Returns (error, clan row)."""
    if self.clanner(master_no) is not None:
        return ERR_IN_CLAN, None
    name = (name or '').strip()
    if not (2 <= len(name) <= 16) or self.clan_by_name(name) is not None:
        return ERR_NAME, None
    now = time.time()
    cur = self.con.execute('INSERT INTO clans (name, master_no, master_name, mark, join_type, introduce, notice, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (name, master_no, master_name, mark, join_type if join_type in (0, 1, 2) else 1, introduce or '', notice or '', now))
    no = cur.lastrowid
    self.con.execute('INSERT INTO clanners (hero_no, clan_no, name, level, grade, joined, last_login) VALUES (?, ?, ?, ?, ?, ?, ?)', (master_no, no, master_name, master_level, GRADE_LEADER, now, now))
    self.con.execute('DELETE FROM clan_applications WHERE hero_no = ?', (master_no,))
    self.audit(master_name, 'clan_create', name); self.con.commit()
    return 0, self.clan(no)


def clan_add(self, no, hero_no, name, level):
    c = self.clan(no)
    if c is None:
        return ERR_NO_CLAN
    if self.clanner(hero_no) is not None:
        return ERR_IN_CLAN
    if self.clan_member_count(no) >= c['member_limit']:
        return ERR_FULL
    now = time.time()
    self.con.execute('INSERT INTO clanners (hero_no, clan_no, name, level, grade, joined, last_login) VALUES (?, ?, ?, ?, ?, ?, ?)', (hero_no, no, name, level, GRADE_GENERAL, now, now))
    self.con.execute('DELETE FROM clan_applications WHERE hero_no = ?', (hero_no,))
    self.audit(name, 'clan_join', c['name']); self.con.commit(); return 0


def clan_remove(self, hero_no):
    m = self.clanner(hero_no)
    if m is None:
        return ERR_NOT_MEMBER
    self.con.execute('DELETE FROM clanners WHERE hero_no = ?', (hero_no,))
    self.audit(m['name'], 'clan_leave', str(m['clan_no'])); self.con.commit(); return 0


def clan_delete(self, no):
    c = self.clan(no)
    if c:
        self.con.execute('DELETE FROM clans WHERE no = ?', (no,)); self.audit(c['master_name'], 'clan_delete', c['name']); self.con.commit()


def clan_set_master(self, no, new_master_no):
    m = self.clanner(new_master_no)
    if m is None or m['clan_no'] != no:
        return ERR_NOT_MEMBER
    c = self.clan(no)
    self.con.execute('UPDATE clanners SET grade = ? WHERE hero_no = ?', (GRADE_GENERAL, c['master_no']))
    self.con.execute('UPDATE clanners SET grade = ? WHERE hero_no = ?', (GRADE_LEADER, new_master_no))
    self.con.execute('UPDATE clans SET master_no = ?, master_name = ? WHERE no = ?', (new_master_no, m['name'], no))
    self.audit(m['name'], 'clan_master', c['name']); self.con.commit(); return 0


def clan_set_grade(self, no, hero_no, grade):
    m = self.clanner(hero_no)
    if m is None or m['clan_no'] != no or grade not in (GRADE_MANAGER, GRADE_GENERAL) or m['grade'] == GRADE_LEADER:
        return ERR_NOT_MEMBER
    self.con.execute('UPDATE clanners SET grade = ? WHERE hero_no = ?', (grade, hero_no)); self.con.commit(); return 0


def clan_update(self, no, mark, join_type, introduce, notice):
    self.con.execute('UPDATE clans SET mark = ?, join_type = ?, introduce = ?, notice = ? WHERE no = ?', (mark, join_type if join_type in (0, 1, 2) else 1, introduce or '', notice or '', no)); self.con.commit()


def clan_touch(self, hero_no, level=None):
    if level is None:
        self.con.execute('UPDATE clanners SET last_login = ? WHERE hero_no = ?', (time.time(), hero_no))
    else:
        self.con.execute('UPDATE clanners SET last_login = ?, level = ? WHERE hero_no = ?', (time.time(), level, hero_no))
    self.con.commit()


def clan_apply(self, no, hero_no, name, level):
    c = self.clan(no)
    if c is None:
        return ERR_NO_CLAN
    if self.clanner(hero_no) is not None:
        return ERR_IN_CLAN
    if c['join_type'] == JOIN_REJECT:
        return ERR_CLOSED
    if self.con.execute('SELECT 1 FROM clan_applications WHERE clan_no = ? AND hero_no = ?', (no, hero_no)).fetchone():
        return ERR_APPLIED
    self.con.execute('INSERT INTO clan_applications (clan_no, hero_no, name, level, at) VALUES (?, ?, ?, ?, ?)', (no, hero_no, name, level, time.time())); self.con.commit(); return 0


def clan_applications(self, no):
    return [_row(APP_COLS, r) for r in self.con.execute('SELECT clan_no, hero_no, name, level, at FROM clan_applications WHERE clan_no = ? ORDER BY at', (no,))]


def hero_applications(self, hero_no):
    return [_row(APP_COLS, r) for r in self.con.execute('SELECT clan_no, hero_no, name, level, at FROM clan_applications WHERE hero_no = ? ORDER BY at', (hero_no,))]


def clan_application_delete(self, no, hero_no):
    self.con.execute('DELETE FROM clan_applications WHERE clan_no = ? AND hero_no = ?', (no, hero_no)); self.con.commit()


METHODS = (clan, clan_by_name, clans, clan_member_count, clanner, clanners, clan_create, clan_add, clan_remove, clan_delete, clan_set_master,
           clan_set_grade, clan_update, clan_touch, clan_apply, clan_applications, hero_applications, clan_application_delete)


def attach(cls):
    for f in METHODS:
        setattr(cls, f.__name__, f)
    old_init = cls.__init__
    def init(self, path=None):
        old_init(self, path) if path is not None else old_init(self)
        clan_init(self)
    cls.__init__ = init
