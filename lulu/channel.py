"""Shopify payload builder and preflight only. Contains no HTTP writer."""

from __future__ import annotations

from .core import CURRENCY, MARKET, digest, money


def preflight(result, pricing=None, policy=None):
    pricing = pricing or {}
    policy = policy or {}
    blockers = []
    if result.get("status") != "CONTENT_READY":
        blockers.append("CONTENT_NOT_READY")
    if not policy.get("fulfillment_ready"):
        blockers.append("FULFILLMENT_NOT_CONFIGURED")
    if not policy.get("market_approved"):
        blockers.append("CA_MARKET_ELIGIBILITY_UNVERIFIED")
    if not policy.get("inventory_policy_ready"):
        blockers.append("PROCUREMENT_INVENTORY_POLICY_UNCONFIGURED")
    if not policy.get("media_rights_approved"):
        blockers.append("CHANNEL_MEDIA_RIGHTS_UNVERIFIED")
    if not policy.get("current_procurement_verified"):
        blockers.append("CURRENT_PROCUREMENT_NOT_VERIFIED")
    if not policy.get("pricing_version"):
        blockers.append("CAD_PRICING_NOT_CONFIGURED")
    variants = []
    labels = (result.get("content") or {}).get("sku_labels") or {}
    for sku in result.get("content_skus") or []:
        sid = sku["source_sku_id"]
        quote = pricing.get(sid) or {}
        if quote.get("currency") != CURRENCY or not money(quote.get("amount")):
            blockers.append("CAD_PRICE_MISSING:" + sid)
        if (
            not quote.get("fx_version")
            or not quote.get("weight_evidence")
            or not quote.get("procurement_evidence_ref")
        ):
            blockers.append("PRICE_EVIDENCE_MISSING:" + sid)
        if quote.get("source_price_cny") != sku.get("source_price_cny") or quote.get(
            "source_evidence_id"
        ) != result.get("source_evidence_id"):
            blockers.append("PRICE_SOURCE_MISMATCH:" + sid)
        variants.append(
            {
                "sku": "LULU-" + result["product_id"] + "-" + sid,
                "price": money(quote.get("amount")),
                "inventoryPolicy": "DENY",
                "inventoryItem": {"tracked": True},
                "optionValues": [{"optionName": "规格", "name": labels.get(sid, "")}],
            }
        )
    names = [v["optionValues"][0]["name"] for v in variants]
    if len(names) != len(set(names)):
        blockers.append("DUPLICATE_CONSUMER_OPTIONS")
    if len(variants) > 2048:
        blockers.append("SHOPIFY_VARIANT_LIMIT")
    if not variants:
        blockers.append("NO_CONTENT_SKUS")
    content = result.get("content") or {}
    # Mapping preview intentionally omits unset prices rather than supplying 0.
    mapping = {
        "title": content.get("title"),
        "descriptionHtml": content.get("description_html"),
        "vendor": content.get("brand"),
        "handle": "lulu-" + result["product_id"],
        "status": "DRAFT",
        "productOptions": [
            {
                "name": "规格",
                "position": 1,
                "values": [{"name": n} for n in dict.fromkeys(names)],
            }
        ],
        "variants": [
            {k: v for k, v in row.items() if v is not None} for row in variants
        ],
        "files": [
            {
                "originalSource": a["url"],
                "contentType": "IMAGE",
                "alt": a.get("alt", content.get("title", "")),
            }
            for a in result.get("media") or []
        ],
        "metafields": [
            {
                "namespace": "lulu",
                "key": "source_product_id",
                "type": "single_line_text_field",
                "value": result["product_id"],
            }
        ],
    }
    # Local asset URLs cannot be sent to Shopify. Remote deployment is a later step.
    if any(not a["url"].startswith("https://") for a in result.get("media") or []):
        blockers.append("CHANNEL_MEDIA_PUBLIC_URL_UNCONFIGURED")
    report = {
        "market": MARKET,
        "currency": CURRENCY,
        "locale": "zh-Hans",
        "status": "BLOCKED" if blockers else "CHANNEL_READY",
        "blockers": sorted(set(blockers)),
        "mapping_preview": mapping,
        "payload": None,
        "shopify_writes": 0,
    }
    if not blockers:
        report["payload"] = mapping
        report["payload_sha"] = digest(mapping)
    return report
