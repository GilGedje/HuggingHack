# hugginghack

HuggingHack, a self-hosted Hugging Face-style model hub for air-gapped networks, as a subchart
of an umbrella chart. The umbrella renders the ConfigMap and Secret with HuggingHack's
settings and passes their names in; this chart consumes them through `envFrom`. The full
guide (what goes in the ConfigMap and the Secret, OpenShift, restarting pods on changes,
tests) is `helm/README.md` in the HuggingHack repository.

## Umbrella values

```yaml
hugginghack:
  envFromConfigMap: shared-config     # an existing ConfigMap; empty: no configMapRef
  envFromSecret: shared-secrets       # an existing Secret (DATABASE_URL, bucket keys…); empty: no secretRef
  replicaCount: 3                     # >1 needs CLUSTER_MODE=true, PostgreSQL and buckets
  image:
    repository: registry.internal/hugginghack
  postgresql:
    enabled: false                    # true: this chart also runs PostgreSQL (password: POSTGRES_PASSWORD in the Secret)
```

## Main values

| Value | Default | Purpose |
| --- | --- | --- |
| `envFromConfigMap` / `envFromSecret` | `""` | Names of the umbrella's ConfigMap and Secret |
| `podAnnotations` | `{}` | Passed through `tpl`, e.g. checksums that roll the pods on a settings change |
| `image.repository` / `image.tag` / `image.pullPolicy` | `hugginghack` / appVersion / `IfNotPresent` | The image |
| `replicaCount` | `1` | Pods |
| `service.port` / `service.targetPort` | `7860` / `7860` | Service port / the port HuggingHack listens on |
| `resources` | `{}` | Container resources |
| `strategy` | RollingUpdate, `maxUnavailable: 0` | Use `{type: Recreate}` for a single server without `CLUSTER_MODE` |
| `podSecurityContext` / `securityContext` | restricted, no fixed UID | OpenShift restricted-v2 and Pod Security "restricted" |
| `volumes.data` / `volumes.models` / `volumes.tmp` | `emptyDir` | Writable paths; persistent volumes for a single server keeping SQLite and models on disk |
| `caBundle.configMap` / `key` / `mountPath` | off / `ca.crt` / `/etc/hugginghack/ca` | Mount a private root CA from an existing ConfigMap |
| `lifecycle` | pre-stop `sleep 10` | Lets the Service drain a stopping pod |
| `postgresql.enabled` | `false` | Run PostgreSQL 17 in the release (StatefulSet, 20 Gi volume) |
| `imagePullSecrets`, `nodeSelector`, `tolerations`, `affinity`, `podLabels`, `partOf` | empty | Pass-throughs |

The chart renders a Deployment and a Service (plus PostgreSQL's StatefulSet and Service when
enabled) and never a ConfigMap or a Secret.
