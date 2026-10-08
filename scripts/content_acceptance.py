"""Read-only snapshot of owned cloud content progress and protected inputs."""

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from urllib.parse import unquote, urlsplit

from google.cloud.sql.connector import Connector
from google.oauth2.credentials import Credentials

from lulu.ops.content.policy import DEFAULT_PROFILE, protected_hash
from scripts.provision_lulu_ops import gc


def main():
    credentials = Credentials(gc("auth", "print-access-token").decode().strip())
    dsn = urlsplit(
        gc("secrets", "versions", "access", "latest", "--secret=lulu-ops-database-url")
        .decode()
        .strip()
    )
    with Connector(credentials=credentials, timeout=60) as connector:
        c = connector.connect(
            "zenheart:asia-east1:zenheart-pg",
            "pg8000",
            user=unquote(dsn.username),
            password=unquote(dsn.password),
            db=dsn.path.lstrip("/"),
        )
        cursor = c.cursor()
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET search_path TO lulu_ops")
        cursor.execute(
            "SELECT DISTINCT ON(cluster_id,profile_id) cluster_id,profile_id,status,method,body,qa,revision_id,protected_sha FROM content_revision ORDER BY cluster_id,profile_id,created_at DESC"
        )
        all_revisions = cursor.fetchall()
        by_id = {x[0]: x for x in all_revisions if x[1] == DEFAULT_PROFILE}
        cursor.execute(
            "SELECT DISTINCT ON(cluster_id,profile_id) cluster_id,profile_id,job_id,status,result,created_at FROM content_job ORDER BY cluster_id,profile_id,created_at DESC"
        )
        latest_jobs = cursor.fetchall()
        jobs_by_id = {r[0]: r for r in latest_jobs if r[1] == DEFAULT_PROFILE}
        cursor.execute(
            "SELECT l.cluster_id,l.source_changed,l.patch,s.payload FROM listing l JOIN source_product s USING(snapshot_id) WHERE l.scope='INCLUDED' ORDER BY l.cluster_id"
        )
        listings = cursor.fetchall()
        results = []
        for cluster, changed, patch, source in listings:
            revision = by_id.get(cluster)
            job = jobs_by_id.get(cluster)
            results.append(
                {
                    "cluster_id": cluster,
                    "status": revision[2] if revision else "NO_CANDIDATE",
                    "method": revision[3] if revision else None,
                    "latest_job_id": job[2] if job else None,
                    "latest_job_status": job[3] if job else "NOT_ENQUEUED",
                    "latest_job_result": job[4] if job else None,
                    "revision_id": revision[6] if revision else None,
                    "description_media": len(revision[4].get("description_media") or [])
                    if revision
                    else 0,
                    "protected_sku_media_equal": protected_hash({"source": source})
                    == revision[7]
                    if revision
                    else None,
                    "qa": revision[5] if revision else None,
                }
            )
        cursor.execute("SELECT status,count(*) FROM content_job GROUP BY status")
        jobs = dict(cursor.fetchall())
        cursor.execute(
            "SELECT COUNT(*),SUM(jsonb_array_length(bundle->'ocr_regions')),SUM((SELECT count(*) FROM jsonb_object_keys(assets))) FROM content_evidence"
        )
        # Some PostgreSQL versions do not offer jsonb_object_length; count separately below.
        evidence_count, ocr_count, originals = cursor.fetchone()
        cursor.execute(
            "SELECT cluster_id,coverage,jsonb_array_length(bundle->'image_assets') FROM content_evidence ORDER BY cluster_id"
        )
        source_coverage = [
            {"cluster_id": r[0], "coverage": r[1], "declared_images": r[2]}
            for r in cursor.fetchall()
        ]
        cursor.execute("SELECT count(*) FROM content_vision_cache")
        neutral_cache_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM content_media_qa")
        media_qa_cache_count = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM content_projection")
        active = cursor.fetchone()[0]
        cursor.execute(
            "SELECT kind,status,count(*),SUM((usage->>'input_tokens')::bigint),SUM((usage->>'output_tokens')::bigint) FROM content_call GROUP BY kind,status"
        )
        calls = [
            {
                "kind": r[0],
                "status": r[1],
                "count": r[2],
                "input_tokens": r[3],
                "output_tokens": r[4],
            }
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT j.cluster_id,call.kind,call.status,call.response->>'status',call.response->'error' FROM content_call call JOIN content_job j USING(job_id) WHERE call.status='FAILED' OR call.response->>'status'='failed' ORDER BY call.started_at"
        )
        provider_failures = [
            {
                "cluster_id": r[0],
                "kind": r[1],
                "receipt_status": r[2],
                "response_status": r[3],
                "error": r[4],
            }
            for r in cursor.fetchall()
        ]
        cursor.execute(
            "SELECT count(*),sum((cost->>'amount')::numeric) FROM content_call WHERE status='DONE' AND cost->>'amount' IS NOT NULL"
        )
        monetary_receipts, known_cost = cursor.fetchone()
        c.rollback()
        c.close()
    report = {
        "captured_at": datetime.now(UTC).isoformat(),
        "products": len(results),
        "evidence_count": evidence_count,
        "ocr_regions": ocr_count,
        "owned_originals": int(originals or 0),
        "declared_originals": sum(r["declared_images"] for r in source_coverage),
        "unavailable_originals": sum(
            len(r["coverage"].get("original_sync_failures") or [])
            for r in source_coverage
        ),
        "latest_status": dict(Counter(r["status"] for r in results)),
        "jobs": jobs,
        "current_product_jobs": dict(Counter(r["latest_job_status"] for r in results)),
        "candidate_methods": dict(Counter(r["method"] for r in results if r["method"])),
        "sku_media_protection_pass": all(
            r["protected_sku_media_equal"] is True for r in results if r["revision_id"]
        ),
        "neutral_ocr_cache_count": neutral_cache_count,
        "media_qa_cache_count": media_qa_cache_count,
        "description_media_count": sum(r["description_media"] for r in results),
        "source_coverage": source_coverage,
        "active": active,
        "calls": calls,
        "provider_failures": provider_failures,
        "provider_failure_counts": dict(
            Counter(
                (r["error"] or {}).get("code", "HTTP_OR_PARSE_FAILURE")
                for r in provider_failures
            )
        ),
        "monetary_cost": {
            "confirmed_receipts": monetary_receipts,
            "amount": known_cost,
            "status": "PROVIDER_AMOUNT_NOT_REPORTED"
            if not monetary_receipts
            else "PARTIAL_PROVIDER_RECEIPTS",
        },
        "language_tests": [
            {"cluster_id": r[0], "profile": r[1], "status": r[2], "revision_id": r[6]}
            for r in all_revisions
            if r[1] != DEFAULT_PROFILE
        ],
        "language_test_jobs": [
            {"cluster_id": r[0], "profile": r[1], "job_id": r[2], "status": r[3]}
            for r in latest_jobs
            if r[1] != DEFAULT_PROFILE
        ],
        "image_edits": sum((r["qa"] or {}).get("image_edits", 0) for r in results),
        "source_writes": 0,
        "shopify_writes": 0,
        "results": results,
    }
    stages = {}
    for result in results:
        for stage, seconds in ((result["qa"] or {}).get("timings") or {}).items():
            stages.setdefault(stage, []).append(float(seconds))
    report["stage_timings_seconds"] = {
        stage: {
            "samples": len(values),
            "median": round(median(values), 3),
            "max": max(values),
            "sum": round(sum(values), 3),
        }
        for stage, values in stages.items()
    }
    report["timing_notes"] = (
        "Per-job observed runtime, including compatible cached candidates; media_wait measures residual blocking only, not full media preparation. Review/repair includes repair generation. Source OCR was imported rather than rerun."
    )
    Path("docs/CONTENT_ACCEPTANCE.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k
                not in {
                    "results",
                    "calls",
                    "source_coverage",
                    "stage_timings_seconds",
                    "timing_notes",
                    "provider_failures",
                }
            },
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
