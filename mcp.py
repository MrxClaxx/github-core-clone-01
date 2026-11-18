"""MCP surface for the github-core-clone-01 forge.

JSON-RPC 2.0 over Streamable HTTP at POST /mcp, speaking both protocol
eras the conformance suite accepts:

  modern 2026-07-28: `server/discover` answers identity, capabilities and
          supportedVersions; every request declares its revision in _meta.
  legacy 2025-11-25 and earlier: `initialize` + `notifications/initialized`,
          with an optional Mcp-Session-Id.

Tools act on the same shared STORE as REST, so a repository or issue
created over MCP reads back over /api/v3 as the same object.

Determinism: no wall clock, no randomness in any response. The tools/list
answer is built once at import and returned unchanged on every call.
"""

import json

from server import ApiError, send_json, send_text
from store import STORE, VIEWER_LOGIN

SERVER_NAME = 'github-core-clone-01'
SERVER_VERSION = '1.0.0'

PREFERRED_REVISION = '2026-07-28'
SUPPORTED_VERSIONS = [
    '2026-07-28',
    '2025-11-25',
    '2025-06-18',
    '2025-03-26',
    '2024-11-05',
]
LEGACY_REVISIONS = SUPPORTED_VERSIONS[1:]

# ---------------------------------------------------------------- tools

_TOOLS = [
    {
        'name': 'create_repo',
        'description': 'Create a new repository owned by the viewer.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string', 'description': 'Name of the repository.'},
                'description': {'type': 'string', 'description': 'Short description of the repository.'},
                'private': {'type': 'boolean', 'description': 'Whether the repository is private.'},
                'owner': {'type': 'string', 'description': 'Owner login; defaults to the viewer.'},
            },
            'required': ['name'],
        },
    },
    {
        'name': 'get_repo',
        'description': 'Get a repository by owner and name.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'list_repos',
        'description': 'List the repositories owned by the viewer.',
        'inputSchema': {
            'type': 'object',
            'properties': {},
            'required': [],
        },
    },
    {
        'name': 'create_issue',
        'description': 'Create an issue in a repository.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'title': {'type': 'string', 'description': 'Issue title.'},
                'body': {'type': 'string', 'description': 'Issue body.'},
                'labels': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Label names; each must already exist.'},
            },
            'required': ['owner', 'repo', 'title'],
        },
    },
    {
        'name': 'get_issue',
        'description': 'Get one issue by owner, repository and issue number.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'issue_number': {'type': 'integer', 'description': 'Issue number.'},
                'number': {'type': 'integer', 'description': 'Issue number (alias).'},
            },
            'required': ['owner', 'repo', 'issue_number'],
        },
    },
    {
        'name': 'list_issues',
        'description': 'List issues in a repository, optionally filtered by state.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'state': {'type': 'string', 'description': 'Issue state: open, closed or all.'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'create_pull_request',
        'description': 'Open a pull request from a head branch into a base branch.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'title': {'type': 'string', 'description': 'Pull request title.'},
                'body': {'type': 'string', 'description': 'Pull request body.'},
                'head': {'type': 'string', 'description': 'Head branch name.'},
                'base': {'type': 'string', 'description': 'Base branch name.'},
            },
            'required': ['owner', 'repo', 'title', 'head', 'base'],
        },
    },
    {
        'name': 'get_pull_request',
        'description': 'Get one pull request by owner, repository and pull request number.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'pull_number': {'type': 'integer', 'description': 'Pull request number.'},
                'number': {'type': 'integer', 'description': 'Pull request number (alias).'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
    {
        'name': 'list_pull_requests',
        'description': 'List pull requests in a repository, optionally filtered by state.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'state': {'type': 'string', 'description': 'Pull request state: open, closed or all.'},
            },
            'required': ['owner', 'repo'],
        },
    },
    {
        'name': 'merge_pull_request',
        'description': 'Merge an open pull request into its base branch.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'owner': {'type': 'string', 'description': 'Owner login.'},
                'repo': {'type': 'string', 'description': 'Repository name.'},
                'full_name': {'type': 'string', 'description': 'Full name "owner/repo".'},
                'pull_number': {'type': 'integer', 'description': 'Pull request number.'},
                'number': {'type': 'integer', 'description': 'Pull request number (alias).'},
            },
            'required': ['owner', 'repo', 'pull_number'],
        },
    },
]

