#!/usr/bin/env bash
#
# Take the local Datum development cluster down.
#
# Default is "stop": the cluster and everything in it is kept, and host ports
# 80 and 443 are released back to your machine. This is what you want at the
# end of a working day.
#
# Pass --delete to remove the cluster entirely. Nothing is kept, and the next
# cluster-up.sh rebuilds it from scratch.

set -euo pipefail

CLUSTER="datum-dev"
MODE="stop"

for arg in "$@"; do
  case "$arg" in
    --delete) MODE="delete" ;;
    -h|--help)
      echo "usage: $(basename "$0") [--delete]"
      echo "  (no args)  stop the cluster, keep its contents"
      echo "  --delete   destroy the cluster completely"
      exit 0
      ;;
    *) echo "error: unknown argument '$arg' (try --help)"; exit 1 ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running, so the cluster is not running either. Nothing to do."
  exit 0
fi

if ! k3d cluster list --no-headers 2>/dev/null | awk '{print $1}' | grep -qx "$CLUSTER"; then
  echo "Cluster '$CLUSTER' does not exist. Nothing to do."
  exit 0
fi

if [[ "$MODE" == "delete" ]]; then
  echo "==> Deleting cluster '$CLUSTER' (everything in it is lost)"
  k3d cluster delete "$CLUSTER"
else
  echo "==> Stopping cluster '$CLUSTER' (contents kept, ports 80/443 released)"
  k3d cluster stop "$CLUSTER"
fi
