# The cluster this operator is developed against

## What it is

`datum-dev` — the team-standard local cluster, defined in the platform team's
`k3d-local-cluster-setup.md`. Every engineer runs the same one. This repo does not
define its own cluster; `scripts/cluster-up.sh` wraps the standard command.

| | |
|---|---|
| Name | `datum-dev` (always this, never `dev`) |
| Nodes | 1 control plane + 2 workers |
| Kubernetes | k3s v1.35.5 |
| Host port 80 | → nodePort `30080` |
| Host port 443 | → nodePort `30443` |
| API server | `127.0.0.1:6550` |
| kubectl context | `k3d-datum-dev` |

The `127.0.0.1:` prefix on the API port matters. Without it, k3d writes
`host.docker.internal` into the kubeconfig, which resolves to your LAN IP and gets
dropped by the firewall on some machines — `kubectl` then hangs for 32 seconds and
fails while the cluster is perfectly healthy.

Node ports 30080 and 30443 are a contract, not a preference: the ingress controller
the platform chart installs later publishes itself on exactly those, which is what
connects it to `localhost:80` and `localhost:443`.

## Prerequisites

Docker, `k3d` and `kubectl` — plus `helm`, which is not used by these scripts but is
needed for platform work. Full per-OS install instructions are in the platform team's
`k3d-local-cluster-setup.md`.

On Linux, if `docker` needs `sudo`:

```bash
sudo usermod -aG docker $USER
newgrp docker
```

## Known gap: there is no ingress controller

**This is deliberate. It is recorded here so it is not rediscovered as a bug.**

k3s ships Traefik and enables it by default. The team-standard cluster disables it,
for two reasons:

1. The Datum platform Helm chart installs its own ingress controller, and two
   controllers cannot both hold ports 80 and 443.
2. Which controller ships is still an open decision — ticket `A-06`,
   "HAProxy vs Traefik decision record."

So on `datum-dev`:

```
kubectl get ingressclass     # No resources found
```

### What this changes for operator development

The construction plan for this operator assumed Traefik would be present and that
v0 would finish with a hostname answering over HTTP. That is not possible here.

- Applications installed by the operator **cannot be reached in a browser by hostname**
  until the platform chart lands.
- Verify work with `kubectl get pods`, `kubectl get appman`, and `kubectl port-forward`
  when a UI genuinely needs to be seen.
- The v0 acceptance test is therefore the **port path**, not ingress: put something on
  nodePort 30080 temporarily and confirm `curl http://localhost` reaches it. That proves
  the same wiring without needing a controller.

### Where it plugs back in

The `surface.host` field in the `ApplicationManifest` contract exists to feed this
layer — it is the hostname an Ingress rule gets generated from once a controller
exists. Generating those routes is ticket `A-31`, owned by the platform/IAM side, not
by this operator.

This operator's responsibility is that the field exists, is required, and is filled in
by real applications.

## Verified capabilities

Checked directly against `datum-dev` on 16 August 2026:

| | |
|---|---|
| `helmcharts.helm.cattle.io` | present |
| `helmchartconfigs.helm.cattle.io` | present |
| helm-controller | runs inside the k3s server process, not as a separate pod |
| Traefik | absent, as designed |
| ingressclass | none |

The first two matter: the operator installs applications by writing a `HelmChart`
object and letting k3s's built-in controller do the install. That controller being
present is what makes the install step ten lines of code instead of a Helm integration.

**Still unverified:** that the controller actually reconciles a real chart end to end.
The CRD existing is not proof the controller acts. Confirm with a small chart before
depending on it.

## Everyday commands

```bash
./scripts/cluster-up.sh                    # create or start
./scripts/cluster-down.sh                  # stop, keep contents, free ports 80/443
./scripts/cluster-down.sh --delete         # destroy completely

kubectl config use-context k3d-datum-dev   # point kubectl back here
k3d image import myimage:dev -c datum-dev  # local images are invisible until imported
```

That last one matters from v7 onward: an image you build on your machine does not
exist inside the cluster until you import it.

## Gotchas

**`stop` releases ports 80 and 443.** If something else on your machine grabs port 80
while the cluster is stopped, `start` will fail. Free the port and start again.

**Never remap to different host ports** to work around a conflict. Platform URLs,
Keycloak's issuer URL and every OIDC redirect assume 80 and 443. If you genuinely
cannot free them, raise it — it needs a decision, not a local workaround.

**Rootless Docker on Linux** cannot publish ports below 1024. Use rootful Docker, or:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
```
