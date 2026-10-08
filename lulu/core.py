from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

APP = "lulu"
MARKET = "CA"
CURRENCY = "CAD"
LOCALE = "zh-Hans"
CONTRACT = "lulu-internal-product-v1.2"
ASIAN_REGIONS = frozenset(
    {
        "CN",
        "TW",
        "HK",
        "MO",
        "JP",
        "KR",
        "KP",
        "MN",
        "BN",
        "KH",
        "ID",
        "LA",
        "MY",
        "MM",
        "PH",
        "SG",
        "TH",
        "TL",
        "VN",
    }
)
CATEGORIES = {
    "food": "食品",
    "supplements": "保健品",
    "beauty": "美妆个护",
    "mother_baby": "母婴",
}


def canonical(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def load(value, default=None):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value or "")
    except (ValueError, TypeError):
        return {} if default is None else default


def money(value) -> str | None:
    try:
        v = Decimal(str(value))
        return str(v) if v.is_finite() and v > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def timestamp(value) -> str:
    try:
        d = datetime.fromisoformat(str(value))
        if d.tzinfo is None:
            d = d.replace(tzinfo=UTC)
        return d.astimezone(UTC).isoformat()
    except (ValueError, TypeError):
        return ""


def url(value) -> str | None:
    value = str(value or "")
    if value.startswith("//"):
        value = "https:" + value
    return (
        value
        if urlsplit(value).scheme in {"http", "https"} and urlsplit(value).hostname
        else None
    )


def brand_key(value) -> str:
    return re.sub(r"[^0-9a-z\u3400-\u9fff]", "", str(value or "").casefold())


def data_dir() -> Path:
    return Path(os.environ.get("LULU_DATA_DIR", "data")).resolve()


def database_url() -> str:
    return os.environ.get("LULU_DATABASE_URL", "postgresql://lulu@127.0.0.1:55438/lulu")
