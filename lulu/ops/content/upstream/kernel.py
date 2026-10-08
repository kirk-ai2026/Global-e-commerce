"""Pinned pure editor kernel; HTTP clients and upstream runtime writes are excluded."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Any

from .shadow_merch_contracts import MEDIA_OMISSION_REASONS, SKU_WITHHOLD_REASONS

COPY_BRIEF = (
    "以天猫消费者详情为原稿，完整理解并忠实本土化为台湾繁中。"
    "Description第一模块必须是这件商品独有、吸引人继续阅读的营销开场："
    "从有证据的特点出发，写出台湾消费者可感知的购买理由、情境或感受；"
    "常青款与新品采用适合各自商品的表达，不套用爆款模板。"
    "营销表达要有天猫原稿的感染力或更好，但不能用冗长套话堆出热闹。"
    "写成易扫读的独立站商品页，不是来源考据或资料汇编。"
    "营销增强不等于加长：先用简短开场点出购买理由，再用小标题和短条列组织不同卖点；"
    "每条只讲一个消费者能快速理解的利益或事实，避免整段堆叠与前后重复；"
    "新稿应以重编替代附加，通常不比已足够完整的旧稿明显更长。"
    "已有的不同购买信息要保留，新增原稿信息应优先选择能帮助辨认、比较或使用本商品的内容；"
    "不要把通用品类科普、重复问答、泛泛保证或低价值警语堆到商品页。"
    "后续可重排、合并重复句，但当前页面已有且来源支持的不同用途、功能、成分、营养、参数、"
    "规格和用法不得丢失；天猫原稿不同且有帮助的卖点也要充分转译。"
    "商品利益是购买理由，不可只列成分或写泛泛的日常照顾：尤其保健品，应清楚讲出来源支持的"
    "舒适、软骨、润滑等具体用途，以及成分与用途的对应关系；图片OCR标为MARKETING也仍是可用的商品文案来源。"
    "原稿若有使用时间、比例和体验结果，确认原图中的人群、条件与统计口径后可忠实转译；"
    "不得把『在有感使用者中』写成所有使用者，不得把『有助于』写成保证见效。"
    "若效果调查只针对特定平台购买者，不能在无平台话术的独立站文案中准确交代样本，"
    "可省略该调查数字，但不能因此省略有来源的商品用途与成分作用。"
    "保健品可用少量条列呈现成分与用途的对应关系；含量只在表格列一次，"
    "利益条列不要再重复剂量，成分与作用相同的条列要合并。"
    "每份基准在表头或引言说明一次即可，不要在每个表格单元重复营养参考值说明；"
    "配料、食用方式、必要警示及保存可用紧凑的短条列，不把示例批号或泛化提醒写成长段。"
    "有来源也不必刊登低价值年份、赛事、论文或法规背景、示例批号、非本页可售规格；"
    "避免反复写『原商品介绍』、来源声明或无必要的免责话语。"
    "可以创作感受、使用情境和邀请式表达；排队、爆火、销量、榜单、本地人都在抢等可核实热度"
    "只有输入证据明确支持才可断言。台湾页面不要套用加拿大等其他市场语境。"
    "只删除平台、店铺、价格、物流及客服话术；不自创商品事实、不写其他SKU、不加空泛套话。"
    "香材、成分、色号等专有名词若OCR含糊，先核对原图；仍不清楚就用已证实的概括表达，"
    "不要凭常识把疑似错字替换成另一项具体事实。"
    "成稿前对照原页面顺序、逐图OCR和图片，自查明显遗漏、错译及数字与卖点的配对。"
    "每段保留证据ID；HTML只用安全的内容标签，营养表逐行保留数值、单位与基准。"
)

class _SafeDescriptionHTML(HTMLParser):
    """Only presentation markup survives; source/model attributes never do."""

    ALLOWED = {"p", "ul", "ol", "li", "strong", "em", "br", "table",
               "thead", "tbody", "tr", "th", "td"}
    DISCARD = {"script", "style", "iframe", "svg", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.discard_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.DISCARD:
            self.discard_depth += 1
        elif not self.discard_depth and tag in self.ALLOWED:
            self.parts.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.DISCARD:
            self.discard_depth = max(0, self.discard_depth - 1)
        elif not self.discard_depth and tag in self.ALLOWED and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.discard_depth:
            self.parts.append(html.escape(data))

def _safe_description_html(value: Any) -> str:
    parser = _SafeDescriptionHTML()
    parser.feed(str(value or ""))
    return "".join(parser.parts)

def render_description_sections(raw_sections: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Render one safe consumer description from cited editorial sections."""
    sections: list[dict[str, Any]] = []
    for row in raw_sections:
        section = {**row, "html": _safe_description_html(row.get("html"))}
        heading = str(row.get("heading") or "").strip()
        # The renderer owns the heading. A model-provided copy of that heading
        # as the first paragraph is presentation noise, not a second fact.
        if heading:
            leading = re.match(r"^\s*<p>(.*?)</p>", section["html"], re.I | re.S)
            if leading:
                first_text = html.unescape(re.sub(r"<[^>]+>", "", leading.group(1))).strip()
                if re.sub(r"[\s：:。.!！?？]+$", "", first_text) == re.sub(
                        r"[\s：:。.!！?？]+$", "", heading):
                    section["html"] = section["html"][leading.end():].lstrip()
            # Some models place the repeated title as plain text or inside
            # <strong>, not a standalone paragraph. Remove only an exact
            # leading title token; never delete a paragraph that develops it.
            escaped_heading = html.escape(heading)
            section["html"] = re.sub(
                rf"^\s*(<strong>)?\s*{re.escape(escaped_heading)}"
                r"\s*[：:。.!！?？]?\s*(</strong>)?",
                "", section["html"], count=1, flags=re.I,
            ).lstrip()
        table_rows = list(row.get("table_rows") or [])
        if table_rows:
            # The structured rows are the one canonical nutrition table.
            # Models sometimes also draw an HTML table, which otherwise gets
            # appended a second time below.
            section["html"] = re.sub(r"<table\b[^>]*>.*?</table>", "",
                                     section["html"], flags=re.I | re.S)
            cell_style = ' style="border:1px solid #d6dbe1;padding:8px 10px;text-align:left;vertical-align:top"'
            rendered_rows = "".join(
                "<tr><th" + cell_style + ">" + html.escape(str(item.get("label") or "")) +
                "</th><td" + cell_style + ">" + html.escape(" ".join(part for part in (
                    str(item.get("amount") or ""), str(item.get("unit") or "")) if part)) +
                "</td><td" + cell_style + ">" + html.escape(str(item.get("basis") or "")) + "</td></tr>"
                for item in table_rows)
            section["html"] += (
                '<table style="width:100%;border-collapse:collapse;margin:12px 0;font-size:14px">'
                "<thead><tr><th" + cell_style + ">項目</th><th" + cell_style +
                ">數值</th><th" + cell_style + ">基準</th></tr></thead>"
                f"<tbody>{rendered_rows}</tbody></table>")
            section["evidence_ids"] = list(dict.fromkeys([
                *(row.get("evidence_ids") or []),
                *(identifier for item in table_rows
                  for identifier in item.get("evidence_ids") or []),
            ]))
        sections.append(section)
    description = "".join(
        (f"<h2>{html.escape(str(row.get('heading') or ''))}</h2>"
         if row.get("heading") else "") + str(row.get("html") or "")
        for row in sections)
    return sections, description

