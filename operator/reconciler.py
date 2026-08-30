# The push reconciler's warehouse hand: ClickHouse admin access, as SQL.
#
# reconciler.mode `push` means the platform materialises grants natively -
# DPS P1: "for a self-hosted warehouse we push native GRANTs". This module is
# the ClickHouse side of that promise. It authenticates with the credential
# the chart generated at install (the `clickhouse` Secret in the app
# namespace - nothing in git, nothing invented by us) and speaks plain SQL.
#
# The route is the Kubernetes API server's service proxy. The operator runs
# on a laptop for now, and the only ingress route to ClickHouse is the login
# gateway - session auth, exactly wrong for a robot. The API server can proxy
# HTTP to any Service using the credentials the operator already holds, so no
# new network path and no new secret exist just for this. When the operator
# moves in-cluster, this becomes plain service DNS and nothing else changes.

import base64
import re
import secrets as pysecrets
from typing import cast

import kubernetes
import requests
from kubernetes.client.exceptions import ApiException

FIELD_MANAGER = "platform-operator"


def _admin_password(app_ns: str) -> str:
    """The warehouse admin password, read on every use like every other
    credential the operator touches.

    Two shapes, one per installer kind: the operator-minted Secret
    (<name>-admin, key `password` - source.clickhouse, we made it before the
    database was born) and the Bitnami chart's (<name>, key `admin-password`
    - source.helm, the chart made it and we read it after). The app's name
    is the namespace's, by the operator's own app-<name> convention.
    """
    name = app_ns.removeprefix("app-")
    api = kubernetes.client.CoreV1Api()
    for secret_name, key in ((f"{name}-admin", "password"), (name, "admin-password")):
        try:
            secret = cast(
                kubernetes.client.V1Secret,
                api.read_namespaced_secret(secret_name, app_ns),
            )
        except ApiException as e:
            if e.status != 404:
                raise
            continue
        if key in (secret.data or {}):
            return base64.b64decode((secret.data or {})[key]).decode()
    raise RuntimeError(f"no warehouse admin secret in {app_ns}")


def _proxy_url(app_ns: str, service: str) -> str:
    cfg = kubernetes.client.Configuration.get_default_copy()
    return f"{cfg.host}/api/v1/namespaces/{app_ns}/services/{service}:http/proxy/"


def execute(app_ns: str, service: str, sql: str) -> str:
    """One SQL statement against the warehouse, as the chart's admin user.

    Credentials travel as ClickHouse's own headers, not in the URL - the API
    server logs request paths, and a password in a query string is a password
    in a log.
    """
    cfg = kubernetes.client.Configuration.get_default_copy()
    assert cfg.cert_file and cfg.key_file, "kubeconfig must carry a client cert"
    resp = requests.post(
        _proxy_url(app_ns, service),
        params={"query": sql},
        headers={
            "X-ClickHouse-User": "default",
            "X-ClickHouse-Key": _admin_password(app_ns),
        },
        cert=(cfg.cert_file, cfg.key_file),
        verify=cfg.ssl_ca_cert,
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"clickhouse said {resp.status_code}: {resp.text[:300]}")
    return resp.text.strip()


# ---------------------------------------------------------------- the policy
#
# What a platform role means in the warehouse, stated once. The mapping reads
# the manifest's entitlements - grantableObjects and actions are the
# vocabulary the application declared - and translates our three actions into
# ClickHouse's words. platform:viewer alone earns no warehouse user: seeing a
# tile is not querying data.

ACTION_BY_ROLE = {
    "platform:analyst": "read",
    "platform:engineer": "write",
    "platform:admin": "admin",
}

# ClickHouse's dialect for each declared action, at granularity `object`
# (per-database). admin is deliberately NOT `ALL`: ALL would carry ACCESS
# MANAGEMENT (user management is this operator's job and nobody else's),
# SYSTEM and CLUSTER - platform machinery, not application administration.
# It is the data plane, whole. Conveniently, ALL is also more than the
# chart's admin user may delegate, which is how the first converge run
# taught us this.
SQL_BY_ACTION = {
    "read": "SELECT, SHOW",
    "write": "SELECT, SHOW, INSERT, ALTER, CREATE TABLE, CREATE VIEW, DROP TABLE, TRUNCATE",
    "admin": "SELECT, SHOW, INSERT, ALTER, CREATE, DROP, UNDROP TABLE, TRUNCATE, OPTIMIZE, KILL QUERY, dictGet",
}

