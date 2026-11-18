"""GitHub REST v3 surface for the github-core-clone-01 forge.

Routes under /api/v3 (see kit sdk/conformance/checks-rest.ts):
  GET  /                          200 JSON + X-OAuth-Scopes
  GET  /meta                      200 {"installed_version": ...}
  GET  /user/repos                200 list, Link rel="next" pagination
  POST /user/repos                201 repo (422 on duplicate / missing name)
  GET/PATCH/DELETE /repos/{o}/{r} 200 / 200 / 204
  GET  /repos/{o}/{r}/branches    200 [{name, commit:{sha}}]
  GET  /repos/{o}/{r}/commits     200 [{sha, commit:{message}}]
  POST /repos/{o}/{r}/git/refs    201 {ref, object:{sha}}
  GET  /repos/{o}/{r}/git/refs[/heads/{b}]
  PUT/GET/DELETE /repos/{o}/{r}/contents/{path}
  POST/GET /repos/{o}/{r}/labels  201 {name, color}
  POST/GET /repos/{o}/{r}/issues, GET/PATCH /issues/{n}, comments
  POST/GET /repos/{o}/{r}/pulls, GET /pulls/{n}, GET /pulls/{n}/files,
  PUT  /pulls/{n}/merge           200 squash merge / 404 / 405 conflict

Owner rule: any owner != viewer login -> 404, never 403 (STORE.get_repo).
Auth: every /api/v3 route requires a bearer token (401 otherwise).
Deterministic: timestamps come from STORE.clock only; no wall clock,
no randomness. Python 3.13 stdlib only; imports only store and server.
"""

import base64
import binascii
import difflib
from urllib.parse import urlencode

from server import ApiError, send_empty, send_json
from store import STORE, VIEWER, VIEWER_LOGIN, hash_object, parse_commit, parse_tree

INSTALLED_VERSION = '1.0.0'
SCOPES = 'repo, user'


# ---------------------------------------------------------------- helpers

def _iso(seq):
    return STORE.clock.iso(seq) if seq else None


def _user(u):
    return {'login': u['login'], 'id': u['id'], 'name': u.get('name', ''),
            'email': u.get('email', ''), 'type': 'User'}


def _label_view(label):
    return {'name': label['name'], 'color': label.get('color', 'ededed'),
            'description': label.get('description', ''), 'default': False}


def _repo_view(repo):
    created = _iso(repo.created_seq)
    return {
        'id': repo.id, 'name': repo.name, 'full_name': repo.full_name,
        'owner': _user({'login': repo.owner, 'id': VIEWER['id'],
                        'name': VIEWER['name'], 'email': VIEWER['email']})
        if repo.owner == VIEWER_LOGIN else
        {'login': repo.owner, 'id': 0, 'name': '', 'email': '', 'type': 'User'},
        'private': repo.private, 'description': repo.description,
        'fork': repo.fork, 'parent': repo.parent,
        'default_branch': repo.default_branch,
        'created_at': created, 'updated_at': created, 'pushed_at': created,
        'html_url': 'https://example.invalid/%s' % repo.full_name,
        'url': 'https://example.invalid/repos/%s' % repo.full_name,
        'clone_url': 'https://example.invalid/%s.git' % repo.full_name,
        'git_url': 'git://example.invalid/%s.git' % repo.full_name,
        'ssh_url': 'git@example.invalid:%s.git' % repo.full_name,
        'homepage': None, 'language': None, 'topics': [],
        'visibility': 'private' if repo.private else 'public',
        'archived': False, 'disabled': False,
        'watchers_count': 0, 'stargazers_count': 0, 'forks_count': 0,
        'open_issues_count': sum(1 for i in repo.issues if i['state'] == 'open'),
    }


def _comment_view(comment):
    return {'id': comment['id'], 'body': comment['body'],
            'user': _user(comment['user']),
            'created_at': _iso(comment['created_seq']),
            'updated_at': _iso(comment['updated_seq'])}


