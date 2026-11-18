"""GraphQL v4 surface for the github-core-clone-01 forge.

A small, dependency-free GraphQL executor (Python 3.13 stdlib only) serving
POST /api/graphql. It speaks the documents gh and github-mcp-server send:
viewer/repository reads, issue and pull-request reads and connections,
createIssue / closeIssue / createPullRequest / mergePullRequest / addComment,
Relay node(id:), __type/__schema introspection, and GitHub-style operation
batching (a JSON array of operations answered in order). @skip/@include
directives (including variable-driven `if:`) are honoured.

All state lives in the shared store (store.py); nothing here touches the
network, the wall clock, or randomness, so responses are deterministic.
Global ids are opaque base64 of "<Kind>:<repo db id>:<number>" — stable
across reads and round-trippable by the mutations and node(id:).
"""

import base64
import re

from server import send_json
from store import STORE, VIEWER

# --------------------------------------------------------------------------
# Schema surface (field names per type, and the enums __type can describe).
# --------------------------------------------------------------------------

TYPE_FIELDS = {
    'Query': ['viewer', 'repository', 'node', '__type', '__schema'],
    'Mutation': ['createIssue', 'closeIssue', 'createPullRequest',
                 'mergePullRequest', 'addComment'],
    'Repository': ['id', 'databaseId', 'name', 'nameWithOwner', 'owner',
                   'description', 'isPrivate', 'defaultBranchRef',
                   'viewerPermission', 'hasIssuesEnabled', 'issue',
                   'issues', 'pullRequests', 'pullRequest', 'createdAt',
                   'url'],
    'Issue': ['id', 'number', 'title', 'body', 'state', 'author',
              'createdAt', 'updatedAt', 'labels', 'comments', 'url'],
    'PullRequest': ['id', 'number', 'title', 'body', 'state', 'merged',
                    'mergeable', 'headRefName', 'baseRefName', 'headRef',
                    'baseRef', 'author', 'createdAt', 'updatedAt', 'files',
                    'url'],
    'User': ['id', 'login', 'name', 'email'],
    'Ref': ['name'],
    'IssueConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'PullRequestConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'PullRequestChangedFileConnection': ['nodes', 'edges', 'pageInfo',
                                         'totalCount'],
    'IssueCommentConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'LabelConnection': ['nodes', 'edges', 'pageInfo', 'totalCount'],
    'IssueEdge': ['node', 'cursor'],
    'PullRequestEdge': ['node', 'cursor'],
    'PullRequestChangedFileEdge': ['node', 'cursor'],
    'IssueCommentEdge': ['node', 'cursor'],
    'LabelEdge': ['node', 'cursor'],
    'PageInfo': ['hasNextPage', 'endCursor', 'hasPreviousPage',
                 'startCursor'],
    'PullRequestChangedFile': ['path'],
    'IssueComment': ['id', 'body', 'author', 'createdAt', 'updatedAt'],
    'Label': ['name', 'color', 'description'],
    'CreateIssuePayload': ['issue'],
    'CloseIssuePayload': ['issue'],
    'CreatePullRequestPayload': ['pullRequest'],
    'MergePullRequestPayload': ['pullRequest'],
    'AddCommentPayload': ['commentEdge'],
    '__Type': ['name', 'kind', 'description', 'fields', 'enumValues'],
    '__Schema': ['queryType', 'mutationType', 'subscriptionType', 'types'],
    '__Field': ['name', 'description', 'args', 'type'],
    '__EnumValue': ['name', 'description'],
    '__InputValue': ['name', 'description'],
}

ENUMS = {
    'SearchType': ['ISSUE', 'REPOSITORY', 'USER', 'DISCUSSION', 'COMMIT',
                   'CODE', 'MILESTONE', 'PACKAGE', 'PROJECT', 'WIKI'],
    'IssueState': ['OPEN', 'CLOSED'],
    'IssueOrderField': ['CREATED_AT', 'UPDATED_AT', 'COMMENTS'],
    'OrderDirection': ['ASC', 'DESC'],
    'MergeableState': ['MERGEABLE', 'CONFLICTING', 'UNKNOWN'],
    'PullRequestState': ['OPEN', 'CLOSED', 'MERGED'],
    'PullRequestMergeMethod': ['MERGE', 'SQUASH', 'REBASE'],
    'RepositoryPermission': ['ADMIN', 'MAINTAIN', 'WRITE', 'TRIAGE',
                             'READ'],
}

