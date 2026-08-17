# Setup

What to install before `./scripts/cluster-up.sh` will work.

On Linux and macOS you can skip most of this: run the script and it tells you
what is missing, with the install command for your OS.

Full per-OS detail and troubleshooting lives in the platform team's
`k3d-local-cluster-setup.md`. This page is the short version.

## Docker

| | |
|---|---|
| Linux | install the engine, then `sudo usermod -aG docker $USER && newgrp docker` |
| macOS | Docker Desktop — Settings, **at least 8 GB of memory** |
| Windows | Docker Desktop, **WSL 2 backend**, 8 GB. Enable WSL integration for your distro under Settings → Resources |

It must be *running*, not just installed: `docker run --rm hello-world`.

## k3d, kubectl, helm

`helm` is not used by these scripts, but is needed for platform work.

**Linux**

```bash
curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash

curl -LO "https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -m 0755 kubectl /usr/local/bin/kubectl && rm kubectl

curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-4 | bash
```

**macOS**

```bash
brew install k3d kubectl helm
```

**Windows** — whichever package manager you have:

```powershell
winget install -e --id k3d.k3d; winget install -e --id Kubernetes.kubectl; winget install -e --id Helm.Helm
choco install k3d kubernetes-cli kubernetes-helm -y
scoop install k3d kubectl helm
```

Open a new terminal afterwards, then check:

```bash
k3d version && kubectl version --client && helm version
```

## Ports 80 and 443 must be free

Cluster creation fails if either is taken, and k3d cannot change port mappings
afterwards — a cluster built without them has to be deleted and rebuilt.

| | Check | Usual culprit |
|---|---|---|
| Linux | `sudo ss -lptn 'sport = :80'` | nginx or Apache — `sudo systemctl stop nginx` |
| macOS | `sudo lsof -iTCP:80 -sTCP:LISTEN -n -P` | built-in Apache — `sudo apachectl stop` |
| Windows | `netstat -ano \| findstr ":80 "` | IIS — `net stop w3svc`. If the owner is `System`, that is `http.sys` — `net stop http` |

**Do not work around a conflict by mapping different host ports.** Platform URLs,
Keycloak's issuer URL and every OIDC redirect assume 80 and 443. If you genuinely
cannot free them, raise it — it needs a decision, not a local workaround.

## WSL

The scripts require bash, which WSL provides. Three caveats:

1. **Line endings.** A file saved by a Windows editor gets CRLF and bash fails
   with `/usr/bin/env: 'bash\r': No such file or directory`. Clone with
   `git config --global core.autocrlf input`, or fix after:
   `sed -i 's/\r$//' scripts/*.sh`
2. **Keep the repo in the WSL filesystem** (`~/projects/...`), not `/mnt/c/...`.
   Cross-filesystem access is slow and mangles permissions.
3. **Docker Desktop needs WSL integration enabled** for your distro, or
   `docker info` fails inside WSL even though Docker is running on Windows.

Ports 80 and 443 are shared with Windows, so stop conflicting services from a
Windows terminal, not from WSL.

## Verifying the setup

There is no ingress controller to test through, so the port path is proven one
layer lower: a temporary NodePort workload on 30080, reached over `localhost:80`.

```bash
kubectl apply -f scripts/portcheck.yaml
kubectl rollout status deployment/portcheck
curl http://localhost                      # nginx welcome page
kubectl delete -f scripts/portcheck.yaml   # cluster back to empty
```

`scripts/helmcheck.yaml` is a second fixture: it applies a single `HelmChart`
record and confirms k3s's built-in helm-controller installs a real chart from it.
That mechanism is how the operator installs applications, so it is worth knowing
it works before depending on it.
