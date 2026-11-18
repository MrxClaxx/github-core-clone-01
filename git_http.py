"""Git smart-HTTP surface for the github-core-clone-01 forge.

Protocol v2 (ls-refs, fetch) and v0 (upload-pack, receive-pack) over the
shared in-memory store in store.py. Python 3.13 standard library only: no
git binary, no network, no wall clock and no randomness in any response.

Routing (from server.py): ``/{owner}/{repo}.git/info/refs``,
``/{owner}/{repo}.git/git-upload-pack`` and
``/{owner}/{repo}.git/git-receive-pack`` all land in ``handle``.

Ref storage: branches live in the shared store (``repo.branches``); any
other ref (tags, ...) lives in a module-level dict keyed by the repo's
identity, because the shared Repo has no slot for them.
"""

import hashlib
import json
import zlib

from server import send_error, send_text
from store import STORE, hash_object, parse_commit, parse_tree

ZERO = '0' * 40
_MAX_PKT = 65520
_SIDEBAND_CHUNK = 65515
_TYPE_CODES = {'commit': 1, 'tree': 2, 'blob': 3, 'tag': 4}
_CODE_TYPES = {1: 'commit', 2: 'tree', 3: 'blob', 4: 'tag'}
_JSON_TYPE = 'application/json; charset=utf-8'


class _BadRequest(Exception):
    """Malformed pkt-line / pack input: answer 400, stay healthy."""


class _NeedBase(Exception):
    """A delta's base is not resolved yet; retry after other entries."""


# --------------------------------------------------------------------------
# pkt-line framing
# --------------------------------------------------------------------------

def _pkt(payload):
    return ('%04x' % (len(payload) + 4)).encode('ascii') + payload


def _pkt_line(text):
    return _pkt(text.encode('utf-8'))


_FLUSH = b'0000'
_DELIM = b'0001'


