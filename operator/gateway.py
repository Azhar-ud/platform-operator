# The gateway: what `identity.driver: gateway` deploys.
#
# Step 3 proved this shape by hand - an oauth2-proxy in front of the
# application's HTTP door, deferring to the platform's Keycloak session, so
# one my-apps login opens every gateway application. This module is that
# scratch YAML turned into contract behavior: the operator generates it from
# the manifest, on every reconcile, for any application that declares the
# driver.
#
# Everything lands in the application's namespace, so ownership needs no new
# machinery: deleting the manifest deletes the namespace, and the gateway
# goes with it. The one thing outside the cluster - the Keycloak client - is
# ensured by keycloak.ensure_client, which is idempotent for the same reason
# everything here is applied with server-side apply.

import base64
import secrets as pysecrets
from typing import cast

import kopf
import kubernetes
from kubernetes.client.exceptions import ApiException

import keycloak

PROXY_IMAGE = "quay.io/oauth2-proxy/oauth2-proxy:v7.8.2"
PROXY_PORT = 4180

FIELD_MANAGER = "platform-operator"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANIFEST_LABEL = "platform.datumlabs.io/manifest"

INGRESS_SERVICE = "haproxy-ingress-kubernetes-ingress"
INGRESS_NAMESPACE = "datum-platform"
CA_CONFIGMAP = "datum-local-ca"


def _labels(name: str) -> dict:
    return {
        "app.kubernetes.io/name": "gateway",
        "app.kubernetes.io/instance": name,
        MANAGED_BY_LABEL: FIELD_MANAGER,
        MANIFEST_LABEL: name,
    }


def _apply(kind_api, method: str, namespace: str, name: str, body: dict):
    """Server-side apply for the core kinds: one call, create-or-update."""
    getattr(kind_api, method)(
        name=name,
        namespace=namespace,
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def _ingress_ip() -> str:
    """Where iam.datum.local actually lives, read from the cluster.

    The hostname exists only in laptop hosts files, so the proxy pod is told
    the ingress address via hostAliases. Read live on every reconcile - a
    reinstalled ingress controller gets a new ClusterIP, and a recorded one
    goes stale (which is exactly what happened to the my-apps values file).
    """
    svc = cast(
        kubernetes.client.V1Service,
        kubernetes.client.CoreV1Api().read_namespaced_service(
            INGRESS_SERVICE, INGRESS_NAMESPACE
        ),
    )
    assert svc.spec and svc.spec.cluster_ip
    return svc.spec.cluster_ip


def _upstream(name: str, app_ns: str) -> str:
    """The service the proxy forwards approved traffic to.

    Convention, not contract: the Service labeled as this application's
    instance, on its port named `http`. Every chart so far satisfies it.
    The contract grows a field for this the day a real application breaks
    the convention - not before.
    """
    services = cast(
        kubernetes.client.V1ServiceList,
        kubernetes.client.CoreV1Api().list_namespaced_service(
            app_ns, label_selector=f"app.kubernetes.io/instance={name}"
        ),
    ).items or []
    for svc in services:
        assert svc.metadata and svc.spec
        for port in svc.spec.ports or []:
            if port.name == "http":
                return f"http://{svc.metadata.name}:{port.port}"
    # The chart may not be up yet; reconcile will come around again.
    raise kopf.TemporaryError(
        f"no service labeled app.kubernetes.io/instance={name} with an http port in {app_ns}",
        delay=30,
    )


def _ensure_secret(name: str, app_ns: str, client_secret: str):
    """The proxy's two secrets, generated once and never rotated in place.

    The client secret is Keycloak's and merely lands here. The cookie secret
    seals the proxy's own session cookie: regenerating it on every reconcile
    would log every user out every loop, so an existing value is kept.
    """
    core = kubernetes.client.CoreV1Api()
    cookie_secret = None
    try:
        existing = cast(
            kubernetes.client.V1Secret,
            core.read_namespaced_secret(f"gateway-{name}", app_ns),
        )
        cookie_secret = base64.b64decode((existing.data or {})["cookie-secret"]).decode()
    except ApiException as e:
        if e.status != 404:
            raise
    if cookie_secret is None:
        cookie_secret = base64.urlsafe_b64encode(pysecrets.token_bytes(32)).decode()

    body = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": f"gateway-{name}",
            "namespace": app_ns,
            "labels": _labels(name),
        },
        "stringData": {
            "client-secret": client_secret,
            "cookie-secret": cookie_secret,
        },
    }
    _apply(core, "patch_namespaced_secret", app_ns, f"gateway-{name}", body)


