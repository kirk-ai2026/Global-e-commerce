from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path


class AssetStore:
    def __init__(self, root=None, bucket=None):
        self.root = Path(
            root or os.getenv("LULU_OPS_DATA_DIR", "data/cloud_ops")
        ).resolve()
        self.bucket_name = bucket or os.getenv("LULU_OPS_BUCKET")
        self._bucket = None

    @property
    def bucket(self):
        if self._bucket is None and self.bucket_name:
            from google.cloud import storage

            self._bucket = storage.Client().bucket(self.bucket_name)
        return self._bucket

    def put(self, data, content_type):
        sha = hashlib.sha256(data).hexdigest()
        key = "lulu/media/" + sha
        if self.bucket_name:
            blob = self.bucket.blob(key)
            if not blob.exists():
                blob.upload_from_string(
                    data, content_type=content_type, if_generation_match=0
                )
        else:
            path = self.root / key
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                temp = path.with_suffix("." + uuid.uuid4().hex + ".tmp")
                temp.write_bytes(data)
                temp.replace(path)
        return sha, key

    def read(self, key):
        if not key.startswith("lulu/media/") or ".." in key.split("/"):
            raise ValueError("INVALID_LULU_ASSET_KEY")
        if self.bucket_name:
            return self.bucket.blob(key).download_as_bytes()
        return (self.root / key).read_bytes()
