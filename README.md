# Zotero Bridge 2.0

`zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

本项目是 Zotero 的存取与工作流桥接层，不是 PDF 阅读引擎。

## 项目定位

这套服务负责：

- Zotero 条目、标签、合集、引用、相关条目、重复项的读取与检索
- DOI、结构化 metadata、discovery hit 的入库
- PDF / 附件上传、管理、交付
- AI note / structured note 的写回、读取、更新、删除
- 为上层 agent 提供稳定的 Zotero workflow API

这套服务不负责：

- PDF 文本抽取
- OCR
- fulltext chunking
- fulltext preview
- bridge 侧论文理解、综述生成、研究 gap 推理

一句话：

> bridge 只负责把文献和附件交给 agent，并把分析结果写回 Zotero；PDF 阅读与理解由 ChatGPT、Codex CLI、Claude Code 自己完成。

## 2.0 的 Breaking Changes

- `GET /v1/items/{itemKey}/fulltext` 已删除
- `POST /v1/items/fulltext/batch-preview` 已删除
- `review-pack` 不再返回 `fulltextPreview`
- 高级搜索不再支持 `fulltext` 字段
- bridge 侧 `pypdf`、本地 fulltext cache、PDF parsing 逻辑已移除

## 当前支持的核心能力

- 条目读取与搜索
  - `GET /v1/items`
  - `GET /v1/items/search`
  - `GET /v1/items/search-advanced`
  - `GET /v1/items/{itemKey}`
  - `GET /v1/items/{itemKey}/related`
- citation / workspace pack
  - `GET /v1/items/{itemKey}/citation`
  - `POST /v1/items/review-pack`
- 附件交付
  - `GET /v1/items/{itemKey}/attachments`
  - `GET /v1/attachments/{attachmentKey}`
  - `POST /v1/attachments/{attachmentKey}/handoff`
  - `GET /v1/attachments/download/{token}`
- 入库与上传
  - `POST /v1/papers/add-by-doi`
  - `POST /v1/papers/import-metadata`
  - `POST /v1/papers/import-discovery-hit`
  - `POST /v1/papers/upload-pdf-action`
  - `POST /v1/papers/upload-pdf-multipart`
- structured notes
  - `POST /v1/items/{itemKey}/notes/upsert-ai-note`
  - `GET /v1/notes/{noteKey}`
- discovery / duplicates / workflow
  - `GET /v1/discovery/search`
  - duplicates 检测与合并

## 多人使用方式

当前版本支持“同一个 bridge，多个 Zotero 账号并行使用”。

规则很简单：

- 每个请求都必须带自己的 Zotero key
- 请求头使用：

```text
X-Zotero-API-Key: <你的 Zotero API Key>
```

bridge 会在每次请求里解析这把 key，并把请求路由到这把 key 对应的 Zotero 用户库。

也就是说：

- A 带 A 的 key，只会操作 A 的 Zotero
- B 带 B 的 key，只会操作 B 的 Zotero

当前这是“按请求切 Zotero 上游”的轻量多用户模式，适合几个人共用同一个 bridge。

## Structured Notes

structured AI note 会以 Zotero child note 的形式存储，包含两部分：

- Zotero 界面里可读的人类文本
- bridge 可 round-trip 解析的机器块

推荐 noteType：

- `paper.summary`
- `paper.methods`
- `paper.findings`
- `paper.limitations`
- `paper.future_work`
- `paper.relevance`
- `synthesis.theme`
- `synthesis.conflict`
- `synthesis.gap_candidate`
- `workflow.todo`

## 部署地址与契约

- 在线服务：`https://hblu.top:8888`
- 在线文档：`https://hblu.top:8888/docs`
- 在线 OpenAPI：`https://hblu.top:8888/openapi.json`
- 完整契约：[openapi.full.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.full.yaml)
- 面向 agent 的契约：[openapi.actions.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
- 接入说明：[AGENT_INTEGRATION.md](/home/ubuntu/zotero-bridge-design-package/AGENT_INTEGRATION.md)
- Codex skill：[skills/zotero-bridge/SKILL.md](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/SKILL.md)

## ChatGPT 中怎么用

适用场景：

- 你想在 ChatGPT 里把 bridge 作为一个 OpenAPI action / tool 使用

基本做法：

1. 在支持自定义 Actions / API tools 的 ChatGPT 环境中导入 `https://hblu.top:8888/openapi.json`
2. 为所有请求配置请求头：

```text
X-Zotero-API-Key: <你的 Zotero API Key>
```

3. 让 ChatGPT 按这条链路工作：
   - 先搜索或 discovery
   - 再导入 Zotero
   - 再列出附件并 handoff
   - 再下载 PDF
   - 最后把总结写回 structured notes

建议直接给 ChatGPT 这样的任务：

```text
使用 zotero bridge：
1. 搜索 software engineering agents 的论文
2. 选 3 篇导入 Zotero
3. 列出每篇的附件
4. 对 PDF 创建 handoff 并下载
5. 阅读 PDF 后写回 paper.summary structured note
```

注意：

- bridge 不会替 ChatGPT 解析 PDF
- 正确流程是 handoff 附件后，由 ChatGPT 自己读取 PDF，再写回 Zotero

## 在 Codex CLI 中怎么用

适用场景：

- 你想在终端里用 Codex 做“搜论文 -> 导入 -> 下载 PDF -> 阅读 -> 写 note”整条链路

推荐做法：

1. 设置环境变量：

```bash
export BRIDGE_BASE_URL="https://hblu.top:8888"
export ZOTERO_API_KEY="你的 Zotero API Key"
```

2. 直接启动 `codex`
3. 在 Codex 中明确说使用 `zotero-bridge`

示例提示词：

```text
Use zotero-bridge to search recent papers on software engineering agents, import 3 papers into Zotero, download the PDFs, summarize them, and write paper.summary notes back.
```

如果你已经安装了 repo 里的 skill，也可以直接让 Codex 按 skill 的默认工作流执行。skill 位置见：

- [skills/zotero-bridge/SKILL.md](/home/ubuntu/zotero-bridge-design-package/skills/zotero-bridge/SKILL.md)

Codex CLI 的正确边界是：

- bridge 负责 Zotero I/O、附件交付、回写
- Codex 负责本地读取 PDF、做总结、写 structured note

## 在 Claude Code 中怎么用

适用场景：

- 你想在 Claude Code 里把 bridge 当成外部 HTTP API 使用

推荐做法：

1. 在 Claude Code 的运行环境里设置：

```bash
export BRIDGE_BASE_URL="https://hblu.top:8888"
export ZOTERO_API_KEY="你的 Zotero API Key"
```

2. 让 Claude Code 使用 `curl` 或你自己的轻量脚本调用 bridge
3. 按和 Codex 类似的 workflow 执行：
   - 搜索 / discovery
   - 导入 Zotero
   - 附件 handoff
   - 下载 PDF
   - 本地阅读
   - 写回 structured note

最小请求示例：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  "$BRIDGE_BASE_URL/v1/items?limit=5"
```

写 structured note：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"agent":"claude-code","noteType":"paper.summary","slot":"default","bodyMarkdown":"简短总结","schemaVersion":"1.0","payload":{"summary":"简短总结"}}' \
  "$BRIDGE_BASE_URL/v1/items/ITEMKEY/notes/upsert-ai-note"
```

如果你要给 Claude Code 更稳定的上下文，建议把这两个文件一起交给它：

- [openapi.actions.yaml](/home/ubuntu/zotero-bridge-design-package/openapi.actions.yaml)
- [AGENT_INTEGRATION.md](/home/ubuntu/zotero-bridge-design-package/AGENT_INTEGRATION.md)

## 推荐工作流

适用于 ChatGPT、Codex CLI、Claude Code 的统一工作流：

1. 搜索 Zotero 或 discovery
2. 把论文导入 Zotero
3. 列出附件并对 PDF 做 handoff
4. 下载 PDF，由 agent 自己阅读
5. 写回 `paper.summary`、`paper.methods`、`paper.findings` 等 structured notes
6. 再次读取 note 或 `review-pack`，确认 round-trip

## 最小示例

列出条目：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  "https://hblu.top:8888/v1/items?limit=5"
```

搜索条目：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  --get "https://hblu.top:8888/v1/items/search" \
  --data-urlencode "q=llm"
```

搜索外部论文库：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  --get "https://hblu.top:8888/v1/discovery/search" \
  --data-urlencode "q=software engineering agents" \
  --data-urlencode "limit=5"
```

创建附件 handoff：

```bash
curl -H "X-Zotero-API-Key: $ZOTERO_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"mode":"proxy_download","expiresInSeconds":900}' \
  "https://hblu.top:8888/v1/attachments/ATTACHMENT_KEY/handoff"
```

## 验证命令

仓库默认应通过：

```bash
uv sync
pytest
ruff check .
mypy app tests
```
