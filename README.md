# platform-operator

A Kubernetes operator for the Datum platform. It watches for `ApplicationManifest`
objects — short YAML files describing how an application is reached and how it
handles identity — and makes the cluster match them.

The goal is that onboarding a new application becomes writing a YAML file rather
than running a project.

> Repo name is not final; it gets decided with the team.

## Quick start

Install Docker, `k3d`, `kubectl` and `helm`, and free ports 80 and 443.
Per-OS commands: [`docs/setup.md`](docs/setup.md).

```bash
git clone <repo> && cd platform-operator
./scripts/cluster-up.sh          # Linux, macOS, WSL, Git Bash
```

In PowerShell, run the same cluster definition directly:

```powershell
k3d cluster create --config k3d-config.yaml
kubectl config use-context k3d-datum-dev
```

Three nodes should come up `Ready`. To stop for the day, `./scripts/cluster-down.sh`
— add `--delete` to destroy it. Both scripts are safe to run twice.

## What is in here

| Path | What it holds |
|---|---|
| `k3d-config.yaml` | the cluster, as data — this file *is* the cluster definition |
| `crd/` | the `ApplicationManifest` schema: the contract every application declares itself in |
| `operator/` | the operator: watches manifests, makes the cluster match them |
| `manifests/` | one `ApplicationManifest` per application. Together, the Platform Registry |
| `scripts/` | cluster up and down, plus verification fixtures |
| `docs/` | setup, the contract, and cluster notes |

## Two things that will surprise you

**There is no ingress controller.** You cannot reach applications in a browser by
hostname yet — the platform Helm chart installs its own, and two controllers
cannot share ports 80 and 443. Deliberate, not missing. Use `kubectl` to check
your work. See [`docs/cluster.md`](docs/cluster.md).

**The cluster definition is not ours.** `k3d-config.yaml` mirrors the team
standard. Changing its name or port mappings here would fork it.

## Docs

- [`docs/setup.md`](docs/setup.md) — prerequisites per OS, WSL notes, verifying the setup
- [`docs/cluster.md`](docs/cluster.md) — what the cluster is, and the ingress gap
- [`docs/contract.md`](docs/contract.md) — what each `ApplicationManifest` field promises
