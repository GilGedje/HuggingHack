# helm/ — maintainer guide

The Helm chart that runs HuggingHack on Kubernetes and OpenShift. `helm/hugginghack` is a
**subchart**: the user's umbrella chart renders a ConfigMap and a Secret with HuggingHack's
settings and passes their names in; this chart consumes them through `envFrom`. The user
guide is [`README.md`](README.md) here; the scaling design behind it is
[`docs/SCALING.md`](../docs/SCALING.md). Root rules are in the root `CLAUDE.md`.

## Layout

| Path | What |
| --- | --- |
| `hugginghack/Chart.yaml` | name `hugginghack`, chart `version`, `appVersion` = the HuggingHack release (image tag fallback) |
| `hugginghack/values.yaml` | every value, commented; the comments are the reference |
| `hugginghack/templates/_helpers.tpl` | name/fullname/chart/labels/selectorLabels/serviceAccountName; PostgreSQL names, labels and `hugginghack.postgresql.podSecurityContext` |
| `hugginghack/templates/deployment.yaml` | HuggingHack: `envFrom`, probes, volumes, CA mount, init wait for PostgreSQL |
| `hugginghack/templates/service.yaml` | ClusterIP on `service.port` → named port `http` |
| `hugginghack/templates/postgresql.yaml` | only with `postgresql.enabled`: Service + StatefulSet (+ PVC template) |
| `hugginghack/README.md` | short version shipped inside the chart (`helm show readme`) |
| `README.md` | the full guide: umbrella wiring, ConfigMap vs Secret, OpenShift, restarts, tests |
| `tests/render-test.sh` | offline assertions (helm + the repo `.venv` python with PyYAML) |
| `tests/cluster-test.sh` | live test on a **local** cluster |
| `tests/cluster/deps.yaml` | MinIO and an external PostgreSQL for the live test (namespace `hh-helm-deps`) |
| `tests/route-test.sh` + `route-browser.mjs` | the production topology: TLS route → 3 replicas, bucket behind its own route, real Chrome |
| `tests/cluster/minio-route.yaml` | the bucket's route for `route-test.sh` |
| `tests/umbrella-fixture/` | TEST FIXTURE: a minimal umbrella in the user's pattern (values under `global`) |

## Commands (from the repo root)

```bash
helm lint --strict helm/hugginghack
helm/tests/render-test.sh                        # after ANY change to the chart
helm template hub helm/hugginghack --set envFromSecret=s --set postgresql.enabled=true
helm template hub helm/hugginghack --api-versions security.openshift.io/v1 ...   # render as on OpenShift

# Live, on the local Docker Desktop cluster only (see "Live testing" below)
KUBE_CONTEXT=docker-desktop LOAD_IMAGE_INTO=desktop-control-plane helm/tests/cluster-test.sh
KUBE_CONTEXT=docker-desktop PG_MODE=external helm/tests/cluster-test.sh
KUBE_CONTEXT=docker-desktop helm/tests/route-test.sh     # route + TLS + 3 replicas + browser
```

## Invariants — do not break

**The umbrella contract** (the user's own prompt defines it; keep it exact)
- `envFromConfigMap` and `envFromSecret` default to `""`. Each renders its `envFrom` entry
  only when set (`- configMapRef:` / `- secretRef:`, list dashes included); with both empty
  there is **no `envFrom:` key at all**, never `envFrom: []`.
- The chart renders **no ConfigMap and no Secret**, and no inline `env:` on the HuggingHack
  container. Every HuggingHack setting arrives through `envFrom`. (`INSTANCE_ID` needs no
  env: it defaults to the hostname, which is the pod name.)
- `podAnnotations` pass through `tpl (toYaml .) $`: plain strings unchanged, template strings
  evaluated in this chart's context. That is how the umbrella fixture's
  `{{ .Values.global.sharedData | toJson | sha256sum }}` checksum works. A subchart cannot
  read the umbrella's non-global values, which is why the README offers three restart methods.
- Resources rendered: Deployment and Service; plus StatefulSet and Service with
  `postgresql.enabled`. Nothing else (no Ingress/Route, HPA, ServiceAccount, NetworkPolicy,
  PDB): the umbrella owns those. `render-test.sh` asserts the exact kinds.

**Security (OpenShift restricted-v2 and Pod Security "restricted")**
- Never default `runAsUser`, `runAsGroup` or `fsGroup` for HuggingHack: OpenShift assigns them
  from the namespace range and refuses fixed ones outside it. The image's own user (1000)
  covers plain Kubernetes.
- Keep `runAsNonRoot`, `seccompProfile: RuntimeDefault`, `allowPrivilegeEscalation: false`,
  `capabilities.drop: [ALL]`, `readOnlyRootFilesystem: true`, and
  `automountServiceAccountToken: false`. Everything HuggingHack writes goes to the emptyDirs at
  `/data`, `/models` and `/tmp` (`HF_HOME` is under `/tmp`). A new write path needs a volume.
- PostgreSQL's official image starts as root. `hugginghack.postgresql.podSecurityContext`
  fills in uid/gid/fsGroup **70** (the image's `postgres` user) only when the cluster has no
  `security.openshift.io/v1` API and the user did not set them. On OpenShift nothing is filled
  in. `PGDATA` is a subdirectory of the volume so a platform-assigned UID can create and own it.
- The init container that waits for PostgreSQL runs on **HuggingHack's image** (non-root,
  a Python socket check), not PostgreSQL's (root, and `pg_isready` fails "no attempt" without
  `-U` when the UID has no passwd entry).

