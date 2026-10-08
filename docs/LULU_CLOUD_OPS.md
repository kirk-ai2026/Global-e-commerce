# Lulu 云端商品运营后台

已上线：[Lulu 商品运营后台](https://lulu-merch-ops-wiv3tqv5ua-de.a.run.app/)。

2026-10-08 验收：86 个目标商品完成简中生成和 CAD 报价，3 个品牌待确认，533 个成品媒体对象已同步。人工运营审核待进行；原品类不足的 2 个商品保留“待分类”。汇率为央行 2026-10-07 的 0.2126 CAD/CNY，POLA 为 C$23.33。每日汇率调用已返回 HTTP 200，口令、修改保存、CAS、内部审核及恢复均经云端验证。

本轮权威输入为无界当前线上 276 个可审商品及有效成品 Manifest；旧本地 6 个成品不是本轮范围。线上界面与镜像基线见 `WUJIE_OPS_BASELINE.json`。Lulu 的内容为 zh-Hans、市场 CA、价格 CAD，原台湾成品、QA 和运营记录保留在来源审计。

## 隔离边界

- Cloud Run：`lulu-merch-ops`，GCP `zenheart / asia-east1`。
- Cloud SQL：同实例 `zenheart-pg`，独立 `lulu_merch_ops` 数据库、`lulu_ops` schema 和 `lulu_ops_runtime` 用户。只写自身表。
- 媒体：独立桶 `zenheart-lulu-merch-ops`，`lulu/media/<sha256>`。字节哈希校验后返回 Lulu 自有 URL。
- Secret Manager：`lulu-ops-database-url`、`lulu-merch-ops-passphrase`。密钥不进入仓库或报告。
- 源 API 仅 GET，源 SQLite 只读。没有 Shopify／Shopline 发布实现；拒绝 PUBLISH 操作。
- 审核与源发布状态分离。内部通过不代表加拿大正式可售。

## 数据和审核

`source_freeze` 保存完整母体、品牌筛选和排除证据；`source_product` 追加保存来源版本、完整 SKU 合同和媒体 workspace；`listing` 保存独立简中内容、修改和审核；`history` 记录修改与基准 watermark。

品牌所属地区通过品牌注册表／当前来源身份补齐，与制造地分开。未确认品牌单列“品牌地区待确认”。默认列表为目标品牌货盘；状态筛选可查看待确认项。完整来源 SKU、已成品子集与暂缓原因分别展示。历史采购观察不当作当前可购买承诺。

简中版本采用 OpenCC 和受控词汇替换，保持已审核源事实、SKU 数字、数量及媒体。台湾市场段落另存而不改称加拿大证据。标题、规格、描述、SEO 与图片说明有独立内容哈希和 QA。未来语言内容不改变来源身份或媒体文件。

价格使用 Decimal：`ceil_cent((CNY成本 + 10 × g / 1000) × 1.25 × CAD/CNY)`。优先计费重量，其次同 SKU p75；缺价、重量或汇率显示“报价待补证”。编辑按天猫 SKU ID 精确绑定，填写原因后自动重算 CAD。源版本与汇率更新使已有审核需要重新确认；CAS 阻止旧页面覆盖新修改。

## 运行和部署

```bash
.venv/bin/python -m lulu.ops.cli migrate
.venv/bin/python -m lulu.ops.cli import --source-db '/Users/apple/Documents/New project/market_compare/data/market_compare.sqlite3' --limit 10
.venv/bin/python -m lulu.ops.cli import --source-db '/Users/apple/Documents/New project/market_compare/data/market_compare.sqlite3'
.venv/bin/python -m lulu.ops.cli serve --port 8897
```

本地 `LULU_OPS_DATABASE_URL`／`LULU_OPS_SCHEMA` 指定独立数据范围；`LULU_OPS_DATA_DIR` 指定媒体目录。对外监听必须配置 `LULU_OPS_PASSPHRASE`。

`scripts/provision_lulu_ops.py` 使用已登录 gcloud 凭据和官方 Cloud SQL connector，创建 Lulu 新资源并迁移独立本地快照；不会修改无界表。重复运行跳过已经存在的不可变记录；后续来源更新应由导入器完成并保留运营 patch，不用初始化脚本覆盖线上修改。部署使用 `Dockerfile.ops`／`cloudbuild.ops.yaml`。

运营写入首次要求 Lulu 独立口令，1 小时会话过期后重新登录。口令由项目管理者从 Secret Manager 查看并通过已有安全渠道提供，页面不显示密钥。

每日汇率任务 `lulu-ops-fx-daily` 调用 `/internal/fx-refresh`；该接口校验 Google OIDC audience 和调度账号。后台“更新汇率”使用同一央行接口。获取失败保留已有快照并显示日期；无快照停止报价。

## 验收记录

- 10 商品对照：`LULU_OPS_CANARY.json`。
- 全量品牌冻结和完成／待补证统计：`LULU_OPS_ACCEPTANCE.json`（部署前生成）。
- 云端端到端检查：`LULU_OPS_CLOUD_VERIFICATION.json`。
- 自动测试覆盖汇率方向、向上取整、P75 回退、缺项、SKU 精确修改、认证、CAS 和禁止实店发布。
- 运营仍需逐商品审核；自动简中 QA 通过不能冒充人工审核完成。源 QA 警告保留。

后台复用了列表、分页、筛选、商品／SKU 图片切换、内容／采购价／重量编辑、媒体隐藏／排序、审核和历史记录。完整历史媒体元数据保留为来源审计；本轮同步的是当前选中的合格成品图片，不重新生成历史媒体，也不触发付费 AI。
