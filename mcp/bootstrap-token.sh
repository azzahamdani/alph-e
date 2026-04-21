#!/usr/bin/env bash
#
# Creates a Grafana service account + token via the Grafana API,
# then stores the token as a Kubernetes Secret in the `mcp` namespace.
#
# Prereqs:
#   - Grafana port-forward running (task grafana) — the script hits localhost:3000
#   - kubectl pointing at the devops-agent cluster
#
# Idempotent: if the SA already exists, reuses it. Always creates a fresh token
# because Grafana only shows the token value once.

set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_USER="${GRAFANA_USER:-admin}"
GRAFANA_PASSWORD="${GRAFANA_PASSWORD:-admin}"
SA_NAME="${SA_NAME:-mcp-server}"
SA_ROLE="${SA_ROLE:-Admin}"  # Admin gets us past fine-grained RBAC headaches for the lab
NAMESPACE="${NAMESPACE:-mcp}"
SECRET_NAME="${SECRET_NAME:-grafana-mcp-token}"

echo "→ Checking Grafana is reachable at ${GRAFANA_URL}..."
if ! curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" "${GRAFANA_URL}/api/health" > /dev/null; then
  echo "✗ Grafana not reachable. Is 'task grafana' running in another terminal?" >&2
  exit 1
fi

echo "→ Looking up service account '${SA_NAME}'..."
sa_id=$(curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  "${GRAFANA_URL}/api/serviceaccounts/search?query=${SA_NAME}" \
  | python3 -c "
import json, sys
data = json.load(sys.stdin)
for sa in data.get('serviceAccounts', []):
    if sa['name'] == '${SA_NAME}':
        print(sa['id'])
        break
")

if [[ -z "${sa_id}" ]]; then
  echo "→ Creating service account '${SA_NAME}' with role '${SA_ROLE}'..."
  sa_id=$(curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
    -X POST \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${SA_NAME}\",\"role\":\"${SA_ROLE}\",\"isDisabled\":false}" \
    "${GRAFANA_URL}/api/serviceaccounts" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  echo "  created SA id=${sa_id}"
else
  echo "  found existing SA id=${sa_id}"
fi

# Delete old tokens with our naming convention so we don't accumulate them.
token_name="mcp-$(date +%s)"
echo "→ Creating fresh token '${token_name}'..."
token=$(curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASSWORD}" \
  -X POST \
  -H "Content-Type: application/json" \
  -d "{\"name\":\"${token_name}\"}" \
  "${GRAFANA_URL}/api/serviceaccounts/${sa_id}/tokens" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")

if [[ -z "${token}" ]]; then
  echo "✗ Failed to obtain token from Grafana API" >&2
  exit 1
fi

echo "→ Writing Kubernetes Secret '${SECRET_NAME}' in namespace '${NAMESPACE}'..."
kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic "${SECRET_NAME}" \
  --namespace "${NAMESPACE}" \
  --from-literal=token="${token}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ""
echo "✓ Done. The MCP deployment will read the token from secret/${SECRET_NAME}."
echo "  If the MCP pod was already running, restart it:"
echo "    kubectl -n ${NAMESPACE} rollout restart deployment/grafana-mcp"
