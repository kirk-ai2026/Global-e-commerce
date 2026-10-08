from dataclasses import asdict

from fastapi import Header, HTTPException

from lulu.ops.projection import effective

from .media import assemble_html
from .policy import DEFAULT_PROFILE, PROFILES
from .repository import ContentStore
from .upstream.ops_source_text import description_source_view


def source_view(content, row):
    active = row.get("content_active")
    evidence = (
        content.evidence(evidence_id=active["evidence_id"])
        if active
        else content.evidence(row["cluster_id"])
    )
    manifest = (
        active["body"]
        if active
        else row["source"]["upstream"]["media_workspace"]["current_manifest"]
    )
    return {
        **description_source_view(manifest, evidence["bundle"] if evidence else None),
        "title": "实际来源原文、逐图 OCR 与引用",
        "content_origin": active["method"] if active else "LEGACY_LOCALIZED",
        "evidence_id": evidence["evidence_id"] if evidence else None,
        "coverage": evidence["coverage"] if evidence else {},
        "owned_images": list(evidence["assets"].values()) if evidence else [],
        "profile_id": active["profile_id"] if active else DEFAULT_PROFILE,
    }


def register(app, repo, assets, authorized, watermark):
    from .launcher import kick

    content = ContentStore(repo)

    @app.get("/api/content/profiles")
    def profiles():
        return {
            "items": [asdict(p) for p in PROFILES.values()],
            "default_profile": DEFAULT_PROFILE,
        }

    @app.get("/api/content/report")
    def report():
        return content.report()

    @app.get("/api/content/issues")
    def issues():
        with repo.connect() as c:
            return {
                "jobs": c.execute(
                    "SELECT job_id,cluster_id,profile_id,intent,status,result FROM content_job WHERE status IN ('NEEDS_EVIDENCE','RESULT_UNKNOWN') ORDER BY updated_at DESC"
                ).fetchall(),
                "source_media": c.execute(
                    "SELECT cluster_id,coverage->'original_sync_failures' missing FROM content_evidence WHERE jsonb_array_length(coverage->'original_sync_failures')>0"
                ).fetchall(),
                "shopify_writes": 0,
            }

    @app.post("/api/v2/ops/listings/{cluster}/description-media-tasks")
    def media_task(
        cluster: str, body: dict, x_ops_token: str | None = Header(default=None)
    ):
        authorized(x_ops_token)
        row = repo.get(cluster)
        if not row:
            raise HTTPException(404, "商品不存在")
        if body.get("base_watermark") != watermark(row, repo.current_fx()):
            raise HTTPException(409, "SOURCE_CHANGED_REVIEW")
        from .media_edit import create_media_job

        try:
            job = create_media_job(
                content,
                row,
                str(body.get("asset_id") or ""),
                body.get("profile_id", DEFAULT_PROFILE),
            )
        except ValueError as error:
            raise HTTPException(422, str(error))
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "shopify_writes": 0,
            "worker": kick()
            if job["status"] == "PENDING"
            else {"status": "EXISTING_TASK"},
        }

    @app.get("/api/content/jobs/{jid}")
    def job(jid: str):
        row = content.job(jid)
        if not row:
            raise HTTPException(404, "内容任务不存在")
        return row

    @app.get("/api/v2/ops/listings/{cluster}/content")
    def revisions(cluster: str, profile_id: str = DEFAULT_PROFILE):
        row = repo.get(cluster)
        if not row:
            raise HTTPException(404, "商品不存在")
        items = content.revisions(cluster, profile_id)
        for revision in items:
            revision["preview_html"] = assemble_html(
                revision["body"]["description_text_html"],
                revision["body"].get("description_media") or [],
            )
            revision["operator_conflict"] = bool(
                row["patch"].get("content_override", {}).get("description_html")
            )
        with repo.connect() as c:
            jobs = (
                c.execute(
                    "SELECT job_id,profile_id,status,result,updated_at FROM content_job WHERE cluster_id=%s AND profile_id=%s ORDER BY created_at DESC LIMIT 10",
                    (cluster, profile_id),
                ).fetchall()
                if content.available(c)
                else []
            )
        return {
            "items": items,
            "jobs": jobs,
            "active_revision_id": (row.get("content_active") or {}).get("revision_id"),
            "profile_id": profile_id,
            "current_text_html": effective(row, repo.current_fx())["consumer"][
                "description_text_html"
            ],
        }

    @app.post("/api/v2/ops/listings/{cluster}/content-jobs")
    def create(
        cluster: str, body: dict, x_ops_token: str | None = Header(default=None)
    ):
        authorized(x_ops_token)
        row = repo.get(cluster)
        if not row:
            raise HTTPException(404, "商品不存在")
        if body.get("base_watermark") != watermark(row, repo.current_fx()):
            raise HTTPException(409, "SOURCE_CHANGED_REVIEW")
        try:
            job = content.create_job(
                row,
                body.get("profile_id", DEFAULT_PROFILE),
                body.get("intent", "AUDIT_AND_FILL"),
            )
        except ValueError as error:
            raise HTTPException(422, str(error))
        return {
            "job_id": job["job_id"],
            "status": job["status"],
            "profile_id": job["profile_id"],
            "shopify_writes": 0,
            "worker": kick()
            if job["status"] == "PENDING"
            else {"status": "EXISTING_TASK"},
        }

    @app.post("/api/v2/ops/listings/{cluster}/content-candidates/{rid}/activate")
    def activate(
        cluster: str,
        rid: str,
        body: dict,
        x_ops_token: str | None = Header(default=None),
    ):
        authorized(x_ops_token)
        try:
            return content.activate(
                cluster,
                rid,
                body.get("base_watermark"),
                watermark,
                body.get("choice", "ACTIVATE_SAFE"),
            )
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get("/api/v2/ops/listings/{cluster}/content-candidates/{rid}/export")
    def export(cluster: str, rid: str):
        candidate = next(
            (
                r
                for p in PROFILES
                for r in content.revisions(cluster, p)
                if r["revision_id"] == rid
            ),
            None,
        )
        if not candidate:
            raise HTTPException(404, "候选不存在")
        return {
            "revision_id": rid,
            "locale": candidate["body"]["locale"],
            "market": candidate["body"]["market"],
            "description_html": assemble_html(
                candidate["body"]["description_text_html"],
                candidate["body"].get("description_media") or [],
                absolute_base="https://lulu-merch-ops-wiv3tqv5ua-de.a.run.app",
            ),
            "description_media": candidate["body"].get("description_media") or [],
            "qa": candidate["qa"],
            "shopify_writes": 0,
        }
