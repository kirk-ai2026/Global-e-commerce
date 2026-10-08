"""Copy owned evidence/candidates and media to owned cloud DB/bucket only."""

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote, urlsplit

from google.cloud import storage
from google.cloud.sql.connector import Connector
from google.oauth2.credentials import Credentials

from lulu.ops.content.repository import SCHEMA
from lulu.ops.repository import OpsRepository
from scripts.provision_lulu_ops import gc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--caches-only",
        action="store_true",
        help="Append neutral OCR/media QA caches without touching live jobs or media",
    )
    args = parser.parse_args()
    local = OpsRepository()
    credential = Credentials(gc("auth", "print-access-token").decode().strip())
    dsn = urlsplit(
        gc("secrets", "versions", "access", "latest", "--secret=lulu-ops-database-url")
        .decode()
        .strip()
    )
    with Connector(credentials=credential, timeout=60) as connector:
        remote = connector.connect(
            "zenheart:asia-east1:zenheart-pg",
            "pg8000",
            user=unquote(dsn.username),
            password=unquote(dsn.password),
            db=dsn.path.lstrip("/"),
        )
        cursor = remote.cursor()
        cursor.execute("SET search_path TO lulu_ops")
        if not args.caches_only:
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        with local.connect() as c:
            tables = [
                "media",
                "content_evidence",
                "content_job",
                "content_call",
                "content_revision",
                "content_vision_cache",
                "content_media_qa",
            ]
            if args.caches_only:
                tables = ["content_vision_cache", "content_media_qa"]
            for table in tables:
                cols = [
                    x["column_name"]
                    for x in c.execute(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                        (local.schema, table),
                    )
                ]
                rows = c.execute("SELECT * FROM " + table).fetchall()
                if table == "content_job" and any(
                    r["status"] == "RUNNING" for r in rows
                ):
                    raise RuntimeError(
                        "LOCAL_WORKER_STILL_RUNNING_WAIT_FOR_CHECKPOINTS"
                    )
                for start in range(0, len(rows), 20):
                    chunk = rows[start : start + 20]
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
                        json.dumps(row[k], ensure_ascii=False)
                        if isinstance(row[k], (dict, list))
                        else row[k]
                        for row in chunk
                        for k in cols
                    ]
                    cursor.execute(sql, values)
                print(
                    json.dumps({"synced_table": table, "rows": len(rows)}), flush=True
                )
        remote.commit()
        remote.close()
    if args.caches_only:
        return
    bucket = storage.Client(project="zenheart", credentials=credential).bucket(
        "zenheart-lulu-merch-ops"
    )
    with local.connect() as c:
        images = c.execute("SELECT * FROM media").fetchall()

    def upload(m):
        blob = bucket.blob(m["object_key"])
        if not blob.exists():
            blob.upload_from_filename(
                "data/cloud_ops/" + m["object_key"],
                content_type=m["content_type"],
                if_generation_match=0,
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(upload, images))
    print(
        json.dumps(
            {
                "synced_media": len(images),
                "upstream_writes": 0,
                "content_projection_writes": 0,
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
