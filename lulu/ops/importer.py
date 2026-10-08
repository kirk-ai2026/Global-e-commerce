"""Only GETs approved upstream API objects; never sends an upstream mutation."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

from PIL import Image

from lulu.core import ASIAN_REGIONS, canonical, digest, now
from lulu.importer import brand_index, category, readonly, resolve_brand

from .localization import localize
from .money import fetch_fx

SOURCE = "https://wujie-merch-ops-shadow-wiv3tqv5ua-de.a.run.app"


def get(path, base=SOURCE, binary=False):
    if path.startswith("http"):
        if urlsplit(path).hostname != urlsplit(base).hostname:
            raise ValueError("UPSTREAM_HOST_NOT_ALLOWED")
        target = path
    else:
        target = base + "/" + path.lstrip("/")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                target, headers={"User-Agent": "LuluOpsReadonly/1.0"}
            )
            with urllib.request.urlopen(req, timeout=60) as response:
                data = response.read()
                if len(data) > 24 * 1024 * 1024:
                    raise ValueError("UPSTREAM_RESPONSE_TOO_LARGE")
                return (
                    (
                        data,
                        response.headers.get(
                            "Content-Type", "application/octet-stream"
                        ),
                    )
                    if binary
                    else json.loads(data)
                )
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(0.3 * (attempt + 1))


def source_listing():
    rows = []
    total = None
    for page in range(1, 101):
        data = get("/api/v3/ops/listings?page_size=100&page=" + str(page))
        if total is None:
            total = data["total"]
        if data["total"] != total:
            raise ValueError("UPSTREAM_POPULATION_CHANGED_RETRY")
        rows.extend(data.get("items") or [])
        if len(rows) >= total:
            break
    if len(rows) != total or len({r["cluster_id"] for r in rows}) != total:
        raise ValueError("UPSTREAM_PAGINATION_INCONSISTENT")
    return rows


def brand_records(source_db, items):
    with readonly(source_db) as c:
        index = brand_index(
            [dict(r) for r in c.execute("SELECT * FROM brand_registry_v4")]
        )
        ids = [
            str(r.get("tmall_product_id") or "")
            for r in items
            if r.get("tmall_product_id")
        ]
        candidates = {}
        if ids:
            marks = ",".join("?" for _ in ids)
            candidates = {
                str(r["platform_product_id"]): dict(r)
                for r in c.execute(
                    "SELECT * FROM tmall_global_core_candidates_v4 WHERE is_current=1 AND platform_product_id IN ("
                    + marks
                    + ") ORDER BY last_seen_at",
                    ids,
                )
            }
        profiles = {
            r["core_candidate_id"]: dict(r)
            for r in c.execute(
                "SELECT * FROM structured_product_profiles_v4 WHERE is_current=1"
            )
        }
    selected = []
    review = []
    excluded = []
    for item in items:
        candidate = candidates.get(str(item.get("tmall_product_id") or ""), {})
        profile = profiles.get(candidate.get("core_candidate_id"), {})
        brand = (
            item.get("brand")
            or profile.get("display_brand")
            or profile.get("canonical_brand")
            or candidate.get("brand")
            or candidate.get("normalized_brand")
            or ""
        )
        fact = resolve_brand(brand, index, profile)
        fact["brand_name"] = brand
        record = {
            "cluster_id": item["cluster_id"],
            "item": item,
            "brand_fact": fact,
            "brand": brand,
            "category": category(candidate, profile, {"title": item.get("title")}),
            "scope": "INCLUDED",
        }
        if fact["status"] != "CONFIRMED":
            record["scope"] = "REVIEW"
            review.append(record)
        elif fact["code"] not in ASIAN_REGIONS:
            excluded.append(record)
        else:
            selected.append(record)
    return selected, review, excluded


def media_objects(value):
    if isinstance(value, dict):
        if value.get("asset_id") and (value.get("url") or value.get("output_url")):
            yield value
        for child in value.values():
            yield from media_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from media_objects(child)


def import_one(record, repo, assets):
    cluster = record["cluster_id"]
    # Both endpoints are read-only. The full workspace retains authoritative SKU matrices and QA.
    detail = get("/api/v3/ops/listings/" + cluster + "/workspace")
    full = get("/api/v3/ops/listings/" + cluster + "/workspace/media")
    root = {"detail": detail, "media_workspace": full, "list_item": record["item"]}
    source_sha = digest(root)
    existing = repo.get(cluster)
    if (
        existing
        and existing["source"]["source_sha"] == source_sha
        and not existing["source"].get("media_failures")
    ):
        return {
            **record,
            "source": existing["source"],
            "localized": existing["localized"],
        }
    derivative = copy.deepcopy(root)
    downloaded = {}
    failures = []
    # Download only the exact approved versions selected for current consumer/SKU output.
    for media in list(media_objects(derivative["detail"].get("consumer", {}))):
        path = media.get("url") or media.get("output_url")
        if path in downloaded:
            result = downloaded[path]
        else:
            try:
                data, content_type = get(path, binary=True)
                with Image.open(io.BytesIO(data)) as image:
                    image.verify()
                actual = hashlib.sha256(data).hexdigest()
                object_key = (
                    media.get("presentation_object") or media.get("output_object") or ""
                )
                expected_match = re.search(r"/([a-f0-9]{64})\.[a-z]+$", object_key)
                if expected_match and expected_match[1] != actual:
                    raise ValueError("UPSTREAM_SELECTED_BYTES_HASH_MISMATCH")
                sha, key = assets.put(data, content_type)
                provenance = {
                    "upstream_url": SOURCE + path,
                    "upstream_object": object_key,
                    "source_sha256": media.get("source_sha256"),
                    "selected_version": media.get("selected_version"),
                    "source_snapshot_sha": source_sha,
                    "source_qa": "INHERITED_UPSTREAM_EFFECTIVE_MANIFEST",
                    "cluster_id": cluster,
                }
                repo.save_media(sha, key, content_type, len(data), provenance)
                result = {"sha": sha, "url": "/api/media/" + sha, "object": key}
                downloaded[path] = result
            except Exception as exc:
                failures.append(
                    {
                        "asset_id": media.get("asset_id"),
                        "source_url": path,
                        "error": type(exc).__name__ + ":" + str(exc)[:160],
                    }
                )
                result = None
        if not result:
            media["upstream_url"] = path
            media["url"] = ""
            media["output_url"] = ""
        if result:
            media["upstream_url"] = path
            media["url"] = result["url"]
            media["output_url"] = result["url"]
            media["lulu_sha256"] = result["sha"]
            media["lulu_object"] = result["object"]
    for variant in derivative["detail"].get("consumer", {}).get("variants") or []:
        pointer = variant.get("sku_image_url")
        if pointer in downloaded:
            variant["sku_image_url"] = downloaded[pointer]["url"]
        elif variant.get("sku_media"):
            variant["sku_image_url"] = variant["sku_media"][0].get("url")
    # Procurement and consumer share an exact selected-SKU map, never a guessed image order.
    variant_map = {
        str(v.get("platform_sku_id")): v
        for v in derivative["detail"].get("consumer", {}).get("variants") or []
    }
    for variant in derivative["detail"].get("procurement", {}).get("variants") or []:
        selected = variant_map.get(str(variant.get("platform_sku_id")))
        if selected:
            variant["sku_image_url"] = selected.get("sku_image_url")
            variant["sku_media"] = selected.get("sku_media", [])
    localization = localize(detail)
    return {
        **record,
        "source": {
            "upstream": root,
            "source_sha": source_sha,
            "derived": derivative,
            "media_failures": failures,
            "captured_at": now(),
            "upstream_writes": 0,
            "media_downloads": len(downloaded),
        },
        "localized": localization,
    }


def import_cloud(source_db, repo, assets, *, limit=None, workers=4):
    items = source_listing()
    selected, review, excluded = brand_records(source_db, items)
    if limit:
        # A bounded canary includes both single and multi-SKU examples.
        selected = sorted(
            selected,
            key=lambda r: (
                r["item"].get("source_variant_count", 0) <= 1,
                r["cluster_id"],
            ),
        )[:limit]
        review = []
    records = []
    errors = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(import_one, r, repo, assets): r for r in selected + review
        }
        for future in as_completed(futures):
            row = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "cluster_id": row["cluster_id"],
                        "error": type(exc).__name__ + ":" + str(exc)[:180],
                    }
                )
            print(
                canonical(
                    {
                        "completed": len(records),
                        "errors": len(errors),
                        "target": len(futures),
                    }
                ),
                flush=True,
            )
    audit = {
        "upstream_total": len(items),
        "included_target": len(selected),
        "brand_review_target": len(review),
        "excluded": len(excluded),
        "imported": len(records),
        "import_errors": errors,
        "regions": dict(Counter(r["brand_fact"].get("code") for r in selected)),
        "localized_pass": sum(
            r["localized"]["qa"]["verdict"] == "PASS" for r in records
        ),
        "media_sync_failures": sum(len(r["source"]["media_failures"]) for r in records),
        "upstream_writes": 0,
        "shopify_writes": 0,
        "paid_calls": 0,
    }
    source = {
        "base_url": SOURCE,
        "population_sha": digest(items),
        "items": items,
        "excluded": [(r["cluster_id"], r["brand_fact"]) for r in excluded],
    }
    freeze = (
        "LCF-"
        + digest(
            [
                (r["cluster_id"], r["source"]["source_sha"])
                for r in sorted(records, key=lambda r: r["cluster_id"])
            ]
        )[:24]
    )
    repo.import_batch(freeze, audit, source, records)
    repo.save_fx(fetch_fx())
    return {**audit, "freeze_id": freeze}
