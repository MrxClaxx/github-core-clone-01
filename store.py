"""Shared in-memory store for the github-core-clone-01 forge.

One process, one store. Every surface (git smart HTTP, REST v3, GraphQL, MCP)
reads and writes the same objects, so an issue created over MCP reads back over
REST with the same ids.

Determinism: no wall clock anywhere in served data. A logical clock counts
up from the scenario epoch (2025-01-01T00:00:00Z); every created_at, commit
date and id sequence derives from it, so a live run and its replay serve
byte-identical state.

The scenario's viewer login is computed from the contest's fixed seed with the
same PRNG wire.ts uses (FNV-1a + mix64 + xorshift64*), so the token names
`adaiversen` and any other owner is a stranger (404, never 403).
"""

import hashlib
import os
import threading

# ---------------------------------------------------------------- scenario RNG

SCENARIO_SEED = 0x6A09E667F3BCC909
U64_MASK = 0xFFFFFFFFFFFFFFFF
SCENARIO_EPOCH_SEC = 1735689600  # 2025-01-01T00:00:00Z

FIRST_NAMES = (
    'ada', 'bram', 'cora', 'dmitri', 'elena', 'felix', 'greta', 'hugo',
    'iris', 'jonas', 'kira', 'lars', 'mira', 'nico', 'olga', 'pavel',
    'quinn', 'rosa', 'sven', 'tess', 'ulla', 'vito', 'wren', 'yara', 'zane',
)
LAST_NAMES = (
    'anders', 'bauer', 'costa', 'dahl', 'eriksen', 'fischer', 'garcia',
    'holm', 'iversen', 'jansen', 'keller', 'lind', 'moreau', 'novak',
    'olsen', 'peters', 'quist', 'rossi', 'sato', 'turi', 'ulrich', 'vega',
    'weber', 'xu', 'yilmaz', 'zimmer',
)


def _fnv1a64(text):
    h = 0xCBF29CE484222325
    for ch in text:
        h ^= ord(ch)
        h = (h * 0x100000001B3) & U64_MASK
    return h


def _mix64(value):
    z = (value + 0x9E3779B97F4A7C15) & U64_MASK
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & U64_MASK
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & U64_MASK
    return (z ^ (z >> 31)) & U64_MASK


class _Rng:
    """xorshift64*, seeded like wire.ts's Rng."""

    def __init__(self, seed):
        masked = seed & U64_MASK
        self.state = masked if masked != 0 else SCENARIO_SEED

    def next_u64(self):
        x = self.state
        x = (x ^ (x >> 12)) & U64_MASK
        x = (x ^ ((x << 25) & U64_MASK)) & U64_MASK
        x = (x ^ (x >> 27)) & U64_MASK
        self.state = x
        return (x * 0x2545F4914F6CDD1D) & U64_MASK

    def next_u32(self):
        return self.next_u64() >> 32

    def next_int(self, n):
        if n <= 1:
            return 0
        limit = 4294967296 - (4294967296 % n)
        while True:
            v = self.next_u32()
            if v < limit:
                return v % n

    def pick(self, items):
        return items[self.next_int(len(items))]


def _scenario_users():
    rng = _Rng(_mix64(SCENARIO_SEED ^ _fnv1a64('users')))
    used = set()
    users = []
    for i in range(12):
        first = rng.pick(FIRST_NAMES)
        last = rng.pick(LAST_NAMES)
        base = (first + last)
        login = base
        suffix = 2
        while login in used:
            login = '%s-%d' % (base, suffix)
            suffix += 1
        used.add(login)
        users.append({'id': 1000 + i, 'login': login,
                      'name': '%s %s' % (first, last),
                      'email': '%s@example.invalid' % login})
    return users


_SCENARIO_USERS = _scenario_users()
VIEWER = _SCENARIO_USERS[0]
STRANGER = _SCENARIO_USERS[2]
VIEWER_LOGIN = VIEWER['login']

