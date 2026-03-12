# Zotero Bridge 升级包（给 Codex 直接执行）

## 0. 任务目标

你正在升级一个已经实现了相当多 REST 接口的 FastAPI 项目：`zotero-bridge-design-package`。

**本次升级的核心目标只有一条：**

> 把当前代码库从“带 fulltext / PDF 读取能力的桥接服务”升级为“纯 Zotero I/O（存取）+ 工作流回写层”，明确 **bridge 不负责读 PDF、不负责解析 PDF、不负责做论文理解**；PDF 的阅读、理解、抽取、综述生成全交给 ChatGPT / Codex CLI。

这不是一个只改文档的任务，而是一次**真实代码升级**。你需要修改代码、路由、模型、OpenAPI、测试和文档，使仓库的新状态与这个产品方向完全一致。

---

## 1. 升级后的产品定位（硬要求）

请把项目定位明确改成下面这套分工：

### 1.1 bridge 负责什么

bridge 负责：

- Zotero 文献条目检索与读取
- 文献元数据入库
- DOI 入库
- discovery（如 OpenAlex）结果回写入库
- PDF / 附件上传与管理
- 附件交付（handoff / proxy download），让上层 agent 自己去读 PDF
- AI note / structured note 的回写、更新、读取、删除
- citation / bibliography 生成
- duplicates 检测与合并
- 与 collections / tags / related items 相关的 Zotero 工作流

### 1.2 bridge 不负责什么

bridge **不负责**：

- PDF 文本抽取
- OCR
- fulltext chunking
- fulltext preview
- 论文内容理解
- 方法/结论/局限的自动抽取
- 文献综述生成
- 研究 gap 推理

### 1.3 一句话定位（必须写进 README / ARCHITECTURE / HANDOFF）

请在文档里明确写出类似这句话：

> `zotero-bridge is a Zotero I/O and workflow layer, not a PDF reading engine.`

中文也要同步写清楚：

> 本项目是 Zotero 的存取与工作流桥接层，不是 PDF 阅引擎。

---

## 2. 当前仓库现状（你需要基于现状升级，不是重写）

你应该先确认并基于以下事实进行修改：

1. 项目当前已经是一个实现中的 FastAPI 服务，不是空设计稿。
2. 当前存在的主要路由包括：`health.py`、`papers.py`、`items.py`、`library.py`、`discovery.py`、`notes.py`。
3. 当前存在 fulltext 相关实现，包括：
   - `app/services/fulltext.py`
   - `app/services/local_fulltext_store.py`
   - `app/models.py` 中的 `FulltextResponse`、`BatchFulltextPreviewRequest`、`ReviewPackRequest.includeFulltextPreview`、`ReviewPackItem.fulltextPreview` 等
4. `app/main.py` 当前把 `LocalFulltextStore`、`LocalSearchIndex`、`FulltextService` 注入到 `BridgeService`。
5. 当前 `pyproject.toml` 依赖里有 `pypdf`。
6. 当前 README / CODEX_HANDOFF / TEST_PLAN / OpenAPI 里仍然把 `/v1/items/{itemKey}/fulltext`、`/v1/items/fulltext/batch-preview`、`review-pack + fulltext preview` 当成重要能力。
7. 当前上传入口里已经接受 `fileUrl` 和 `openaiFileIdRefs`，但还缺少更严格的 SSRF / redirect / size / MIME 安全约束。
8. 当前 AI note 已经有一定基础：`agent`、`noteType`、`slot`、`model`、`sourceAttachmentKey`、`sourceCursorStart`、`sourceCursorEnd` 等字段已经存在。
9. 当前 `bridge_service.py` 很大，已经到了应该拆分的程度。

**本次任务必须在现有实现基础上演进，不要推倒重来。**

---

## 3. 本次升级的输出要求

你需要产出一个**可运行、可测试、文档同步、OpenAPI 同步**的新版本。

### 3.1 必须完成的结果

