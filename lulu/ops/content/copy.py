from __future__ import annotations

import base64
import copy
import html as html_module
import io
import re
from decimal import Decimal

from PIL import Image, ImageDraw

from lulu.core import digest
from lulu.ops.localization import safe_html

from .localize import text_for
from .media import CHANNEL
from .upstream.kernel import GlobalMerchEditorClient, render_description_sections


def render_for(raw_sections, locale):
    sections, _ = render_description_sections(raw_sections)
    for section in sections:
        section["heading"] = text_for(section.get("heading"), locale)
        section["html"] = text_for(section.get("html"), locale)
        rows = section.get("table_rows") or []
        for row in rows:
            for key in ["label", "basis", "unit"]:
                row[key] = text_for(row.get(key), locale)
        if rows:
            labels = {
                "zh-Hans": ["项目", "含量", "基准"],
                "zh-Hant": ["項目", "含量", "基準"],
                "en": ["Item", "Amount", "Basis"],
            }[locale]
            bases = {str(r.get("basis") or "").strip() for r in rows}
            shared = next(iter(bases)) if len(bases) == 1 else None
            headers = [labels[0], shared or labels[1]] if shared is not None else labels
            table = (
                "<table><thead><tr>"
                + "".join("<th>" + html_module.escape(x) + "</th>" for x in headers)
                + "</tr></thead><tbody>"
            )
            for row in rows:
                cells = [
                    str(row.get("label") or ""),
                    (
                        str(row.get("amount") or "") + " " + str(row.get("unit") or "")
                    ).strip(),
                ]
                if shared is None:
                    cells.append(str(row.get("basis") or ""))
                table += (
                    "<tr>"
                    + "".join("<td>" + html_module.escape(x) + "</td>" for x in cells)
                    + "</tr>"
                )
            table += "</tbody></table>"
            section["html"] = re.sub(
                r"<table\b.*?</table>", lambda _: table, section["html"], flags=re.S
            )
        section["html"] = safe_html(section["html"])
    result = "".join(
        ("<h2>" + html_module.escape(s["heading"]) + "</h2>" if s["heading"] else "")
        + s["html"]
        for s in sections
    )
    return sections, result


def review_schema():
    issue = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "kind": {"type": "string"},
            "source_evidence_ids": {"type": "array", "items": {"type": "string"}},
            "explanation": {"type": "string"},
        },
        "required": ["kind", "source_evidence_ids", "explanation"],
    }
    disposition = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "action": {"type": "string", "enum": ["ADOPTED", "MERGED", "EXCLUDED"]},
            "reason": {"type": "string"},
        },
        "required": ["evidence_ids", "action", "reason"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["PASS", "REPAIR"]},
            "issues": {"type": "array", "items": issue},
            "source_dispositions": {"type": "array", "items": disposition},
            "inspect_asset_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["status", "issues", "source_dispositions", "inspect_asset_ids"],
    }


def evidence_view(bundle):
    view = GlobalMerchEditorClient._editor_evidence_view(bundle)
    view["sku_scope_note"] = (
        "authoritative_skus 中列明的真实来源 SKU ID 才是采购规格标识。OCR applicable_sku_ids 历史字段可能含 orange/all 等识别提示，不能把这些颜色或语义标签当作真实 SKU ID 排除商品事实；仍须对照具体款式/配方、当前发布子集与原图，不跨款式挪用事实。"
    )
    for k in ["media_families", "sku_evidence_matrix", "ocr_regions"]:
        view.pop(k, None)
    for row in view["authoritative_skus"]:
        for k in ["procurement_price_cny", "billable_weight_g", "reference_weight_g"]:
            row.pop(k, None)
    return view


def image_input(asset_id, owned, assets, *, detail="high"):
    data = assets.read(owned["object_key"])
    result = []
    with Image.open(io.BytesIO(data)) as im:
        step = max(900, int(im.width * 1.25))
        overlap = min(120, step // 8)
        if im.height > im.width * 2 and im.height > 1600:
            boxes = []
            top = 0
            while top < im.height:
                bottom = min(im.height, top + step)
                boxes.append((0, top, im.width, bottom))
                if bottom == im.height:
                    break
                top = bottom - overlap
        else:
            boxes = [(0, 0, im.width, im.height)]
        for i, box in enumerate(boxes):
            im.crop(box).convert("RGB").save(buffer := io.BytesIO(), "JPEG", quality=92)
            result.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"原始证据图 {asset_id}，高清分段 {i + 1}/{len(boxes)}，原图坐标 {box}",
                    },
                    {
                        "type": "input_image",
                        "detail": detail,
                        "image_url": "data:image/jpeg;base64,"
                        + base64.b64encode(buffer.getvalue()).decode(),
                    },
                ]
            )
    return result


