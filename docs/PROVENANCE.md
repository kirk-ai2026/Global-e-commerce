# 代码与证据溯源

## 继承

- `lulu/vendor/source_sku_contract.py`：从 `New project/market_compare/market_compare/source_sku_contract.py` 原样复制，作为独立纯函数模块；本链路使用精确属性 token 图绑定与消费者规格清理。没有导入旧数据库初始化入口，也没有直接使用该模块将未知库存当作可售的历史过滤函数。
- `lulu/importer.py`：参考 `current_spu_catalog.py` 的跨 Run 证据读取、两库采购观察及 superseded Cluster 解析，独立实现，不继承台湾评估／原产地排除、模糊品牌匹配或伪造默认 SKU。
- `lulu/storage.py`：借鉴原云端持久化 Worker 的租约、阶段指纹、幂等及回执设计，使用独立 `lulu_*` 表。
- `lulu/channel.py`：继承 MarketListingManifest 的内容／市场分离以及 Shopify payload 映射方式，未包含 HTTP 写客户端。
- `Market_Compare/zenheart-bot`：参考渠道与商品分离边界。参考版本 `83b4d4f91a8ee321f748dac338ad0daa207548a6`；没有复制 LINE、会话或推送模块。

源 New project 尚无 Git 提交，实际文件哈希记录在 `SOURCE_FILES.json`。

## 独立性

Lulu 不通过旧项目路径加载运行时代码。只有一次性导入命令接收源库路径；API、Worker 和预览均从自己的 PostgreSQL 与本地内容寻址媒体空间读取。

原始事实库：`data/market_compare.sqlite3`；附加采购观察库：`exports/RUN-WUJIE-8044-OVERNIGHT/overnight_state.sqlite3`，均只读。

不把历史 276 个运营商品、388 个投影或 5,359 个 SPU 报告当作当前完整范围。冻结清单按品牌地区、渠道原始账本及四品类重新计算，并保留范围待确认项。

## 本期边界

仅内部成品及问题清单。Search Actor、搜索 Agent、扩大陆货盘、多语言实生成、CAD 商业公式、实店写入和部署均未启用。

所有付费模型调用数与 Shopify 写入数在本期回执中为零。已修复的源媒体只有具备精确绑定、文件版本与独立 QA 证据，才可通过后续冻结进入成品。
