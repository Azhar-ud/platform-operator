#!/usr/bin/env bash
#
# Install the team-standard ingress layer onto a running datum-dev cluster:
# HAProxy ingress controller terminating TLS with the *.datum.local wildcard
# certificate, plus in-cluster DNS so pods resolve the same hostnames.
#
# Follows the platform team's haproxy-ingress-setup.md and tls-dns-setup.md
# (A-01/A-02/A-06/A-07). Safe to run twice: every step is apply-or-skip.
#
# One-time machine setup (cannot be scripted — needs your password, and the CA
# belongs to your machine, not this repo):
#
#   1. install mkcert           Arch:  sudo pacman -S mkcert
#                               macOS: brew install mkcert
#                               other: see the team's tls-dns-setup.md
#   2. mkcert -install          creates + trusts the local CA, then fully
#                               restart your browser
#   3. hosts file               hosts files cannot do wildcards; each name is
#                               listed. Run:
#      echo "127.0.0.1 datum.local iam.datum.local apps.datum.local smoke.datum.local myapps.datum.local clickhouse.datum.local dagster.datum.local chat.datum.local" | sudo tee -a /etc/hosts
#
# The certificate itself IS scripted below (mkcert without sudo), into certs/,
# which is gitignored. The CA private key never enters this repo.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="datum-platform"
SECRET="datum-platform-tls"
RELEASE="haproxy-ingress"
CHART_VERSION="1.52.1"   # pinned deliberately - exact pins, no ranges (A-03)

# --- the cluster must be up and be ours ------------------------------------

if ! kubectl config use-context k3d-datum-dev >/dev/null 2>&1; then
  echo "error: kubectl context k3d-datum-dev not found. Run ./scripts/cluster-up.sh first."
  exit 1
fi
if ! kubectl get nodes >/dev/null 2>&1; then
  echo "error: cluster not reachable. Run ./scripts/cluster-up.sh first."
  exit 1
fi

# --- certificate ------------------------------------------------------------

if ! command -v mkcert >/dev/null 2>&1; then
  echo "error: mkcert is not installed. See the one-time setup at the top of this script."
  exit 1
fi

if [[ ! -f "$REPO_ROOT/certs/datum-local.crt" ]]; then
  echo "==> creating wildcard certificate for *.datum.local"
  mkdir -p "$REPO_ROOT/certs"
  mkcert -cert-file "$REPO_ROOT/certs/datum-local.crt" \
         -key-file  "$REPO_ROOT/certs/datum-local.key" \
         "*.datum.local" datum.local
else
  echo "==> certificate already present in certs/, keeping it"
fi

# A certificate signed by an untrusted CA still "works" - the browser just
# warns. Catch that now rather than in the smoke test.
if ! mkcert -CAROOT >/dev/null 2>&1 || [[ ! -f "$(mkcert -CAROOT)/rootCA.pem" ]]; then
  echo "warning: no local CA found. Run 'mkcert -install' (one-time, needs sudo)"
  echo "         or every https://*.datum.local page will warn."
fi

# --- pre-checks from haproxy-ingress-setup.md -------------------------------

# Another controller already holding an IngressClass means a port fight, except
# our own from a previous run of this script.
other_class="$(kubectl get ingressclass -o name 2>/dev/null | grep -v '/haproxy$' || true)"
if [[ -n "$other_class" ]]; then
  echo "error: another ingress controller is installed: $other_class"
  echo "    Two controllers cannot share ports 80 and 443. Remove it first."
  exit 1
fi

if kubectl -n kube-system get deploy traefik >/dev/null 2>&1; then
  echo "error: Traefik is running. It was supposed to be disabled at cluster"
  echo "    creation (k3d-config.yaml). This cluster was not built from the"
  echo "    team config - recreate it: ./scripts/cluster-down.sh --delete && ./scripts/cluster-up.sh"
  exit 1
fi

# --- namespace and TLS secret -----------------------------------------------

# The dry-run | apply pattern makes both safe to re-run. The secret name is the
# handoff point: on a real client install cert-manager fills the same secret
# and nothing else changes.
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NAMESPACE" create secret tls "$SECRET" \
  --cert="$REPO_ROOT/certs/datum-local.crt" \
  --key="$REPO_ROOT/certs/datum-local.key" \
  --dry-run=client -o yaml | kubectl apply -f -

# --- HAProxy ingress controller ----------------------------------------------

echo "==> installing HAProxy ingress controller (chart $CHART_VERSION)"
helm repo add haproxytech https://haproxytech.github.io/helm-charts >/dev/null
helm repo update haproxytech >/dev/null
helm upgrade --install "$RELEASE" haproxytech/kubernetes-ingress \
  --version "$CHART_VERSION" \
  --namespace "$NAMESPACE" \
  -f "$REPO_ROOT/cluster/haproxy-values.yaml"

kubectl -n "$NAMESPACE" rollout status deploy/haproxy-ingress-kubernetes-ingress

# --- in-cluster DNS -----------------------------------------------------------

echo "==> pointing *.datum.local at the ingress service for pods (coredns-custom)"
kubectl apply -f "$REPO_ROOT/cluster/coredns-datum.yaml"
kubectl -n kube-system rollout restart deployment coredns >/dev/null
kubectl -n kube-system rollout status deployment coredns

# --- verify -------------------------------------------------------------------

echo
ports="$(kubectl -n "$NAMESPACE" get svc haproxy-ingress-kubernetes-ingress \
  -o jsonpath='{range .spec.ports[*]}{.port}:{.nodePort} {end}')"
case "$ports" in
  *80:30080*443:30443*|*443:30443*80:30080*) echo "node ports:    80:30080 443:30443 - correct" ;;
  *) echo "error: node ports are '$ports', expected 80:30080 and 443:30443."
     echo "    The values file did not apply - check cluster/haproxy-values.yaml."
     exit 1 ;;
esac

kubectl get ingressclass haproxy >/dev/null && echo "ingressclass:  haproxy - present"

if kubectl -n "$NAMESPACE" logs deploy/haproxy-ingress-kubernetes-ingress --tail=50 2>/dev/null \
   | grep -qi "default.*certificate.*not\|does not exist"; then
  echo "warning: controller logs mention a missing default certificate -"
  echo "         it is serving a self-signed fallback. Check the $SECRET secret."
else
  echo "default cert:  served from $NAMESPACE/$SECRET"
fi

echo
echo "Ready. Ingress objects with class 'haproxy' (the default) now route"
echo "*.datum.local hostnames, TLS included. If the browser still warns,"
echo "run 'mkcert -install' and fully restart the browser."
