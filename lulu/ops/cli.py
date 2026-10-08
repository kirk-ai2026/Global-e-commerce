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
    args = p.parse_args()
    repo = OpsRepository()
    if args.cmd == "migrate":
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
