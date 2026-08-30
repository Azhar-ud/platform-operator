# v9: the operator becomes a pod. Conventions are polaris's (dlt-pipeline
# Dockerfile is the precedent): pinned base, non-root, deps before source.
#
# Pinned by DIGEST, not by tag. A tag moves: `python:3.12-slim` today is not
# the image it was last month, so a rebuild of an unchanged commit can produce
# a different container. (In polaris, Renovate raises the digest as a
# reviewable pull request; here it moves by hand until the code moves there.)
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

# A container that runs as root runs as root on the node. The UID is NUMERIC
# because Kubernetes `runAsNonRoot` resolves a number rather than a name.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

# The root filesystem is read-only at runtime; without this Python would try
# (and fail) to drop __pycache__ next to the source.
ENV PYTHONDONTWRITEBYTECODE=1

# Dependencies before source, so a code change does not invalidate the layer
# that installs them.
COPY operator/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=10001:10001 operator/ ./operator/

USER 10001

# --standalone: exactly one operator per cluster is the deployment's job
# (replicas: 1), not a peering election's. Liveness serves the probe.
CMD ["kopf", "run", "--standalone", "--all-namespaces", \
     "--liveness=http://0.0.0.0:8080/healthz", "operator/main.py"]