INPUT_TYPES = {
    'CreateIssueInput': ['repositoryId', 'title', 'body'],
    'CloseIssueInput': ['issueId'],
    'CreatePullRequestInput': ['repositoryId', 'baseRefName', 'headRefName',
                               'title', 'body'],
    'MergePullRequestInput': ['pullRequestId', 'mergeMethod'],
    'AddCommentInput': ['subjectId', 'body'],
}

# --------------------------------------------------------------------------
# Global ids: opaque, stable, decodable.
# --------------------------------------------------------------------------


def _gid(kind, *parts):
    raw = ':'.join([kind] + [str(p) for p in parts])
    return base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii')


def _ungid(gid):
    if not isinstance(gid, str) or not gid:
        return None
    try:
        raw = base64.urlsafe_b64decode(gid.encode('ascii')).decode('utf-8')
    except Exception:
        return None
    parts = raw.split(':')
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]


def _find_repo_by_id(dbid):
    try:
        dbid = int(dbid)
    except (TypeError, ValueError):
        return None
    for repo in STORE.repos.values():
        if repo.id == dbid:
            return repo
    return None


def _repo_by_gid(gid):
    dec = _ungid(gid)
    if dec is None or dec[0] != 'Repository' or len(dec[1]) != 1:
        return None
    return _find_repo_by_id(dec[1][0])


def _issue_by_gid(gid):
    dec = _ungid(gid)
    if dec is None or dec[0] != 'Issue' or len(dec[1]) != 2:
        return None
    repo = _find_repo_by_id(dec[1][0])
    if repo is None:
        return None
    try:
        number = int(dec[1][1])
    except ValueError:
        return None
    issue = STORE.get_issue(repo, number)
    if issue is None:
        return None
    return repo, issue


def _pull_by_gid(gid):
    dec = _ungid(gid)
    if dec is None or dec[0] != 'PullRequest' or len(dec[1]) != 2:
        return None
    repo = _find_repo_by_id(dec[1][0])
    if repo is None:
        return None
    try:
        number = int(dec[1][1])
    except ValueError:
        return None
    pull = STORE.get_pull(repo, number)
    if pull is None:
        return None
    return repo, pull


# --------------------------------------------------------------------------
# Cursors: opaque base64 of "cursor:<index into the ordered list>".
# --------------------------------------------------------------------------


def _encode_cursor(index):
    return base64.urlsafe_b64encode(
        ('cursor:%d' % index).encode('utf-8')).decode('ascii')


def _decode_cursor(cursor):
    if not isinstance(cursor, str) or not cursor:
        return -1
    try:
        raw = base64.urlsafe_b64decode(cursor.encode('ascii')).decode('utf-8')
    except Exception:
        return -1
    if not raw.startswith('cursor:'):
        return -1
    try:
        return int(raw[7:])
    except ValueError:
        return -1


# --------------------------------------------------------------------------
# Lexer / parser for the GraphQL documents the checks send.
# --------------------------------------------------------------------------


class _ParseError(Exception):
    pass


_TOKEN_RE = re.compile(
    r"""(?P<ws>\s+|\#[^\n]*)
      | (?P<ellipsis>\.\.\.)
      | (?P<punct>[!$():=@\[\]{|}&])
      | (?P<number>-?[0-9]+(\.[0-9]*)?([eE][+-]?[0-9]+)?)
      | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """, re.VERBOSE)


def _tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == ',':
            # Commas are insignificant whitespace in GraphQL.
            i += 1
            continue
        if ch == '"':
            if text.startswith('"""', i):
                j = text.find('"""', i + 3)
                if j < 0:
                    raise _ParseError('unterminated block string')
                tokens.append(('string', text[i + 3:j]))
                i = j + 3
                continue
            j = i + 1
            buf = []
            while j < n:
                c = text[j]
                if c == '"':
                    break
                if c == '\\':
                    j += 1
                    if j >= n:
                        break
                    esc = text[j]
                    buf.append({'n': '\n', 't': '\t', 'r': '\r', '"': '"',
                                '\\': '\\', '/': '/', 'b': '\b',
                                'f': '\f'}.get(esc, esc))
                    j += 1
                    continue
                buf.append(c)
                j += 1
            else:
                raise _ParseError('unterminated string')
            tokens.append(('string', ''.join(buf)))
            i = j + 1
            continue
        m = _TOKEN_RE.match(text, i)
        if m is None:
            raise _ParseError('unexpected character %r' % ch)
        kind = m.lastgroup
        if kind != 'ws':
            value = m.group(0)
            if kind == 'number':
                kind = ('float' if ('.' in value or 'e' in value
                                    or 'E' in value) else 'int')
            tokens.append((kind, value))
        i = m.end()
    tokens.append(('eof', ''))
    return tokens


