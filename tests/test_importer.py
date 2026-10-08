import json
import sqlite3

import pytest

from lulu.core import file_sha
from lulu.importer import (
    brand_index,
    category,
    channel,
    latest_observations,
    readonly,
    resolve_brand,
    source_skus,
)


def test_channel_does_not_trust_rewritten_url_or_is_tmall():
    assert (
        channel(
            {"canonical_url": "https://detail.tmall.hk/item.htm?id=1"},
            {"isTmall": True},
        )["status"]
        == "UNKNOWN"
    )
    c = {
        "channel_status": "CONFIRMED",
        "channel_confidence": 1,
        "core_candidate_id": "c1",
        "source_evidence_json": json.dumps(
            {
                "source_channels": ["TMALL_GLOBAL"],
                "channel_reasons": ["DIRECT_TMALL_GLOBAL_COLLECTION"],
            }
        ),
    }
    assert (
        channel(c, {"url": "https://item.taobao.com/item.htm?id=1", "isTmall": False})[
            "status"
        ]
        == "CONFIRMED"
    )


def test_brand_country_exact_matching_conflicts_and_historical_preservation():
    rows = [
        {
            "brand_id": "1",
            "canonical_brand": "花王",
            "brand_country_code": "JP",
            "confidence": 0.99,
            "is_current": False,
        },
        {
            "brand_id": "2",
            "canonical_brand": "花王",
            "brand_country_code": None,
            "confidence": 1,
            "is_current": True,
        },
        {
            "brand_id": "3",
            "canonical_brand": "Nature Made",
            "brand_country_code": "US",
            "confidence": 0.99,
        },
    ]
    index = brand_index(rows)
    assert resolve_brand("花王", index)["code"] == "JP"
    assert resolve_brand("Nature", index)["status"] == "UNKNOWN"
    assert resolve_brand("花王/另一品牌", index)["code"] == "JP"
    rows.append(
        {
            "brand_id": "4",
            "canonical_brand": "花王",
            "brand_country_code": "CN",
            "confidence": 0.99,
        }
    )
    assert resolve_brand("花王", brand_index(rows))["status"] == "CONFLICT"


@pytest.mark.parametrize(
    "major,leaf,title,result",
    [
        ("BEAUTY", "面霜", "", "beauty"),
        ("FOOD", "饼干", "", "food"),
        ("FOOD_HEALTH", "nutrition_supplement", "", "supplements"),
        ("FOOD_HEALTH", "food", "", "food"),
        ("MOTHER_BABY", "baby_food", "", "mother_baby"),
        ("OTHER", "unknown", "", "unknown"),
    ],
)
def test_category_map(major, leaf, title, result):
    assert (
        category({"major_category": major, "leaf_category": leaf}, {}, {"title": title})
        == result
    )


def test_exact_property_image_and_unknown_stock():
    raw = {
        "skus": [
            {
                "skuId": "1",
                "propsIds": "1:2;3:4",
                "propsNames": "1:2:口味:抹茶;3:4:规格:100g",
                "price": "2.5",
            }
        ],
        "propsImages": {
            "1:20": "https://img.test/wrong.png",
            "1:2": "https://img.test/right.png",
        },
    }
    s = source_skus(raw, "2026-09-01")[0]
    assert s["source_image_url"] == "https://img.test/right.png"
    assert s["availability_at_observation"] == "UNKNOWN"
    assert source_skus({"price": 10, "stock": 20}, None) == []


def test_historical_image_binding_rejects_reused_sku_with_changed_capacity(facts):
    from lulu.media import bind_historical_axes

    sku = facts["source_skus"][0]
    candidate = facts["media_candidates"][0]
    old = {
        "skuId": sku["source_sku_id"],
        "propsNames": "1:2:规格:抹茶味 100g",
        "imageUrl": sku["source_image_url"],
        "price": "20",
        "quantity": 10,
    }
    evidence = [{"raw": {"skus": [old]}}]
    assert bind_historical_axes(candidate, sku, evidence) is not None
    newer = {**old, "propsNames": "1:2:规格:抹茶味 200g"}
    evidence.append({"raw": {"skus": [newer]}})
    assert bind_historical_axes(candidate, sku, evidence) is None


def test_readonly_no_sidecar_or_mutation(tmp_path):
    p = tmp_path / "source.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t(v int)")
    c.commit()
    c.close()
    before = file_sha(p)
    with readonly(p) as c:
        with pytest.raises(sqlite3.OperationalError):
            c.execute("INSERT INTO t VALUES(1)")
    assert file_sha(p) == before


def test_latest_success_technical_failure_and_delisting(tmp_path):
    src = tmp_path / "source.sqlite"
    state = tmp_path / "state.sqlite"
    c = sqlite3.connect(src)
    c.execute(
        "CREATE TABLE apify_detail_observations_v1(item_id,raw_json,scraped_at,created_at,observation_id)"
    )
    old = {"itemId": "1", "_detailFetched": True, "skus": [{"skuId": "a"}]}
    c.execute(
        "INSERT INTO apify_detail_observations_v1 VALUES(?,?,?,?,?)",
        ("1", json.dumps(old), "2026-09-01T00:00:00Z", "2026-09-01", "old"),
    )
    c.commit()
    c.close()
    c = sqlite3.connect(state)
    c.execute(
        "CREATE TABLE procurement_refresh_tasks_v2(task_id,platform_product_id,raw_json,result_json,status,change_class,completed_at,updated_at,created_at)"
    )
    c.execute(
        "INSERT INTO procurement_refresh_tasks_v2 VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "error",
            "1",
            "{}",
            "{}",
            "MERGED",
            "TECHNICAL_FAILED",
            "2026-09-02T00:00:00Z",
            None,
            None,
        ),
    )
    c.execute(
        "INSERT INTO procurement_refresh_tasks_v2 VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "gone",
            "1",
            "{}",
            "{}",
            "MERGED",
            "UNAVAILABLE",
            "2026-09-03T00:00:00Z",
            None,
            None,
        ),
    )
    c.commit()
    c.close()
    with readonly(src) as s, readonly(state) as t:
        history, unavailable, evidence = latest_observations(s, t)
    assert history["1"][-1]["raw"] == old
    assert unavailable["1"].startswith("2026-09-03")
    assert len(evidence) == 2
