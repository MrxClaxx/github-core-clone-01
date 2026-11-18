"""Entry point for the github-core-clone-01 forge (Python 3.13, stdlib only).

Trial mode (MAKEFUN_ENV_URL set): bind an ephemeral port (never 8080),
POST {"port": n} to $MAKEFUN_ENV_URL/register, then poll GET
$MAKEFUN_ENV_URL/done until {"done": true} and exit 0.

Dev mode (no MAKEFUN_ENV_URL, for the kit's local runner): bind
127.0.0.1:$FORGE_PORT (default 18765), print the URL, serve forever.
Set MAKEFUN_SHOP_TOKEN to the same token the runner passes with --token.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

from server import create_server


def _http_json(url, method='GET', body=None, timeout=30):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, json.loads(raw.decode('utf-8')) if raw else {}


def _sleep_retry(attempt):
    time.sleep(min(2.0, 0.25 * (2 ** min(attempt, 3))))


def register(env_url, port):
    """POST the bound port until the env accepts it (200, or 409 when a
    first register already won). 400/405 are contract errors and fatal."""
    for attempt in range(100000):
        try:
            status, _ = _http_json(env_url + '/register', 'POST',
                                   {'port': port})
            if status in (200, 409):
                return
            if status in (400, 405):
                raise SystemExit('register answered HTTP %d' % status)
        except SystemExit:
            raise
        except Exception:
            pass
        _sleep_retry(attempt)


def wait_for_done(env_url):
    """Poll GET /done until it answers {"done": true}; never return early."""
    for attempt in range(100000):
        try:
            status, body = _http_json(env_url + '/done', 'GET')
            if status == 200 and body.get('done') is True:
                return
        except Exception:
            pass
        _sleep_retry(attempt)


def main():
    env_url = os.environ.get('MAKEFUN_ENV_URL', '').rstrip('/')
    if env_url:
        server = create_server('0.0.0.0', 0)  # constructor binds + activates
        port = server.server_address[1]
        print('[entry] listening on port %d' % port, flush=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        register(env_url, port)
        print('[entry] registered; waiting for the env to say done', flush=True)
        wait_for_done(env_url)
        server.shutdown()
        server.server_close()
        print('[entry] done', flush=True)
        return 0
    port = int(os.environ.get('FORGE_PORT', '18765'))
    server = create_server('127.0.0.1', port)
    print('[entry] dev mode: http://127.0.0.1:%d (MAKEFUN_SHOP_TOKEN=%s)'
          % (port, 'set' if os.environ.get('MAKEFUN_SHOP_TOKEN') else 'unset'),
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