**Behaviour the app depends on**
- Probes send `Host: localhost`. The app's `AllowedHosts` (backend `main.py`) checks the
  **Host header** and always admits loopback names; kubelet's default Host is the pod IP,
  which a production `ALLOWED_HOSTS` would reject with 400.
- `service.targetPort` is the container port and must stay 7860 (the image's CMD).
- Rolling updates: `maxUnavailable: 0`, `maxSurge: 1`, `lifecycle.preStop` `sleep 10` (below
  `terminationGracePeriodSeconds: 60`). Without the pause, one request in ~28 failed during a
  rollout. Two pods overlap during an update, which is only safe with `CLUSTER_MODE=true`;
  single-server installs must use `strategy: {type: Recreate}` (documented, not enforced:
  the chart cannot see the ConfigMap).
- PostgreSQL selector labels use name `<name>-postgresql`, so the HuggingHack Service can never
  select the database pod. Keep the two selector sets disjoint (`render-test.sh` checks it).
- `postgresql.enabled` without `envFromSecret` must `fail` at render time: the server's
  password is `secretKeyRef` into that Secret (`postgresql.passwordKey`).

## Live testing

- **Only a local cluster.** The user's kubeconfig also holds shared OpenShift contexts (and its
  current context is one of them). Always pass `--context` / `--kube-context` explicitly, never
  switch the current context, and never deploy anywhere but the local cluster unless the user
  asks. The script refuses to run without `KUBE_CONTEXT`.
- The local cluster runs other workloads (`exodus-ai`, `ingress-nginx`): touch only the
  namespaces `hh-helm-test`, `hh-helm-deps` (and ad-hoc ones you create and delete).
- Docker Desktop's cluster is a kind node (`desktop-control-plane`) that cannot see host images:
  `LOAD_IMAGE_INTO=desktop-control-plane` imports `hugginghack:local`; remove it afterwards with
  `docker exec desktop-control-plane crictl rmi docker.io/library/hugginghack:local`.
- The fixture emulates OpenShift by setting a random high UID with group 0; the chart's own
  defaults (no UID) must also be installed as they are now and then (standalone, and with
  `postgresql.enabled`) because the fixture's overrides hide default-only failures.
- `route-test.sh` uses `*.127.0.0.1.nip.io` (Python cannot resolve `*.localhost` on macOS; do
  not edit `/etc/hosts`), port-forwards the ingress controller to 18443, and must never send a
  request with an unknown Host: the local cluster has another Ingress with host `*` that would
  answer it.
- What only the route test caught: `git clone` through several replicas failed ("Unable to
  find <object>"), because git read `info/refs` from one pod and objects from another that had
  no mirror yet. `GitMirrors.read_file` now refreshes the mirror from the system folder when an
  object is missing (only when mirrors are shared). Keep a multi-replica git clone in the tests.
- Traps met while writing the tests: a previous run's namespace still `Terminating` breaks the
  next install (the script waits for it); background port-forwards must be `kubectl` itself,
  not a shell function, or the trap cannot kill them; zsh does not split `$VAR` into words, so
  use functions for `kubectl --context …`; `pg_isready` without `-U` under a random UID.

## Recipes

**New value** — add it to `values.yaml` with a `# --` comment, use it in the template, add an
assertion to `render-test.sh` when it changes what renders, mention it in `README.md` (and in
`hugginghack/README.md` if it is a main value).

**New HuggingHack setting** — nothing to do here: settings go in the umbrella's ConfigMap or
Secret. Update `README.md`'s "What goes where" table if it is secret or needed in production.

**Release** — `appVersion` follows the app version (root `CLAUDE.md`: bump `config.py`,
`frontend/package.json` and `Chart.yaml` together). Bump the chart `version` whenever the
chart itself changes.
