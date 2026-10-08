import os
import uuid

import psycopg
import pytest
from PIL import Image

from lulu.core import digest, file_sha
from lulu.storage import Store


@pytest.fixture
def store():
    dsn = os.environ.get(
        "LULU_TEST_DATABASE_URL", "postgresql://lulu@127.0.0.1:55438/lulu"
    )
    schema = "lulu_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("CREATE SCHEMA " + schema)
    scoped = Store(
        dsn + ("&" if "?" in dsn else "?") + "options=-csearch_path%3D" + schema
    )
    scoped.migrate()
    yield scoped
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA " + schema + " CASCADE")


@pytest.fixture
def facts(tmp_path):
    p = tmp_path / "original.png"
    Image.new("RGB", (400, 400), "white").save(p)
    sha = file_sha(p)
    sku = {
        "source_sku_id": "sku-1",
        "duplicate_id": False,
        "source_options": [{"token": "1:2", "name": "规格", "value": "抹茶味 100g"}],
        "raw_label": "1:2:规格:抹茶味 100g",
        "property_path": "1:2",
        "source_price_cny": "20.00",
        "availability_at_observation": "AVAILABLE",
        "stock_at_observation": 10,
        "observed_at": "2026-09-09T00:00:00+00:00",
        "source_image_url": "https://img.alicdn.com/sku-1.png",
        "image_binding_method": "EXPLICIT_SKU_IMAGE",
        "raw": {},
    }
    media = {
        "asset_id": "asset-1",
        "cluster_id": "spu-1",
        "source_evidence_id": "image-evidence-1",
        "source_sku_id": "sku-1",
        "source_image_url": sku["source_image_url"],
        "physical_signature": digest(sku["source_options"]),
        "path": str(p),
        "sha256": sha,
        "source_sha256": sha,
        "qa_verdict": "PASS",
        "qa_version": "media-localization-quality-v10-sku-coverage",
        "latest_audit": {
            "verdict": "PASS",
            "audit_json": {
                "same_product_identity": True,
                "package_print_changed": False,
            },
        },
        "rights_status": "UNKNOWN",
    }
    return {
        "product_id": "10000001",
        "global_spu_id": "spu-1",
        "identity_version": "id-v1",
        "scope_status": "INCLUDED",
        "scope_reasons": [],
        "title": "【自营】测试品牌 抹茶饼干 100g",
        "brand": "测试品牌",
        "category": "food",
        "brand_region": {"status": "CONFIRMED", "code": "JP"},
        "channel": {"status": "CONFIRMED", "value": "TMALL_GLOBAL"},
        "observed_at": sku["observed_at"],
        "evidence_id": "source-1",
        "attributes": [
            {"name": "产地", "value": "日本"},
            {"name": "功效", "value": "治疗疾病"},
        ],
        "source_skus": [sku],
        "media_candidates": [media],
        "gallery": [sku["source_image_url"]],
    }


@pytest.fixture
def frozen(store, facts):
    manifest = {"test": digest(facts)}
    fid = store.import_freeze(manifest, {"scope_counts": {"INCLUDED": 1}}, [facts], [])
    return fid
