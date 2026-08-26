# The platform operator, v3: create something real, and take it away again.
#
# v2 proved the plumbing - the loop saw manifests and wrote status. Now the
# loop builds: one namespace per application, deliberately boring, because it
# is the smallest thing that exercises the two disciplines everything later
# depends on:
#
#   IDEMPOTENCE - running the loop once and a thousand times leaves the
#   cluster in the same state. In practice: server-side apply, never
#   create-if-it-does-not-exist.
#
#   OWNERSHIP - everything created is stamped as belonging to the manifest
#   that caused it, and deleting the manifest deletes what it made. One
#   subtlety: namespaces are cluster-scoped and the manifest is namespaced,
#   and Kubernetes forbids that owner reference - so for the namespace the
#   cleanup is an explicit delete handler (kopf backs it with a finalizer on
#   the manifest). Owner references proper start in v4, where the HelmChart
#   child is namespaced and kopf.adopt() works.
#
# Run it against the current kubectl context:
#
#   .venv/bin/kopf run operator/main.py --verbose

import kopf
import kubernetes

GROUP = "platform.datumlabs.io"
VERSION = "v1alpha1"
PLURAL = "applicationmanifests"

# The name server-side apply records as the owner of the fields we set. If a
# second party patches the same fields with force, THEY become the owner -
# that is the "field managers can fight" trade the deck warns about.
FIELD_MANAGER = "platform-operator"

MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
MANIFEST_LABEL = "platform.datumlabs.io/manifest"


@kopf.on.startup()
def configure(logger, **_):
    # kopf authenticates its own watch client; the kubernetes client we use to
    # write objects needs configuring separately. In-cluster config arrives
    # with v7 - until then this runs on a laptop against a kubeconfig.
    kubernetes.config.load_kube_config()
    logger.info("kubernetes client configured from kubeconfig")


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, name, patch, logger, **_):
    ns = f"app-{name}"

    body = {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {
            "name": ns,
            "labels": {
                # The ownership stamp. Not an owner reference (impossible here,
                # see the header) but enough for a human or a later controller
                # to answer "who made this, and from what?"
                MANAGED_BY_LABEL: FIELD_MANAGER,
                MANIFEST_LABEL: name,
            },
        },
    }

    # Server-side apply: the API server merges this declaration with whatever
    # exists, and applying the same declaration twice is a no-op. This one
    # call is create-or-update-or-nothing, which is the whole idempotence
    # story - there is no "does it exist yet?" branch to get wrong.
    kubernetes.client.CoreV1Api().patch_namespace(
        name=ns,
        body=body,
        field_manager=FIELD_MANAGER,
        force=True,
        _content_type="application/apply-patch+yaml",
    )
    logger.info("applied namespace %s", ns)

    patch.status["phase"] = "Ready"
    patch.status["namespace"] = ns
    patch.status["observedFields"] = sorted(spec.keys())


@kopf.on.delete(GROUP, VERSION, PLURAL)
def cleanup(name, logger, **_):
    # Registering this handler makes kopf hold the manifest with a finalizer
    # until we return successfully - so the namespace is gone BEFORE the
    # manifest disappears, and a failed cleanup blocks deletion visibly
    # instead of leaking the namespace silently.
    ns = f"app-{name}"
    try:
        kubernetes.client.CoreV1Api().delete_namespace(ns)
        logger.info("deleted namespace %s", ns)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 404:
            logger.info("namespace %s already gone, nothing to do", ns)
        else:
            raise  # kopf retries with backoff; the finalizer keeps the manifest
