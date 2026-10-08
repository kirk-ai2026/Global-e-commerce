from __future__ import annotations

import copy
import html
import re
from html.parser import HTMLParser

from opencc import OpenCC

from lulu.core import digest

VERSION = "lulu-ca-hans-source-localization-v1"
CONVERTER = OpenCC("t2s")
TERMS = {
    "洗发精": "洗发水",
    "沐浴乳": "沐浴露",
    "公克": "克",
    "资讯": "信息",
    "纽西兰": "新西兰",
    "台湾用语": "简体中文用语",
    "商品页预览": "商品页面预览",
}
ALLOWED = {
    "p",
    "h2",
    "h3",
    "ul",
    "ol",
    "li",
    "strong",
    "em",
    "br",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
}


def hans(value):
    value = CONVERTER.convert(str(value or ""))
    for before, after in TERMS.items():
        value = value.replace(before, after)
    return value


class Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "iframe", "object"}:
            self.suppressed += 1
        elif not self.suppressed and tag in ALLOWED:
            self.parts.append("<" + tag + ">")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "iframe", "object"}:
            self.suppressed = max(0, self.suppressed - 1)
        elif not self.suppressed and tag in ALLOWED and tag != "br":
            self.parts.append("</" + tag + ">")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(html.escape(data))


def safe_html(value):
    parser = Sanitizer()
    parser.feed(value or "")
    return "".join(parser.parts)


def canonical_numbers(value):
    return re.findall(r"\d+(?:\.\d+)?", str(value or ""))


def localize(source):
    consumer = copy.deepcopy(source.get("consumer") or {})
    original = consumer.get("description_html") or ""
    title = hans(consumer.get("title"))
    description = safe_html(hans(original))
    # Market-specific business prose remains evidence, never becomes a Canadian claim.
    market_segments = []

    def remove_segment(match):
        if re.search(
            r"台湾(?:市场|消费者|用户|售价|运费|配送|热销|现货|法规|认证)|新台币|NT\$",
            match.group(0),
        ):
            market_segments.append(match.group(0))
            return ""
        return match.group(0)

    description = re.sub(r"<p>.*?</p>", remove_segment, description, flags=re.S)
    labels = {}
    errors = []
    for variant in consumer.get("variants") or []:
        old_label = variant.get("localized_label") or variant.get("title") or ""
        label = hans(old_label)
        sid = str(variant.get("platform_sku_id") or "")
        labels[sid] = label
        if canonical_numbers(old_label) != canonical_numbers(label):
            errors.append("SKU数字变化:" + sid)
    if canonical_numbers(consumer.get("title")) != canonical_numbers(title):
        errors.append("标题数字变化")
    if not title or not description:
        errors.append("商品内容缺失")
    preview_text = re.sub(r"<[^>]+>", " ", description)
    value = {
        "locale": "zh-Hans",
        "market": "CA",
        "title": title,
        "description_html": description,
        "brand": hans(consumer.get("brand")),
        "sku_labels": labels,
        "seo_title": title[:70],
        "seo_description": re.sub(r"\s+", " ", preview_text).strip()[:160],
        "media_alt": title,
        "version": VERSION,
        "source_content_sha": digest(consumer),
        "omitted_market_segments": market_segments,
        "qa": {
            "verdict": "PASS" if not errors else "HOLD",
            "errors": errors,
            "method": "SOURCE_APPROVED_SEMANTICS_PRESERVING_LOCALIZATION",
            "upstream_media_qa_reused": True,
            "canadian_sale_eligibility_verified": False,
        },
    }
    return {**value, "content_sha": digest(value)}
