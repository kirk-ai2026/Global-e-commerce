import copy
import hashlib
import io
import json
import uuid

import psycopg
import pytest
from PIL import Image
from test_ops import FX, sample

from lulu.ops.assets import AssetStore
from lulu.ops.content.copy import render_for, static_qa
from lulu.ops.content.media import (
    assemble_html,
    assess_image,
    external_language_status,
    prepare_media,
    safe_edge_crop_box,
)
from lulu.ops.content.policy import profile, protected_hash
from lulu.ops.content.provider import (
    ProviderBlocked,
    ResponsesProvider,
    UnknownPaidResult,
)
from lulu.ops.content.repository import ContentStore
from lulu.ops.projection import effective, watermark
from lulu.ops.repository import OpsRepository


@pytest.fixture
def content(tmp_path):
    repo = OpsRepository(schema="lulu_content_test_" + uuid.uuid4().hex)
    repo.migrate()
    store = ContentStore(repo)
    store.migrate()
    row = sample()
    row["source"]["upstream"]["media_workspace"] = {
        "source_snapshot_id": "aps1",
        "current_manifest": {"source_snapshot_id": "aps1", "description_sections": []},
    }
    repo.import_batch(
        "f1",
        {},
        {},
        [
            {
                k: row[k]
                for k in [
                    "cluster_id",
                    "source",
                    "localized",
                    "brand_fact",
                    "scope",
                    "category",
                ]
            }
        ],
    )
    repo.save_fx(FX)
    image = Image.new("RGB", (80, 160), "green")
    image.save(raw := io.BytesIO(), "PNG")
    data = raw.getvalue()
    sha = hashlib.sha256(data).hexdigest()
    assets = AssetStore(tmp_path)
    assets.put(data, "image/png")
    repo.save_media(sha, "lulu/media/" + sha, "image/png", len(data), {})
    bundle = {
        "source_snapshot_id": "aps1",
        "structured_facts": [
            {"evidence_id": "ev1", "text": "水润保湿，容量500ml，含量50mg。"}
        ],
        "source_text_blocks": ["<p>水润保湿，容量500ml。</p>"],
        "image_assets": [
            {
                "asset_id": "img1",
                "source_sha256": sha,
                "read_status": "READABLE",
                "ocr_status": "CACHED",
                "role": "DETAIL",
                "visual_facts": {
                    "external_simplified_text": True,
                    "channel_content": False,
                },
            }
        ],
        "ocr_regions": [
            {
                "evidence_id": "ocr1",
                "asset_id": "img1",
                "source_text": "水润保湿",
                "placement": "EXTERNAL",
                "category": "MARKETING",
                "confidence": 0.98,
            }
        ],
    }
    store.save_evidence(
        "sample",
        "aps1",
        bundle,
        {
            "img1": {
                "asset_id": "img1",
                "sha256": sha,
                "object_key": "lulu/media/" + sha,
                "url": "/api/media/" + sha,
                "width": 80,
                "height": 160,
            }
        },
        {},
    )
    try:
        yield repo, store, assets, bundle
    finally:
        with psycopg.connect(repo.dsn) as c:
            c.execute('DROP SCHEMA "' + repo.schema + '" CASCADE')


def candidate(store, repo):
    job = store.create_job(repo.get("sample"))
    body = {
        "description_text_html": "<h2>水润呵护</h2><p>水润保湿，容量500ml。</p>",
        "description_sections": [
            {
                "heading": "水润呵护",
                "html": "<p>水润保湿，容量500ml。</p>",
                "evidence_ids": ["ev1"],
                "table_rows": [],
            }
        ],
        "description_media": [],
        "evidence_sha": "es",
    }
    revision = store.save_revision(
        job, body, {"status": "PASS"}, "SOURCE_GROUNDED_REBUILD"
    )
    return job, revision


def test_product_marketing_retained_without_image_edit_and_strict_profiles(content):
    repo, store, assets, bundle = content
    e = store.evidence("sample")
    m = prepare_media(e, profile(), {"sku1"})
    assert len(m["description_media"]) == 1 and m["image_edits"] == 0
    assert (
        m["description_media"][0]["sha256"]
        == bundle["image_assets"][0]["source_sha256"]
    )
    assert (
        assess_image(
            bundle["image_assets"][0],
            bundle["ocr_regions"],
            profile("tw-zh-Hant-strict-v1"),
            {"sku1"},
        )["status"]
        == "SOURCE_ONLY"
    )
    assert (
        assess_image(
            bundle["image_assets"][0],
            bundle["ocr_regions"],
            profile("en-strict-v1"),
            {"sku1"},
        )["status"]
        == "SOURCE_ONLY"
    )


