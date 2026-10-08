from __future__ import annotations

import os
import socket
from pathlib import Path

from .channel import preflight
from .content import (
    SERVICE_ONLY,
    compose,
    content_dependency,
    neutral,
    qa,
    rebase_evidence,
    sku_label,
)
from .core import CONTRACT, data_dir, digest
from .media import bind_historical_axes, cached_files_valid, compatible, reuse


def build(facts, root: Path, store=None, extra_media=None):
    pid = facts["product_id"]
    issues = []
    retained = []
    withheld = []
    media = []
    base = {
        "schema": CONTRACT,
        "product_id": pid,
        "global_spu_id": facts.get("global_spu_id"),
        "facts_sha": digest(facts),
        "market": "CA",
        "currency": "CAD",
        "locale": "zh-Hans",
        "source_evidence_id": facts.get("evidence_id"),
        "source_observed_at": facts.get("observed_at"),
        "price_status": "NOT_CONFIGURED",
        "source_prices_are_historical": True,
        "retail_price": None,
        "content": None,
        "qa": None,
        "source_sku_count": len(facts.get("source_skus") or []),
        "content_skus": retained,
        "withheld_skus": withheld,
        "media": media,
        "issues": issues,
        "paid_calls": 0,
        "shopify_writes": 0,
    }
    reasons = facts.get("scope_reasons") or []
    if facts["scope_status"] == "OUT_OF_SCOPE" or "EXPLICIT_UNAVAILABLE" in reasons:
        base["status"] = "EXCLUDED"
        issues.extend(reasons)
        base["preflight"] = preflight(base)
        return base
    hard = [
        r
        for r in reasons
        if r
        in {
            "IDENTITY_MEMBERSHIP_UNPROVEN",
            "DETAIL_EVIDENCE_MISSING",
            "SOURCE_SKU_TREE_MISSING",
        }
    ]
    if facts["scope_status"] == "SCOPE_REVIEW":
        hard += reasons
    history = store.evidence(pid) if store and facts.get("media_candidates") else []
    for original in facts.get("source_skus") or []:
        s = dict(original)
        sid = s["source_sku_id"]
        gaps = []
        if not sid or s.get("duplicate_id"):
            gaps.append("SOURCE_SKU_ID_INVALID")
        if SERVICE_ONLY.search(neutral(s.get("raw_label"))):
            gaps.append("SERVICE_OR_NON_PRODUCT_SKU")
        if s["availability_at_observation"] == "UNAVAILABLE":
            gaps.append("SKU_UNAVAILABLE_AT_OBSERVATION")
        if s["availability_at_observation"] == "UNKNOWN":
            gaps.append("SKU_AVAILABILITY_UNKNOWN")
        s["consumer_label"] = sku_label(s)
        if not s["consumer_label"]:
            gaps.append("SKU_LABEL_MISSING")
        if not s.get("source_price_cny"):
            gaps.append("SOURCE_SKU_PRICE_MISSING")
        chosen = None
        media_reasons = []
        for candidate in (extra_media or []) + list(
            facts.get("media_candidates") or []
        ):
            candidate = dict(candidate)
            if not candidate.get("physical_signature") and history:
                candidate["physical_signature"] = bind_historical_axes(
                    candidate, s, history
                )
            error = compatible(candidate, s, facts)
            if error:
                media_reasons.append(error)
                continue
            chosen, error = reuse(candidate, root)
            if error:
                media_reasons.append(error)
                continue
            if chosen:
                break
        if not chosen:
            gaps.append("MEDIA_EVIDENCE_REQUIRED")
            gaps.extend(sorted(set(media_reasons)))
        if gaps:
            withheld.append(
                {
                    "source_sku_id": sid,
                    "label": s["consumer_label"],
                    "reasons": sorted(set(gaps)),
                    "source_image_url": s.get("source_image_url"),
                }
            )
        else:
            s["media_sha256"] = chosen["sha256"]
            retained.append(s)
            chosen["alt"] = s["consumer_label"]
            if not any(a["sha256"] == chosen["sha256"] for a in media):
                media.append(chosen)
    if hard or not retained:
        base["status"] = "NEEDS_EVIDENCE"
        issues.extend(hard)
        if not retained:
            issues.append("NO_COMPLETE_CONTENT_SKU")
    else:
        try:
            fp = content_dependency(facts, retained)
            cached = store.cache(fp) if store else None
            base["content"] = (
                rebase_evidence(cached, facts) if cached else compose(facts, retained)
            )
            base["content_fingerprint"] = fp
            base["content_cache_hit"] = bool(cached)
            base["qa"] = qa(base["content"], retained)
            if store and not cached and base["qa"]["verdict"] == "PASS":
                store.put_artifact(fp, "CONTENT", base["content"])
            base["status"] = (
                "CONTENT_READY" if base["qa"]["verdict"] == "PASS" else "NEEDS_EVIDENCE"
            )
            issues.extend(base["qa"]["errors"])
        except ValueError as exc:
            base["status"] = "NEEDS_EVIDENCE"
            issues.append(str(exc))
    if not base["content"]:
        # Internal reference text and supplier URLs are explicitly unqualified.
        base["reference_preview"] = {
            "title": neutral(facts.get("title")),
            "brand": facts.get("brand"),
            "attributes": facts.get("attributes"),
            "source_gallery": facts.get("gallery"),
            "status": "UNQUALIFIED_SOURCE_REFERENCE",
        }
    base["issues"] = sorted(set(issues))
    base["remediation"] = [
        {
            "stage": "SOURCE" if reason in hard else "CONTENT",
            "reason": reason,
            "fingerprint": digest([pid, reason, base["facts_sha"]]),
        }
        for reason in base["issues"]
    ]
    for sku in withheld:
        base["remediation"].append(
            {
                "stage": "MEDIA"
                if "MEDIA_EVIDENCE_REQUIRED" in sku["reasons"]
                else "SKU",
                "source_sku_id": sku["source_sku_id"],
                "reasons": sku["reasons"],
                "fingerprint": digest(
                    [pid, sku["source_sku_id"], sku["reasons"], base["facts_sha"]]
                ),
            }
        )
    base["preflight"] = preflight(base)
    base["output_sha"] = digest(base)
    return base