# Everything the reconciler creates wears this prefix. It is the ownership
# stamp SQL objects can carry - the operator converges what is datum_'s and
# never touches a DBA's hand-made users or roles.
ROLE_PREFIX = "datum_"


def desired_state(entitlements: dict, platform_users: dict[str, list[str]]) -> dict:
    """(contract, decision source) -> what should exist. Pure on purpose:
    this is the whole policy, testable with two dicts and no cluster.

    Returns {"roles": {role: sql_privileges}, "users": {username: role}}.
    Each user lands in exactly one role - the strongest their platform
    roles earn - so revocation is one REVOKE, not a hunt.
    """
    declared = set(entitlements.get("actions") or [])
    roles = {
        f"{ROLE_PREFIX}{action}": SQL_BY_ACTION[action]
        for action in ("read", "write", "admin")
        if action in declared and action in SQL_BY_ACTION
    }

    strength = ["admin", "write", "read"]  # strongest first
    users = {}
    for username, platform_roles in platform_users.items():
        earned = {ACTION_BY_ROLE[r] for r in platform_roles if r in ACTION_BY_ROLE}
        for action in strength:
            if action in earned and f"{ROLE_PREFIX}{action}" in roles:
                users[username] = f"{ROLE_PREFIX}{action}"
                break
    return {"roles": roles, "users": users}


# --------------------------------------------------------------- the converge

def _ident(name: str) -> str:
    """A quoted SQL identifier, or a refusal. Usernames arrive from Keycloak,
    which we administer - but SQL built from any external string gets
    validated and quoted, no exceptions to think about later."""
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,64}", name):
        raise ValueError(f"refusing to build SQL from identifier: {name!r}")
    return f'"{name}"'


def _ledger_name(app_ns: str) -> str:
    """<app>-platform-users, named by the app, not the Service the SQL rides
    to - the Service's name belongs to whoever installed the database, and
    it changed when the official operator arrived. The gateway mounts the
    ledger by this name; the two must never drift again."""
    return f"{app_ns.removeprefix('app-')}-platform-users"


def _managed_passwords(app_ns: str) -> dict[str, str]:
    """The users this reconciler owns, with their passwords.

    The Secret is the ownership ledger: its keys ARE the set of users the
    operator created. A warehouse user not in this ledger is somebody
    else's and never touched; a ledger entry no longer desired is dropped
    from both the warehouse and the ledger. Passwords are generated once
    and kept - rotation on every pass would break every saved connection.
    """
    try:
        secret = cast(
            kubernetes.client.V1Secret,
            kubernetes.client.CoreV1Api().read_namespaced_secret(
                _ledger_name(app_ns), app_ns
            ),
        )
        return {
            user: base64.b64decode(pw).decode()
            for user, pw in (secret.data or {}).items()
        }
    except ApiException as e:
        if e.status != 404:
            raise
        return {}


def _store_passwords(app_ns: str, passwords: dict[str, str]):
    kubernetes.client.CoreV1Api().patch_namespaced_secret(
        name=_ledger_name(app_ns),
        namespace=app_ns,
        body={
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": _ledger_name(app_ns),
                "namespace": app_ns,
                "labels": {"app.kubernetes.io/managed-by": FIELD_MANAGER},
            },
            "stringData": passwords,
        },
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def _shown_privileges(show_grants: str) -> set[str]:
    """SHOW GRANTS output -> the set of privileges, order and quoting aside.

    ClickHouse renders grants back canonically - its own privilege order,
    backticks around identifiers - so comparing rendered strings to written
    ones re-fixes forever. Sets are what the policy meant anyway.
    """
    granted = set()
    for line in show_grants.splitlines():
        if line.startswith("GRANT ") and " ON " in line:
            privs = line[len("GRANT "):].split(" ON ")[0]
            granted |= {p.strip() for p in privs.split(",")}
    return granted


def _shown_roles(show_grants: str) -> set[str]:
    """SHOW GRANTS output -> the set of roles granted (lines with no ON)."""
    roles = set()
    for line in show_grants.splitlines():
        if line.startswith("GRANT ") and " ON " not in line:
            names = line[len("GRANT "):].split(" TO ")[0]
            roles |= {n.strip().strip("`") for n in names.split(",")}
    return roles


