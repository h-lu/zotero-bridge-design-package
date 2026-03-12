# Agent 接入说明

这份文档用于把 ChatGPT、Codex CLI、Claude Code 或其他 agent 接到 Zotero Bridge。

## 接入地址

- base URL: `https://hblu.top:8888`
- 在线文档: `https://hblu.top:8888/docs`
- 在线 OpenAPI: `https://hblu.top:8888/openapi.json`

## 认证方式

所有普通 API 都要求调用方提供自己的 Zotero key：

```text
X-Zotero-API-Key: <你的 Zotero API Key>
```

bridge 会在每次请求里解析这把 key，并把请求路由到这把 key 对应的 Zotero 用户库。

这意味着：

- 不同用户可以共用同一个 bridge
- 每个人只操作自己的 Zotero
- 不需要额外的 `BRIDGE_API_KEY`

## OpenAPI 文件

- [openapi.actions.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
  - 推荐给 ChatGPT Actions、agent tool 调用、轻量接入场景
- [openapi.full.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.full.yaml)
  - 推荐给开发、调试、全量能力查看

## 推荐工作流

推荐 agent 按下面的顺序工作：

1. 搜索 Zotero 或 discovery
   - `GET /v1/items/search`
   - `GET /v1/items/search-advanced`
   - `GET /v1/discovery/search`
2. 导入论文
   - `POST /v1/papers/import-discovery-hit`
   - `POST /v1/papers/import-metadata`
   - `POST /v1/papers/add-by-doi`
3. 列出并交付附件
   - `GET /v1/items/{itemKey}/attachments`
   - `POST /v1/attachments/{attachmentKey}/handoff`
   - `GET /v1/attachments/download/{token}`
4. 让 agent 自己下载并阅读 PDF
5. 把分析结果写回 structured notes
   - `POST /v1/items/{itemKey}/notes/upsert-ai-note`
   - `GET /v1/notes/{noteKey}`
6. 需要打包时使用：
   - `POST /v1/items/review-pack`

## 重要约束

- bridge 是 Zotero I/O 和 workflow layer，不是 PDF reading engine
- fulltext 接口已经删除，不要再使用
- `review-pack` 不再返回 `fulltextPreview`
- 正确做法是：
  - 先用 attachment handoff 拿到 PDF
  - 再由 ChatGPT / Codex CLI / Claude Code 自己读取 PDF
  - 最后把结果写回 Zotero

## ChatGPT 接入建议

适合方式：

- 导入 `https://hblu.top:8888/openapi.json`
- 或使用 [openapi.actions.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)

请求头配置：

```text
X-Zotero-API-Key: <你的 Zotero API Key>
```

附件读取链路：

1. `POST /v1/attachments/{attachmentKey}/handoff`
2. 从响应里取 `downloadUrl`
3. `GET /v1/attachments/download/{token}`

注意：

- `download/{token}` 本身是 capability URL
- 这一步不需要再传 `X-Zotero-API-Key`

## Codex CLI 接入建议

建议在终端里先设环境变量：

```bash
export BRIDGE_BASE_URL="https://hblu.top:8888"
export ZOTERO_API_KEY="你的 Zotero API Key"
```

然后在 Codex 中明确使用 `zotero-bridge`：

```text
Use zotero-bridge to search papers, import them into Zotero, download the PDFs, analyze them locally, and write structured notes back.
```

如果你安装了 repo 里的 skill，可配合使用：

- [skills/zotero-bridge/SKILL.md](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/SKILL.md)

## Claude Code 接入建议

Claude Code 更适合直接调用 HTTP API 或 `curl`。

建议环境变量：

```bash
export BRIDGE_BASE_URL="https://hblu.top:8888"
export ZOTERO_API_KEY="你的 Zotero API Key"
```

最小示例：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items?limit=5"
```

## 最小 curl 示例

列出条目：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items?limit=5"
```

搜索条目：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  --get "$BRIDGE_BASE_URL/v1/items/search" \
  --data-urlencode "q=llm"
```

搜索外部论文：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  --get "$BRIDGE_BASE_URL/v1/discovery/search" \
  --data-urlencode "q=software engineering agents" \
  --data-urlencode "limit=5"
```

导入 discovery hit：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"title":"SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering","publicationYear":2024,"authors":[{"name":"John Yang"}]}' \
  "$BRIDGE_BASE_URL/v1/papers/import-discovery-hit"
```

写 structured note：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent":"codex-cli","noteType":"paper.summary","slot":"default","bodyMarkdown":"Short summary","schemaVersion":"1.0","payload":{"summary":"Short summary"}}' \
  "$BRIDGE_BASE_URL/v1/items/ITEMKEY/notes/upsert-ai-note"
```