- 删除 bridge 的 PDF/fulltext 读取职责
- 删除或下线 fulltext API（见迁移策略）
- 保留并强化 Zotero 存取、入库、附件交付、结构化笔记回写能力
- 新增 attachment handoff / proxy download 能力
- 新增更适合 agent 自动入库的 metadata/discovery import 能力
- 把 AI note 升级为结构化 note
- 把文档、OpenAPI、测试全部改到一致
- 进行代码整理，降低 `bridge_service.py` 的耦合

### 3.2 本次升级后的版本号

当前 `pyproject.toml` 版本是 `1.0.0`。

由于这是**明确的 breaking change**（fulltext API 能力被移除/下线），请把版本升级到：

> `2.0.0`

并在 README / release note 风格的变更说明中明确写出 breaking changes。

---

## 4. 迁移策略（重要）

不要悄悄删除接口。请采用**清晰但务实的迁移策略**：

### 4.1 fulltext 路由处理方式

对于以下旧接口：

- `GET /v1/items/{itemKey}/fulltext`
- `POST /v1/items/fulltext/batch-preview`

不要再返回真正的 fulltext 内容。处理方式如下：

- 在 OpenAPI 中移除它们，不再作为受支持能力公开
- 在服务端保留一个**兼容性 stub**（只保留 1 个版本周期）
- stub 返回 `410 Gone`
- body 中给出明确提示，例如：
  - bridge-side fulltext / PDF reading has been removed
  - use attachment handoff or let ChatGPT/Codex read the PDF directly

这样做的目的：

- 让旧客户端尽快发现能力已下线
- 避免静默失败
- 不需要再保留大段 fulltext 业务逻辑

### 4.2 review-pack 处理方式

保留 `review-pack` 路由，但它不再负责正文预览。

升级后：

- 删除 `includeFulltextPreview` 的主支持地位
- 如果请求里还带着 `includeFulltextPreview`，可以：
  - 忽略并返回 warning；或
  - 直接 400 并提示参数已废弃

推荐做法：**忽略旧参数并在响应里带 warning**，减少无谓 breakage。

升级后的 `review-pack` 应只包含：

- item metadata
- citation
- attachments
- ai notes / structured notes
- related items

不要再有 `fulltextPreview` 字段。

---

## 5. 代码层面的具体改造任务

下面是必须执行的改造清单。

---

## 5A. 删除 bridge 的 fulltext / PDF 读取职责

### 5A.1 删除或瘦身的模块

请删除或替换以下能力：

- `app/services/fulltext.py`
- `app/services/local_fulltext_store.py`
- 所有 bridge-side PDF 解析逻辑
- 所有 `pypdf` 依赖相关代码

**建议做法：**

- 完全删除 `local_fulltext_store.py`
- 删除 `fulltext.py`
- 如果某些 route 还需要兼容性 stub，则用一个很薄的 `legacy_fulltext.py` 或直接在 route 层返回 `410 Gone`

### 5A.2 main.py 的依赖注入

更新 `app/main.py`：

- 移除 `LocalFulltextStore`
- 移除 `FulltextService`
- 如果 `LocalSearchIndex` 继续保留，只允许其索引：
  - title
  - creators
  - abstract
  - venue
  - doi
  - tags
  - ai notes / structured notes
- 不允许再索引 fulltext

### 5A.3 config.py 清理

删除或废弃以下配置项（如存在）：

- `fulltext_default_max_chars`
- `fulltext_max_chars_hard_limit`
- `enable_local_fulltext_cache`
- `local_fulltext_cache_dir`
- 任何仅为 fulltext preview / chunking / PDF parsing 服务的配置项

如果为了兼容性需要暂时保留配置项，也必须：

- 标记为 deprecated
- 不再真正使用
- 在代码注释中注明将删除

### 5A.4 pyproject.toml 清理

- 删除 `pypdf`
- 更新 `uv.lock`
- 确保其余依赖仍然可通过 `uv sync` 正常安装

---

## 5B. 保留并强化“附件交付”能力（替代 fulltext）

核心思想：

> 不是 bridge 帮上层读 PDF，而是 bridge 把附件安全、稳定地交给上层 agent，让上层 agent 自己读。

