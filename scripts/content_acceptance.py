"""Read-only snapshot of owned cloud content progress and protected inputs."""

import json
from collections import Counter
from pathlib import Path
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
            "SELECT l.cluster_id,l.source_changed,l.patch,s.payload FROM listing l JOIN source_product s USING(snapshot_id) WHERE l.scope='INCLUDED' ORDER BY l.cluster_id"
        )
        listings = cursor.fetchall()
        results = []
        for cluster, changed, patch, source in listings:
            revision = by_id.get(cluster)
            results.append(
                {
                    "cluster_id": cluster,
                    "status": revision[2] if revision else "NO_CANDIDATE",
                    "method": revision[3] if revision else None,
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
        c.rollback()
        c.close()
    report = {
        "products": len(results),
        "evidence_count": evidence_count,
        "ocr_regions": ocr_count,
        "owned_originals": originals,
        "latest_status": dict(Counter(r["status"] for r in results)),
        "jobs": jobs,
        "active": active,
        "calls": calls,
        "language_tests": [
            {"cluster_id": r[0], "profile": r[1], "status": r[2], "revision_id": r[6]}
            for r in all_revisions
            if r[1] != DEFAULT_PROFILE
        ],
        "image_edits": sum((r["qa"] or {}).get("image_edits", 0) for r in results),
        "source_writes": 0,
        "shopify_writes": 0,
        "results": results,
    }
    Path("docs/CONTENT_ACCEPTANCE.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str)
    )
    print(
        json.dumps(
            {k: v for k, v in report.items() if k not in {"results", "calls"}},
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
