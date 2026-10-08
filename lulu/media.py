from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from PIL import Image

from .core import digest, file_sha, load


def bind_historical_axes(candidate, sku, evidence):
    """Require unchanged physical axes across collected supplier observations."""
    from .importer import source_skus

    signatures = set()
    binding_seen = False
    for observation in evidence:
        for old in source_skus(
            observation.get("raw", {}), observation.get("observed_at")
        ):
            if old["source_sku_id"] != sku["source_sku_id"] or not old.get(
                "source_options"
            ):
                continue
            signatures.add(digest(old["source_options"]))
            binding_seen |= same_url(
                old.get("source_image_url"), candidate.get("source_image_url")
            )
    expected = digest(sku.get("source_options") or [])
    return expected if binding_seen and signatures == {expected} else None


def same_url(a, b):
    return bool(a and b) and str(a).removeprefix("https:").removeprefix("http:") == str(
        b
    ).removeprefix("https:").removeprefix("http:")


def compatible(candidate, sku, facts):
    if candidate.get("qa_verdict") != "PASS":
        return "MEDIA_QA_NOT_PASS"
    audit = candidate.get("latest_audit") or {}
    if audit and (
        audit.get("verdict") != "PASS"
        or audit.get("unauthorized_product_change")
        or audit.get("external_commercial_text_remaining")
    ):
        return "MEDIA_QA_SUPERSEDED_OR_REJECTED"
    report = load(audit.get("audit_json"))
    if (
        report.get("package_print_changed")
        or report.get("same_product_identity") is False
    ):
        return "MEDIA_IDENTITY_QA_FAILED"
    if candidate.get("cluster_id") != facts.get("global_spu_id"):
        return "MEDIA_CLUSTER_CHANGED"
    if not sku.get("source_options") or candidate.get("physical_signature") != digest(
        sku["source_options"]
    ):
        return "MEDIA_PHYSICAL_AXES_UNPROVEN_OR_CHANGED"
    if candidate.get("source_sku_id"):
        if candidate["source_sku_id"] != sku["source_sku_id"]:
            return "MEDIA_SKU_MISMATCH"
        if not same_url(candidate.get("source_image_url"), sku.get("source_image_url")):
            return "MEDIA_SOURCE_BINDING_CHANGED"
    else:
        # SPU image sharing is conservative: only one current physical SKU.
        if len(facts.get("source_skus") or []) != 1:
            return "SPU_IMAGE_MULTI_VARIANT_AMBIGUOUS"
        if not same_url(candidate.get("source_image_url"), sku.get("source_image_url")):
            return "MEDIA_SOURCE_BINDING_UNPROVEN"
    return None


def reuse(candidate, root: Path):
    expected = candidate.get("sha256") or ""
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        return None, "MEDIA_FILE_HASH_MISMATCH"
    owned = list((root / "media").glob(expected + ".*"))
    source = owned[0] if owned else Path(candidate.get("path") or "")
    if not source.is_file():
        return None, "MEDIA_FILE_MISSING"
    sha = file_sha(source)
    if sha != expected:
        return None, "MEDIA_FILE_HASH_MISMATCH"
    try:
        with Image.open(source) as im:
            im.verify()
        with Image.open(source) as im:
            w, h = im.size
            fmt = im.format.lower()
        if min(w, h) < 120:
            return None, "MEDIA_RESOLUTION_TOO_LOW"
    except (OSError, ValueError):
        return None, "MEDIA_DECODE_FAILED"
    ext = {"jpeg": "jpg"}.get(fmt, fmt)
    if ext not in {"png", "jpg", "webp", "gif"}:
        return None, "MEDIA_FORMAT_UNSUPPORTED"
    relative = "media/" + sha + "." + ext
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and file_sha(dest) != sha:
        raise ValueError("LOCAL_MEDIA_HASH_CONFLICT")
    if not dest.exists():
        temp = dest.with_suffix(dest.suffix + "." + uuid.uuid4().hex + ".tmp")
        shutil.copyfile(source, temp)
        temp.replace(dest)
    return {
        "asset_id": candidate["asset_id"],
        "sha256": sha,
        "path": relative,
        "url": "/assets/" + relative,
        "width": w,
        "height": h,
        "source_sku_id": candidate.get("source_sku_id"),
        "source_evidence_id": candidate.get("source_evidence_id"),
        "qa_version": candidate["qa_version"],
        "qa_verdict": "PASS",
        "qa_scope": "INTERNAL_REVIEW",
        "rights_status": candidate.get("rights_status", "UNKNOWN"),
        "source_image_url": candidate.get("source_image_url"),
        "physical_signature": candidate.get("physical_signature"),
        "route": candidate.get("route", "REUSED_LLM_PRODUCTION_MEDIA"),
    }, None


def cached_files_valid(result, root):
    for asset in result.get("media") or []:
        p = (root / asset["path"]).resolve()
        if (
            not p.is_relative_to(root.resolve())
            or not p.is_file()
            or file_sha(p) != asset["sha256"]
        ):
            return False
    return True