### 5B.1 新增路由文件

新增：

- `app/routes/attachments.py`

并在 `app/main.py` 注册。

### 5B.2 新增服务模块

新增：

- `app/services/attachment_service.py`

职责：

- 列出 item 的 attachments
- 读取 attachment metadata
- 生成 handoff token / download URL
- 代理下载 attachment 内容（如果采用 proxy download）

### 5B.3 新增 API

#### 1) 列出附件

`GET /v1/items/{itemKey}/attachments`

返回：

- `itemKey`
- `attachments[]`
  - `attachmentKey`
  - `title`
  - `filename`
  - `contentType`
  - `linkMode`
  - `isPdf`
  - `downloadable`
  - `parentItemKey`

#### 2) 附件详情

`GET /v1/attachments/{attachmentKey}`

返回单个 attachment metadata。

#### 3) 附件交付（推荐）

`POST /v1/attachments/{attachmentKey}/handoff`

请求体示例：

```json
{
  "mode": "proxy_download",
  "expiresInSeconds": 900,
  "requestId": "req_123"
}
```

响应示例：

```json
{
  "attachmentKey": "ABCD1234",
  "filename": "paper.pdf",
  "contentType": "application/pdf",
  "mode": "proxy_download",
  "downloadUrl": "https://bridge.example.com/v1/attachments/download/tkn_xxx",
  "expiresAt": "2026-03-11T12:34:56Z"
}
```

#### 4) 代理下载（内部/外部使用）

`GET /v1/attachments/download/{token}`

行为要求：

- 校验 token
- 校验过期时间
- 校验 token 绑定的 attachmentKey
- 从 Zotero 安全拉取附件内容
- 透传正确的 `Content-Type`
- 返回 `Content-Disposition`，包含合理文件名

### 5B.4 token 策略

handoff token 至少需要满足：

- 有 TTL
- 不可伪造
- 最好一次性使用（如果实现成本不高）
- 不暴露 Zotero API key

推荐：

- 使用 HMAC 签名 token 或短期 server-side token store
- TTL 默认 15 分钟
- 支持配置

### 5B.5 不要把原始 Zotero 凭据暴露给模型

这是硬要求。

---

## 5C. 强化上传与导入能力（让 agent 更容易“搜到 -> 存进去”）

### 5C.1 保留现有能力

保留当前已有：

- `POST /v1/papers/add-by-doi`
- `POST /v1/papers/upload-pdf-action`
- `POST /v1/papers/upload-pdf-multipart`

### 5C.2 新增 metadata 直写入库

新增：

`POST /v1/papers/import-metadata`

用途：

- 当 ChatGPT / Codex 从外部搜到一篇论文的结构化信息时，不必一定依赖 DOI，直接把 metadata 写入 Zotero。

请求体至少支持：

```json
{
  "itemType": "journalArticle",
  "title": "...",
  "creators": [
    {"firstName": "A", "lastName": "B", "creatorType": "author"}
  ],
  "abstractNote": "...",
  "publicationTitle": "...",
  "date": "2024",
  "doi": "10.xxxx/xxxxx",
  "url": "https://...",
  "tags": ["llm", "review"],
  "collectionKey": "COLL123",
  "extra": "...",
  "requestId": "req_123"
}
```

响应至少返回：

- `status`: `created | exists | updated`
- `itemKey`
- `title`
- `dedupeStrategy`

### 5C.3 新增 discovery result 一键入库

新增：

`POST /v1/papers/import-discovery-hit`

用途：

- 把当前 discovery 搜索结果（如 OpenAlex hit）直接存入 Zotero
- 如果有 DOI，则优先 DOI 去重
- 如果没有 DOI，则按 title + year + first author 做弱去重

请求体可以接受：

- 完整 discovery record；或
- discovery response 中定义好的最小字段集

并支持参数：

- `attachPdfFromOpenAccessUrl`（默认 false）
- `collectionKey`
- `tags`

### 5C.4 import-metadata / import-discovery-hit 的 dedupe 行为

要求：