# ---------------------------------------------------------------- logical clock


class Clock:
    """Deterministic clock: epoch + one second per tick."""

    def __init__(self):
        self._seq = 0
        self._lock = threading.Lock()

    def tick(self):
        with self._lock:
            self._seq += 1
            return SCENARIO_EPOCH_SEC + self._seq

    def iso(self, epoch_sec):
        import datetime
        return datetime.datetime.fromtimestamp(
            epoch_sec, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    def now_iso(self):
        return self.iso(self.tick())


# ---------------------------------------------------------------- git objects


def hash_object(otype, content):
    """Git object id: sha1('<type> <len>\\0' + content)."""
    header = ('%s %d\x00' % (otype, len(content))).encode('ascii')
    return hashlib.sha1(header + content).hexdigest()


def parse_commit(content):
    """Parse a commit object body into (parents, tree, author, committer, message)."""
    head, _, message = content.partition(b'\n\n')
    parents = []
    tree = None
    author = b''
    committer = b''
    for line in head.split(b'\n'):
        if line.startswith(b'tree '):
            tree = line[5:].decode('ascii')
        elif line.startswith(b'parent '):
            parents.append(line[7:].decode('ascii'))
        elif line.startswith(b'author '):
            author = line[7:]
        elif line.startswith(b'committer '):
            committer = line[10:]
    return parents, tree, author, committer, message


def parse_tree(content):
    """Parse a tree object body into [(mode, name, sha)]."""
    entries = []
    i = 0
    n = len(content)
    while i < n:
        sp = content.index(b' ', i)
        mode = content[i:sp].decode('ascii')
        nul = content.index(b'\x00', sp)
        name = content[sp + 1:nul].decode('utf-8', 'surrogateescape')
        sha = content[nul + 1:nul + 21].hex()
        entries.append((mode, name, sha))
        i = nul + 21
    return entries


def build_tree_bytes(entries):
    """Serialize [(mode, name, sha)] into a tree object body (sorted by name)."""
    out = bytearray()
    for mode, name, sha in sorted(entries, key=lambda e: e[1]):
        out += mode.encode('ascii') + b' ' + name.encode('utf-8', 'surrogateescape')
        out += b'\x00' + bytes.fromhex(sha)
    return bytes(out)


# ---------------------------------------------------------------- the store


class Repo:
    __slots__ = ('id', 'name', 'owner', 'full_name', 'description', 'private',
                 'default_branch', 'created_seq', 'branches', 'objects',
                 'issues', 'pulls', 'labels', 'next_issue_number',
                 'next_pull_number', 'next_comment_id', 'next_issue_id',
                 'next_pull_id', 'fork', 'parent')

    def __init__(self, rid, name, owner, description, private, default_branch,
                 created_seq):
        self.id = rid
        self.name = name
        self.owner = owner
        self.full_name = '%s/%s' % (owner, name)
        self.description = description or ''
        self.private = bool(private)
        self.default_branch = default_branch or 'main'
        self.created_seq = created_seq
        self.branches = {}          # branch name -> commit sha
        self.objects = {}           # sha -> (otype, content bytes)
        self.issues = []            # canonical issue dicts, in creation order
        self.pulls = []             # canonical pull dicts, in creation order
        self.labels = {}            # name -> label dict
        self.next_issue_number = 1
        self.next_pull_number = 1
        self.next_comment_id = 1
        self.next_issue_id = 1
        self.next_pull_id = 1
        self.fork = False
        self.parent = None


class Store:
    """All forge state. One global RLock guards every mutation."""

    def __init__(self):
        self.lock = threading.RLock()
        self.clock = Clock()
        self.token = os.environ.get('MAKEFUN_SHOP_TOKEN', '')
        self.repos = {}             # (owner, name) -> Repo
        self.repo_order = []        # (owner, name) keys in creation order
        self.next_repo_id = 9000
        self._req_seq = 0

    # -- misc ---------------------------------------------------------
    def next_request_id(self):
        with self.lock:
            self._req_seq += 1
            return 'req-%d' % self._req_seq

    def check_token(self, presented):
        """True when the Authorization bearer token is the shop token.

        A missing token never passes. When no token is configured (local
        practice without MAKEFUN_SHOP_TOKEN), any non-empty bearer passes.
        """
        if not presented:
            return False
        if not self.token:
            return True
        return presented == self.token

    # -- repos --------------------------------------------------------
    def create_repo(self, name, description='', private=False,
                    default_branch='main', owner=None):
        owner = owner or VIEWER_LOGIN
        with self.lock:
            key = (owner, name)
            if key in self.repos:
                return None
            seq = self.clock.tick()
            repo = Repo(self.next_repo_id, name, owner, description, private,
                        default_branch or 'main', seq)
            self.next_repo_id += 1
            self.repos[key] = repo
            self.repo_order.append(key)
            return repo

    def get_repo(self, owner, name):
        """None when the owner is not the viewer or the repo does not exist."""
        if owner != VIEWER_LOGIN:
            return None
        return self.repos.get((owner, name))

    def list_viewer_repos(self):
        with self.lock:
            return [self.repos[k] for k in self.repo_order
                    if k[0] == VIEWER_LOGIN]

    def delete_repo(self, owner, name):
        with self.lock:
            key = (owner, name)
            repo = self.repos.pop(key, None)
            if repo is not None:
                self.repo_order.remove(key)
            return repo is not None

    # -- git objects --------------------------------------------------
    def put_object(self, repo, otype, content):
        sha = hash_object(otype, content)
        repo.objects.setdefault(sha, (otype, content))
        return sha

    def get_object(self, repo, sha):
        return repo.objects.get(sha)

    def resolve_ref(self, repo, ref):
        """ref like 'refs/heads/main' or 'main' -> commit sha or None."""
        if ref in repo.branches:
            return repo.branches[ref]
        full = ref if ref.startswith('refs/') else 'refs/heads/' + ref
        if full.startswith('refs/heads/'):
            return repo.branches.get(full[len('refs/heads/'):])
        return None

    def head_commit(self, repo, branch=None):
        branch = branch or repo.default_branch
        sha = repo.branches.get(branch)
        if sha is None:
            return None
        return sha

    def commit_parents(self, repo, sha):
        obj = repo.objects.get(sha)
        if obj is None or obj[0] != 'commit':
            return []
        parents, _, _, _, _ = parse_commit(obj[1])
        return parents

    def commit_tree(self, repo, sha):
        obj = repo.objects.get(sha)
        if obj is None or obj[0] != 'commit':
            return None
        _, tree, _, _, _ = parse_commit(obj[1])
        return tree

    def is_ancestor(self, repo, ancestor, descendant):
        """True when ancestor is reachable from descendant (or equal)."""
        if ancestor == descendant:
            return True
        seen = set()
        stack = [descendant]
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            for parent in self.commit_parents(repo, cur):
                if parent == ancestor:
                    return True
                stack.append(parent)
        return False

    def merge_base(self, repo, a, b):
        """First common ancestor of a and b (BFS from both sides)."""
        if a is None or b is None:
            return None
        seen_a = {a}
        stack = [a]
        while stack:
            cur = stack.pop()
            for parent in self.commit_parents(repo, cur):
                if parent not in seen_a:
                    seen_a.add(parent)
                    stack.append(parent)
        stack = [b]
        seen_b = set()
        while stack:
            cur = stack.pop()
            if cur in seen_a:
                return cur
            if cur in seen_b:
                continue
            seen_b.add(cur)
            stack.extend(self.commit_parents(repo, cur))
        return None

    def flatten_tree(self, repo, tree_sha, prefix=''):
        """{path: (mode, blob_sha)} for every blob under tree_sha."""
        out = {}
        if tree_sha is None:
            return out
        obj = repo.objects.get(tree_sha)
        if obj is None or obj[0] != 'tree':
            return out
        for mode, name, sha in parse_tree(obj[1]):
            path = prefix + name
            if mode == '40000':
                out.update(self.flatten_tree(repo, sha, path + '/'))
            else:
                out[path] = (mode, sha)
        return out

    def build_tree(self, repo, base_tree_sha, updates):
        """New tree sha: base tree with {path: blob_sha|None} applied.

        updates maps a path to a blob sha (create/update) or None (delete).
        Intermediate trees are created as needed.
        """
        flat = self.flatten_tree(repo, base_tree_sha)
        for path, blob_sha in updates.items():
            if blob_sha is None:
                flat.pop(path, None)
            else:
                flat[path] = ('100644', blob_sha)
        # Rebuild nested trees bottom-up.
        dirs = {}

        def ensure_dir(parts):
            node = dirs
            for part in parts:
                node = node.setdefault(part, {})
            return node

        for path in flat:
            parts = path.split('/')
            ensure_dir(parts[:-1])

        def build_dir(node, prefix):
            entries = []
            for name, child in sorted(node.items()):
                if isinstance(child, dict) and '_file' not in child:
                    sub = build_dir(child, prefix + name + '/')
                    entries.append(('40000', name, sub))
            for path, (mode, blob_sha) in sorted(flat.items()):
                parts = path.split('/')
                if '/'.join(parts[:-1]) == prefix.rstrip('/'):
                    entries.append((mode, parts[-1], blob_sha))
            if not entries:
                return None
            return self.put_object(repo, 'tree', build_tree_bytes(entries))

        root = build_dir(dirs, '')
        if root is None:
            return self.put_object(repo, 'tree', b'')
        return root

    def make_commit(self, repo, branch, message, updates, author=None):
        """Commit {path: bytes|None} onto branch; returns the commit sha.

        updates maps a path to new file bytes, or None to delete the path.
        A commit on a branch with no head yet has no parents.
        """
        author = author or VIEWER
        with self.lock:
            parent = repo.branches.get(branch)
            base_tree = self.commit_tree(repo, parent) if parent else None
            blob_updates = {}
            for path, content in updates.items():
                if content is None:
                    blob_updates[path] = None
                else:
                    blob_updates[path] = self.put_object(repo, 'blob', content)
            tree = self.build_tree(repo, base_tree, blob_updates)
            epoch = self.clock.tick()
            lines = []
            lines.append(b'tree ' + tree.encode('ascii'))
            if parent:
                lines.append(b'parent ' + parent.encode('ascii'))
            ident = ('%s <%s> %d +0000' % (author['name'], author['email'],
                                           epoch)).encode('utf-8')
            lines.append(b'author ' + ident)
            lines.append(b'committer ' + ident)
            body = b'\n'.join(lines) + b'\n\n' + message.encode('utf-8')
            sha = self.put_object(repo, 'commit', body)
            repo.branches[branch] = sha
            return sha

    def tree_diff(self, repo, tree_a, tree_b):
        """{path: (sha_a|None, sha_b|None)} for blobs differing between trees."""
        flat_a = self.flatten_tree(repo, tree_a)
        flat_b = self.flatten_tree(repo, tree_b)
        out = {}
        for path in sorted(set(flat_a) | set(flat_b)):
            sha_a = flat_a[path][1] if path in flat_a else None
            sha_b = flat_b[path][1] if path in flat_b else None
            if sha_a != sha_b:
                out[path] = (sha_a, sha_b)
        return out

    # -- issues -------------------------------------------------------
    def create_issue(self, repo, title, body='', labels=()):
        """Create an issue. Raises ValueError(name) when a label is unknown:
        the REST layer turns that into a 422, and labels are never invented
        here (a label must be created through POST /labels first)."""
        with self.lock:
            missing = [name for name in labels if name not in repo.labels]
            if missing:
                raise ValueError(missing[0])
            number = repo.next_issue_number
            repo.next_issue_number += 1
            iid = repo.next_issue_id
            repo.next_issue_id += 1
            epoch = self.clock.tick()
            issue = {
                'id': iid, 'number': number, 'title': title,
                'body': body or '', 'state': 'open',
                'labels': [dict(repo.labels[name]) for name in labels],
                'user': VIEWER, 'created_seq': epoch, 'updated_seq': epoch,
                'closed_seq': None, 'comments': [],
                'assignees': [], 'milestone': None,
            }
            repo.issues.append(issue)
            return issue

    def get_issue(self, repo, number):
        for issue in repo.issues:
            if issue['number'] == number:
                return issue
        return None

    def create_label(self, repo, name, color='ededed', description=''):
        with self.lock:
            label = {'name': name, 'color': color or 'ededed',
                     'description': description or ''}
            repo.labels[name] = label
            return label

    def add_comment(self, repo, issue, body):
        with self.lock:
            cid = repo.next_comment_id
            repo.next_comment_id += 1
            epoch = self.clock.tick()
            comment = {'id': cid, 'body': body, 'user': VIEWER,
                       'created_seq': epoch, 'updated_seq': epoch}
            issue['comments'].append(comment)
            issue['updated_seq'] = epoch
            return comment

    # -- pull requests ------------------------------------------------
    def create_pull(self, repo, title, body, head, base):
        with self.lock:
            number = repo.next_pull_number
            repo.next_pull_number += 1
            pid = repo.next_pull_id
            repo.next_pull_id += 1
            epoch = self.clock.tick()
            head_sha = self.resolve_ref(repo, head)
            base_sha = self.resolve_ref(repo, base)
            pull = {
                'id': pid, 'number': number, 'title': title,
                'body': body or '', 'state': 'open',
                'head': {'ref': head, 'sha': head_sha,
                         'label': '%s:%s' % (repo.owner, head)},
                'base': {'ref': base, 'sha': base_sha,
                         'label': '%s:%s' % (repo.owner, base)},
                'merged': False, 'merge_commit_sha': None,
                'mergeable': True, 'user': VIEWER,
                'created_seq': epoch, 'updated_seq': epoch,
                'closed_seq': None, 'merged_seq': None,
            }
            repo.pulls.append(pull)
            return pull

    def get_pull(self, repo, number):
        for pull in repo.pulls:
            if pull['number'] == number:
                return pull
        return None

    def merge_pull(self, repo, pull):
        """Squash-merge an open pull onto its base branch.

        The squash commit's tree is the head branch's tree, parented on the
        base head. Returns the merge commit sha, or None when not mergeable.
        """
        with self.lock:
            if pull['state'] != 'open':
                return None
            head_sha = self.resolve_ref(repo, pull['head']['ref'])
            base_sha = self.resolve_ref(repo, pull['base']['ref'])
            if head_sha is None or base_sha is None:
                return None
            head_tree = self.commit_tree(repo, head_sha)
            if head_tree is None:
                return None
            epoch = self.clock.tick()
            lines = [b'tree ' + head_tree.encode('ascii')]
            if base_sha:
                lines.append(b'parent ' + base_sha.encode('ascii'))
            ident = ('%s <%s> %d +0000' % (VIEWER['name'], VIEWER['email'],
                                           epoch)).encode('utf-8')
            lines.append(b'author ' + ident)
            lines.append(b'committer ' + ident)
            msg = ('%s (#%d)\n\n%s' % (pull['title'], pull['number'],
                                       pull['body'] or '')).encode('utf-8')
            sha = self.put_object(repo, 'commit', b'\n'.join(lines) + b'\n\n' + msg)
            repo.branches[pull['base']['ref']] = sha
            pull['state'] = 'closed'
            pull['merged'] = True
            pull['merge_commit_sha'] = sha
            pull['merged_seq'] = epoch
            pull['closed_seq'] = epoch
            pull['updated_seq'] = epoch
            return sha


STORE = Store()
