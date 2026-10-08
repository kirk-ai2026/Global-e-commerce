from __future__ import annotations

from psycopg.types.json import Jsonb

from lulu.core import digest

from .policy import DEFAULT_PROFILE, operator_content_hash, profile, protected_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_evidence (
 evidence_id text PRIMARY KEY,cluster_id text NOT NULL,source_snapshot_id text NOT NULL,
 evidence_sha text NOT NULL,bundle jsonb NOT NULL,assets jsonb NOT NULL,coverage jsonb NOT NULL,
 created_at timestamptz DEFAULT now(),UNIQUE(cluster_id,evidence_sha));
CREATE TABLE IF NOT EXISTS content_job (
 job_id text PRIMARY KEY,fingerprint text UNIQUE NOT NULL,cluster_id text NOT NULL,
 profile_id text NOT NULL,evidence_id text REFERENCES content_evidence,intent text NOT NULL,
 status text NOT NULL DEFAULT 'PENDING',base_operator_sha text NOT NULL,protected_sha text NOT NULL,
 checkpoints jsonb NOT NULL DEFAULT '{}',result jsonb NOT NULL DEFAULT '{}',
 lease_owner text,lease_until timestamptz,created_at timestamptz DEFAULT now(),updated_at timestamptz DEFAULT now());
ALTER TABLE content_job ADD COLUMN IF NOT EXISTS recipe jsonb NOT NULL DEFAULT '{}';
CREATE TABLE IF NOT EXISTS content_call (
 fingerprint text PRIMARY KEY,job_id text NOT NULL REFERENCES content_job,kind text NOT NULL,
 status text NOT NULL,request_meta jsonb NOT NULL,response jsonb,usage jsonb,
 cost jsonb,started_at timestamptz DEFAULT now(),finished_at timestamptz);
