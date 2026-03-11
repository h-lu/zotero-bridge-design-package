# Setup

## Endpoint

- `BRIDGE_BASE_URL=https://hblu.top:8888`
- live docs: `https://hblu.top:8888/docs`
- live OpenAPI JSON: `https://hblu.top:8888/openapi.json`
- static agent contract: [`openapi.actions.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)

## Auth

```bash
export BRIDGE_BASE_URL="https://hblu.top:8888"
export BRIDGE_API_KEY="..."
```

All requests use:

```text
Authorization: Bearer $BRIDGE_API_KEY
```

## Sanity Checks

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  "$BRIDGE_BASE_URL/healthz"
```

```bash
curl -H "Authorization: Bearer $BRIDGE_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items?limit=1"
```

## Primary OpenAPI Artifacts

- [`openapi.actions.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
- [`openapi.full.yaml`](/home/ubuntu/zotero-bridge-design-package/openapi.full.yaml)