def _issue_view(issue):
    return {'id': issue['id'], 'number': issue['number'],
            'title': issue['title'], 'body': issue['body'],
            'state': issue['state'],
            'labels': [_label_view(l) for l in issue['labels']],
            'user': _user(issue['user']), 'assignees': issue.get('assignees', []),
            'milestone': issue.get('milestone'),
            'comments': [_comment_view(c) for c in issue['comments']],
            'created_at': _iso(issue['created_seq']),
            'updated_at': _iso(issue['updated_seq']),
            'closed_at': _iso(issue['closed_seq'])}


def _ident_date(raw):
    """'Name <email> 1735689601 +0000' -> ISO date, or None."""
    try:
        epoch = int(raw.rsplit(b' ', 2)[-2])
        return STORE.clock.iso(epoch)
    except (ValueError, IndexError):
        return None


def _commit_view(repo, sha):
    obj = repo.objects.get(sha)
    if obj is None or obj[0] != 'commit':
        return {'sha': sha, 'commit': {'message': ''}}
    parents, tree, author, committer, message = parse_commit(obj[1])
    text = message.decode('utf-8', 'replace')
    return {'sha': sha,
            'commit': {'message': text,
                       'tree': {'sha': tree},
                       'author': {'name': VIEWER['name'], 'email': VIEWER['email'],
                                  'date': _ident_date(author)},
                       'committer': {'name': VIEWER['name'], 'email': VIEWER['email'],
                                     'date': _ident_date(committer)}},
            'author': _user(VIEWER), 'committer': _user(VIEWER),
            'parents': [{'sha': p} for p in parents]}


def _head_base_sha(repo, pull, side):
    ref = pull[side]['ref']
    live = STORE.resolve_ref(repo, ref)
    return live if live is not None else pull[side]['sha']


def _has_conflict(repo, pull):
    """Three-way file conflict vs the merge-base (base tip vs head tip)."""
    base_sha = STORE.resolve_ref(repo, pull['base']['ref'])
    head_sha = STORE.resolve_ref(repo, pull['head']['ref'])
    if base_sha is None or head_sha is None:
        return False
    base = STORE.merge_base(repo, base_sha, head_sha)
    if base is None:
        return False
    flat_base = STORE.flatten_tree(repo, STORE.commit_tree(repo, base))
    flat_head = STORE.flatten_tree(repo, STORE.commit_tree(repo, head_sha))
    flat_tip = STORE.flatten_tree(repo, STORE.commit_tree(repo, base_sha))
    for path in set(flat_base) | set(flat_head) | set(flat_tip):
        b = flat_base[path][1] if path in flat_base else None
        h = flat_head[path][1] if path in flat_head else None
        t = flat_tip[path][1] if path in flat_tip else None
        if h == b or t == b or h == t:
            continue
        return True
    return False


def _pull_view(repo, pull):
    merged_at = _iso(pull['merged_seq']) if pull.get('merged_seq') else None
    return {'id': pull['id'], 'number': pull['number'],
            'title': pull['title'], 'body': pull['body'],
            'state': pull['state'],
            'head': {'ref': pull['head']['ref'],
                     'sha': _head_base_sha(repo, pull, 'head'),
                     'label': pull['head']['label']},
            'base': {'ref': pull['base']['ref'],
                     'sha': _head_base_sha(repo, pull, 'base'),
                     'label': pull['base']['label']},
            'merged': pull['merged'],
            'merge_commit_sha': pull['merge_commit_sha'],
            'mergeable': pull['state'] == 'open' and not _has_conflict(repo, pull),
            'merged_at': merged_at,
            'user': _user(pull['user']),
            'created_at': _iso(pull['created_seq']),
            'updated_at': _iso(pull['updated_seq']),
            'closed_at': _iso(pull['closed_seq'])}


def _body(req):
    """Parsed JSON body ({} when empty); ApiError(400) on malformed JSON."""
    data = req.parse_json()
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ApiError(400, 'Problems parsing JSON')
    return data


def _repo_or_404(owner, name):
    repo = STORE.get_repo(owner, name)
    if repo is None:
        raise ApiError(404, 'Not Found')
    return repo


def _int_query(req, key, default):
    try:
        return int(req.query.get(key, default))
    except (TypeError, ValueError):
        return default


def _page_params(req):
    page = max(1, _int_query(req, 'page', 1))
    per_page = _int_query(req, 'per_page', 30)
    per_page = min(100, max(1, per_page))
    return page, per_page