class _Parser:
    def __init__(self, tokens):
        self.toks = tokens
        self.i = 0

    def peek(self):
        return self.toks[self.i]

    def next(self):
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, kind, value=None):
        tok = self.next()
        if tok[0] != kind or (value is not None and tok[1] != value):
            raise _ParseError('expected %s %s, got %r' % (kind, value, tok))
        return tok

    def skip(self, kind, value=None):
        tok = self.peek()
        if tok[0] == kind and (value is None or tok[1] == value):
            self.i += 1
            return True
        return False

    def parse_directives(self):
        """Parse trailing @directives; returns [(name, {arg: value-node})]."""
        directives = []
        while self.peek()[0] == 'punct' and self.peek()[1] == '@':
            self.next()
            name = self.expect('name')[1]
            args = {}
            if self.peek()[0] == 'punct' and self.peek()[1] == '(':
                args = self.parse_args()
            directives.append((name, args))
        return directives

    def parse_type(self):
        if self.skip('punct', '['):
            self.parse_type()
            self.expect('punct', ']')
        else:
            self.expect('name')
        self.skip('punct', '!')

    def parse_var_defs(self):
        defs = {}
        self.expect('punct', '(')
        while not self.skip('punct', ')'):
            self.expect('punct', '$')
            name = self.expect('name')[1]
            self.expect('punct', ':')
            self.parse_type()
            default = None
            if self.skip('punct', '='):
                default = self.parse_value()
            defs[name] = default
        return defs

    def parse_args(self):
        args = {}
        self.expect('punct', '(')
        while not self.skip('punct', ')'):
            name = self.expect('name')[1]
            self.expect('punct', ':')
            args[name] = self.parse_value()
        return args

    def parse_value(self):
        tok = self.next()
        if tok[0] == 'punct' and tok[1] == '$':
            return ('var', self.expect('name')[1])
        if tok[0] == 'int':
            return ('int', int(tok[1]))
        if tok[0] == 'float':
            return ('float', float(tok[1]))
        if tok[0] == 'string':
            return ('string', tok[1])
        if tok[0] == 'name':
            if tok[1] == 'true':
                return ('bool', True)
            if tok[1] == 'false':
                return ('bool', False)
            if tok[1] == 'null':
                return ('null', None)
            return ('enum', tok[1])
        if tok[0] == 'punct' and tok[1] == '[':
            items = []
            while not self.skip('punct', ']'):
                items.append(self.parse_value())
            return ('list', items)
        if tok[0] == 'punct' and tok[1] == '{':
            obj = {}
            while not self.skip('punct', '}'):
                key = self.expect('name')[1]
                self.expect('punct', ':')
                obj[key] = self.parse_value()
            return ('object', obj)
        raise _ParseError('unexpected value token %r' % (tok,))

    def parse_selection_set(self):
        self.expect('punct', '{')
        sels = []
        while not self.skip('punct', '}'):
            sels.append(self.parse_selection())
        return sels

    def parse_selection(self):
        if self.peek()[0] == 'ellipsis':
            self.next()
            nxt = self.peek()
            if nxt[0] == 'name' and nxt[1] == 'on':
                self.next()
                cond = self.expect('name')[1]
                directives = self.parse_directives()
                return ('inline', cond, self.parse_selection_set(),
                        directives)
            if nxt[0] == 'name':
                name = self.next()[1]
                directives = self.parse_directives()
                return ('spread', name, directives)
            if nxt[0] == 'punct' and nxt[1] == '{':
                directives = self.parse_directives()
                return ('inline', None, self.parse_selection_set(),
                        directives)
            raise _ParseError('bad fragment')
        name = self.expect('name')[1]
        alias = None
        if self.skip('punct', ':'):
            alias = name
            name = self.expect('name')[1]
        args = {}
        if self.peek()[0] == 'punct' and self.peek()[1] == '(':
            args = self.parse_args()
        directives = self.parse_directives()
        sub = None
        if self.peek()[0] == 'punct' and self.peek()[1] == '{':
            sub = self.parse_selection_set()
        return ('field', alias, name, args, sub, directives)

    def parse_operation(self):
        if self.peek()[0] == 'punct' and self.peek()[1] == '{':
            return {'kind': 'query', 'name': None, 'var_defs': {},
                    'selections': self.parse_selection_set()}
        kind = self.expect('name')[1]
        if kind not in ('query', 'mutation', 'subscription'):
            raise _ParseError('unknown operation %r' % kind)
        name = None
        if self.peek()[0] == 'name':
            name = self.next()[1]
        var_defs = {}
        if self.peek()[0] == 'punct' and self.peek()[1] == '(':
            var_defs = self.parse_var_defs()
        self.parse_directives()
        return {'kind': kind, 'name': name, 'var_defs': var_defs,
                'selections': self.parse_selection_set()}

    def parse_document(self):
        ops = []
        frags = {}
        while self.peek()[0] != 'eof':
            tok = self.peek()
            if (tok[0] == 'punct' and tok[1] == '{') or (tok[0] == 'name'
                                 and tok[1] in ('query', 'mutation',
                                                'subscription')):
                ops.append(self.parse_operation())
            elif tok[0] == 'name' and tok[1] == 'fragment':
                self.next()
                fname = self.expect('name')[1]
                if self.expect('name')[1] != 'on':
                    raise _ParseError('fragment needs "on"')
                cond = self.expect('name')[1]
                self.parse_directives()
                frags[fname] = (cond, self.parse_selection_set())
            else:
                raise _ParseError('unexpected definition %r' % (tok,))
        return ops, frags