1. DOI 命中优先认为已存在
2. DOI 不存在时，再做 title-based 弱匹配
3. 已存在时不要重复创建，返回 `exists`
4. 可选允许 `updateIfExists=true`

### 5C.5 可选增强（非本次硬要求）

如果时间和上下文允许，可以额外加：

- `POST /v1/papers/import-bibtex-ris`

但这不是本次必须项。优先级低于 attachment handoff 和 structured notes。

---

## 5D. AI note 升级为 structured note

这是本次升级里非常重要的一部分。

目标：

> 让模型不仅能把“自然语言笔记”写回 Zotero，还能把“结构化研究信息”稳定写回 Zotero，并在之后被检索、聚合、复用。

### 5D.1 扩展 UpsertAINoteRequest

在当前 `UpsertAINoteRequest` 基础上新增：

- `schemaVersion: str | None`
- `payload: dict | None`
- `provenance: list[ProvenanceRecord] | None`

新增 `ProvenanceRecord` 模型，建议字段：

```json
{
  "attachmentKey": "ATTACH123",
  "page": 5,
  "locator": "p.5",
  "cursorStart": 1200,
  "cursorEnd": 1680,
  "quote": "optional short excerpt"
}
```

### 5D.2 规范 noteType

不要再让 `noteType` 完全自由散漫。请实现一组推荐 canonical note types，并在文档中列出：

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

要求：

- 仍然允许自定义 noteType（不要做死枚举导致扩展性很差）
- 但在文档和示例中强推 canonical 命名

### 5D.3 结构化 payload 的存储策略

由于 Zotero child note 本身是 HTML note，请实现一个**可 round-trip 的机器可读嵌入方案**。

要求：

- note 在 Zotero 里仍然可读（人类可看）
- bridge 读回时能稳定解析出 `payload`
- 尽量不要依赖外部数据库作为唯一真相来源

可接受实现方式：

- HTML comment 中嵌入 JSON / base64 JSON
- note 尾部带一个有明确 marker 的 machine block
- 其他你验证过可 round-trip 的方式

要求：

- 写入时：生成人类可读正文 + 机器可读 payload
- 读取时：解析出 `structuredPayload`
- 更新时：保持幂等，不要重复堆叠机器块

### 5D.4 note 读取响应增强

更新 note detail/read 接口响应，使其返回：

- `isAiNote`
- `agent`
- `noteType`
- `slot`
- `schemaVersion`
- `structuredPayload`
- `provenance`

### 5D.5 note 写入模式

保留并规范以下语义：

- `replace`
- `append`

推荐：

- 对 structured note，默认使用 `replace`
- 对一般自由笔记，允许 `append`

### 5D.6 搜索必须能命中 structured notes

搜索层要能命中：

- note title
- note human-readable text
- structured payload 中的重要文本字段（如果实现成本可控）

但不要引入 fulltext/PDF 索引。

---

## 5E. 搜索能力重构：保留“文献检索”，去掉“fulltext 检索”

### 5E.1 普通搜索

保留并加强：

- title
- creators
- abstract
- venue
- doi
- year
- tags
- collections（如已有）
- ai notes / structured notes

### 5E.2 高级搜索

如果当前高级搜索支持 `fulltext` field，请移除它。

升级后，不要再暴露 `fulltext` 作为可搜索字段。

### 5E.3 local_search_index

如果保留 `LocalSearchIndex`：

- 删除所有与 fulltext 相关的字段和刷新逻辑
- 只服务 metadata + notes 检索
- 如果实现成本很高，也可以彻底删掉 local index，然后只保留 upstream Zotero 搜索 + notes 辅助逻辑

### 5E.4 searchHints / whyMatched

当前已有 `searchHints` / `score` 基础，请把它做得更稳定：

- 明确返回命中字段，如：
  - `title`
  - `doi`
  - `tag:llm`
  - `aiNote:paper.methods`
- 不要再有 `fulltext` 命中提示

---

## 5F. review-pack 改造成“研究工作包”，不是正文预览包

保留 `review-pack`，但改语义。

### 5F.1 响应里保留