def _paginate(req, items, base_path):
    """Slice items to the requested page; Link rel=next when more remain."""
    page, per_page = _page_params(req)
    start = (page - 1) * per_page
    chunk = items[start:start + per_page]
    extra = None
    if start + per_page < len(items):
        host = req.headers.get('Host') or '127.0.0.1'
        # Preserve the caller's filters (state, sort, sha, ...) on the
        # followed page; only page/per_page change.
        kept = {k: v for k, v in req.query.items() if k not in ('page', 'per_page')}
        last_page = (len(items) + per_page - 1) // per_page

        def _page_url(page_no):
            qs = dict(kept)
            qs['per_page'] = str(per_page)
            qs['page'] = str(page_no)
            return 'http://%s%s?%s' % (host, base_path, urlencode(qs))

        extra = {'Link': '<%s>; rel="next", <%s>; rel="last"'
                 % (_page_url(page + 1), _page_url(last_page))}
    return chunk, extra


def _decode_b64(text):
    if not isinstance(text, str):
        raise ApiError(400, 'content must be base64')
    try:
        return base64.b64decode(''.join(text.split()), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, 'content is not valid base64')


def _resolve_tip(repo, ref):
    sha = STORE.resolve_ref(repo, ref)
    if sha is not None:
        return sha
    obj = repo.objects.get(ref)
    if obj is not None and obj[0] == 'commit':
        return ref
    return None


def _tree_entry(repo, tree_sha, name):
    """(mode, sha) of `name` in tree_sha, or None."""
    obj = repo.objects.get(tree_sha) if tree_sha else None
    if obj is None or obj[0] != 'tree':
        return None
    for mode, entry_name, sha in parse_tree(obj[1]):
        if entry_name == name:
            return mode, sha
    return None


def _lookup_path(repo, tree_sha, parts):
    """('file', blob_sha) / ('dir', tree_sha) / None for a path in a tree."""
    cur = tree_sha
    for part in parts[:-1]:
        found = _tree_entry(repo, cur, part)
        if found is None or found[0] != '40000':
            return None
        cur = found[1]
    found = _tree_entry(repo, cur, parts[-1])
    if found is None:
        return None
    mode, sha = found
    return ('dir', sha) if mode == '40000' else ('file', sha)


def _dir_listing(repo, tree_sha, prefix):
    obj = repo.objects.get(tree_sha) if tree_sha else None
    if obj is None or obj[0] != 'tree':
        return []
    out = []
    for mode, name, sha in parse_tree(obj[1]):
        path = prefix + name
        if mode == '40000':
            out.append({'type': 'dir', 'name': name, 'path': path, 'sha': sha,
                        'size': 0})
        else:
            blob = repo.objects.get(sha)
            size = len(blob[1]) if blob else 0
            out.append({'type': 'file', 'name': name, 'path': path, 'sha': sha,
                        'size': size})
    return out


def _file_view(repo, path, blob_sha, raw):
    return {'type': 'file', 'encoding': 'base64',
            'size': len(raw), 'name': path.rsplit('/', 1)[-1], 'path': path,
            'sha': blob_sha,
            'content': base64.b64encode(raw).decode('ascii')}


# ---------------------------------------------------------------- repos

def _list_user_repos(req, handler):
    repos = STORE.list_viewer_repos()
    chunk, extra = _paginate(req, repos, '/api/v3/user/repos')
    send_json(handler, 200, [_repo_view(r) for r in chunk], extra)


def _create_user_repo(req, handler):
    body = _body(req)
    name = body.get('name')
    if not name or not isinstance(name, str):
        raise ApiError(422, 'name is required')
    repo = STORE.create_repo(name, description=body.get('description') or '',
                             private=bool(body.get('private', False)),
                             default_branch=body.get('default_branch') or 'main')
    if repo is None:
        raise ApiError(422, 'Repository already exists')
    send_json(handler, 201, _repo_view(repo))


def _get_repo(req, handler, owner, name):
    send_json(handler, 200, _repo_view(_repo_or_404(owner, name)))


def _patch_repo(req, handler, owner, name):
    repo = _repo_or_404(owner, name)
    body = _body(req)
    if 'description' in body:
        repo.description = body['description'] or ''
    if 'private' in body:
        repo.private = bool(body['private'])
    if body.get('default_branch'):
        repo.default_branch = body['default_branch']
    send_json(handler, 200, _repo_view(repo))


