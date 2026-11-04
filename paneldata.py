"""Read-only game-data lookups for the management panel (2026-11-04): English names of items / classes / maps, item facts
(type, rarity, stackable, equip slot) and the teleport destinations (every static portal's end point + the town spawn).
Everything is read lazily from export/ExportedProject/Assets/gamedatabaseasset and cached; nothing is written."""
import os, re, glob

HERE = os.path.dirname(os.path.abspath(__file__))
GDB = os.environ.get('STUB_GDB') or next((p for p in (os.path.join(HERE, 'gamedata', 'gamedatabaseasset'),
    os.path.join(HERE, '..', '..', 'export', 'ExportedProject', 'Assets', 'gamedatabaseasset')) if os.path.isdir(p)), os.path.join(HERE, 'gamedata', 'gamedatabaseasset'))
TOWN_MAP, TOWN_POS = 9001001, (-125.66, 8.78, -45.89)

ITEM_TYPES = {1: 'Weapon', 2: 'Armor', 3: 'Accessory', 4: 'Consumable', 5: 'Material', 6: 'Quest', 7: 'Stack', 8: 'Etc'}
RARITY = {0: '', 1: 'Normal', 2: 'Magic', 3: 'Rare', 4: 'Epic', 5: 'Legendary', 6: 'Mythic'}
SLOTS = {1: 'Weapon', 2: 'Head', 3: 'Shoulder', 4: 'Chest', 5: 'Legs', 6: 'Hands', 7: 'Feet', 8: 'Necklace', 9: 'Ring', 10: 'Ring 2',
         11: 'Earring', 12: 'Belt', 13: 'Cloak', 14: 'Wing', 15: 'Costume'}

_cache = {}


def _text_table(folder):
    """{ID: Text} from every text/eng/<folder>/*.asset."""
    if folder in _cache:
        return _cache[folder]
    out = {}
    for f in glob.glob(os.path.join(GDB, 'text', 'eng', folder, '*.asset')):
        t = open(f, encoding='utf-8', errors='replace').read()
        for m in re.finditer(r'- ID: ([^\r\n]+)\r?\n\s+Text: ?([^\r\n]*)', t):
            v = m.group(2).strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in '"\'':
                v = v[1:-1].replace('\\"', '"').replace("''", "'")
            out[m.group(1).strip()] = v
    _cache[folder] = out
    return out


def item_name(index):
    return _text_table('item').get('%d_Name' % int(index), '') or ('Item %d' % int(index))


def class_name(index):
    return _text_table('item').get('%d_Name' % int(index), '') or ('Class %d' % int(index))


def map_name(index):
    if not index:
        return ''
    return _text_table('map').get('Map%d_1' % int(index), '') or ('Map %d' % int(index))


def item_info(index):
    """{'type', 'rarity', 'stackable', 'slot'} from item/item<index>.asset (cached)."""
    key = ('info', int(index))
    if key in _cache:
        return _cache[key]
    f = os.path.join(GDB, 'item', 'item%d.asset' % int(index))
    info = {'exists': os.path.exists(f), 'type': '', 'rarity': '', 'stackable': False, 'slot': ''}
    if info['exists']:
        t = open(f, encoding='utf-8', errors='replace').read()
        ty = re.search(r'\n  ItemType: (\d+)', t); ra = re.search(r'\n  ItemRarity: (\d+)', t); pl = re.search(r'PlaceType: (\d+)', t)
        if ty:
            info['type'] = ITEM_TYPES.get(int(ty.group(1)), 'Type %s' % ty.group(1)); info['stackable'] = int(ty.group(1)) in (4, 7)
        if ra:
            info['rarity'] = RARITY.get(int(ra.group(1)), str(ra.group(1)))
        if pl:
            info['slot'] = SLOTS.get(int(pl.group(1)), 'Slot %s' % pl.group(1))
    _cache[key] = info
    return info


def item_index_list():
    """[(index, name)] of every item that has an asset and an English name."""
    if 'items' in _cache:
        return _cache['items']
    names = _text_table('item'); out = []
    for f in glob.glob(os.path.join(GDB, 'item', 'item*.asset')):
        m = re.match(r'item(\d+)\.asset$', os.path.basename(f))
        if m:
            idx = int(m.group(1)); n = names.get('%d_Name' % idx, '')
            if n:
                out.append((idx, n))
    out.sort()
    _cache['items'] = out
    return out


def search_items(q, limit=60):
    q = (q or '').strip().lower()
    if not q:
        return []
    res = []
    for idx, n in item_index_list():
        if q in n.lower() or q == str(idx):
            res.append({'index': idx, 'name': n})
            if len(res) >= limit:
                break
    return res


def classes():
    """Player classes (ClassIndex values in characterlist) with names."""
    if 'classes' in _cache:
        return _cache['classes']
    found = set()
    for f in glob.glob(os.path.join(GDB, 'characterlist', '*.asset')):
        found.update(int(x) for x in re.findall(r'ClassIndex: (9\d{5})', open(f, encoding='utf-8', errors='replace').read()))
    out = [{'index': c, 'name': class_name(c)} for c in sorted(found)]
    _cache['classes'] = out
    return out


def destinations():
    """[{'map', 'name', 'pos'}]: the town spawn plus the first end point of every static portal, one per map."""
    if 'dest' in _cache:
        return _cache['dest']
    seen = {TOWN_MAP: list(TOWN_POS)}
    for f in sorted(glob.glob(os.path.join(GDB, 'staticportal', 'portal*.asset'))):
        t = open(f, encoding='utf-8', errors='replace').read()
        for blk in re.split(r'\n  - Index: ', t)[1:]:
            em = re.search(r'EndMapIndex: (\d+)', blk)
            ep = re.search(r'EndPosX: (-?[\d.eE+-]+)\r?\n\s+EndPosY: (-?[\d.eE+-]+)\r?\n\s+EndPosZ: (-?[\d.eE+-]+)', blk)
            if em and ep and int(em.group(1)) not in seen:
                seen[int(em.group(1))] = [float(ep.group(1)), float(ep.group(2)), float(ep.group(3))]
    out = [{'map': m, 'name': map_name(m), 'pos': p} for m, p in seen.items()]
    out.sort(key=lambda d: (d['map'] != TOWN_MAP, d['name']))
    _cache['dest'] = out
    return out
