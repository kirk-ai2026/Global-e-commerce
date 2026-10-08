from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from lulu.core import digest


class UnknownPaidResult(RuntimeError):
    pass


def settings():
    keys = {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MARKET_COMPARE_OPENAI_API_KEY",
        "MARKET_COMPARE_OPENAI_BASE_URL",
        "WUJIE_GLOBAL_MERCH_EDITOR_MODEL",
        "WUJIE_GLOBAL_MERCH_REASONING_EFFORT",
        "WUJIE_EVIDENCE_VISION_MODEL",
    }
    value = {}
    path = Path(
        os.getenv(
            "LULU_CONTENT_PROVIDER_ENV",
            "/Users/apple/Documents/New project/market_compare/.runtime/secure/narra.env",
        )
    )
    if path.is_file():
        for line in path.read_text().splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            k, v = line.removeprefix("export ").split("=", 1)
            if k.strip() in keys:
                value[k.strip()] = v.strip().strip("\"'")
    value.update({k: os.environ[k] for k in keys if os.environ.get(k)})
    return {
        "api_key": value.get("MARKET_COMPARE_OPENAI_API_KEY")
        or value.get("OPENAI_API_KEY"),
        "base_url": value.get("MARKET_COMPARE_OPENAI_BASE_URL")
        or value.get("OPENAI_BASE_URL")
        or "https://api.openai.com/v1",
        "model": os.getenv("LULU_CONTENT_MODEL")
        or value.get("WUJIE_GLOBAL_MERCH_EDITOR_MODEL")
        or "gpt-6-astra",
        "vision_model": value.get("WUJIE_EVIDENCE_VISION_MODEL") or "gpt-5.6-luna",
        "reasoning": value.get("WUJIE_GLOBAL_MERCH_REASONING_EFFORT") or "medium",
        "background_poll": os.getenv("LULU_CONTENT_BACKGROUND_POLL", "1") == "1",
    }


class ResponsesProvider:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config or settings()
        if not self.config.get("api_key"):
            raise ValueError("CONTENT_PROVIDER_CREDENTIAL_MISSING")

    def call(
        self, job, kind, instructions, payload, schema, images=None, *, vision=False
    ):
        content = [
            {"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}
        ] + (images or [])
        body = {
            "model": self.config["vision_model"] if vision else self.config["model"],
            "store": False,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lulu_" + kind.lower(),
                    "strict": True,
                    "schema": schema,
                }
            },
        }
        if not vision:
            body["reasoning"] = {"effort": self.config["reasoning"]}
        fingerprint = digest(["responses-call-v1", job["job_id"], kind, body])
        previous = self.store.call(fingerprint) or self.store.matching_call(
            digest(body), kind
        )
        if previous:
            if previous["status"] == "DONE":
                return parse(previous["response"]), {
                    "reused": True,
                    "fingerprint": fingerprint,
                    **(previous["usage"] or {}),
                }
            if previous["status"] in {"DISPATCHED", "RESULT_UNKNOWN"}:
                if (previous.get("response") or {}).get("id"):
                    return self.poll(
                        job,
                        previous["fingerprint"],
                        kind,
                        previous["response"],
                        time.monotonic(),
                    )
                raise UnknownPaidResult("PAID_RESULT_UNKNOWN:" + fingerprint)
            raise RuntimeError("PROVIDER_CALL_ALREADY_FAILED:" + fingerprint)
        meta = {
            "model": body["model"],
            "request_sha": digest(body),
            "kind": kind,
            "schema_sha": digest(schema),
            "image_count": len(images or []),
        }
        dispatched = self.store.start_call(job["job_id"], fingerprint, kind, meta)
        if not dispatched.get("dispatched_now"):
            raise UnknownPaidResult("CONCURRENT_PAID_DISPATCH:" + fingerprint)
        request = urllib.request.Request(
            self.config["base_url"].rstrip("/") + "/responses",
            data=json.dumps(
                {**body, "background": True}
                if self.config.get("background_poll")
                else body
            ).encode(),
            method="POST",
            headers={
                "Authorization": "Bearer " + self.config["api_key"],
                "Content-Type": "application/json",
                "X-Client-Request-Id": fingerprint,
            },
        )
        start = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=600) as response:
                raw = json.load(response)
        except urllib.error.HTTPError as error:
            self.store.finish_call(
                fingerprint,
                "FAILED",
                {"http_status": error.code, "reason": "PROVIDER_HTTP_REJECTION"},
            )
            raise RuntimeError("PROVIDER_HTTP_" + str(error.code)) from None
        except (OSError, ValueError):
            self.store.finish_call(
                fingerprint, "RESULT_UNKNOWN", {"reason": "TRANSPORT_RESULT_UNKNOWN"}
            )
            raise UnknownPaidResult("PAID_RESULT_UNKNOWN:" + fingerprint) from None
        if raw.get("id"):
            self.store.progress_call(fingerprint, raw)
        return self.poll(job, fingerprint, kind, raw, start)

    def poll(self, job, fingerprint, kind, raw, start):
        deadline = time.monotonic() + 1800
        while raw.get("status") in {"queued", "in_progress"}:
            rid = raw.get("id")
            if not rid:
                raise UnknownPaidResult("BACKGROUND_RESPONSE_ID_MISSING")
            self.store.progress_call(fingerprint, raw)
            self.store.checkpoint(
                job["job_id"],
                "provider_progress",
                {"kind": kind, "response_id": rid, "status": raw["status"]},
            )
            if time.monotonic() > deadline:
                self.store.finish_call(fingerprint, "RESULT_UNKNOWN", raw)
                raise UnknownPaidResult("BACKGROUND_RESULT_PENDING:" + fingerprint)
            time.sleep(5)
            request = urllib.request.Request(
                self.config["base_url"].rstrip("/") + "/responses/" + rid,
                headers={"Authorization": "Bearer " + self.config["api_key"]},
            )
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    raw = json.load(response)
            except (OSError, ValueError):
                self.store.finish_call(fingerprint, "RESULT_UNKNOWN", raw)
                raise UnknownPaidResult(
                    "BACKGROUND_POLL_UNAVAILABLE:" + fingerprint
                ) from None
        usage = {
            **(raw.get("usage") or {}),
            "elapsed_seconds": round(time.monotonic() - start, 3),
            "response_id": raw.get("id"),
            "paid_calls": 1,
        }
        cost = raw.get("cost") or {
            "amount": None,
            "currency": "USD",
            "reason": "PROVIDER_BILLING_NOT_RETURNED",
        }
        self.store.finish_call(fingerprint, "DONE", raw, usage, cost)
        return parse(raw), {"reused": False, "fingerprint": fingerprint, **usage}


def parse(raw):
    if raw.get("status") not in {None, "completed"}:
        raise RuntimeError("PROVIDER_OUTPUT_" + str(raw.get("status")))
    if any(
        p.get("type") == "refusal"
        for r in raw.get("output") or []
        for p in r.get("content") or []
    ):
        raise RuntimeError("PROVIDER_REFUSAL")
    text = raw.get("output_text") or "".join(
        str(p.get("text") or "")
        for r in raw.get("output") or []
        for p in r.get("content") or []
        if p.get("type") == "output_text"
    )
    return json.loads(text)
