# The ApplicationManifest contract

Every application on the platform declares itself in the same shape. This is that
shape: what each field promises, and who keeps the promise.

Schema: [`crd/applicationmanifest.yaml`](../crd/applicationmanifest.yaml), version
`v1alpha1`. The contract's home is now polaris
(`gitops/platform/application-manifest/`), merged there via PR #7 — the copy
here is verbatim from polaris **plus one prototype extension, `source`**, which
goes to polaris as its own PR before the operator moves.

```yaml
apiVersion: platform.datumlabs.io/v1alpha1
kind: ApplicationManifest
metadata:
  name: clickhouse
spec:
  source:
    chart: clickhouse
    repo: https://charts.bitnami.com/bitnami
    version: "6.2.16"  # pin it. always
    values: |
      shards: 1
      replicaCount: 1
  identity:
    driver: gateway
  entitlements:
    grantableObjects: [database, table, view]
    actions: [read, write, admin]
  reconciler:
    mode: push
  surface:
    host: clickhouse.datum.local
    launchUrl: https://clickhouse.datum.local/play
    embed: link
    healthCheck: /ping
```

## The fields

| Field | Promises | Required |
|---|---|---|
| `source` | which chart installs this application — repo, chart, pinned version | no |
| `identity` | that users can log in, and that accounts can be provisioned in | yes |
| `entitlements` | what there is to grant: the objects and actions permissions are made of | no |
| `surface` | that the platform can reach the application, and that my-apps can draw its tile | yes |
| `reconciler` | how a permission decision becomes real inside the application | no |
| `dependencies` | what must exist before it can start | no |
| `observability` | where its logs, metrics and traces go | no |
| `machineIdentity` | how the application authenticates as itself, not as a person | no |

### `source`

Where the application's software comes from: a Helm chart, named by `repo`,
`chart` and `version` — pinned always, because "whatever is newest" is a state
nobody can roll back to — plus optional `values`, the YAML string handed to
Helm as-is.

Grew at v4, because the operator could not install a chart without knowing
which chart, and a real requirement is the only thing that grows the contract.
An application that ships nothing to install (or is installed by other means)
omits it; the operator answers `phase: Unmanaged` and manages the namespace
only.

