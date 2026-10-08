# Lulu 原始证据商品内容引擎

本轮默认策略为 `ca-zh-Hans-lite-v1`。86 个商品原有简中转换稿继续展示；最新原稿／OCR候选经独立审稿后保存，运营确认才启用。已有主图、Gallery、SKU 子集和 CAD 定价独立保护，不随文案任务重建。

## 代码与事实边界

New project 最新纯文案内核被固定提取到 `lulu/ops/content/upstream`。`REVISION.json` 记录原文件哈希；提取的是原页面顺序阅读、文案 Schema、分段渲染、原稿审阅提示与来源预览能力，没有复制上游 Repository 或启用 Runtime。

Lulu 自身适配层移除了台湾硬编码：生成和审稿提示按目标市场／语言生成；营养表共同基准只显示一次，表头按 zh-Hans／zh-Hant／en 渲染。原始数据不改称加拿大事实。旧 `MARKETING` 标签不排除商品利益；颜色提示如 `orange` 不能被当成实际 SKU ID。实际未售 SKU 的容量、配方及数量仍不能混入成品。

完整 EvidenceBundle、原始快照、原图哈希、OCR 原文及原图区域保存在 `content_evidence`；中性 OCR 缓存剥离旧繁中译文及 SKU 决策。原图读取失败明确列出。原始来源仅通过限定 SELECT 与 GCS download 读取；Lulu 服务和 Worker 只连接独立 `lulu_merch_ops` 数据库。

## 市场策略

| 策略 | 商品信息文字 | 渠道内容 | 包装／身份 |
|---|---|---|---|
| 加拿大简中 | 原图可保留简体、繁体或其他语言；正文提供简中解释 | 去除平台、店铺、优惠和渠道配送信息 | 真实包装、品牌、SKU、数量不变 |
| 台湾繁中 | 包装外简体执行严格本土化门 | 去除 | 同样保护 |
| 英语 | 包装外非英语执行严格门，不能只检测简体 | 去除 | 同样保护 |

主图和 SKU 图复用现有合格版本。新增合格原图进入 `description_media`。无法明确适用多 SKU 的原图保持 SOURCE_ONLY，不为凑图数强配。安全边缘裁切只在无实体产品、全部保护区域有可靠坐标且不相交时执行；否则保留证据并列出修复任务。局部 AI 清理是独立、显式请求的任务，限定非主图／SKU图的渠道区域，不会阻断文字成品。

描述区图片由服务端用登记资产构建，模型不输出任意 URL。图片懒加载，原始来源可以查看高清。消费者描述和导出契约使用同一组装逻辑。英语／繁中当前仅生成验证候选，不自动生成完整外语店铺发布资格。

## 任务、缓存与恢复

内容任务记录证据版本、SKU／媒体保护指纹、运营内容基线、市场策略及实际模型配置。独立 Worker 全局锁防止多个执行同时消费；内部最多两商品并行。文案与媒体准备分开，文案不等待 AI 图编辑。

付费请求先登记 DISPATCHED 意图；已完成结果按完整请求语义指纹复用。响应 ID 立即保存；新文本请求采用背景模式，恢复时只 GET 同一响应，不再次 POST。没有响应 ID 的未知付费结果阻止该商品再次自动生成，列为 RESULT_UNKNOWN。失败的图单独挂起。

背景响应保留 `store=false`。该模式为轮询临时保存响应，并非永久存储；见 [OpenAI 背景模式文档](https://developers.openai.com/api/docs/guides/background)。结构化输出只保证结构，来源准确性仍由独立原稿审阅验证；见 [结构化输出文档](https://developers.openai.com/api/docs/guides/structured-outputs)。

候选和回执追加保存。新合约只能创建版本化任务，不修改正在执行的旧任务。兼容的格式升级可复用已确认来源审稿结果；无法证明兼容时重新验证。最多两轮修复后仍不通过时列为待改稿／补证，不冒充完成。

## 后台与接口

- `GET /api/content/profiles`、`GET /api/content/report`、`GET /api/content/issues`：策略、真实进度和问题。
- `POST /api/v2/ops/listings/{id}/content-jobs`：创建审计补齐／重生成任务，返回任务 ID。
- `GET /api/content/jobs/{id}`：查询 checkpoint、状态与结果。
- `GET /api/v2/ops/listings/{id}/content`：候选版本与实际当前稿对照。
- `POST .../content-candidates/{revision}/activate`：watermark 校验后确认启用；遇到运营修改可保留当前稿或明确替换，原修改归档。
- `GET .../content-candidates/{revision}/export`：生成对应候选的 HTML／图片／QA 契约。
- `GET .../source-text`：读取实际关联的完整原文、逐图 OCR、原始规格与引用。
- `POST .../description-media-tasks`：明确选择安全区域后创建局部渠道清理任务；严格市场的完整图片本土化不套用加拿大轻量操作。

原商品接口默认保持加拿大简中；旧稿标为 LEGACY_LOCALIZED，不能计为最新原稿审阅通过。模型与任务明细默认折叠。

## 运维

```bash
python -m lulu.ops.cli content migrate
python -m lulu.ops.cli content import-evidence
python -m lulu.ops.cli content enqueue
python -m lulu.ops.cli content work --workers 2
python -m lulu.ops.cli content report
```

云端 Job 为 `lulu-merch-content`，专属账号仅访问自身数据库、媒体桶和模型 Secret。后台启动自身 Job，失败时保留 PENDING；全局锁和检查点避免重放已知付费请求。原无界运行指针、Worker 和店铺不受该链路控制。

报告区分审阅后复用、原稿补齐、待改稿／补证、费用结果未知、待运营确认。金额未由服务商返回时记为未提供，报告实际 token、请求数和耗时，不填零费用。本期 Shopify 写入保持 0。