def converge(app_ns: str, service: str, entitlements: dict,
             platform_users: dict[str, list[str]], logger) -> dict:
    """Make the warehouse match the desired state. Safe to run forever.

    Order matters only once: roles before users, because a user is granted
    a role that must exist. Everything else is compare-then-fix.
    """
    desired = desired_state(entitlements, platform_users)
    run = lambda sql: execute(app_ns, service, sql)  # noqa: E731

    # Roles: create, then converge privileges deterministically. REVOKE+GRANT
    # only when the observed grants disagree with the policy.
    for role, privileges in desired["roles"].items():
        run(f"CREATE ROLE IF NOT EXISTS {_ident(role)}")
        expected = {p.strip() for p in privileges.split(",")}
        if _shown_privileges(run(f"SHOW GRANTS FOR {_ident(role)}")) != expected:
            run(f"REVOKE ALL ON *.* FROM {_ident(role)}")
            run(f"GRANT {privileges} ON *.* TO {_ident(role)}")
            logger.info("role %s: granted %s", role, privileges)

    # Stale datum_* roles: entitlements shrank, the role goes. Ours by prefix.
    have_roles = set(run("SHOW ROLES").split())
    for stale in sorted(r for r in have_roles
                        if r.startswith(ROLE_PREFIX) and r not in desired["roles"]):
        run(f"DROP ROLE IF EXISTS {_ident(stale)}")
        logger.info("role %s: dropped, no longer in entitlements", stale)

    # Users: the ledger decides ownership. New user: create with a generated
    # password. Known user: converge role membership only. Gone user: drop.
    ledger = _managed_passwords(app_ns)
    for username, role in sorted(desired["users"].items()):
        if username not in ledger:
            ledger[username] = pysecrets.token_urlsafe(24)
            run(f"CREATE USER IF NOT EXISTS {_ident(username)} "
                f"IDENTIFIED BY '{ledger[username]}'")
            logger.info("user %s: created", username)
        if _shown_roles(run(f"SHOW GRANTS FOR {_ident(username)}")) != {role}:
            for other in desired["roles"]:
                if other != role:
                    run(f"REVOKE {_ident(other)} FROM {_ident(username)}")
            run(f"GRANT {_ident(role)} TO {_ident(username)}")
            run(f"SET DEFAULT ROLE {_ident(role)} TO {_ident(username)}")
            logger.info("user %s -> %s", username, role)

    for gone in sorted(set(ledger) - set(desired["users"])):
        run(f"DROP USER IF EXISTS {_ident(gone)}")
        del ledger[gone]
        logger.info("user %s: dropped, no longer entitled", gone)

    _store_passwords(app_ns, ledger)
    return {"users": len(desired["users"]), "roles": len(desired["roles"])}


if __name__ == "__main__":
    # Smoke tests, run from the repo root against the current kubeconfig:
    #   .venv/bin/python operator/reconciler.py           # warehouse hand
    #   .venv/bin/python operator/reconciler.py mapping   # pure policy, no cluster
    #   .venv/bin/python operator/reconciler.py converge  # the real thing, once
    import sys

    if len(sys.argv) == 2 and sys.argv[1] == "mapping":
        state = desired_state(
            {"grantableObjects": ["database", "table", "view"],
             "actions": ["read", "write", "admin"]},
            {"dev-analyst": ["platform:analyst", "platform:viewer"],
             "dev-engineer": ["platform:admin", "platform:analyst",
                              "platform:engineer", "platform:viewer"],
             "dev-onlooker": ["platform:viewer"]},
        )
        print("roles:", state["roles"])
        print("users:", state["users"])
    elif len(sys.argv) == 2 and sys.argv[1] == "converge":
        import logging

        import keycloak
        import warehouse

        logging.basicConfig(level=logging.INFO, format="%(message)s")
        kubernetes.config.load_kube_config()
        svc = warehouse.service_name("clickhouse")
        entitlements = {"grantableObjects": ["database", "table", "view"],
                        "actions": ["read", "write", "admin"]}
        result = converge("app-clickhouse", svc, entitlements,
                          keycloak.list_platform_users(), logging.getLogger("converge"))
        print("converged:", result)
        print("warehouse users now:",
              execute("app-clickhouse", svc, "SHOW USERS").replace("\n", ", "))
    else:
        import warehouse

        kubernetes.config.load_kube_config()
        svc = warehouse.service_name("clickhouse")
        print("version:     ", execute("app-clickhouse", svc, "SELECT version()"))
        print("connected as:", execute("app-clickhouse", svc, "SELECT currentUser()"))
        print("users:       ", execute("app-clickhouse", svc, "SHOW USERS").replace("\n", ", "))
