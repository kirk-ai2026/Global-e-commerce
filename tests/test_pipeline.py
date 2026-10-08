from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from lulu.api import create_app
from lulu.channel import preflight
from lulu.content import compose, sku_label
from lulu.core import digest
from lulu.worker import build, run


def test_fact_content_is_simplified_and_claims_omitted(facts, tmp_path):
    facts["title"] = "【自營】測試品牌 抹茶餅乾 100g"
    r = build(facts, tmp_path / "out")
    assert r["status"] == "CONTENT_READY"
    assert "自营" not in r["content"]["title"]
    assert "饼干" in r["content"]["title"]
    assert "治疗" not in r["content"]["description_html"]
    assert r["retail_price"] is None
    assert r["preflight"]["payload"] is None


def test_partial_sku_does_not_block_valid_sku(facts, tmp_path):
    second = deepcopy(facts["source_skus"][0])
    second["source_sku_id"] = "sku-2"
    second["source_options"][0]["value"] = "草莓味 200g"
    facts["source_skus"].append(second)
    r = build(facts, tmp_path)
    assert r["status"] == "CONTENT_READY"
    assert [s["source_sku_id"] for s in r["content_skus"]] == ["sku-1"]
    assert list(r["content"]["sku_labels"]) == ["sku-1"]
    assert r["withheld_skus"][0]["source_sku_id"] == "sku-2"


@pytest.mark.parametrize(
    "change,expected",
    [
        ("sku", "MEDIA_SKU_MISMATCH"),
        ("url", "MEDIA_SOURCE_BINDING_CHANGED"),
        ("cluster", "MEDIA_CLUSTER_CHANGED"),
        ("hash", "MEDIA_FILE_HASH_MISMATCH"),
        ("qa", "MEDIA_QA_SUPERSEDED_OR_REJECTED"),
        ("missing", "MEDIA_FILE_MISSING"),
        ("axes", "MEDIA_PHYSICAL_AXES_UNPROVEN_OR_CHANGED"),
    ],
)
def test_media_reuse_guards(facts, tmp_path, change, expected):
    m = facts["media_candidates"][0]
    if change == "sku":
        m["source_sku_id"] = "different"
    elif change == "url":
        m["source_image_url"] = "https://img.alicdn.com/different.png"
    elif change == "cluster":
        m["cluster_id"] = "different"
    elif change == "hash":
        m["sha256"] = "0" * 64
    elif change == "qa":
        m["latest_audit"]["verdict"] = "HOLD"
    elif change == "missing":
        m["path"] = str(tmp_path / "missing.png")
    elif change == "axes":
        m["physical_signature"] = "different-size"
    r = build(facts, tmp_path / "out")
    assert r["status"] == "NEEDS_EVIDENCE"
    assert expected in r["withheld_skus"][0]["reasons"]


def test_scope_review_not_publishable(facts, tmp_path):
    facts["scope_status"] = "SCOPE_REVIEW"
    facts["scope_reasons"] = ["TMALL_GLOBAL_CHANNEL_UNVERIFIED"]
    r = build(facts, tmp_path)
    assert r["status"] == "NEEDS_EVIDENCE"
    assert r["preflight"]["payload"] is None


def test_owned_media_survives_legacy_project_removal(facts, tmp_path):
    from pathlib import Path

    root = tmp_path / "lulu"
    first = build(facts, root)
    Path(facts["media_candidates"][0]["path"]).unlink()
    second = build(facts, root)
    assert first["status"] == second["status"] == "CONTENT_READY"
    assert first["media"][0]["sha256"] == second["media"][0]["sha256"]


def test_explicit_delisting_excluded(facts, tmp_path):
    facts["scope_reasons"] = ["EXPLICIT_UNAVAILABLE"]
    assert build(facts, tmp_path)["status"] == "EXCLUDED"


def test_no_fabricated_fallback_sku(facts, tmp_path):
    facts["source_skus"] = []
    r = build(facts, tmp_path)
    assert not r["content_skus"] and r["status"] == "NEEDS_EVIDENCE"


def test_multilanguage_does_not_change_source_identity(facts, tmp_path):
    before = digest(facts)
    s = deepcopy(facts["source_skus"][0])
    s["consumer_label"] = sku_label(s)
    with pytest.raises(ValueError, match="LOCALE_NOT_ENABLED"):
        compose(facts, [s], "en")
    content = compose(facts, [s])
    assert content["locale"] == "zh-Hans"
    assert digest(facts) == before


def test_price_only_change_reuses_content_stage(store, facts, tmp_path):
    first = build(facts, tmp_path, store)
    changed = deepcopy(facts)
    changed["source_skus"][0]["source_price_cny"] = "25.00"
    changed["evidence_id"] = "new-price-observation"
    second = build(changed, tmp_path, store)
    assert first["content_fingerprint"] == second["content_fingerprint"]
    assert second["content_cache_hit"]
    assert first["content"]["description_html"] == second["content"]["description_html"]
    assert second["content"]["evidence_refs"][0] == "new-price-observation"


