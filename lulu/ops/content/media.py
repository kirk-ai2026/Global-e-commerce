from __future__ import annotations

import html
import io
import re

from PIL import Image

from lulu.core import digest

from .localize import text_for

CHANNEL = re.compile(
    r"天[猫貓]|淘[宝寶]|店[铺鋪]|客服|旗舰店|旗艦店|优惠券|優惠券|满减|滿減|包邮|包郵|到手价|到手價|保税仓|保稅倉|发货|發貨|扫码进店|掃碼進店|¥|￥|\bCNY\b",
    re.I,
)
CHANNEL_CATEGORIES = {"PLATFORM", "PRICE_PROMOTION", "LOGISTICS", "SERVICE"}


def assess_image(image, regions, p, selected_skus, authoritative_skus=None):
    """Visual observations are facts; eligibility is role/market-specific."""
    facts = image.get("visual_facts") or {}
    product = []
    channel = []
    languages = []
    other_sku_regions = 0
    applicable_regions = set()
    source_ids = set(authoritative_skus or selected_skus)
    semantic_tags = set()
    for region in regions:
        value = str(region.get("source_text") or "")
        applicable = set(str(x) for x in region.get("applicable_sku_ids") or [])
        semantic_tags.update(applicable - source_ids)
        applicable = applicable.intersection(source_ids)
        if applicable and not applicable.intersection(selected_skus):
            other_sku_regions += 1
            continue
        applicable_regions.update(applicable.intersection(selected_skus))
        if region.get("placement") == "PACKAGE":
            continue
        if region.get("category") in CHANNEL_CATEGORIES or CHANNEL.search(value):
            channel.append(region)
        elif value.strip():
            product.append(region)
        languages.extend(
            re.findall(r"[\u3040-\u30ff]|[\uac00-\ud7af]|[\u4e00-\u9fff]", value)
        )
    result = {
        "asset_id": image.get("asset_id"),
        "profile_id": p.profile_id,
        "profile_sha": p.sha,
        "role": "DESCRIPTION",
        "source_sha": image.get("source_sha256"),
        "reasons": [],
        "operation": "ORIGINAL_REUSE",
        "product_regions": len(product),
        "channel_regions": len(channel),
    }
    if image.get("read_status") != "READABLE":
        result["reasons"].append("SOURCE_UNREADABLE")
    if image.get("ocr_status") in {"PENDING", "FAILED", "UNAVAILABLE", "UNKNOWN", None}:
        result["reasons"].append("OCR_MISSING")
    bindings = {
        str(b.get("platform_sku_id") or b.get("source_sku_id"))
        for b in image.get("bindings") or []
    }
    bindings.discard("None")
    if bindings and not bindings.intersection(selected_skus):
        result["reasons"].append("OTHER_SKU_ONLY")
    if facts.get("contains_non_target_product"):
        result["reasons"].append("OTHER_PRODUCT_VISUAL")
    if other_sku_regions:
        result["reasons"].append("OTHER_SKU_INFORMATION_PRESENT")
    applicable = bindings.intersection(selected_skus) or applicable_regions
    if len(source_ids) == 1 and source_ids == set(selected_skus):
        applicable = set(selected_skus)
    if len(selected_skus) > 1 and not applicable:
        result["reasons"].append("SKU_SCOPE_UNCONFIRMED")
    result["applicable_sku_ids"] = sorted(
        applicable or (selected_skus if len(selected_skus) == 1 else set())
    )
    result["source_semantic_tags"] = sorted(semantic_tags)
    if channel:
        result["operation"] = "CROP_OR_LOCAL_EDIT"
        result["reasons"].append("CHANNEL_REGION_PRESENT")
    elif facts.get("channel_content"):
        result["operation"] = "REGION_REVIEW"
        result["reasons"].append("LEGACY_CHANNEL_FLAG_NEEDS_REGION_REVIEW")
    if not p.retain_product_text:
        if p.locale == "en" and (languages or facts.get("external_simplified_text")):
            result["reasons"].append("EXTERNAL_NON_ENGLISH_TEXT")
            result["operation"] = "LOCALIZE_TEXT"
        if p.locale == "zh-Hant" and facts.get("external_simplified_text"):
            result["reasons"].append("EXTERNAL_SIMPLIFIED_TEXT")
            result["operation"] = "LOCALIZE_TEXT"
    if not product:
        result["reasons"].append("NO_DISTINCT_PRODUCT_INFORMATION")
    result["status"] = "PASS" if not result["reasons"] else "SOURCE_ONLY"
    result["marketing_retained"] = any(
        r.get("category") == "MARKETING" for r in product
    )
    result["binding_sha"] = digest(sorted(selected_skus))
    return result


def safe_edge_crop_box(image, regions, width, height):
    if (image.get("visual_facts") or {}).get("contains_physical_product"):
        return None
    if any(int(r.get("segment_count") or 1) > 1 for r in regions):
        return None
    boxes = []
    protected = []
    for r in regions:
        box = r.get("bbox")
        if (
            not isinstance(box, list)
            or len(box) != 4
            or float(r.get("confidence") or 0) < 0.9
        ):
            return None
        x1, y1, x2, y2 = [float(x) for x in box]
        if not 0 <= x1 < x2 <= 1000 or not 0 <= y1 < y2 <= 1000:
            return None
        pixel = (
            int(x1 * width / 1000),
            int(y1 * height / 1000),
            int(x2 * width / 1000),
            int(y2 * height / 1000),
        )
        is_channel = r.get("placement") != "PACKAGE" and (
            r.get("category") in CHANNEL_CATEGORIES
            or CHANNEL.search(str(r.get("source_text") or ""))
        )
        if is_channel:
            if re.search(
                r"成分|容量|净含量|淨含量|使用方法|食用|过敏|過敏|营养|營養",
                str(r.get("source_text") or ""),
            ):
                return None
            boxes.append(pixel)
        else:
            protected.append(pixel)
    if not boxes or not protected:
        return None
    top = 0
    bottom = height
    for _, y1, _, y2 in boxes:
        if y2 <= height * 0.15:
            top = max(top, y2 + 1)
        elif y1 >= height * 0.85:
            bottom = min(bottom, y1 - 1)
        else:
            return None
    if top >= bottom or bottom - top < height * 0.8:
        return None
    if any(y1 < top or y2 > bottom for _, y1, _, y2 in protected):
        return None
    return (0, top, width, bottom)


