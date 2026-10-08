import pytest

from lulu.core import digest, file_sha
from lulu.remediation import import_media
from lulu.worker import run


@pytest.fixture
def receipt(facts, tmp_path):
    path = tmp_path / "repair.png"
    path.write_bytes(
        __import__("pathlib").Path(facts["media_candidates"][0]["path"]).read_bytes()
    )
    sha = file_sha(path)
    return {
        "base_facts_sha": digest(facts),
        "source_sku_id": "sku-1",
        "physical_signature": digest(facts["source_skus"][0]["source_options"]),
        "source_image_url": facts["source_skus"][0]["source_image_url"],
        "route": "ORIGINAL_DIRECT_PASS",
        "repair_receipt_id": "download-original-1",
        "path": str(path),
        "sha256": sha,
        "source_sha256": sha,
        "qa": {
            "receipt_id": "independent-inspection-1",
            "verdict": "PASS",
            "same_product_identity": True,
            "package_print_preserved": True,
            "external_commercial_text_remaining": False,
            "output_sha256": sha,
            "version": "lulu-media-qa-v1",
        },
    }


def test_media_receipt_intake_idempotency(store, frozen, facts, tmp_path, receipt):
    result = import_media(store, frozen, facts["product_id"], receipt, tmp_path)
    assert import_media(store, frozen, facts["product_id"], receipt, tmp_path) == result
    run(store, result["job_id"], root=tmp_path / "out")
    row = store.product(frozen, facts["product_id"])
    assert row["facts_sha"] == digest(facts)
    assert row["result"]["status"] == "CONTENT_READY"
    assert row["result"]["media"][0]["route"] == "ORIGINAL_DIRECT_PASS"
    assert result["shopify_writes"] == 0


@pytest.mark.parametrize(
    "change,expected",
    [
        ("base", "MEDIA_SOURCE_CHANGED"),
        ("axes", "SKU_PHYSICAL_AXES_CHANGED"),
        ("sku", "EXACT_SKU_BINDING_REQUIRED"),
        ("file", "MEDIA_QA_FILE_HASH_MISMATCH"),
        ("selfqa", "INDEPENDENT_QA_RECEIPT_REQUIRED"),
        ("identity", "MEDIA_VISUAL_QA_NOT_PASS"),
        ("route", "UNSUPPORTED_MEDIA_REPAIR_ROUTE"),
    ],
)
def test_repair_intake_rejects_incompatible_evidence(
    store, frozen, facts, tmp_path, receipt, change, expected
):
    if change == "base":
        receipt["base_facts_sha"] = "stale"
    elif change == "axes":
        receipt["physical_signature"] = "other"
    elif change == "sku":
        receipt["source_sku_id"] = "other"
    elif change == "file":
        receipt["qa"]["output_sha256"] = "other"
    elif change == "selfqa":
        receipt["qa"]["receipt_id"] = receipt["repair_receipt_id"]
    elif change == "identity":
        receipt["qa"]["same_product_identity"] = False
    elif change == "route":
        receipt["route"] = "LOCAL_CROP"
    with pytest.raises(ValueError, match=expected):
        import_media(store, frozen, facts["product_id"], receipt, tmp_path)