- item metadata
- citation
- attachments
- notes / ai notes / structured notes
- related items

### 5F.2 响应里删除

- `fulltextPreview`

### 5F.3 兼容旧参数

如果收到旧参数 `includeFulltextPreview=true`：

- 不要返回正文
- 返回 `warnings: ["includeFulltextPreview is deprecated and ignored"]`

### 5F.4 可选增强

如果你愿意顺手把名字改得更准确，可以考虑新增别名路由：

- `POST /v1/items/workspace-pack`

并把 `review-pack` 标记为 legacy alias。

这不是硬要求，但如果实现不复杂，可以做。

---

## 5G. 上传安全与 SSRF 防护（必须做）

当前 `upload-pdf-action` 接受 `fileUrl`。这块必须加固。

### 5G.1 必须做的限制

对于 `fileUrl` 下载逻辑：

- 默认只允许 `https`
- `http` 仅允许通过显式开发配置开关启用
- 拒绝 `localhost`
- 拒绝 `127.0.0.0/8`
- 拒绝 `::1`
- 拒绝 RFC1918 私网地址
- 拒绝 link-local / multicast / metadata service 等危险地址
- 每次重定向都重新校验目标
- 限制最大重定向次数（建议 3）
- 流式下载时强制大小上限
- 只允许 PDF（基于 MIME + 文件头双重校验，容忍一部分服务端 MIME 不准）

### 5G.2 新增配置项

在 `config.py` 中新增类似配置：

- `allow_insecure_http_file_url: bool = False`
- `max_file_url_redirects: int = 3`
- `allowed_file_source_hosts: list[str] | None = None`（可选 allowlist）
- `download_handoff_ttl_seconds: int = 900`

### 5G.3 安全实现建议

建议把这块单独抽到一个模块，例如：

- `app/services/remote_fetch_guard.py`

不要把 SSRF 校验逻辑散落在 route 里。

---

## 5H. 拆分 bridge_service.py（必须做，但不要过度设计）

当前 `bridge_service.py` 过大。请进行一次**务实拆分**。

目标不是炫技，而是把职责边界理顺。

### 5H.1 目标模块

建议至少拆成：

- `app/services/library_service.py`
  - items 读取
  - search / advanced search
  - related items
  - duplicates

- `app/services/ingest_service.py`
  - add-by-doi
  - upload-pdf-action
  - upload-pdf-multipart
  - import-metadata
  - import-discovery-hit

- `app/services/attachment_service.py`
  - list attachments
  - attachment detail
  - handoff token
  - proxy download

- `app/services/notes_service.py`
  - upsert ai note
  - read/update/delete note
  - structured note encode/decode

- `app/services/discovery_service.py`
  - OpenAlex discovery search

### 5H.2 BridgeService 的去留

可接受两种做法：

1. 保留 `BridgeService` 作为薄 façade，内部转调上述服务
2. route 直接依赖更细粒度 service

推荐：

- 本次先保留薄 façade，减少大面积改动

### 5H.3 约束

- 不要做超大规模重构导致难以 review
- 以“职责清晰 + 测试可维护”为主

---

## 6. API / 模型层的具体修改要求

下面是模型层必须同步的地方。

---

## 6A. models.py 修改

### 6A.1 删除/废弃的模型

删除或标记为 legacy：

- `FulltextResponse`
- `BatchFulltextPreviewRequest`
- 与 fulltext preview 强绑定的响应模型

### 6A.2 修改 review-pack 相关模型

- 从 `ReviewPackRequest` 中移除/废弃 `includeFulltextPreview`
- 从 `ReviewPackItem` 中删除 `fulltextPreview`
- 增加 `warnings: list[str] | None`

### 6A.3 新增 attachment 模型

请新增类似模型：

- `AttachmentRecord`
- `AttachmentListResponse`
- `AttachmentDetailResponse`
- `AttachmentHandoffRequest`
- `AttachmentHandoffResponse`

### 6A.4 新增 metadata import 模型

新增类似：

- `ImportMetadataRequest`
- `ImportMetadataResponse`
- `ImportDiscoveryHitRequest`
- `ImportDiscoveryHitResponse`

