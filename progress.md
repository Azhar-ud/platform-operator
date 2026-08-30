# Progress — platform-operator (through 2026-08-31)

Session keypoints. Written before a context compaction; the full reasoning lives in
`docs/contract.md`, the commit messages, and the visual explainer artifact:
https://claude.ai/code/artifact/0710e4e0-0bf3-4ba4-8ba1-eb18a706e5a7

## Where things run

- **This repo** (`~/projects/platform-operator`): the operator prototype. Separate from
  **polaris** (`~/projects/polaris`) — never carry config between them. Contract's home
  is polaris (`gitops/platform/application-manifest/`); the local `crd/` copy is verbatim
  + one prototype extension (`source`).
- **Cluster `datum-dev`** (k3d): full stack — HAProxy ingress (`datum-platform`),
  Keycloak (`identity` chart, realm `datum`, iam.datum.local), my-apps
  (apps.datum.local), ClickHouse + gateway (`app-clickhouse`). Secrets generated
  locally, out-of-band (`datum-identity-admin`, `datum-my-apps`, `datum-local-ca`).
  my-apps chart was installed with a `--set-json hostAliases` override (values file
  records another machine's ingress ClusterIP — re-derive on reinstall).
- Run the operator: **in-cluster** — Deployment `platform-operator` in
  `datum-platform` (image `datum/platform-operator:0.1.0-dev`, k3d-imported;
  `deploy/` has RBAC + Deployment). Laptop dev loop still works:
  `kubectl scale deploy/platform-operator -n datum-platform --replicas=0`
  FIRST (two standalone operators double-handle), then
  `.venv/bin/kopf run operator/main.py --verbose`; scale back to 1 when done.
- Seed login: `dev-analyst` / seedPassword in `datum-identity-admin` secret.
  Keycloak admin: `admin` / password field, same secret.

## polaris PR #7 (merged) and the contract

- CRD review round with Usman: `identity` regained `sync`+`scimEndpoint`, `surface`
  regained `launchUrl`/`embed`/`healthCheck`, `dependencies` became an open object.
  Extensions (`driver`, `gate`, `host`) flagged; upstream proposal:
  **datumlabsio/datum-standards#35**. ClickHouse decided `driver: gateway`.
- gitops CI: kubeconform CRD-kind gap + reusable-paths API-group false positive were
  fixed by Humayun in `datumlabsio/actions` v0.20.2 (polaris pins it).

## Milestones (all committed + pushed on main)

- **v4 `37b7de0`** — `source` (chart/repo/version/values, prototype CRD extension):
  operator writes a HelmChart; k3s helm-controller installs. Same-namespace HelmChart
  (owner refs can't cross namespaces — doc said kube-system, deviated deliberately).
  Status observed on a 30s timer (Deployments **and** StatefulSets — Bitnami taught us).
- **v5 `82d378c`** — gateway: `identity.driver: gateway` deploys oauth2-proxy;
  Keycloak client `gateway-<app>` ensured via **admin API** (realm imports once —
  ADR-0006), get-or-create, secret never rotated. Upstream by convention (Service
  labeled instance=<name>, port `http`). Ingress ClusterIP read live. **Charts must not
  create ingresses on surface.host** — chart ingress = unauthenticated bypass (caught by
  the wipe-and-rebuild test); `surface.host` has one writer, the operator.
- **v6 `b0a39ee`** — push reconciler (`operator/reconciler.py`): 60s timer converges
  ClickHouse roles/users/grants from Keycloak realm roles (composite-expanded) +
  manifest `entitlements`. Pure policy (`desired_state`), set-based compare-then-fix
  (ClickHouse renders grants canonically), **ledger Secret `<app>-platform-users`** =
  ownership record + key store (keys are usernames, values generated passwords, made
  once, never rotated in place). Admin = data plane, never `ALL`. SQL reaches ClickHouse
  via the **API-server service proxy** (ingress is gateway-only), creds in headers.
  Verified: per-user auth, grants enforced, query_log names people, Keycloak demotion →
  REVOKE within a tick.
- **v7 `7636cfe`** — identity shim (`operator/shim.py`, ~140 lines stdlib): loopback-only
  (127.0.0.1) sidecar in the gateway pod, shipped as ConfigMap into pinned
  `python:3.12-alpine` (no registry). Maps **X-Forwarded-Preferred-Username** (NOT
  X-Forwarded-User = sub UUID — first browser test 403'd on that) → ledger file →
  injects X-ClickHouse-User/Key; strips all typed/forged creds (`user=`/`password=`
  params, X-ClickHouse-*, X-Forwarded-*). Ledger present but user absent → 403 by name;
  no ledger → passthrough (future gateway apps without push). Result: **one Keycloak
  login end to end, Play boxes empty**, verified in a cold incognito browser + full
  wipe-and-rebuild.

- **v8 (official operator, 2026-08-31)** — CTO decision: ClickHouse
  runs on the official ClickHouse operator (`clickhouse-operator-system`,
  chart v0.0.7, installed by hand with certManager/webhook/secure-metrics off —
  cert-manager is a real-install question). `source` is a union (`helm:` |
  `clickhouse:`, CEL exactly-one); `operator/warehouse.py` renders admin Secret
  (minted pre-birth) + KeeperCluster (image pinned = DB version; unpinned
  keeper → `:latest`) + ClickHouseCluster (image is `{repository, tag}`).
  Service: `<name>-clickhouse-headless`, port `http`. Three bugs caught by the
  rebuild: image-as-object, ledger named by APP not service, gateway
  `_upstream` must skip its own Service (self-loop, latent since v5).
  polaris PR #29 (open): source union, helm branch only. `ch-lab` ns = scratch
  lab, deletable. Cluster rebuild ORDER: ClickHouse operator before any
  `source.clickhouse` manifest. Verified live by Azhar.

- **v9 (in-cluster, 2026-08-31)** — the operator is a pod. Dual-mode identity
  (`load_incluster_config` first, kubeconfig fallback); `reconciler._api_auth`
  sends client cert OR the SA token read fresh per call (bound tokens rotate).
  `Dockerfile`: digest-pinned python:3.12-slim, uid 10001, polaris (dlt-pipeline)
  conventions. `deploy/rbac.yaml`: least privilege, execution-tested — the API
  server itself caught services create/patch (gateway writes its Service) and
  CRD read (kopf watches its own noun's definition). `deploy/deployment.yaml`:
  hostAliases iam.datum.local → ingress ClusterIP (hand-pasted, my-apps
  convention — re-derive on reinstall), `DATUM_CA=/etc/ssl/datum/ca.pem` from
  the `datum-local-ca` ConfigMap, restricted securityContext, IfNotPresent.
  Verified: wipe-and-rebuild fully in-cluster, laptop idle. (A rename to
  "jackdaw" was tested end-to-end and reverted same day — name stays;
  naming is a polaris team-conversation item.)

## Design facts worth not re-deriving

- Login SSO both directions; logout propagates in ≤5 min (token lifetime); sessions:
  access 300s / idle 30min / max 10h. Back-channel logout = future hardening.
- Steps 1–5 of the request flow run per request (stateless shim, file read per hit);
  only the Keycloak login is per session.
- No policy engine: desired state = Keycloak roles (who) × hardcoded
  `ACTION_BY_ROLE` dict (what a role means — THE seam for Usman's future permission
  graph, one function swap) × manifest entitlements (what's grantable).
- Fresh install settles ~1 min (first converge 45s + kubelet Secret projection).
- 60s reconcile = polling because nothing pushes; quiet ticks write nothing.
- Username reuse over time is the identity edge (sub-claim fix sketched) — noted in
  contract.md boundaries (uncommitted).
- **bitnamilegacy image = frozen archive, prototype-only** (ADR-0006 precedent). Exit:
  lane B = authored chart on official image (first draft in git at `37b7de0^`);
  lane C = Altinity operator when the warehouse gets serious (fixes ops, not just image;
  our operator would write ClickHouseInstallation CRs instead of HelmChart — retires
  nothing). Dagster/Kafka taxonomy: first-party charts fine; databases → operators;
  Bitnami *subcharts* are the sneaky case (Dagster bundles Bitnami PostgreSQL).
- Native-TCP door: closed on purpose; opening it is its own design conversation
  (options: keep closed / expose w/ passwords / mTLS / ldap-bridge revived). Most
  "I need DB access" asks are better answered by installing the tool as a platform app.

## Uncommitted right now

- nothing once v9 lands

## Next moves (agreed direction)

1. **Polaris migration** (IN PROGRESS): v9 in-cluster is DONE. Remaining:
   polaris PR #29 review (OPEN; Usman message DRAFTED, NOT SENT) → operator
   PR to polaris (placement per ADR-0005 = `gitops/charts/platform-operator/`
   + values in `gitops/values/`; add folder to CI `changed-paths` +
   `container-platform-operator` job + `publish-*` in release.yml with unique
   image-name; team questions: who reviews Python, ClickHouse-operator
   install as gitops dir not runbook step, `application/` naming collision).
2. **Dagster** (`manifests/dagster.yaml`) — second app, zero code changes expected;
   pre-known friction: Bitnami postgres subchart image, upstream port-name convention,
   hosts entry; exercises `gate` + shim passthrough. Independent — slots anywhere.
3. my-apps tile wiring (`surface.launchUrl` — my-apps roster is placeholder data),
   back-channel logout, native-TCP + rotation policy (one conversation),
   observability milestone (Prometheus+Grafana as platform apps; the operator
   reads the manifest's `observability` field and writes the scrape targets).

## Rebuild cheatsheet

- App only: `kubectl delete appman clickhouse` ⇄ `kubectl apply -f manifests/clickhouse.yaml`
- Platform: polaris runbook `docs/runbooks/my-apps-local.md` (secrets → CA → helm install
  identity, my-apps → CRD apply). New passwords each time.
- ClickHouse operator (needed BEFORE any source.clickhouse manifest):
  `helm install clickhouse-operator oci://ghcr.io/clickhouse/clickhouse-operator-helm
  --create-namespace -n clickhouse-operator-system --set certManager.enabled=false
  --set webhook.enabled=false --set metrics.secure=false`
- Operator image: `docker build -t datum/platform-operator:0.1.0-dev .`
  → `k3d image import datum/platform-operator:0.1.0-dev -c datum-dev`
  → `kubectl apply -f deploy/` → `kubectl rollout restart deploy/platform-operator -n datum-platform`.
  hostAliases IP in deploy/deployment.yaml must match the ingress ClusterIP.
- Cluster: `./scripts/cluster-down.sh [--delete]` / `cluster-up.sh` → `ingress-up.sh`.
  After boot: proxy CrashLoops until Keycloak wakes — normal, self-heals.
- 502 at the door = a hop is (re)starting (cold boot, pod roll); self-heals <1 min.
