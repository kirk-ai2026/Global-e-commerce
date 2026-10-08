from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .assets import AssetStore
from .repository import OpsRepository


def main():
    p = argparse.ArgumentParser(description="Lulu 云端成品运营后台（源端只读）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate")
    imp = sub.add_parser("import")
    imp.add_argument("--source-db", type=Path, required=True)
    imp.add_argument("--limit", type=int)
    imp.add_argument("--workers", type=int, default=4)
    sub.add_parser("fx-refresh")
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.getenv("PORT", "8897")))
    sub.add_parser("audit")
    content_parser = sub.add_parser("content")
    content_sub = content_parser.add_subparsers(dest="content_cmd", required=True)
    content_sub.add_parser("migrate")
    content_sub.add_parser("import-evidence")
    enqueue = content_sub.add_parser("enqueue")
    enqueue.add_argument("--profile", default="ca-zh-Hans-lite-v1")
    enqueue.add_argument("--clusters", nargs="*")
    work = content_sub.add_parser("work")
    work.add_argument("--jobs", nargs="*")
    work.add_argument("--workers", type=int, default=2)
    content_sub.add_parser("report")
    content_sub.add_parser("normalize-provider-failures")
    resume = content_sub.add_parser("resume-provider")
    resume.add_argument("--provider-ready", action="store_true", required=True)
    resume.add_argument("--reason", required=True)
    args = p.parse_args()
    repo = OpsRepository()
    if args.cmd == "content":
        from .content.repository import ContentStore

        content = ContentStore(repo)
        if args.content_cmd == "migrate":
            content.migrate()
            value = {"status": "CONTENT_MIGRATED"}
        elif args.content_cmd == "import-evidence":
            from .content.source import FrozenSourceReader, import_evidence

            with FrozenSourceReader() as reader:
                value = import_evidence(repo, content, AssetStore(), reader)
        elif args.content_cmd == "enqueue":
            value = {
                "jobs": [
                    content.create_job(row, args.profile)["job_id"]
                    for row in repo.all_listings()
                    if row["scope"] == "INCLUDED"
                    and (not args.clusters or row["cluster_id"] in args.clusters)
                ]
            }
        elif args.content_cmd == "work":
            from .content.worker import run_worker

            value = run_worker(
                content, AssetStore(), job_ids=args.jobs, workers=args.workers
            )
        elif args.content_cmd == "normalize-provider-failures":
            value = content.normalize_provider_failures()
        elif args.content_cmd == "resume-provider":
            value = content.resume_provider_blocked(args.reason)
        else:
            value = content.report()
    elif args.cmd == "migrate":
        repo.migrate()
        value = {"status": "MIGRATED", "schema": repo.schema}
    elif args.cmd == "import":
        from .importer import import_cloud

        value = import_cloud(
            args.source_db, repo, AssetStore(), limit=args.limit, workers=args.workers
        )
    elif args.cmd == "fx-refresh":
        from .money import fetch_fx

        value = fetch_fx()
        repo.save_fx(value)
    elif args.cmd == "audit":
        value = repo.latest_audit()
    else:
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv(
            "LULU_OPS_PASSPHRASE"
        ):
            p.error("独立云端运营写入必须配置口令")
        import uvicorn

        uvicorn.run("lulu.ops.api:app", host=args.host, port=args.port)
        return
    print(json.dumps(value, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