def source_contacts(evidence, assets):
    # Contact sheets are model-reading aids; the displayed originals stay byte-identical.
    owned = list(evidence["assets"].items())
    content = []
    for offset in range(0, len(owned), 12):
        batch = owned[offset : offset + 12]
        sheet = Image.new("RGB", (1000, 320 * ((len(batch) + 3) // 4)), "white")
        draw = ImageDraw.Draw(sheet)
        for i, (aid, row) in enumerate(batch):
            with Image.open(io.BytesIO(assets.read(row["object_key"]))) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((242, 288))
                x = (i % 4) * 250
                y = (i // 4) * 320
                sheet.paste(thumb, (x, y + 20))
                draw.text((x + 2, y + 2), str(offset + i + 1), fill="black")
        sheet.save(buffer := io.BytesIO(), "JPEG", quality=82)
        content.extend(
            [
                {
                    "type": "input_text",
                    "text": "按原页面顺序的原图联系表："
                    + str(
                        [
                            {"index": offset + i + 1, "asset_id": a}
                            for i, (a, _) in enumerate(batch)
                        ]
                    ),
                },
                {
                    "type": "input_image",
                    "detail": "low",
                    "image_url": "data:image/jpeg;base64,"
                    + base64.b64encode(buffer.getvalue()).decode(),
                },
            ]
        )
    return content


def current_page(row):
    original = row["source"]["upstream"]["media_workspace"]["current_manifest"]
    patch = row["patch"].get("content_override") or {}
    return {
        "title": patch.get("title") or row["localized"]["title"],
        "variants": copy.deepcopy(
            row["source"]["derived"]["detail"]["consumer"]["variants"]
        ),
        "description_html": patch.get("description_html")
        or row["localized"]["description_html"],
        "description_sections": original.get("description_sections") or [],
    }


def generate(
    provider,
    job,
    evidence,
    p,
    page,
    assets,
    *,
    previous=None,
    issues=None,
    inspect_ids=None,
):
    content = source_contacts(evidence, assets) if not inspect_ids else []
    for aid in inspect_ids or []:
        if aid not in evidence["assets"]:
            raise ValueError("REQUESTED_ORIGINAL_UNAVAILABLE:" + aid)
        content.extend(image_input(aid, evidence["assets"][aid], assets))
    instructions = (
        p.instructions("COPY")
        + " 本次只编辑 Description，标题、SKU、价格、主图与图库不可改变。首次有关键字或数字不清楚时请求 inspect_asset_ids；高清核对后仍不清楚则省略该具体断言并说明缺口，不猜测。copy_priorities 返回空数组。"
    )
    payload = {
        "evidence": evidence_view(evidence["bundle"]),
        "current_page": page,
        "previous_draft": previous,
        "repair_issues": issues or [],
    }
    if issues:
        instructions += "本轮只修复 repair_issues 指明的问题，保留已经正确的事实与段落。具体数字、名称或条件无法从输入证实时，删去该具体断言，保留有来源的商品利益；不得换成另一个看似合理的事实。"
    if inspect_ids:
        instructions += "这是本次唯一高清复核，已附请求的原图。以原图核对，inspect_asset_ids 必须返回空数组；看不清的精确名称和数字不得补写。"
    raw, usage = provider.call(
        job,
        "COPY",
        instructions,
        payload,
        GlobalMerchEditorClient.copy_schema(),
        content,
    )
    requested = list(dict.fromkeys(raw.get("inspect_asset_ids") or []))
    if requested and not inspect_ids:
        return generate(
            provider,
            job,
            evidence,
            p,
            page,
            assets,
            previous=raw,
            issues=issues,
            inspect_ids=requested,
        )
    if requested:
        raise ValueError("HIGH_RES_REVIEW_INCOMPLETE")
    sections, html = render_for(raw["description_sections"], p.locale)
    return {
        "description_sections": sections,
        "description_text_html": html,
        "copy_contract": p.copy_contract,
    }, usage


def review(provider, job, evidence, p, page, candidate, assets, *, inspect_ids=None):
    inputs = []
    for aid in inspect_ids or []:
        if aid not in evidence["assets"]:
            raise ValueError("REQUESTED_ORIGINAL_UNAVAILABLE:" + aid)
        inputs.extend(image_input(aid, evidence["assets"][aid], assets))
    result, usage = provider.call(
        job,
        "SOURCE_REVIEW",
        p.instructions("REVIEW")
        + " 明确区分商品功能营销与平台店铺话术。逐项记录不同购买信息的采用、合并或排除及理由；低价值与重复信息可排除，不以旧类别标签作为过滤门。需要高清核对才返回 inspect_asset_ids，已经高清核对的图不再次请求。",
        {
            "source": evidence_view(evidence["bundle"]),
            "current_page": page,
            "candidate": candidate,
            "high_res_inspected": inspect_ids or [],
        },
        review_schema(),
        inputs,
    )
    requested = list(dict.fromkeys(result.get("inspect_asset_ids") or []))
    if requested and not inspect_ids:
        return review(
            provider, job, evidence, p, page, candidate, assets, inspect_ids=requested
        )
    if requested:
        result["status"] = "REPAIR"
        result["issues"].append(
            {
                "kind": "LOW_CONFIDENCE_SOURCE",
                "source_evidence_ids": [],
                "explanation": "高清复核后仍存在未确认信息",
            }
        )
    return result, usage


def static_qa(candidate, bundle, selected_skus):
    sections = candidate.get("description_sections") or []
    html = candidate.get("description_text_html") or ""
    rows = {
        str(r["evidence_id"]): r
        for r in [
            *(bundle.get("structured_facts") or []),
            *(bundle.get("ocr_regions") or []),
        ]
        if r.get("evidence_id")
    }
    errors = []
    authoritative = {
        str(r.get("source_sku_id") or r.get("platform_sku_id"))
        for r in bundle.get("authoritative_skus") or []
    }
    if not html.strip() or not sections:
        errors.append("DESCRIPTION_MISSING")
    if CHANNEL.search(re.sub("<[^>]+>", " ", html)):
        errors.append("CHANNEL_TEXT_IN_COPY")
    for section in sections:
        ids = set(section.get("evidence_ids") or [])
        if not ids:
            errors.append("SECTION_WITHOUT_EVIDENCE")
        if ids - set(rows):
            errors.append("UNKNOWN_EVIDENCE_ID")
        for eid in ids.intersection(rows):
            applicable = set(str(x) for x in rows[eid].get("applicable_sku_ids") or [])
            known = applicable.intersection(authoritative)
            if known and not known.intersection(selected_skus):
                errors.append("WITHHELD_SKU_EVIDENCE_USED")
        for table_row in section.get("table_rows") or []:
            row_ids = set(table_row.get("evidence_ids") or [])
            source = " ".join(
                str(rows[eid].get(k) or "")
                for eid in row_ids.intersection(rows)
                for k in ["text", "value", "source_text", "original_text"]
            )

            def normalize(value):
                return {
                    str(Decimal(x).normalize())
                    for x in re.findall(r"\d+(?:\.\d+)?", value.replace(",", ""))
                }

            numbers = normalize(source)
            if not row_ids or row_ids - set(rows):
                errors.append("TABLE_SOURCE_MISSING")
            if not normalize(
                str(table_row.get("amount")) + " " + str(table_row.get("basis"))
            ).issubset(numbers):
                errors.append("TABLE_NUMBER_UNSUPPORTED")
            unit = str(table_row.get("unit") or "").strip()
            aliases = {
                "mg": {"mg", "毫克"},
                "g": {"g", "克", "公克"},
                "μg": {"μg", "ug", "mcg", "微克"},
                "ml": {"ml", "毫升"},
                "kcal": {"kcal", "千卡", "大卡"},
                "kj": {"kj", "千焦"},
                "%": {"%", "％"},
                "iu": {"iu", "国际单位", "國際單位"},
            }
            unit_aliases = next(
                (v for v in aliases.values() if unit.lower() in v), {unit.lower()}
            )
            if unit and not any(x in source.lower() for x in unit_aliases):
                errors.append("TABLE_UNIT_NEEDS_SOURCE_REVIEW")
    return {
        "status": "PASS" if not errors else "REPAIR",
        "errors": sorted(set(errors)),
        "text_sha": digest(safe_html(html)),
    }
