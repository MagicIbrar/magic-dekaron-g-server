# Generic default answers for the stub: for a client query (CQ_X) the matching answer (SA_X) filled with zero / empty /
# error 0 values, built from protocol/protocol.json (field order, struct definitions) + protocol/enums.json (enum sizes).
# The point is that no request ever blocks the client: the log then shows what it asks for next, and the handlers in
# stub-server.py replace the defaults one by one once the fields are understood.
import json, os, re, struct

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO_DIR = next((p for p in (os.path.join(HERE, 'protocol'), os.path.join(HERE, '..', '..', 'protocol')) if os.path.isdir(p)),
                 os.path.join(HERE, 'protocol'))   # ./protocol (packaged) else ../../protocol (dev tree)
PROTO = json.load(open(os.path.join(PROTO_DIR, 'protocol.json'), encoding='utf-8'))
ENUMS = json.load(open(os.path.join(PROTO_DIR, 'enums.json'), encoding='utf-8'))
STRUCTS = {s['name']: s for s in PROTO['structs']}
BY_CODE = {p['code']: p for p in PROTO['packets']}
BY_NS_NAME = {(p['namespace'], p['name']): p for p in PROTO['packets']}

PRIM_SIZE = {'int': 4, 'uint': 4, 'long': 8, 'ulong': 8, 'short': 2, 'ushort': 2, 'byte': 1, 'sbyte': 1, 'bool': 1,
             'float': 4, 'double': 8, 'char': 2}
POLY = {'ActionBase', 'LogBase', 'QueryBase'}      # polymorphic blobs: no sensible zero value


class NoDefault(Exception):
    pass


def zero(t, depth=0):
    """Zero-valued wire bytes for type name t (primitive, enum, string, DateTime, Nullable<>, T[], struct)."""
    t = t.strip()
    if depth > 8:
        raise NoDefault('recursion ' + t)
    m = re.match(r'Nullable<(.+)>$', t)
    if m:
        return b'\x00'                                  # absent
    if t.endswith('[]'):
        base = t[:-2]
        if base in PRIM_SIZE or base in ENUMS:
            return struct.pack('<h', 0)                 # primitive arrays: int16 byte count
        return struct.pack('<i', 0)                     # struct / string arrays: int32 count
    if t in PRIM_SIZE:
        return b'\x00' * PRIM_SIZE[t]
    if t == 'string':
        return struct.pack('<H', 0)
    if t == 'DateTime':
        return struct.pack('<qi', 0, 0)
    if t == 'TimeSpan':
        return struct.pack('<q', 0)
    if t in ENUMS:
        return b'\x00' * PRIM_SIZE[ENUMS[t]]
    if t in POLY:
        raise NoDefault(t)
    if t in STRUCTS:
        return b''.join(zero(f['type'], depth + 1) for f in STRUCTS[t]['fields'])
    raise NoDefault('unknown type ' + t)


def answer_for(code):
    """(answerCode, answerName, bytes) for query `code`, or None when it has no SA_/zero-able answer."""
    q = BY_CODE.get(code)
    if not q or not q['name'].startswith('CQ_'):
        return None
    a = BY_NS_NAME.get((q['namespace'], 'SA_' + q['name'][3:])) or BY_CODE.get(code + 1)
    if not a or not a['name'].startswith('SA_'):
        return None
    try:
        body = b''.join(zero(f['wire']) for f in a['fields'])
    except NoDefault:
        return None
    return a['code'], a['name'], body


def describe(code):
    p = BY_CODE.get(code)
    if not p:
        return '0x%X ?' % code
    return '0x%X %s %s { %s }' % (code, p['name'], p['namespace'].split('.')[-1],
                                  '; '.join('%s %s' % (f['wire'], f['name']) for f in p['fields']))


if __name__ == '__main__':
    import sys
    for a in sys.argv[1:]:
        c = int(a, 16)
        print(describe(c))
        r = answer_for(c)
        print('  ->', r and (describe(r[0]), r[2].hex()))
