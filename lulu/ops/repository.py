from __future__ import annotations

import contextlib
import os
import re

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from lulu.core import database_url, digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_freeze (
 freeze_id text PRIMARY KEY, created_at timestamptz DEFAULT now(), audit jsonb NOT NULL, source jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS source_product (
 snapshot_id text PRIMARY KEY, cluster_id text NOT NULL, freeze_id text NOT NULL REFERENCES source_freeze,
 payload_sha text NOT NULL, payload jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS listing (
 cluster_id text PRIMARY KEY, snapshot_id text NOT NULL REFERENCES source_product,
 brand_fact jsonb NOT NULL, category text NOT NULL, scope text NOT NULL,
 localized jsonb NOT NULL, patch jsonb NOT NULL DEFAULT '{}', status text NOT NULL DEFAULT 'OPS_REVIEW',
 source_changed boolean NOT NULL DEFAULT false, updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS fx_snapshot (
 fx_id text PRIMARY KEY, date date NOT NULL, payload jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS media (
 sha256 text PRIMARY KEY, object_key text NOT NULL, content_type text NOT NULL, bytes bigint NOT NULL,
 provenance jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS history (
 event_id bigserial PRIMARY KEY, cluster_id text NOT NULL, event_type text NOT NULL,
 note text NOT NULL, payload jsonb NOT NULL, created_at timestamptz DEFAULT now());
CREATE INDEX IF NOT EXISTS history_cluster ON history(cluster_id,event_id);
"""


class OpsRepository:
    def __init__(self, dsn=None, schema=None):
        self.dsn = dsn or os.getenv("LULU_OPS_DATABASE_URL") or database_url()
        self.schema = schema or os.getenv("LULU_OPS_SCHEMA", "lulu_ops")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", self.schema):
            raise ValueError("INVALID_LULU_SCHEMA")
        if self.schema.startswith("wujie"):
            raise ValueError("UPSTREAM_SCHEMA_WRITE_FORBIDDEN")

    @contextlib.contextmanager
    def connect(self):
        with psycopg.connect(
            self.dsn, row_factory=dict_row, connect_timeout=15
        ) as connection:
            connection.execute('SET search_path TO "' + self.schema + '"')
            yield connection

    def migrate(self):
        with psycopg.connect(self.dsn) as connection:
            connection.execute('CREATE SCHEMA IF NOT EXISTS "' + self.schema + '"')
            connection.execute('SET search_path TO "' + self.schema + '"')
            connection.execute(SCHEMA)

    def current_fx(self):
        with self.connect() as c:
            row = c.execute(
                "SELECT payload FROM fx_snapshot ORDER BY date DESC,created_at DESC LIMIT 1"
            ).fetchone()
            return row["payload"] if row else None

    def save_fx(self, value):
        with self.connect() as c:
            old = c.execute(
                "SELECT fx_id FROM fx_snapshot ORDER BY date DESC,created_at DESC LIMIT 1"
            ).fetchone()
            c.execute(
                "INSERT INTO fx_snapshot(fx_id,date,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (value["fx_id"], value["date"], Jsonb(value)),
            )
            if old and old["fx_id"] != value["fx_id"]:
                c.execute(
                    "UPDATE listing SET status='RECONFIRM_REQUIRED',updated_at=now() WHERE status='OPS_APPROVED'"
                )

    def import_batch(self, freeze_id, audit, source, records):
        with self.connect() as c:
            c.execute(
                "INSERT INTO source_freeze VALUES(%s,now(),%s,%s) ON CONFLICT DO NOTHING",
                (freeze_id, Jsonb(audit), Jsonb(source)),
            )
            for record in records:
                raw = {
                    **record["source"],
                    "lulu_scope_evidence": {
                        "brand_fact": record["brand_fact"],
                        "category": record["category"],
                        "scope": record["scope"],
                        "content_sha": record["localized"]["content_sha"],
                    },
                }
                sha = digest(
                    {
                        k: v
                        for k, v in raw.items()
                        if k not in {"captured_at", "media_downloads"}
                    }
                )
                sid = "LS-" + sha[:32]
                cluster = record["cluster_id"]
                existing = c.execute(
                    "SELECT * FROM listing WHERE cluster_id=%s", (cluster,)
                ).fetchone()
                c.execute(
                    "INSERT INTO source_product VALUES(%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                    (sid, cluster, freeze_id, sha, Jsonb(raw)),
                )
                if existing and existing["snapshot_id"] == sid:
                    continue
                status = "BRAND_REVIEW" if record["scope"] == "REVIEW" else "OPS_REVIEW"
                if existing and existing["patch"]:
                    status = "RECONFIRM_REQUIRED"
                c.execute(
                    """INSERT INTO listing(cluster_id,snapshot_id,brand_fact,category,scope,localized,status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(cluster_id) DO UPDATE SET
                    snapshot_id=excluded.snapshot_id,brand_fact=excluded.brand_fact,category=excluded.category,
                    scope=excluded.scope,localized=excluded.localized,status=excluded.status,
                    source_changed=(listing.patch<>'{}'::jsonb),updated_at=now()""",
                    (
                        cluster,
                        sid,
                        Jsonb(record["brand_fact"]),
                        record["category"],
                        record["scope"],
                        Jsonb(record["localized"]),
                        status,
                    ),
                )
                c.execute(
                    "INSERT INTO history(cluster_id,event_type,note,payload) VALUES(%s,'IMPORT','同步无界云端有效成品',%s)",
                    (
                        cluster,
                        Jsonb(
                            {
                                "snapshot_id": sid,
                                "freeze_id": freeze_id,
                                "upstream_writes": 0,
                            }
                        ),
                    ),
                )

    def all_listings(self):
        with self.connect() as c:
            rows = c.execute(
                "SELECT l.*,s.payload source FROM listing l JOIN source_product s USING(snapshot_id) ORDER BY l.cluster_id"
            ).fetchall()
            from .content.repository import ContentStore

            return ContentStore(self).decorate(rows, c)

    def get(self, cluster):
        with self.connect() as c:
            row = c.execute(
                "SELECT l.*,s.payload source FROM listing l JOIN source_product s USING(snapshot_id) WHERE l.cluster_id=%s",
                (cluster,),
            ).fetchone()
            if row:
                row["history"] = c.execute(
                    "SELECT event_type,note,created_at,payload FROM history WHERE cluster_id=%s ORDER BY event_id DESC LIMIT 100",
                    (cluster,),
                ).fetchall()
                from .content.repository import ContentStore

                ContentStore(self).decorate([row], c)
            return row

    def latest_audit(self):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM source_freeze ORDER BY created_at DESC LIMIT 1"
            ).fetchone()

    def media(self, sha):
        with self.connect() as c:
            return c.execute("SELECT * FROM media WHERE sha256=%s", (sha,)).fetchone()

    def save_media(self, sha, key, content_type, size, provenance):
        with self.connect() as c:
            c.execute(
                "INSERT INTO media VALUES(%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                (sha, key, content_type, size, Jsonb(provenance)),
            )

    def mutate(
        self,
        cluster,
        expected,
        current_watermark,
        *,
        patch=None,
        decision=None,
        note="",
    ):
        with self.connect() as c:
            c.execute("LOCK TABLE fx_snapshot IN SHARE MODE")
            row = c.execute(
                "SELECT * FROM listing WHERE cluster_id=%s FOR UPDATE", (cluster,)
            ).fetchone()
            if not row:
                raise ValueError("LISTING_NOT_FOUND")
            fx = c.execute(
                "SELECT payload FROM fx_snapshot ORDER BY date DESC,created_at DESC LIMIT 1"
            ).fetchone()
            from .content.repository import ContentStore

            ContentStore(self).decorate([row], c)
            actual = current_watermark(row, fx["payload"] if fx else None)
            if expected != actual:
                raise ValueError("SOURCE_CHANGED_REVIEW")
            value = row["patch"]
            if patch is not None:
                value = merge(value, patch)
            target = {
                "APPROVE": "OPS_APPROVED",
                "DEFER": "DEFERRED",
                "RESTORE": "OPS_REVIEW",
                "RETURN_TO_AI": "NEEDS_EVIDENCE",
            }.get(decision, "OPS_REVIEW")
            c.execute(
                "UPDATE listing SET patch=%s,status=%s,source_changed=false,updated_at=now() WHERE cluster_id=%s",
                (Jsonb(value), target, cluster),
            )
            c.execute(
                "INSERT INTO history(cluster_id,event_type,note,payload) VALUES(%s,%s,%s,%s)",
                (
                    cluster,
                    decision or "EDIT",
                    note,
                    Jsonb(
                        {
                            "patch": patch,
                            "base_watermark": expected,
                            "shopify_writes": 0,
                        }
                    ),
                ),
            )
            return target


def merge(base, patch):
    result = dict(base or {})
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result
