# The clickhouse installer branch: source.clickhouse means the official
# ClickHouse operator runs the database.
#
# The delegation pattern is v4's, with a more knowledgeable cook: for
# source.helm the operator writes a recipe name (HelmChart) and
# helm-controller installs it; here it writes a database description
# (ClickHouseCluster) and ClickHouse Inc's own operator - installed once, as
# platform furniture, next to helm-controller - builds and runs it on
# official images. bitnamilegacy exits the platform with this branch.
#
# Three objects are rendered from four manifest fields, and two of the three
# are things the manifest author deliberately never names:
#
#   ADMIN SECRET, minted before the database is born. The Bitnami chart
#   invented the admin password and we fished it out afterward; here the
#   arrow reverses - we generate <name>-admin once, and the cluster's
#   settings.defaultUserPassword points at it. Generated once and kept,
#   like the gateway's cookie secret: the grants reconciler reads it on
#   every converge.
#
#   KEEPERCLUSTER, the coordination service. The official operator requires
#   keeperClusterRef even at one replica, because it defaults databases to
#   the replicated engine - replication is its normal case. A database needs
#   a consensus quorum the way a gateway needs a cookie secret: platform
#   knowledge, not something the author should have to know. Keeper runs
#   Raft, so its replica count must be odd: 1 under a single database
#   replica, 3 under anything more.
#
#   CLICKHOUSECLUSTER, the database itself: pinned official image (the lab
#   proved an unpinned cluster resolves to whatever is newest), storage,
#   shape, and pointers to the two objects above.
#
# No kopf.adopt here: these live in the app namespace, the manifest lives in
# its own, and a cross-namespace owner reference is treated as missing (the
# v4 lesson). The namespace delete in cleanup() removes them, and the
# ClickHouse operator garbage-collects what it built from them.

import base64
import secrets as pysecrets
from typing import cast

import kubernetes
from kubernetes.client.exceptions import ApiException

FIELD_MANAGER = "platform-operator"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANIFEST_LABEL = "platform.datumlabs.io/manifest"

CH_GROUP = "clickhouse.com"
CH_VERSION = "v1alpha1"
IMAGE = "clickhouse/clickhouse-server"
KEEPER_IMAGE = "clickhouse/clickhouse-keeper"
KEEPER_STORAGE = "1Gi"


def service_name(name: str) -> str:
    """The Service the official operator creates: <cluster>-clickhouse-headless,
    with a port named `http` on 8123. Its convention, observed in the lab,
    not configured by us."""
    return f"{name}-clickhouse-headless"


def admin_secret_name(name: str) -> str:
    return f"{name}-admin"


def _labels(name: str) -> dict:
    return {MANAGED_BY_LABEL: FIELD_MANAGER, MANIFEST_LABEL: name}


def ensure_admin_secret(name: str, app_ns: str) -> None:
    """The default user's password, generated once and kept.

    Kept, not converged: regenerating would break the running database's
    idea of its own credential mid-flight. Reading before writing is the
    same pattern as the gateway cookie secret.
    """
    api = kubernetes.client.CoreV1Api()
    try:
        secret = cast(
            kubernetes.client.V1Secret,
            api.read_namespaced_secret(admin_secret_name(name), app_ns),
        )
        if "password" in (secret.data or {}):
            return
    except ApiException as e:
        if e.status != 404:
            raise
    api.patch_namespaced_secret(
        name=admin_secret_name(name),
        namespace=app_ns,
        body={
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": admin_secret_name(name),
                "namespace": app_ns,
                "labels": _labels(name),
            },
            "stringData": {"password": pysecrets.token_urlsafe(24)},
        },
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def admin_password(name: str, app_ns: str) -> str:
    secret = cast(
        kubernetes.client.V1Secret,
        kubernetes.client.CoreV1Api().read_namespaced_secret(
            admin_secret_name(name), app_ns
        ),
    )
    return base64.b64decode((secret.data or {})["password"]).decode()


def keeper_body(name: str, app_ns: str, replicas: int, version: str) -> dict:
    # Raft wants an odd quorum: one keeper under one database replica,
    # three under anything more.
    return {
        "apiVersion": f"{CH_GROUP}/{CH_VERSION}",
        "kind": "KeeperCluster",
        "metadata": {
            "name": f"{name}-keeper",
            "namespace": app_ns,
            "labels": _labels(name),
        },
        "spec": {
            "replicas": 1 if replicas <= 1 else 3,
            "containerTemplate": {
                # Same version as the database, and pinned for the same
                # reason: leaving this out defaulted keeper to `latest`,
                # which the first rebuild caught.
                "image": {"repository": KEEPER_IMAGE, "tag": version},
            },
            "dataVolumeClaimSpec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": KEEPER_STORAGE}},
            },
        },
    }


def clickhouse_body(name: str, app_ns: str, cfg: dict) -> dict:
    return {
        "apiVersion": f"{CH_GROUP}/{CH_VERSION}",
        "kind": "ClickHouseCluster",
        "metadata": {
            "name": name,
            "namespace": app_ns,
            "labels": _labels(name),
        },
        "spec": {
            "replicas": cfg.get("replicas", 1),
            "shards": cfg.get("shards", 1),
            "keeperClusterRef": {"name": f"{name}-keeper"},
            "containerTemplate": {
                # version, not image: the manifest speaks the application's
                # language and the platform knows where official images live.
                "image": {"repository": IMAGE, "tag": cfg["version"]},
            },
            "dataVolumeClaimSpec": {
                "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": cfg.get("storage", "2Gi")}},
            },
            "settings": {
                "defaultUserPassword": {
                    "passwordType": "password",
                    "secret": {"name": admin_secret_name(name), "key": "password"},
                },
            },
        },
    }


def _apply_cr(body: dict) -> None:
    kinds = {"KeeperCluster": "keeperclusters", "ClickHouseCluster": "clickhouseclusters"}
    kubernetes.client.CustomObjectsApi().patch_namespaced_custom_object(
        group=CH_GROUP,
        version=CH_VERSION,
        namespace=body["metadata"]["namespace"],
        plural=kinds[body["kind"]],
        name=body["metadata"]["name"],
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def apply(name: str, app_ns: str, cfg: dict, logger) -> None:
    """source.clickhouse -> a running warehouse. Secret first (the database
    reads it at birth), keeper next (the database refuses to exist without
    it), database last. All server-side apply: safe to run forever."""
    ensure_admin_secret(name, app_ns)
    replicas = cfg.get("replicas", 1)
    _apply_cr(keeper_body(name, app_ns, replicas, cfg["version"]))
    _apply_cr(clickhouse_body(name, app_ns, cfg))
    logger.info(
        "applied clickhouse %s@%s (%dx%d, keeper x%d) -> %s",
        name, cfg["version"], cfg.get("shards", 1), replicas,
        1 if replicas <= 1 else 3, app_ns,
    )


if __name__ == "__main__":
    # Pure smoke: prints what would be applied, touches nothing.
    #   .venv/bin/python operator/warehouse.py
    import json

    cfg = {"version": "26.8.1.2041", "replicas": 1, "shards": 1, "storage": "2Gi"}
    print(json.dumps(keeper_body("clickhouse", "app-clickhouse", 1, cfg["version"]), indent=2))
    print(json.dumps(clickhouse_body("clickhouse", "app-clickhouse", cfg), indent=2))
    print("service:", service_name("clickhouse"))
    print("admin secret:", admin_secret_name("clickhouse"))