# The tools/list answer: built once, returned unchanged on every call so
# two calls answer byte for byte the same list.
TOOLS = _TOOLS


def _tool_by_name(name):
    """Resolve a tool by exact name, else by normalized suffix match
    (a server prefix such as github__create_issue), case-insensitive."""
    if not isinstance(name, str) or not name:
        return None
    for tool in TOOLS:
        if tool['name'] == name:
            return tool
    normalized = ''.join(ch for ch in name.lower() if ch.isalnum())
    for tool in TOOLS:
        candidate = ''.join(ch for ch in tool['name'].lower() if ch.isalnum())
        if normalized.endswith(candidate):
            return tool
    return None


# ---------------------------------------------------------------- serializers

def _iso(seq):
    if seq is None:
        return None
    return STORE.clock.iso(seq)


def _repo_public(repo):
    return {
        'id': repo.id,
        'name': repo.name,
        'full_name': repo.full_name,
        'owner': repo.owner,
        'description': repo.description,
        'private': repo.private,
        'default_branch': repo.default_branch,
        'fork': repo.fork,
        'created_at': _iso(repo.created_seq),
    }


def _comment_public(comment):
    return {
        'id': comment['id'],
        'body': comment['body'],
        'user': comment['user'],
        'created_at': _iso(comment['created_seq']),
        'updated_at': _iso(comment['updated_seq']),
    }


def _issue_public(issue):
    return {
        'id': issue['id'],
        'number': issue['number'],
        'title': issue['title'],
        'body': issue['body'],
        'state': issue['state'],
        'labels': issue['labels'],
        'user': issue['user'],
        'assignees': issue['assignees'],
        'milestone': issue['milestone'],
        'comments': [_comment_public(c) for c in issue['comments']],
        'created_at': _iso(issue['created_seq']),
        'updated_at': _iso(issue['updated_seq']),
        'closed_at': _iso(issue['closed_seq']),
    }


def _pull_public(pull):
    return {
        'id': pull['id'],
        'number': pull['number'],
        'title': pull['title'],
        'body': pull['body'],
        'state': pull['state'],
        'head': pull['head'],
        'base': pull['base'],
        'merged': pull['merged'],
        'merge_commit_sha': pull['merge_commit_sha'],
        'mergeable': pull['mergeable'],
        'user': pull['user'],
        'created_at': _iso(pull['created_seq']),
        'updated_at': _iso(pull['updated_seq']),
        'closed_at': _iso(pull['closed_seq']),
        'merged_at': _iso(pull['merged_seq']),
    }


# ---------------------------------------------------------------- argument helpers

def _owner_repo(args):
    """(owner, repo) out of a tool's arguments, honouring full_name."""
    owner = args.get('owner') or args.get('owner_login') or None
    repo = (args.get('repo') or args.get('repository')
            or args.get('repository_name') or args.get('repo_name') or None)
    full_name = args.get('full_name') or args.get('repo_full_name') or None
    if full_name and isinstance(full_name, str) and '/' in full_name:
        named_owner, _, named_repo = full_name.partition('/')
        if not owner:
            owner = named_owner
        if not repo:
            repo = named_repo
    if not owner:
        owner = VIEWER_LOGIN
    return owner, repo


def _number(args, *keys):
    for key in keys:
        value = args.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def _resolve_repo(args):
    """(repo, error-message): the store repo for owner/repo, or None + why."""
    owner, name = _owner_repo(args)
    if not name:
        return None, 'missing required argument: repo'
    repo = STORE.get_repo(owner, name)
    if repo is None:
        return None, 'Repository not found: %s/%s' % (owner, name)
    return repo, None


def _missing(args, *keys):
    return [key for key in keys
            if args.get(key) is None or args.get(key) == '']


# ---------------------------------------------------------------- tool implementations

def _tool_result(obj):
    text = json.dumps(obj, separators=(',', ':'), ensure_ascii=False)
    return {
        'content': [{'type': 'text', 'text': text}],
        'structuredContent': obj,
        'isError': False,
    }


def _tool_error(message):
    return {
        'content': [{'type': 'text', 'text': message}],
        'isError': True,
    }


