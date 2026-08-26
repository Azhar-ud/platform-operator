# The ApplicationManifest contract

Every application on the platform declares itself in the same shape. This is that
shape: what each field promises, and who keeps the promise.

Schema: [`crd/applicationmanifest.yaml`](../crd/applicationmanifest.yaml), version
`v1alpha1`.

```yaml
apiVersion: platform.datumlabs.io/v1alpha1
kind: ApplicationManifest
metadata:
  name: clickhouse
spec:
  identity:
    driver: ldap-bridge
  entitlements:
    grantableObjects: [database, table, view]
    actions: [read, write, admin]
  reconciler:
    mode: push
  surface:
    host: clickhouse.datum.local
```

## The fields

| Field | Promises | Required |
|---|---|---|
| `identity` | that users can log in, even if the application cannot authenticate them itself | yes |
| `entitlements` | what there is to grant: the objects and actions permissions are made of | no |
| `surface` | that the application has one address the platform knows how to reach | yes |
| `reconciler` | how a permission decision becomes real inside the application | no |
| `dependencies` | what must exist before it can start | no |
| `observability` | where its logs, metrics and traces go | no |
| `machineIdentity` | how the application authenticates as itself, not as a person | no |

### `identity.driver`

How users log in. One of three values, chosen by what the application can do:

| Driver | Pick it when | Proven by |
|---|---|---|
| `native-oidc` | the application logs people in itself. Cleanest — it knows who the user is | LibreChat |
| `gateway` | the application has no login. A proxy in front handles it and passes identity on | Dagster |
| `ldap-bridge` | native clients such as JDBC or a CLI that will never speak OIDC | ClickHouse |

Consumed by the identity work — Keycloak, SSO, the login itself. Not by this operator.

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

### `surface.host`

The hostname the application is reached on. Becomes an Ingress rule once an
ingress controller exists — see `A-31`, which is not this operator's work.

There is no ingress controller in the development cluster today, so nothing
consumes this field yet. See [`cluster.md`](cluster.md).

### The three unspecified fields

`dependencies`, `observability` and `machineIdentity` are named in the contract
but have no agreed shape. They accept any content for now, so nobody is blocked
writing manifests, and so their shape gets decided by a real requirement rather
than a guess.

(`reconciler` used to be on this list, ambiguous between "how grants are
materialised" and "how the application is installed". Decided 2026-08-24: it
means grants, matching datum-standards. Installation — chart, repo, version —
gets its own field when it arrives at v4, likely named `source`.)

Each should be tightened the moment it is settled. Current best understanding:

- **`dependencies`** — an array of what must exist first. ClickHouse needs
  Zookeeper; LibreChat needs Mongo and a search index.
- **`observability`** — where logs and metrics go. Collection happens at the node,
  so this likely only declares what the application emits.
- **`machineIdentity`** — service-to-service authentication. Distinct from
  `identity`, which is about people. Called NHI in tickets `A-19` and `A-20`.

## Where each field plugs in

| Field | Owned here | Consumed by |
|---|---|---|
| `identity` | the field, its values, its validation | Keycloak / SSO |
| `entitlements` | same | the permission graph |
| `reconciler` | same | the permission graph's enforcement machinery |
| `surface` | same | the ingress controller (`A-31`) |
| `observability` | same | log collection and the audit store |
| `dependencies`, `machineIdentity` | same | undecided |

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

So each field is either described or explicitly opened. The four settled fields
(`identity`, `entitlements`, `reconciler`, `surface`) are described, which means
the cluster enforces the three drivers and three modes rather than this document
describing them. The three unspecified fields are opened, because inventing a
shape for them would be worse than leaving them undecided.

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

## Not yet covered

There is no conformance check per field. The contract is prose plus a schema, and
a protocol without a check is prose. Each field should eventually have a test
proving the platform actually honours it.
