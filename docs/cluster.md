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

## Ingress: HAProxy, decided and installable

k3s ships Traefik; the team cluster disables it at creation because the platform
brings its own controller. That controller is now decided — **HAProxy** (A-06),
with a setup doc shared by the platform team on 18 Aug (`haproxy-ingress-setup.md`,
plus its prerequisite `tls-dns-setup.md` covering A-01/A-02).

This repo carries that setup as one idempotent script:

```bash
./scripts/ingress-up.sh
```

which, on a running `datum-dev`:

1. creates the `*.datum.local` wildcard certificate with `mkcert` into `certs/`
   (gitignored) if missing,
2. loads it as the `datum-platform-tls` Secret in namespace `datum-platform` —
   the name is a contract: cert-manager fills the same Secret on a real install,
3. installs the HAProxy ingress controller, chart pinned at `1.52.1`, publishing
   NodePorts **30080/30443** (exactly what `k3d-config.yaml` maps host 80/443 to),
   default TLS from that Secret, HTTP→HTTPS 301,
4. applies a `coredns-custom` ConfigMap so **pods** resolve `*.datum.local` to the
   ingress service too. This is not optional once identity lands: Keycloak checks
   the issuer URL in every token, so pod and browser must resolve the same name.

### One-time per machine (not scriptable, needs your password)

```bash
sudo pacman -S mkcert        # or brew install mkcert; other OSes: tls-dns-setup.md
mkcert -install              # create + trust the local CA, then restart the browser
echo "127.0.0.1 datum.local iam.datum.local apps.datum.local smoke.datum.local myapps.datum.local clickhouse.datum.local dagster.datum.local chat.datum.local" | sudo tee -a /etc/hosts
```

Hosts files cannot do wildcards, so every hostname is listed — when a new
application arrives, its hostname joins that line. That is the whole maintenance
cost. (`nslookup` ignores hosts files by design; check with `ping -c1 iam.datum.local`.)

### What this changes for operator development

`surface.host` values in manifests use `*.datum.local` (they already do). Once an
application publishes an Ingress with class `haproxy` — or once route generation
from `surface.host` lands (ticket `A-31`, platform/IAM side, not this operator) —
it is reachable at `https://<host>` with a padlock and no `-k`.

Until an app publishes routes, verify operator work with `kubectl get appman`,
`kubectl get pods`, and `kubectl port-forward` where a UI must be seen.

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
