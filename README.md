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
./scripts/ingress-up.sh          # HAProxy + *.datum.local TLS on top of it
```

In PowerShell, run the same cluster definition directly:

```powershell
k3d cluster create --config k3d-config.yaml
kubectl config use-context k3d-datum-dev
```

Three nodes should come up `Ready`. To stop for the day, `./scripts/cluster-down.sh`
— add `--delete` to destroy it. Both scripts are safe to run twice.

## Run the operator

```bash
python -m venv .venv && .venv/bin/pip install -r operator/requirements.txt   # once
kubectl apply -f crd/applicationmanifest.yaml                                # once
.venv/bin/kopf run operator/main.py --verbose
```

Then, in another terminal, `kubectl apply -f manifests/clickhouse.yaml` and watch
`kubectl get appman` grow a `PHASE` column written by the operator. Kill the
operator and start it again — it re-reads every manifest and converges without
any event being replayed. That property (level-triggered reconciliation) is the
whole design.

## What is in here

| Path | What it holds |
|---|---|
| `k3d-config.yaml` | the cluster, as data — this file *is* the cluster definition |
| `cluster/` | the ingress layer: HAProxy values and in-cluster DNS, per the team setup |
| `crd/` | the `ApplicationManifest` schema: the contract every application declares itself in |
| `operator/` | the operator: watches manifests, makes the cluster match them |
| `manifests/` | one `ApplicationManifest` per application. Together, the Platform Registry |
| `scripts/` | cluster up and down, plus verification fixtures |
| `docs/` | setup, the contract, and cluster notes |

## Two things that will surprise you

**Hostnames need one-time machine setup.** `https://*.datum.local` works with a
padlock only after `mkcert -install` (trusts a local CA) and a hosts-file line —
hosts files cannot do wildcards. `./scripts/ingress-up.sh` prints exactly what to
run. See [`docs/cluster.md`](docs/cluster.md).

**The cluster definition is not ours.** `k3d-config.yaml` mirrors the team
standard. Changing its name or port mappings here would fork it.

## Docs

- [`docs/setup.md`](docs/setup.md) — prerequisites per OS, WSL notes, verifying the setup
- [`docs/cluster.md`](docs/cluster.md) — what the cluster is, ingress and TLS
- [`docs/contract.md`](docs/contract.md) — what each `ApplicationManifest` field promises
