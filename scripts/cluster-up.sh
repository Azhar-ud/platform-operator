#!/usr/bin/env bash
#
# Bring up the local Datum development cluster.
#
# The cluster itself is defined in k3d-config.yaml, not here. That file is the
# source of truth and works with plain `k3d cluster create --config` on any OS,
# including PowerShell. This script only adds convenience: a prerequisite check,
# and the create / start / already-running logic.
#
# Requires bash. On Windows use WSL or Git Bash, or run the k3d command directly:
#   k3d cluster create --config k3d-config.yaml

set -euo pipefail

CLUSTER="datum-dev"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="$REPO_ROOT/k3d-config.yaml"

# --- prerequisites --------------------------------------------------------
# Report everything that is missing at once, with the install command for this
# OS, rather than failing one tool at a time.

case "$(uname -s)" in
  Darwin) OS="macos" ;;
  Linux)  OS="linux" ;;
  *)      OS="other" ;;
esac

install_hint() {
  case "$1:$OS" in
    k3d:macos)     echo "brew install k3d" ;;
    k3d:linux)     echo "curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash" ;;
    k3d:*)         echo "winget install -e --id k3d.k3d" ;;
    kubectl:macos) echo "brew install kubectl" ;;
    kubectl:linux) echo "curl -LO \"https://dl.k8s.io/release/\$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl\" && sudo install -m 0755 kubectl /usr/local/bin/kubectl" ;;
    kubectl:*)     echo "winget install -e --id Kubernetes.kubectl" ;;
    helm:macos)    echo "brew install helm" ;;
    helm:linux)    echo "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash" ;;
    helm:*)        echo "winget install -e --id Helm.Helm" ;;
    *)             echo "see the platform setup guide" ;;
  esac
}

missing=0
for tool in k3d kubectl helm; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "missing: $tool"
    echo "    install with: $(install_hint "$tool")"
    missing=1
  fi
done

if [[ "$missing" -eq 1 ]]; then
  echo
  echo "Install the tools above, then run this again."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "error: Docker is installed but not running."
  case "$OS" in
    linux) echo "    start it with: sudo systemctl start docker" ;;
    *)     echo "    start Docker Desktop and wait for it to report Running" ;;
  esac
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "error: cluster definition not found at $CONFIG"
  exit 1
fi

# --- create it, start it, or leave it alone -------------------------------

if k3d cluster list --no-headers 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER"; then

  # It exists. The servers column reads "1/1" when running, "0/1" when stopped.
  running="$(k3d cluster list --no-headers | awk -v c="$CLUSTER" '$1==c {print $2}')"

  if [[ "$running" == 0/* ]]; then
    echo "==> starting existing cluster '$CLUSTER'"
    k3d cluster start "$CLUSTER"
  else
    echo "==> cluster '$CLUSTER' already running, nothing to do"
  fi

else
  echo "==> creating cluster '$CLUSTER' from k3d-config.yaml"

  # Ports 80 and 443 must be free before creation. k3d cannot change port
  # mappings afterwards, so getting this wrong means deleting and rebuilding.
  # Uses bash's own /dev/tcp rather than ss or lsof, which differ by platform.
  for port in 80 443; do
    if (exec 3<>/dev/tcp/127.0.0.1/"$port") 2>/dev/null; then
      exec 3<&- 3>&-
      echo "error: something is already listening on port $port."
      echo "    is the other cluster running? try: k3d cluster stop datum"
      echo "    do NOT work around this by mapping a different host port -"
      echo "    platform URLs and every OIDC redirect assume 80 and 443."
      exit 1
    fi
  done

  k3d cluster create --config "$CONFIG"
fi

# --- point kubectl at it and verify ---------------------------------------

kubectl config use-context "k3d-$CLUSTER" >/dev/null

echo
kubectl get nodes
echo
echo "Ready. The cluster has no ingress controller yet - that is the next layer:"
echo "  ./scripts/ingress-up.sh   # HAProxy + *.datum.local TLS (see docs/cluster.md)"