def _create_repo(args):
    missing = _missing(args, 'name')
    if missing:
        return _tool_error('missing required argument: %s' % ', '.join(missing))
    name = args['name']
    if not isinstance(name, str) or not name.strip():
        return _tool_error('missing required argument: name')
    owner = args.get('owner') or args.get('owner_login') or VIEWER_LOGIN
    repo = STORE.create_repo(
        name.strip(),
        args.get('description') or '',
        bool(args.get('private', False)),
        'main',
        owner=owner,
    )
    if repo is None:
        return _tool_error('Repository already exists: %s/%s' % (owner, name))
    return _tool_result(_repo_public(repo))


def _get_repo(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    return _tool_result(_repo_public(repo))


def _list_repos(args):
    return _tool_result([_repo_public(r) for r in STORE.list_viewer_repos()])


def _create_issue(args):
    missing = _missing(args, 'title')
    if missing:
        return _tool_error('missing required argument: %s' % ', '.join(missing))
    owner, name = _owner_repo(args)
    if not name:
        return _tool_error('missing required argument: repo')
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    labels = args.get('labels') or []
    if isinstance(labels, str):
        labels = [labels]
    try:
        issue = STORE.create_issue(repo, args['title'],
                                   args.get('body') or '', labels)
    except ValueError as exc:
        return _tool_error('Unknown label: %s' % exc)
    return _tool_result(_issue_public(issue))


def _get_issue(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    number = _number(args, 'issue_number', 'number')
    if number is None:
        return _tool_error('missing required argument: issue_number')
    issue = STORE.get_issue(repo, number)
    if issue is None:
        return _tool_error('Issue not found: %s/%s#%d'
                           % (repo.owner, repo.name, number))
    return _tool_result(_issue_public(issue))


def _list_issues(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    state = args.get('state') or 'all'
    issues = repo.issues
    if state == 'open':
        issues = [i for i in issues if i['state'] == 'open']
    elif state == 'closed':
        issues = [i for i in issues if i['state'] == 'closed']
    return _tool_result([_issue_public(i) for i in issues])


def _create_pull_request(args):
    missing = _missing(args, 'title', 'head', 'base')
    if missing:
        return _tool_error('missing required argument: %s' % ', '.join(missing))
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    pull = STORE.create_pull(repo, args['title'], args.get('body') or '',
                             args['head'], args['base'])
    return _tool_result(_pull_public(pull))


def _get_pull_request(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    number = _number(args, 'pull_number', 'number')
    if number is None:
        return _tool_error('missing required argument: pull_number')
    pull = STORE.get_pull(repo, number)
    if pull is None:
        return _tool_error('Pull request not found: %s/%s#%d'
                           % (repo.owner, repo.name, number))
    return _tool_result(_pull_public(pull))


def _list_pull_requests(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    state = args.get('state') or 'all'
    pulls = repo.pulls
    if state == 'open':
        pulls = [p for p in pulls if p['state'] == 'open']
    elif state == 'closed':
        pulls = [p for p in pulls if p['state'] == 'closed']
    return _tool_result([_pull_public(p) for p in pulls])


def _merge_pull_request(args):
    repo, err = _resolve_repo(args)
    if err is not None:
        return _tool_error(err)
    number = _number(args, 'pull_number', 'number')
    if number is None:
        return _tool_error('missing required argument: pull_number')
    pull = STORE.get_pull(repo, number)
    if pull is None:
        return _tool_error('Pull request not found: %s/%s#%d'
                           % (repo.owner, repo.name, number))
    if pull['state'] != 'open':
        return _tool_error('Pull request #%d is not open' % number)
    sha = STORE.merge_pull(repo, pull)
    if sha is None:
        return _tool_error('Pull request #%d is not mergeable' % number)
    return _tool_result(_pull_public(pull))


_TOOL_IMPL = {
    'create_repo': _create_repo,
    'get_repo': _get_repo,
    'list_repos': _list_repos,
    'create_issue': _create_issue,
    'get_issue': _get_issue,
    'list_issues': _list_issues,
    'create_pull_request': _create_pull_request,
    'get_pull_request': _get_pull_request,
    'list_pull_requests': _list_pull_requests,
    'merge_pull_request': _merge_pull_request,
}


def _call_tool(name, arguments):
    """Run one tools/call. Returns (result, rpc_error); exactly one is set."""
    tool = _tool_by_name(name)
    if tool is None:
        return None, {'code': -32601, 'message': 'Unknown tool: %s' % name}
    if not isinstance(arguments, dict):
        arguments = {}
    try:
        result = _TOOL_IMPL[tool['name']](arguments)
    except Exception as exc:  # a tool failure is isError, never HTTP 500
        return _tool_error('Tool %s failed: %s' % (tool['name'], exc)), None
    return result, None


# ---------------------------------------------------------------- JSON-RPC plumbing

def _is_revision(value):
    if not isinstance(value, str):
        return False
    parts = value.split('-')
    return (len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2
            and len(parts[2]) == 2
            and all(part.isdigit() for part in parts))


def _server_info():
    return {'name': SERVER_NAME, 'version': SERVER_VERSION}


def _result_envelope(req_id, result):
    return {'jsonrpc': '2.0', 'id': req_id, 'result': result}


def _error_envelope(req_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return {'jsonrpc': '2.0', 'id': req_id, 'error': error}


def _discover_result():
    info = _server_info()
    return {
        'protocolVersion': PREFERRED_REVISION,
        'supportedVersions': list(SUPPORTED_VERSIONS),
        'capabilities': {'tools': {}},
        'serverInfo': info,
        'instructions': 'GitHub-core clone: repositories, issues and pull requests over MCP tools.',
        '_meta': {'io.modelcontextprotocol/serverInfo': info},
    }


def _handle_rpc(data, handler):
    """Answer one parsed JSON-RPC request object. Always sends a reply."""
    req_id = data.get('id')
    method = data.get('method')
    params = data.get('params')
    if not isinstance(params, dict):
        params = {}

    if method == 'server/discover':
        send_json(handler, 200, _result_envelope(req_id, _discover_result()))
        return

    if method == 'initialize':
        offered = params.get('protocolVersion')
        if _is_revision(offered) and offered in SUPPORTED_VERSIONS:
            negotiated = offered
        elif _is_revision(offered):
            negotiated = offered
        else:
            negotiated = LEGACY_REVISIONS[0]
        send_json(handler, 200, _result_envelope(req_id, {
            'protocolVersion': negotiated,
            'capabilities': {'tools': {}},
            'serverInfo': _server_info(),
        }))
        return

    if isinstance(method, str) and method.startswith('notifications/'):
        send_json(handler, 202, {})
        return

    if method == 'ping':
        send_json(handler, 200, _result_envelope(req_id, {}))
        return

    if method == 'tools/list':
        send_json(handler, 200, _result_envelope(req_id, {'tools': TOOLS}))
        return

    if method == 'tools/call':
        name = params.get('name')
        arguments = params.get('arguments') or {}
        if isinstance(arguments, str):
            # Some clients send arguments as a JSON string; accept that too.
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = {}
        result, rpc_error = _call_tool(name, arguments)
        if rpc_error is not None:
            send_json(handler, 200,
                      _error_envelope(req_id, rpc_error['code'],
                                      rpc_error['message']))
            return
        send_json(handler, 200, _result_envelope(req_id, result))
        return

    send_json(handler, 200, _error_envelope(
        req_id, -32601, 'Method not found: %s' % method))


def handle(req, handler):
    """Route one /mcp request. Never raises out of here."""
    try:
        if req.method == 'DELETE':
            # A legacy session goodbye: nothing is held, acknowledge it.
            send_text(handler, 204, b'', 'application/json')
            return
        if req.method != 'POST':
            send_json(handler, 405, {'message': 'Method Not Allowed'})
            return
        try:
            data = req.parse_json()
        except ApiError:
            # A body that is not JSON: HTTP 400 with JSON-RPC -32700.
            send_json(handler, 400, _error_envelope(
                None, -32700, 'Parse error'))
            return
        if data is None or not isinstance(data, dict):
            send_json(handler, 400, _error_envelope(
                None, -32600, 'Invalid Request'))
            return
        try:
            req.require_auth()
        except ApiError:
            send_json(handler, 401, _error_envelope(
                data.get('id'), -32001, 'Requires authentication'))
            return
        _handle_rpc(data, handler)
    except ApiError as err:
        try:
            send_json(handler, err.status, {'message': err.message})
        except Exception:
            pass
    except Exception as exc:  # never leak a stack or a 500 page shape
        try:
            send_json(handler, 500, {'message': 'Internal Server Error: %s' % exc})
        except Exception:
            pass
