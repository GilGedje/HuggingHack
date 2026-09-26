#!/usr/bin/env bash
# Live test of the hugginghack chart on a throwaway local cluster (kind, Docker Desktop,
# minikube): two HuggingHack servers in cluster mode through the umbrella fixture, with
# in-cluster PostgreSQL and MinIO, under Pod Security "restricted" and an OpenShift-style
# random UID. Never run it against a shared cluster.
#
#   KUBE_CONTEXT=docker-desktop helm/tests/cluster-test.sh
#
# Optional: LOAD_IMAGE_INTO=<kind node container> loads hugginghack:local into that node
# (Docker Desktop's kind node is desktop-control-plane); KEEP=1 leaves everything running;
# PG_MODE=external uses the PostgreSQL in cluster/deps.yaml instead of the chart's own
# (postgresql.enabled=false, DATABASE_URL pointing outside the release).
set -euo pipefail
: "${KUBE_CONTEXT:?Set KUBE_CONTEXT to a local test cluster}"
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-$here/../../.venv/bin/python}"
ns=hh-helm-test
deps=hh-helm-deps
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
h() { helm --kube-context "$KUBE_CONTEXT" "$@"; }
pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != 1 ]; then
    h uninstall hh -n "$ns" >/dev/null 2>&1 || true
    k delete namespace "$ns" "$deps" --wait=false >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

if [ -n "${LOAD_IMAGE_INTO:-}" ]; then
  echo "== loading hugginghack:local into $LOAD_IMAGE_INTO"
  docker save hugginghack:local | docker exec -i "$LOAD_IMAGE_INTO" ctr -n k8s.io images import - >/dev/null
fi

echo "== waiting for any earlier run's namespaces to be gone"
for _ in $(seq 1 120); do
  k get namespace "$ns" "$deps" >/dev/null 2>&1 || k get namespace "$ns" >/dev/null 2>&1 || k get namespace "$deps" >/dev/null 2>&1 || break
  sleep 2
done

echo "== PostgreSQL and MinIO"
k apply -f "$here/cluster/deps.yaml" >/dev/null
k -n "$deps" rollout status deploy/postgres deploy/minio --timeout=180s >/dev/null

echo "== namespace $ns under Pod Security 'restricted'"
k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
k label namespace "$ns" --overwrite pod-security.kubernetes.io/enforce=restricted pod-security.kubernetes.io/warn=restricted >/dev/null

