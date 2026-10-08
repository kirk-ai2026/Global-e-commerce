from __future__ import annotations

import copy
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from lulu.core import digest

from .copy import current_page, generate, render_for, review, static_qa
from .localize import text_for
from .media import prepare_media
from .policy import profile, protected_hash
from .provider import ProviderBlocked, ResponsesProvider, UnknownPaidResult


def _legacy(page, p):
    sections = copy.deepcopy(page["description_sections"])
    if p.locale == "en":
        return {
            "description_sections": sections,
            "description_text_html": page["description_html"],
        }
    for section in sections:
        section["heading"] = text_for(section.get("heading"), p.locale)
        section["html"] = text_for(section.get("html"), p.locale)
    sections, html = render_for(sections, p.locale)
    return {
        "description_sections": sections,
        "description_text_html": html or page["description_html"],
    }


def run_job(content, assets, job, provider=None):
    provider = provider or ResponsesProvider(content)
    repo = content.repo
    row = repo.get(job["cluster_id"])
    evidence = content.evidence(evidence_id=job["evidence_id"])
    p = profile(job["profile_id"])
    if (job.get("recipe") or {}).get("copy_contract") != p.copy_contract:
        raise ValueError("VERSIONED_CONTENT_JOB_REQUIRED")
    if (job.get("recipe") or {}).get("profile_sha") != p.sha:
        raise ValueError("VERSIONED_MEDIA_POLICY_JOB_REQUIRED")
    if isinstance(provider, ResponsesProvider) and any(
        provider.config[k] != job["recipe"][j]
        for k, j in [
            ("model", "model"),
            ("vision_model", "vision_model"),
            ("reasoning", "reasoning"),
            ("base_url", "provider"),
        ]
    ):
        raise ValueError("WORKER_PROVIDER_RECIPE_CHANGED")
    if protected_hash(row) != job["protected_sha"]:
        raise ValueError("PROTECTED_INPUT_CHANGED")
    checkpoints = job.get("checkpoints") or {}
    if not checkpoints.get("candidate"):
        previous = next(
            (
                r
                for r in content.revisions(job["cluster_id"], job["profile_id"])
                if r["evidence_id"] == job["evidence_id"]
                and r["body"].get("copy_contract_sha")
                in {p.copy_contract, p.compatible_render_v1, p.compatible_render_v2}
                and r["protected_sha"] == job["protected_sha"]
                and r["base_operator_sha"] == job["base_operator_sha"]
                and all(
                    i.get("kind") in {"POOR_SCANABILITY", "LOCALIZATION_ERROR"}
                    for i in r["qa"].get("source_review", {}).get("issues", [])
                )
            ),
            None,
        )
        if previous:
            sections, html = render_for(
                previous["body"]["description_sections"], p.locale
            )
            checkpoints = {
                "candidate": {
                    "description_sections": sections,
                    "description_text_html": html,
                    "copy_contract": p.copy_contract,
                },
                "method": previous["method"],
            }
            if (
                html == previous["body"]["description_text_html"]
                and previous["qa"]["status"] == "PASS"
            ):
                checkpoints["source_review"] = previous["qa"]["source_review"]
            content.checkpoint(
                job["job_id"],
                "compatibility_reuse",
                {
                    "revision_id": previous["revision_id"],
                    "reason": "Same evidence/SKU/operator baseline; locale/table rendering migration only",
                },
            )
        else:
            with content.repo.connect() as c:
                older = c.execute(
                    "SELECT checkpoints FROM content_job WHERE cluster_id=%s AND profile_id=%s AND evidence_id=%s AND protected_sha=%s AND base_operator_sha=%s AND job_id<>%s AND status IN ('PASS','NEEDS_EVIDENCE') ORDER BY created_at DESC",
                    (
                        job["cluster_id"],
                        job["profile_id"],
                        job["evidence_id"],
                        job["protected_sha"],
                        job["base_operator_sha"],
                        job["job_id"],
                    ),
                ).fetchall()
            old = next(
                (
                    x["checkpoints"]
                    for x in older
                    if x["checkpoints"].get("candidate", {}).get("copy_contract")
                    in {p.copy_contract, p.compatible_render_v1, p.compatible_render_v2}
                ),
                None,
            )
            if old:
                sections, html = render_for(
                    old["candidate"]["description_sections"], p.locale
                )
                checkpoints = {
                    "candidate": {
                        "description_sections": sections,
                        "description_text_html": html,
                        "copy_contract": p.copy_contract,
                    },
                    "method": old.get("method", "SOURCE_GROUNDED_REBUILD"),
                }
                content.checkpoint(
                    job["job_id"],
                    "compatibility_reuse",
                    {
                        "reason": "Resume compatible persisted candidate; upstream paid calls preserved"
                    },
                )
    timings = {}
    start = time.monotonic()
    page = current_page(row)
    selected = {str(v["platform_sku_id"]) for v in page["variants"]}
    # Media preparation runs independently from the slow source-review/model branch.
    with ThreadPoolExecutor(max_workers=1) as media_pool:
        media_future = media_pool.submit(
            prepare_media, evidence, p, selected, assets, repo
        )
        candidate = checkpoints.get("candidate")
        source_review = checkpoints.get("source_review")
        method = checkpoints.get("method")
        if candidate is None:
            legacy = _legacy(page, p)
            t = time.monotonic()
            legacy_static = static_qa(legacy, evidence["bundle"], selected)
            if (
                job["intent"] == "AUDIT_AND_FILL"
                and p.locale != "en"
                and legacy_static["status"] == "PASS"
            ):
                old_review, _ = review(provider, job, evidence, p, page, legacy, assets)
                content.checkpoint(job["job_id"], "legacy_review", old_review)
                if old_review["status"] == "PASS" and not old_review["issues"]:
                    candidate = legacy
                    source_review = old_review
                    method = "SOURCE_REVIEWED_REUSE"
            timings["audit_seconds"] = round(time.monotonic() - t, 3)
            if candidate is None:
                t = time.monotonic()
                candidate, _ = generate(provider, job, evidence, p, page, assets)
                method = "SOURCE_GROUNDED_REBUILD"
                timings["generation_seconds"] = round(time.monotonic() - t, 3)
            content.checkpoint(job["job_id"], "candidate", candidate)
            content.checkpoint(job["job_id"], "method", method)
        qa = static_qa(candidate, evidence["bundle"], selected)
        t = time.monotonic()
        for attempt in range(3):
            if source_review is None:
                source_review, _ = review(
                    provider, job, evidence, p, page, candidate, assets
                )
                content.checkpoint(job["job_id"], "source_review", source_review)
            if (
                qa["status"] == "PASS"
                and source_review["status"] == "PASS"
                and not source_review["issues"]
            ):
                break
            if attempt == 2:
                break
            issues = [*source_review.get("issues", []), *qa.get("errors", [])]
            candidate, _ = generate(
                provider,
                job,
                evidence,
                p,
                page,
                assets,
                previous=candidate,
                issues=issues,
            )
            method = "SOURCE_GROUNDED_REBUILD"
            source_review = None
            qa = static_qa(candidate, evidence["bundle"], selected)
            content.checkpoint(job["job_id"], "candidate", candidate)
            content.checkpoint(job["job_id"], "method", method)
            content.checkpoint(job["job_id"], "source_review", None)
        timings["review_and_repair_seconds"] = round(time.monotonic() - t, 3)
        t = time.monotonic()
        media = media_future.result()
        timings["media_wait_seconds"] = round(time.monotonic() - t, 3)
        timings["media_prepare_seconds"] = media.get("preparation_seconds", 0)
    passed = (
        qa["status"] == "PASS"
        and source_review["status"] == "PASS"
        and not source_review["issues"]
    )
    body = {
        **candidate,
        "description_media": media["description_media"],
        "media_ledger": media["media_ledger"],
        "profile_id": p.profile_id,
        "market": p.market,
        "locale": p.locale,
        "copy_contract_sha": p.copy_contract,
        "media_policy_sha": p.sha,
        "evidence_sha": evidence["evidence_sha"],
        "protected_sha": job["protected_sha"],
    }
    summary = {
        "status": "PASS" if passed else "REPAIR",
        "static_qa": qa,
        "source_review": source_review,
        "coverage": evidence["coverage"],
        "image_edits": media["image_edits"],
        "image_edit_pending": media["edit_pending"],
        "deterministic_crops": media.get("deterministic_crops", 0),
        "timings": {**timings, "total_seconds": round(time.monotonic() - start, 3)},
        "shopify_writes": 0,
        "upstream_writes": 0,
    }
    revision = content.save_revision(job, body, summary, method)
    content.finish(
        job["job_id"],
        "PASS" if passed else "NEEDS_EVIDENCE",
        {"revision_id": revision["revision_id"], **summary},
    )
    return {
        "job_id": job["job_id"],
        "cluster_id": job["cluster_id"],
        "status": "PASS" if passed else "NEEDS_EVIDENCE",
        "method": method,
        "revision_id": revision["revision_id"],
        "description_media": len(media["description_media"]),
        "timings": summary["timings"],
    }