def parse_document(text):
    return _Parser(_tokenize(text)).parse_document()


# --------------------------------------------------------------------------
# Resolved values: GQLNode carries a GraphQL type name plus the store
# object (or plain dict) its fields read from.
# --------------------------------------------------------------------------


class GQLNode:
    __slots__ = ('type', 'data')

    def __init__(self, type_name, data):
        self.type = type_name
        self.data = data


def _repo_node(repo):
    return GQLNode('Repository', repo)


def _issue_node(repo, issue):
    return GQLNode('Issue', (repo, issue))


def _pull_node(repo, pull):
    return GQLNode('PullRequest', (repo, pull))


def _user_node(user):
    return GQLNode('User', user)


def _issue_state(issue):
    return 'OPEN' if issue['state'] == 'open' else 'CLOSED'


def _pull_state(pull):
    if pull['state'] == 'open':
        return 'OPEN'
    return 'MERGED' if pull['merged'] else 'CLOSED'


def _connection(kind, nodes, first, after):
    """Slice `nodes` (already ordered) into one page; cursors are indexes."""
    start = _decode_cursor(after) + 1
    if start < 0:
        start = 0
    if first is None:
        page = nodes[start:]
    else:
        page = nodes[start:start + first]
    cursors = [_encode_cursor(start + j) for j in range(len(page))]
    has_next = (start + len(page)) < len(nodes)
    end_cursor = cursors[-1] if cursors else None
    return GQLNode(kind + 'Connection', {
        'items': page, 'cursors': cursors, 'has_next': has_next,
        'end_cursor': end_cursor, 'total': len(nodes),
    })


def _issues_connection(repo, args):
    issues = list(repo.issues)
    states = args.get('states')
    if states:
        wanted = {'OPEN': 'open', 'CLOSED': 'closed'}
        keep = {wanted.get(s, str(s).lower()) for s in states}
        issues = [i for i in issues if i['state'] in keep]
    order_by = args.get('orderBy') or {}
    field = order_by.get('field') or 'CREATED_AT'
    direction = order_by.get('direction') or 'ASC'
    if field == 'UPDATED_AT':
        key = lambda i: i['updated_seq']  # noqa: E731
    elif field == 'COMMENTS':
        key = lambda i: len(i['comments'])  # noqa: E731
    else:  # CREATED_AT and anything else
        key = lambda i: i['created_seq']  # noqa: E731
    issues.sort(key=lambda i: (key(i), i['number']),
                reverse=(direction == 'DESC'))
    nodes = [_issue_node(repo, i) for i in issues]
    return _connection('Issue', nodes, args.get('first'), args.get('after'))