def _delete_repo(req, handler, owner, name):
    _repo_or_404(owner, name)
    STORE.delete_repo(owner, name)
    send_empty(handler, 204)


def _list_branches(req, handler, repo):
    out = [{'name': b, 'commit': {'sha': sha}, 'protected': False}
           for b, sha in repo.branches.items()]
    send_json(handler, 200, out)


def _list_commits(req, handler, repo):
    ref = req.query.get('sha') or repo.default_branch
    tip = _resolve_tip(repo, ref)
    if tip is None:
        raise ApiError(404, 'Not Found')
    seen = []
    cur, guard = tip, 0
    while cur is not None and guard < 10000:
        guard += 1
        seen.append(_commit_view(repo, cur))
        parents = STORE.commit_parents(repo, cur)
        cur = parents[0] if parents else None
    chunk, _ = _paginate(req, seen, '/api/v3/repos/%s/%s/commits'
                         % (repo.owner, repo.name))
    send_json(handler, 200, chunk)


def _create_ref(req, handler, repo):
    body = _body(req)
    ref = body.get('ref') or ''
    sha = body.get('sha') or ''
    if not ref.startswith('refs/heads/') or not sha:
        raise ApiError(422, 'ref must be refs/heads/<branch> with a sha')
    branch = ref[len('refs/heads/'):]
    if not branch or branch in repo.branches:
        raise ApiError(422, 'Reference already exists')
    obj = repo.objects.get(sha)
    if obj is None or obj[0] != 'commit':
        raise ApiError(422, 'Invalid sha')
    repo.branches[branch] = sha
    send_json(handler, 201, {'ref': ref, 'url': '',
                             'object': {'sha': sha, 'type': 'commit', 'url': ''}})


def _get_refs(req, handler, repo, rest):
    if not rest:
        out = [{'ref': 'refs/heads/%s' % b,
                'object': {'sha': sha, 'type': 'commit'}}
               for b, sha in repo.branches.items()]
        send_json(handler, 200, out)
        return
    if rest[0] == 'heads' and len(rest) == 2:
        branch = rest[1]
        sha = repo.branches.get(branch)
        if sha is None:
            raise ApiError(404, 'Not Found')
        send_json(handler, 200, {'ref': 'refs/heads/%s' % branch,
                                 'object': {'sha': sha, 'type': 'commit'}})
        return
    raise ApiError(404, 'Not Found')


# ---------------------------------------------------------------- contents

def _put_contents(req, handler, repo, path):
    body = _body(req)
    if 'content' not in body:
        raise ApiError(422, 'content is required')
    raw = _decode_b64(body['content'])
    message = body.get('message') or 'Update %s' % path
    branch = body.get('branch') or repo.default_branch
    tip = repo.branches.get(branch)
    if tip is None and branch != repo.default_branch:
        raise ApiError(404, 'Branch not found')
    tree = STORE.commit_tree(repo, tip) if tip else None
    flat = STORE.flatten_tree(repo, tree)
    existing = flat.get(path)
    sha_in = body.get('sha')
    if existing is not None:
        if sha_in is None or sha_in != existing[1]:
            raise ApiError(409, 'sha does not match the current file')
        status = 200
    else:
        status = 201
    commit_sha = STORE.make_commit(repo, branch, message, {path: raw})
    blob_sha = hash_object('blob', raw)
    send_json(handler, status, {
        'content': _file_view(repo, path, blob_sha, raw),
        'commit': {'sha': commit_sha, 'message': message}})


def _get_contents(req, handler, repo, path):
    ref = req.query.get('ref') or repo.default_branch
    tip = _resolve_tip(repo, ref)
    if tip is None:
        raise ApiError(404, 'Not Found')
    tree = STORE.commit_tree(repo, tip)
    if not path:
        send_json(handler, 200, _dir_listing(repo, tree, ''))
        return
    parts = path.split('/')
    found = _lookup_path(repo, tree, parts)
    if found is None:
        raise ApiError(404, 'Not Found')
    kind, sha = found
    if kind == 'dir':
        send_json(handler, 200, _dir_listing(repo, sha, path + '/'))
        return
    blob = repo.objects.get(sha)
    if blob is None:
        raise ApiError(404, 'Not Found')
    send_json(handler, 200, _file_view(repo, path, sha, blob[1]))


