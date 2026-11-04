# Dekaron G stub: the marketplace (NTrading: NBuy / NSell / NMarketPrice) in the account database. Attached to stubdb.Db.
#   trade_listings  one row per listed item: the seller (hero), the item (serial kept so Cancel returns the same item), count,
#                   enchant, price (gold), flag 0 selling / 1 sold (money waiting) / 2 settled / 3 cancelled, times, the buyer.
# Market prices come from sold rows (min / max / average); the current listings answer when nothing sold yet.
import time

TRADE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS trade_listings (
  no INTEGER PRIMARY KEY AUTOINCREMENT,
  seller_no INTEGER NOT NULL, seller_name TEXT NOT NULL, seller_login TEXT NOT NULL,
  serial INTEGER NOT NULL, item_index INTEGER NOT NULL, count INTEGER NOT NULL, enchant INTEGER NOT NULL DEFAULT 0,
  price INTEGER NOT NULL,
  flag INTEGER NOT NULL DEFAULT 0,
  started REAL NOT NULL, expires REAL NOT NULL, sold_at REAL, settled_at REAL,
  buyer_no INTEGER, buyer_name TEXT
);
CREATE INDEX IF NOT EXISTS trade_item ON trade_listings(item_index, flag);
CREATE INDEX IF NOT EXISTS trade_seller ON trade_listings(seller_no, flag);
'''
COLS = ('no', 'seller_no', 'seller_name', 'seller_login', 'serial', 'item_index', 'count', 'enchant', 'price', 'flag', 'started', 'expires', 'sold_at', 'settled_at', 'buyer_no', 'buyer_name')
SELLING, SOLD, SETTLED, CANCELLED = 0, 1, 2, 3
LISTING_DAYS = 7
FEE_PERCENT = 5
ERR_NO_LISTING, ERR_GOLD, ERR_ITEM, ERR_OWN, ERR_PRICE, ERR_NOTHING, ERR_LIMIT = 1, 2, 3, 4, 5, 6, 7
ERROR_TEXT = {ERR_NO_LISTING: 'that listing is gone', ERR_GOLD: 'not enough gold', ERR_ITEM: 'item not in the bag (or worn)', ERR_OWN: 'that is your own listing',
              ERR_PRICE: 'price must be 1 .. 999,999,999', ERR_NOTHING: 'nothing to settle', ERR_LIMIT: 'at most 20 listings at a time'}


def _row(r):
    return None if r is None else dict(zip(COLS, r))


def trade_init(self):
    self.con.executescript(TRADE_SCHEMA); self.con.commit()
    if self.con.execute("SELECT seq FROM sqlite_sequence WHERE name = 'trade_listings'").fetchone() is None:
        self.con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('trade_listings', 5000)"); self.con.commit()


def trade_listing(self, no):
    return _row(self.con.execute('SELECT %s FROM trade_listings WHERE no = ?' % ', '.join(COLS), (no,)).fetchone())


def trade_expire(self):
    """Listings past their time go back to 'cancelled' (the client's Reclaim returns the item)."""
    self.con.execute('UPDATE trade_listings SET flag = ? WHERE flag = ? AND expires < ?', (CANCELLED, SELLING, time.time())); self.con.commit()


def trade_list(self, item_index=None, enchant=None, flag=SELLING):
    q = 'SELECT %s FROM trade_listings WHERE flag = ?' % ', '.join(COLS); a = [flag]
    if item_index is not None:
        q += ' AND item_index = ?'; a.append(item_index)
    if enchant is not None:
        q += ' AND enchant = ?'; a.append(enchant)
    return [_row(r) for r in self.con.execute(q + ' ORDER BY price, no', a)]


def trade_mine(self, seller_no, flags=(SELLING, SOLD, CANCELLED)):
    return [_row(r) for r in self.con.execute('SELECT %s FROM trade_listings WHERE seller_no = ? AND flag IN (%s) ORDER BY no' % (', '.join(COLS), ','.join('?' * len(flags))), (seller_no,) + tuple(flags))]


def trade_sell(self, seller_no, seller_name, seller_login, serial, item_index, count, enchant, price):
    if not (1 <= price <= 999999999):
        return ERR_PRICE, None
    if len(self.trade_mine(seller_no, (SELLING,))) >= 20:
        return ERR_LIMIT, None
    now = time.time()
    cur = self.con.execute('INSERT INTO trade_listings (seller_no, seller_name, seller_login, serial, item_index, count, enchant, price, flag, started, expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)',
                           (seller_no, seller_name, seller_login, serial, item_index, count, enchant, price, now, now + LISTING_DAYS * 86400))
    self.audit(seller_name, 'trade_sell', 'item %d x%d for %d' % (item_index, count, price)); self.con.commit()
    return 0, self.trade_listing(cur.lastrowid)


def trade_cancel(self, seller_no, no):
    """Returns the listing when it was selling / expired and belongs to the hero (the caller gives the item back), else None."""
    l = self.trade_listing(no)
    if l is None or l['seller_no'] != seller_no or l['flag'] not in (SELLING, CANCELLED):
        return None
    self.con.execute('DELETE FROM trade_listings WHERE no = ?', (no,)); self.con.commit()
    self.audit(l['seller_name'], 'trade_cancel', 'listing %d' % no)
    return l


def trade_buy(self, buyer_no, buyer_name, no, price):
    """Marks the listing sold (the caller moves the gold and the item). Returns (error, listing)."""
    l = self.trade_listing(no)
    if l is None or l['flag'] != SELLING or l['expires'] < time.time():
        return ERR_NO_LISTING, None
    if l['seller_no'] == buyer_no:
        return ERR_OWN, l
    if price != l['price']:
        return ERR_NO_LISTING, l   # the price changed under the buyer: the client asks again
    self.con.execute('UPDATE trade_listings SET flag = ?, sold_at = ?, buyer_no = ?, buyer_name = ? WHERE no = ?', (SOLD, time.time(), buyer_no, buyer_name, no))
    self.audit(buyer_name, 'trade_buy', 'listing %d from %s for %d' % (no, l['seller_name'], l['price'])); self.con.commit()
    return 0, self.trade_listing(no)


def trade_settle(self, seller_no):
    """Sold listings not yet settled -> settled. Returns (gross, fee, rows)."""
    rows = self.trade_mine(seller_no, (SOLD,))
    gross = sum(r['price'] for r in rows); fee = gross * FEE_PERCENT // 100
    if rows:
        self.con.execute('UPDATE trade_listings SET flag = ?, settled_at = ? WHERE seller_no = ? AND flag = ?', (SETTLED, time.time(), seller_no, SOLD)); self.con.commit()
    return gross, fee, rows


def trade_history(self, seller_no):
    return [_row(r) for r in self.con.execute('SELECT %s FROM trade_listings WHERE seller_no = ? AND flag IN (1, 2) ORDER BY sold_at DESC LIMIT 50' % ', '.join(COLS), (seller_no,))]


def trade_prices(self, item_index, enchant=None):
    """(min, max, average) of sold listings of the item, else of the current listings, else zeros."""
    for flags in ('1, 2', '0'):
        q = 'SELECT min(price), max(price), avg(price) FROM trade_listings WHERE item_index = ? AND flag IN (%s)' % flags; a = [item_index]
        if enchant is not None:
            q += ' AND enchant = ?'; a.append(enchant)
        r = self.con.execute(q, a).fetchone()
        if r and r[0] is not None:
            return float(r[0]), float(r[1]), float(r[2])
    return 0.0, 0.0, 0.0


METHODS = (trade_listing, trade_expire, trade_list, trade_mine, trade_sell, trade_cancel, trade_buy, trade_settle, trade_history, trade_prices)


def attach(cls):
    for f in METHODS:
        setattr(cls, f.__name__, f)
    old_init = cls.__init__
    def init(self, path=None):
        old_init(self, path) if path is not None else old_init(self)
        trade_init(self)
    cls.__init__ = init