This is the one field the operator itself consumes end to end: it becomes a
`HelmChart` object that k3s's helm-controller installs. It is not yet in the
polaris contract nor in DPS P1 — taking it there is open work, alongside the
extensions already proposed in
[datum-standards#35](https://github.com/datumlabsio/datum-standards/issues/35).

A wrinkle this section used to record — the hostname appearing both in
`surface.host` and inside `source.values` as the chart's ingress — is closed
(2026-08-29): charts create no ingress, and the operator routes `surface.host`
itself, to the login gateway. A chart ingress on the same host would be an
unauthenticated door around it, which is exactly what the gateway rebuild
test caught.

### `identity`

Two questions live here since the polaris review (PR #7). `sync` and
`scimEndpoint` are DPS P1's provisioning question — how accounts get created
inside the application (`scim`, `custom` or `none`); optional until something
provisions accounts. `driver` is the authentication question — how a person
logs in — and is a Datum extension, proposed upstream in
[datum-standards#35](https://github.com/datumlabsio/datum-standards/issues/35).

| Driver | Pick it when | Proven by |
|---|---|---|
| `native-oidc` | the application logs people in itself. Cleanest — it knows who the user is | LibreChat |
| `gateway` | the application cannot speak OIDC itself. A proxy in front defers to the platform's Keycloak session and passes identity on — my-apps SSO carries through | Dagster, ClickHouse |
| `ldap-bridge` | native clients such as JDBC or a CLI must verify central credentials. **Nothing implements this yet** — Keycloak does not serve LDAP and no bridge is ticketed; the value declares a direction | — |

ClickHouse moved from `ldap-bridge` to `gateway` in the PR #7 review: humans
reach it through the auth proxy on its HTTP interface, native clients use
service accounts, and the reconciler needs neither — it writes grants with its
own admin account.

**`gateway` is implemented — by this operator** (`operator/gateway.py`,
2026-08-29). Declaring it deploys an oauth2-proxy in front of the
application's HTTP surface: the operator ensures a confidential OIDC client in
Keycloak via the admin API (idempotently — the realm imports once, so clients
that exist because manifests exist are reconciled, not imported), generates
the proxy's secrets, discovers the upstream by convention (the Service labeled
as the application's instance, port named `http`), and routes `surface.host`
to the proxy. One my-apps login opens every gateway application; verified both
directions. The proxy stamps `X-Forwarded-User` upstream — nothing consumes it
until the push reconciler does. `native-oidc` needs nothing deployed;
`ldap-bridge` stays unimplemented.

### `entitlements`

The menu: what exists inside the application that a permission can be about.
`grantableObjects` lists the kinds of things (ClickHouse: `database`, `table`,
`view`), `actions` lists what can be done to them (`read`, `write`, `admin`), and
`granularity` says how fine the platform can slice (`application`, `object`, or
`row` — everything today is `object`).

An application with no permission system of its own, such as Dagster, simply
omits this field. There is nothing to list.

Consumed by the permission graph, which uses it to render meaningful choices —
"read on the sales database" rather than an on/off switch for the whole
application. Not by this operator.

### `reconciler.mode`

The kitchen: once the platform decides "this team may read that database", how
does the decision become true inside the application? One of three, chosen by
what the application supports:

| Mode | Pick it when | Proven by |
|---|---|---|
| `push` | we run the application and it keeps its own grants; write them in natively (a real `GRANT` in ClickHouse) | ClickHouse |
| `delegate` | we do not own the application, but it exposes an IAM API we can call | LibreChat |
| `gate` | the application has no permission system at all, so the platform stops people at the front door | Dagster |

`gate` exists because Dagster had nothing to push grants into. The contract grew
to fit a real application rather than an imagined one. It is not yet in the
datum-standards contract, which allows only `push` and `delegate` — proposing it
upstream is open work.

The optional `reconciler.driver` names the piece of code that speaks the
application's grant dialect, once such code exists.

`entitlements` and `reconciler` were one field (`entitlements.mode`) until
2026-08-24. They were split to match the datum-standards contract: what is
grantable and how a grant is materialised have different consumers, and an
application needs to state both. Consumed by the permission graph and its
enforcement machinery. Not by this operator.

### `surface`

Since the polaris review, this is DPS P1's my-apps entry point plus one Datum
extension. `launchUrl` is what a tile on the my-apps home screen opens,
`embed` is how it opens (the spec never enumerates its values, so neither does
the schema — the my-apps design owns that), and `healthCheck` is how my-apps
tells whether the application is alive. `host` is the extension: the routing
answer, the hostname the platform serves the application on — proposed
upstream with the others in datum-standards#35.

The development cluster runs the team ingress layer (HAProxy, `*.datum.local`
TLS — see [`cluster.md`](cluster.md)), so `host` is consumed for real — by
this operator, which writes the Ingress itself. For a `gateway` application
the route lands on the login proxy, never directly on the application: one
hostname, one writer, one authenticated door.

### The three unspecified fields

`dependencies`, `observability` and `machineIdentity` are named in the contract
but have no agreed shape. They accept any content for now, so nobody is blocked
writing manifests, and so their shape gets decided by a real requirement rather
than a guess.

(`reconciler` used to be on this list, ambiguous between "how grants are
materialised" and "how the application is installed". Decided 2026-08-24: it
means grants, matching datum-standards. Installation got its own field at v4,
named `source` — exactly as predicted here.)

Each should be tightened the moment it is settled. Current best understanding:

- **`dependencies`** — what must exist first. ClickHouse needs Zookeeper;
  LibreChat needs Mongo and a search index. The PR #7 review fixed its shape
  from array to open object: DPS's own example carries `requires` and
  `namespace` keys.
- **`observability`** — where logs and metrics go. Collection happens at the node,
  so this likely only declares what the application emits.
- **`machineIdentity`** — service-to-service authentication. Distinct from
  `identity`, which is about people. Called NHI in tickets `A-19` and `A-20`.

## Where each field plugs in

| Field | Owned here | Consumed by |
|---|---|---|
| `source` | prototype extension, pending polaris | **this operator** → k3s helm-controller |
| `identity` | via polaris | Keycloak / SSO; `driver: gateway` → **this operator** |
| `entitlements` | via polaris | the permission graph |
| `reconciler` | via polaris | the permission graph's enforcement machinery |
| `surface` | via polaris | the ingress layer; my-apps tiles (`launchUrl`) |
| `observability` | via polaris | log collection and the audit store |
| `dependencies`, `machineIdentity` | via polaris | undecided |

This operator's responsibility is that every field exists, is honest, and is filled
in by applications that really run. What reads them is somebody else's work, and
the two only need to agree on the shape.

## Validation the cluster enforces

Applying a manifest is checked before any code sees it:

```
spec.surface: Required value

spec.identity.driver: Unsupported value: "magic":
  supported values: "native-oidc", "gateway", "ldap-bridge"
```

Two things worth knowing about how that behaves:

- **Unknown fields are rejected** by `kubectl apply`, which uses strict validation
  by default. With validation disabled they are silently pruned instead.
- **Validation only runs on write.** Nothing re-checks objects already stored, so
  tightening the schema leaves existing objects that quietly violate it. Code
  reading a manifest must not assume a required field is present.

## Why some fields are described and others are open

A `type: object` with no `properties` and no `x-kubernetes-preserve-unknown-fields`
permits nothing inside it — the only legal value is `{}`. Declaring the seven
fields without describing them produces a contract that cannot hold any content:

```
strict decoding error: unknown field "spec.entitlements.mode",
unknown field "spec.identity.driver", unknown field "spec.surface.host"
```

So each field is either described or explicitly opened. The five settled fields
(`source`, `identity`, `entitlements`, `reconciler`, `surface`) are described,
which means the cluster enforces the three drivers, the three modes and the
pinned chart coordinates rather than this document describing them. The three
unspecified fields are opened, because inventing a shape for them would be
worse than leaving them undecided.

## Changing this contract

The schema is a public contract. Changing it breaks people.

- Adding an optional field is safe.
- Making a field required, or removing one, is not.
- Moving off `v1alpha1` needs a served second version and conversion.

Record every change here, with the requirement that forced it. The contract
growing because a real application did not fit is the process working; growing
because someone imagined a need is how it becomes a configuration language nobody
can read.

### Changes

- **2026-08-24** — `mode` moved from `entitlements` to `reconciler`;
  `entitlements` became the list of grantable objects and actions;
  `reconciler` typed (`mode`, `driver`). Forced by the datum-standards
  contract ([DPS P1](https://github.com/datumlabsio/datum-standards/blob/main/standards/platform/service-manifest.md)),
  which puts push/delegate on `reconciler` — reconciled before any consumer
  built against the old shape. Only stored manifest (`clickhouse`) migrated by
  re-applying. `gate` kept as a local extension; proposing it upstream is open.
- **2026-08-28** — the contract's home moved to polaris
  (`gitops/platform/application-manifest/`, PR #7), and its review restored
  what this schema had silently replaced: `identity` regained `sync` and
  `scimEndpoint`, `surface` regained `launchUrl`, `embed` and `healthCheck`,
  `dependencies` became an open object. The extensions (`driver`, `gate`,
  `host`) are flagged and proposed in
  [datum-standards#35](https://github.com/datumlabsio/datum-standards/issues/35).
  ClickHouse's driver decided as `gateway`. This copy synced verbatim.
- **2026-08-28** — `source` added (`chart`, `repo`, `version`, `values`).
  Forced by v4: the operator installs software by delegating to Helm, and a
  chart cannot be installed without knowing which chart. Prototype extension —
  not yet in polaris or DPS; taking it there is open work.
- **2026-08-29** — no schema change, but two meanings landed. `identity.driver:
  gateway` became implemented behavior: the operator deploys the login proxy,
  ensures the Keycloak client, and reports the door in `status.gateway`.
  `surface.host` became the operator's alone to route: charts stopped writing
  ingresses (the clickhouse manifest's `source.values` dropped its ingress
  block), because a chart ingress on the gateway's host is an unauthenticated
  bypass. Verified end to end: SSO in both directions, single route, full
  rebuild from a cold cluster.

## Not yet covered

There is no conformance check per field. The contract is prose plus a schema, and
a protocol without a check is prose. Each field should eventually have a test
proving the platform actually honours it.
