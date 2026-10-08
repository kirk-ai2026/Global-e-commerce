from __future__ import annotations

import copy
import re
from collections import Counter

from lulu.core import digest

from .localization import VERSION, canonical_numbers, hans, safe_html
from .money import quote

REGIONS = {
    "JP": "日本",
    "KR": "韩国",
    "CN": "中国大陆",
    "TW": "台湾",
    "HK": "香港",
    "MO": "澳门",
    "TH": "泰国",
    "MY": "马来西亚",
    "SG": "新加坡",
    "VN": "越南",
    "ID": "印度尼西亚",
    "PH": "菲律宾",
    "BN": "文莱",
    "KH": "柬埔寨",
    "LA": "老挝",
    "MM": "缅甸",
    "TL": "东帝汶",
    "MN": "蒙古",
    "KP": "朝鲜",
}
CATEGORIES = {
    "beauty": "美妆个护",
    "food": "食品",
    "supplements": "保健品",
    "mother_baby": "母婴",
    "unknown": "待分类",
}


def strip_twd(value):
    if isinstance(value, dict):
        return {
            k: strip_twd(v)
            for k, v in value.items()
            if not k.endswith("_twd")
            and k not in {"price_formula", "country_origin_name_zh_tw"}
        }
    if isinstance(value, list):
        return [strip_twd(v) for v in value]
    return value


def watermark(row, fx):
    return digest(
        [
            row["snapshot_id"],
            row["localized"]["content_sha"],
            row["patch"],
            row["status"],
            (fx or {}).get("fx_id"),
            row.get("source_changed"),
            VERSION,
        ]
    )