def run(store, jid, *, batch_size=20, max_tasks=None, root=None):
    with store.session():
        return _run(store, jid, batch_size=batch_size, max_tasks=max_tasks, root=root)


def _run(store, jid, *, batch_size=20, max_tasks=None, root=None):
    root = root or data_dir()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    counts = {"processed": 0, "cached": 0, "failed": 0}
    while max_tasks is None or counts["processed"] + counts["failed"] < max_tasks:
        n = (
            min(batch_size, max_tasks - counts["processed"] - counts["failed"])
            if max_tasks
            else batch_size
        )
        tasks = store.claim(jid, owner, n)
        if not tasks:
            break
        for task in tasks:
            try:
                row = store.product(task["freeze_id"], task["product_id"])
                cached = store.cache(task["fingerprint"])
                if cached and cached_files_valid(cached, root):
                    result = cached
                    counts["cached"] += 1
                else:
                    from .remediation import as_candidate

                    receipts = store.media_evidence(
                        task["freeze_id"], task["product_id"]
                    )
                    extra = [
                        as_candidate(r)
                        for r in receipts
                        if r["base_facts_sha"] == row["facts_sha"]
                        and r["_receipt_id"] in task.get("media_evidence_ids", [])
                    ]
                    result = build(row["facts"], root, store, extra)
                store.finish(task, result, owner)
                counts["processed"] += 1
            except Exception as exc:  # noqa: BLE001 - task boundary must record unexpected failures
                store.fail(task, owner, type(exc).__name__ + ":" + str(exc)[:200])
                counts["failed"] += 1
        print(__import__("json").dumps(counts), flush=True)
    return counts
