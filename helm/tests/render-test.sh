#!/usr/bin/env bash
# Offline checks of the hugginghack chart: lint, the four envFrom combinations, the
# annotation passthrough, and the umbrella fixture's wiring. Needs helm and python3 with
# PyYAML (the repo's .venv has it). No cluster involved.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
chart="$here/../hugginghack"
python="${PYTHON:-$here/../../.venv/bin/python}"

helm lint --strict "$chart"

render() { helm template t "$chart" --show-only templates/deployment.yaml "$@"; }

read -r -d '' CHECK_ENVFROM <<'PY' || true
import sys, yaml
expected_cm, expected_secret, document = sys.argv[1], sys.argv[2], sys.stdin.read()
container = yaml.safe_load(document)["spec"]["template"]["spec"]["containers"][0]
refs = container.get("envFrom")
if not expected_cm and not expected_secret:
    assert "envFrom" not in container, f"envFrom must be absent, got {refs!r}"
else:
    wanted = ([{"configMapRef": {"name": expected_cm}}] if expected_cm else []) + (
        [{"secretRef": {"name": expected_secret}}] if expected_secret else [])
    assert refs == wanted, f"envFrom {refs!r} != {wanted!r}"
assert "env" not in container, "no inline env entries"
PY
check() { "$python" -c "$CHECK_ENVFROM" "$@"; }

for cm in "" shared-config; do
  for secret in "" shared-secrets; do
    render --set envFromConfigMap="$cm" --set envFromSecret="$secret" | check "$cm" "$secret"
    echo "ok  envFrom configmap='${cm}' secret='${secret}'"
  done
done

# The chart owns no ConfigMap or Secret, and only a Deployment and a Service.
kinds="$(helm template t "$chart" | grep '^kind:' | sort | tr '\n' ' ')"
[ "$kinds" = "kind: Deployment kind: Service " ] || { echo "unexpected kinds: $kinds"; exit 1; }
echo "ok  renders only a Deployment and a Service"

# Annotations: none by default, plain strings unchanged, template strings evaluated.
render | grep -q "annotations:" && { echo "annotations rendered without podAnnotations"; exit 1; }
render --set-string 'podAnnotations.plain=hello' | grep -q "plain: hello"
render -f <(printf 'podAnnotations:\n  computed: "{{ .Chart.Name }}"\n') | grep -qE "computed: '?hugginghack'?$"
echo "ok  podAnnotations pass through, templates evaluated"

# The private-CA mount appears only when a ConfigMap is named.
render | grep -q "ca-bundle" && { echo "ca-bundle mounted without caBundle.configMap"; exit 1; }
render --set caBundle.configMap=internal-ca | grep -q "name: \"internal-ca\""
echo "ok  caBundle mount only when set"

# The umbrella fixture wires the names through and its checksums follow the data.
fixture="$here/umbrella-fixture"
helm dependency build "$fixture" >/dev/null
helm lint --strict "$fixture" >/dev/null
one="$(helm template f "$fixture" --show-only charts/hugginghack/templates/deployment.yaml)"
two="$(helm template f "$fixture" --set global.sharedData.ALLOWED_HOSTS=changed --show-only charts/hugginghack/templates/deployment.yaml)"
echo "$one" | check shared-config shared-secrets
[ "$(echo "$one" | grep checksum/config)" != "$(echo "$two" | grep checksum/config)" ]
[ "$(echo "$one" | grep checksum/secret)" = "$(echo "$two" | grep checksum/secret)" ]
echo "ok  umbrella fixture: names flow into envFrom, checksum/config follows the ConfigMap"

# PostgreSQL switch: off renders no database; on adds a StatefulSet, its Service and a wait
# in the application pod; the password comes from the umbrella's Secret; the database pods
# never match the application's Service selector; on without a Secret refuses to render.
pg="$(helm template t "$chart" --set postgresql.enabled=true --set envFromSecret=shared-secrets)"
[ "$(echo "$pg" | grep '^kind:' | sort | tr '\n' ' ')" = "kind: Deployment kind: Service kind: Service kind: StatefulSet " ]
echo "$pg" | grep -q 'name: wait-for-postgresql'
echo "$pg" | "$python" -c '
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d]
app_svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "t-hugginghack")
pg_pod = next(d for d in docs if d["kind"] == "StatefulSet")["spec"]["template"]
env = {e["name"]: e for e in pg_pod["spec"]["containers"][0]["env"]}
assert env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"] == {"name": "shared-secrets", "key": "POSTGRES_PASSWORD"}
labels = pg_pod["metadata"]["labels"]
assert not all(labels.get(k) == v for k, v in app_svc["spec"]["selector"].items()), "app Service would select the database"
'
if helm template t "$chart" --set postgresql.enabled=true >/dev/null 2>&1; then echo "postgresql.enabled without envFromSecret rendered"; exit 1; fi
echo "ok  postgresql on/off: StatefulSet + Service + wait only when on, password from the Secret, refuses without one"
echo "All render checks passed."
