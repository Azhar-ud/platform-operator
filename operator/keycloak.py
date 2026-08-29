# The operator's Keycloak hand: admin API access, and nothing else.
#
# Why the admin API and not the realm file: the realm imports ONCE
# (--import-realm ignores an existing realm and still logs success - polaris
# ADR-0006 documents the trap). Per-application clients are dynamic - they
# exist because a manifest exists - so they are reconciled against the running
# Keycloak the same way namespaces are reconciled against the running cluster.
# Calling the provider's own API directly, with no wrapper service in between,
# is polaris ADR-0007's decision; this module follows it.
#
# Credentials come from the same out-of-band secret everything else uses:
# datum-identity-admin in datum-platform. Nothing here is stored in git, and
# nothing here caches the password - it is read from the cluster on each use,
# so a rotated secret takes effect on the next reconcile.

import base64
import os
from pathlib import Path
from typing import cast

import kubernetes
import requests

# The issuer as browsers see it. Keycloak stamps this hostname into every
# token it signs, so the operator must speak to the same name - a shortcut
# via the in-cluster service DNS would get answers signed for a different
# issuer. On a laptop the name resolves through /etc/hosts.
ISSUER_BASE = os.environ.get("DATUM_IAM_URL", "https://iam.datum.local")
REALM = os.environ.get("DATUM_REALM", "datum")

ADMIN_SECRET_NAME = "datum-identity-admin"
ADMIN_SECRET_NAMESPACE = "datum-platform"

# Local clusters serve mkcert-signed TLS. requests trusts certifi's bundle,
# not the OS store, so the CA is named explicitly. A real install with a
# publicly trusted issuer sets DATUM_CA=system (or nothing to verify against
# the default bundle).
def _ca() -> str | bool:
    ca = os.environ.get("DATUM_CA")
    if ca == "system":
        return True
    if ca:
        return ca
    mkcert = Path.home() / ".local/share/mkcert/rootCA.pem"
    return str(mkcert) if mkcert.is_file() else True


def _admin_credentials() -> tuple[str, str]:
    """The admin user and password, read from the cluster on every call."""
    secret = cast(
        kubernetes.client.V1Secret,
        kubernetes.client.CoreV1Api().read_namespaced_secret(
            ADMIN_SECRET_NAME, ADMIN_SECRET_NAMESPACE
        ),
    )
    data = secret.data or {}
    return (
        base64.b64decode(data["username"]).decode(),
        base64.b64decode(data["password"]).decode(),
    )