class _Reader:
    """Sequential pkt-line reader over a raw request body."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def next(self):
        """('data', payload) | ('flush', None) | ('delim', None) |
        ('response-end', None) | (None, None) at end of input."""
        if self.pos >= len(self.data):
            return None, None
        if self.pos + 4 > len(self.data):
            raise _BadRequest('truncated pkt-line')
        head = self.data[self.pos:self.pos + 4]
        try:
            length = int(head.decode('ascii'), 16)
        except ValueError:
            raise _BadRequest('invalid pkt-line length')
        if length == 0:
            self.pos += 4
            return 'flush', None
        if length == 1:
            self.pos += 4
            return 'delim', None
        if length == 2:
            self.pos += 4
            return 'response-end', None
        if length < 4 or length > _MAX_PKT:
            raise _BadRequest('invalid pkt-line length')
        if self.pos + length > len(self.data):
            raise _BadRequest('truncated pkt-line')
        payload = self.data[self.pos + 4:self.pos + length]
        self.pos += length
        return 'data', payload


def _sideband_pack(pack):
    """Wrap pack bytes in sideband band-1 pkt-lines (<= 65520 bytes each)."""
    out = []
    for start in range(0, len(pack), _SIDEBAND_CHUNK):
        out.append(_pkt(b'\x01' + pack[start:start + _SIDEBAND_CHUNK]))
    return b''.join(out)


def _json_body(obj):
    return (json.dumps(obj, separators=(',', ':')) + '\n').encode('utf-8')


# --------------------------------------------------------------------------
# refs (branches in the shared store, everything else module-local)
# --------------------------------------------------------------------------

_EXTRA_REFS = {}  # (owner, name, repo_id) -> {ref: sha}


def _extra_refs(repo):
    key = (repo.owner, repo.name, repo.id)
    return _EXTRA_REFS.setdefault(key, {})


def _all_refs(repo):
    refs = [('refs/heads/' + branch, repo.branches[branch])
            for branch in sorted(repo.branches)]
    for ref in sorted(_extra_refs(repo)):
        refs.append((ref, _extra_refs(repo)[ref]))
    return refs


def _get_ref(repo, ref):
    if ref.startswith('refs/heads/'):
        return repo.branches.get(ref[len('refs/heads/'):])
    return _extra_refs(repo).get(ref)


def _set_ref(repo, ref, oid):
    if ref.startswith('refs/heads/'):
        branch = ref[len('refs/heads/'):]
        if oid is None:
            repo.branches.pop(branch, None)
        else:
            repo.branches[branch] = oid
    else:
        extra = _extra_refs(repo)
        if oid is None:
            extra.pop(ref, None)
        else:
            extra[ref] = oid


def _head_oid(repo):
    return repo.branches.get(repo.default_branch)


def _tag_target(content):
    head = content.split(b'\n\n', 1)[0]
    oid = None
    for line in head.split(b'\n'):
        if line.startswith(b'object '):
            oid = line[7:].decode('ascii')
    return oid


def _peel(repo, sha):
    """Follow tag objects to the ultimate non-tag target."""
    seen = set()
    cur = sha
    while True:
        obj = STORE.get_object(repo, cur)
        if obj is None or obj[0] != 'tag' or cur in seen:
            return cur
        seen.add(cur)
        target = _tag_target(obj[1])
        if not target:
            return cur
        cur = target


# --------------------------------------------------------------------------
# object graph walks (O(changed objects) for the requested set)
# --------------------------------------------------------------------------

def _closure(repo, roots, prune_parents_of=frozenset()):
    """Every object reachable from roots. Parents of commits in
    prune_parents_of are not traversed (a shallow boundary the client
    already holds: it has the commit and its tree, not its history)."""
    out = set()
    stack = [r for r in roots if r and r != ZERO]
    while stack:
        oid = stack.pop()
        if oid in out:
            continue
        obj = STORE.get_object(repo, oid)
        if obj is None:
            continue
        out.add(oid)
        otype, content = obj
        if otype == 'commit':
            parents, tree, _, _, _ = parse_commit(content)
            if tree:
                stack.append(tree)
            if oid not in prune_parents_of:
                stack.extend(parents)
        elif otype == 'tag':
            target = _tag_target(content)
            if target:
                stack.append(target)
        elif otype == 'tree':
            for mode, _name, sha in parse_tree(content):
                if mode != '160000':
                    stack.append(sha)
    return out


def _fetch_plan(repo, wants, haves, shallow_in, depth):
    """(oids to send, new shallow boundary, unshallow list, acked wants).

    No depth: reachable(wants) minus reachable(haves). With depth: only
    ``depth`` generations of commits from each want, plus each included
    commit's full tree; the deepest included commits are the new shallow
    boundary, and old shallow commits that are now interior are unshallow.
    A want is acked when the client already holds it (it is in the
    have-closure), so the fetch can answer ACK + ready instead of NAK.
    """
    shallow_set = set(shallow_in or [])
    have_set = _closure(repo, haves, shallow_set) if haves else set()
    acked = [w for w in wants if w in have_set]
    if depth is None or depth <= 0:
        return _closure(repo, wants) - have_set, [], [], acked
    send = set()
    gen = {}
    queue = []
    for want in wants:
        obj = STORE.get_object(repo, want)
        if obj is None:
            continue
        if obj[0] == 'tag':
            send.add(want)
            target = _tag_target(obj[1])
            if not target:
                continue
            tgt = STORE.get_object(repo, target)
            if tgt is None:
                continue
            if tgt[0] == 'commit':
                queue.append((target, 0))
            else:
                send |= _closure(repo, [target])
        elif obj[0] == 'commit':
            queue.append((want, 0))
        else:
            send |= _closure(repo, [want])
    index = 0
    while index < len(queue):
        oid, g = queue[index]
        index += 1
        if oid in gen:
            continue
        gen[oid] = g
        obj = STORE.get_object(repo, oid)
        if obj is None or obj[0] != 'commit':
            continue
        parents, tree, _, _, _ = parse_commit(obj[1])
        send.add(oid)
        if tree:
            send |= _closure(repo, [tree])
        if g + 1 < depth:
            for parent in parents:
                queue.append((parent, g + 1))
    boundary = sorted(oid for oid, g in gen.items() if g == depth - 1)
    interior = {oid for oid, g in gen.items() if g < depth - 1}
    unshallow = sorted(shallow_set & interior)
    send -= have_set
    return send, boundary, unshallow, acked


# --------------------------------------------------------------------------
# packfile write (non-delta pack v2, sha1 trailer)
# --------------------------------------------------------------------------

def _pack_header(otype, size):
    code = _TYPE_CODES[otype]
    out = bytearray()
    first = (code << 4) | (size & 0x0f)
    size >>= 4
    while size:
        out.append(first | 0x80)
        first = size & 0x7f
        size >>= 7
    out.append(first)
    return bytes(out)


def _build_pack(repo, oids):
    parts = [b'PACK' + (2).to_bytes(4, 'big')
             + len(oids).to_bytes(4, 'big')]
    for oid in sorted(oids):
        otype, content = STORE.get_object(repo, oid)
        parts.append(_pack_header(otype, len(content)))
        parts.append(zlib.compress(content))
    body = b''.join(parts)
    return body + hashlib.sha1(body).digest()


# --------------------------------------------------------------------------
# packfile read (full objects, OFS_DELTA, REF_DELTA)
# --------------------------------------------------------------------------

def _read_varint_size(data, pos):
    if pos >= len(data):
        raise _BadRequest('pack entry has no size header')
    first = data[pos]
    pos += 1
    typ = (first >> 4) & 0x07
    size = first & 0x0f
    shift = 4
    byte = first
    while byte & 0x80:
        if pos >= len(data):
            raise _BadRequest('pack entry size varint is truncated')
        byte = data[pos]
        pos += 1
        size |= (byte & 0x7f) << shift
        shift += 7
    return typ, size, pos


def _read_ofs_base(data, pos):
    if pos >= len(data):
        raise _BadRequest('ofs-delta has no base offset')
    byte = data[pos]
    pos += 1
    value = byte & 0x7f
    while byte & 0x80:
        if pos >= len(data):
            raise _BadRequest('ofs-delta base offset is truncated')
        byte = data[pos]
        pos += 1
        value = ((value + 1) << 7) | (byte & 0x7f)
    return value, pos


def _inflate_at(data, pos, end):
    try:
        decomp = zlib.decompressobj()
        out = decomp.decompress(data[pos:end])
    except zlib.error as exc:
        raise _BadRequest('pack zlib stream did not inflate: %s' % exc)
    if not decomp.eof:
        raise _BadRequest('truncated zlib stream in pack')
    consumed = (end - pos) - len(decomp.unused_data)
    return out, pos + consumed


def _apply_delta(base, delta):
    pos = 0

    def read_varint():
        nonlocal pos
        value = 0
        shift = 0
        while True:
            if pos >= len(delta):
                raise _BadRequest('truncated delta size varint')
            byte = delta[pos]
            pos += 1
            value |= (byte & 0x7f) << shift
            shift += 7
            if not byte & 0x80:
                return value

    src_size = read_varint()
    tgt_size = read_varint()
    if src_size != len(base):
        raise _BadRequest('delta base size mismatch')
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            copy_off = 0
            copy_size = 0
            for bit in range(4):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise _BadRequest('truncated delta copy')
                    copy_off |= delta[pos] << (bit * 8)
                    pos += 1
            for bit in range(4, 7):
                if op & (1 << bit):
                    if pos >= len(delta):
                        raise _BadRequest('truncated delta copy')
                    copy_size |= delta[pos] << ((bit - 4) * 8)
                    pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            if copy_off + copy_size > len(base):
                raise _BadRequest('delta copy out of range')
            out += base[copy_off:copy_off + copy_size]
        elif op:
            count = op & 0x7f
            if pos + count > len(delta):
                raise _BadRequest('truncated delta insert')
            out += delta[pos:pos + count]
            pos += count
        else:
            raise _BadRequest('invalid delta opcode 0')
    if len(out) != tgt_size:
        raise _BadRequest('delta size mismatch')
    return bytes(out)


def _parse_pack(repo, pack):
    """Resolve every entry of a packfile to (otype, content) objects.

    Raises _BadRequest on any structural problem (bad header, bad
    trailer, unknown type, truncated stream, unresolvable delta base).
    """
    if len(pack) < 32:
        raise _BadRequest('pack too short')
    if pack[:4] != b'PACK':
        raise _BadRequest('pack does not start with PACK')
    version = int.from_bytes(pack[4:8], 'big')
    if version not in (2, 3):
        raise _BadRequest('unsupported pack version')
    count = int.from_bytes(pack[8:12], 'big')
    trailer_start = len(pack) - 20
    if hashlib.sha1(pack[:trailer_start]).digest() != pack[trailer_start:]:
        raise _BadRequest('pack trailer checksum mismatch')
    raw = []
    by_offset = {}
    pos = 12
    for _ in range(count):
        start = pos
        typ, size, pos = _read_varint_size(pack, pos)
        entry = {'offset': start, 'type': typ, 'size': size}
        if typ == 6:
            off, pos = _read_ofs_base(pack, pos)
            if off <= 0 or off > start:
                raise _BadRequest('invalid ofs-delta base offset')
            entry['base_offset'] = start - off
        elif typ == 7:
            if pos + 20 > trailer_start:
                raise _BadRequest('truncated ref-delta base')
            entry['base_oid'] = pack[pos:pos + 20].hex()
            pos += 20
        elif typ not in _CODE_TYPES:
            raise _BadRequest('unknown pack entry type')
        data, pos = _inflate_at(pack, pos, trailer_start)
        if len(data) != size:
            raise _BadRequest('pack entry size mismatch')
        entry['data'] = data
        raw.append(entry)
        by_offset[start] = entry
    if pos != trailer_start:
        raise _BadRequest('pack has trailing bytes before its trailer')

    resolved = {}       # offset -> (otype, content)
    resolved_by_oid = {}  # oid -> entry

    def try_resolve(entry):
        typ = entry['type']
        if typ == 6:
            base = resolved.get(entry['base_offset'])
            if base is None:
                raise _NeedBase()
            return base[0], _apply_delta(base[1], entry['data'])
        if typ == 7:
            base_oid = entry['base_oid']
            base_entry = resolved_by_oid.get(base_oid)
            if base_entry is not None:
                base = resolved[base_entry['offset']]
            else:
                base = STORE.get_object(repo, base_oid)
                if base is None:
                    raise _NeedBase()
            return base[0], _apply_delta(base[1], entry['data'])
        return _CODE_TYPES[typ], entry['data']

    pending = list(raw)
    while pending:
        remaining = []
        progressed = False
        for entry in pending:
            try:
                result = try_resolve(entry)
            except _NeedBase:
                remaining.append(entry)
                continue
            oid = hash_object(result[0], result[1])
            entry['oid'] = oid
            resolved[entry['offset']] = result
            resolved_by_oid[oid] = entry
            progressed = True
        if not progressed:
            raise _BadRequest('ref-delta base is not in the pack or store')
        pending = remaining
    return [resolved[entry['offset']] for entry in raw]


# --------------------------------------------------------------------------
# info/refs
# --------------------------------------------------------------------------

def _advertisement(repo, service, v2):
    if v2:
        lines = [b'version 2\n', b'agent=forge/1.0\n',
                 b'ls-refs=unborn\n', b'fetch=shallow filter\n',
                 b'object-format=sha1\n']
        return b''.join(_pkt(line) for line in lines) + _FLUSH
    if service == 'git-receive-pack':
        caps = ('report-status side-band-64k delete-refs ofs-delta '
                'agent=forge/1.0 symref=HEAD:refs/heads/%s'
                % repo.default_branch)
    else:
        caps = ('symref=HEAD:refs/heads/%s side-band-64k thin-pack '
                'ofs-delta multi_ack multi_ack_detailed shallow '
                'agent=forge/1.0' % repo.default_branch)
    out = [_pkt(('# service=%s\n' % service).encode('ascii')), _FLUSH]
    lines = [(_head_oid(repo) or ZERO, 'HEAD')]
    for ref, oid in _all_refs(repo):
        lines.append((oid, ref))
        if ref.startswith('refs/tags/'):
            obj = STORE.get_object(repo, oid)
            if obj is not None and obj[0] == 'tag':
                lines.append((_peel(repo, oid), ref + '^{}'))
    first = True
    for oid, name in lines:
        text = '%s %s' % (oid, name)
        if first:
            text += '\x00' + caps
            first = False
        out.append(_pkt((text + '\n').encode('utf-8')))
    out.append(_FLUSH)
    return b''.join(out)


def _info_refs(req, repo):
    service = req.query.get('service', '')
    if service not in ('git-upload-pack', 'git-receive-pack'):
        return 404, _json_body({'message': 'Not Found'}), _JSON_TYPE
    proto = req.headers.get('Git-Protocol') or ''
    v2 = service == 'git-upload-pack' and 'version=2' in proto
    with STORE.lock:
        body = _advertisement(repo, service, v2)
    return 200, body, 'application/x-%s-advertisement' % service


# --------------------------------------------------------------------------
# upload-pack: v2 ls-refs / fetch, v0 fetch
# --------------------------------------------------------------------------

def _ls_refs(repo, args):
    peel = 'peel' in args
    symrefs = 'symrefs' in args
    unborn = 'unborn' in args
    prefixes = [a[len('ref-prefix '):] for a in args
                if a.startswith('ref-prefix ')]
    with STORE.lock:
        head_oid = _head_oid(repo)
        out = []

        def keep(name):
            return not prefixes or any(name.startswith(p) for p in prefixes)

        if not prefixes:
            if head_oid:
                line = '%s HEAD' % head_oid
                if symrefs:
                    line += ' symref-target:refs/heads/%s' % repo.default_branch
                out.append(line)
            elif unborn:
                line = 'unborn HEAD'
                if symrefs:
                    line += ' symref-target:refs/heads/%s' % repo.default_branch
                out.append(line)
        for ref, oid in _all_refs(repo):
            if not keep(ref):
                continue
            line = '%s %s' % (oid, ref)
            if peel and ref.startswith('refs/tags/'):
                obj = STORE.get_object(repo, oid)
                if obj is not None and obj[0] == 'tag':
                    line += ' peeled:%s' % _peel(repo, oid)
            out.append(line)
    body = b''.join(_pkt((line + '\n').encode('utf-8')) for line in out)
    body += _FLUSH
    return 200, body, 'application/x-git-upload-pack-result'


def _parse_fetch_args(args):
    wants, haves, shallow_in = [], [], []
    depth = None
    for arg in args:
        if arg.startswith('want '):
            wants.append(arg[5:].strip())
        elif arg.startswith('have '):
            haves.append(arg[5:].strip())
        elif arg.startswith('shallow '):
            shallow_in.append(arg[8:].strip())
        elif arg.startswith('deepen '):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                pass
    return wants, haves, shallow_in, depth


def _ack_lines(wants, acked):
    """ACK every held want plus ready when nothing is left to send, else NAK."""
    if wants and len(acked) == len(wants):
        return [('ACK %s\n' % oid).encode('ascii') for oid in acked] + [b'ready\n']
    return [b'NAK\n']


def _fetch_v2(repo, args):
    wants, haves, shallow_in, depth = _parse_fetch_args(args)
    with STORE.lock:
        send, shallow, unshallow, acked = _fetch_plan(repo, wants, haves,
                                                      shallow_in, depth)
        pack = _build_pack(repo, send) if send else None
    out = [_pkt(b'acknowledgments\n')]
    out += [_pkt(line) for line in _ack_lines(wants, acked)]
    out.append(_DELIM)
    if shallow or unshallow:
        out.append(_pkt(b'shallow-info\n'))
        for oid in shallow:
            out.append(_pkt(('shallow %s\n' % oid).encode('ascii')))
        for oid in unshallow:
            out.append(_pkt(('unshallow %s\n' % oid).encode('ascii')))
        out.append(_DELIM)
    if pack is not None:
        out.append(_pkt(b'packfile\n'))
        out.append(_sideband_pack(pack))
    out.append(_FLUSH)
    return 200, b''.join(out), 'application/x-git-upload-pack-result'


def _fetch_v0(repo, packets):
    wants, haves, shallow_in, depth = [], [], [], None
    for kind, payload in packets:
        if kind != 'data':
            continue
        text = payload.decode('utf-8', 'surrogateescape').rstrip('\n')
        if text.startswith('want '):
            parts = text.split(' ')
            if len(parts) >= 2:
                wants.append(parts[1])
        elif text.startswith('have '):
            haves.append(text[5:].strip())
        elif text.startswith('shallow '):
            shallow_in.append(text[8:].strip())
        elif text.startswith('deepen '):
            try:
                depth = int(text[7:].strip())
            except ValueError:
                pass
    with STORE.lock:
        send, shallow, unshallow, acked = _fetch_plan(repo, wants, haves,
                                                      shallow_in, depth)
        pack = _build_pack(repo, send) if send else None
    out = [_pkt(line) for line in _ack_lines(wants, acked)]
    for oid in shallow:
        out.append(_pkt(('shallow %s\n' % oid).encode('ascii')))
    for oid in unshallow:
        out.append(_pkt(('unshallow %s\n' % oid).encode('ascii')))
    if pack is not None:
        out.append(_sideband_pack(pack))
    out.append(_FLUSH)
    return 200, b''.join(out), 'application/x-git-upload-pack-result'


def _upload_pack(req, repo):
    body = req.read_body()
    reader = _Reader(body)
    packets = []
    while True:
        kind, payload = reader.next()
        if kind is None:
            break
        packets.append((kind, payload))
    proto = req.headers.get('Git-Protocol') or ''
    if 'version=2' in proto:
        command = None
        args = []
        seen_delim = False
        for kind, payload in packets:
            if kind == 'delim':
                seen_delim = True
                continue
            if kind != 'data':
                continue
            text = payload.decode('utf-8', 'surrogateescape')
            if command is None:
                command = text.strip()
                continue
            if not seen_delim:
                continue
            args.append(text.rstrip('\n'))
        if command == 'command=ls-refs':
            return _ls_refs(repo, args)
        if command == 'command=fetch':
            return _fetch_v2(repo, args)
        raise _BadRequest('unknown v2 command')
    return _fetch_v0(repo, packets)


# --------------------------------------------------------------------------
# receive-pack: v0 only, fast-forward only, report-status over sideband
# --------------------------------------------------------------------------

def _apply_ref(repo, old, new, ref):
    """Apply one push command. Returns (ok, reason)."""
    current = _get_ref(repo, ref)
    if new == ZERO:
        if current is None:
            return False, 'remote ref does not exist'
        if old != ZERO and old != current:
            return False, 'old value does not match'
        _set_ref(repo, ref, None)
        return True, ''
    if STORE.get_object(repo, new) is None:
        return False, 'object not found'
    if current is None:
        if old != ZERO:
            return False, 'remote ref does not exist'
        _set_ref(repo, ref, new)
        return True, ''
    if old != current:
        return False, 'old value does not match'
    if ref.startswith('refs/heads/'):
        if new == current:
            return True, ''
        if not STORE.is_ancestor(repo, current, new):
            return False, 'non-fast-forward'
        _set_ref(repo, ref, new)
        return True, ''
    if new == current:
        return True, ''
    return False, 'already exists'


def _receive_pack(req, repo):
    body = req.read_body()
    reader = _Reader(body)
    commands = []
    while True:
        kind, payload = reader.next()
        if kind is None:
            break
        if kind == 'flush':
            break
        if kind != 'data':
            continue
        text = payload.decode('utf-8', 'surrogateescape')
        if '\x00' in text:
            text = text.partition('\x00')[0]
        parts = text.rstrip('\n').split(' ')
        if len(parts) != 3:
            raise _BadRequest('bad receive-pack command')
        commands.append((parts[0], parts[1], parts[2]))
    if not commands:
        raise _BadRequest('no receive-pack commands')
    pack_bytes = body[reader.pos:]
    with STORE.lock:
        unpack_error = None
        objects = []
        if pack_bytes:
            try:
                objects = _parse_pack(repo, pack_bytes)
            except _BadRequest as exc:
                unpack_error = str(exc)
            except Exception as exc:  # never leak a stack; report unpack
                unpack_error = 'unpack failed: %s' % exc
        if unpack_error is None:
            for otype, content in objects:
                STORE.put_object(repo, otype, content)
        report_lines = []
        if unpack_error is not None:
            report_lines.append('unpack %s' % unpack_error)
            for _old, _new, ref in commands:
                report_lines.append('ng %s unpack failed' % ref)
        else:
            report_lines.append('unpack ok')
            for old, new, ref in commands:
                ok, reason = _apply_ref(repo, old, new, ref)
                if ok:
                    report_lines.append('ok %s' % ref)
                else:
                    report_lines.append('ng %s %s' % (ref, reason))
    report = b''.join(_pkt((line + '\n').encode('utf-8'))
                      for line in report_lines) + _FLUSH
    out = b''.join(_pkt(b'\x01' + report[i:i + _SIDEBAND_CHUNK])
                   for i in range(0, len(report), _SIDEBAND_CHUNK))
    out += _FLUSH
    return 200, out, 'application/x-git-receive-pack-result'


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def _route(req):
    segments = req.segments
    if len(segments) < 2 or not segments[1].endswith('.git'):
        return 404, _json_body({'message': 'Not Found'}), _JSON_TYPE
    owner = segments[0]
    name = segments[1][:-4]
    if not name:
        return 404, _json_body({'message': 'Not Found'}), _JSON_TYPE
    repo = STORE.get_repo(owner, name)
    if repo is None:
        return 404, _json_body({'message': 'Not Found'}), _JSON_TYPE
    rest = segments[2:]
    if req.method == 'GET' and rest == ['info', 'refs']:
        return _info_refs(req, repo)
    if req.method == 'POST' and rest == ['git-upload-pack']:
        return _upload_pack(req, repo)
    if req.method == 'POST' and rest == ['git-receive-pack']:
        return _receive_pack(req, repo)
    return 404, _json_body({'message': 'Not Found'}), _JSON_TYPE


def handle(req, handler):
    try:
        status, body, ctype = _route(req)
    except _BadRequest as exc:
        send_error(handler, 400, str(exc) or 'Bad Request')
        return
    except Exception as exc:  # never leak a stack or a 500 page shape
        send_error(handler, 500, 'Internal Server Error: %s' % exc)
        return
    send_text(handler, status, body, ctype)
