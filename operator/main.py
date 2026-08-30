# The platform operator, v4: install real software by delegating to Helm.
#
# v3 proved idempotence and ownership on a namespace. Now the loop reads the
# manifest's fields for the first time and turns them into an install:
#
#   MANIFEST -> HELM. The operator templates nothing and shells out to
#   nothing. The manifest's `source` names a chart (repo, chart, version,
#   pinned always); the operator writes one HelmChart object and stops.
#   k3s's built-in helm-controller - the same one that installed the ingress
#   this cluster already runs - picks it up and does the install.
#   Controllers all the way down: every chart in the ecosystem becomes
#   installable through the manifest, and the operator grew by one object.
#
#   `source` became a union of named installers when the official ClickHouse
#   operator arrived: source.helm keeps the path above, source.clickhouse
#   writes the CRs that operator reconciles instead (warehouse.py). Same
#   delegation, more knowledgeable cook. Exactly-one-branch is the API
#   server's job (a CEL rule in the CRD), not this loop's.
#
#   OWNERSHIP, now the easy way. The HelmChart is created in the manifest's
#   own namespace (targetNamespace points at the app's), so kopf.adopt()
#   finally works: delete the manifest and garbage collection deletes the
#   HelmChart, and helm-controller uninstalls the release. One deliberate
#   deviation from the build notes, which put the HelmChart in kube-system:
#   an owner reference only works inside one namespace - a cross-namespace
#   owner is treated as missing and the child is collected immediately - and
#   k3s watches HelmChart objects everywhere, which the v4 bring-up proved
#   live. The app namespace still needs the explicit delete handler from v3:
#   it is cluster-scoped, out of owner-reference reach.
#
#   STATUS is a claim about reality, not an acknowledgement. A timer checks
#   the deployments Helm created and reports what it sees:
#
#     Unmanaged  - the manifest declares no source; namespace only
#     Deploying  - children applied, workloads not (yet) ready
#     Ready      - every deployment in the app namespace is available
#     Failed     - the helm-controller's install job gave up
#
# Run it against the current kubectl context:
#
#   .venv/bin/kopf run operator/main.py --verbose

from typing import cast

import kopf
import kubernetes
from kubernetes.client.exceptions import ApiException

import gateway
import keycloak
import reconciler
import warehouse

GROUP = "platform.datumlabs.io"
VERSION = "v1alpha1"
PLURAL = "applicationmanifests"

HELM_GROUP = "helm.cattle.io"
HELM_VERSION = "v1"
HELM_PLURAL = "helmcharts"

FIELD_MANAGER = "platform-operator"
MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANIFEST_LABEL = "platform.datumlabs.io/manifest"


@kopf.on.startup()
def configure(logger, **_):
    # kopf authenticates its own watch client; the kubernetes client we use to
    # write objects needs configuring separately. In-cluster config arrives
    # with the polaris move - until then this runs on a laptop against a
    # kubeconfig.
    kubernetes.config.load_kube_config()
    logger.info("kubernetes client configured from kubeconfig")


def apply_namespace(app_ns: str, manifest_name: str):
    body = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": app_ns,
            "labels": {
                MANAGED_BY_LABEL: FIELD_MANAGER,
                MANIFEST_LABEL: manifest_name,
            },
        },
    }
    # Server-side apply: create-or-update-or-nothing in one call, the whole
    # idempotence story since v3.
    kubernetes.client.CoreV1Api().patch_namespace(
        name=app_ns,
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


def apply_helmchart(name: str, namespace: str, app_ns: str, source):
    body = {
        "apiVersion": f"{HELM_GROUP}/{HELM_VERSION}",
        "kind": "HelmChart",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                MANAGED_BY_LABEL: FIELD_MANAGER,
                MANIFEST_LABEL: name,
            },
        },
        "spec": {
            "chart": source["chart"],
            "repo": source["repo"],
            "version": source["version"],
            "targetNamespace": app_ns,
            "valuesContent": source.get("values", ""),
        },
    }
    # Same namespace as the manifest, so this owner reference is legal and
    # garbage collection does the cleanup we wrote by hand for the namespace.
    kopf.adopt(body)
    kubernetes.client.CustomObjectsApi().patch_namespaced_custom_object(
        group=HELM_GROUP,
        version=HELM_VERSION,
        namespace=namespace,
        plural=HELM_PLURAL,
        name=name,
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, name, namespace, patch, logger, **_):
    app_ns = f"app-{name}"

    apply_namespace(app_ns, name)
    logger.info("applied namespace %s", app_ns)

    patch.status["namespace"] = app_ns
    patch.status["observedFields"] = sorted(spec.keys())

    # Validation ran when the manifest was written, not now - a stored object
    # may predate the schema that would have required this field - so read,
    # never assume.
    source = spec.get("source")
    if not source:
        # An honest answer, not a failure: the contract was accepted, the
        # namespace exists, and this application ships nothing to install.
        patch.status["phase"] = "Unmanaged"
        logger.info("no source in %s; namespace only", name)
        return

    # The union: exactly one installer, enforced by the CRD's CEL rule at
    # write time. A stored object may predate the union (flat chart fields at
    # the top of source) - read, never assume, so that shape still installs.
    helm = source.get("helm") or (source if "chart" in source else None)
    clickhouse = source.get("clickhouse")
    if helm:
        apply_helmchart(name, namespace, app_ns, helm)
        logger.info(
            "applied helmchart %s (%s@%s -> %s)",
            name, helm["chart"], helm["version"], app_ns,
        )
    elif clickhouse:
        warehouse.apply(name, app_ns, clickhouse, logger)
    else:
        # Stored under a schema this operator predates: honest and inert,
        # like a manifest with no source at all.
        patch.status["phase"] = "Unmanaged"
        logger.info("source in %s names no installer this operator knows", name)
        return
    patch.status["phase"] = "Deploying"

    # The second translation: identity.driver gateway means this application
    # cannot authenticate platform users itself, so the operator puts the
    # login door in front of it. Same read-never-assume rule as source.
    identity = spec.get("identity") or {}
    host = (spec.get("surface") or {}).get("host")
    if identity.get("driver") == "gateway" and host:
        gateway.reconcile(name, app_ns, host, logger)
        patch.status["gateway"] = f"https://{host}"