def _admin_token() -> str:
    """A short-lived admin token from the master realm.

    admin-cli is Keycloak's built-in public client for exactly this; the
    password grant against it is the documented admin-API entry point. The
    token lives about a minute - long enough for one reconcile, short enough
    that holding it is not worth the code.
    """
    username, password = _admin_credentials()
    resp = requests.post(
        f"{ISSUER_BASE}/realms/master/protocol/openid-connect/token",
        data={
            "grant_type": "password",
            "client_id": "admin-cli",
            "username": username,
            "password": password,
        },
        verify=_ca(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _admin_get(path: str, token: str, **params):
    resp = requests.get(
        f"{ISSUER_BASE}/admin/realms/{REALM}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        verify=_ca(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def list_clients() -> list[str]:
    """Every clientId in the realm. Step 1's proof that the hand works."""
    token = _admin_token()
    return sorted(c["clientId"] for c in _admin_get("/clients", token))


def _admin_send(method: str, path: str, token: str, payload) -> requests.Response:
    resp = requests.request(
        method,
        f"{ISSUER_BASE}/admin/realms/{REALM}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=payload,
        verify=_ca(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp


def _client_representation(client_id: str, redirect_uri: str) -> dict:
    """What the gateway's client IS, stated once.

    The same posture polaris chose for my-apps (ADR-0008): confidential -
    the proxy is a server and can hold a secret, which proves WHICH app is
    asking - with PKCE on top, which protects the code in flight. Every flow
    the proxy does not use is off; an ability nobody exercises is only an
    attack surface.

    The audience mapper is the non-obvious part: Keycloak does not put a
    client's own id into the token's aud claim by default, and the proxy
    (rightly) refuses a token that does not name it. my-apps carries the
    same mapper for the same reason.
    """
    return {
        "clientId": client_id,
        "name": f"login gateway for {client_id.removeprefix('gateway-')}",
        "publicClient": False,
        "standardFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "implicitFlowEnabled": False,
        "serviceAccountsEnabled": False,
        "redirectUris": [redirect_uri],
        "attributes": {"pkce.code.challenge.method": "S256"},
        "protocolMappers": [
            {
                "name": "audience",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "config": {
                    "included.client.audience": client_id,
                    "access.token.claim": "true",
                },
            }
        ],
    }


def list_platform_users() -> dict[str, list[str]]:
    """Every realm user and their effective platform:* roles.

    This is the push reconciler's DECISION SOURCE, and a stand-in on
    purpose: the permission graph that will someday decide who may do what
    does not exist yet, while realm roles do - they are ADR-backed, my-apps
    already expands them, and they are exactly what the future graph will
    refine. When the graph arrives, it replaces this one function and
    nothing downstream moves.

    The composite endpoint is the important choice: dev-analyst is ASSIGNED
    only platform:analyst, but the realm's roles are composites, so the
    role expands to viewer too - the same expansion Keycloak performs into
    tokens. Reading direct assignments would disagree with what every token
    says.
    """
    token = _admin_token()
    users = {}
    for user in _admin_get("/users", token, max=1000):
        roles = _admin_get(f"/users/{user['id']}/role-mappings/realm/composite", token)
        platform_roles = sorted(
            r["name"] for r in roles if r["name"].startswith("platform:")
        )
        if platform_roles:
            users[user["username"]] = platform_roles
    return users


def ensure_client(client_id: str, redirect_uri: str) -> str:
    """Get-or-create the client; return its secret. Safe to call forever.

    The same discipline as every Kubernetes object the operator touches:
    state what should exist, converge reality toward it, change nothing that
    already matches. Keycloak has no server-side apply, so the converging is
    spelled out - absent: create; present with a stale redirect: update;
    present and correct: read and leave alone. The secret is generated by
    Keycloak on creation and only ever READ here - rerunning never rotates
    it, because a rotation restarts every proxy that holds the old one.
    """
    token = _admin_token()
    found = _admin_get("/clients", token, clientId=client_id)

    if not found:
        _admin_send("POST", "/clients", token, _client_representation(client_id, redirect_uri))
        found = _admin_get("/clients", token, clientId=client_id)

    client = found[0]
    if client.get("redirectUris") != [redirect_uri]:
        # surface.host changed in the manifest; the registered callback
        # follows it. Everything else is left as Keycloak has it - an admin
        # may have tightened settings, and a reconcile must not undo that.
        _admin_send(
            "PUT", f"/clients/{client['id']}", token,
            {**client, "redirectUris": [redirect_uri]},
        )

    secret = _admin_get(f"/clients/{client['id']}/client-secret", token)
    return secret["value"]


if __name__ == "__main__":
    # Smoke tests, run from the repo root against the current kubeconfig:
    #   .venv/bin/python operator/keycloak.py                    # list clients
    #   .venv/bin/python operator/keycloak.py ensure <id> <uri>  # get-or-create
    import sys

    kubernetes.config.load_kube_config()
    if len(sys.argv) == 4 and sys.argv[1] == "ensure":
        value = ensure_client(sys.argv[2], sys.argv[3])
        print(f"client {sys.argv[2]} ensured; secret ends ...{value[-4:]}")
    elif len(sys.argv) == 2 and sys.argv[1] == "users":
        for username, roles in sorted(list_platform_users().items()):
            print(f"{username}: {', '.join(roles)}")
    else:
        for client_id in list_clients():
            print(client_id)
