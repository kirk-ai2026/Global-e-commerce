from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .storage import Store


def main():
    p = argparse.ArgumentParser(description="Lulu 简中商品成品链路（不写实店）")
    p.add_argument("--database-url", default=None)
    commands = p.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--source-db", type=Path, required=True)
    freeze.add_argument("--state-db", type=Path)
    job = commands.add_parser("job")
    job.add_argument("--freeze")
    job.add_argument("--limit", type=int)
    job.add_argument("--products", nargs="+")
    worker = commands.add_parser("worker")
    worker.add_argument("--job", required=True)
    worker.add_argument("--batch-size", type=int, default=20)
    worker.add_argument("--max-tasks", type=int)
    status = commands.add_parser("status")
    status.add_argument("--job")
    intake = commands.add_parser("media-import")
    intake.add_argument("--freeze")
    intake.add_argument("--product", required=True)
    intake.add_argument("--receipt", type=Path, required=True)
    for action in ["pause", "resume", "retry-failed"]:
        cmd = commands.add_parser(action)
        cmd.add_argument("--job", required=True)
    report = commands.add_parser("export")
    report.add_argument("--freeze")
    report.add_argument("--job")
    report.add_argument("--output", type=Path, required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8896)
    args = p.parse_args()
    store = Store(args.database_url)
    if args.database_url:
        os.environ["LULU_DATABASE_URL"] = args.database_url
    if args.command == "migrate":
        store.migrate()
        result = {"migration": "complete"}
    elif args.command == "freeze":
        from .importer import freeze

        fid, audit = freeze(args.source_db, args.state_db, store)
        result = {"freeze_id": fid, "audit": audit}
    elif args.command == "job":
        fid = args.freeze or store.latest_freeze()["freeze_id"]
        result = store.job(store.create_job(fid, args.products, limit=args.limit))
    elif args.command == "worker":
        from .worker import run

        result = run(
            store, args.job, batch_size=args.batch_size, max_tasks=args.max_tasks
        )
    elif args.command == "status":
        result = store.job(args.job) if args.job else store.latest_freeze()
    elif args.command == "media-import":
        from .core import data_dir
        from .remediation import import_media

        fid = args.freeze or store.latest_freeze()["freeze_id"]
        result = import_media(
            store, fid, args.product, json.loads(args.receipt.read_text()), data_dir()
        )
    elif args.command in {"pause", "resume"}:
        result = store.pause(args.job, args.command == "pause")
    elif args.command == "retry-failed":
        result = {"queued": store.retry_failed(args.job)}
    elif args.command == "export":
        from .export import export

        fid = args.freeze or store.latest_freeze()["freeze_id"]
        result = export(store, fid, args.output, args.job)
    elif args.command == "serve":
        if args.host not in {"127.0.0.1", "localhost", "::1"} and not os.getenv(
            "LULU_API_TOKEN"
        ):
            p.error("Non-localhost serving requires LULU_API_TOKEN")
        import uvicorn

        uvicorn.run("lulu.api:app", host=args.host, port=args.port)
        return
    print(json.dumps(result, ensure_ascii=False, default=str, indent=2))


if __name__ == "__main__":
    main()
