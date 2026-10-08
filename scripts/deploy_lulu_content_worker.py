"""Provision only the isolated Lulu worker and its own scoped credentials."""

import json

from lulu.ops.content.provider import settings
from scripts.provision_lulu_ops import exists, gc

PROJECT = "zenheart"
SA = "lulu-merch-content-worker@zenheart.iam.gserviceaccount.com"


def main():
    if not exists("iam", "service-accounts", "describe", SA):
        gc(
            "iam",
            "service-accounts",
            "create",
            "lulu-merch-content-worker",
            "--display-name=Lulu source-grounded content worker",
        )
    gc(
        "projects",
        "add-iam-policy-binding",
        PROJECT,
        "--member=serviceAccount:" + SA,
        "--role=roles/cloudsql.client",
        "--condition=None",
    )
    gc(
        "storage",
        "buckets",
        "add-iam-policy-binding",
        "gs://zenheart-lulu-merch-ops",
        "--member=serviceAccount:" + SA,
        "--role=roles/storage.objectUser",
    )
    for name in ["lulu-ops-database-url", "lulu-content-openai-key"]:
        if not exists("secrets", "describe", name):
            if name != "lulu-content-openai-key":
                raise ValueError("OWN_DATABASE_SECRET_MISSING")
            gc("secrets", "create", name, "--replication-policy=automatic")
            gc(
                "secrets",
                "versions",
                "add",
                name,
                "--data-file=-",
                stdin=settings()["api_key"].encode(),
            )
        gc(
            "secrets",
            "add-iam-policy-binding",
            name,
            "--member=serviceAccount:" + SA,
            "--role=roles/secretmanager.secretAccessor",
        )
    print(
        json.dumps(
            {
                "worker_service_account": SA,
                "own_db_secret": True,
                "own_bucket_access": True,
                "upstream_access": False,
            }
        )
    )


if __name__ == "__main__":
    main()