def test_channel_and_wrong_sku_information_never_enters_description(content):
    *_, bundle = content
    image = bundle["image_assets"][0]
    for extra in [
        {
            "source_text": "天猫旗舰店满减",
            "category": "PLATFORM",
            "placement": "EXTERNAL",
        },
        {
            "source_text": "两瓶装",
            "category": "PRODUCT_FACT",
            "placement": "EXTERNAL",
            "applicable_sku_ids": ["withheld"],
        },
    ]:
        qa = assess_image(
            image,
            bundle["ocr_regions"] + [extra],
            profile(),
            {"sku1"},
            {"sku1", "withheld"},
        )
        assert qa["status"] == "SOURCE_ONLY"


def test_english_gate_covers_non_cjk_scripts_and_unconfirmed_latin_text():
    external = {"placement": "EXTERNAL", "category": "PRODUCT_FACT"}
    for value in ["วิตามิน", "витамины", "保湿", "보습", "保湿成分"]:
        assert (
            external_language_status([{**external, "source_text": value}])
            == "NON_ENGLISH"
        )
    assert (
        external_language_status(
            [{**external, "source_text": "Crème hydratante", "language": "fr"}]
        )
        == "NON_ENGLISH"
    )
    assert (
        external_language_status(
            [{**external, "source_text": "Hydratation quotidienne"}]
        )
        == "UNCONFIRMED"
    )
    assert (
        external_language_status(
            [{**external, "source_text": "Vitamin C 50µg", "language": "en-CA"}]
        )
        == "ENGLISH_OR_NO_EXTERNAL_TEXT"
    )
    assert (
        external_language_status(
            [{**external, "source_text": "วิตามิน", "placement": "PACKAGE"}]
        )
        == "ENGLISH_OR_NO_EXTERNAL_TEXT"
    )


def test_html_assets_are_owned_and_safe(content):
    repo, store, assets, bundle = content
    m = prepare_media(store.evidence("sample"), profile(), {"sku1"})[
        "description_media"
    ]
    m[0]["caption"] = "<img src=x onerror=alert(1)>"
    html = assemble_html("<p>正文</p>", m)
    assert 'src="/api/media/' in html and 'loading="lazy"' in html and "&lt;img" in html
    assert "<img src=x" not in html
    assert (
        assemble_html("<p>正文</p>", [{**m[0], "sha256": "javascript:bad"}])
        == "<p>正文</p>"
    )


def test_price_fx_and_profile_cache_isolation(content):
    repo, store, _, _ = content
    row = repo.get("sample")
    first = store.create_job(row)
    repo.mutate(
        "sample",
        watermark(row, FX),
        watermark,
        patch={
            "price_weight_override": {
                "variants": {"sku1": {"procurement_price_cny": "90", "reason": "test"}}
            }
        },
    )
    row = repo.get("sample")
    assert store.create_job(row)["job_id"] == first["job_id"]
    assert protected_hash(row) == first["protected_sha"]
    assert store.create_job(row, "en-strict-v1")["evidence_id"] == first["evidence_id"]
    assert store.create_job(row, "en-strict-v1")["job_id"] != first["job_id"]


def test_operator_patch_preserved_and_activation_requires_current_watermark(content):
    repo, store, _, _ = content
    job, revision = candidate(store, repo)
    row = repo.get("sample")
    old = watermark(row, FX)
    repo.mutate(
        "sample",
        old,
        watermark,
        patch={"content_override": {"description_html": "<p>运营保留稿500ml。</p>"}},
    )
    with pytest.raises(ValueError, match="SOURCE_CHANGED_REVIEW"):
        store.activate("sample", revision["revision_id"], old, watermark)
    row = repo.get("sample")
    fresh = watermark(row, FX)
    with pytest.raises(ValueError, match="OPERATOR_CONTENT_CONFLICT"):
        store.activate("sample", revision["revision_id"], fresh, watermark)
    store.activate("sample", revision["revision_id"], fresh, watermark, "KEEP_OPERATOR")
    assert not repo.get("sample").get("content_active")
    store.activate(
        "sample", revision["revision_id"], fresh, watermark, "REPLACE_OPERATOR"
    )
    after = repo.get("sample")
    assert after["content_active"]["revision_id"] == revision["revision_id"]
    assert any(x["payload"].get("previous_operator_patch") for x in after["history"])
    assert (
        effective(after, FX)["consumer"]["description_text_html"]
        == revision["body"]["description_text_html"]
    )


