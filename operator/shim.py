# The identity shim: the gateway<->warehouse handshake.
#
# It sits INSIDE the gateway pod, bound to 127.0.0.1, between oauth2-proxy
# and the application. The proxy has already verified the person and stamps
# X-Forwarded-User from the session; the shim turns that trusted stamp into
# the application's own credentials, read from the ledger Secret the push
# reconciler maintains. Play's boxes stay empty; queries run as you.
#
# Believing a header is only safe because of WHERE this listens: loopback,
# reachable solely from the proxy sharing the pod. That pairing - trusted
# header + localhost bind - is the entire design. Never bind this to
# anything but 127.0.0.1.
#
# Per request, four moves:
#   1. trust X-Forwarded-User (the proxy overwrites it from the session)
#   2. strip everything auth-shaped the client sent - X-ClickHouse-*
#      headers AND user=/password= query params (Play sends user=default
#      even with empty boxes; unstripped, the upstream sees two conflicting
#      logins). The session decides who you are; typed credentials are
#      ignored at this door.
#   3. look the person up in the ledger (a directory: one file per user,
#      mounted from the Secret; kubelet refreshes it, so rotation needs no
#      restart). In the ledger: inject their credentials. Ledger present
#      but no entry: 403 with a plain sentence - never forward as nobody.
#      No ledger at all: pass through untouched (a gateway application
#      with no push reconciler does its own thing).
#   4. forward, and hand the answer back.
#
# Deliberately boring: stdlib only (it runs from a ConfigMap in a stock
# python image - no registry exists here), no state, no threads beyond the
# server's own, responses buffered whole (a prototype trade: fine for a
# person at a UI, revisit for bulk exports).
#
# Offline proof of the pure parts:  python shim.py test

import http.client
import http.server
import os
import sys
import urllib.parse

BIND = "127.0.0.1"
PORT = int(os.environ.get("SHIM_PORT", "4181"))
UPSTREAM = os.environ.get("SHIM_UPSTREAM", "http://clickhouse:8123")
LEDGER_DIR = os.environ.get("SHIM_LEDGER_DIR", "/etc/warehouse-users")

# Who the session says you are. oauth2-proxy sends the OIDC `sub` claim in
# X-Forwarded-User - Keycloak's opaque UUID, stable but not a name - and the
# human username in X-Forwarded-Preferred-Username. The ledger is keyed by
# username (the reconciler creates warehouse users from Keycloak usernames),
# so preferred-username is the identity and the UUID is the fallback.
# Learned live: the first browser test 403'd on a UUID.
IDENTITY_HEADERS = ("x-forwarded-preferred-username", "x-forwarded-user")

# Headers that must never travel through: anything that authenticates (we
# set our own), anything hop-by-hop (each connection negotiates its own).
STRIPPED_HEADERS = {
    "x-clickhouse-user", "x-clickhouse-key", "authorization",
    "x-forwarded-user", "x-forwarded-email", "x-forwarded-preferred-username",
    "host", "connection", "keep-alive", "transfer-encoding", "te",
    "upgrade", "proxy-authorization", "content-length",
}

STRIPPED_PARAMS = {"user", "password"}


def clean_params(query: str) -> str:
    """The query string, minus credential parameters. Order preserved."""
    kept = [
        (k, v)
        for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if k.lower() not in STRIPPED_PARAMS
    ]
    return urllib.parse.urlencode(kept)


def clean_headers(headers) -> dict:
    """The forwardable headers: nothing auth-shaped, nothing hop-by-hop."""
    return {
        k: v for k, v in headers.items() if k.lower() not in STRIPPED_HEADERS
    }


def look_up(ledger_dir: str, username: str | None):
    """('inject', password) | ('forbid', reason) | ('passthrough', None).

    The ledger is the mounted Secret: one file per user. Read on every
    request so a rotated key applies as soon as kubelet refreshes the
    mount. Filenames come from the reconciler, but the username comes from
    a header - never let it walk the filesystem.
    """
    if not os.path.isdir(ledger_dir) or not os.listdir(ledger_dir):
        return ("passthrough", None)
    if not username:
        return ("forbid", "no identity on the request")
    if "/" in username or username.startswith("."):
        return ("forbid", f"refusing suspicious identity {username!r}")
    try:
        with open(os.path.join(ledger_dir, username)) as f:
            return ("inject", f.read())
    except FileNotFoundError:
        return ("forbid", f"no warehouse account for {username!r}")


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _identity(self) -> str | None:
        for header in IDENTITY_HEADERS:
            if value := self.headers.get(header):
                return value
        return None

    def _serve(self):
        user = self._identity()
        action, value = look_up(LEDGER_DIR, user)
        if action == "forbid":
            body = f"{value or 'forbidden'}\n".encode()
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        parsed = urllib.parse.urlsplit(self.path)
        query = clean_params(parsed.query)
        path = parsed.path + ("?" + query if query else "")

        outbound = clean_headers(self.headers)
        if action == "inject":
            assert user is not None and value is not None
            outbound["X-ClickHouse-User"] = user
            outbound["X-ClickHouse-Key"] = value

        length = int(self.headers.get("Content-Length") or 0)
        request_body = self.rfile.read(length) if length else None

        up = urllib.parse.urlsplit(UPSTREAM)
        assert up.hostname, f"SHIM_UPSTREAM has no hostname: {UPSTREAM!r}"
        conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=120)
        try:
            conn.request(self.command, path, body=request_body, headers=outbound)
            resp = conn.getresponse()
            payload = resp.read()
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("connection", "transfer-encoding", "content-length"):
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        finally:
            conn.close()

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_OPTIONS = _serve

    def log_message(self, format, *args):  # noqa: A002 - stdlib's chosen name
        # One line per request, no bodies, no credentials - who and what.
        user = self._identity() or "-"
        sys.stderr.write(f"{user} {format % args}\n")


def _test():
    """The pure parts, proven with no cluster and no server."""
    assert clean_params("user=default&password=&query=SELECT+1") == "query=SELECT+1"
    assert clean_params("query=SELECT+1&default_format=JSON") == "query=SELECT+1&default_format=JSON"
    assert clean_params("USER=x&Password=y") == ""

    cleaned = clean_headers({
        "X-ClickHouse-User": "default", "X-ClickHouse-Key": "hunter2",
        "Authorization": "Basic abc", "X-Forwarded-User": "mallory",
        "Content-Type": "text/plain", "Accept-Encoding": "gzip",
    })
    assert cleaned == {"Content-Type": "text/plain", "Accept-Encoding": "gzip"}, cleaned

    import tempfile
    with tempfile.TemporaryDirectory() as empty:
        assert look_up(empty, "dev-analyst") == ("passthrough", None)
    with tempfile.TemporaryDirectory() as ledger:
        with open(os.path.join(ledger, "dev-analyst"), "w") as f:
            f.write("s3cret")
        assert look_up(ledger, "dev-analyst") == ("inject", "s3cret")
        assert look_up(ledger, "dev-onlooker")[0] == "forbid"
        assert look_up(ledger, None)[0] == "forbid"
        assert look_up(ledger, "../etc/passwd")[0] == "forbid"
        assert look_up(ledger, ".hidden")[0] == "forbid"
    print("shim: all offline checks pass")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "test":
        _test()
    else:
        server = http.server.ThreadingHTTPServer((BIND, PORT), Handler)
        sys.stderr.write(f"shim: {BIND}:{PORT} -> {UPSTREAM}, ledger {LEDGER_DIR}\n")
        server.serve_forever()
