"""Optional local channel cleanup. It never blocks copy or replaces SKU photos."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import urllib.request

from PIL import Image
from psycopg.types.json import Jsonb

from lulu.core import digest

from .copy import image_input
from .media import CHANNEL, CHANNEL_CATEGORIES, assess_image, safe_edge_crop_box
from .policy import operator_content_hash, profile, protected_hash
from .provider import ResponsesProvider, UnknownPaidResult, settings


def isolated_channel_box(image, regions, width, height):
    if image.get("role") in {"PRIMARY", "VARIANT", "SKU"}:
        return None
    if (image.get("visual_facts") or {}).get("contains_physical_product"):
        return None
    channels = []
    protected = []
    for r in regions:
        b = r.get("bbox")
        if (
            not isinstance(b, list)
            or len(b) != 4
            or int(r.get("segment_count") or 1) != 1
            or float(r.get("confidence") or 0) < 0.9
        ):
            return None
        x1, y1, x2, y2 = [float(x) for x in b]
        if not 0 <= x1 < x2 <= 1000 or not 0 <= y1 < y2 <= 1000:
            return None
        pixel = (
            int(x1 * width / 1000),
            int(y1 * height / 1000),
            int(x2 * width / 1000),
            int(y2 * height / 1000),
        )
        is_channel = r.get("placement") != "PACKAGE" and (
            r.get("category") in CHANNEL_CATEGORIES
            or CHANNEL.search(str(r.get("source_text") or ""))
        )
        if is_channel:
            if re.search(
                r"成分|容量|净含量|淨含量|食用|过敏|過敏|营养|營養",
                str(r.get("source_text") or ""),
            ):
                return None
            channels.append(pixel)
        else:
            protected.append(pixel)
    if not channels or not protected:
        return None
    box = (
        min(b[0] for b in channels),
        min(b[1] for b in channels),
        max(b[2] for b in channels),
        max(b[3] for b in channels),
    )
    if (box[2] - box[0]) * (box[3] - box[1]) > width * height * 0.15:
        return None
    if any(
        not (b[2] <= box[0] or b[0] >= box[2] or b[3] <= box[1] or b[1] >= box[3])
        for b in protected
    ):
        return None
    return box


def create_media_job(content, row, asset_id, profile_id):
    p = profile(profile_id)
    e = content.evidence(row["cluster_id"])
    if not p.retain_product_text:
        raise ValueError("STRICT_MARKET_REQUIRES_FULL_LOCALIZATION_PLAN")
    if not e or asset_id not in e["assets"]:
        raise ValueError("ORIGINAL_ASSET_NOT_MATERIALIZED")
    image = next(
        (a for a in e["bundle"]["image_assets"] if a["asset_id"] == asset_id), None
    )
    regions = [
        r for r in e["bundle"].get("ocr_regions") or [] if r.get("asset_id") == asset_id
    ]
    owned = e["assets"][asset_id]
    selected = {
        str(v["platform_sku_id"])
        for v in row["source"]["derived"]["detail"]["consumer"]["variants"]
    }
    assessed = assess_image(image, regions, p, selected)
    if any(
        r not in {"CHANNEL_REGION_PRESENT", "LEGACY_CHANNEL_FLAG_NEEDS_REGION_REVIEW"}
        for r in assessed["reasons"]
    ):
        raise ValueError("MEDIA_IDENTITY_OR_SCOPE_NEEDS_EVIDENCE")
    if safe_edge_crop_box(image, regions, owned["width"], owned["height"]):
        raise ValueError("SAFE_CROP_AVAILABLE_NO_AI_NEEDED")
    box = isolated_channel_box(image, regions, owned["width"], owned["height"])
    if not box:
        raise ValueError("LOCAL_EDIT_NOT_PROVEN_SAFE_KEEP_SOURCE_ONLY")
    config = settings()
    recipe = {
        "copy_contract": p.copy_contract,
        "profile_sha": p.sha,
        "model": config["model"],
        "vision_model": config["vision_model"],
        "reasoning": config["reasoning"],
        "provider": config["base_url"],
        "image_model": os.getenv("WUJIE_MERCH_IMAGE_MODEL", "gpt-image-2"),
    }
    # The source hash/box is an explicit work item; no auto-expansion to other pictures.
    with content.repo.connect() as c:
        fingerprint = digest(
            [
                row["cluster_id"],
                e["evidence_sha"],
                recipe,
                asset_id,
                owned["sha256"],
                box,
                "local-channel-inpaint-v1",
            ]
        )
        jid = "LCJ-" + fingerprint[:28]
        c.execute(
            "INSERT INTO content_job(job_id,fingerprint,cluster_id,profile_id,evidence_id,intent,base_operator_sha,protected_sha,recipe,checkpoints) VALUES(%s,%s,%s,%s,%s,'MEDIA_EDIT',%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (
                jid,
                fingerprint,
                row["cluster_id"],
                profile_id,
                e["evidence_id"],
                operator_content_hash(row),
                protected_hash(row),
                Jsonb(recipe),
                Jsonb(
                    {
                        "media_request": {
                            "asset_id": asset_id,
                            "source_sha": owned["sha256"],
                            "box": box,
                        }
                    }
                ),
            ),
        )
    return content.job(jid)


def multipart(fields, raw):
    boundary = "----Lulu" + hashlib.sha256(raw).hexdigest()[:20]
    parts = []
    for k, v in fields.items():
        parts.extend(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            ]
        )
    parts.extend(
        [
            f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="region.png"\r\nContent-Type: image/png\r\n\r\n'.encode(),
            raw,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts), "multipart/form-data; boundary=" + boundary


def run_media_job(content, assets, job):
    p = profile(job["profile_id"])
    e = content.evidence(evidence_id=job["evidence_id"])
    request = job["checkpoints"]["media_request"]
    owned = e["assets"][request["asset_id"]]
    if owned["sha256"] != request["source_sha"]:
        raise ValueError("MEDIA_SOURCE_CHANGED")
    source = assets.read(owned["object_key"])
    box = tuple(request["box"])
    with Image.open(io.BytesIO(source)) as im:
        original = im.convert("RGBA")
        patch = original.crop(box)
        patch.save(buffer := io.BytesIO(), "PNG")
    raw_patch = buffer.getvalue()
    config = settings()
    instruction = "Remove only marketplace/shop/price/logistics/service text from this isolated channel region. Fill its background naturally. Do not invent product graphics or words. Product facts, original packaging and brand artwork outside this region are protected and will stay pixel-identical."
    fields = {
        "model": job["recipe"]["image_model"],
        "prompt": instruction,
        "size": "1024x1024",
        "quality": "medium",
        "n": "1",
    }
    fingerprint = digest(["lulu-local-channel-edit-v1", owned["sha256"], box, fields])
    prior = content.call(fingerprint)
    if prior:
        if prior["status"] != "DONE":
            raise UnknownPaidResult("MEDIA_EDIT_RESULT_UNKNOWN:" + fingerprint)
        result = prior["response"]
    else:
        started = content.start_call(
            job["job_id"],
            fingerprint,
            "MEDIA_EDIT",
            {
                "model": fields["model"],
                "source_sha": owned["sha256"],
                "box": box,
                "request_sha": digest(fields),
            },
        )
        if not started["dispatched_now"]:
            raise UnknownPaidResult("MEDIA_EDIT_RESULT_UNKNOWN:" + fingerprint)
        body, mime = multipart(fields, raw_patch)
        req = urllib.request.Request(
            config["base_url"].rstrip("/") + "/images/edits",
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + config["api_key"],
                "Content-Type": mime,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as response:
                result = json.load(response)
        except (OSError, ValueError):
            content.finish_call(
                fingerprint,
                "RESULT_UNKNOWN",
                {"reason": "IMAGE_EDIT_TRANSPORT_UNKNOWN"},
            )
            raise UnknownPaidResult(
                "MEDIA_EDIT_RESULT_UNKNOWN:" + fingerprint
            ) from None
        content.finish_call(fingerprint, "DONE", result, result.get("usage") or {})
    encoded = (result.get("data") or [{}])[0].get("b64_json")
    if not encoded:
        raise ValueError("MEDIA_EDIT_OUTPUT_MISSING")
    with Image.open(io.BytesIO(base64.b64decode(encoded))) as edited:
        new_patch = edited.convert("RGBA").resize((box[2] - box[0], box[3] - box[1]))
    output = original.copy()
    output.paste(new_patch, (box[0], box[1]))
    output.save(out := io.BytesIO(), "PNG")
    data = out.getvalue()
    sha, key = assets.put(data, "image/png")
    content.repo.save_media(
        sha,
        key,
        "image/png",
        len(data),
        {
            "kind": "LOCAL_CHANNEL_EDIT",
            "parent_sha": owned["sha256"],
            "edit_box": box,
            "outside_box_pixels": "EXACT_SOURCE",
            "profile_id": p.profile_id,
        },
    )
    output_asset = {
        "asset_id": "edited-" + request["asset_id"],
        "sha256": sha,
        "object_key": key,
        "width": owned["width"],
        "height": owned["height"],
    }
    inputs = image_input(request["asset_id"], owned, assets) + image_input(
        output_asset["asset_id"], output_asset, assets
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "enum": ["PASS", "REPAIR"]},
            "same_product": {"type": "boolean"},
            "package_changed": {"type": "boolean"},
            "product_facts_changed": {"type": "boolean"},
            "channel_remaining": {"type": "boolean"},
            "wrong_sku": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": [
            "status",
            "same_product",
            "package_changed",
            "product_facts_changed",
            "channel_remaining",
            "wrong_sku",
            "reason",
        ],
    }
    qa, _ = ResponsesProvider(content).call(
        job,
        "MEDIA_QA",
        "Compare original and local cleanup. Product text and factual marketing may remain in Canadian Chinese. Verify unchanged product/package/SKU/quantities and zero shop/platform information.",
        {
            "profile_id": p.profile_id,
            "edit_box": box,
            "source_ocr": [
                r
                for r in e["bundle"]["ocr_regions"]
                if r.get("asset_id") == request["asset_id"]
            ],
        },
        schema,
        inputs,
        vision=True,
    )
    passed = (
        qa["status"] == "PASS"
        and qa["same_product"]
        and not any(
            qa[k]
            for k in [
                "package_changed",
                "product_facts_changed",
                "channel_remaining",
                "wrong_sku",
            ]
        )
    )
    outcome = {
        "status": "PASS" if passed else "SOURCE_ONLY",
        "source_asset_id": request["asset_id"],
        "source_sha": owned["sha256"],
        "output_sha": sha,
        "url": "/api/media/" + sha,
        "qa": qa,
        "image_edits": 1,
        "shopify_writes": 0,
    }
    if passed:
        prior = next(
            (
                r
                for r in content.revisions(job["cluster_id"], job["profile_id"])
                if r["status"] == "QA_PASS"
            ),
            None,
        )
        if prior:
            import copy

            updated = copy.deepcopy(prior["body"])
            media = updated.get("description_media") or []
            media.append(
                {
                    "asset_id": request["asset_id"],
                    "source_asset_id": request["asset_id"],
                    "sha256": sha,
                    "parent_sha": owned["sha256"],
                    "url": "/api/media/" + sha,
                    "object_key": key,
                    "width": owned["width"],
                    "height": owned["height"],
                    "caption": "商品信息图",
                    "order": len(media),
                    "qa": {"status": "PASS", "operation": "LOCAL_CHANNEL_EDIT", **qa},
                }
            )
            updated["description_media"] = media
            copied_qa = {
                **prior["qa"],
                "media_cleanup": outcome,
                "image_edits": int(prior["qa"].get("image_edits") or 0) + 1,
            }
            revision = content.save_revision(job, updated, copied_qa, prior["method"])
            outcome["revision_id"] = revision["revision_id"]
    content.finish(job["job_id"], "PASS" if passed else "NEEDS_EVIDENCE", outcome)
    return {"job_id": job["job_id"], **outcome}