def effective(row, fx):
    source = row["source"]
    d = copy.deepcopy(source["derived"]["detail"])
    original = d.get("consumer") or {}
    local = row["localized"]
    patch = row["patch"]
    overrides = patch.get("content_override") or {}
    c = strip_twd(original)
    c.update(
        {
            "locale": "zh-Hans",
            "market": "CA",
            "currency": "CAD",
            "title": hans(overrides.get("title", local["title"])),
            "brand": hans(
                original.get("brand") or row["brand_fact"].get("brand_name") or ""
            ),
            "description_html": safe_html(
                hans(overrides.get("description_html", local["description_html"]))
            ),
            "seo_title": hans(overrides.get("seo_title", local["seo_title"])),
            "seo_description": hans(
                overrides.get("seo_description", local["seo_description"])
            ),
        }
    )
    region = row["brand_fact"].get("code", "")
    c["brand_country_code"] = region
    c["brand_country_name"] = REGIONS.get(region, "待确认")
    # Product manufacture origin is not inferred from brand country.
    c["manufacturing_origin_name"] = ""
    origin = re.search(r"产地[：:]\s*([^<。；]+)", c["description_html"])
    if origin:
        c["manufacturing_origin_name"] = origin[1].strip()
    p = strip_twd(d.get("procurement") or {})
    raw_by_id = {
        str(v.get("platform_sku_id")): v
        for v in (d.get("procurement") or {}).get("variants") or []
    }
    price_override = (patch.get("price_weight_override") or {}).get("variants") or {}
    variants = []
    quote_errors = []
    for original_v in original.get("variants") or []:
        sid = str(original_v.get("platform_sku_id"))
        v = strip_twd(original_v)
        v.update(strip_twd(raw_by_id.get(sid, {})))
        for name in ["procurement_price_cny", "billable_weight_g"]:
            if name in price_override.get(sid, {}):
                v[name] = price_override[sid][name]
        label = local["sku_labels"].get(
            sid, hans(original_v.get("localized_label") or original_v.get("title"))
        )
        v["title"] = label
        v["localized_label"] = label
        v["normalized_variant_title"] = label
        v["options"] = {
            hans(k): hans(value) for k, value in original_v.get("options", {}).items()
        }
        money = quote(v, fx)
        v.update(
            {
                k: money[k]
                for k in [
                    "retail_price_cad",
                    "raw_price_cad",
                    "billable_weight_g",
                    "weight_basis",
                ]
            }
        )
        v["price_formula"] = money
        v["procurement_price"] = v.get("procurement_price_cny")
        v["price_status"] = money["status"]
        quote_errors.extend(sid + ":" + error for error in money["errors"])
        variants.append(v)
    c["variants"] = variants
    p["variants"] = variants
    p["withheld_variants"] = [
        simplify_tree(v) for v in p.get("withheld_variants") or []
    ]
    base_media = copy.deepcopy(c.get("media") or [])
    media_over = (patch.get("media_override") or {}).get("assets") or {}
    shown = []
    hidden = []
    for i, m in enumerate(base_media):
        m["alt"] = local["media_alt"]
        override = media_over.get(m.get("asset_id"), {})
        m["_order"] = override.get("order", i)
        if override.get("is_primary"):
            m["role"] = m["media_role"] = "PRIMARY"
        (hidden if override.get("visible") is False else shown).append(m)
    shown.sort(key=lambda m: m["_order"])
    c["media"] = shown
    source_ready = d.get("readiness") or {}
    errors = list(source_ready.get("hard_blockers") or [])
    errors += local["qa"]["errors"] + quote_errors
    if source.get("media_failures"):
        errors.append("成品媒体同步待补证")
    if row["scope"] == "REVIEW":
        errors.append("品牌所属地区待确认")
    if not any(
        str(m.get("role") or m.get("media_role")).upper() == "PRIMARY"
        and m.get("lulu_sha256")
        for m in shown
    ):
        errors.append("展示主图缺失")
    if row.get("source_changed"):
        errors.append("来源已更新，请确认运营修改")
    if canonical_numbers(overrides.get("title", local["title"])) != canonical_numbers(
        local["title"]
    ):
        errors.append("运营标题数字变更需核验")
    c["price_notice"] = "临时参考售价／沿用台湾系数；加拿大实际履约及可售资格独立验证"
    c["fx_snapshot"] = fx
    c["price_range_cad"] = price_range(variants)
    manifest = {
        "schema": "lulu-ops-manifest-v1",
        "application": "lulu",
        "locale": "zh-Hans",
        "market": "CA",
        "currency": "CAD",
        "cluster_id": row["cluster_id"],
        "consumer": c,
        "source_snapshot_id": row["snapshot_id"],
        "fx_id": (fx or {}).get("fx_id"),
    }
    sha = digest(manifest)
    manifest["manifest_sha256"] = sha
    listing = {
        k: v
        for k, v in (d.get("listing") or {}).items()
        if k in {"cluster_id", "input_sha256", "created_at"}
    }
    listing.update(
        {
            "status": row["status"],
            "title": c["title"],
            "brand": c["brand"],
            "updated_at": row["updated_at"],
            "stale_reason": "来源已更新，运营修改保留待确认"
            if row.get("source_changed")
            else None,
            "manifest_sha256": sha,
            "shopify_product_gid": None,
            "commerce_product_id": None,
        }
    )
    result = {
        "listing": listing,
        "readiness": {
            **source_ready,
            "eligible": not errors and bool(source_ready.get("eligible")),
            "hard_blockers": errors,
            "upstream_standard_reused": True,
            "canadian_sale_eligibility_verified": False,
        },
        "manifest": manifest,
        "consumer": c,
        "procurement": p,
        "metrics": {
            **(d.get("metrics") or {}),
            "content_level": "C2_MERCH_STANDARD"
            if local["qa"]["verdict"] == "PASS"
            else "C1_DRAFT_MINIMUM",
        },
        "edit": {
            k: c[k]
            for k in ["title", "description_html", "seo_title", "seo_description"]
        },
        "history": row.get("history", []),
        "hidden_media": hidden,
        "watermark": watermark(row, fx),
        "release_candidate": None,
        "taiwan_evidence": [],
        "source_audit": {
            "upstream_manifest": d.get("manifest"),
            "upstream_status": (d.get("listing") or {}).get("status"),
            "upstream_readiness": source_ready,
            "authoritative_skus": source["upstream"]
            .get("media_workspace", {})
            .get("authoritative_skus", []),
            "upstream_media_snapshot": source["upstream"]
            .get("media_workspace", {})
            .get("source_snapshot_id"),
            "brand_evidence": row["brand_fact"],
            "taiwan_evidence": d.get("taiwan_evidence"),
            "captured_at": source.get("captured_at"),
            "original_source_sha": source["source_sha"],
        },
        "currency": "CAD",
        "locale": "zh-Hans",
        "price_notice": c["price_notice"],
        "fx_snapshot": fx,
        "shopify_writes": 0,
    }
    return result


