"""Integration invariants use only a newly created disposable Lulu test schema."""

import copy
import uuid

import psycopg
import pytest
from test_ops import FX, sample

from lulu.core import database_url
from lulu.ops.projection import watermark
from lulu.ops.repository import OpsRepository


@pytest.fixture
def repo():
    try:
        with psycopg.connect(database_url(), connect_timeout=2):
            pass
    except psycopg.Error:
        pytest.skip("local independent PostgreSQL unavailable")
    r = OpsRepository(schema="lulu_ops_test_" + uuid.uuid4().hex)
    r.migrate()
    try:
        yield r
    finally:
        with psycopg.connect(r.dsn) as c:
            c.execute('DROP SCHEMA "' + r.schema + '" CASCADE')


def record():
    row = sample()
    return {
        k: row[k]
        for k in [
            "cluster_id",
            "source",
            "localized",
            "brand_fact",
            "category",
            "scope",
        ]
    }


def test_reimport_preserves_edit_and_source_change_requires_reconfirmation(repo):
    first = record()
    first["source"]["captured_at"] = "t1"
    first["source"]["media_downloads"] = 4
    repo.import_batch("f1", {}, {}, [first])
    repo.save_fx(FX)
    row = repo.get("sample")
    sid = row["snapshot_id"]
    repo.mutate(
        "sample",
        watermark(row, FX),
        watermark,
        patch={"content_override": {"seo_title": "own edit"}},
    )
    second = copy.deepcopy(first)
    second["source"]["captured_at"] = "t2"
    second["source"]["media_downloads"] = 0
    repo.import_batch("f2", {}, {}, [second])
    assert repo.get("sample")["snapshot_id"] == sid
    assert repo.get("sample")["patch"]["content_override"]["seo_title"] == "own edit"
    changed = copy.deepcopy(second)
    changed["source"]["source_sha"] = "new"
    repo.import_batch("f3", {}, {}, [changed])
    row = repo.get("sample")
    assert row["status"] == "RECONFIRM_REQUIRED" and row["source_changed"]
    assert row["patch"]["content_override"]["seo_title"] == "own edit"
    with pytest.raises(ValueError):
        repo.mutate("sample", "old-watermark", watermark, decision="APPROVE")


def test_fx_invalidates_approved_version(repo):
    repo.import_batch("f1", {}, {}, [record()])
    repo.save_fx(FX)
    row = repo.get("sample")
    repo.mutate("sample", watermark(row, FX), watermark, decision="APPROVE")
    repo.save_fx(FX)
    assert repo.get("sample")["status"] == "OPS_APPROVED"
    repo.save_fx({**FX, "fx_id": "fx2", "date": "2026-10-08", "cad_per_cny": "0.2130"})
    assert repo.get("sample")["status"] == "RECONFIRM_REQUIRED"


def test_brand_evidence_refresh_changes_scope_without_replacing_edits(repo):
    first = record()
    first["scope"] = "REVIEW"
    first["brand_fact"] = {"status": "REVIEW"}
    repo.import_batch("f1", {}, {}, [first])
    old = repo.get("sample")["snapshot_id"]
    repo.import_batch("f2", {}, {}, [record()])
    row = repo.get("sample")
    assert row["scope"] == "INCLUDED" and row["status"] == "OPS_REVIEW"
    assert row["snapshot_id"] != old