def _delete_contents(req, handler, repo, path):
    body = _body(req)
    message = body.get('message') or 'Delete %s' % path
    branch = body.get('branch') or repo.default_branch
    tip = repo.branches.get(branch)
    if tip is None:
        raise ApiError(404, 'Not Found')
    flat = STORE.flatten_tree(repo, STORE.commit_tree(repo, tip))
    existing = flat.get(path)
    if existing is None:
        raise ApiError(404, 'Not Found')
    sha_in = body.get('sha')
    if sha_in is not None and sha_in != existing[1]:
        raise ApiError(409, 'sha does not match the current file')
    commit_sha = STORE.make_commit(repo, branch, message, {path: None})
    send_json(handler, 200, {'content': None,
                             'commit': {'sha': commit_sha, 'message': message}})


# ---------------------------------------------------------------- labels / issues

def _create_label(req, handler, repo):
    body = _body(req)
    name = body.get('name')
    if not name or not isinstance(name, str):
        raise ApiError(422, 'name is required')
    label = STORE.create_label(repo, name, color=body.get('color') or 'ededed',
                               description=body.get('description') or '')
    send_json(handler, 201, _label_view(label))


def _list_labels(req, handler, repo):
    send_json(handler, 200, [_label_view(l) for l in repo.labels.values()])


def _create_issue(req, handler, repo):
    body = _body(req)
    title = body.get('title')
    if not title or not isinstance(title, str):
        raise ApiError(422, 'title is required')
    labels = body.get('labels') or []
    names = [l.get('name') if isinstance(l, dict) else l for l in labels]
    try:
        issue = STORE.create_issue(repo, title, body=body.get('body') or '',
                                   labels=names)
    except ValueError as exc:
        raise ApiError(422, 'Label does not exist: %s' % exc)
    send_json(handler, 201, _issue_view(issue))


def _list_issues(req, handler, repo):
    state = req.query.get('state', 'open')
    items = repo.issues
    if state != 'all':
        items = [i for i in items if i['state'] == state]
    sort_key = req.query.get('sort', 'created')
    key = (lambda i: i['updated_seq']) if sort_key == 'updated' else \
        (lambda i: i['created_seq'])
    items = sorted(items, key=key,
                   reverse=req.query.get('direction', 'desc') != 'asc')
    chunk, extra = _paginate(req, items, '/api/v3/repos/%s/%s/issues'
                             % (repo.owner, repo.name))
    send_json(handler, 200, [_issue_view(i) for i in chunk], extra)


