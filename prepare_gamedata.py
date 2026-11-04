"""Dekaron G: prepare the server's game data from YOUR OWN copy of the game (2026-11-04).

The server reads a handful of the game's data tables (items, monsters, maps, portals, NPCs, skills, quests, levels, ...).
Those tables are Thumbage's content and are NOT shipped with this server. This tool converts them, on your machine, from the
AssetBundles the official client already downloaded to your device, into the plain-text form the server reads.

    python prepare_gamedata.py "<path to the client's AssetBundles folder>"
    python prepare_gamedata.py "<...>/AssetBundles" --out tools/stub/gamedata     # choose the output folder

It reads <AssetBundles>/gamedatabaseasset/*.h2 (and text/<lang>/*.h2) and writes tools/stub/gamedata/gamedatabaseasset/...
.asset files (AssetRipper-style Unity YAML). The server then loads from there automatically (or set STUB_GDB to the folder).
Nothing here is distributed: it only rewrites files you already have. Run it with a sandboxed interpreter on untrusted input:
    python -E -P prepare_gamedata.py ...     (UnityPy is required: pip install UnityPy)
"""
import os, sys, struct, argparse

try:
    import UnityPy
except ImportError:
    sys.exit('This tool needs UnityPy:  pip install UnityPy')


def f32(x):
    """Shortest decimal that round-trips to the same float32 (matches the game's exported values)."""
    if x != x or x in (float('inf'), float('-inf')):
        return '0'
    if x == int(x) and abs(x) < 1e15:
        return str(int(x))          # integral floats plainly (e.g. a 340-degree direction), never 3.4e+02
    b = struct.pack('<f', x)
    for p in range(1, 10):
        s = '%.*g' % (p, x)
        if struct.pack('<f', float(s)) == b:
            return s
    return repr(x)


def scalar(v):
    if v is None:
        return ''
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return f32(v)
    s = str(v)
    if s == '':
        return ''
    if any(c in s for c in '\r\n\t"\\') or s != s.strip() or s[0] in '-?:,[]{}#&*!|>%@`\'"':
        s = s.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n').replace('\t', '\\t')
        return '"' + s + '"'
    return s


def emit(key, val, indent, out):
    pad = '  ' * indent
    if isinstance(val, dict):
        if not val:
            out.append('%s%s: {}' % (pad, key)); return
        out.append('%s%s:' % (pad, key))
        for k, v in val.items():
            emit(k, v, indent + 1, out)
    elif isinstance(val, list):
        if not val:
            out.append('%s%s: []' % (pad, key))
        else:
            out.append('%s%s:' % (pad, key))
            for item in val:
                emit_item(item, indent, out)
    else:
        out.append('%s%s: %s' % (pad, key, scalar(val)))


def emit_item(item, indent, out):
    """A list element: the dash sits at `indent`, the body at indent+1, first body line folded onto the dash line."""
    sub = []
    if isinstance(item, dict):
        if not item:
            sub.append('%s{}' % ('  ' * (indent + 1)))
        for k, v in item.items():
            emit(k, v, indent + 1, sub)
    elif isinstance(item, list):
        if not item:
            sub.append('%s[]' % ('  ' * (indent + 1)))
        else:
            for it in item:
                emit_item(it, indent + 1, sub)
    else:
        sub.append('%s%s' % ('  ' * (indent + 1), scalar(item)))
    head = '  ' * (indent + 1)
    sub[0] = '  ' * indent + '- ' + sub[0][len(head):]
    out.extend(sub)


def to_yaml(tree):
    out = ['MonoBehaviour:']
    for k, v in tree.items():
        emit(k, v, 1, out)
    return '\r\n'.join(out) + '\r\n'


def out_path(container, base):
    """assets/gamedatabaseasset/<folder>/<file>.asset -> <base>/gamedatabaseasset/<folder>/<file>.asset.
    The server opens staticnpc files as Npc<map>.asset (capital N); keep that case so it works on Linux too."""
    c = container.lower()
    i = c.find('gamedatabaseasset/')
    rel = container[i:] if i >= 0 else container.lstrip('/')
    parts = rel.split('/')
    if len(parts) >= 3 and parts[-2] == 'staticnpc' and parts[-1].startswith('npc'):
        parts[-1] = 'Npc' + parts[-1][3:]
    return os.path.join(base, *parts)


def convert(bundles_dir, out_base, verbose=False):
    root = bundles_dir
    if os.path.isdir(os.path.join(root, 'gamedatabaseasset')):
        root = os.path.join(root, 'gamedatabaseasset')
    files = []
    for dirpath, _, names in os.walk(root):
        for n in names:
            if n.endswith('.h2'):
                files.append(os.path.join(dirpath, n))
    if not files:
        sys.exit('No .h2 bundles under %s - point this at the client AssetBundles folder.' % bundles_dir)
    written = skipped = 0
    for f in sorted(files):
        try:
            env = UnityPy.load(f)
        except Exception as e:
            print('  skip %s (%s)' % (os.path.basename(f), e)); skipped += 1; continue
        wrote_here = 0
        for path, obj in env.container.items():
            if obj.type.name != 'MonoBehaviour' or not path.lower().endswith('.asset'):
                continue
            try:
                tree = obj.read_typetree()
            except Exception:
                continue
            dst = out_path(path, out_base)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, 'w', encoding='utf-8', newline='') as fh:
                fh.write('%YAML 1.1\r\n%TAG !u! tag:unity3d.com,2011:\r\n--- !u!114 &11400000\r\n')
                fh.write(to_yaml(tree))
            wrote_here += 1; written += 1
        if verbose or wrote_here:
            print('  %-28s %d assets' % (os.path.basename(f), wrote_here))
    print('\nWrote %d .asset files to %s' % (written, os.path.join(out_base, 'gamedatabaseasset')))
    if skipped:
        print('%d bundles could not be read (text / display-name tables may be among them; the server runs without them,'
              ' showing numeric ids in the panel).' % skipped)
    print('The server now loads this data automatically. To use a different folder, set STUB_GDB to it.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Build the Dekaron G server game data from your own client AssetBundles.')
    ap.add_argument('bundles', help="the client's AssetBundles folder (contains gamedatabaseasset/)")
    ap.add_argument('--out', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gamedata'),
                    help='output base folder (default: tools/stub/gamedata)')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    convert(a.bundles, a.out, a.verbose)
