# HuggingHack Helm chart

Everything for running HuggingHack on Kubernetes or OpenShift lives in this folder:

```
helm/
├── README.md                 this file
├── hugginghack/              the chart (a subchart of your umbrella)
│   ├── README.md             short version, packaged with the chart (helm show readme)
│   ├── Chart.yaml
│   ├── values.yaml
│   ├── .helmignore
│   └── templates/
│       ├── _helpers.tpl
│       ├── deployment.yaml   HuggingHack
│       ├── service.yaml
│       └── postgresql.yaml   only with postgresql.enabled
└── tests/
    ├── render-test.sh        offline checks (helm + python, no cluster)
    ├── cluster-test.sh       live test on a throwaway local cluster
    ├── cluster/deps.yaml     MinIO and an external PostgreSQL for the live test
    └── umbrella-fixture/     a minimal umbrella in your pattern, for the tests only
```

The chart renders a Deployment and a Service, plus a PostgreSQL StatefulSet and Service
when you switch the in-chart database on. It renders **no ConfigMap or Secret**: the umbrella
owns them and passes their names in, and every HuggingHack setting reaches the container
through `envFrom`.

## Integration with the umbrella

### Umbrella values

```yaml
# umbrella/Chart.yaml
dependencies:
  - name: hugginghack
    version: 0.1.0
    repository: file://../hugginghack        # or your chart repository

# umbrella/values.yaml
hugginghack:
  envFromConfigMap: shared-config            # the ConfigMap the umbrella renders
  envFromSecret: shared-secrets              # the Secret the umbrella renders
  replicaCount: 3                            # >1 needs CLUSTER_MODE=true (below)
  image:
    repository: registry.internal/hugginghack
    tag: "1.3.0"
  imagePullSecrets:
    - name: registry-pull
  caBundle:
    configMap: internal-root-ca              # optional: your private root CA
    key: ca.crt
  postgresql:
    enabled: false                           # true: the chart also runs PostgreSQL
```

The names flow straight through: `hugginghack.envFromConfigMap` becomes
`envFrom[].configMapRef.name` and `hugginghack.envFromSecret` becomes
`envFrom[].secretRef.name` in the Deployment. Either one empty leaves its entry out; both
empty leaves out `envFrom` entirely, and HuggingHack then starts with its built-in defaults
(SQLite and a local folder). Every key in the ConfigMap and Secret becomes an environment
variable with the same name, so the umbrella's keys must be HuggingHack's setting names
(`.env.example` in the repository lists them all).

### What goes where

A production setup (several servers, PostgreSQL, NetApp S3 behind a private CA):

| ConfigMap (`sharedData`) | Secret (`sharedSecrets`) |
| --- | --- |
| `CLUSTER_MODE: "true"` | `DATABASE_URL` (it holds the database password) |
| `ACCOUNTS_ENABLED: "true"` | the bucket keys named by `access_key_env` / `secret_key_env`, e.g. `GRID_KEY`, `GRID_SECRET` |
| `PUBLIC_URL: https://hub.example.internal` | `OIDC_CLIENT_SECRET` |
| `ALLOWED_HOSTS: hub.example.internal` | `RUNTIME_API_TOKEN` (if used) |
| `FORWARDED_ALLOW_IPS: 10.128.0.0/14` (the ingress/router pod network; ranges work) | `POSTGRES_PASSWORD` (only with `postgresql.enabled`) |
| `DEFAULT_STORAGE_TARGET: grid`, `SYSTEM_STORAGE_TARGET: grid` | |
| `STORAGE_TARGETS_JSON` (endpoint, `public_endpoint_url`, `ca_bundle: /etc/hugginghack/ca/ca.crt`, `direct_downloads`, `direct_uploads`) | |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_PROVIDER_NAME`, `OIDC_CA_BUNDLE: /etc/hugginghack/ca/ca.crt` | |
| `DATABASE_POOL_SIZE` | |

Budget PostgreSQL connections at `replicas × (2 × DATABASE_POOL_SIZE + 1)` (see
`docs/SCALING.md`). Direct uploads also need the bucket's CORS policy
(`docs/SERVE_FROM_S3.md`, "Direct uploads") and every uploading browser must trust the CA.

### PostgreSQL: your own server, or one run by the chart

HuggingHack always connects with `DATABASE_URL` from the umbrella's Secret.

- **`postgresql.enabled: false`** (default): `DATABASE_URL` points at your PostgreSQL server,
  e.g. `postgresql://hugginghack:<password>@pg.example.internal:5432/hugginghack`. The chart
  runs no database.
- **`postgresql.enabled: true`**: the chart also runs PostgreSQL 17 (one pod, a StatefulSet
  with a 20 Gi volume by default, Service `<release>-hugginghack-postgresql`). Point
  `DATABASE_URL` at it and put the same password under `POSTGRES_PASSWORD` in the Secret
  (key `postgresql.passwordKey`), e.g. for a release named `hub`:
  ```yaml
  sharedSecrets:
    DATABASE_URL: postgresql://hugginghack:S3cret@hub-hugginghack-postgresql:5432/hugginghack
    POSTGRES_PASSWORD: S3cret
  ```
  HuggingHack's pods wait for it to answer before starting. It is one pod, not a replicated
  database; for high availability run your own and leave this off. Mirror
  `postgres:17-alpine` into your registry (`postgresql.image`) for an air-gapped cluster.
  Its volume (PVC `data-<release>-hugginghack-postgresql-0`) is kept by `helm uninstall`, so
  the data survives a reinstall; delete the PVC yourself to start over.

### Restart pods when settings change

The umbrella owns the ConfigMap and Secret, so this chart cannot checksum them, and a
subchart cannot read the umbrella's own values. `podAnnotations` is passed through and
rendered with `tpl`, which gives three ways to roll the pods on a change:

