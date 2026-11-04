# Account administration for the Dekaron G stub database (tools/stub/stub.sqlite, env STUB_DB).
#   python -I tools/stub/accounts.py list                      accounts (kind, heroes, last login, ban)
#   python -I tools/stub/accounts.py heroes                    every hero and its account
#   python -I tools/stub/accounts.py create <login>            password account; the password is asked on the console (or env STUB_PASSWORD)
#   python -I tools/stub/accounts.py passwd <login>            set / reset a password (turns a device account into a password account)
#   python -I tools/stub/accounts.py ban <login> <hours> [reason] / unban <login>
#   python -I tools/stub/accounts.py delete <login>            removes the account, its heroes and sessions (asks first)
#   python -I tools/stub/accounts.py audit [N]                 last N audit rows
# Passwords are never printed or logged. The stub keeps account states in memory: restart it after changing them here.
import getpass, os, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stubdb


def when(t):
    return '-' if not t else time.strftime('%Y-%m-%d %H:%M', time.localtime(t))


def main(argv):
    db = stubdb.Db()
    cmd = argv[1] if len(argv) > 1 else 'list'
    if cmd == 'list':
        print('%-5s %-36s %-9s %6s  %-16s  %s' % ('id', 'login', 'kind', 'heroes', 'last login', 'ban'))
        for a in db.list():
            ban = '' if not a['banned_until'] or a['banned_until'] < time.time() else 'until ' + when(a['banned_until'])
            print('%-5d %-36s %-9s %6d  %-16s  %s' % (a['id'], a['login'][:36], a['kind'], a['heroes'], when(a['last_login']), ban))
    elif cmd == 'heroes':
        for h in db.heroes():
            print('%-16s lv %-3s class %-9s map %-4s  %s' % (h['name'], h['level'], h['class_index'], h['map'], h['login'][:36]))
    elif cmd in ('create', 'passwd'):
        login = argv[2]
        pw = os.environ.get('STUB_PASSWORD') or getpass.getpass('password for %s: ' % login)
        err = db.create(login, pw) if cmd == 'create' else db.set_password(login, pw)
        print('ok' if err == 0 else stubdb.ERROR_TEXT.get(err, err)); return err
    elif cmd == 'ban':
        db.ban(argv[2], float(argv[3]) * 3600, ' '.join(argv[4:])); print('ok')
    elif cmd == 'unban':
        db.ban(argv[2], 0); print('ok')
    elif cmd == 'delete':
        login = argv[2]
        if db.account(login) is None:
            print('no such account'); return 1
        if os.environ.get('STUB_YES') != '1' and input('delete %s and its heroes? type yes: ' % login) != 'yes':
            return 1
        db.delete(login); print('ok')
    elif cmd == 'audit':
        for at, login, what, detail in db.audit_rows(int(argv[2]) if len(argv) > 2 else 50):
            print('%s  %-36s %-14s %s' % (when(at), (login or '-')[:36], what, detail or ''))
    else:
        print(__doc__ or 'see the header of accounts.py'); return 2
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv) or 0)
