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

报告区分审阅后复用、原稿补齐、待改稿／补证、服务商额度受阻、费用结果未知、待运营确认。金额未由服务商返回时记为未提供，报告实际 token、请求数和耗时，不填零费用。本期 Shopify 写入保持 0。

## 当前云端验收（2026-10-08）

后台已更新至 `lulu-merch-ops-00006-vxb`，Worker 与后台共用固定镜像 `sha256:06e7a099527797c51fd956d7e1bdf55270ae64f855d9337c5a32a69eee2eba96`。首次全量执行固定使用当时的镜像，未在执行中替换代码。源无界仍为 `wujie-merch-ops-shadow-archv3-0930-0741`，镜像及 100% 流量指针保持不变。

86 个商品全部导入完整证据，共 12,303 个 OCR 区域、1,312 张声明原图，其中 1,287 张成功读取并存入 Lulu。25 张原图在来源侧不可读取，分别属于三个商品；对应记录保留失败原因，未将缺图当成全商品失败。已有 OCR 中没有可用于自动裁切的区域坐标，清理操作不会猜测位置；混合渠道信息图需要补充可靠区域证据才能裁切或局部编辑。

| 加拿大简中本轮结果 | 商品数 |
|---|---:|
| 独立原稿审阅通过，可运营确认 | 65 |
| 两轮修复后仍待营养表展示调整 | 2 |
| 模型账户额度不足，未完成 | 18 |
| 先前付费调用结果未知，暂停重试 | 1 |
| 冻结分母 | 86 |

通过的 65 个候选中，10 个经原稿审阅后复用，55 个从完整来源补齐。最新候选合计登记 511 张描述区信息图，新增 AI 图片编辑 0；未自动启用任何候选。原成品 SKU、主图／Gallery、采购价、计费重量和 CAD 价格继续使用当前版本。POLA 的 CAD 23.33 与原有定价一致。

10 商品验收为 9 个 QA_PASS、1 个 HOLD。样例包括食品、美妆、贝亲母婴、单／多 SKU、部分暂缓和原图不可读场景；两个保健品样例的原类目仍是 UNKNOWN，未借验收修改来源类目。两个全量 HOLD 的事实检查已通过，问题是营养表比较布局和重复基准，补救方向为调整表格呈现后重新原稿审阅，不需要重新采集。

中性 OCR 缓存已在云端保存 1,208 条，三市场媒体 QA 缓存 540 条。另用同一批 10 商品证据执行了 30 组媒体策略验证，未新增 OCR 或付费图片工作。英语检查覆盖中文、日文、韩文、泰文及其他非拉丁文字；没有明确英语语言证据的外部拉丁文字保留为 SOURCE_ONLY，不能默认判成英语。

**繁中／英语文案生成尚未通过真实模型验收**：两个 POLA 测试任务与剩余简中任务一样被服务商 `credit_balance_exhausted` 拒绝。媒体策略、缓存隔离和语言渲染已验证，不能据此声称实际外语生成完成。

67 个已有审阅结果的候选，单商品本轮运行耗时中位数 198.922 秒；旧稿审计 81.252 秒、初次生成 51.601 秒、独立审阅与修复 65.587 秒（各阶段样本不同，不能直接相加）。媒体残余等待中位数为 0 秒；独立本地媒体验证耗时为 0.101–0.739 秒，这不是云端媒体 RT。当前轻量链路的主要等待来自模型调用；这些数据不能推断旧无界链路的图片编辑占比。新版本单独记录完整媒体准备耗时。

当前任务状态 BLOCKED_PROVIDER 与商品 NEEDS_EVIDENCE 分离。服务商明确失败的响应保存为 FAILED 回执；额度不足时 Worker 暂停消费，定时恢复执行不会不断提交失败模型请求。账户补充额度后，运营可选择“账户额度已补充，恢复受阻批次”；必须登录并明确确认 provider_ready。恢复保留任务与 checkpoint，使用一次性重试授权追加新回执，不删除旧回执，已知成功结果继续缓存复用。旧媒体合约已变更的英语任务创建新版本，保留旧任务。RESULT_UNKNOWN 不参与恢复；需先用登记的请求指纹向服务商核对回执。

CLI 恢复命令（仅在账户额度确已补充后执行）：

```bash
python -m lulu.ops.cli content resume-provider --provider-ready --reason "已确认现有模型账户额度补充"
python -m lulu.ops.cli content work --workers 2
```

完整测试套件 95 项通过。云端验证确认已登记图片可读、预览与导出采用同一 HTML 组装、旧 watermark 启用请求返回 409、未确认账户就绪的恢复请求返回 422。来源服务保持原版本；本次实际内容启用和 Shopify 写入均为 0。

- [全货盘结果、问题和阶段计时](CONTENT_ACCEPTANCE.json)
- [10 商品云端页面与导出验证](CONTENT_CLOUD_VERIFICATION.json)
- [三市场媒体策略验证及本地计时](CONTENT_MEDIA_ACCEPTANCE.json)