def test_unknown_paid_result_is_not_resent(content, monkeypatch):
    repo, store, _, _ = content
    job = store.create_job(repo.get("sample"))
    calls = []

    def timeout(*args, **kwargs):
        calls.append(True)
        raise TimeoutError()

    monkeypatch.setattr("urllib.request.urlopen", timeout)
    provider = ResponsesProvider(
        store,
        {
            "api_key": "test",
            "model": "test-model",
            "vision_model": "vision",
            "reasoning": "medium",
            "base_url": "https://api.openai.com/v1",
        },
    )
    for _ in range(2):
        with pytest.raises(UnknownPaidResult):
            provider.call(job, "COPY", "instructions", {}, {"type": "object"})
    assert len(calls) == 1


def test_credit_failure_requires_explicit_resume_and_keeps_unknown_results_paused(
    content, monkeypatch
):
    repo, store, _, _ = content
    job = store.create_job(repo.get("sample"))
    calls = []

    def response(*args, **kwargs):
        calls.append(True)
        raw = (
            {
                "id": "resp_rejected",
                "status": "failed",
                "error": {"code": "credit_balance_exhausted"},
            }
            if len(calls) == 1
            else {
                "id": "resp_completed",
                "status": "completed",
                "output_text": '{"ok":true}',
            }
        )
        return io.BytesIO(json.dumps(raw).encode())

    monkeypatch.setattr("urllib.request.urlopen", response)
    config = {
        "api_key": "test",
        "model": "test",
        "vision_model": "vision",
        "reasoning": "medium",
        "base_url": "https://api.openai.com/v1",
    }
    provider = ResponsesProvider(store, config)
    for _ in range(2):
        with pytest.raises(ProviderBlocked):
            provider.call(job, "COPY", "instructions", {}, {"type": "object"})
    assert len(calls) == 1
    with repo.connect() as c:
        receipt = c.execute(
            "SELECT * FROM content_call WHERE job_id=%s", (job["job_id"],)
        ).fetchone()
    assert receipt["status"] == "FAILED" and receipt["cost"]["amount"] is None
    store.finish(job["job_id"], "NEEDS_EVIDENCE", {"reason": "old provider error"})
    assert store.normalize_provider_failures()["normalized_jobs"] == 1
    assert store.provider_blocked()
    resumed = store.resume_provider_blocked("Test confirms replenished credits")
    assert resumed["jobs"] == [job["job_id"]] and not store.provider_blocked()
    job = store.job(job["job_id"])
    assert provider.call(job, "COPY", "instructions", {}, {"type": "object"})[0] == {
        "ok": True
    }
    assert provider.call(job, "COPY", "instructions", {}, {"type": "object"})[1][
        "reused"
    ]
    assert len(calls) == 2
    store.finish(job["job_id"], "PASS", {})
    unknown = store.create_job(repo.get("sample"), "tw-zh-Hant-strict-v1")
    store.start_call(
        unknown["job_id"], "unknown_paid", "SOURCE_REVIEW", {"request_sha": "unknown"}
    )
    store.finish_call("unknown_paid", "RESULT_UNKNOWN", {"reason": "transport timeout"})
    store.finish(unknown["job_id"], "RESULT_UNKNOWN", {})
    assert store.resume_provider_blocked("Credits remain available")["resumed"] == 0
    assert (
        store.job(unknown["job_id"])["status"] == "RESULT_UNKNOWN" and len(calls) == 2
    )


def test_worker_refuses_a_changed_media_contract_before_paid_work(content):
    from lulu.ops.content.worker import run_job

    repo, store, assets, _ = content
    job = store.create_job(repo.get("sample"), "en-strict-v1")
    job["recipe"]["profile_sha"] = "older-language-gate"
    with pytest.raises(ValueError, match="VERSIONED_MEDIA_POLICY_JOB_REQUIRED"):
        run_job(store, assets, job, provider=object())


