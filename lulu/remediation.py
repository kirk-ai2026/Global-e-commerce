"""Intake for repaired/original media with separately recorded visual QA.

This does not edit images or infer QA from a file existing. A trusted operator
supplies repair and independent inspection receipts; all bindings and file
hashes are checked against the frozen supplier SKU before any reuse.
"""

from pathlib import Path

from .core import digest, file_sha, url


def import_media(store, fid, pid, receipt, root):
    row = store.product(fid, pid)
    if not row:
        raise ValueError("PRODUCT_NOT_FOUND")
    f = row["facts"]
    if f["scope_status"] != "INCLUDED":
        raise ValueError("PRODUCT_SCOPE_UNVERIFIED")
    if receipt.get("base_facts_sha") != row["facts_sha"]:
        raise ValueError("MEDIA_SOURCE_CHANGED")
    sid = receipt.get("source_sku_id")
    matches = [s for s in f["source_skus"] if s["source_sku_id"] == sid]
    if len(matches) != 1:
        raise ValueError("EXACT_SKU_BINDING_REQUIRED")
    sku = matches[0]
    if receipt.get("physical_signature") != digest(sku["source_options"]):
        raise ValueError("SKU_PHYSICAL_AXES_CHANGED")
    if url(receipt.get("source_image_url")) != sku.get("source_image_url"):
        raise ValueError("MEDIA_SOURCE_BINDING_CHANGED")
    route = receipt.get("route")
    if route not in {"ORIGINAL_DIRECT_PASS", "LLM_EDIT"}:
        raise ValueError("UNSUPPORTED_MEDIA_REPAIR_ROUTE")
    if not receipt.get("repair_receipt_id"):
        raise ValueError("REPAIR_RECEIPT_REQUIRED")
    qa = receipt.get("qa") or {}
    if not qa.get("receipt_id") or qa["receipt_id"] == receipt["repair_receipt_id"]:
        raise ValueError("INDEPENDENT_QA_RECEIPT_REQUIRED")
    if (
        qa.get("verdict") != "PASS"
        or not qa.get("same_product_identity")
        or not qa.get("package_print_preserved")
        or qa.get("external_commercial_text_remaining")
    ):
        raise ValueError("MEDIA_VISUAL_QA_NOT_PASS")
    path = Path(receipt.get("path") or "").resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("MEDIA_FILE_OUTSIDE_LULU_ROOT")
    sha = file_sha(path)
    if receipt.get("sha256") != sha or qa.get("output_sha256") != sha:
        raise ValueError("MEDIA_QA_FILE_HASH_MISMATCH")
    if route == "ORIGINAL_DIRECT_PASS" and receipt.get("source_sha256") != sha:
        raise ValueError("ORIGINAL_BYTES_CHANGED")
    if not receipt.get("source_sha256") or not qa.get("version"):
        raise ValueError("MEDIA_EVIDENCE_INCOMPLETE")
    normalized = {
        **receipt,
        "product_id": pid,
        "global_spu_id": f["global_spu_id"],
        "freeze_id": fid,
        "usage_scope": "INTERNAL_REVIEW",
    }
    rid = store.register_media(fid, pid, normalized)
    return {
        "receipt_id": rid,
        "job_id": store.create_job(fid, [pid]),
        "shopify_writes": 0,
    }


def as_candidate(receipt):
    qa = receipt["qa"]
    return {
        "asset_id": "LULU-" + digest(receipt)[:24],
        "cluster_id": receipt["global_spu_id"],
        "source_sku_id": receipt["source_sku_id"],
        "physical_signature": receipt["physical_signature"],
        "source_image_url": receipt["source_image_url"],
        "path": receipt["path"],
        "sha256": receipt["sha256"],
        "source_sha256": receipt["source_sha256"],
        "source_evidence_id": receipt["repair_receipt_id"],
        "qa_version": qa["version"],
        "qa_verdict": "PASS",
        "latest_audit": {
            "verdict": "PASS",
            "audit_json": {
                "same_product_identity": True,
                "package_print_changed": False,
            },
        },
        "rights_status": receipt.get("rights_status", "UNKNOWN"),
        "route": receipt["route"],
    }
