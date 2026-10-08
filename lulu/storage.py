from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import CONTRACT, database_url, digest


class Store:
    def __init__(self, dsn: str | None = None):
        self.dsn = dsn or database_url()
        self._session_connection = None

    @contextmanager
    def connect(self):
        if self._session_connection is not None:
            with self._session_connection.transaction():
                yield self._session_connection
        else:
            with psycopg.connect(self.dsn, row_factory=dict_row) as c:
                yield c

    @contextmanager
    def session(self):
        """Worker-local persistent connection; operations remain atomic transactions."""
        if self._session_connection is not None:
            raise ValueError("NESTED_WORKER_SESSION")
        with psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True) as c:
            self._session_connection = c
            try:
                yield
            finally:
                self._session_connection = None

    def migrate(self):
        with self.connect() as c:
            c.execute(
                (Path(__file__).parent / "migrations/001_initial.sql").read_text()
            )

    def latest_freeze(self):
        with self.connect() as c:
            r = c.execute(
                "SELECT * FROM lulu_freeze ORDER BY created_at DESC,freeze_id DESC LIMIT 1"
            ).fetchone()
            return r

    def import_freeze(self, manifest, audit, products, evidence):
        fid = "LF-" + digest(manifest)[:24]
        with self.connect() as c:
            c.execute(
                "INSERT INTO lulu_freeze(freeze_id,manifest,audit) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (fid, Jsonb(manifest), Jsonb(audit)),
            )
            for e in evidence:
                old = c.execute(
                    "SELECT payload_sha FROM lulu_evidence WHERE evidence_id=%s",
                    (e["evidence_id"],),
                ).fetchone()
                sha = digest(e)
                if old and old["payload_sha"] != sha:
                    raise ValueError("IMMUTABLE_EVIDENCE_CONFLICT")
                c.execute(
                    "INSERT INTO lulu_evidence VALUES(%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                    (e["evidence_id"], e["product_id"], sha, Jsonb(e)),
                )
            for p in products:
                status = (
                    "EXCLUDED" if p["scope_status"] == "OUT_OF_SCOPE" else "PENDING"
                )
                c.execute(
                    "INSERT INTO lulu_product(freeze_id,product_id,scope_status,status,facts_sha,facts) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        fid,
                        p["product_id"],
                        p["scope_status"],
                        status,
                        digest(p),
                        Jsonb(p),
                    ),
                )
        return fid

    def products(
        self,
        fid,
        *,
        status=None,
        category=None,
        scope=None,
        query=None,
        after="",
        limit=50,
    ):
        clauses, args = ["freeze_id=%s", "product_id>%s"], [fid, after]
        for field, value in [("status", status), ("scope_status", scope)]:
            if value:
                clauses.append(field + "=%s")
                args.append(value)
        if category:
            clauses.append("facts->>'category'=%s")
            args.append(category)
        if query:
            clauses.append("(facts->>'title' ILIKE %s OR facts->>'brand' ILIKE %s)")
            args.extend(["%" + query + "%"] * 2)
        args.append(min(max(limit, 1), 1000))
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM lulu_product WHERE "
                + " AND ".join(clauses)
                + " ORDER BY product_id LIMIT %s",
                args,
            ).fetchall()

    def product(self, fid, pid):
        with self.connect() as c:
            return c.execute(
                "SELECT * FROM lulu_product WHERE freeze_id=%s AND product_id=%s",
                (fid, pid),
            ).fetchone()

    def evidence(self, pid):
        with self.connect() as c:
            return [
                r["payload"]
                for r in c.execute(
                    "SELECT payload FROM lulu_evidence WHERE product_id=%s ORDER BY evidence_id",
                    (pid,),
                )
            ]

    def cache(self, fingerprint):
        with self.connect() as c:
            row = c.execute(
                "SELECT payload FROM lulu_artifact WHERE fingerprint=%s", (fingerprint,)
            ).fetchone()
            return row["payload"] if row else None

    def put_artifact(self, fingerprint, kind, payload):
        with self.connect() as c:
            c.execute(
                "INSERT INTO lulu_artifact(fingerprint,kind,payload) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (fingerprint, kind, Jsonb(payload)),
            )

    def media_evidence(self, fid, pid):
        with self.connect() as c:
            return [
                {**r["payload"], "_receipt_id": r["receipt_id"]}
                for r in c.execute(
                    "SELECT receipt_id,payload FROM lulu_media_evidence WHERE freeze_id=%s AND product_id=%s ORDER BY receipt_id",
                    (fid, pid),
                )
            ]

    def register_media(self, fid, pid, payload):
        rid = "LMR-" + digest(payload)[:24]
        with self.connect() as c:
            c.execute(
                "INSERT INTO lulu_media_evidence(receipt_id,freeze_id,product_id,payload) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (rid, fid, pid, Jsonb(payload)),
            )
        return rid

    def create_job(self, fid, product_ids=None, *, limit=None):
        with self.connect() as c:
            if not c.execute(
                "SELECT 1 FROM lulu_freeze WHERE freeze_id=%s", (fid,)
            ).fetchone():
                raise ValueError("FREEZE_NOT_FOUND")
            rows = c.execute(
                "SELECT product_id,facts_sha,facts FROM lulu_product WHERE freeze_id=%s AND scope_status<>'OUT_OF_SCOPE' ORDER BY product_id",
                (fid,),
            ).fetchall()
            if product_ids is not None:
                wanted = set(product_ids)
                if not wanted or not wanted <= {r["product_id"] for r in rows}:
                    raise ValueError("INVALID_PRODUCT_SCOPE")
                rows = [r for r in rows if r["product_id"] in wanted]
            if limit is not None:
                rows = self.canary(
                    [r for r in rows if r["facts"]["scope_status"] == "INCLUDED"], limit
                )
            receipts = {}
            for r in c.execute(
                "SELECT product_id,receipt_id FROM lulu_media_evidence WHERE freeze_id=%s ORDER BY receipt_id",
                (fid,),
            ):
                receipts.setdefault(r["product_id"], []).append(r["receipt_id"])
            request = {
                "freeze_id": fid,
                "product_ids": [r["product_id"] for r in rows],
                "contract": CONTRACT,
            }
            if receipts:
                request["media_evidence"] = {
                    r["product_id"]: receipts[r["product_id"]]
                    for r in rows
                    if r["product_id"] in receipts
                }
            fp = digest(request)
            jid = "LJ-" + fp[:24]
            c.execute(
                "INSERT INTO lulu_job(job_id,freeze_id,fingerprint,request) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (jid, fid, fp, Jsonb(request)),
            )
            for r in rows:
                parts = [r["product_id"], r["facts_sha"], CONTRACT]
                if r["product_id"] in receipts:
                    parts.append(receipts[r["product_id"]])
                tfp = digest(parts)
                tid = "LT-" + digest([jid, r["product_id"]])[:24]
                c.execute(
                    "INSERT INTO lulu_task(task_id,job_id,product_id,fingerprint) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (tid, jid, r["product_id"], tfp),
                )
            return jid

    @staticmethod
    def canary(rows, limit):
        groups = {}
        for r in rows:
            f = r["facts"]
            key = (
                f.get("category", "unknown"),
                len(f.get("source_skus", [])) > 1,
                bool(f.get("media_candidates")),
            )
            groups.setdefault(key, []).append(r)
        for values in groups.values():
            values.sort(
                key=lambda r: (
                    not bool(r["facts"].get("media_candidates")),
                    not bool(r["facts"].get("source_skus")),
                    r["product_id"],
                )
            )
        out = []
        while groups and len(out) < limit:
            for key in sorted(groups):
                out.append(groups[key].pop(0))
                if not groups[key]:
                    del groups[key]
                if len(out) >= limit:
                    break
        return out

    def claim(self, jid, owner, limit=10):
        with self.connect() as c:
            job = c.execute(
                "SELECT * FROM lulu_job WHERE job_id=%s FOR UPDATE", (jid,)
            ).fetchone()
            if not job:
                raise ValueError("JOB_NOT_FOUND")
            if job["paused"]:
                return []
            tasks = c.execute(
                """WITH selected AS (
                SELECT task_id FROM lulu_task WHERE job_id=%s AND
                (status='QUEUED' OR (status='RUNNING' AND lease_until<now()))
                ORDER BY task_id FOR UPDATE SKIP LOCKED LIMIT %s)
                UPDATE lulu_task SET status='RUNNING',owner=%s,lease_until=now()+interval '5 minutes',attempts=attempts+1
                WHERE task_id IN (SELECT task_id FROM selected) RETURNING *""",
                (jid, limit, owner),
            ).fetchall()
            if tasks:
                c.execute(
                    "UPDATE lulu_job SET status='RUNNING' WHERE job_id=%s", (jid,)
                )
            for t in tasks:
                t["freeze_id"] = job["freeze_id"]
                t["media_evidence_ids"] = (
                    job["request"].get("media_evidence", {}).get(t["product_id"], [])
                )
            return tasks

    def finish(self, task, result, owner):
        with self.connect() as c:
            current = c.execute(
                "SELECT * FROM lulu_task WHERE task_id=%s FOR UPDATE",
                (task["task_id"],),
            ).fetchone()
            if current["owner"] != owner or current["status"] != "RUNNING":
                raise ValueError("LEASE_LOST")
            c.execute(
                "INSERT INTO lulu_artifact(fingerprint,kind,payload) VALUES(%s,'PRODUCT_RESULT',%s) ON CONFLICT DO NOTHING",
                (task["fingerprint"], Jsonb(result)),
            )
            c.execute(
                "UPDATE lulu_product SET status=%s,result=%s,updated_at=now() WHERE freeze_id=%s AND product_id=%s",
                (
                    result["status"],
                    Jsonb(result),
                    task["freeze_id"],
                    task["product_id"],
                ),
            )
            if result.get("content"):
                content = result["content"]
                c.execute(
                    "INSERT INTO lulu_content VALUES(%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                    (
                        task["product_id"],
                        content["locale"],
                        result["facts_sha"],
                        digest(content),
                        Jsonb(content),
                    ),
                )
            c.execute(
                "UPDATE lulu_task SET status='COMPLETE',lease_until=NULL,updated_at=now() WHERE task_id=%s",
                (task["task_id"],),
            )
            receipt = {
                "product_id": task["product_id"],
                "result_sha": digest(result),
                "paid_calls": 0,
                "shopify_writes": 0,
            }
            c.execute(
                "INSERT INTO lulu_receipt VALUES(%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                (
                    "LR-" + digest([task["task_id"], receipt])[:24],
                    task["task_id"],
                    Jsonb(receipt),
                ),
            )
            c.execute(
                "UPDATE lulu_job SET status='COMPLETE' WHERE job_id=%s AND NOT EXISTS(SELECT 1 FROM lulu_task WHERE job_id=%s AND status<>'COMPLETE')",
                (task["job_id"], task["job_id"]),
            )

    def fail(self, task, owner, error):
        with self.connect() as c:
            c.execute(
                "UPDATE lulu_task SET status='FAILED',last_error=%s,lease_until=NULL WHERE task_id=%s AND owner=%s",
                (error, task["task_id"], owner),
            )
            c.execute(
                "UPDATE lulu_job SET status='NEEDS_ATTENTION' WHERE job_id=%s",
                (task["job_id"],),
            )

    def job(self, jid):
        with self.connect() as c:
            r = c.execute("SELECT * FROM lulu_job WHERE job_id=%s", (jid,)).fetchone()
            if r:
                r["counts"] = {
                    x["status"]: x["n"]
                    for x in c.execute(
                        "SELECT status,count(*) n FROM lulu_task WHERE job_id=%s GROUP BY status",
                        (jid,),
                    )
                }
                ids = r["request"].get("product_ids", [])
                r["request"] = {
                    k: v
                    for k, v in r["request"].items()
                    if k not in {"product_ids", "media_evidence"}
                }
                r["request"]["product_count"] = len(ids)
                if len(ids) <= 30:
                    r["request"]["product_ids"] = ids
            return r

    def pause(self, jid, paused):
        with self.connect() as c:
            return c.execute(
                "UPDATE lulu_job SET paused=%s WHERE job_id=%s RETURNING job_id",
                (paused, jid),
            ).fetchone()

    def retry_failed(self, jid):
        with self.connect() as c:
            return c.execute(
                "UPDATE lulu_task SET status='QUEUED',last_error=NULL WHERE job_id=%s AND status='FAILED'",
                (jid,),
            ).rowcount
