from __future__ import annotations

import hmac
import os
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .core import LOCALE, data_dir
from .storage import Store


def create_app(store=None, root=None):
    store = store or Store()
    root = (root or data_dir()).resolve()
    app = FastAPI(title="Lulu 商品成品 API", version="0.1.0")

    def authorized(authorization: str | None = Header(default=None)):
        token = os.environ.get("LULU_API_TOKEN")
        if token and not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "AUTHENTICATION_REQUIRED")

    def freeze_id(fid=None):
        if fid:
            with store.connect() as c:
                if not c.execute(
                    "SELECT 1 FROM lulu_freeze WHERE freeze_id=%s", (fid,)
                ).fetchone():
                    raise HTTPException(404, "FREEZE_NOT_FOUND")
            return fid
        f = store.latest_freeze()
        if not f:
            raise HTTPException(503, "FREEZE_REQUIRED")
        return f["freeze_id"]

    protected = [Depends(authorized)]

    @app.get("/health")
    def health():
        try:
            with store.connect() as c:
                c.execute("SELECT 1")
        except (psycopg.Error, OSError):
            raise HTTPException(503, "DATABASE_UNAVAILABLE")
        return {"status": "ok", "application": "lulu", "shopify_writes_enabled": False}

    @app.get("/", response_class=HTMLResponse)
    def preview():
        return (Path(__file__).parent / "templates/ops.html").read_text()

    @app.get("/v1/audit", dependencies=protected)
    def audit():
        f = store.latest_freeze()
        if not f:
            raise HTTPException(503, "FREEZE_REQUIRED")
        with store.connect() as c:
            counts = c.execute(
                "SELECT scope_status,status,count(*) n FROM lulu_product WHERE freeze_id=%s GROUP BY scope_status,status",
                (f["freeze_id"],),
            ).fetchall()
        return {
            "freeze_id": f["freeze_id"],
            "audit": f["audit"],
            "counts": counts,
            "locales_enabled": [LOCALE],
            "channel_writes": 0,
        }

    @app.get("/v1/products", dependencies=protected)
    def products(
        freeze: str | None = None,
        status: str | None = None,
        category: str | None = None,
        scope: str | None = "INCLUDED",
        q: str | None = None,
        after: str = "",
        limit: int = Query(24, ge=1, le=100),
        locale: str = LOCALE,
    ):
        if locale != LOCALE:
            raise HTTPException(422, "LOCALE_NOT_ENABLED")
        fid = freeze_id(freeze)
        rows = store.products(
            fid,
            status=status,
            category=category,
            scope=scope,
            query=q,
            after=after,
            limit=limit,
        )
        items = []
        for r in rows:
            f = r["facts"]
            out = r["result"] or {}
            content = out.get("content") or {}
            items.append(
                {
                    "product_id": r["product_id"],
                    "scope_status": r["scope_status"],
                    "status": r["status"],
                    "title": content.get("title") or f.get("title"),
                    "brand": f.get("brand"),
                    "category": f.get("category"),
                    "region": f.get("brand_region", {}).get("code"),
                    "source_sku_count": len(f.get("source_skus", [])),
                    "content_sku_count": len(out.get("content_skus", [])),
                    "withheld_sku_count": len(out.get("withheld_skus", [])),
                    "observed_at": f.get("observed_at"),
                    "media": out.get("media", []),
                    "reference_image": (f.get("gallery") or [None])[0],
                    "issues": out.get("issues") or f.get("scope_reasons"),
                }
            )
        return {
            "freeze_id": fid,
            "items": items,
            "after": rows[-1]["product_id"] if len(rows) == limit else None,
        }

    @app.get("/v1/products/{pid}", dependencies=protected)
    def product(pid: str, freeze: str | None = None, locale: str = LOCALE):
        if locale != LOCALE:
            raise HTTPException(422, "LOCALE_NOT_ENABLED")
        row = store.product(freeze_id(freeze), pid)
        if not row:
            raise HTTPException(404, "PRODUCT_NOT_FOUND")
        return row

    @app.get("/v1/products/{pid}/preflight", dependencies=protected)
    def preflight(pid: str, freeze: str | None = None):
        row = store.product(freeze_id(freeze), pid)
        if not row:
            raise HTTPException(404, "PRODUCT_NOT_FOUND")
        if not row["result"]:
            raise HTTPException(409, "PRODUCT_NOT_PROCESSED")
        return row["result"]["preflight"]

    @app.post(
        "/v1/products/{pid}/media-evidence", dependencies=protected, status_code=202
    )
    def media_evidence(pid: str, body: dict, freeze: str | None = None):
        from .remediation import import_media

        try:
            return import_media(store, freeze_id(freeze), pid, body, root)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/v1/issues", dependencies=protected)
    def issues(
        freeze: str | None = None, after: str = "", limit: int = Query(50, ge=1, le=100)
    ):
        fid = freeze_id(freeze)
        with store.connect() as c:
            rows = c.execute(
                "SELECT product_id,scope_status,status,facts->'scope_reasons' scope_reasons,result->'issues' issues,result->'remediation' remediation,result->'withheld_skus' withheld_skus FROM lulu_product WHERE freeze_id=%s AND product_id>%s AND (status='NEEDS_EVIDENCE' OR scope_status='SCOPE_REVIEW' OR jsonb_array_length(coalesce(result->'withheld_skus','[]'::jsonb))>0) ORDER BY product_id LIMIT %s",
                (fid, after, limit),
            ).fetchall()
        return {
            "items": rows,
            "after": rows[-1]["product_id"] if len(rows) == limit else None,
        }

    class JobRequest(BaseModel):
        freeze_id: str | None = None
        product_ids: list[str] | None = None
        limit: int | None = Field(None, ge=1, le=100)

    @app.post("/v1/jobs", dependencies=protected, status_code=202)
    def create_job(body: JobRequest):
        try:
            jid = store.create_job(
                freeze_id(body.freeze_id), body.product_ids, limit=body.limit
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return store.job(jid)

    @app.get("/v1/jobs/{jid}", dependencies=protected)
    def job(jid: str):
        r = store.job(jid)
        if not r:
            raise HTTPException(404, "JOB_NOT_FOUND")
        return r

    @app.post("/v1/jobs/{jid}/pause", dependencies=protected)
    def pause(jid: str):
        if not store.pause(jid, True):
            raise HTTPException(404, "JOB_NOT_FOUND")
        return store.job(jid)

    @app.post("/v1/jobs/{jid}/resume", dependencies=protected)
    def resume(jid: str):
        if not store.pause(jid, False):
            raise HTTPException(404, "JOB_NOT_FOUND")
        return store.job(jid)

    @app.get("/assets/{relative:path}", dependencies=protected)
    def asset(relative: str):
        p = (root / relative).resolve()
        if not p.is_relative_to((root / "media").resolve()) or not p.is_file():
            raise HTTPException(404, "ASSET_NOT_FOUND")
        return FileResponse(p)

    return app


app = create_app()