def test_complete_paid_receipt_reused_after_restart(content, monkeypatch):
    repo, store, _, _ = content
    job = store.create_job(repo.get("sample"))
    calls = []

    def complete(*args, **kwargs):
        calls.append(True)
        return io.BytesIO(
            json.dumps(
                {
                    "status": "completed",
                    "output_text": '{"ok":true}',
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", complete)
    config = {
        "api_key": "test",
        "model": "test",
        "vision_model": "vision",
        "reasoning": "medium",
        "base_url": "https://api.openai.com/v1",
    }
    assert ResponsesProvider(store, config).call(
        job, "COPY", "instruction", {}, {"type": "object"}
    )[0] == {"ok": True}
    result, usage = ResponsesProvider(store, config).call(
        job, "COPY", "instruction", {}, {"type": "object"}
    )
    assert usage["reused"] and len(calls) == 1


def test_numeric_basis_and_unit_traceability(content):
    *_, bundle = content
    valid = {
        "description_text_html": "<p>含量50毫克。</p>",
        "description_sections": [
            {
                "evidence_ids": ["ev1"],
                "table_rows": [
                    {
                        "amount": "50.0",
                        "basis": "",
                        "unit": "毫克",
                        "evidence_ids": ["ev1"],
                    }
                ],
            }
        ],
    }
    assert static_qa(valid, bundle, {"sku1"})["status"] == "PASS"
    wrong = copy.deepcopy(valid)
    wrong["description_sections"][0]["table_rows"][0]["amount"] = "500"
    # 500 exists as capacity but reviewer still independently checks semantic numeric pairing.
    wrong["description_sections"][0]["table_rows"][0]["amount"] = "700"
    assert static_qa(wrong, bundle, {"sku1"})["status"] == "REPAIR"


def test_locale_table_headers_and_shared_basis_render_once():
    rows = [
        {
            "label": "蛋白質",
            "amount": "3.2",
            "unit": "g",
            "basis": "每100克",
            "evidence_ids": ["ev1"],
        },
        {
            "label": "脂肪",
            "amount": "10",
            "unit": "g",
            "basis": "每100克",
            "evidence_ids": ["ev1"],
        },
    ]
    raw = [
        {
            "heading": "營養資訊",
            "html": "<p>营养含量</p>",
            "evidence_ids": ["ev1"],
            "table_rows": rows,
        }
    ]
    sections, html = render_for(raw, "zh-Hans")
    assert "<th>项目</th>" in html and "項目" not in html and html.count("每100克") == 1
    assert "蛋白质" in html and len(sections[0]["table_rows"]) == 2
    assert "<th>Item</th>" in render_for(raw, "en")[1]


def test_safe_crop_only_removes_edge_channel_preserving_product_regions():
    image = {"visual_facts": {"contains_physical_product": False}}
    regions = [
        {
            "source_text": "天猫店铺",
            "category": "PLATFORM",
            "placement": "EXTERNAL",
            "confidence": 0.99,
            "bbox": [0, 0, 1000, 90],
        },
        {
            "source_text": "容量500ml",
            "category": "PRODUCT_FACT",
            "placement": "EXTERNAL",
            "confidence": 0.99,
            "bbox": [0, 200, 1000, 900],
        },
    ]
    assert safe_edge_crop_box(image, regions, 1000, 1000) == (0, 91, 1000, 1000)
    image["visual_facts"]["contains_physical_product"] = True
    assert safe_edge_crop_box(image, regions, 1000, 1000) is None

    image["visual_facts"]["contains_physical_product"] = False
    regions[0]["bbox"] = [0, 350, 1000, 400]
    assert safe_edge_crop_box(image, regions, 1000, 1000) is None


def test_ocr_color_hint_is_not_a_withheld_sku_id(content):
    *_, bundle = content
    bundle["authoritative_skus"] = [{"source_sku_id": "sku1"}]
    bundle["structured_facts"][0]["applicable_sku_ids"] = ["orange"]
    value = {
        "description_text_html": "<p>保湿500ml。</p>",
        "description_sections": [{"evidence_ids": ["ev1"], "table_rows": []}],
    }
    assert static_qa(value, bundle, {"sku1"})["status"] == "PASS"
    bundle["authoritative_skus"].append({"source_sku_id": "withheld"})
    bundle["structured_facts"][0]["applicable_sku_ids"] = ["withheld"]
    assert "WITHHELD_SKU_EVIDENCE_USED" in static_qa(value, bundle, {"sku1"})["errors"]


def test_background_response_recovered_by_get_only(content, monkeypatch):
    repo, store, _, _ = content
    job = store.create_job(repo.get("sample"))
    methods = []

    def respond(request, **kwargs):
        methods.append(request.get_method())
        if request.get_method() == "POST":
            return io.BytesIO(
                json.dumps({"id": "resp_test", "status": "queued"}).encode()
            )
        return io.BytesIO(
            json.dumps(
                {
                    "id": "resp_test",
                    "status": "completed",
                    "output_text": '{"ok":true}',
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                }
            ).encode()
        )

    monkeypatch.setattr("urllib.request.urlopen", respond)
    monkeypatch.setattr("time.sleep", lambda _: None)
    config = {
        "api_key": "test",
        "model": "test",
        "vision_model": "vision",
        "reasoning": "medium",
        "base_url": "https://api.openai.com/v1",
        "background_poll": True,
    }
    assert ResponsesProvider(store, config).call(
        job, "COPY", "instruction", {}, {"type": "object"}
    )[0] == {"ok": True}
    assert methods == ["POST", "GET"]
