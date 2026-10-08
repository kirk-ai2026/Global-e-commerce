# Lulu 商品成品链路

面向加拿大华人的天猫国际内部商品中心。首版仅简体中文，覆盖东亚／东南亚品牌的食品、保健品、美妆个护和母婴。

本服务读取完整来源证据，不依赖某次 Run 或台湾选品结果。商品事实、SKU、媒体、语言内容和发布资格分别保存。首版没有 Shopify 写接口，也没有搜索 Agent。

## 安装与启动

Python 3.11+、PostgreSQL 16+。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
export LULU_DATABASE_URL='postgresql://lulu@127.0.0.1:55438/lulu'
.venv/bin/python -m lulu.cli migrate
.venv/bin/python -m lulu.cli serve
```

开发地址：`http://127.0.0.1:8896`。默认仅监听本机；对外监听必须设置 `LULU_API_TOKEN`。API 使用 Bearer token；不要把令牌放在 URL 中。

数据与媒体默认保存于 `data/`，报告位于 `exports/`，均不提交 Git。部署时显式配置独立 PostgreSQL 和 `LULU_DATA_DIR`；服务启动不执行 DDL。

## 导入与运行

```bash
.venv/bin/python -m lulu.cli freeze --source-db /path/to/market_compare.sqlite3 --state-db /path/to/overnight_state.sqlite3
.venv/bin/python -m lulu.cli job --limit 30
.venv/bin/python -m lulu.cli worker --job <job_id>
.venv/bin/python -m lulu.cli export --job <job_id> --output exports/CANARY30
.venv/bin/python -m lulu.cli job
.venv/bin/python -m lulu.cli worker --job <full_job_id> --batch-size 100
.venv/bin/python -m lulu.cli export --output exports/FULL
```

导入会读取全部当前候选和跨批次 Apify 观察。数据库使用 SQLite `mode=ro` 和 `query_only`；无 WAL 的静止数据库使用只读 immutable 模式，不创建上游 sidecar。技术失败不覆盖最近完整详情；更新且明确的停售观察单独留证。

冻结清单包含范围内、范围待确认和范围外三类。范围外直接记录排除原因；全量任务处理范围内及待确认项。数量同时报告来源商品与唯一 SPU，不能混用。重复冻结及任务创建是幂等的。

```bash
.venv/bin/python -m lulu.cli pause --job <job_id>
.venv/bin/python -m lulu.cli resume --job <job_id>
.venv/bin/python -m lulu.cli retry-failed --job <job_id>
.venv/bin/python -m lulu.cli status --job <job_id>
```

Worker 使用 PostgreSQL `SKIP LOCKED` 和租约，暂停阻止新的任务领取，在途结果正常落库；崩溃后续接相同任务，过期 Worker 无权提交。完成任务不会重新领取。事实变化生成新冻结版本，旧版本和回执保留。

## 状态与质量边界

- `CONTENT_READY`：至少一个 SKU 的标签、来源价、历史可买观察、媒体绑定及简中 QA 合格，仅代表内部成品。
- `NEEDS_EVIDENCE`：详情、身份、来源、SKU 或媒体缺证，保留全部原因与定向补证指纹。
- `EXCLUDED`：范围外或有更新的明确停售事实。
- `CHANNEL_READY`：仅预检在全部市场、报价、履约、媒体及库存策略配置齐全时返回；首版不调用实店。

来源库存和采购价始终带历史观察时间，不宣称当前可售。CAD 未配置时保持空值，不填零价，不套用台湾公式。源图参考在预览中明确标识，不能作为媒体 QA PASS。

中文源数据以事实驱动的中性编排形成新简中内容，OpenCC 只用于文字转换。完整商品描述、规格、SEO 和 alt 均独立生成，精确属性引用来源观察。没有 LLM 付费调用。价格变化复用语言内容；改变发布子集使描述和 QA 失效。

媒体只复用原项目标记为 LLM 生产链且文件哈希、最新 QA、Cluster、来源 SKU 和原图绑定兼容的成品。旧传统裁切或缺乏生产路线证明的结果不自动通过。缺图与修复项留在问题清单，不冒充完成；实际图片修复和独立 QA 结果经证据导入后，在原冻结事实下重建受影响商品。

历史媒体额外核对已采集观察中的完整实物轴；同 SKU ID、同图片 URL 但容量／口味／型号变化不能继承旧 PASS。没有可证明的历史绑定时进入补证队列。

媒体补证可通过 `lulu media-import --product <id> --receipt <receipt.json>` 或 `POST /v1/products/{id}/media-evidence` 接入。文件须在 Lulu 数据空间内，回执必须匹配冻结事实哈希、精确 SKU／实物轴及原图 URL；修复与独立视觉 QA 使用不同回执 ID，QA 绑定实际输出 SHA-256。只接受 `ORIGINAL_DIRECT_PASS`／`LLM_EDIT`，不自动认定图片存在即为 PASS。回执追加保存并只建立该商品的新任务，冻结事实不修改；任务绑定当时的媒体回执水位。

## API

- `GET /health`、`GET /v1/audit`
- `GET /v1/products?scope=INCLUDED&status=CONTENT_READY&category=food&q=品牌&after=&limit=24`
- `GET /v1/products/{id}`、`GET /v1/products/{id}/preflight`
- `GET /v1/issues`
- `POST /v1/jobs`，`GET /v1/jobs/{id}`
- `POST /v1/jobs/{id}/pause`、`POST /v1/jobs/{id}/resume`

所有消费者语言接口仅接受 `zh-Hans`。增加英语只需增加语言策略及启用配置；商品、采购、媒体身份不含语言。

## 验证

```bash
LULU_TEST_DATABASE_URL='postgresql://lulu@127.0.0.1:55438/lulu' .venv/bin/python -m pytest -q
```

每个 PostgreSQL 测试使用独立随机 schema；不清理日常开发表，不连接无界库或店铺。

代码来源、阶段边界与真实数据运行结果见 `docs/PROVENANCE.md` 和本机 `exports/` 下报告。
