# The platform operator, v2: notice a manifest and report back.
#
# It deliberately converges to nothing - sees an ApplicationManifest, writes a
# status, stops. This proves the plumbing (watch stream, reconcile loop, status
# subresource, restart survival) before the loop is given anything real to do.
# Creating things starts in v3.
#
# Run it against the current kubectl context:
#
#   .venv/bin/kopf run operator/main.py --verbose
#
# The loop is LEVEL-TRIGGERED: it never asks "what changed?", it asks "what
# should be true, and is it?". That is why on.resume is here - on startup kopf
# re-reads every manifest that already exists and runs this handler again, so
# a restarted operator converges with no events replayed. Kill it and start it
# again: the status is re-written and nothing is lost.

import kopf

GROUP = "platform.datumlabs.io"
VERSION = "v1alpha1"
PLURAL = "applicationmanifests"


@kopf.on.create(GROUP, VERSION, PLURAL)
@kopf.on.update(GROUP, VERSION, PLURAL)
@kopf.on.resume(GROUP, VERSION, PLURAL)
def reconcile(spec, name, patch, logger, **_):
    logger.info("reconciling %s", name)

    # Written through the status subresource the CRD enables; the Phase printer
    # column on `kubectl get appman` reads .status.phase, so this line is what
    # makes that column light up.
    patch.status["phase"] = "Seen"

    # What the operator actually observed, so a manifest author can tell at a
    # glance whether the field they added was seen at all.
    patch.status["observedFields"] = sorted(spec.keys())
