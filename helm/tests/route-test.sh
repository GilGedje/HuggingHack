#!/usr/bin/env bash
# The production topology on a throwaway local cluster: a DNS name and TLS at the route, the
# route to a multi-replica hugginghack Service, and the bucket behind its own route for direct
# transfers. Clients use real host names (*.127.0.0.1.nip.io) and the test CA, as machines
# with the root CA installed would.
#
#   KUBE_CONTEXT=docker-desktop helm/tests/route-test.sh
#
# Needs an ingress-nginx controller in the cluster (it plays the OpenShift router's part),
# helm, kubectl, openssl, the repo .venv (httpx, huggingface_hub), git with git-lfs, Node and
# Google Chrome (playwright-core is installed into a temp folder when missing).
# Optional: LOAD_IMAGE_INTO=<kind node>, REPLICAS (default 3), KEEP=1.
set -euo pipefail
: "${KUBE_CONTEXT:?Set KUBE_CONTEXT to a local test cluster}"
here="$(cd "$(dirname "$0")" && pwd)"
python="${PYTHON:-$here/../../.venv/bin/python}"
ns=hh-helm-test
deps=hh-helm-deps
port=18443
hub=hub.127.0.0.1.nip.io
s3=s3.127.0.0.1.nip.io
base="https://$hub:$port"
replicas="${REPLICAS:-3}"
work="$(mktemp -d)"
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
h() { helm --kube-context "$KUBE_CONTEXT" "$@"; }
pids=()
cleanup() {
  for pid in "${pids[@]:-}"; do [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; done
  if [ "${KEEP:-0}" != 1 ]; then
    h uninstall hh -n "$ns" >/dev/null 2>&1 || true
    k delete namespace "$ns" "$deps" --wait=false >/dev/null 2>&1 || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

echo "== waiting for any earlier run's namespaces to be gone"
for _ in $(seq 1 120); do
  k get namespace "$ns" >/dev/null 2>&1 || k get namespace "$deps" >/dev/null 2>&1 || break
  sleep 2
done
if [ -n "${LOAD_IMAGE_INTO:-}" ]; then
  echo "== loading hugginghack:local into $LOAD_IMAGE_INTO"
  docker save hugginghack:local | docker exec -i "$LOAD_IMAGE_INTO" ctr -n k8s.io images import - >/dev/null
fi

echo "== a test CA and a certificate for $hub and $s3"
openssl req -x509 -newkey rsa:2048 -nodes -keyout "$work/ca.key" -out "$work/ca.pem" -days 2 -subj "/CN=HuggingHack route test CA" 2>/dev/null
openssl req -newkey rsa:2048 -nodes -keyout "$work/tls.key" -out "$work/tls.csr" -subj "/CN=$hub" 2>/dev/null
printf "subjectAltName=DNS:%s,DNS:%s\nextendedKeyUsage=serverAuth\n" "$hub" "$s3" > "$work/ext.cnf"
openssl x509 -req -in "$work/tls.csr" -CA "$work/ca.pem" -CAkey "$work/ca.key" -CAcreateserial -out "$work/tls.crt" -days 2 -extfile "$work/ext.cnf" 2>/dev/null
spki="$(openssl x509 -in "$work/tls.crt" -pubkey -noout | openssl pkey -pubin -outform der | openssl dgst -sha256 -binary | base64)"

echo "== MinIO behind its own route"
k apply -f "$here/cluster/deps.yaml" >/dev/null
k -n "$deps" create secret tls s3-tls --cert "$work/tls.crt" --key "$work/tls.key" --dry-run=client -o yaml | k apply -f - >/dev/null
k apply -f "$here/cluster/minio-route.yaml" >/dev/null
k -n "$deps" rollout status deploy/minio --timeout=180s >/dev/null

echo "== hugginghack x$replicas behind the route $base"
k create namespace "$ns" --dry-run=client -o yaml | k apply -f - >/dev/null
k label namespace "$ns" --overwrite pod-security.kubernetes.io/enforce=restricted >/dev/null
k -n "$ns" create secret tls hub-tls --cert "$work/tls.crt" --key "$work/tls.key" --dry-run=client -o yaml | k apply -f - >/dev/null
pod_cidr="$(k get nodes -o jsonpath='{.items[0].spec.podCIDR}')"
cat > "$work/route-values.yaml" <<EOF
global:
  sharedData:
    PUBLIC_URL: $base
    ALLOWED_HOSTS: $hub
    # The router (here ingress-nginx) runs in the pod network.
    FORWARDED_ALLOW_IPS: $pod_cidr
    STORAGE_TARGETS_JSON: >-
      [{"id":"grid","name":"MinIO","bucket":"models","prefix":"models",
      "endpoint_url":"http://minio.hh-helm-deps.svc:9000",
      "public_endpoint_url":"https://$s3:$port",
      "region":"us-east-1","addressing_style":"path","use_ssl":false,
      "access_key_env":"GRID_KEY","secret_key_env":"GRID_SECRET",
      "direct_downloads":true,"direct_uploads":true,"part_size_mb":16}]
hugginghack:
  replicaCount: $replicas
route:
  enabled: true
  host: $hub
EOF
h dependency update "$here/umbrella-fixture" >/dev/null
h upgrade --install hh "$here/umbrella-fixture" -n "$ns" -f "$work/route-values.yaml" --wait --timeout 6m >/dev/null
k -n "$ns" get pods -l app.kubernetes.io/name=hugginghack -o wide | awk '{print $1, $2, $3, $6}'

kubectl --context "$KUBE_CONTEXT" -n ingress-nginx port-forward svc/ingress-nginx-controller "$port:443" >/dev/null 2>&1 & pids+=($!)
for _ in $(seq 1 30); do curl -sf --cacert "$work/ca.pem" -o /dev/null "$base/api/health" && break; sleep 1; done

echo "== the route: TLS, cookies, client addresses, cross-site writes, load spread"
CA="$work/ca.pem" BASE="$base" REPLICAS="$replicas" "$python" - <<'PY'
import os, httpx
base, ca = os.environ["BASE"], os.environ["CA"]
c = httpx.Client(base_url=base, verify=ca, headers={"Origin": base}, timeout=60)
r = c.post("/api/auth/setup", json={"username": "owner", "display_name": "Owner", "password": "route-owner-password"})
if r.status_code == 409:
    r = c.post("/api/auth/login", json={"username": "owner", "password": "route-owner-password"})
r.raise_for_status()
cookie = r.headers.get("set-cookie", "")
assert "secure" in cookie.lower() and "httponly" in cookie.lower(), cookie
c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
print("ok  TLS at the route; the session cookie is Secure and HttpOnly (X-Forwarded-Proto believed)")
sessions = c.get("/api/account/sessions").json()["items"]
addresses = {s.get("ip_address") or s.get("ip") for s in sessions}
assert not any(str(a).startswith("10.") for a in addresses), addresses
print(f"ok  sessions record the client's address {sorted(map(str, addresses))}, not the router's (FORWARDED_ALLOW_IPS)")
evil = httpx.post(f"{base}/api/account/preferences", verify=ca, headers={"Origin": "https://evil.example", "X-CSRF-Token": c.headers["X-CSRF-Token"]}, cookies=c.cookies, json={"theme": "dark"})
assert evil.status_code == 403, evil.status_code
print("ok  a write from another site is refused through the route (403)")
for _ in range(60):
    assert c.get("/api/auth/status").status_code == 200
print("ok  60 signed-in requests, every one answered by whichever replica the Service chose")
PY
spread="$(for pod in $(k -n "$ns" get pods -l app.kubernetes.io/name=hugginghack -o name); do k -n "$ns" logs "$pod" | grep -c 'GET /api/auth/status' || true; done | tr '\n' ' ')"
served="$(echo "$spread" | tr ' ' '\n' | grep -cv '^0\?$' || true)"
[ "$served" -ge 2 ] || { echo "the Service sent everything to one pod: $spread"; exit 1; }
echo "ok  requests per pod: $spread"

echo "== a real browser through the route: sign in, browse, write, upload straight to the bucket"
pw="${PLAYWRIGHT_CORE:-}"
if [ -z "$pw" ]; then
  mkdir -p "$work/pw" && (cd "$work/pw" && npm install -s playwright-core@1 >/dev/null 2>&1)
  pw="$work/pw/node_modules/playwright-core/index.mjs"
fi
mkdir -p "$work/upload/route-check"
printf '{"model_type":"llama"}' > "$work/upload/route-check/config.json"
head -c 41943040 /dev/urandom > "$work/upload/route-check/model.safetensors"
first="$(PLAYWRIGHT_CORE="$pw" node "$here/route-browser.mjs" first "$base" "$spki" "$work/state.json" "$work/upload/route-check")"
echo "   $first"
echo "$first" | "$python" -c '
import json, sys
r = json.load(sys.stdin)
assert r["sessionCookie"] and r["sessionCookie"]["secure"], r["sessionCookie"]
assert "created" in r["createOrg"], r["createOrg"]
assert "committed" in r["upload"], r["upload"]
assert r["hubPuts"] == 0 and r["bucketPuts"] > 0, (r["hubPuts"], r["bucketPuts"])
assert not r["csp"] and not r["errors"], (r["csp"], r["errors"])
assert not any(s.startswith("5") for s in r["apiStatuses"]), r["apiStatuses"]
parts = r["bucketPuts"]
print(f"ok  browser: signed in, org created, 40 MiB uploaded in {parts} parts to the bucket route, 0 through the hub, no CSP or page errors")'

echo "== every pod replaced while the browser is signed in"
k -n "$ns" rollout restart deploy/hh-hugginghack >/dev/null
k -n "$ns" rollout status deploy/hh-hugginghack --timeout=6m >/dev/null
after="$(PLAYWRIGHT_CORE="$pw" node "$here/route-browser.mjs" after-restart "$base" "$spki" "$work/state.json")"
echo "   $after"
echo "$after" | "$python" -c '
import json, sys
r = json.load(sys.stdin)
assert r["stillSignedIn"], r
assert "saved" in r["saveModel"], r["saveModel"]
assert not r["errors"], r["errors"]
print("ok  after all pods were replaced: still signed in, the model page loads, a write succeeds")'

echo "== pull clients through the route, trusting the CA"
REQUESTS_CA_BUNDLE="$work/ca.pem" SSL_CERT_FILE="$work/ca.pem" HF_ENDPOINT="$base" HF_HOME="$work/hf" HF_HUB_DISABLE_TELEMETRY=1 \
  "$python" -c '
import hashlib, os, sys
from huggingface_hub import hf_hub_download, snapshot_download
path = snapshot_download("owner/route-check")
got = hashlib.sha256(open(os.path.join(path, "model.safetensors"), "rb").read()).hexdigest()
want = hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest()
assert got == want, (got, want)
print("ok  snapshot_download through the route; weights from the bucket route; hash matches")' "$work/upload/route-check/model.safetensors"
git -c http.sslCAInfo="$work/ca.pem" clone -q "$base/owner/route-check" "$work/clone"
(cd "$work/clone" && git -c http.sslCAInfo="$work/ca.pem" lfs pull >/dev/null && [ -z "$(git status --short)" ] \
  && cmp -s model.safetensors "$work/upload/route-check/model.safetensors")
echo "ok  git clone + git lfs pull through the route: clean checkout, identical weights"
echo "All route checks passed."
