"""Neutral contracts for the isolated three-stage merchandising runtime.

This module intentionally has no dependency on any merchandising runtime.  It
is safe for repositories, API readers and the Shadow worker to share.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any


SHADOW_SCHEMA = "three-stage-shadow-v2-sku-matrix"
EVIDENCE_SCHEMA = "product-evidence-bundle-v6-source-copy-segments"
CANDIDATE_SCHEMA = "candidate-listing-manifest-v3-buyer-copy-priorities"
QA_SCHEMA = "listing-qa-report-v5-buyer-copy-checks"
SHADOW_POLICY = hashlib.sha256(
    b"apify-authoritative-three-stage-shadow-v1"
).hexdigest()
EVIDENCE_VISION_POLICY = hashlib.sha256(
    b"neutral-full-image-ocr-and-visual-facts-v2-localization-strict"
).hexdigest()

V3_OBJECT_TYPES = {
    "SOURCE_PRODUCT_SNAPSHOT",
    "SKU_CORE_COMPLETION",
    "UNIVERSAL_MERCH_EVIDENCE",
    "MEDIA_CENSUS",
    "GLOBAL_MERCH_DECISION",
    "MEDIA_ARTIFACT_FAMILY",
    "LOCALIZED_LISTING_MANIFEST",
    "FINAL_LISTING_DECISION",
}

MEDIA_OMISSION_REASONS = {
    "OTHER_PRODUCT",
    "TRUE_NEAR_DUPLICATE",
    "CHANNEL_SERVICE_ONLY",
    "COPY_EVIDENCE_ONLY",
    "UNREADABLE",
    "SEVERE_DISTORTION",
    "WRONG_SKU_BINDING",
}

SKU_WITHHOLD_REASONS = {
    "SELLER_SERVICE_OPTION",
    "TRIAL_OR_GIFT",
    "SOURCE_UNAVAILABLE",
    "IMAGE_BINDING_UNRESOLVED",
    "PACK_PRICE_ANOMALY",
}


def json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