def _pulls_connection(repo, args):
    pulls = list(repo.pulls)
    states = args.get('states')
    if states:
        wanted = {'OPEN': 'open', 'CLOSED': 'closed', 'MERGED': 'merged'}
        keep = {wanted.get(s, str(s).lower()) for s in states}
        pulls = [p for p in pulls
                 if ('merged' if p['merged'] else p['state']) in keep]
    nodes = [_pull_node(repo, p) for p in pulls]
    return _connection('PullRequest', nodes, args.get('first'),
                       args.get('after'))


def _files_connection(repo, pull, args):
    head_sha = STORE.resolve_ref(repo, pull['head']['ref'])
    base_sha = STORE.resolve_ref(repo, pull['base']['ref'])
    head_tree = STORE.commit_tree(repo, head_sha) if head_sha else None
    base_tree = STORE.commit_tree(repo, base_sha) if base_sha else None
    diff = STORE.tree_diff(repo, base_tree, head_tree)
    nodes = [GQLNode('PullRequestChangedFile', path)
             for path in sorted(diff.keys())]
    return _connection('PullRequestChangedFile', nodes, args.get('first'),
                       args.get('after'))


# --------------------------------------------------------------------------
# Field resolution.
# --------------------------------------------------------------------------


def _resolve_field(node, name, args, exe):
    """Resolve one field of `node` to a scalar, a list, a GQLNode, or None."""
    t = node.type
    d = node.data

    if t == 'Query':
        if name == 'viewer':
            return _user_node(VIEWER)
        if name == 'repository':
            repo = STORE.get_repo(args.get('owner'), args.get('name'))
            return _repo_node(repo) if repo is not None else None
        if name == 'node':
            dec = _ungid(args.get('id'))
            if dec is None:
                return None
            kind, parts = dec
            if kind == 'Repository' and len(parts) == 1:
                repo = _find_repo_by_id(parts[0])
                return _repo_node(repo) if repo is not None else None
            if kind == 'Issue' and len(parts) == 2:
                loc = _issue_by_gid(args.get('id'))
                return _issue_node(*loc) if loc is not None else None
            if kind == 'PullRequest' and len(parts) == 2:
                loc = _pull_by_gid(args.get('id'))
                return _pull_node(*loc) if loc is not None else None
            return None
        if name == '__type':
            tname = args.get('name')
            if tname in TYPE_FIELDS or tname in ENUMS or tname in INPUT_TYPES:
                return GQLNode('__Type', tname)
            return None
        if name == '__schema':
            return GQLNode('__Schema', None)

    if t == 'Mutation':
        return _resolve_mutation(name, args, exe)

    if t == 'Repository':
        repo = d
        if name == 'id':
            return _gid('Repository', repo.id)
        if name == 'databaseId':
            return repo.id
        if name == 'name':
            return repo.name
        if name == 'nameWithOwner':
            return repo.full_name
        if name == 'owner':
            return _user_node(VIEWER)
        if name == 'description':
            return repo.description
        if name == 'isPrivate':
            return repo.private
        if name == 'defaultBranchRef':
            if repo.default_branch in repo.branches:
                return GQLNode('Ref', repo.default_branch)
            return None
        if name == 'viewerPermission':
            return 'ADMIN'
        if name == 'hasIssuesEnabled':
            return True
        if name == 'createdAt':
            return STORE.clock.iso(repo.created_seq)
        if name == 'url':
            return 'https://github.com/' + repo.full_name
        if name == 'issue':
            issue = STORE.get_issue(repo, args.get('number'))
            return _issue_node(repo, issue) if issue is not None else None
        if name == 'issues':
            return _issues_connection(repo, args)
        if name == 'pullRequests':
            return _pulls_connection(repo, args)
        if name == 'pullRequest':
            pull = STORE.get_pull(repo, args.get('number'))
            return _pull_node(repo, pull) if pull is not None else None

    if t == 'Issue':
        repo, issue = d
        if name == 'id':
            return _gid('Issue', repo.id, issue['number'])
        if name == 'number':
            return issue['number']
        if name == 'title':
            return issue['title']
        if name == 'body':
            return issue['body']
        if name == 'state':
            return _issue_state(issue)
        if name == 'author':
            return _user_node(issue['user'])
        if name == 'createdAt':
            return STORE.clock.iso(issue['created_seq'])
        if name == 'updatedAt':
            return STORE.clock.iso(issue['updated_seq'])
        if name == 'url':
            return ('https://github.com/%s/issues/%d'
                    % (repo.full_name, issue['number']))
        if name == 'labels':
            nodes = [GQLNode('Label', lab) for lab in issue['labels']]
            return _connection('Label', nodes, args.get('first'),
                               args.get('after'))
        if name == 'comments':
            nodes = [GQLNode('IssueComment', (repo, issue, c))
                     for c in issue['comments']]
            return _connection('IssueComment', nodes, args.get('first'),
                               args.get('after'))

    if t == 'PullRequest':
        repo, pull = d
        if name == 'id':
            return _gid('PullRequest', repo.id, pull['number'])
        if name == 'number':
            return pull['number']
        if name == 'title':
            return pull['title']
        if name == 'body':
            return pull['body']
        if name == 'state':
            return _pull_state(pull)
        if name == 'merged':
            return bool(pull['merged'])
        if name == 'mergeable':
            return 'MERGEABLE' if pull['mergeable'] else 'CONFLICTING'
        if name == 'headRefName':
            return pull['head']['ref']
        if name == 'baseRefName':
            return pull['base']['ref']
        if name == 'headRef':
            return GQLNode('Ref', pull['head']['ref'])
        if name == 'baseRef':
            return GQLNode('Ref', pull['base']['ref'])
        if name == 'author':
            return _user_node(pull['user'])
        if name == 'createdAt':
            return STORE.clock.iso(pull['created_seq'])
        if name == 'updatedAt':
            return STORE.clock.iso(pull['updated_seq'])
        if name == 'url':
            return ('https://github.com/%s/pull/%d'
                    % (repo.full_name, pull['number']))
        if name == 'files':
            return _files_connection(repo, pull, args)

    if t == 'User':
        if name == 'id':
            return d['id']
        if name == 'login':
            return d['login']
        if name == 'name':
            return d['name']
        if name == 'email':
            return d['email']

    if t == 'Ref':
        if name == 'name':
            return d

    if t == 'PullRequestChangedFile':
        if name == 'path':
            return d

    if t == 'Label':
        if name == 'name':
            return d['name']
        if name == 'color':
            return d['color']
        if name == 'description':
            return d['description']

    if t == 'IssueComment':
        repo, issue, comment = d
        if name == 'id':
            return _gid('IssueComment', repo.id, comment['id'])
        if name == 'body':
            return comment['body']
        if name == 'author':
            return _user_node(comment['user'])
        if name == 'createdAt':
            return STORE.clock.iso(comment['created_seq'])
        if name == 'updatedAt':
            return STORE.clock.iso(comment['updated_seq'])

    if t.endswith('Connection'):
        if name == 'nodes':
            return d['items']
        if name == 'edges':
            kind = t[:-len('Connection')]
            return [GQLNode(kind + 'Edge', {'node': item, 'cursor': cur})
                    for item, cur in zip(d['items'], d['cursors'])]
        if name == 'pageInfo':
            return GQLNode('PageInfo', {
                'has_next': d['has_next'], 'end_cursor': d['end_cursor'],
                'has_prev': False,
                'start_cursor': d['cursors'][0] if d['cursors'] else None,
            })
        if name == 'totalCount':
            return d['total']

    if t.endswith('Edge'):
        if name == 'node':
            return d['node']
        if name == 'cursor':
            return d['cursor']

    if t == 'PageInfo':
        if name == 'hasNextPage':
            return d['has_next']
        if name == 'endCursor':
            return d['end_cursor']
        if name == 'hasPreviousPage':
            return d['has_prev']
        if name == 'startCursor':
            return d['start_cursor']

    if t.endswith('Payload'):
        return d.get(name)

    if t == '__Type':
        if name == 'name':
            return d
        if name == 'kind':
            if d in ENUMS:
                return 'ENUM'
            if d in INPUT_TYPES:
                return 'INPUT_OBJECT'
            return 'OBJECT'
        if name == 'description':
            return None
        if name == 'fields':
            if d in TYPE_FIELDS:
                return [GQLNode('__Field', f) for f in TYPE_FIELDS[d]]
            return None
        if name == 'enumValues':
            if d in ENUMS:
                return [GQLNode('__EnumValue', v) for v in ENUMS[d]]
            return None

    if t == '__Schema':
        if name == 'queryType':
            return GQLNode('__Type', 'Query')
        if name == 'mutationType':
            return GQLNode('__Type', 'Mutation')
        if name == 'subscriptionType':
            return None
        if name == 'types':
            names = sorted(set(TYPE_FIELDS) | set(ENUMS) | set(INPUT_TYPES))
            return [GQLNode('__Type', n) for n in names]

    if t == '__Field':
        if name == 'name':
            return d
        if name == 'description':
            return None
        if name == 'args':
            return []
        if name == 'type':
            return None

    if t == '__EnumValue':
        if name == 'name':
            return d
        if name == 'description':
            return None

    if t == '__InputValue':
        if name == 'name':
            return d
        if name == 'description':
            return None

    return None