def _get_issue(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    issue = STORE.get_issue(repo, num)
    if issue is None:
        raise ApiError(404, 'Not Found')
    send_json(handler, 200, _issue_view(issue))


def _patch_issue(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    issue = STORE.get_issue(repo, num)
    if issue is None:
        raise ApiError(404, 'Not Found')
    body = _body(req)
    if 'title' in body:
        issue['title'] = body['title'] or issue['title']
    if 'body' in body:
        issue['body'] = body['body'] or ''
    if 'labels' in body:
        names = [l.get('name') if isinstance(l, dict) else l
                 for l in (body['labels'] or [])]
        missing = [n for n in names if n not in repo.labels]
        if missing:
            raise ApiError(422, 'Label does not exist: %s' % missing[0])
        issue['labels'] = [dict(repo.labels[n]) for n in names]
    if 'state' in body and body['state'] in ('open', 'closed'):
        epoch = STORE.clock.tick()
        issue['state'] = body['state']
        issue['updated_seq'] = epoch
        issue['closed_seq'] = epoch if body['state'] == 'closed' else None
    else:
        issue['updated_seq'] = STORE.clock.tick()
    send_json(handler, 200, _issue_view(issue))


def _create_comment(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    issue = STORE.get_issue(repo, num)
    if issue is None:
        raise ApiError(404, 'Not Found')
    body = _body(req)
    text = body.get('body')
    if not text or not isinstance(text, str):
        raise ApiError(422, 'body is required')
    comment = STORE.add_comment(repo, issue, text)
    send_json(handler, 201, _comment_view(comment))


def _list_comments(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    issue = STORE.get_issue(repo, num)
    if issue is None:
        raise ApiError(404, 'Not Found')
    send_json(handler, 200, [_comment_view(c) for c in issue['comments']])


# ---------------------------------------------------------------- pulls

def _create_pull(req, handler, repo):
    body = _body(req)
    title = body.get('title')
    head = body.get('head')
    if not title or not head:
        raise ApiError(422, 'title and head are required')
    head = str(head).split(':', 1)[-1]
    base = str(body.get('base') or repo.default_branch).split(':', 1)[-1]
    if STORE.resolve_ref(repo, head) is None:
        raise ApiError(422, 'head branch not found')
    if STORE.resolve_ref(repo, base) is None:
        raise ApiError(422, 'base branch not found')
    pull = STORE.create_pull(repo, title, body.get('body') or '', head, base)
    send_json(handler, 201, _pull_view(repo, pull))


def _list_pulls(req, handler, repo):
    state = req.query.get('state', 'open')
    items = repo.pulls
    if state != 'all':
        items = [p for p in items if p['state'] == state]
    chunk, extra = _paginate(req, items, '/api/v3/repos/%s/%s/pulls'
                             % (repo.owner, repo.name))
    send_json(handler, 200, [_pull_view(repo, p) for p in chunk], extra)


def _get_pull(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    pull = STORE.get_pull(repo, num)
    if pull is None:
        raise ApiError(404, 'Not Found')
    send_json(handler, 200, _pull_view(repo, pull))


def _pull_files(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    pull = STORE.get_pull(repo, num)
    if pull is None:
        raise ApiError(404, 'Not Found')
    head_sha = STORE.resolve_ref(repo, pull['head']['ref'])
    base_sha = STORE.resolve_ref(repo, pull['base']['ref'])
    if head_sha is None or base_sha is None:
        raise ApiError(404, 'Not Found')
    merge_base = STORE.merge_base(repo, base_sha, head_sha) or base_sha
    diff = STORE.tree_diff(repo, STORE.commit_tree(repo, merge_base),
                           STORE.commit_tree(repo, head_sha))
    out = []
    for path in sorted(diff):
        sha_a, sha_b = diff[path]
        raw_a = repo.objects[sha_a][1] if sha_a and sha_a in repo.objects else b''
        raw_b = repo.objects[sha_b][1] if sha_b and sha_b in repo.objects else b''
        lines_a = raw_a.decode('utf-8', 'replace').splitlines()
        lines_b = raw_b.decode('utf-8', 'replace').splitlines()
        if sha_a is None:
            status, additions, deletions = 'added', len(lines_b), 0
        elif sha_b is None:
            status, additions, deletions = 'removed', 0, len(lines_a)
        else:
            status = 'modified'
            additions = deletions = 0
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
                    None, lines_a, lines_b).get_opcodes():
                if tag == 'insert':
                    additions += j2 - j1
                elif tag == 'delete':
                    deletions += i2 - i1
                elif tag == 'replace':
                    additions += j2 - j1
                    deletions += i2 - i1
        out.append({'filename': path, 'status': status,
                    'additions': additions, 'deletions': deletions,
                    'changes': additions + deletions,
                    'sha': sha_b or sha_a})
    send_json(handler, 200, out)


def _merge_pull(req, handler, repo, number):
    try:
        num = int(number)
    except ValueError:
        raise ApiError(404, 'Not Found')
    pull = STORE.get_pull(repo, num)
    if pull is None:
        raise ApiError(404, 'Not Found')
    if pull['state'] != 'open':
        raise ApiError(405, 'Pull request is already closed')
    if _has_conflict(repo, pull):
        raise ApiError(405, 'Merge conflict')
    sha = STORE.merge_pull(repo, pull)
    if sha is None:
        raise ApiError(405, 'Pull request cannot be merged')
    send_json(handler, 200, {'sha': sha, 'merged': True,
                             'message': 'Pull request successfully merged'})


# ---------------------------------------------------------------- dispatch

def handle(req, handler):
    """Route one /api/v3 request. ApiError propagates to server.dispatch."""
    req.require_auth()
    segs = req.segments[2:]  # strip ['api', 'v3']
    method = req.method

    if not segs:
        if method == 'GET':
            send_json(handler, 200, {
                'current_user_url': '/api/v3/user',
                'repository_url': '/api/v3/repos/{owner}/{repo}'},
                {'X-OAuth-Scopes': SCOPES})
            return
        raise ApiError(404, 'Not Found')

    if segs == ['meta']:
        if method == 'GET':
            send_json(handler, 200, {'installed_version': INSTALLED_VERSION})
            return
        raise ApiError(404, 'Not Found')

    if segs == ['user', 'repos']:
        if method == 'GET':
            _list_user_repos(req, handler)
            return
        if method == 'POST':
            _create_user_repo(req, handler)
            return
        raise ApiError(404, 'Not Found')

    if len(segs) >= 3 and segs[0] == 'repos':
        owner, name, rest = segs[1], segs[2], segs[3:]
        repo = _repo_or_404(owner, name)
        if not rest:
            if method == 'GET':
                _get_repo(req, handler, owner, name)
            elif method == 'PATCH':
                _patch_repo(req, handler, owner, name)
            elif method == 'DELETE':
                _delete_repo(req, handler, owner, name)
            else:
                raise ApiError(404, 'Not Found')
            return
        if rest == ['branches']:
            if method == 'GET':
                _list_branches(req, handler, repo)
                return
            raise ApiError(404, 'Not Found')
        if rest == ['commits']:
            if method == 'GET':
                _list_commits(req, handler, repo)
                return
            raise ApiError(404, 'Not Found')
        if rest[:2] == ['git', 'refs']:
            if method == 'POST' and rest == ['git', 'refs']:
                _create_ref(req, handler, repo)
                return
            if method == 'GET':
                _get_refs(req, handler, repo, rest[2:])
                return
            raise ApiError(404, 'Not Found')
        if rest[0] == 'contents':
            path = '/'.join(rest[1:])
            if method == 'PUT':
                _put_contents(req, handler, repo, path)
            elif method == 'GET':
                _get_contents(req, handler, repo, path)
            elif method == 'DELETE':
                _delete_contents(req, handler, repo, path)
            else:
                raise ApiError(404, 'Not Found')
            return
        if rest == ['labels']:
            if method == 'POST':
                _create_label(req, handler, repo)
            elif method == 'GET':
                _list_labels(req, handler, repo)
            else:
                raise ApiError(404, 'Not Found')
            return
        if rest == ['issues']:
            if method == 'POST':
                _create_issue(req, handler, repo)
            elif method == 'GET':
                _list_issues(req, handler, repo)
            else:
                raise ApiError(404, 'Not Found')
            return
        if len(rest) == 2 and rest[0] == 'issues':
            if method == 'GET':
                _get_issue(req, handler, repo, rest[1])
            elif method == 'PATCH':
                _patch_issue(req, handler, repo, rest[1])
            else:
                raise ApiError(404, 'Not Found')
            return
        if len(rest) == 3 and rest[0] == 'issues' and rest[2] == 'comments':
            if method == 'POST':
                _create_comment(req, handler, repo, rest[1])
            elif method == 'GET':
                _list_comments(req, handler, repo, rest[1])
            else:
                raise ApiError(404, 'Not Found')
            return
        if rest == ['pulls']:
            if method == 'POST':
                _create_pull(req, handler, repo)
            elif method == 'GET':
                _list_pulls(req, handler, repo)
            else:
                raise ApiError(404, 'Not Found')
            return
        if len(rest) == 2 and rest[0] == 'pulls':
            if method == 'GET':
                _get_pull(req, handler, repo, rest[1])
            else:
                raise ApiError(404, 'Not Found')
            return
        if len(rest) == 3 and rest[0] == 'pulls' and rest[2] == 'files':
            if method == 'GET':
                _pull_files(req, handler, repo, rest[1])
                return
            raise ApiError(404, 'Not Found')
        if len(rest) == 3 and rest[0] == 'pulls' and rest[2] == 'merge':
            if method == 'PUT':
                _merge_pull(req, handler, repo, rest[1])
                return
            raise ApiError(404, 'Not Found')

    raise ApiError(404, 'Not Found')
