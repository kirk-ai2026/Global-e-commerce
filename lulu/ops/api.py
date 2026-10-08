from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from lulu.core import canonical

from .assets import AssetStore
from .localization import canonical_numbers, hans, safe_html
from .money import decimal, fetch_fx
from .projection import effective, list_projection, watermark
from .repository import OpsRepository


def create_app(repo=None, assets=None, passphrase=None):
    repo = repo or OpsRepository()
    assets = assets or AssetStore()
    passphrase = (
        passphrase if passphrase is not None else os.getenv("LULU_OPS_PASSPHRASE", "")
    )
    app = FastAPI(title="Lulu 商品运营后台", version="1.0.0")
    static = Path(__file__).parent / "static"
    secret = hashlib.sha256((passphrase + "|lulu-ops-session-v1").encode()).digest()

    def issue():
        raw = (
            base64.urlsafe_b64encode(
                canonical(
                    {"exp": int(time.time()) + 3600, "scope": "lulu-ops"}
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        return raw + "." + hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()

    def verify(token):
        try:
            raw, signature = token.split(".")
            if not hmac.compare_digest(
                signature, hmac.new(secret, raw.encode(), hashlib.sha256).hexdigest()
            ):
                return False
            value = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            return (
                value.get("scope") == "lulu-ops" and value.get("exp", 0) >= time.time()
            )
        except (ValueError, TypeError):
            return False

    def authorized(token):
        if not passphrase or not token or not verify(token):
            raise HTTPException(401, "运营写入需要独立口令")

    def detail(cluster):
        row = repo.get(cluster)
        if not row:
            raise HTTPException(404, "商品不存在")
        return effective(row, repo.current_fx())

    @app.get("/health")
    def health():
        try:
            repo.latest_audit()
        except Exception:
            raise HTTPException(503, "后台数据库尚未就绪")
        return {
            "status": "ok",
            "application": "lulu",
            "currency": "CAD",
            "locale": "zh-Hans",
            "shopify_writes": 0,
        }

    @app.get("/", response_class=HTMLResponse)
    def root():
        return (static / "index.html").read_text()

    @app.get("/app.js")
    def js():
        return FileResponse(static / "app.js", media_type="application/javascript")

    @app.get("/app.css")
    def css():
        return FileResponse(static / "app.css", media_type="text/css")

    @app.get("/app-v2.css")
    def css2():
        return FileResponse(static / "app-v2.css", media_type="text/css")

    @app.get("/api/session")
    def session():
        return {"token": "", "writeAuthRequired": True, "shopifyWrites": False}

    @app.post("/api/session")
    def login(body: dict):
        if not passphrase or not hmac.compare_digest(
            str(body.get("passphrase") or ""), passphrase
        ):
            raise HTTPException(403, "运营口令错误")
        return {"token": issue(), "shopifyWrites": False}

    @app.get("/api/v2/ops/listings")
    def listings(
        q: str = "",
        status: str = "",
        category: str = "",
        origin: str = "",
        brand_country: str = "",
        media: str = "",
        page: int = 1,
        page_size: int = 40,
    ):
        return list_projection(
            repo,
            q=q,
            status=status,
            category=category,
            origin=origin,
            brand_country=brand_country,
            media=media,
            page=page,
            page_size=page_size,
        )

    @app.get("/api/v2/ops/listings/{cluster}")
    def listing(cluster: str):
        return detail(cluster)

    @app.put("/api/v2/ops/listings/{cluster}/draft")
    def edit(cluster: str, body: dict, x_ops_token: str | None = Header(default=None)):
        authorized(x_ops_token)
        d = detail(cluster)
        patch = body.get("patch") or {}
        if set(patch) - {"content_override", "price_weight_override", "media_override"}:
            raise HTTPException(422, "不支持的修改字段")
        content = patch.get("content_override") or {}
        if set(content) - {
            "title",
            "description_html",
            "seo_title",
            "seo_description",
            "locked_fields",
        }:
            raise HTTPException(422, "不支持的内容字段")
        for key in ["title", "description_html", "seo_title", "seo_description"]:
            if key in content:
                content[key] = (
                    safe_html(hans(content[key]))
                    if key == "description_html"
                    else hans(content[key])
                )
        if "description_html" in content and not content["description_html"].strip():
            raise HTTPException(422, "商品介绍不能为空")
        if content.get("title") is not None and (
            not content["title"].strip()
            or canonical_numbers(content["title"])
            != canonical_numbers(d["consumer"]["title"])
        ):
            raise HTTPException(422, "标题为空或规格数字变更；请先核验来源")
        prices = (patch.get("price_weight_override") or {}).get("variants") or {}
        known = {str(v["platform_sku_id"]) for v in d["consumer"]["variants"]}
        if set(prices) - known:
            raise HTTPException(422, "价格修改必须绑定当前成品 SKU")
        for value in prices.values():
            if set(value) - {"procurement_price_cny", "billable_weight_g", "reason"}:
                raise HTTPException(422, "只能修改采购价及计费重量")
            if not value.get("reason"):
                raise HTTPException(422, "修改价格或重量必须说明原因")
            for key in ["procurement_price_cny", "billable_weight_g"]:
                if key in value and (
                    decimal(value[key]) is None or decimal(value[key]) <= 0
                ):
                    raise HTTPException(422, "采购价和重量必须为正数")
        media_patch = (patch.get("media_override") or {}).get("assets") or {}
        known_assets = {
            m["asset_id"] for m in d["consumer"]["media"] + d["hidden_media"]
        }
        if set(media_patch) - known_assets:
            raise HTTPException(422, "只能调整已同步成品媒体")
        if any(v.get("is_primary") for v in media_patch.values()):
            raise HTTPException(422, "主图身份沿用源 QA，不能凭排序重新指定")
        try:
            repo.mutate(
                cluster,
                body.get("base_watermark"),
                watermark,
                patch=patch,
                note=str(body.get("note") or "运营修改"),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        after = detail(cluster)
        return {
            "status": after["listing"]["status"],
            "watermark": after["watermark"],
            "shopify_writes": 0,
        }

    @app.post("/api/v2/ops/listings/{cluster}/decision")
    def decision(
        cluster: str, body: dict, x_ops_token: str | None = Header(default=None)
    ):
        authorized(x_ops_token)
        d = detail(cluster)
        value = body.get("decision")
        if value not in {"APPROVE", "DEFER", "RESTORE", "RETURN_TO_AI"}:
            raise HTTPException(422, "本期仅支持内部审核，不执行店铺发布")
        if value == "APPROVE" and not d["readiness"]["eligible"]:
            raise HTTPException(409, "当前商品尚未通过内部审核硬门")
        try:
            status = repo.mutate(
                cluster,
                body.get("base_watermark"),
                watermark,
                decision=value,
                note=str(body.get("note") or "内部审核"),
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"status": status, "shopify_writes": 0}

    @app.get("/api/v2/ops/listings/{cluster}/source-text")
    def source_text(cluster: str):
        row = repo.get(cluster)
        if not row:
            raise HTTPException(404, "商品不存在")
        upstream = row["source"]["upstream"]
        return {
            "kind": "SOURCE_AUDIT",
            "title": "无界原始文本与来源证据",
            "original_title": upstream["detail"]["consumer"].get("title"),
            "original_html": upstream["detail"]["consumer"].get("description_html"),
            "source_snapshot": upstream["media_workspace"].get("source_snapshot_id"),
            "upstream_readiness": upstream["detail"].get("readiness"),
            "source_sha": row["source"]["source_sha"],
        }

    @app.get("/api/v2/ops/listings/{cluster}/media-library")
    def media_library(cluster: str):
        d = detail(cluster)
        return {
            "items": [
                {
                    **m,
                    "kind": "已审核成品",
                    "label": m.get("selected_version") or "源 QA 已通过",
                    "can_promote": False,
                    "needs_ai": False,
                }
                for m in d["consumer"]["media"] + d["hidden_media"]
            ],
            "source_sku_count": d["procurement"].get("source_variant_count"),
            "shopify_writes": 0,
        }

    @app.get("/api/media/{sha}")
    def media_bytes(sha: str):
        if not __import__("re").fullmatch(r"[a-f0-9]{64}", sha):
            raise HTTPException(404)
        m = repo.media(sha)
        if not m:
            raise HTTPException(404)
        data = assets.read(m["object_key"])
        if hashlib.sha256(data).hexdigest() != sha:
            raise HTTPException(409, "媒体文件版本不一致")
        return Response(
            data,
            media_type=m["content_type"],
            headers={"Cache-Control": "public,max-age=31536000,immutable"},
        )

    @app.get("/api/fx")
    def fx():
        return repo.current_fx() or {"status": "MISSING"}

    @app.post("/api/fx/refresh")
    def refresh(x_ops_token: str | None = Header(default=None)):
        authorized(x_ops_token)
        try:
            value = fetch_fx()
            repo.save_fx(value)
        except (OSError, ValueError):
            return {"status": "FETCH_FAILED", "retained_snapshot": repo.current_fx()}
        return {"status": "UPDATED", "snapshot": value}

    @app.post("/internal/fx-refresh")
    def scheduled_refresh(authorization: str | None = Header(default=None)):
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        try:
            claims = id_token.verify_oauth2_token(
                (authorization or "").removeprefix("Bearer "),
                GoogleRequest(),
                os.environ["LULU_OPS_URL"],
            )
            if claims.get("email") != os.environ[
                "LULU_OPS_SCHEDULER_EMAIL"
            ] or not claims.get("email_verified"):
                raise ValueError("wrong identity")
        except Exception:
            raise HTTPException(401, "Scheduler 身份无效")
        value = fetch_fx()
        repo.save_fx(value)
        return {"status": "UPDATED", "fx_id": value["fx_id"]}

    @app.get("/api/audit")
    def audit():
        value = repo.latest_audit()
        if not value:
            raise HTTPException(503, "尚未导入云端货盘")
        return {
            "freeze_id": value["freeze_id"],
            "audit": value["audit"],
            "currency": "CAD",
            "locale": "zh-Hans",
            "fx": repo.current_fx(),
            "shopify_writes": 0,
        }

    return app


app = create_app()