def test_multiple_sizes_not_asserted_as_single_title(facts):
    a = deepcopy(facts["source_skus"][0])
    a["consumer_label"] = sku_label(a)
    b = deepcopy(a)
    b["source_sku_id"] = "sku-2"
    b["source_options"][0]["value"] = "草莓味 200g"
    b["consumer_label"] = sku_label(b)
    result = compose(facts, [a, b])
    assert "100g" not in result["title"]
    assert (
        "100g" in result["sku_labels"]["sku-1"]
        and "200g" in result["sku_labels"]["sku-2"]
    )


def test_html_and_source_injection_escaped(facts, tmp_path):
    facts["title"] = "<script>alert(1)</script> 测试饼干 100g"
    r = build(facts, tmp_path)
    assert "<script>" not in r["content"]["description_html"]


def test_preflight_no_zero_price_and_no_writer(facts, tmp_path):
    r = build(facts, tmp_path)
    report = r["preflight"]
    assert report["payload"] is None and report["shopify_writes"] == 0
    assert "price" not in report["mapping_preview"]["variants"][0]
    assert "FULFILLMENT_NOT_CONFIGURED" in report["blockers"]
    pricing = {
        "sku-1": {
            "amount": "9.90",
            "currency": "CAD",
            "source_price_cny": "20.00",
            "source_evidence_id": "source-1",
            "fx_version": "fx",
            "weight_evidence": "weight",
            "procurement_evidence_ref": "source-1",
        }
    }
    policy = {
        k: True
        for k in [
            "fulfillment_ready",
            "market_approved",
            "inventory_policy_ready",
            "media_rights_approved",
            "current_procurement_verified",
        ]
    }
    policy["pricing_version"] = "cad-v1"
    r["media"][0]["url"] = "https://assets.example.test/image.png"
    good = preflight(r, pricing, policy)
    assert good["status"] == "CHANNEL_READY"
    pricing["sku-1"]["source_evidence_id"] = "stale"
    assert "PRICE_SOURCE_MISMATCH:sku-1" in preflight(r, pricing, policy)["blockers"]


def test_pg_job_idempotency_pause_and_resume(store, frozen, tmp_path):
    jid = store.create_job(frozen)
    assert store.create_job(frozen) == jid
    store.pause(jid, True)
    assert store.claim(jid, "worker") == []
    store.pause(jid, False)
    counts = run(store, jid, root=tmp_path / "out")
    assert counts["processed"] == 1 and store.job(jid)["status"] == "COMPLETE"
    assert run(store, jid, root=tmp_path / "out")["processed"] == 0
    with store.connect() as c:
        assert c.execute("SELECT count(*) n FROM lulu_receipt").fetchone()["n"] == 1


def test_expired_lease_recovery_and_stale_writer_rejected(store, frozen, tmp_path):
    jid = store.create_job(frozen)
    first = store.claim(jid, "first")[0]
    with store.connect() as c:
        c.execute("UPDATE lulu_task SET lease_until=now()-interval '1 minute'")
    second = store.claim(jid, "second")[0]
    r = build(store.product(frozen, first["product_id"])["facts"], tmp_path)
    with pytest.raises(ValueError, match="LEASE_LOST"):
        store.finish(first, r, "first")
    store.finish(second, r, "second")
    assert store.job(jid)["status"] == "COMPLETE"


def test_evidence_immutable(store):
    e = {"evidence_id": "test-1", "product_id": "1", "raw": {"price": 10}}
    store.import_freeze({"v": 1}, {}, [], [e])
    with pytest.raises(ValueError, match="IMMUTABLE_EVIDENCE_CONFLICT"):
        store.import_freeze({"v": 2}, {}, [], [{**e, "raw": {"price": 20}}])


def test_api_contract_authorization_and_no_search(store, frozen, tmp_path, monkeypatch):
    jid = store.create_job(frozen)
    run(store, jid, root=tmp_path / "assets")
    app = create_app(store, tmp_path / "assets")
    with TestClient(app) as client:
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/v1/products/{pid}/media-evidence" in schema.json()["paths"]
        created = client.post("/v1/jobs", json={"freeze_id": frozen})
        assert created.status_code == 202 and created.json()["job_id"] == jid
        assert client.post("/v1/jobs", json={"limit": 0}).status_code == 422
        assert client.get("/health").status_code == 200
        assert client.get("/v1/products?locale=en").status_code == 422
        row = client.get("/v1/products").json()["items"][0]
        assert row["status"] == "CONTENT_READY"
        assert client.get("/v1/products/not-found").status_code == 404
        assert (
            client.get("/v1/products/" + row["product_id"] + "/preflight").json()[
                "payload"
            ]
            is None
        )
        assert client.get("/assets/../core.py").status_code == 404
        assert client.post("/v1/search", json={"query": "饼干"}).status_code == 404
        monkeypatch.setenv("LULU_API_TOKEN", "test-secret")
        assert client.get("/v1/audit").status_code == 401
        assert (
            client.get(
                "/v1/audit", headers={"Authorization": "Bearer test-secret"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/v1/issues", headers={"Authorization": "Bearer test-secret"}
            ).status_code
            == 200
        )