def run_worker(content, assets, *, job_ids=None, workers=2, max_jobs=None):
    with content.repo.connect() as gate:
        gate.commit()
        gate.autocommit = True
        locked = gate.execute(
            "SELECT pg_try_advisory_lock(hashtext('lulu-content-global-worker')) held"
        ).fetchone()["held"]
        if not locked:
            return {"status": "WORKER_ALREADY_RUNNING", "processed": 0}
        try:
            return _run_worker(
                content, assets, job_ids=job_ids, workers=workers, max_jobs=max_jobs
            )
        finally:
            gate.execute(
                "SELECT pg_advisory_unlock(hashtext('lulu-content-global-worker'))"
            )


def _run_worker(content, assets, *, job_ids=None, workers=2, max_jobs=None):
    if content.provider_blocked():
        return {
            "status": "BLOCKED_PROVIDER",
            "reason": "credit_balance_exhausted",
            "processed": 0,
        }
    owner = "lulu-content-" + uuid.uuid4().hex
    results = []

    def loop(index):
        count = 0
        while max_jobs is None or count < max_jobs:
            job = content.claim(owner + "-" + str(index), job_ids)
            if not job:
                return
            try:
                if job["intent"] == "MEDIA_EDIT":
                    from .media_edit import run_media_job

                    result = run_media_job(content, assets, job)
                else:
                    result = run_job(content, assets, job)
            except UnknownPaidResult as error:
                result = {
                    "job_id": job["job_id"],
                    "cluster_id": job["cluster_id"],
                    "status": "RESULT_UNKNOWN",
                    "reason": str(error),
                }
                content.finish(job["job_id"], "RESULT_UNKNOWN", result)
            except ProviderBlocked as error:
                result = {
                    "job_id": job["job_id"],
                    "cluster_id": job["cluster_id"],
                    "status": "BLOCKED_PROVIDER",
                    "reason": str(error),
                }
                content.finish(job["job_id"], "BLOCKED_PROVIDER", result)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                return
            except Exception as error:
                result = {
                    "job_id": job["job_id"],
                    "cluster_id": job["cluster_id"],
                    "status": "NEEDS_EVIDENCE",
                    "reason": type(error).__name__ + ":" + str(error)[:180],
                }
                content.finish(job["job_id"], "NEEDS_EVIDENCE", result)
            count += 1
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 2))) as pool:
        list(pool.map(loop, range(max(1, min(workers, 2)))))
    return {
        "processed": len(results),
        "results": results,
        "report": content.report(),
        "execution_sha": digest(results),
    }
