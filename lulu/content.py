"""Fact-only Simplified Chinese composition; no unsupported editorial claims.

Chinese supplier evidence does not require an LLM translation call. Layout and
neutral copy are deterministic and every precise field carries its source ref.
The language strategy is independent from product, source, and media identity.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

from opencc import OpenCC

from .core import CATEGORIES, LOCALE, digest
from .vendor.source_sku_contract import consumer_text

VERSION = "lulu-fact-content-zh-v1.2"
_simplified = OpenCC("t2s")
COMMERCIAL = re.compile(
    r"天猫国际|天猫|淘宝|旗舰店|官方店|自营|保税仓|保税|包邮|直邮|发货|现货速达|领券|券后|到手价|正品保证|买\d+送\d+|促销|特价|爆款|热卖|热销|限时|临期|赠品|优惠|销量|补贴|淘金币|客服|国内现货|拼团|团购|直播间|正品|旗舰|官方|拍下|满减|囤货|实惠|超值|畅销|加拿大热卖|大赏",
    re.IGNORECASE,
)
CLAIMS = re.compile(
    r"治愈|治疗|疗效|抗炎|消炎|改善(?:睡眠|便秘|贫血|关节|免疫力)|增强(?:免疫力|抵抗力)|强健骨骼|补钙|抗疲劳|抗氧化|抗衰老|降血糖|降血压|祛斑|美白|壮阳|减肥|促进代谢|燃脂|丰胸|三甲医院|专家推荐|销量第一|最好|最佳|顶级"
)
SERVICE_ONLY = re.compile(
    r"勿拍|仅供说明|下拉详情|联系客服|运费|快递费|测试链接|不发货|赠品专用|非卖品"
)
PLACEHOLDER = re.compile(
    r"待AI|待解析|占位|暂无|待补充|请填写|\(\s*\)|（\s*）", re.IGNORECASE
)
SAFE_ATTRIBUTES = {
    "品牌",
    "系列",
    "产品类别",
    "食品类别",
    "质地",
    "材质",
    "产品材质",
    "执行标准",
    "配料表",
    "成分表",
    "主要成分",
    "产地",
    "原产地",
    "生产国",
    "制造国",
    "储存方法",
    "储存条件",
    "保质期",
    "适用肤质",
}


def simplify(value):
    return _simplified.convert(str(value or ""))


def neutral(value):
    text = simplify(consumer_text(value))
    text = re.sub(
        r"[【\[]([^】\]]*)[】\]]",
        lambda m: " " if COMMERCIAL.search(m[1]) else m[1],
        text,
    )
    text = COMMERCIAL.sub(" ", text)
    text = CLAIMS.sub(" ", text)
    text = re.sub(r"(?:日本|韩国|马来西亚|台湾|泰国|新加坡)?进口", " ", text)
    text = re.sub(r"([\u4e00-\u9fff]{2,6})\1", r"\1", text)
    text = re.sub(r"\b(?:¥|￥|CNY|RMB)\s*\d+(?:\.\d+)?", " ", text)
    text = re.sub(r"^[\s丨|·:：,，]+|[\s丨|·:：,，]+$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def sku_label(sku):
    values = []
    for axis in sku.get("source_options") or []:
        value = neutral(axis["value"])
        name = simplify(axis["name"])
        if value:
            values.append(name + "：" + value)
    if not values:
        values = [neutral(sku.get("raw_label"))]
    return "；".join(v for v in values if v)


def content_dependency(facts, skus, locale=LOCALE):
    """Content depends on physical facts and selected SKUs, never source prices."""
    return digest(
        {
            "version": VERSION,
            "locale": locale,
            "product_id": facts["product_id"],
            "identity": facts.get("identity_version"),
            "title": facts.get("title"),
            "brand": facts.get("brand"),
            "category": facts.get("category"),
            "attributes": facts.get("attributes"),
            "skus": [
                (s["source_sku_id"], s.get("source_options"), s["consumer_label"])
                for s in skus
            ],
        }
    )


def rebase_evidence(content, facts):
    from copy import deepcopy

    content = deepcopy(content)
    old = (content.get("evidence_refs") or [None])[0]
    new = facts.get("evidence_id")
    if old and new:
        content["evidence_refs"] = [
            new + ref[len(old) :]
            if isinstance(ref, str) and ref.startswith(old)
            else ref
            for ref in content["evidence_refs"]
        ]
        for a in content["attributes"]:
            if a["evidence_ref"].startswith(old):
                a["evidence_ref"] = new + a["evidence_ref"][len(old) :]
    content["source_facts_sha"] = digest(facts)
    return content


def compose(facts, skus, locale=LOCALE):
    if locale != LOCALE:
        raise ValueError("LOCALE_NOT_ENABLED")
    title = neutral(facts.get("title"))
    title = re.sub(
        r"^\d+\.\d{1,2}\s+(?!(?:ml|kg|mg|cm|mm|g|l)\b)", "", title, flags=re.I
    )
    title = re.sub(r"\d{1,2}/\d{1,2}起\S*", "", title).strip()
    brand = simplify(facts.get("brand"))
    if not title:
        raise ValueError("PRODUCT_TITLE_MISSING")
    if len(skus) > 1:
        # A marketplace headline often advertises just one capacity. Multiple
        # retained sizes are listed in options rather than applied to the SPU.
        title = re.sub(
            r"\d+(?:\.\d+)?\s*(?:ml|kg|g|l|片|粒|枚|袋|包|瓶|盒|罐)(?:\s*[×xX*]\s*\d+\s*(?:袋|包|瓶|盒|罐)?)?",
            " ",
            title,
            flags=re.IGNORECASE,
        )
        title = re.sub(r"\s+", " ", title).strip()
    evidence = []
    attributes = []
    for i, row in enumerate(facts.get("attributes") or []):
        if not isinstance(row, dict):
            continue
        name = simplify(row.get("name"))
        value = simplify(row.get("value")).strip()
        if (
            name not in SAFE_ATTRIBUTES
            or not value
            or COMMERCIAL.search(value)
            or CLAIMS.search(value)
            or PLACEHOLDER.search(value)
        ):
            continue
        if value in {
            "其他",
            "其他/other",
            "other",
            "未知",
            "详见包装",
            "详见图片",
            "见详情",
            "无",
            "-",
        }:
            continue
        ref = f"{facts.get('evidence_id')}:attribute:{i}"
        attributes.append({"name": name, "value": value, "evidence_ref": ref})
        evidence.append(ref)
    labels = {s["source_sku_id"]: s["consumer_label"] for s in skus}
    # Use exact physical quantities from retained SKU options. A single root
    # net-content field is not applied to multiple variants.
    body = "<p>" + html.escape(title) + "</p><h2>商品信息</h2><ul>"
    body += "<li>品牌：" + html.escape(brand) + "</li>"
    body += (
        "<li>品类："
        + html.escape(CATEGORIES.get(facts.get("category"), "商品"))
        + "</li>"
    )
    for a in attributes:
        body += (
            "<li>" + html.escape(a["name"]) + "：" + html.escape(a["value"]) + "</li>"
        )
    body += "</ul><h2>可选规格</h2><ul>"
    for label in labels.values():
        body += "<li>" + html.escape(label) + "</li>"
    body += "</ul>"
    features = [a["name"] + "：" + a["value"] for a in attributes[:4]]
    content = {
        "locale": locale,
        "title": title,
        "brand": brand,
        "description_html": body,
        "selling_points": features,
        "sku_labels": labels,
        "seo": {
            "title": title[:70],
            "description": ("；".join(features) or title)[:160],
        },
        "media_alt": {
            s["source_sku_id"]: title + "｜" + s["consumer_label"] for s in skus
        },
        "attributes": attributes,
        "evidence_refs": [facts.get("evidence_id")] + evidence,
        "generator": "DETERMINISTIC_EVIDENCE_COMPOSITION",
        "version": VERSION,
        "source_facts_sha": digest(facts),
        "sku_scope": sorted(labels),
    }
    return content


class SafeHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.errors = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"p", "h2", "ul", "li"} or attrs:
            self.errors.append("UNSAFE_HTML")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append("UNBALANCED_HTML")


def qa(content, skus):
    errors = []
    if content.get("locale") != LOCALE:
        errors.append("LOCALE_MISMATCH")
    if set(content.get("sku_labels") or {}) != {s["source_sku_id"] for s in skus}:
        errors.append("SKU_SCOPE_MISMATCH")
    text = " ".join(
        [content.get("title", ""), content.get("description_html", "")]
        + list((content.get("sku_labels") or {}).values())
    )
    if COMMERCIAL.search(text):
        errors.append("MARKETPLACE_COPY_REMAINING")
    if CLAIMS.search(text):
        errors.append("UNSUPPORTED_CLAIM")
    if PLACEHOLDER.search(text):
        errors.append("PLACEHOLDER")
    if simplify(text) != text:
        errors.append("NOT_SIMPLIFIED_CHINESE")
    parser = SafeHTML()
    parser.feed(content.get("description_html", ""))
    errors += parser.errors
    if parser.stack:
        errors.append("UNBALANCED_HTML")
    for s in skus:
        label = content["sku_labels"].get(s["source_sku_id"], "")
        if not label:
            errors.append("EMPTY_SKU_LABEL")
        source = " ".join(a["value"] for a in s.get("source_options") or []) or s.get(
            "raw_label", ""
        )
        # Numbers distinguish capacities, amounts, shades and model versions.
        numbers = re.findall(r"\d+(?:\.\d+)?", simplify(consumer_text(source)))
        if any(n not in label for n in numbers):
            errors.append("PHYSICAL_NUMBER_LOST:" + s["source_sku_id"])
    return {
        "verdict": "PASS" if not errors else "HOLD",
        "errors": sorted(set(errors)),
        "content_sha": digest(content),
        "version": VERSION,
    }
