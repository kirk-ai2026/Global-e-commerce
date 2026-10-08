import copy

import pytest
from fastapi.testclient import TestClient

from lulu.ops.api import create_app
from lulu.ops.localization import localize, safe_html
from lulu.ops.money import quote
from lulu.ops.projection import effective, watermark
from lulu.ops.repository import OpsRepository

FX = {"fx_id": "fx-1", "date": "2026-10-07", "cad_per_cny": "0.2126"}


def sample():
    d = {
        "listing": {"cluster_id": "sample", "status": "LIVE"},
        "consumer": {
            "title": "花香沐浴乳 500ml",
            "brand": "POLA",
            "description_html": "<p>產地：日本；容量 500ml。</p><p>臺灣市場售價 NT$ 699。</p>",
            "variants": [
                {
                    "platform_sku_id": "sku1",
                    "variant_key": "key1",
                    "title": "玫瑰 500ml",
                    "options": {"香味": "玫瑰"},
                }
            ],
            "media": [{"asset_id": "a", "url": "/api/media/a", "role": "PRIMARY"}],
        },
        "procurement": {
            "variants": [
                {
                    "platform_sku_id": "sku1",
                    "procurement_price_cny": "80",
                    "billable_weight_g": "778.75",
                }
            ],
            "withheld_variants": [{"platform_sku_id": "sku2", "reason": "待補圖"}],
        },
        "readiness": {"eligible": True, "hard_blockers": []},
    }
    return {
        "cluster_id": "sample",
        "snapshot_id": "s1",
        "brand_fact": {"code": "JP"},
        "category": "beauty",
        "scope": "INCLUDED",
        "localized": localize(d),
        "patch": {},
        "status": "OPS_REVIEW",
        "source_changed": False,
        "updated_at": "today",
        "source": {
            "derived": {"detail": d},
            "upstream": {"media_workspace": {}},
            "source_sha": "sha",
        },
    }


def test_pola_decimal_and_missing():
    assert (
        quote({"procurement_price_cny": "80", "billable_weight_g": "778.75"}, FX)[
            "retail_price_cad"
        ]
        == "23.33"
    )
    assert (
        quote({"procurement_price_cny": "80", "predicted_weight_p75_g": "778.75"}, FX)[
            "weight_basis"
        ]
        == "SAME_SKU_P75"
    )
    for v, f in [
        ({}, FX),
        ({"procurement_price_cny": 80, "billable_weight_g": 778.75}, None),
        ({"procurement_price_cny": "NaN", "billable_weight_g": 20}, FX),
    ]:
        assert quote(v, f)["retail_price_cad"] is None


def test_localization_and_exact_subset():
    row = sample()
    d = effective(row, FX)
    assert d["consumer"]["title"] == "花香沐浴露 500ml"
    assert "台湾市场" not in d["consumer"]["description_html"]
    assert len(d["consumer"]["variants"]) == 1
    assert d["procurement"]["withheld_variants"][0]["platform_sku_id"] == "sku2"
    assert d["consumer"]["variants"][0]["retail_price_cad"] == "23.33"
    assert d["listing"]["status"] == "OPS_REVIEW"
    assert d["source_audit"]["upstream_status"] == "LIVE"
    assert d["manifest"]["locale"] == "zh-Hans"
    assert "script" not in safe_html('<script>alert(1)</script><p onclick="x">ok</p>')


def test_independent_patch_and_watermark():
    row = sample()
    before = copy.deepcopy(row["source"])
    row["patch"] = {
        "price_weight_override": {
            "variants": {"sku1": {"procurement_price_cny": "90", "reason": "new"}}
        }
    }
    assert effective(row, FX)["consumer"]["variants"][0]["retail_price_cad"] == "25.99"
    assert row["source"] == before
    assert watermark(row, FX) != watermark(row, {**FX, "fx_id": "fx2"})
    with pytest.raises(ValueError):
        OpsRepository(schema="wujie_merch_shadow")


class Repo:
    def __init__(self):
        self.row = sample()

    def get(self, c):
        return self.row if c == "sample" else None

    def current_fx(self):
        return FX

    def mutate(self, c, expected, wm, **kw):
        if expected != wm(self.row, FX):
            raise ValueError("SOURCE_CHANGED_REVIEW")
        self.row["patch"] = kw.get("patch") or self.row["patch"]
        return "OPS_REVIEW"


def test_auth_cas_and_no_store_action():
    repo = Repo()
    client = TestClient(create_app(repo=repo, passphrase="test"))
    d = client.get("/api/v2/ops/listings/sample").json()
    body = {
        "base_watermark": d["watermark"],
        "patch": {
            "price_weight_override": {
                "variants": {
                    "sku1": {
                        "procurement_price_cny": "90",
                        "reason": "fresh observation",
                    }
                }
            }
        },
    }
    assert client.put("/api/v2/ops/listings/sample/draft", json=body).status_code == 401
    token = client.post("/api/session", json={"passphrase": "test"}).json()["token"]
    headers = {"X-Ops-Token": token}
    assert (
        client.put(
            "/api/v2/ops/listings/sample/draft", json=body, headers=headers
        ).status_code
        == 200
    )
    assert (
        client.put(
            "/api/v2/ops/listings/sample/draft", json=body, headers=headers
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v2/ops/listings/sample/decision",
            json={"decision": "PUBLISH"},
            headers=headers,
        ).status_code
        == 422
    )
    body["patch"]["price_weight_override"]["variants"] = {
        "wrongsku": {"procurement_price_cny": "90", "reason": "x"}
    }
    assert (
        client.put(
            "/api/v2/ops/listings/sample/draft", json=body, headers=headers
        ).status_code
        == 422
    )


def test_taiwan_market_prose_never_becomes_canadian_evidence():
    row = sample()
    d = row["source"]["derived"]["detail"]
    d["consumer"]["description_html"] = "<p>容量 500ml。</p><p>臺灣消費者熱銷推薦。</p>"
    result = localize(d)
    assert "加拿大" not in result["description_html"]
    assert "台湾消费者" not in result["description_html"]
    assert result["omitted_market_segments"]