### 6A.5 扩展 AI note 模型

扩展：

- `UpsertAINoteRequest`
- note read/detail response

加入：

- `schemaVersion`
- `payload`
- `provenance`

### 6A.6 尽量保持向后兼容的字段名风格

现有项目的字段命名已经比较统一，请延续当前风格。

---

## 6B. routes 修改

### 6B.1 items.py

- 移除真正的 fulltext 处理逻辑
- 保留 legacy stub（410）
- 更新 `review-pack`
- 增加 `GET /v1/items/{itemKey}/attachments`

### 6B.2 新增 attachments.py

- `GET /v1/attachments/{attachmentKey}`
- `POST /v1/attachments/{attachmentKey}/handoff`
- `GET /v1/attachments/download/{token}`

### 6B.3 papers.py

保留现有能力并新增：

- `POST /v1/papers/import-metadata`
- `POST /v1/papers/import-discovery-hit`

### 6B.4 notes.py

- 保留当前 GET / PATCH / DELETE
- 如已有 AI note upsert route，则升级其 schema 支持 structured payload
- 如果 AI note 目前是挂在 item route 上，也请同步更新 schema

---

## 7. Zotero client 层的要求

请在 `app/services/zotero_client.py` 里补足/整理这些能力：

### 7.1 保留

- item metadata 读取
- citation
- search
- create/update notes
- add item / add by DOI 相关支持
- upload 授权与上传

### 7.2 新增/补强

- attachment metadata 获取
- attachment file download（供 proxy/handoff 使用）
- 更清晰的错误映射
- 上传与下载链路里的 timeout / redirect / content-type 处理

### 7.3 约束

- 任何 attachment handoff / download 实现都不能把 Zotero API key 暴露到外部响应里

---

## 8. 文档必须同步更新

本次升级不是“代码改了，文档不管”。以下文件必须更新。

### 8.1 README.md

需要改成新的产品描述：

- 强调 Zotero I/O 和 workflow layer
- 删除 fulltext / PDF parsing 作为卖点
- 增加 attachment handoff
- 增加 import-metadata / import-discovery-hit
- 强调 structured AI notes
- 写清 breaking changes

### 8.2 README_IMPLEMENTATION.md

同步实际实现状态，不要保留旧说法。

### 8.3 ARCHITECTURE.md

明确写出：

- bridge 不是 PDF reading engine
- PDF 理解由 ChatGPT/Codex 执行
- bridge 只负责附件交付和知识回写
- structured notes 是核心能力之一

### 8.4 CODEX_HANDOFF.md

这是重点。

请把 handoff 改成新的硬要求：

#### 旧方向（要删/改）

- 不要再要求实现 `app/services/fulltext.py`
- 不要再把 `/v1/items/{itemKey}/fulltext` 当成核心能力
- 不要再把 “reads from Zotero full text API” 当成主要工作流

#### 新方向（必须写进去）

- attachment handoff / proxy download
- metadata import
- discovery hit import
- structured note round-trip
- no bridge-side PDF reading

### 8.5 TEST_PLAN.md

移除或重写这些测试目标：

- fulltext chunking
- batch fulltext preview
- includeFulltextPreview 行为
- PDF parsing on bridge

新增测试目标：

- attachment handoff
- handoff token expiry
- proxy download
- import-metadata
- import-discovery-hit
- structured AI note round-trip
- SSRF guardrails

### 8.6 OpenAPI 文件

需要更新：

- `openapi.actions.yaml`
- `openapi.full.yaml`

要求：

- 不再公开 fulltext endpoints
- 新增 attachments / handoff endpoints
- 新增 import-metadata / import-discovery-hit endpoints
- 更新 notes schema
- 更新 review-pack schema

如果为了过渡保留 legacy stub 路由，它们也不要再出现在主 OpenAPI 里。

---

## 9. 测试改造要求

请务必让测试反映新产品方向。

### 9.1 删除或重写