def _ensure_ca_copy(app_ns: str) -> bool:
    """The mkcert CA, copied from the platform namespace if this install has
    one. A ConfigMap cannot be mounted across namespaces, so each gateway
    namespace gets a copy. Absent (a real install, publicly trusted issuer)
    the proxy simply runs without the extra CA."""
    core = kubernetes.client.CoreV1Api()
    try:
        source = cast(
            kubernetes.client.V1ConfigMap,
            core.read_namespaced_config_map(CA_CONFIGMAP, INGRESS_NAMESPACE),
        )
    except ApiException as e:
        if e.status == 404:
            return False
        raise
    body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CA_CONFIGMAP, "namespace": app_ns},
        "data": source.data or {},
    }
    _apply(core, "patch_namespaced_config_map", app_ns, CA_CONFIGMAP, body)
    return True


def _deployment(name: str, app_ns: str, host: str, upstream: str, with_ca: bool) -> dict:
    args = [
        "--provider=keycloak-oidc",
        f"--client-id=gateway-{name}",
        f"--oidc-issuer-url={keycloak.ISSUER_BASE}/realms/{keycloak.REALM}",
        f"--redirect-url=https://{host}/oauth2/callback",
        "--code-challenge-method=S256",
        # Any authenticated realm user may pass. Role-gating is reconciler
        # mode `gate` work, not the door's.
        "--email-domain=*",
        f"--upstream={upstream}",
        f"--http-address=0.0.0.0:{PROXY_PORT}",
        "--reverse-proxy=true",
        "--cookie-secure=true",
        "--pass-user-headers=true",
        "--skip-provider-button=true",
    ]
    if with_ca:
        args.append("--provider-ca-file=/etc/datum-ca/ca.pem")

    container = {
        "name": "oauth2-proxy",
        "image": PROXY_IMAGE,
        "args": args,
        "env": [
            {"name": "OAUTH2_PROXY_CLIENT_SECRET",
             "valueFrom": {"secretKeyRef": {"name": f"gateway-{name}", "key": "client-secret"}}},
            {"name": "OAUTH2_PROXY_COOKIE_SECRET",
             "valueFrom": {"secretKeyRef": {"name": f"gateway-{name}", "key": "cookie-secret"}}},
        ],
        "ports": [{"name": "http", "containerPort": PROXY_PORT}],
        "readinessProbe": {"httpGet": {"path": "/ping", "port": "http"}},
        "resources": {
            "requests": {"cpu": "10m", "memory": "32Mi"},
            "limits": {"memory": "128Mi"},
        },
    }
    volumes = []
    if with_ca:
        container["volumeMounts"] = [
            {"name": "datum-ca", "mountPath": "/etc/datum-ca", "readOnly": True}
        ]
        volumes = [{"name": "datum-ca", "configMap": {"name": CA_CONFIGMAP}}]

    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": f"gateway-{name}", "namespace": app_ns, "labels": _labels(name)},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {
                "app.kubernetes.io/name": "gateway",
                "app.kubernetes.io/instance": name,
            }},
            "template": {
                "metadata": {"labels": _labels(name)},
                "spec": {
                    "hostAliases": [{"ip": _ingress_ip(), "hostnames": ["iam.datum.local"]}],
                    "containers": [container],
                    "volumes": volumes,
                },
            },
        },
    }


def _service(name: str, app_ns: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": f"gateway-{name}", "namespace": app_ns, "labels": _labels(name)},
        "spec": {
            "selector": {
                "app.kubernetes.io/name": "gateway",
                "app.kubernetes.io/instance": name,
            },
            "ports": [{"name": "http", "port": PROXY_PORT, "targetPort": "http"}],
        },
    }


def _ingress(name: str, app_ns: str, host: str) -> dict:
    # surface.host, consumed here and nowhere else: the operator owns the
    # application's one address, and it routes to the gateway, not the app.
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": f"gateway-{name}", "namespace": app_ns, "labels": _labels(name)},
        "spec": {
            "ingressClassName": "haproxy",
            "rules": [{
                "host": host,
                "http": {"paths": [{
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {"service": {
                        "name": f"gateway-{name}",
                        "port": {"name": "http"},
                    }},
                }]},
            }],
        },
    }


def reconcile(name: str, app_ns: str, host: str, logger):
    """The whole gateway, converged: Keycloak client, secret, proxy, route."""
    client_secret = keycloak.ensure_client(
        f"gateway-{name}", f"https://{host}/oauth2/callback"
    )
    _ensure_secret(name, app_ns, client_secret)
    with_ca = _ensure_ca_copy(app_ns)
    upstream = _upstream(name, app_ns)

    core = kubernetes.client.CoreV1Api()
    apps = kubernetes.client.AppsV1Api()
    net = kubernetes.client.NetworkingV1Api()
    _apply(apps, "patch_namespaced_deployment", app_ns, f"gateway-{name}",
           _deployment(name, app_ns, host, upstream, with_ca))
    _apply(core, "patch_namespaced_service", app_ns, f"gateway-{name}",
           _service(name, app_ns))
    _apply(net, "patch_namespaced_ingress", app_ns, f"gateway-{name}",
           _ingress(name, app_ns, host))
    logger.info("gateway for %s: %s -> %s (client gateway-%s)", name, host, upstream, name)