echo "== install the umbrella fixture (2 replicas, CLUSTER_MODE)"
h dependency build "$here/umbrella-fixture" >/dev/null
pg_args=()
if [ "${PG_MODE:-chart}" = external ]; then
  pg_args=(--set hugginghack.postgresql.enabled=false
           --set global.sharedSecrets.DATABASE_URL=postgresql://hugginghack:fixture-db-password@postgres.hh-helm-deps.svc:5432/hugginghack)
fi
echo "   PostgreSQL: ${PG_MODE:-chart}"
h upgrade --install hh "$here/umbrella-fixture" -n "$ns" "${pg_args[@]}" --wait --timeout 5m >/dev/null
k -n "$ns" get pods -l app.kubernetes.io/name=hugginghack

# Ready pods that are not shutting down.
pods() {
  k -n "$ns" get pods -l app.kubernetes.io/name=hugginghack -o json | "$python" -c '
import json, sys
for pod in json.load(sys.stdin)["items"]:
    ready = any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
    if ready and not pod["metadata"].get("deletionTimestamp"):
        print(pod["metadata"]["name"], end=" ")'
}
leader_of() {
  k -n "$ns" exec "$1" -- python -c '
import json, urllib.request, http.cookiejar
jar = http.cookiejar.CookieJar(); opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
headers = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:7860"}
body = json.dumps({"username": "owner", "password": "fixture-owner-password"}).encode()
opener.open(urllib.request.Request("http://127.0.0.1:7860/api/auth/login", body, headers))
print(json.load(opener.open("http://127.0.0.1:7860/api/health"))["cluster"]["leader"])'
}

echo "== owner, a direct upload and a pull through the Service"
# kubectl itself, not the k() wrapper, so the trap can stop it.
kubectl --context "$KUBE_CONTEXT" -n "$ns" port-forward svc/hh-hugginghack 17860:7860 >/dev/null 2>&1 & pids+=($!)
kubectl --context "$KUBE_CONTEXT" -n "$deps" port-forward svc/minio 9700:9000 >/dev/null 2>&1 & pids+=($!)
for _ in $(seq 1 30); do
  curl -sf -o /dev/null http://127.0.0.1:17860/api/health && curl -sf -o /dev/null http://127.0.0.1:9700/minio/health/live && break
  sleep 1
done
"$python" - <<'PY'
import hashlib, os, tempfile, httpx
base = "http://127.0.0.1:17860"
c = httpx.Client(base_url=base, headers={"Origin": base}, timeout=120)
r = c.post("/api/auth/setup", json={"username": "owner", "display_name": "Owner", "password": "fixture-owner-password"})
if r.status_code == 409:
    r = c.post("/api/auth/login", json={"username": "owner", "password": "fixture-owner-password"})
r.raise_for_status(); c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
repo = c.post("/api/uploads/repositories", json={"slug": "cluster-check", "visibility": "public"}).json()["repo_id"]
weights = os.urandom(40 * 1024 * 1024)
for path, data in (("config.json", b'{"model_type":"llama"}'), ("model.safetensors", weights)):
    params = {"repo_id": repo}
    state = c.post("/api/uploads/repositories/files/begin", params=params, json={"path": path, "size": len(data)}).json()
    assert state["direct"], state
    if state["part_count"]:
        links = c.post("/api/uploads/repositories/files/parts", params=params,
                       json={"path": path, "parts": list(range(1, state["part_count"] + 1))}).json()
        for part in links["parts"]:
            start = (part["number"] - 1) * state["part_size"]
            assert httpx.put(part["url"], content=data[start:start + part["size"]]).status_code == 200
    c.post("/api/uploads/repositories/files/complete", params=params, json={"path": path}).raise_for_status()
c.post("/api/uploads/repositories/finalize", params={"repo_id": repo}, json={}).raise_for_status()
os.environ.update(HF_ENDPOINT=base, HF_HOME=tempfile.mkdtemp(), HF_HUB_DISABLE_TELEMETRY="1")
from huggingface_hub import hf_hub_download
pulled = open(hf_hub_download(repo, "model.safetensors"), "rb").read()
assert hashlib.sha256(pulled).digest() == hashlib.sha256(weights).digest()
print(f"ok  {repo}: 40 MiB uploaded straight to MinIO and pulled back through the Service, hashes match")
PY

echo "== exactly one leader"
leaders=0; for pod in $(pods); do [ "$(leader_of "$pod" 2>/dev/null || true)" = True ] && leaders=$((leaders + 1)); done
[ "$leaders" = 1 ] || { echo "expected one leader, found $leaders"; exit 1; }
echo "ok  one leader among $(pods | wc -w | tr -d ' ') pods"

echo "== no permission problems (read-only root, random UID)"
if k -n "$ns" logs -l app.kubernetes.io/name=hugginghack --tail=-1 | grep -iE "PermissionError|Read-only file system|Traceback"; then exit 1; fi
k -n "$ns" exec "$(pods | cut -d' ' -f1)" -- id
echo "ok  no permission errors in the logs"

echo "== a settings change rolls the pods one at a time without downtime"
k -n "$ns" run poller --image=curlimages/curl --restart=Never --overrides='{"spec":{"securityContext":{"runAsNonRoot":true,"runAsUser":100,"seccompProfile":{"type":"RuntimeDefault"}},"containers":[{"name":"poller","image":"curlimages/curl","securityContext":{"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}},"command":["sh","-c","while true; do curl -s -o /dev/null -w \"%{http_code}\\n\" -H \"Host: localhost\" --max-time 2 http://hh-hugginghack:7860/api/health; sleep 0.5; done"]}]}}' >/dev/null
k -n "$ns" wait --for=condition=Ready pod/poller --timeout=60s >/dev/null
before="$(k -n "$ns" get deploy hh-hugginghack -o jsonpath='{.spec.template.metadata.annotations.checksum/config}')"
h upgrade hh "$here/umbrella-fixture" -n "$ns" --reuse-values --set global.sharedData.ROLL_CHECK="$(date +%s)" --wait --timeout 5m >/dev/null
after="$(k -n "$ns" get deploy hh-hugginghack -o jsonpath='{.spec.template.metadata.annotations.checksum/config}')"
[ "$before" != "$after" ] || { echo "checksum/config did not change"; exit 1; }
k -n "$ns" rollout status deploy/hh-hugginghack --timeout=5m >/dev/null
sleep 3
answers="$(k -n "$ns" logs poller)"; k -n "$ns" delete pod poller --wait=false >/dev/null
total="$(echo "$answers" | wc -l | tr -d ' ')"; failed="$(echo "$answers" | grep -vc '^200$' || true)"
[ "$failed" = 0 ] || { echo "$failed of $total health checks failed during the rollout: $(echo "$answers" | grep -v '^200$' | sort | uniq -c | tr '\n' ' ')"; exit 1; }
echo "ok  checksum/config changed, pods rolled, $total health checks all 200"

echo "== the leader's pod is deleted: another takes over"
leader=""
for _ in $(seq 1 15); do
  for pod in $(pods); do [ "$(leader_of "$pod" 2>/dev/null || true)" = True ] && leader="$pod"; done
  [ -n "$leader" ] && break; sleep 2
done
[ -n "$leader" ] || { echo "no leader after the rollout"; exit 1; }
k -n "$ns" delete pod "$leader" --wait=false >/dev/null
for _ in $(seq 1 30); do
  sleep 2
  for pod in $(pods); do
    [ "$pod" != "$leader" ] && [ "$(leader_of "$pod" 2>/dev/null || true)" = True ] && { echo "ok  $pod took over from $leader"; exit 0; }
  done
done
echo "no pod took over from $leader"; exit 1
