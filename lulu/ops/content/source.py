"""Import exact frozen evidence using SELECT-only source SQL and GCS reads."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote, urlsplit

from PIL import Image

from lulu.core import digest


def gcloud(*args):
    value = subprocess.run(
        ["gcloud", *args, "--project=zenheart", "--quiet"],
        capture_output=True,
        check=True,
    )
    return value.stdout.decode().strip()


class FrozenSourceReader:
    def __enter__(self):
        from google.cloud import storage
        from google.cloud.sql.connector import Connector
        from google.oauth2.credentials import Credentials

        credentials = Credentials(gcloud("auth", "print-access-token"))
        dsn = urlsplit(
            gcloud(
                "secrets",
                "versions",
                "access",
                "latest",
                "--secret=zenheart-database-url",
            )
        )
        self.connector = Connector(credentials=credentials, timeout=60)
        self.connection = self.connector.connect(
            "zenheart:asia-east1:zenheart-pg",
            "pg8000",
            user=unquote(dsn.username),
            password=unquote(dsn.password),
            db=dsn.path.lstrip("/"),
        )
        self.cursor = self.connection.cursor()
        self.cursor.execute("SET TRANSACTION READ ONLY")
        self.bucket = storage.Client(
            project="zenheart", credentials=credentials
        ).bucket("zenheart")
        return self

    def __exit__(self, *args):
        self.connection.rollback()
        self.connection.close()
        self.connector.close()

    def objects(self, ids):
        self.cursor.execute(
            "SELECT object_id,object_type,cluster_id,payload,payload_sha256 FROM wujie_merch_shadow.merch_v3_object WHERE object_id=ANY(%s)",
            (ids,),
        )
        return {
            row[0]: {
                "object_type": row[1],
                "cluster_id": row[2],
                "payload": row[3],
                "payload_sha": row[4],
            }
            for row in self.cursor.fetchall()
        }

    def read_image(self, key):
        prefix = "merch-media/shadow/"
        if key.startswith(prefix):
            full = key
        else:
            full = prefix + key
        if not full.startswith(
            prefix + "shadow-evidence/source/"
        ) or ".." in full.split("/"):
            raise ValueError("SOURCE_OBJECT_PREFIX_FORBIDDEN")
        return self.bucket.blob(full).download_as_bytes()


def import_evidence(repo, content, assets, reader, *, clusters=None, workers=8):
    rows = [
        r
        for r in repo.all_listings()
        if r["scope"] == "INCLUDED" and (not clusters or r["cluster_id"] in clusters)
    ]
    manifests = {
        r["cluster_id"]: r["source"]["upstream"]["media_workspace"]["current_manifest"]
        for r in rows
    }
    ids = list(
        {
            str(m.get(k))
            for m in manifests.values()
            for k in ["evidence_bundle_object_id", "source_snapshot_object_id"]
            if m.get(k)
        }
    )
    objects = reader.objects(ids)
    targets = []
    results = {}
    errors = []
    for row in rows:
        cluster = row["cluster_id"]
        m = manifests[cluster]
        obj = objects.get(m.get("evidence_bundle_object_id"))
        if (
            not obj
            or obj["object_type"] != "UNIVERSAL_MERCH_EVIDENCE"
            or obj["cluster_id"] != cluster
        ):
            errors.append(
                {"cluster_id": cluster, "reason": "EXACT_EVIDENCE_OBJECT_MISSING"}
            )
            continue
        bundle = copy.deepcopy(obj["payload"])
        if bundle.get("source_snapshot_id") != m.get("source_snapshot_id"):
            errors.append({"cluster_id": cluster, "reason": "FROZEN_SNAPSHOT_MISMATCH"})
            continue
        snapshot = objects.get(m.get("source_snapshot_object_id"))
        bundle["lulu_source_provenance"] = {
            "evidence_object_id": m["evidence_bundle_object_id"],
            "evidence_payload_sha": obj["payload_sha"],
            "source_snapshot_object_id": m.get("source_snapshot_object_id"),
            "source_snapshot": snapshot["payload"]
            if snapshot and snapshot["cluster_id"] == cluster
            else None,
            "upstream_writes": 0,
        }
        results[cluster] = {"bundle": bundle, "assets": {}, "failures": []}
        for image in bundle.get("image_assets") or []:
            targets.append((cluster, image))

    def download(target):
        cluster, image = target
        asset_id = str(image["asset_id"])
        expected = str(image.get("source_sha256") or "")
        existing = repo.media(expected) if expected else None
        if existing:
            raw = assets.read(existing["object_key"])
        else:
            raw = reader.read_image(str(image.get("source_object") or ""))
        actual = hashlib.sha256(raw).hexdigest()
        if not expected or actual != expected:
            raise ValueError("SOURCE_IMAGE_HASH_MISMATCH")
        with Image.open(io.BytesIO(raw)) as im:
            width, height = im.size
            mime = Image.MIME.get(im.format, "image/jpeg")
            im.verify()
        sha, key = assets.put(raw, mime)
        repo.save_media(
            sha,
            key,
            mime,
            len(raw),
            {
                "kind": "ORIGINAL_EVIDENCE",
                "cluster_id": cluster,
                "asset_id": asset_id,
                "source_object": image.get("source_object"),
                "source_sha256": expected,
                "upstream_writes": 0,
            },
        )
        return (
            cluster,
            asset_id,
            {
                "asset_id": asset_id,
                "sha256": sha,
                "url": "/api/media/" + sha,
                "object_key": key,
                "width": width,
                "height": height,
                "content_type": mime,
                "bytes": len(raw),
            },
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download, t): t for t in targets}
        completed = 0
        for future in as_completed(futures):
            cluster, image = futures[future]
            try:
                c, aid, owned = future.result()
                results[c]["assets"][aid] = owned
            except Exception as exc:
                results[cluster]["failures"].append(
                    {
                        "asset_id": image.get("asset_id"),
                        "reason": type(exc).__name__ + ":" + str(exc)[:100],
                    }
                )
            completed += 1
            if completed % 25 == 0:
                print(
                    json.dumps(
                        {
                            "stage": "source_images",
                            "complete": completed,
                            "total": len(targets),
                        }
                    ),
                    flush=True,
                )
    for cluster, value in results.items():
        bundle = value["bundle"]
        coverage = {
            **(bundle.get("evidence_coverage") or {}),
            "owned_originals": len(value["assets"]),
            "original_sync_failures": value["failures"],
            "original_sha_check": "PASS" if not value["failures"] else "PARTIAL",
        }
        eid = content.save_evidence(
            cluster, bundle["source_snapshot_id"], bundle, value["assets"], coverage
        )
        print(
            json.dumps(
                {
                    "cluster_id": cluster,
                    "evidence_id": eid,
                    "ocr_regions": len(bundle.get("ocr_regions") or []),
                    "images": len(value["assets"]),
                    "failures": len(value["failures"]),
                }
            ),
            flush=True,
        )
    return {
        "products": len(results),
        "errors": errors,
        "source_images": len(targets),
        "owned_images": sum(len(x["assets"]) for x in results.values()),
        "image_failures": sum(len(x["failures"]) for x in results.values()),
        "upstream_writes": 0,
        "paid_calls": 0,
        "evidence_population_sha": digest(sorted(results)),
    }