def simplify_tree(value):
    if isinstance(value, str):
        return hans(value)
    if isinstance(value, list):
        return [simplify_tree(x) for x in value]
    if isinstance(value, dict):
        return {k: simplify_tree(v) for k, v in value.items()}
    return value


def price_range(variants):
    prices = [
        float(v["retail_price_cad"])
        for v in variants
        if v.get("retail_price_cad") is not None
    ]
    return [min(prices), max(prices)] if prices else []


def list_projection(repo, **filters):
    fx = repo.current_fx()
    rows = repo.all_listings()
    items = []
    region_counts = Counter()
    categories = Counter()
    origins = Counter()
    for row in rows:
        if row["scope"] == "REVIEW" and filters.get("status") != "BRAND_REVIEW":
            continue
        d = effective(row, fx)
        c = d["consumer"]
        listing = d["listing"]
        media = c["media"]
        variants = c["variants"]
        country = c["brand_country_name"]
        category = row["category"]
        region_counts[country] += 1
        categories[category] += 1
        if c["manufacturing_origin_name"]:
            origins[c["manufacturing_origin_name"]] += 1
        item = {
            "cluster_id": row["cluster_id"],
            "title": c["title"],
            "brand": c["brand"],
            "status": listing["status"],
            "variant_count": len(variants),
            "published_variant_count": len(variants),
            "source_variant_count": d["procurement"].get(
                "source_variant_count", len(variants)
            ),
            "withheld_variant_count": len(
                d["procurement"].get("withheld_variants") or []
            ),
            "price_range_cad": price_range(variants),
            "media_level": d["metrics"].get("media_level"),
            "media_count": len(media),
            "primary_image_url": next(
                (m["url"] for m in media if m.get("role") == "PRIMARY"),
                media[0]["url"] if media else None,
            ),
            "brand_country": country,
            "brand_country_code": c["brand_country_code"],
            "major_category": category,
            "origin": c["manufacturing_origin_name"],
            "review_pinned": False,
            "currency": "CAD",
            "locale": "zh-Hans",
            "ready_for_internal_review": d["readiness"]["eligible"],
            "price_notice": c["price_notice"],
        }
        if filters.get("status") and filters["status"] != item["status"]:
            continue
        if filters.get("category") and filters["category"] != category:
            continue
        if filters.get("brand_country") and filters["brand_country"] not in {
            country,
            c["brand_country_code"],
        }:
            continue
        if filters.get("origin") and filters["origin"] != item["origin"]:
            continue
        if filters.get("media") == "M2" and not str(item["media_level"]).startswith(
            ("M2", "M3")
        ):
            continue
        if (
            filters.get("q")
            and filters["q"].casefold()
            not in (
                " ".join(
                    [
                        c["title"],
                        c["brand"],
                        row["cluster_id"],
                        d["procurement"].get("url", ""),
                    ]
                    + [str(v.get("platform_sku_id", "")) for v in variants]
                )
            ).casefold()
        ):
            continue
        items.append(item)
    page = max(1, int(filters.get("page", 1)))
    size = max(10, min(100, int(filters.get("page_size", 40))))
    start = (page - 1) * size
    return {
        "items": items[start : start + size],
        "total": len(items),
        "page": page,
        "page_size": size,
        "environment": "lulu",
        "currency": "CAD",
        "locale": "zh-Hans",
        "fx_snapshot": fx,
        "review_cohort": None,
        "categories": [
            {"value": k, "label": CATEGORIES.get(k, k), "count": v}
            for k, v in categories.items()
        ],
        "brand_countries": [
            {"value": k, "label": k, "count": v} for k, v in region_counts.items()
        ],
        "origins": [{"value": k, "label": k, "count": v} for k, v in origins.items()],
    }