def _resolve_mutation(name, args, exe):
    inp = args.get('input') or {}
    if not isinstance(inp, dict):
        inp = {}
    if name == 'createIssue':
        repo = _repo_by_gid(inp.get('repositoryId'))
        if repo is None:
            exe.errors.append({'message': 'repository not found'})
            return GQLNode('CreateIssuePayload', {'issue': None})
        issue = STORE.create_issue(repo, inp.get('title') or '',
                                   inp.get('body') or '')
        return GQLNode('CreateIssuePayload',
                       {'issue': _issue_node(repo, issue)})
    if name == 'closeIssue':
        loc = _issue_by_gid(inp.get('issueId'))
        if loc is None:
            exe.errors.append({'message': 'issue not found'})
            return GQLNode('CloseIssuePayload', {'issue': None})
        repo, issue = loc
        seq = STORE.clock.tick()
        issue['state'] = 'closed'
        issue['closed_seq'] = seq
        issue['updated_seq'] = seq
        return GQLNode('CloseIssuePayload',
                       {'issue': _issue_node(repo, issue)})
    if name == 'createPullRequest':
        repo = _repo_by_gid(inp.get('repositoryId'))
        if repo is None:
            exe.errors.append({'message': 'repository not found'})
            return GQLNode('CreatePullRequestPayload', {'pullRequest': None})
        pull = STORE.create_pull(repo, inp.get('title') or '',
                                 inp.get('body') or '',
                                 inp.get('headRefName') or '',
                                 inp.get('baseRefName')
                                 or repo.default_branch)
        return GQLNode('CreatePullRequestPayload',
                       {'pullRequest': _pull_node(repo, pull)})
    if name == 'mergePullRequest':
        loc = _pull_by_gid(inp.get('pullRequestId'))
        if loc is None:
            exe.errors.append({'message': 'pull request not found'})
            return GQLNode('MergePullRequestPayload', {'pullRequest': None})
        repo, pull = loc
        STORE.merge_pull(repo, pull)
        return GQLNode('MergePullRequestPayload',
                       {'pullRequest': _pull_node(repo, pull)})
    if name == 'addComment':
        loc = _issue_by_gid(inp.get('subjectId'))
        if loc is None:
            exe.errors.append({'message': 'subject not found'})
            return GQLNode('AddCommentPayload', {'commentEdge': None})
        repo, issue = loc
        comment = STORE.add_comment(repo, issue, inp.get('body') or '')
        cnode = GQLNode('IssueComment', (repo, issue, comment))
        edge = GQLNode('IssueCommentEdge',
                       {'node': cnode, 'cursor': None})
        return GQLNode('AddCommentPayload', {'commentEdge': edge})
    return None


