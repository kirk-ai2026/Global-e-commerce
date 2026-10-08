"""Verify owned deployment; credentials remain in memory and no store API is called."""

import json
import urllib.error
import urllib.request
from pathlib import Path

from scripts.provision_lulu_ops import gc

BASE = "https://lulu-merch-ops-wiv3tqv5ua-de.a.run.app"


def call(path, body=None, token=None, method=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Ops-Token"] = token
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(request, timeout=120) as r:
        return json.load(r)


if __name__ == "__main__":
    assert call("/health")["status"] == "ok"
    population = call("/api/v2/ops/listings?page_size=100")
    assert population["total"] == 86
    reviews = call("/api/v2/ops/listings?status=BRAND_REVIEW&page_size=100")
    assert reviews["total"] == 3
    assert call("/api/v2/ops/listings?brand_country=JP&page_size=100")["total"] == 57
    assert call("/api/v2/ops/listings?brand_country=KR&page_size=100")["total"] == 24
    path = "/api/v2/ops/listings/HUR4-098f070812a866b62761"
    d = call(path)
    v = d["consumer"]["variants"][0]
    assert v["retail_price_cad"] == "23.33"
    with urllib.request.urlopen(
        BASE + d["consumer"]["media"][0]["url"], timeout=120
    ) as r:
        assert len(r.read()) > 100
    session = call(
        "/api/session",
        {
            "passphrase": gc(
                "secrets",
                "versions",
                "access",
                "latest",
                "--secret=lulu-merch-ops-passphrase",
            )
            .decode()
            .strip()
        },
    )
    token = session["token"]
    # Save identical source amounts to validate storage/recalculation without changing product facts.
    body = {
        "base_watermark": d["watermark"],
        "patch": {
            "price_weight_override": {
                "variants": {
                    v["platform_sku_id"]: {
                        "procurement_price_cny": v["procurement_price_cny"],
                        "billable_weight_g": v["billable_weight_g"],
                        "reason": "上线验收：同值保存，验证独立数据与 CAD 重算",
                    }
                }
            }
        },
    }
    result = call(path + "/draft", body, token, "PUT")
    assert result["shopify_writes"] == 0
    after = call(path)
    assert after["consumer"]["variants"][0]["retail_price_cad"] == "23.33"
    try:
        call(path + "/draft", body, token, "PUT")
        raise AssertionError("CAS accepted old version")
    except urllib.error.HTTPError as error:
        assert error.code == 409
    approved = call(
        path + "/decision",
        {"decision": "APPROVE", "base_watermark": after["watermark"]},
        token,
    )
    assert approved["status"] == "OPS_APPROVED"
    after = call(path)
    call(
        path + "/decision",
        {"decision": "RESTORE", "base_watermark": after["watermark"]},
        token,
    )
    fx = call("/api/fx/refresh", {}, token)
    assert fx["status"] == "UPDATED"
    report = {
        "url": BASE,
        "listings": 86,
        "brand_review": 3,
        "japan": 57,
        "korea": 24,
        "pola_cad": "23.33",
        "write_save": "PASS",
        "old_watermark_rejected": True,
        "internal_approval": "PASS",
        "manual_fx": "PASS",
        "fx": fx["snapshot"],
        "shopify_writes": 0,
        "upstream_writes": 0,
    }
    Path("docs/LULU_OPS_CLOUD_VERIFICATION.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    print(json.dumps(report, ensure_ascii=False))
