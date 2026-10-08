"""Read deployed content candidates; reject stale activation and unconfirmed retries."""

import json
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from scripts.provision_lulu_ops import gc
from scripts.verify_lulu_cloud_ops import BASE, call

CANARY = [
    "HUR4-098f070812a866b62761",
    "HUR4-12c8008867c1dff09cf8",
    "HUR4-245568018ed8110dca37",
    "HUR4-197f58a861971d35f1af",
    "HUR4-9b939cf0c447092ef091",
    "HUR4-159a8612f5906d004244",
    "HUR4-52ef69b6f21a52bc2571",
    "HUR4-0aed27ebebf987345557",
    "HUR4-5cfbaabe629ed1b71485",
    "HUR4-9eea0e5eb93b2a255520",
]


def main():
    assert call("/health")["status"] == "ok"
    assert call("/api/v2/ops/listings?page_size=100")["total"] == 86
    assert len(call("/api/content/profiles")["items"]) == 3
    report = call("/api/content/report")
    samples = []
    for cluster in CANARY:
        data = call(f"/api/v2/ops/listings/{cluster}/content")
        revision = data["items"][0]
        export = call(
            f"/api/v2/ops/listings/{cluster}/content-candidates/{revision['revision_id']}/export"
        )
        assert export["locale"] == "zh-Hans" and export["market"] == "CA"
        assert export["shopify_writes"] == 0
        assert export["description_html"].count("<figure>") == len(
            export["description_media"]
        )
        for media in export["description_media"]:
            assert media["qa"]["status"] == "PASS"
            assert BASE + "/api/media/" + media["sha256"] in export["description_html"]
        samples.append(
            {
                "cluster_id": cluster,
                "candidate_status": revision["status"],
                "method": revision["method"],
                "revision_id": revision["revision_id"],
                "description_media": len(export["description_media"]),
                "image_edits": export["qa"]["image_edits"],
                "activated": data["active_revision_id"] == revision["revision_id"],
            }
        )
    source = call(f"/api/v2/ops/listings/{CANARY[0]}/source-text")
    assert source["available"] and len(source["ocr_images"]) == 11
    assert sum(len(i["regions"]) for i in source["ocr_images"]) == 115
    assert len(source["owned_images"]) == 11
    pola = call(f"/api/v2/ops/listings/{CANARY[0]}")
    assert pola["consumer"]["variants"][0]["retail_price_cad"] == "23.33"
    first = call(f"/api/v2/ops/listings/{CANARY[0]}/content")["items"][0]
    with urllib.request.urlopen(
        BASE + "/api/media/" + first["body"]["description_media"][0]["sha256"],
        timeout=120,
    ) as response:
        assert response.headers.get_content_type().startswith("image/")
        assert len(response.read()) > 100
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
    for path, body, status in [
        (
            f"/api/v2/ops/listings/{CANARY[0]}/content-candidates/{first['revision_id']}/activate",
            {"base_watermark": "stale-verification-only", "choice": "ACTIVATE_SAFE"},
            409,
        ),
        ("/api/content/provider-resume", {"provider_ready": False}, 422),
    ]:
        try:
            call(path, body, token)
            raise AssertionError("Unsafe verification request accepted")
        except urllib.error.HTTPError as error:
            assert error.code == status
    assert call("/api/content/report")["active"] == report["active"]
    output = {
        "captured_at": datetime.now(UTC).isoformat(),
        "url": BASE,
        "canary_products": len(samples),
        "canary_statuses": dict(Counter(r["candidate_status"] for r in samples)),
        "pola_source_images": 11,
        "pola_ocr_regions": 115,
        "pola_cad": "23.33",
        "registered_images_readable": True,
        "export_preview_assembly_consistent": True,
        "stale_activation_rejected": True,
        "unconfirmed_provider_resume_rejected": True,
        "content_activation_writes": 0,
        "shopify_writes": 0,
        "upstream_writes": 0,
        "current_product_jobs": report["current_product_jobs"],
        "samples": samples,
    }
    Path("docs/CONTENT_CLOUD_VERIFICATION.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2)
    )
    print(
        json.dumps(
            {k: v for k, v in output.items() if k != "samples"}, ensure_ascii=False
        )
    )


if __name__ == "__main__":
    main()