- `tests/unit/test_fulltext.py`：删除或改成 legacy 410 stub 测试
- 所有期待 fulltext preview 的 contract/integration 测试

### 9.2 新增单元测试

新增至少这些测试：

#### 1) structured notes

- structured payload 写入成功
- 读回时能解析出 `structuredPayload`
- replace 模式不会重复堆叠机器块
- append 模式行为符合预期

#### 2) attachment handoff

- token 生成成功
- token 过期后不可用
- 非法 token 返回正确错误
- handoff 返回 download URL

#### 3) proxy download

- 正确透传 PDF content-type
- 正确返回 filename / disposition

#### 4) import-metadata

- 新建成功
- DOI 去重命中 existing
- `updateIfExists=true` 行为正确

#### 5) import-discovery-hit

- discovery hit 成功入库
- 弱去重逻辑合理

#### 6) SSRF 防护

- localhost 被拒绝
- 私网 IP 被拒绝
- 非 https 被拒绝（默认）
- redirect 到危险地址被拒绝
- 超大文件被拒绝

### 9.3 更新 contract / integration 测试

确保以下接口在 contract 层被覆盖：

- `GET /v1/items/{itemKey}/attachments`
- `GET /v1/attachments/{attachmentKey}`
- `POST /v1/attachments/{attachmentKey}/handoff`
- `GET /v1/attachments/download/{token}`
- `POST /v1/papers/import-metadata`
- `POST /v1/papers/import-discovery-hit`
- structured notes 相关写入/读取接口
- legacy fulltext stub（410）

### 9.4 质量门槛

本次 PR 必须通过：

```bash
uv sync
pytest
ruff check .
mypy app tests
```

如果某些类型标注需要补齐，请顺手补齐。

---

## 10. 建议的实现顺序（按这个顺序做，降低风险）

请按下面顺序实现，而不是到处乱改。

### 第 1 步：文档和方向先统一

先改：

- README
- ARCHITECTURE
- CODEX_HANDOFF
- TEST_PLAN
- OpenAPI 草稿

目的：先把目标定清楚。

### 第 2 步：下线 fulltext 能力

- 删除 fulltext service / local fulltext store
- main.py 移除注入
- models 清理
- routes 改成 legacy 410 stub
- 删除 `pypdf`

### 第 3 步：做 attachment handoff

- 新增 models
- 新增 attachment service
- 新增 routes
- 实现 token + download

### 第 4 步：做 metadata/discovery import

- papers.py 新接口
- ingest service 实现
- dedupe 逻辑打通

### 第 5 步：structured notes

- request/response schema 扩展
- encode/decode 实现
- 搜索层支持 notes 检索

### 第 6 步：bridge_service 拆分

- 抽出 attachment / notes / ingest / discovery / library 服务
- 保留薄 façade 或简化 route 依赖

### 第 7 步：测试与收尾

- 删除/替换旧 fulltext 测试
- 增加新测试
- 运行 lint / mypy / pytest
- 更新 lockfile

---

## 11. 你需要遵守的实现约束

### 11.1 不要做的事

- 不要引入新的数据库作为唯一真相来源
- 不要实现 bridge-side OCR
- 不要实现 bridge-side PDF parsing
- 不要把 Zotero API key 返回给任何客户端
- 不要为了结构化 payload 把所有信息只存在 bridge 本地缓存里
- 不要把这次升级做成“只是文档说不支持，代码其实还在偷偷支持”

### 11.2 允许的折中

- 允许保留 legacy fulltext stub（410）一个版本周期
- 允许 structured payload 采用嵌入 note body 的机器块方案
- 允许保留 local_search_index，但必须去掉 fulltext 维度

### 11.3 偏好

- 简单、稳定、可维护优先
- 面向 agent 使用体验优先
- API 响应紧凑、字段清晰优先

---

## 12. 交付物格式（请按此输出）

完成修改后，请输出：

### 12.1 变更摘要

按模块说明你改了什么：

- docs
- routes
- models
- services
- tests
- dependencies

### 12.2 breaking changes 清单

明确列出：

- fulltext endpoints 下线
- review-pack 不再返回正文预览
- search 不再支持 fulltext field