1. **Keep the shared values under `global`** (globals reach subcharts), render the ConfigMap
   and Secret from `.Values.global.sharedData` / `.Values.global.sharedSecrets`, and let the
   chart compute the checksums:
   ```yaml
   hugginghack:
     podAnnotations:
       checksum/config: '{{ .Values.global.sharedData | toJson | sha256sum }}'
       checksum/secret: '{{ .Values.global.sharedSecrets | toJson | sha256sum }}'
   ```
   The test fixture does exactly this; the live test changes a value and watches the pods
   roll with no failed request.
2. **Keep `sharedData` at the top level** and pass a checksum computed in CI, e.g.
   `--set-string hugginghack.podAnnotations.checksum/config=$(sha256sum values.yaml | cut -c1-64)`.
3. **Use Stakater Reloader** if the cluster runs it:
   `podAnnotations: {reloader.stakater.com/auto: "true"}`.

### Test the chart on its own

```bash
helm lint --strict helm/hugginghack
helm template hub helm/hugginghack                                   # no envFrom at all
helm template hub helm/hugginghack --set envFromConfigMap=shared-config \
  --set envFromSecret=shared-secrets                                   # both refs
helm template hub helm/hugginghack --set envFromSecret=shared-secrets \
  --set postgresql.enabled=true                                        # with the chart's PostgreSQL
helm/tests/render-test.sh                                            # all of the above, asserted
```

## OpenShift

The defaults follow the restricted-v2 SCC and Kubernetes Pod Security "restricted":

- No `runAsUser`, `runAsGroup` or `fsGroup` is set; OpenShift assigns them from the
  namespace's range. Both HuggingHack and the chart's PostgreSQL run under such a random
  UID (tested with UID 1000710000, group 0).
- On plain Kubernetes the defaults work as they are: HuggingHack's image runs as its own
  non-root user (1000), and for the chart's PostgreSQL, whose official image would start as
  root, the chart fills in the image's `postgres` user (70) unless it detects OpenShift
  (`security.openshift.io/v1`) or you set the user yourself. `helm template` cannot see the
  cluster, so pass `--api-versions security.openshift.io/v1` when rendering for OpenShift.
- `runAsNonRoot`, `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`,
  `capabilities: drop [ALL]`, `readOnlyRootFilesystem: true`. Everything HuggingHack writes
  goes to emptyDirs at `/data`, `/models` and `/tmp`; PostgreSQL writes to its volume,
  `/var/run/postgresql` and `/tmp`.
- No ServiceAccount token is mounted (`automountServiceAccountToken: false`).
- Expose it with a Route (or Ingress) from the umbrella; TLS terminates there. Set
  `FORWARDED_ALLOW_IPS` to the router's pod network so HuggingHack sees real client addresses.
- A private root CA: mount it with `caBundle.configMap` (OpenShift's injected trusted-CA
  ConfigMaps work, key `ca-bundle.crt`) and name the file in `STORAGE_TARGETS_JSON`'s
  `ca_bundle` and `OIDC_CA_BUNDLE`.

## Behaviour worth knowing

- **Probes** call `/api/health` with `Host: localhost`, which `ALLOWED_HOSTS` always accepts,
  so they keep working once `ALLOWED_HOSTS` lists your real host names.
- **Rolling updates** start the new pod first (`maxUnavailable: 0`) and a stopping pod waits
  10 s before shutting down (`lifecycle.preStop`) so the Service stops sending it requests;
  the live test saw zero failed requests during a rollout. This needs `CLUSTER_MODE=true`.
  A single server without cluster mode (SQLite, local models) must use
  `strategy: {type: Recreate}` and persistent volumes (`volumes.data`, `volumes.models`).
- **Replicas:** more than one requires `CLUSTER_MODE=true`, PostgreSQL and buckets for all
  storage. A misconfigured pod refuses to start and logs why.

## Where this chart differs from the generic subchart template

| Template said | This chart | Why |
| --- | --- | --- |
| `service.targetPort` default 8080 | 7860 | The HuggingHack image listens on 7860. |
| Plain `podAnnotations` passthrough | Passed through `tpl` | Lets the umbrella compute checksums from `global` values; plain strings are unchanged. |
| Deployment and Service only | Plus PostgreSQL StatefulSet + Service when `postgresql.enabled` | Asked for: an optional database pod. Off by default. |
| Minimal values | Plus security contexts, probes, volumes, `caBundle`, `lifecycle`, `strategy`, pass-throughs | OpenShift restricted-v2, a read-only root filesystem, a private CA, and zero-downtime rollouts. |

## Tests

```bash
helm/tests/render-test.sh                                    # offline
KUBE_CONTEXT=docker-desktop LOAD_IMAGE_INTO=desktop-control-plane helm/tests/cluster-test.sh
KUBE_CONTEXT=docker-desktop PG_MODE=external helm/tests/cluster-test.sh
```

The chart's defaults were also installed as they are (no security overrides) in a
"restricted" namespace: standalone with no `envFrom`, and with `postgresql.enabled` (HuggingHack
as uid 1000 creating its tables in the chart's PostgreSQL running as uid 70), plus the
private-CA mount (read-only at `/etc/hugginghack/ca/ca.crt`).

The live test only touches the namespaces `hh-helm-test` and `hh-helm-deps` and deletes them
afterwards (`KEEP=1` leaves them). Always name a local context; never point it at a shared
cluster. It checks, with 2 replicas in cluster mode under Pod Security "restricted" and a
random UID: pods admitted and ready; a 40 MiB direct upload to MinIO pulled back through the
Service with a matching hash; exactly one leader; no permission errors; a settings change
rolls the pods with every health check answering 200; deleting the leader's pod hands
leadership to another.