# --------------------------------------------------------------------------
# Execution.
# --------------------------------------------------------------------------


class _Executor:
    def __init__(self, variables, fragments):
        self.vars = variables or {}
        self.fragments = fragments or {}
        self.errors = []

    def _resolve_value(self, vnode, var_defs):
        kind = vnode[0]
        if kind == 'var':
            name = vnode[1]
            if name in self.vars:
                return self.vars[name]
            return var_defs.get(name)
        if kind == 'list':
            return [self._resolve_value(v, var_defs) for v in vnode[1]]
        if kind == 'object':
            return {k: self._resolve_value(v, var_defs)
                    for k, v in vnode[1].items()}
        return vnode[1]

    def _directives_include(self, directives, var_defs):
        """@skip(if:) drops a selection; @include(if:) keeps it only when
        true. `if:` may be a variable, resolved against the operation."""
        for name, args in directives:
            if name == 'skip':
                if self._resolve_value(args.get('if', ('bool', False)),
                                       var_defs) is True:
                    return False
            elif name == 'include':
                if self._resolve_value(args.get('if', ('bool', True)),
                                       var_defs) is not True:
                    return False
        return True

    def _execute_selections(self, node, selections, var_defs):
        out = {}
        for sel in selections:
            if sel[0] == 'field':
                _, alias, name, arg_nodes, sub, directives = sel
                if not self._directives_include(directives, var_defs):
                    continue
                out[alias or name] = self._execute_field(
                    node, name, arg_nodes, sub, var_defs)
            elif sel[0] == 'spread':
                _, fname, directives = sel
                if not self._directives_include(directives, var_defs):
                    continue
                frag = self.fragments.get(fname)
                if frag is None:
                    self.errors.append(
                        {'message': 'Unknown fragment "%s".' % fname})
                    continue
                cond, fsel = frag
                if cond is None or node.type == cond:
                    out.update(self._execute_selections(node, fsel, var_defs))
            elif sel[0] == 'inline':
                _, cond, sub, directives = sel
                if not self._directives_include(directives, var_defs):
                    continue
                if cond is None or node.type == cond:
                    out.update(self._execute_selections(node, sub, var_defs))
        return out

    def _execute_field(self, node, name, arg_nodes, sub, var_defs):
        if name == '__typename':
            return node.type
        if name not in TYPE_FIELDS.get(node.type, []):
            self.errors.append(
                {'message': 'Cannot query field "%s" on type "%s".'
                 % (name, node.type)})
            return None
        args = {k: self._resolve_value(v, var_defs)
                for k, v in arg_nodes.items()}
        value = _resolve_field(node, name, args, self)
        if sub is None:
            return value
        if value is None:
            return None
        if isinstance(value, GQLNode):
            return self._execute_selections(value, sub, var_defs)
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, GQLNode):
                    out.append(self._execute_selections(item, sub, var_defs))
                else:
                    out.append(item)
            return out
        return value


