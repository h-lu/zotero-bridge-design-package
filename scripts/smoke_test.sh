#!/usr/bin/env bash
set -euo pipefail

: "${BRIDGE_BASE_URL:?set BRIDGE_BASE_URL}"
: "${BRIDGE_API_KEY:?set BRIDGE_API_KEY}"

AUTH=(-H "Authorization: Bearer ${BRIDGE_API_KEY}")

echo "== healthz =="
curl -fsS "${AUTH[@]}" "${BRIDGE_BASE_URL}/healthz" | jq .

echo
echo "== add by DOI =="
curl -fsS -X POST "${AUTH[@]}"   -H "Content-Type: application/json"   "${BRIDGE_BASE_URL}/v1/papers/add-by-doi"   -d '{
    "doi": "10.1038/nrd842",
    "tags": ["inbox", "api-test"],
    "requestId": "smoke-add-by-doi-001"
  }' | tee /tmp/zbridge_add.json | jq .

ITEM_KEY="$(jq -r '.itemKey' /tmp/zbridge_add.json)"

echo
echo "== search =="
curl -fsS "${AUTH[@]}"   "${BRIDGE_BASE_URL}/v1/items/search?q=10.1038%2Fnrd842&limit=5" | jq .

echo
echo "== citation =="
curl -fsS "${AUTH[@]}"   "${BRIDGE_BASE_URL}/v1/items/${ITEM_KEY}/citation?style=apa&locale=en-US" | jq .

echo
echo "== upsert AI note =="
curl -fsS -X POST "${AUTH[@]}"   -H "Content-Type: application/json"   "${BRIDGE_BASE_URL}/v1/items/${ITEM_KEY}/notes/upsert-ai-note"   -d '{
    "agent": "codex",
    "noteType": "summary",
    "slot": "default",
    "mode": "replace",
    "title": "AI Reading Note",
    "bodyMarkdown": "## Summary\n\nThis is a smoke-test note written by the bridge.",
    "tags": ["ai-summary", "smoke-test"],
    "model": "gpt-5.4",
    "requestId": "smoke-note-001"
  }' | jq .

echo
echo "== item detail =="
curl -fsS "${AUTH[@]}"   "${BRIDGE_BASE_URL}/v1/items/${ITEM_KEY}" | jq .

echo
echo "Smoke test complete."