def prepare_media(evidence, p, selected_skus, assets=None, repo=None):
    bundle = evidence["bundle"]
    source_ids = {
        str(s.get("source_sku_id") or s.get("platform_sku_id"))
        for s in bundle.get("authoritative_skus") or []
    }
    owned = evidence["assets"]
    by_asset = {}
    for region in bundle.get("ocr_regions") or []:
        by_asset.setdefault(str(region.get("asset_id")), []).append(region)
    adopted = []
    ledger = []
    seen = set()
    for image in bundle.get("image_assets") or []:
        aid = str(image.get("asset_id"))
        regions = by_asset.get(aid, [])
        binding_sha = digest(
            [sorted(selected_skus), sorted(source_ids), image, regions]
        )
        cached = None
        if repo and image.get("source_sha256"):
            from psycopg.types.json import Jsonb

            with repo.connect() as c:
                found = c.execute(
                    "SELECT result FROM content_media_qa WHERE asset_sha=%s AND profile_sha=%s AND binding_sha=%s",
                    (image["source_sha256"], p.sha, binding_sha),
                ).fetchone()
                cached = found["result"] if found else None
        qa = cached or assess_image(image, regions, p, selected_skus, source_ids)
        if repo and image.get("source_sha256") and not cached:
            with repo.connect() as c:
                c.execute(
                    "INSERT INTO content_media_qa VALUES(%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                    (image["source_sha256"], p.sha, binding_sha, Jsonb(qa)),
                )
        asset = owned.get(aid)
        if not asset:
            qa["status"] = "SOURCE_ONLY"
            qa["reasons"].append("OWNED_ORIGINAL_MISSING")
        if (
            asset
            and assets
            and repo
            and p.retain_product_text
            and qa["reasons"] == ["CHANNEL_REGION_PRESENT"]
            and image.get("role") not in {"PRIMARY", "VARIANT", "SKU"}
        ):
            box = safe_edge_crop_box(image, regions, asset["width"], asset["height"])
            if box:
                with Image.open(
                    io.BytesIO(assets.read(asset["object_key"]))
                ) as original:
                    cropped = original.crop(box)
                    cropped.save(buffer := io.BytesIO(), "PNG")
                    width, height = cropped.size
                data = buffer.getvalue()
                sha, key = assets.put(data, "image/png")
                repo.save_media(
                    sha,
                    key,
                    "image/png",
                    len(data),
                    {
                        "kind": "DETERMINISTIC_EDGE_CROP",
                        "parent_sha": asset["sha256"],
                        "crop_box": box,
                        "proof": "All protected OCR/package regions survive; original retained",
                    },
                )
                asset = {
                    **asset,
                    "parent_sha": asset["sha256"],
                    "sha256": sha,
                    "object_key": key,
                    "url": "/api/media/" + sha,
                    "width": width,
                    "height": height,
                    "crop_box": box,
                }
                qa = {
                    **qa,
                    "status": "PASS",
                    "operation": "SAFE_EDGE_CROP",
                    "reasons": [],
                    "crop_proof": box,
                }
        ledger.append(qa)
        if qa["status"] != "PASS" or asset["sha256"] in seen:
            continue
        seen.add(asset["sha256"])
        # Primary/SKU photos stay in their existing roles; supplementary information goes here.
        if image.get("role") in {"PRIMARY", "VARIANT", "SKU"}:
            continue
        text = " ".join(
            str(r.get("source_text") or "")
            for r in regions
            if r.get("placement") != "PACKAGE"
            and r.get("category") not in CHANNEL_CATEGORIES
        )
        adopted.append(
            {
                **asset,
                "source_asset_id": aid,
                "order": len(adopted),
                "applicable_sku_ids": qa["applicable_sku_ids"],
                "caption": text_for(text[:90], p.locale),
                "qa": qa,
            }
        )
    return {
        "description_media": adopted,
        "media_ledger": ledger,
        "image_edits": 0,
        "deterministic_crops": sum(x["operation"] == "SAFE_EDGE_CROP" for x in ledger),
        "edit_pending": sum(x["operation"] not in {"ORIGINAL_REUSE"} for x in ledger),
    }


def assemble_html(text_html, media, *, absolute_base=""):
    sections = [text_html]
    for item in media:
        sha = str(item.get("sha256") or "")
        if (
            not re.fullmatch(r"[a-f0-9]{64}", sha)
            or item.get("qa", {}).get("status") != "PASS"
        ):
            continue
        url = absolute_base.rstrip("/") + "/api/media/" + sha
        alt = html.escape(item.get("caption") or "", quote=True)
        width = min(int(item.get("width") or 800), 1200)
        sections.append(
            f'<figure><img src="{url}" alt="{alt}" loading="lazy" decoding="async" width="{width}"><figcaption>{alt}</figcaption></figure>'
        )
    return "".join(sections)