def _execute_operation(op):
    """One {query, variables, operationName} object -> {data, errors?}."""
    if not isinstance(op, dict):
        return {'data': None,
                'errors': [{'message': 'Each operation must be an object.'}]}
    query = op.get('query')
    if not isinstance(query, str):
        return {'data': None,
                'errors': [{'message': 'No query provided.'}]}
    variables = op.get('variables') or {}
    if not isinstance(variables, dict):
        variables = {}
    try:
        ops, frags = parse_document(query)
    except _ParseError as exc:
        return {'data': None, 'errors': [{'message': str(exc)}]}
    if not ops:
        return {'data': None,
                'errors': [{'message': 'No operation provided.'}]}
    operation_name = op.get('operationName')
    chosen = None
    if operation_name:
        for candidate in ops:
            if candidate['name'] == operation_name:
                chosen = candidate
                break
        if chosen is None:
            return {'data': None, 'errors': [
                {'message': 'Unknown operation named "%s".'
                 % operation_name}]}
    elif len(ops) == 1:
        chosen = ops[0]
    else:
        return {'data': None, 'errors': [
            {'message': 'Multiple operations require an operationName.'}]}
    if chosen['kind'] == 'subscription':
        return {'data': None,
                'errors': [{'message': 'Subscriptions are not supported.'}]}
    exe = _Executor(variables, frags)
    root = GQLNode('Mutation' if chosen['kind'] == 'mutation' else 'Query',
                   None)
    data = exe._execute_selections(root, chosen['selections'],
                                   chosen['var_defs'])
    result = {'data': data}
    if exe.errors:
        result['errors'] = exe.errors
    return result


# --------------------------------------------------------------------------
# HTTP entry point: POST /api/graphql (server.py routes here).
# --------------------------------------------------------------------------


def handle(req, handler):
    req.require_auth()  # 401 unless the bearer token is the shop token
    body = req.parse_json()  # ApiError(400) on malformed JSON
    if isinstance(body, list):
        send_json(handler, 200, [_execute_operation(op) for op in body])
        return
    if isinstance(body, dict):
        send_json(handler, 200, _execute_operation(body))
        return
    send_json(handler, 200, {
        'data': None,
        'errors': [{'message':
                    'Request body must be a JSON object or array of '
                    'operations.'}],
    })
