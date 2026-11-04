# Stub server tests

Self-contained tests for the hub account authority and the management panel. Each loads `stub-server.py` under a scratch
SQLite database and scratch admin files, so none of them touch the live `stub.sqlite`, `panel-admin.json`, or a running stub.

Run them with a scratch dir that already exists:

```bash
cd tools/stub/tests
S="$TMPDIR/dkg"; mkdir -p "$S"
SCRATCH="$S" python hubauth_test.py     # hub token sign/verify, guest + password auth, sign_in with a hub token (11 checks)
SCRATCH="$S" python hubhttp_test.py     # live /account page, /account/register, /account/login, /hub/auth, /hub/validate, /Login (10 checks)
SCRATCH="$S" python panel_test.py       # live panel: login, dashboard, players, broadcast, kick, accounts, offline edit, servers, logs, config, pw change (23 checks)
```

All three print `ALL PASS` on success. They start their HTTP servers on 127.0.0.1 ports 18077/18099 (not the stub's real ports),
so they can run while the live stub is up.