CREATE TABLE IF NOT EXISTS content_revision (
 revision_id text PRIMARY KEY,job_id text NOT NULL REFERENCES content_job,
 cluster_id text NOT NULL,profile_id text NOT NULL,evidence_id text NOT NULL REFERENCES content_evidence,
 protected_sha text NOT NULL,base_operator_sha text NOT NULL,body jsonb NOT NULL,qa jsonb NOT NULL,
 method text NOT NULL,status text NOT NULL,created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS content_projection (
 cluster_id text NOT NULL,profile_id text NOT NULL,revision_id text NOT NULL REFERENCES content_revision,
 updated_at timestamptz DEFAULT now(),PRIMARY KEY(cluster_id,profile_id));
CREATE TABLE IF NOT EXISTS content_vision_cache (
 source_sha text NOT NULL,contract_sha text NOT NULL,result jsonb NOT NULL,created_at timestamptz DEFAULT now(),
 PRIMARY KEY(source_sha,contract_sha));
CREATE TABLE IF NOT EXISTS content_media_qa (
 asset_sha text NOT NULL,profile_sha text NOT NULL,binding_sha text NOT NULL,result jsonb NOT NULL,
 created_at timestamptz DEFAULT now(),PRIMARY KEY(asset_sha,profile_sha,binding_sha));
CREATE INDEX IF NOT EXISTS content_job_claim ON content_job(status,created_at);
CREATE INDEX IF NOT EXISTS content_revision_cluster ON content_revision(cluster_id,profile_id,created_at);
ALTER TABLE content_revision DROP CONSTRAINT IF EXISTS content_revision_job_id_key;
CREATE INDEX IF NOT EXISTS content_revision_job ON content_revision(job_id,created_at);
"""


class ContentStore:
    def __init__(self, repo):
        self.repo = repo

    def migrate(self):
        with self.repo.connect() as c:
            c.execute(SCHEMA)

    def available(self, c):
        return bool(
            c.execute(
                "SELECT to_regclass(%s) AS value",
                (self.repo.schema + ".content_revision",),
            ).fetchone()["value"]
        )

    def save_evidence(self, cluster, snapshot, bundle, assets, coverage):
        sha = digest(bundle)
        eid = "LCE-" + digest([cluster, sha])[:28]
        with self.repo.connect() as c:
            c.execute(
                "INSERT INTO content_evidence VALUES(%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                (
                    eid,
                    cluster,
                    snapshot,
                    sha,
                    Jsonb(bundle),
                    Jsonb(assets),
                    Jsonb(coverage),
                ),
            )
            regions = {}
            for row in bundle.get("ocr_regions") or []:
                neutral = {
                    k: v
                    for k, v in row.items()
                    if k
                    not in {
                        "localized_text",
                        "applicable_sku_ids",
                        "asset_id",
                        "evidence_id",
                    }
                }
                regions.setdefault(str(row.get("asset_id")), []).append(neutral)
            for image in bundle.get("image_assets") or []:
                if image.get("source_sha256") and image.get("ocr_status") in {
                    "CACHED",
                    "EXTRACTED",
                    "HISTORICAL",
                }:
                    result = {
                        "regions": regions.get(str(image["asset_id"]), []),
                        "visual_facts": image.get("visual_facts") or {},
                        "compatibility": "SOURCE_TEXT_ONLY_NO_MARKET_DECISION",
                        "origin_evidence_id": eid,
                    }
                    c.execute(
                        "INSERT INTO content_vision_cache VALUES(%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                        (
                            image["source_sha256"],
                            digest(
                                [
                                    "neutral-compatible-source-text-v1",
                                    bundle.get("schema"),
                                ]
                            ),
                            Jsonb(result),
                        ),
                    )
        return eid

    def evidence(self, cluster=None, evidence_id=None):
        with self.repo.connect() as c:
            if not self.available(c):
                return None
            if evidence_id:
                return c.execute(
                    "SELECT * FROM content_evidence WHERE evidence_id=%s",
                    (evidence_id,),
                ).fetchone()
            return c.execute(
                "SELECT * FROM content_evidence WHERE cluster_id=%s ORDER BY created_at DESC LIMIT 1",
                (cluster,),
            ).fetchone()

    def create_job(self, row, profile_id=DEFAULT_PROFILE, intent="AUDIT_AND_FILL"):
        p = profile(profile_id)
        with self.repo.connect() as c:
            unresolved = c.execute(
                "SELECT call.fingerprint FROM content_call call JOIN content_job j USING(job_id) WHERE j.cluster_id=%s AND call.kind IN ('COPY','SOURCE_REVIEW') AND call.status='RESULT_UNKNOWN' AND NOT (call.response ? 'id') LIMIT 1",
                (row["cluster_id"],),
            ).fetchone()
            if unresolved:
                raise ValueError(
                    "PREVIOUS_PAID_RESULT_UNKNOWN:" + unresolved["fingerprint"]
                )
        if intent not in {"AUDIT_AND_FILL", "REGENERATE", "MEDIA_EDIT"}:
            raise ValueError("INVALID_CONTENT_INTENT")
        e = self.evidence(row["cluster_id"])
        if not e:
            raise ValueError("FULL_EVIDENCE_NOT_IMPORTED")
        frozen = row["source"]["upstream"]["media_workspace"]["source_snapshot_id"]
        if e["source_snapshot_id"] != frozen:
            raise ValueError("EVIDENCE_SNAPSHOT_MISMATCH")
        from .provider import settings

        config = settings()
        recipe = {
            "copy_contract": p.copy_contract,
            "profile_sha": p.sha,
            "model": config["model"],
            "vision_model": config["vision_model"],
            "reasoning": config["reasoning"],
            "provider": config["base_url"],
        }
        fingerprint = digest(
            [
                row["cluster_id"],
                e["evidence_sha"],
                p.copy_contract,
                protected_hash(row),
                operator_content_hash(row),
                intent,
                recipe,
            ]
        )
        jid = "LCJ-" + fingerprint[:28]
        with self.repo.connect() as c:
            c.execute(
                "INSERT INTO content_job(job_id,fingerprint,cluster_id,profile_id,evidence_id,intent,base_operator_sha,protected_sha,recipe) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    jid,
                    fingerprint,
                    row["cluster_id"],
                    profile_id,
                    e["evidence_id"],
                    intent,
                    operator_content_hash(row),
                    protected_hash(row),
                    Jsonb(recipe),
                ),
            )
        return self.job(jid)

    def job(self, jid):
        with self.repo.connect() as c:
            return c.execute(
                "SELECT * FROM content_job WHERE job_id=%s", (jid,)
            ).fetchone()

    def claim(self, owner, job_ids=None):
        with self.repo.connect() as c:
            condition = " AND job_id=ANY(%s)" if job_ids else ""
            row = c.execute(
                "SELECT * FROM content_job WHERE (status='PENDING' OR (status='RUNNING' AND lease_until<now()))"
                + condition
                + " ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1",
                (job_ids,) if job_ids else (),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE content_job SET status='RUNNING',lease_owner=%s,lease_until=now()+interval '20 minutes',updated_at=now() WHERE job_id=%s",
                (owner, row["job_id"]),
            )
            return row

    def checkpoint(self, jid, stage, value):
        with self.repo.connect() as c:
            c.execute(
                "UPDATE content_job SET checkpoints=checkpoints || %s,lease_until=now()+interval '20 minutes',updated_at=now() WHERE job_id=%s",
                (Jsonb({stage: value}), jid),
            )

    def finish(self, jid, status, result):
        with self.repo.connect() as c:
            c.execute(
                "UPDATE content_job SET status=%s,result=%s,lease_owner=NULL,lease_until=NULL,updated_at=now() WHERE job_id=%s",
                (status, Jsonb(result), jid),
            )

    def start_call(self, jid, fingerprint, kind, meta):
        with self.repo.connect() as c:
            created = c.execute(
                "INSERT INTO content_call(fingerprint,job_id,kind,status,request_meta) VALUES(%s,%s,%s,'DISPATCHED',%s) ON CONFLICT DO NOTHING RETURNING fingerprint",
                (fingerprint, jid, kind, Jsonb(meta)),
            ).fetchone()
            row = c.execute(
                "SELECT * FROM content_call WHERE fingerprint=%s", (fingerprint,)
            ).fetchone()
            row["dispatched_now"] = bool(created)
            return row

    def call(self, fingerprint):
        with self.repo.connect() as c:
            return c.execute(
                "SELECT * FROM content_call WHERE fingerprint=%s", (fingerprint,)
            ).fetchone()

    def matching_call(self, request_sha, kind):
        with self.repo.connect() as c:
            return c.execute(
                "SELECT * FROM content_call WHERE request_meta->>'request_sha'=%s AND kind=%s ORDER BY started_at LIMIT 1",
                (request_sha, kind),
            ).fetchone()

    def finish_call(self, fingerprint, status, response, usage=None, cost=None):
        with self.repo.connect() as c:
            c.execute(
                "UPDATE content_call SET status=%s,response=%s,usage=%s,cost=%s,finished_at=now() WHERE fingerprint=%s",
                (
                    status,
                    Jsonb(response),
                    Jsonb(usage or {}),
                    Jsonb(
                        cost
                        or {"amount": None, "reason": "PROVIDER_BILLING_NOT_RETURNED"}
                    ),
                    fingerprint,
                ),
            )

    def progress_call(self, fingerprint, response):
        with self.repo.connect() as c:
            c.execute(
                "UPDATE content_call SET response=%s WHERE fingerprint=%s AND status='DISPATCHED'",
                (Jsonb(response), fingerprint),
            )

    def save_revision(self, job, body, qa, method):
        rid = "LCR-" + digest([job["fingerprint"], body, qa])[:28]
        with self.repo.connect() as c:
            c.execute(
                "INSERT INTO content_revision VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT(revision_id) DO NOTHING",
                (
                    rid,
                    job["job_id"],
                    job["cluster_id"],
                    job["profile_id"],
                    job["evidence_id"],
                    job["protected_sha"],
                    job["base_operator_sha"],
                    Jsonb(body),
                    Jsonb(qa),
                    method,
                    "QA_PASS" if qa["status"] == "PASS" else "HOLD",
                ),
            )
            return c.execute(
                "SELECT * FROM content_revision WHERE revision_id=%s", (rid,)
            ).fetchone()

    def revisions(self, cluster, profile_id=DEFAULT_PROFILE):
        with self.repo.connect() as c:
            if not self.available(c):
                return []
            return c.execute(
                "SELECT * FROM content_revision WHERE cluster_id=%s AND profile_id=%s ORDER BY created_at DESC",
                (cluster, profile_id),
            ).fetchall()

    def decorate(self, rows, c=None):
        if c is None:
            with self.repo.connect() as connection:
                return self.decorate(rows, connection)
        if not self.available(c):
            return rows
        active = c.execute(
            "SELECT r.* FROM content_projection p JOIN content_revision r USING(revision_id) WHERE p.profile_id=%s",
            (DEFAULT_PROFILE,),
        ).fetchall()
        by_id = {r["cluster_id"]: r for r in active}
        for row in rows:
            row["content_active"] = by_id.get(row["cluster_id"])
        return rows

    def activate(self, cluster, rid, expected, watermark, choice="ACTIVATE_SAFE"):
        if choice not in {"ACTIVATE_SAFE", "REPLACE_OPERATOR", "KEEP_OPERATOR"}:
            raise ValueError("INVALID_ACTIVATION_CHOICE")
        with self.repo.connect() as c:
            c.execute("LOCK TABLE fx_snapshot IN SHARE MODE")
            row = c.execute(
                "SELECT l.*,s.payload source FROM listing l JOIN source_product s USING(snapshot_id) WHERE l.cluster_id=%s FOR UPDATE OF l",
                (cluster,),
            ).fetchone()
            if not row:
                raise ValueError("LISTING_NOT_FOUND")
            self.decorate([row], c)
            fx = c.execute(
                "SELECT payload FROM fx_snapshot ORDER BY date DESC,created_at DESC LIMIT 1"
            ).fetchone()
            if expected != watermark(row, fx["payload"] if fx else None):
                raise ValueError("SOURCE_CHANGED_REVIEW")
            revision = c.execute(
                "SELECT * FROM content_revision WHERE revision_id=%s AND cluster_id=%s",
                (rid, cluster),
            ).fetchone()
            if not revision or revision["status"] != "QA_PASS":
                raise ValueError("CANDIDATE_NOT_QA_PASS")
            if protected_hash(row) != revision["protected_sha"]:
                raise ValueError("PROTECTED_SKU_OR_MEDIA_CHANGED")
            if choice == "KEEP_OPERATOR":
                return {"status": "CURRENT_CONTENT_RETAINED", "revision_id": rid}
            override = row["patch"].get("content_override") or {}
            conflict = operator_content_hash(row) != revision[
                "base_operator_sha"
            ] or bool(override.get("description_html"))
            if conflict and choice != "REPLACE_OPERATOR":
                raise ValueError("OPERATOR_CONTENT_CONFLICT")
            old_patch = row["patch"]
            if choice == "REPLACE_OPERATOR":
                new_content = {
                    k: v for k, v in override.items() if k != "description_html"
                }
                if "locked_fields" in new_content:
                    new_content["locked_fields"] = [
                        x
                        for x in new_content["locked_fields"]
                        if x != "description_html"
                    ]
                c.execute(
                    "UPDATE listing SET patch=%s WHERE cluster_id=%s",
                    (Jsonb({**old_patch, "content_override": new_content}), cluster),
                )
            c.execute(
                "INSERT INTO content_projection VALUES(%s,%s,%s,now()) ON CONFLICT(cluster_id,profile_id) DO UPDATE SET revision_id=excluded.revision_id,updated_at=now()",
                (cluster, revision["profile_id"], rid),
            )
            c.execute(
                "UPDATE listing SET status='OPS_REVIEW',updated_at=now() WHERE cluster_id=%s",
                (cluster,),
            )
            c.execute(
                "INSERT INTO history(cluster_id,event_type,note,payload) VALUES(%s,'CONTENT_ACTIVATION','运营确认启用原文/OCR文案候选',%s)",
                (
                    cluster,
                    Jsonb(
                        {
                            "revision_id": rid,
                            "choice": choice,
                            "previous_operator_patch": old_patch,
                            "shopify_writes": 0,
                        }
                    ),
                ),
            )
            return {"status": "OPS_REVIEW", "revision_id": rid, "shopify_writes": 0}

    def report(self):
        with self.repo.connect() as c:
            return {
                "evidence": c.execute(
                    "SELECT count(*) n FROM content_evidence"
                ).fetchone()["n"],
                "jobs": c.execute(
                    "SELECT status,count(*) n FROM content_job GROUP BY status"
                ).fetchall(),
                "revisions": c.execute(
                    "SELECT method,status,count(*) n FROM (SELECT DISTINCT ON(cluster_id,profile_id) * FROM content_revision ORDER BY cluster_id,profile_id,created_at DESC) current GROUP BY method,status"
                ).fetchall(),
                "calls": c.execute(
                    "SELECT kind,status,count(*) n,sum((usage->>'input_tokens')::bigint) input_tokens,sum((usage->>'output_tokens')::bigint) output_tokens FROM content_call GROUP BY kind,status"
                ).fetchall(),
                "active": c.execute(
                    "SELECT count(*) n FROM content_projection"
                ).fetchone()["n"],
            }
