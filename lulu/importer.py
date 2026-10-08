"""Read-only Wujie evidence adapter. No commercial-selection/run filter.

The main evidence ledger and procurement state are resolved separately from
Taiwan readiness. A generated canonical URL is never channel evidence.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from .core import (
    ASIAN_REGIONS,
    brand_key,
    digest,
    load,
    money,
    timestamp,
    url,
)

IMPORT_VERSION = "wujie-readonly-lulu-v1"


@contextmanager
def readonly(path: Path):
    path = path.resolve()
    if not path.is_file():
        raise ValueError("SOURCE_DATABASE_MISSING")
    # immutable is safe only for a quiescent database without an unapplied WAL.
    has_wal = Path(str(path) + "-wal").exists()
    mode = "?mode=ro" if has_wal else "?mode=ro&immutable=1"
    c = sqlite3.connect(path.as_uri() + mode, uri=True, timeout=60)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA query_only=ON")
    c.execute("BEGIN")
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def table(c, name):
    return bool(
        c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


def brand_index(records):
    index = defaultdict(list)
    for record in records:
        if (
            not record.get("brand_country_code")
            or float(record.get("confidence") or 0) < 0.9
        ):
            continue
        names = [record.get("canonical_brand"), record.get("display_brand")] + load(
            record.get("aliases_json"), []
        )
        for name in names:
            key = brand_key(name)
            if len(key) < 2 or key in {"品牌", "official", "global", "国际", "进口"}:
                continue
            index[key].append(record)
    return index


def resolve_brand(name, index, profile=None):
    keys = [brand_key(name)] + [
        brand_key(x)
        for x in re.split(r"[/／|｜()（）]", name or "")
        if len(brand_key(x)) >= 2
    ]
    matches = []
    for key in keys:
        matches.extend(index.get(key, []))
    countries = {r["brand_country_code"] for r in matches}
    if len(countries) > 1:
        return {
            "status": "CONFLICT",
            "evidence_ids": sorted({r["brand_id"] for r in matches}),
        }
    if matches:
        best = max(
            matches,
            key=lambda r: (
                float(r.get("confidence") or 0),
                str(r.get("updated_at") or ""),
            ),
        )
        return {
            "status": "CONFIRMED",
            "code": best["brand_country_code"],
            "confidence": best["confidence"],
            "evidence_ids": sorted({r["brand_id"] for r in matches}),
            "evidence": load(best.get("evidence_json")),
        }
    p = profile or {}
    if (
        p.get("brand_country_code")
        and float(p.get("profile_confidence") or 0) >= 0.9
        and brand_key(name)
        in {brand_key(p.get("canonical_brand")), brand_key(p.get("display_brand"))}
    ):
        return {
            "status": "CONFIRMED",
            "code": p["brand_country_code"],
            "confidence": p["profile_confidence"],
            "evidence_ids": [p["profile_id"]],
            "evidence": load(p.get("field_evidence_json")),
        }
    return {"status": "UNKNOWN", "evidence_ids": []}


def channel(candidate, raw):
    source = load(candidate.get("source_evidence_json"))
    channels = set(source.get("source_channels") or [])
    confirmed = (
        candidate.get("channel_status") == "CONFIRMED"
        and float(candidate.get("channel_confidence") or 0) >= 0.95
    )
    if confirmed and channels == {"TMALL_GLOBAL"}:
        return {
            "status": "CONFIRMED",
            "value": "TMALL_GLOBAL",
            "evidence": source,
            "evidence_id": candidate.get("core_candidate_id"),
        }
    resolved = url(raw.get("resolvedUrl"))
    if resolved and (urlsplit(resolved).hostname or "").endswith(".tmall.hk"):
        return {
            "status": "CONFIRMED",
            "value": "TMALL_GLOBAL",
            "evidence": {"resolved_url": resolved},
        }
    if channels and "TMALL_GLOBAL" not in channels and confirmed:
        return {"status": "OUT_OF_SCOPE", "value": sorted(channels), "evidence": source}
    return {"status": "UNKNOWN", "evidence": source}


def category(candidate, profile, raw):
    major = str(candidate.get("major_category") or "")
    text = " ".join(
        str(x or "")
        for x in [
            candidate.get("leaf_category"),
            profile.get("product_form"),
            raw.get("title"),
        ]
    )
    if major in {"MOTHER_BABY", "BABY"}:
        return "mother_baby"
    if major == "BEAUTY":
        return "beauty"
    if major == "FOOD":
        return "food"
    if major == "FOOD_HEALTH":
        if re.search(
            r"保健|营养补充|营养素|维生素|維生素|益生菌|鱼油|魚油|辅酶|輔酶|胶原蛋白|膠原蛋白|褪黑素|叶黄素|葉黃素|氨糖|膳食补充|nutrition_supplement|supplement|probiotic",
            text,
            re.IGNORECASE,
        ):
            return "supplements"
        if re.search(
            r"食品|零食|饮料|飲料|咖啡|茶|饼|餅|糖|坚果|堅果|奶粉|麦片|麥片|燕麦|海苔|巧克力|薯|food|drink|snack|cereal",
            text,
            re.IGNORECASE,
        ):
            return "food"
    return "unknown"


def latest_observations(source, state):
    history = defaultdict(list)
    unavailable = {}
    evidence = []

    def add(pid, raw_json, observed, eid, origin, result=None):
        raw = load(raw_json)
        if not raw:
            return
        pid = str(pid)
        when = timestamp(raw.get("scrapedAt") or raw.get("recordTime") or observed)
        if not when:
            return
        if str(raw.get("itemId") or raw.get("item_id") or pid) != pid:
            return
        record = {
            "evidence_id": origin + ":" + str(eid),
            "product_id": pid,
            "observed_at": when,
            "origin": origin,
            "raw": raw,
            "result": load(result),
        }
        evidence.append(record)
        if (
            raw.get("productUnavailable") is True
            or str(raw.get("itemStatus") or "").upper() == "DELISTED"
        ):
            unavailable[pid] = max(unavailable.get(pid, ""), when)
            return
        if raw.get("_detailFetched") is True and isinstance(raw.get("skus"), list):
            history[pid].append(record)

    for r in source.execute("SELECT * FROM apify_detail_observations_v1"):
        add(
            r["item_id"],
            r["raw_json"],
            r["scraped_at"] or r["created_at"],
            r["observation_id"],
            "APIFY_RAW",
        )
    if state:
        for t, pk in [
            ("procurement_refresh_tasks_v2", "task_id"),
            ("candidate_procurement_rechecks_v1", "recheck_id"),
        ]:
            if not table(state, t):
                continue
            for row in state.execute("SELECT * FROM " + t):
                r = dict(row)
                pid = str(r["platform_product_id"])
                when = timestamp(
                    r.get("completed_at") or r.get("updated_at") or r.get("created_at")
                )
                if (
                    r.get("change_class") == "UNAVAILABLE"
                    or r.get("status") == "CURRENT_UNAVAILABLE"
                ):
                    unavailable[pid] = max(unavailable.get(pid, ""), when)
                    evidence.append(
                        {
                            "evidence_id": t + ":" + r[pk] + ":unavailable",
                            "product_id": pid,
                            "observed_at": when,
                            "origin": t,
                            "raw": {},
                            "explicit_unavailable": True,
                        }
                    )
                elif r.get("change_class") not in {
                    "TECHNICAL_FAILED",
                    "SKU_AMBIGUOUS",
                } and r.get("status") in {
                    "MERGED",
                    "COMPLETE",
                    "COMPLETED",
                    "CURRENT_VALID",
                }:
                    add(pid, r.get("raw_json"), when, r[pk], t, r.get("result_json"))
    for values in history.values():
        values.sort(key=lambda e: (e["observed_at"], e["evidence_id"]))
    return history, unavailable, evidence


def source_skus(raw, observed):
    result = []
    ids = set()
    for r in raw.get("skus") or []:
        if not isinstance(r, dict):
            continue
        sid = str(r.get("skuId") or r.get("sku_id") or "")
        stock = r.get("quantity", r.get("stock"))
        try:
            availability = (
                "AVAILABLE"
                if stock is not None and float(stock) > 0
                else "UNAVAILABLE"
                if stock is not None
                else "UNKNOWN"
            )
        except (ValueError, TypeError):
            availability = "UNKNOWN"
        axes = []
        for part in str(r.get("propsNames") or "").split(";"):
            match = re.fullmatch(r"(-?\d+:-?\d+):([^:]+):(.*)", part)
            if match:
                axes.append({"token": match[1], "name": match[2], "value": match[3]})
        image = url(r.get("imageUrl") or r.get("image"))
        method = "EXPLICIT_SKU_IMAGE" if image else None
        if not image:
            # Vendored exact-token resolver; never image-order or substring matching.
            from .vendor.source_sku_contract import resolve_property_image

            image, method = resolve_property_image(
                sid,
                r.get("propsIds") or r.get("propsNames") or "",
                raw.get("propsImages") or {},
            )
            image = url(image)
        result.append(
            {
                "source_sku_id": sid,
                "duplicate_id": sid in ids,
                "source_options": axes,
                "raw_label": r.get("propsNames") or r.get("title") or "",
                "property_path": r.get("propsIds") or "",
                "source_price_cny": money(r.get("couponPrice") or r.get("price")),
                "availability_at_observation": availability,
                "stock_at_observation": stock,
                "observed_at": observed,
                "source_image_url": image,
                "image_binding_method": method,
                "raw": r,
            }
        )
        ids.add(sid)
    return result


def media_index(source):
    result = defaultdict(list)
    if not table(source, "media_asset_registry_v1"):
        return result
    audit_by_local = {}
    if table(source, "media_quality_audits_v1"):
        for row in source.execute(
            "SELECT * FROM media_quality_audits_v1 ORDER BY created_at"
        ):
            if row["localized_media_id"]:
                audit_by_local[row["localized_media_id"]] = dict(row)
    localizations = {
        r["localized_media_id"]: dict(r)
        for r in source.execute("SELECT * FROM localized_media_versions_v1")
    }
    assets = {
        r["evidence_asset_id"]: dict(r)
        for r in source.execute("SELECT * FROM media_asset_evidence_v1")
    }
    for row in source.execute("SELECT * FROM media_asset_registry_v1"):
        r = dict(row)
        loc = localizations.get(r["localized_media_id"], {})
        asset = assets.get(r["source_evidence_id"], {})
        audit = audit_by_local.get(r["localized_media_id"], {})
        transform = load(loc.get("transformation_json"))
        ops = str(transform).lower()
        blocked = any(
            word in ops
            for word in [
                "traditional_crop",
                "deterministic_text_render",
                "local_repair",
                "opencv",
                "inpaint",
            ]
        )
        safe_route = (
            r["qa_version"]
            in {
                "media-localization-quality-v9-llm-production-only",
                "media-localization-quality-v10-sku-coverage",
            }
            and not blocked
        )
        if not safe_route:
            continue
        result[str(r["product_id"])].append(
            {
                "asset_id": r["localized_media_id"],
                "source_evidence_id": r["source_evidence_id"],
                "cluster_id": r["cluster_id"],
                "source_sku_id": str(r["sku_id"] or loc.get("variant_key") or ""),
                "source_image_url": asset.get("source_image_url"),
                "path": r["derivative_path"],
                "sha256": r["derivative_file_sha"],
                "source_sha256": r["original_file_sha"],
                "qa_version": r["qa_version"],
                "qa_verdict": r["qa_verdict"],
                "latest_audit": audit,
                "transform": transform,
                "rights_status": asset.get("rights_status", "UNKNOWN"),
                "usage_scope": "INTERNAL_REVIEW",
                "identity_map_id": asset.get("identity_map_id"),
                "snapshot_id": asset.get("snapshot_id"),
            }
        )
    return result


def freeze(source_path: Path, state_path: Path | None, store):
    """Resolve all current candidates in one read transaction per source DB."""
    source_path = source_path.resolve()
    from contextlib import ExitStack

    with ExitStack() as stack:
        source = stack.enter_context(readonly(source_path))
        state = stack.enter_context(readonly(state_path)) if state_path else None
        index = brand_index(
            [dict(r) for r in source.execute("SELECT * FROM brand_registry_v4")]
        )
        profiles = {
            r["core_candidate_id"]: dict(r)
            for r in source.execute(
                "SELECT * FROM structured_product_profiles_v4 WHERE is_current=1 ORDER BY created_at"
            )
        }
        parents = {
            r["cluster_id"]: r["superseded_by"]
            for r in source.execute(
                "SELECT cluster_id,superseded_by FROM product_clusters_v4"
            )
        }
        identities = {
            r["cluster_id"]: dict(r)
            for r in source.execute(
                "SELECT * FROM cluster_identity_versions_v4 WHERE is_current=1"
            )
        }
        memberships = {
            r["node_id"]: dict(r)
            for r in source.execute("SELECT * FROM current_cluster_memberships_v4")
        }
        history, unavailable, evidence = latest_observations(source, state)
        media = media_index(source)
        candidates = {}
        for r in source.execute(
            "SELECT * FROM tmall_global_core_candidates_v4 WHERE is_current=1 ORDER BY last_seen_at,core_candidate_id"
        ):
            candidates[str(r["platform_product_id"])] = dict(r)
        # Preserve collected detail products even if they lack current candidate membership.
        for pid in history:
            if pid not in candidates:
                candidates[pid] = {"platform_product_id": pid}
        products = []
        for pid, c in sorted(candidates.items()):
            observations = history.get(pid, [])
            last = observations[-1] if observations else {}
            raw = last.get("raw", {})
            profile = profiles.get(c.get("core_candidate_id"), {})
            brand = str(
                raw.get("brandName")
                or profile.get("display_brand")
                or profile.get("canonical_brand")
                or c.get("normalized_brand")
                or c.get("brand")
                or ""
            ).strip()
            brand_evidence = resolve_brand(brand, index, profile)
            chan = channel(c, raw)
            cat = category(c, profile, raw)
            cluster = str(c.get("cluster_id") or "")
            seen = set()
            cycle = False
            while cluster and parents.get(cluster):
                if cluster in seen:
                    cycle = True
                    break
                seen.add(cluster)
                cluster = str(parents[cluster])
            member = memberships.get("tmall:" + pid, {})
            identity = identities.get(cluster, {})
            problems = []
            scope = "INCLUDED"
            if (
                brand_evidence.get("code")
                and brand_evidence["code"] not in ASIAN_REGIONS
            ):
                scope = "OUT_OF_SCOPE"
                problems.append("BRAND_REGION_OUTSIDE_SCOPE")
            elif brand_evidence["status"] != "CONFIRMED":
                scope = "SCOPE_REVIEW"
                problems.append("BRAND_REGION_" + brand_evidence["status"])
            if chan["status"] == "OUT_OF_SCOPE":
                scope = "OUT_OF_SCOPE"
                problems.append("SOURCE_CHANNEL_OUTSIDE_SCOPE")
            elif chan["status"] != "CONFIRMED":
                if scope != "OUT_OF_SCOPE":
                    scope = "SCOPE_REVIEW"
                problems.append("TMALL_GLOBAL_CHANNEL_UNVERIFIED")
            if cat == "unknown":
                if scope != "OUT_OF_SCOPE":
                    scope = "SCOPE_REVIEW"
                problems.append("CATEGORY_UNRESOLVED")
            if (
                not cluster
                or cycle
                or member.get("membership_status") != "CONFIRMED"
                or not identity
            ):
                problems.append("IDENTITY_MEMBERSHIP_UNPROVEN")
            if not last:
                problems.append("DETAIL_EVIDENCE_MISSING")
            explicit = unavailable.get(pid)
            if explicit and (not last or explicit >= last["observed_at"]):
                problems.append("EXPLICIT_UNAVAILABLE")
            skus = source_skus(raw, last.get("observed_at"))
            if not skus:
                problems.append("SOURCE_SKU_TREE_MISSING")
            facts = {
                "product_id": pid,
                "global_spu_id": cluster or None,
                "identity_version": identity.get("cluster_identity_version_id"),
                "identity": load(identity.get("identity_json")),
                "membership_evidence": member,
                "scope_status": scope,
                "scope_reasons": sorted(set(problems)),
                "brand": brand,
                "brand_region": brand_evidence,
                "channel": chan,
                "category": cat,
                "source_category": c.get("major_category"),
                "source_leaf_category": c.get("leaf_category"),
                "title": raw.get("title") or c.get("title") or "",
                "source_url": url(raw.get("resolvedUrl") or raw.get("url"))
                or url(c.get("canonical_url")),
                "observed_at": last.get("observed_at"),
                "evidence_id": last.get("evidence_id"),
                "attributes": raw.get("attributes") or [],
                "source_skus": skus,
                "gallery": raw.get("pictures") or [],
                "media_candidates": media.get(pid, []),
                "source_profile": {
                    k: profile.get(k)
                    for k in [
                        "profile_id",
                        "product_subject",
                        "product_line",
                        "product_form",
                        "quantity_json",
                        "profile_confidence",
                    ]
                },
                "explicit_unavailable_at": explicit,
                "candidate_evidence_id": c.get("core_candidate_id"),
                "source_evidence": load(c.get("source_evidence_json")),
            }
            products.append(facts)
        # Freeze hashes exclude wall-clock execution timestamps and paid-run metadata.
        manifest = {
            "version": IMPORT_VERSION,
            "sources": [str(source_path)]
            + ([str(state_path.resolve())] if state_path else []),
            "products": [
                {"product_id": p["product_id"], "sha256": digest(p)} for p in products
            ],
            "evidence": [
                {"evidence_id": e["evidence_id"], "sha256": digest(e)}
                for e in sorted(evidence, key=lambda e: e["evidence_id"])
            ],
        }
        selected = [p for p in products if p["scope_status"] == "INCLUDED"]
        audit = {
            "candidate_count": len(products),
            "scope_counts": dict(Counter(p["scope_status"] for p in products)),
            "included_categories": dict(Counter(p["category"] for p in selected)),
            "included_brand_regions": dict(
                Counter(p["brand_region"].get("code") for p in selected)
            ),
            "included_spus": len(
                {p["global_spu_id"] for p in selected if p["global_spu_id"]}
            ),
            "included_source_skus": sum(len(p["source_skus"]) for p in selected),
            "included_with_detail": sum(bool(p["evidence_id"]) for p in selected),
            "included_with_media_candidates": sum(
                bool(p["media_candidates"]) for p in selected
            ),
            "review_reasons": dict(
                Counter(
                    r
                    for p in products
                    if p["scope_status"] == "SCOPE_REVIEW"
                    for r in p["scope_reasons"]
                )
            ),
            "source_read_only": True,
            "selection_filter_used": False,
            "paid_calls": 0,
            "shopify_writes": 0,
            "detail_observations": len(evidence),
            "source_paths": manifest["sources"],
        }
    return store.import_freeze(manifest, audit, products, evidence), audit