@kopf.timer(GROUP, VERSION, PLURAL, interval=30, initial_delay=30)
def observe(spec, name, namespace, status, patch, logger, **_):
    """Phase tracks reality on a clock, not an event.

    Level-triggered to the end: every tick re-derives the phase from what
    exists, so a crashed install or a deleted deployment is noticed without
    any event having been watched for.
    """
    if not spec.get("source"):
        return

    phase = observed_phase(name, namespace, f"app-{name}")
    if phase != status.get("phase"):
        patch.status["phase"] = phase
        logger.info("%s -> %s", name, phase)


def observed_phase(name: str, namespace: str, app_ns: str) -> str:
    batch = kubernetes.client.BatchV1Api()
    try:
        # helm-controller runs the install as a job named helm-install-<chart>.
        job = cast(
            kubernetes.client.V1Job,
            batch.read_namespaced_job(f"helm-install-{name}", namespace),
        )
        job_status = job.status or kubernetes.client.V1JobStatus()
        if (job_status.failed or 0) > 0 and not job_status.active:
            return "Failed"
    except ApiException as e:
        if e.status != 404:
            raise  # a missing job just means helm-controller hasn't started

    # Real charts ship real shapes: a web app is a Deployment, a database is
    # a StatefulSet (Bitnami's ClickHouse is one - the first run of v4 sat on
    # "Deploying" forever because only Deployments were counted). Both kinds
    # answer the same question: does reality have the replicas the spec asks?
    apps = kubernetes.client.AppsV1Api()
    deployments = cast(
        kubernetes.client.V1DeploymentList, apps.list_namespaced_deployment(app_ns)
    ).items or []
    statefulsets = cast(
        kubernetes.client.V1StatefulSetList, apps.list_namespaced_stateful_set(app_ns)
    ).items or []

    workloads = deployments + statefulsets
    if not workloads:
        return "Deploying"
    for w in workloads:
        # Deployments report available_replicas, StatefulSets ready_replicas.
        observed = getattr(w.status, "available_replicas", None) if w.status else None
        if observed is None and w.status is not None:
            observed = w.status.ready_replicas
        wanted = (w.spec.replicas or 1) if w.spec else 1
        if (observed or 0) < wanted:
            return "Deploying"
    return "Ready"


@kopf.timer(GROUP, VERSION, PLURAL, interval=60, initial_delay=45)
def push_grants(spec, name, status, patch, logger, **_):
    """The third translation: reconciler.mode push means grants converge.

    On a clock, like phase observation, and for the same reason: a new
    platform user, a role change in Keycloak, a hand-deleted warehouse user
    - none of those are Kubernetes events the operator could watch. Every
    tick re-reads the decision source (Keycloak) and the contract
    (entitlements), and makes the warehouse match. A quiet tick writes
    nothing anywhere - the converge compares before it fixes.

    The service the SQL lands on depends on who installed the database:
    a chart's release is named after the manifest, the official ClickHouse
    operator names its Service by its own convention (warehouse.py states
    it once).
    """
    if (spec.get("reconciler") or {}).get("mode") != "push":
        return

    source = spec.get("source") or {}
    service = warehouse.service_name(name) if "clickhouse" in source else name
    summary = reconciler.converge(
        app_ns=f"app-{name}",
        service=service,
        entitlements=spec.get("entitlements") or {},
        platform_users=keycloak.list_platform_users(),
        logger=logger,
    )
    if status.get("grants") != summary:
        patch.status["grants"] = summary
        logger.info("grants for %s: %s", name, summary)


@kopf.on.delete(GROUP, VERSION, PLURAL)
def cleanup(name, logger, **_):
    # The HelmChart needs no handler: it is owned by the manifest, so garbage
    # collection deletes it and helm-controller uninstalls the release. The
    # namespace is cluster-scoped - out of owner-reference reach - so it keeps
    # the explicit handler, and kopf's finalizer holds the manifest until this
    # returns.
    ns = f"app-{name}"
    try:
        kubernetes.client.CoreV1Api().delete_namespace(ns)
        logger.info("deleted namespace %s", ns)
    except ApiException as e:
        if e.status == 404:
            logger.info("namespace %s already gone, nothing to do", ns)
        else:
            raise  # kopf retries with backoff; the finalizer keeps the manifest