class GlobalMerchEditorClient:
    @staticmethod
    def schema() -> dict[str, Any]:
            variant_label = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "source_sku_id": {"type": "string"},
                    "localized_label": {"type": "string"},
                    "semantic_signature": {"type": "string"},
                },
                "required": ["source_sku_id", "localized_label", "semantic_signature"],
            }
            withheld = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "source_sku_id": {"type": "string"},
                    "reason_code": {"type": "string", "enum": sorted(SKU_WITHHOLD_REASONS)},
                    "reason": {"type": "string"},
                }, "required": ["source_sku_id", "reason_code", "reason"],
            }
            omission = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "family_id": {"type": "string"},
                    "reason_code": {"type": "string", "enum": sorted(MEDIA_OMISSION_REASONS)},
                    "reason": {"type": "string"},
                }, "required": ["family_id", "reason_code", "reason"],
            }
            edit = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "family_id": {"type": "string"},
                    "source_derivative_id": {"type": ["string", "null"]},
                    "operation": {"type": "string", "enum": [
                        "REMOVE_EXTERNAL_TEXT", "CLEAN_CONTEXT", "CATALOG_COMPOSE"]},
                    "reason": {"type": "string"},
                }, "required": ["family_id", "source_derivative_id", "operation", "reason"],
            }
            derivative_override = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "family_id": {"type": "string"},
                    "derivative_id": {"type": "string"},
                }, "required": ["family_id", "derivative_id"],
            }
            section = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "heading": {"type": "string"}, "html": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "table_rows": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"}, "amount": {"type": "string"},
                            "unit": {"type": "string"}, "basis": {"type": "string"},
                            "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        }, "required": ["label", "amount", "unit", "basis", "evidence_ids"]}},
                }, "required": ["heading", "html", "evidence_ids", "table_rows"],
            }
            copy_priority = {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "buyer_question": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "status": {"type": "string", "enum": [
                        "COVERED", "SOURCE_MISSING", "OMITTED"]},
                    "reason": {"type": "string"},
                }, "required": ["buyer_question", "evidence_ids", "status", "reason"],
            }
            return {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "variant_labels": {"type": "array", "items": variant_label},
                    "collapsed_sku_aliases": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"representative_sku_id": {"type": "string"},
                                       "alias_sku_ids": {"type": "array", "items": {"type": "string"}}},
                        "required": ["representative_sku_id", "alias_sku_ids"]}},
                    "withheld_skus": {"type": "array", "items": withheld},
                    "description_sections": {"type": "array", "items": section},
                    "copy_priorities": {"type": "array", "items": copy_priority},
                    "primary_family_id": {"type": "string"},
                    "gallery_order": {"type": "array", "items": {"type": "string"}},
                    "sku_media_overrides": {"type": "array", "items": {
                        "type": "object", "additionalProperties": False,
                        "properties": {"source_sku_id": {"type": "string"},
                                       "family_id": {"type": "string"},
                                       "visual_evidence": {"type": "string"}},
                        "required": ["source_sku_id", "family_id", "visual_evidence"]}},
                    "media_derivative_overrides": {
                        "type": "array", "items": derivative_override},
                    "media_edits": {"type": "array", "items": edit},
                    "media_omissions": {"type": "array", "items": omission},
                },
                "required": ["title", "variant_labels", "collapsed_sku_aliases",
                             "withheld_skus", "description_sections", "copy_priorities",
                             "primary_family_id",
                             "gallery_order", "sku_media_overrides", "media_edits",
                             "media_derivative_overrides", "media_omissions"],
            }

    @staticmethod
    def copy_schema() -> dict[str, Any]:
            """The same editor can revise copy without deciding SKU or media again."""
            whole = GlobalMerchEditorClient.schema()
            keys = ("description_sections", "copy_priorities")
            return {"type": "object", "additionalProperties": False,
                    "properties": {**{key: whole["properties"][key] for key in keys},
                                   "inspect_asset_ids": {"type": "array", "items": {"type": "string"}}},
                    "required": [*keys, "inspect_asset_ids"]}

    @staticmethod
    def _editor_evidence_view(evidence: dict[str, Any]) -> dict[str, Any]:
            """Preserve the consumer page and every image's OCR in source order.

            Old category labels are hints, never an input gate. Repetition can be
            resolved editorially only after the original layout is visible.
            """
            facts = list(evidence.get("structured_facts") or [])
            regions_by_asset: dict[str, list[dict[str, Any]]] = {}
            for row in evidence.get("ocr_regions") or []:
                asset_id = str(row.get("asset_id") or "")
                regions_by_asset.setdefault(asset_id, []).append({
                    **row, "reading_order": len(regions_by_asset.get(asset_id, [])) + 1})
            image_groups = []
            registered: set[str] = set()
            for source_order, asset in enumerate(evidence.get("image_assets") or [], 1):
                asset_id = str(asset.get("asset_id") or "")
                registered.add(asset_id)
                image_groups.append({
                    "asset_id": asset_id, "source_order": source_order,
                    "role": asset.get("role"), "sku_bindings": asset.get("bindings") or [],
                    "source_sha256": asset.get("source_sha256"),
                    "read_status": asset.get("read_status"),
                    "ocr_status": asset.get("ocr_status"),
                    "ocr_regions": regions_by_asset.get(asset_id, []),
                })
            for asset_id, rows in regions_by_asset.items():
                if asset_id not in registered:
                    image_groups.append({"asset_id": asset_id, "source_order": None,
                                         "ocr_regions": rows})
            families = []
            for family in evidence.get("media_families") or []:
                families.append({
                    "family_id": family.get("family_id"),
                    "source_sha256": family.get("source_sha256"),
                    "sku_bindings": family.get("sku_bindings") or [],
                    "read_status": family.get("read_status"),
                    "visual_facts": family.get("visual_facts") or {},
                    "sources": [
                        {key: source.get(key) for key in (
                            "asset_id", "role", "width", "height", "read_status")
                         if source.get(key) not in (None, "")}
                        for source in [*(family.get("latest_apify_sources") or []),
                                       *(family.get("historical_apify_sources") or [])]
                    ],
                    "historical_derivatives": [
                        {key: derivative.get(key) for key in (
                            "derivative_id", "kind", "legacy_asset_id")
                         if derivative.get(key) not in (None, "")}
                        for derivative in family.get("historical_ai_derivatives") or []
                    ],
                })
            return {
                "schema": evidence.get("schema"),
                "cluster_id": evidence.get("cluster_id"),
                "brand": evidence.get("brand"),
                "source_title": evidence.get("source_title"),
                "category_path": evidence.get("category_path") or [],
                # The full raw SKU survives in ProductEvidenceBundle for audit;
                # the Editor sees one parsed axis view, never Apify property IDs.
                "authoritative_skus": [{
                    key: row.get(key) for key in (
                        "source_sku_id", "platform_sku_id", "variant_key",
                        "source_axes", "consumer_source_label",
                        "procurement_price_cny", "billable_weight_g",
                        "reference_weight_g", "source_image_authority",
                        "latest_apify_image_url", "historical_recovered_image_url",
                        "source_image_binding_method", "source_media_binding",
                    ) if row.get(key) not in (None, "")
                } for row in evidence.get("authoritative_skus") or []],
                "sku_evidence_matrix": [{
                    key: row.get(key) for key in (
                        "source_sku_id", "source_axes", "pack_evidence",
                        "supplier_inventory_quantity", "procurement_price_cny",
                        "procurement_price_source_field", "billable_weight_g",
                        "weight_basis", "source_image_authority",
                        "source_image_binding_method", "image_family_id",
                        "partial_candidate_family_id", "price_anomaly",
                    ) if row.get(key) not in (None, "")
                } for row in evidence.get("sku_evidence_matrix") or []],
                "structured_facts": facts,
                "source_text_blocks": list(evidence.get("source_text_blocks") or []),
                "supplementary_source_text_blocks": list(
                    evidence.get("supplementary_source_text_blocks") or []),
                "image_ocr_groups": image_groups,
                "ocr_regions": list(evidence.get("ocr_regions") or []),
                "media_families": families,
                "failures": evidence.get("failures") or [],
                "evidence_coverage": evidence.get("evidence_coverage") or {},
            }