### 12.3 新增接口清单

列出：

- attachments list/detail/handoff/download
- import-metadata
- import-discovery-hit
- structured notes schema changes

### 12.4 验证结果

给出：

- `pytest` 结果
- `ruff check .` 结果
- `mypy app tests` 结果

### 12.5 后续建议（最多 5 条）

仅列真正有价值的下一步，不要泛泛而谈。

---

## 13. 直接执行时可以参考的实现草图

下面给出一个建议性的目标结构，供你实现时参考。不是必须完全一致，但请尽量接近。

```text
app/
  main.py
  config.py
  models.py
  routes/
    health.py
    items.py
    papers.py
    library.py
    discovery.py
    notes.py
    attachments.py        # new
  services/
    bridge_service.py     # much thinner
    library_service.py    # new
    ingest_service.py     # new
    attachment_service.py # new
    notes_service.py      # new
    discovery_service.py  # new
    zotero_client.py
    note_renderer.py
    remote_fetch_guard.py # new
```

---

## 14. 建议新增的数据模型示意

以下是建议，不要求字面一模一样，但语义要实现到位。

### 14.1 AttachmentRecord

```json
{
  "attachmentKey": "ABCD1234",
  "parentItemKey": "PARENT123",
  "title": "Paper PDF",
  "filename": "paper.pdf",
  "contentType": "application/pdf",
  "linkMode": "imported_file",
  "isPdf": true,
  "downloadable": true
}
```

### 14.2 AttachmentHandoffResponse

```json
{
  "attachmentKey": "ABCD1234",
  "filename": "paper.pdf",
  "contentType": "application/pdf",
  "mode": "proxy_download",
  "downloadUrl": "https://bridge.example.com/v1/attachments/download/tkn_xxx",
  "expiresAt": "2026-03-11T12:34:56Z"
}
```

### 14.3 Structured AI Note Request

```json
{
  "agent": "chatgpt",
  "noteType": "paper.findings",
  "slot": "default",
  "mode": "replace",
  "title": "Key Findings",
  "bodyMarkdown": "This paper reports ...",
  "schemaVersion": "1.0",
  "payload": {
    "researchQuestion": "...",
    "method": "...",
    "dataset": "...",
    "findings": ["..."],
    "limitations": ["..."]
  },
  "provenance": [
    {
      "attachmentKey": "ATT123",
      "page": 5,
      "locator": "p.5",
      "cursorStart": 1200,
      "cursorEnd": 1680
    }
  ],
  "tags": ["ai-note", "paper.findings"],
  "model": "gpt-5.4-pro",
  "requestId": "req_123"
}
```

### 14.4 ImportMetadataResponse

```json
{
  "status": "created",
  "itemKey": "ITEM1234",
  "title": "A Study on ...",
  "dedupeStrategy": "doi"
}
```

---

## 15. Definition of Done

只有满足下面这些条件，这次升级才算完成：

1. fulltext / bridge-side PDF reading 已不再是受支持能力
2. OpenAPI 不再公开 fulltext endpoints
3. legacy fulltext 路由返回 410，而不是继续提供正文
4. review-pack 不再包含 `fulltextPreview`
5. attachment handoff / proxy download 可用
6. import-metadata 可用
7. import-discovery-hit 可用
8. AI note 支持 structured payload round-trip
9. SSRF guardrails 已覆盖 `fileUrl`
10. 文档、OpenAPI、测试全部同步
11. 版本号升级到 `2.0.0`
12. `uv sync && pytest && ruff check . && mypy app tests` 通过

---

## 16. 给 Codex 的最后执行指令

请直接在当前仓库里完成这次升级，不要只做分析。

执行原则：

- 先做文档与 OpenAPI 对齐
- 再下线 fulltext
- 再补 attachments / import / structured notes
- 再做 service 拆分和测试收尾

如果某个细节存在实现分歧，请选择**更简单、更稳定、更符合“Zotero I/O + workflow layer”定位**的方案。

不要把 PDF 阅读能力重新偷偷加回来。
