"""Provision only new Lulu resources; credentials stay in memory, never in stdout."""

import json
import secrets
import subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlencode, urlsplit

from google.cloud import storage
from google.cloud.sql.connector import Connector
from google.oauth2.credentials import Credentials

from lulu.ops.repository import SCHEMA, OpsRepository

PROJECT = "zenheart"
REGION = "asia-east1"
SA = "lulu-merch-ops@zenheart.iam.gserviceaccount.com"
BUCKET = "zenheart-lulu-merch-ops"


def gc(*args, stdin=None):
    p = subprocess.run(
        ["gcloud", *args, "--project=" + PROJECT, "--quiet"],
        input=stdin,
        capture_output=True,
    )
    if p.returncode:
        raise RuntimeError(p.stderr.decode()[:1200])
    return p.stdout


def exists(*args):
    return (
        subprocess.run(
            ["gcloud", *args, "--project=" + PROJECT], capture_output=True
        ).returncode
        == 0
    )


def secret(name, data):
    if not exists("secrets", "describe", name):
        gc("secrets", "create", name, "--replication-policy=automatic")
    gc("secrets", "versions", "add", name, "--data-file=-", stdin=data.encode())
    gc(
        "secrets",
        "add-iam-policy-binding",
        name,
        "--member=serviceAccount:" + SA,
        "--role=roles/secretmanager.secretAccessor",
    )


if __name__ == "__main__":
    if not exists("iam", "service-accounts", "describe", SA):
        gc(
            "iam",
            "service-accounts",
            "create",
            "lulu-merch-ops",
            "--display-name=Lulu merchandise operations",
        )
    gc(
        "projects",
        "add-iam-policy-binding",
        PROJECT,
        "--member=serviceAccount:" + SA,
        "--role=roles/cloudsql.client",
        "--condition=None",
    )
    if not exists("storage", "buckets", "describe", "gs://" + BUCKET):
        gc(
            "storage",
            "buckets",
            "create",
            "gs://" + BUCKET,
            "--location=" + REGION,
            "--uniform-bucket-level-access",
        )
    gc(
        "storage",
        "buckets",
        "add-iam-policy-binding",
        "gs://" + BUCKET,
        "--member=serviceAccount:" + SA,
        "--role=roles/storage.objectUser",
    )
    credential = Credentials(gc("auth", "print-access-token").decode().strip())
    parsed = urlsplit(
        gc("secrets", "versions", "access", "latest", "--secret=zenheart-database-url")
        .decode()
        .strip()
    )
    if exists("secrets", "describe", "lulu-ops-database-url"):
        dsn = (
            gc(
                "secrets",
                "versions",
                "access",
                "latest",
                "--secret=lulu-ops-database-url",
            )
            .decode()
            .strip()
        )
        password = unquote(urlsplit(dsn).password)
    else:
        password = secrets.token_urlsafe(32)
        dsn = (
            "postgresql://lulu_ops_runtime:"
            + password
            + "@/"
            + parsed.path.lstrip("/")
            + "?"
            + urlencode({"host": "/cloudsql/zenheart:asia-east1:zenheart-pg"})
        )
        secret("lulu-ops-database-url", dsn)
    if not exists("secrets", "describe", "lulu-merch-ops-passphrase"):
        secret("lulu-merch-ops-passphrase", secrets.token_urlsafe(24))
    with Connector(credentials=credential, timeout=60) as connector:
        c = connector.connect(
            "zenheart:asia-east1:zenheart-pg",
            "pg8000",
            user=unquote(parsed.username),
            password=unquote(parsed.password),
            db=parsed.path.lstrip("/"),
        )
        cur = c.cursor()
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname='lulu_ops_runtime'")
        found = bool(cur.fetchone())
        from pg8000.native import literal

        if not found:
            cur.execute(
                "CREATE ROLE lulu_ops_runtime LOGIN PASSWORD " + literal(password)
            )
        c.commit()
        c.autocommit = True
        cur.execute("SELECT 1 FROM pg_database WHERE datname='lulu_merch_ops'")
        if not cur.fetchone():
            cur.execute("CREATE DATABASE lulu_merch_ops")
        cur.execute(
            "GRANT CONNECT, CREATE ON DATABASE lulu_merch_ops TO lulu_ops_runtime"
        )
        c.close()
        c = connector.connect(
            "zenheart:asia-east1:zenheart-pg",
            "pg8000",
            user="lulu_ops_runtime",
            password=password,
            db="lulu_merch_ops",
        )
        cur = c.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS lulu_ops")
        cur.execute("SET search_path TO lulu_ops")
        for statement in SCHEMA.split(";"):
            if statement.strip():
                cur.execute(statement)
        # Import local independent data, never read or write source cloud tables.
        local = OpsRepository()
        with local.connect() as src:
            for table in [
                "source_freeze",
                "source_product",
                "listing",
                "fx_snapshot",
                "media",
                "history",
            ]:
                cols = [
                    r["column_name"]
                    for r in src.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                        (local.schema, table),
                    )
                ]
                rows = src.execute("SELECT * FROM " + table).fetchall()
                for offset in range(0, len(rows), 25):
                    chunk = rows[offset : offset + 25]
                    sql = (
                        "INSERT INTO "
                        + table
                        + "("
                        + ",".join(cols)
                        + ") VALUES "
                        + ",".join(
                            ["(" + ",".join(["%s"] * len(cols)) + ")"] * len(chunk)
                        )
                        + " ON CONFLICT DO NOTHING"
                    )
                    values = [
                        json.dumps(row[col], ensure_ascii=False)
                        if isinstance(row[col], (list, dict))
                        else row[col]
                        for row in chunk
                        for col in cols
                    ]
                    cur.execute(sql, values)
                print(
                    json.dumps({"migrated_table": table, "rows": len(rows)}), flush=True
                )
                if table == "history":
                    cur.execute(
                        "SELECT setval(pg_get_serial_sequence('history','event_id'),COALESCE(MAX(event_id),1)) FROM history"
                    )
        c.commit()
        c.close()
    target_dsn = (
        "postgresql://lulu_ops_runtime:"
        + password
        + "@/lulu_merch_ops?"
        + urlencode({"host": "/cloudsql/zenheart:asia-east1:zenheart-pg"})
    )
    if target_dsn != dsn:
        secret("lulu-ops-database-url", target_dsn)
    client = storage.Client(project=PROJECT, credentials=credential)
    bucket = client.bucket(BUCKET)
    with local.connect() as c:
        media = c.execute("SELECT * FROM media").fetchall()

    def upload(m):
        blob = bucket.blob(m["object_key"])
        if not blob.exists():
            blob.upload_from_filename(
                "data/cloud_ops/" + m["object_key"],
                content_type=m["content_type"],
                if_generation_match=0,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(upload, media))
    print(
        json.dumps(
            {
                "schema": "lulu_ops",
                "bucket": BUCKET,
                "media_objects": len(media),
                "service_account": SA,
                "source_writes": 0,
            }
        )
    )
