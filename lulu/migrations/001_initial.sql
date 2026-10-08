CREATE TABLE IF NOT EXISTS lulu_freeze (
 freeze_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
 manifest jsonb NOT NULL, audit jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS lulu_evidence (
 evidence_id text PRIMARY KEY, product_id text NOT NULL,
 payload_sha text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lulu_product (
 freeze_id text NOT NULL REFERENCES lulu_freeze(freeze_id),
 product_id text NOT NULL, scope_status text NOT NULL,
 status text NOT NULL DEFAULT 'PENDING', facts_sha text NOT NULL,
 facts jsonb NOT NULL, result jsonb,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(freeze_id,product_id)
);
CREATE INDEX IF NOT EXISTS lulu_product_scope ON lulu_product(freeze_id,scope_status,status);
CREATE TABLE IF NOT EXISTS lulu_job (
 job_id text PRIMARY KEY, freeze_id text NOT NULL REFERENCES lulu_freeze(freeze_id),
 fingerprint text NOT NULL UNIQUE, status text NOT NULL DEFAULT 'QUEUED',
 request jsonb NOT NULL, paused boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lulu_task (
 task_id text PRIMARY KEY, job_id text NOT NULL REFERENCES lulu_job(job_id),
 product_id text NOT NULL, fingerprint text NOT NULL,
 status text NOT NULL DEFAULT 'QUEUED', owner text, lease_until timestamptz,
 attempts integer NOT NULL DEFAULT 0, last_error text,
 updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(job_id,product_id)
);
CREATE TABLE IF NOT EXISTS lulu_artifact (
 fingerprint text PRIMARY KEY, kind text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS lulu_task_unfinished ON lulu_task(job_id) WHERE status<>'COMPLETE';
CREATE INDEX IF NOT EXISTS lulu_task_claim ON lulu_task(job_id,status,task_id);
CREATE TABLE IF NOT EXISTS lulu_receipt (
 receipt_id text PRIMARY KEY, task_id text NOT NULL,
 payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS lulu_content (
 product_id text NOT NULL, locale text NOT NULL, facts_sha text NOT NULL,
 content_sha text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(product_id,locale,facts_sha,content_sha)
);
CREATE TABLE IF NOT EXISTS lulu_media_evidence (
 receipt_id text PRIMARY KEY, freeze_id text NOT NULL REFERENCES lulu_freeze(freeze_id),
 product_id text NOT NULL, payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 FOREIGN KEY(freeze_id,product_id) REFERENCES lulu_product(freeze_id,product_id)
);
