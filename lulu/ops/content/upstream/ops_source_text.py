"""Read-only, on-demand view of the evidence behind a listing's Description.

This module deliberately does not select a newer Apify snapshot.  Operators
must see the inputs attached to the current Manifest, not today's potentially
different source page.
"""
from __future__ import annotations

from typing import Any


def description_source_view(manifest: dict[str, Any],
                            evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not evidence:
        return {
            "available": False,
            "reason": "当前商品稿没有可核对的原始证据对象；不能把其他采集批次的资料冒充生成输入。",
            "source_snapshot_id": manifest.get("source_snapshot_id"),
            "social_references": (manifest.get("social_module") or {}).get("items") or
                                 manifest.get("social_references") or [],
        }

    cited: set[str] = set()
    for section in manifest.get("description_sections") or []:
        if isinstance(section, dict):
            cited.update(str(value) for value in section.get("evidence_ids") or [])
    for claim in manifest.get("claim_evidence") or []:
        if isinstance(claim, dict):
            cited.update(str(value) for value in claim.get("evidence_ids") or [])

    attributes = []
    detail_segments: dict[int, list[dict[str, Any]]] = {}
    for fact in evidence.get("structured_facts") or []:
        if not isinstance(fact, dict):
            continue
        kind = str(fact.get("evidence_kind") or "")
        if kind == "STRUCTURED_ATTRIBUTE":
            attributes.append({"evidence_id": fact.get("evidence_id"),
                               "source": {key: value for key, value in fact.items()
                                          if key not in {"evidence_id", "evidence_kind"}}})
        elif fact.get("source_evidence_id"):
            order = fact.get("source_order") or []
            index = order[0] if order and isinstance(order[0], int) else 0
            detail_segments.setdefault(index, []).append({
                "evidence_id": fact.get("evidence_id"),
                "text": fact.get("original_text") or fact.get("text") or "",
                "category": fact.get("category") or "OTHER",
                "cited": str(fact.get("evidence_id") or "") in cited,
            })

    details = [{"index": index + 1, "raw_html": str(raw),
                "segments": detail_segments.get(index, [])}
               for index, raw in enumerate(evidence.get("source_text_blocks") or [])]
    regions_by_asset: dict[str, list[dict[str, Any]]] = {}
    for region in evidence.get("ocr_regions") or []:
        if not isinstance(region, dict):
            continue
        asset_id = str(region.get("asset_id") or "")
        regions_by_asset.setdefault(asset_id, []).append({
            "evidence_id": region.get("evidence_id"),
            "source_text": region.get("source_text") or "",
            "category": region.get("category") or "",
            "placement": region.get("placement") or "",
            "confidence": region.get("confidence"),
            "cited": str(region.get("evidence_id") or "") in cited,
        })
    images = [{"asset_id": asset.get("asset_id"),
               "source_url": asset.get("url") or asset.get("source_url") or "",
               "role": asset.get("role") or "",
               "ocr_status": asset.get("ocr_status") or "UNKNOWN",
               "read_status": asset.get("read_status") or "UNKNOWN",
               "regions": regions_by_asset.pop(str(asset.get("asset_id") or ""), [])}
              for asset in evidence.get("image_assets") or [] if isinstance(asset, dict)]
    # A partial historical bundle may have OCR regions without an image row.
    images.extend({"asset_id": asset_id, "source_url": "", "role": "",
                   "ocr_status": "UNKNOWN", "read_status": "UNKNOWN", "regions": regions}
                  for asset_id, regions in regions_by_asset.items())
    skus = [{"source_sku_id": sku.get("source_sku_id") or sku.get("platform_sku_id"),
             "source_label": sku.get("source_label") or sku.get("title") or "",
             "source_axes": sku.get("source_axes") or [],
             "property_path": sku.get("property_path") or ""}
            for sku in evidence.get("authoritative_skus") or [] if isinstance(sku, dict)]
    return {
        "available": True,
        "source_snapshot_id": evidence.get("source_snapshot_id"),
        "source_title": evidence.get("source_title") or "",
        "sku_specs": skus,
        "attributes": attributes,
        "details": details,
        "ocr_images": images,
        "coverage": evidence.get("evidence_coverage") or {},
        "citation_available": bool(cited),
        "social_references": (manifest.get("social_module") or {}).get("items") or
                             manifest.get("social_references") or [],
    }
