# platform-operator

A Kubernetes operator for the Datum platform. It watches for `ApplicationManifest`
objects — short YAML files describing how an application is reached and how it
handles identity — and makes the cluster match them.

The goal is that onboarding a new application becomes writing a YAML file rather
than running a project.

> **Repo name is not final.** It gets decided with the team; renaming is cheap.

---

## Get a cluster

You need Docker, `k3d` and `kubectl` installed first. Per-OS instructions are in
the platform team's `k3d-local-cluster-setup.md`; the summary is in
[`docs/cluster.md`](docs/cluster.md).

**Linux, macOS, or Windows via WSL / Git Bash:**

```bash
./scripts/cluster-up.sh
```

**Windows in PowerShell:**

```powershell
k3d cluster create --config k3d-config.yaml
kubectl config use-context k3d-datum-dev
```

Either way you should get three nodes in `Ready`:

```
NAME                     STATUS   ROLES           AGE   VERSION
k3d-datum-dev-server-0   Ready    control-plane   18s   v1.35.5+k3s1
k3d-datum-dev-agent-0    Ready    <none>          15s   v1.35.5+k3s1
k3d-datum-dev-agent-1    Ready    <none>          15s   v1.35.5+k3s1
```

To stop for the day — contents kept, ports 80 and 443 released:

```bash
./scripts/cluster-down.sh
```

To destroy it completely:

```bash
./scripts/cluster-down.sh --delete
```

Both scripts are safe to run twice.

---

## The cluster definition lives in `k3d-config.yaml`

That file *is* the cluster: three nodes, Traefik disabled, host ports 80 and 443
mapped to node ports 30080 and 30443, API server on `127.0.0.1:6550`. It is the
team-standard `datum-dev` cluster from `k3d-local-cluster-setup.md`, expressed as
data instead of a long command line.

Keeping it as a file rather than a command means it is versioned, a change to it
is reviewable in a pull request, and `k3d cluster create --config` behaves the
same in bash, zsh and PowerShell.

**Do not change the name or the port mappings without agreeing it with the
platform team.** Ports 80/443 → node ports 30080/30443 are a contract: the
ingress controller the platform chart installs later publishes itself on exactly
those node ports.

The scripts add convenience on top — a prerequisite check, and create / start /
already-running handling — but they call the same config file. Nothing is
defined twice.

---

## What is in here

| Path | What it holds |
|---|---|
| `k3d-config.yaml` | the cluster, as data |
| `crd/` | the `ApplicationManifest` schema — the contract every application declares itself in |
| `operator/` | the operator: watches manifests, makes the cluster match them |
| `manifests/` | one `ApplicationManifest` per application. Together these are the Platform Registry |
| `scripts/` | cluster up and down, plus verification fixtures |
| `docs/` | the contract, the catalog, and cluster notes |

---

## Things that will surprise you

**There is no ingress controller in this cluster.** You cannot open applications
in a browser by hostname yet. This is deliberate, not missing — the platform Helm
chart installs its own, and two controllers cannot share ports 80 and 443. See
[`docs/cluster.md`](docs/cluster.md). Check your work with `kubectl` instead.

**The cluster definition is not ours.** It mirrors the team standard. Changing
ports or the name here would fork it.

---

## Verifying the setup

There is no ingress to test through, so the port path is proven one layer lower:
a temporary NodePort workload on 30080, reached over `localhost:80`.

```bash
kubectl apply -f scripts/portcheck.yaml
kubectl rollout status deployment/portcheck
curl http://localhost                      # nginx welcome page
kubectl delete -f scripts/portcheck.yaml   # cluster back to empty
```

`scripts/helmcheck.yaml` is a second fixture: it applies a single `HelmChart`
record and confirms k3s's built-in helm-controller installs a real chart from it.
That mechanism is what the operator uses to install applications, so it is worth
knowing it works before depending on it.

---

## Windows and WSL notes

The scripts require bash. In WSL they work normally, with three caveats:

1. **Line endings.** A file saved by a Windows editor gets CRLF, and bash fails
   with `/usr/bin/env: 'bash\r': No such file or directory`. Either clone with
   `git config --global core.autocrlf input`, or fix afterwards:
   `sed -i 's/\r$//' scripts/*.sh`
2. **Keep the repo in the WSL filesystem** (`~/projects/...`), not under
   `/mnt/c/...`. Cross-filesystem access is slow and mangles permissions.
3. **Enable Docker Desktop's WSL integration** for your distro, under
   Settings → Resources → WSL Integration. Without it `docker info` fails inside
   WSL even though Docker is running on Windows.

Ports 80 and 443 are shared with Windows, so anything listening there — IIS, for
example — blocks the cluster. Stop it from a Windows terminal, not from WSL.

---

## Related

- Cluster standard: the platform team's `k3d-local-cluster-setup.md`
- The contract each application declares: [`docs/contract.md`](docs/contract.md)
- Cluster notes and the ingress gap: [`docs/cluster.md`](docs/cluster.md)
