"""Lossless, deterministic text preparation for the merchandising editor.

The source HTML remains in the evidence bundle.  These smaller facts make its
buyer-facing sentences and tables usable without asking the editor to read a
large, noisy page as one fact.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from .shadow_merch_contracts import digest


_SPACE = re.compile(r"\s+")
_CHANNEL = re.compile(
    r"天[猫貓]|淘[宝寶]|店[铺鋪]|客服|物流|快[递遞]|保[税稅][仓倉]|"
    r"优惠|優惠|满减|滿減|到手价|到手價|包邮|包郵|售后|售後|"
    r"正品保证|正品保證|下单|下單|发货|發貨|购物须知|購物須知", re.I)
_NUTRITION = re.compile(
    r"营养|營養|每份|每100|热量|熱量|蛋白质|蛋白質|碳水|脂肪|钠|鈉|"
    r"膳食纤维|膳食纖維|卡路里|能量|糖分|糖類|糖类|"
    r"protein|sodium|calories|carbohydrate|total fat|total sugars|kcal|kj\b", re.I)
_INGREDIENTS = re.compile(r"成分|配料|原料|含有|添加|提取物|萃取|维生素|維生素", re.I)
_USAGE = re.compile(r"使用方法|使用方式|食用方法|食用方式|每日|每次|涂抹|塗抹|服用", re.I)
_SPEC = re.compile(r"规格|規格|净含量|淨含量|容量|毫升|公克|公升|\bml\b|\bg\b", re.I)
_FEATURE = re.compile(r"保湿|保濕|清爽|防晒|防曬|质地|質地|香调|香調|口味|肤感|膚感|妆效|妝效|特色|亮点|亮點", re.I)
_RISKY_CLAIM = re.compile(r"治疗|治療|治愈|治癒|预防疾病|預防疾病|止痛|消炎|改善关节疼痛|改善關節疼痛", re.I)


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip(" \u3000\t\r\n")


def _kind(text: str) -> str:
    if _CHANNEL.search(text):
        return "CHANNEL"
    if _RISKY_CLAIM.search(text):
        return "SELLER_HEALTH_CLAIM"
    if _NUTRITION.search(text):
        return "NUTRITION"
    if _INGREDIENTS.search(text):
        return "INGREDIENTS"
    if _USAGE.search(text):
        return "USAGE"
    if _SPEC.search(text):
        return "SPECIFICATION"
    if _FEATURE.search(text):
        return "FEATURE"
    return "OTHER"


class _VisibleBlocks(HTMLParser):
    _BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "dt", "dd", "br"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.blocks: list[tuple[str, str]] = []
        self.cells: list[str] = []
        self.cell: list[str] | None = None

    def _flush(self) -> None:
        value = _clean(" ".join(self.parts))
        if value:
            self.blocks.append(("TEXT", value))
        self.parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "tr":
            self._flush()
            self.cells = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag in self._BLOCKS:
            self._flush()
        if tag == "img":
            alt = next((value for key, value in attrs if key == "alt"), None)
            if alt:
                self.parts.append(alt)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cells.append(_clean(" ".join(self.cell)))
            self.cell = None
        elif tag == "tr":
            if any(self.cells):
                self.blocks.append(("TABLE_ROW", " | ".join(self.cells)))
            self.cells = []
        elif tag in self._BLOCKS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        else:
            self.parts.append(data)

    def finish(self) -> list[tuple[str, str]]:
        self._flush()
        return self.blocks


def source_text_facts(source_text_blocks: list[Any]) -> list[dict[str, Any]]:
    """Return short source-linked facts; never overwrite the original blocks."""
    facts: list[dict[str, Any]] = []
    for source_index, raw in enumerate(source_text_blocks):
        source = str(raw or "")
        source_id = "EV-" + digest(["SOURCE_TEXT", source_index, source])[:24]
        parser = _VisibleBlocks()
        parser.feed(source)
        blocks = parser.finish()
        for block_index, (block_kind, value) in enumerate(blocks):
            sentences = ([value] if block_kind == "TABLE_ROW" else
                         re.split(r"(?<=[。！？!?；;])\s*", value))
            fragments = [fragment for sentence in sentences for fragment in
                         (re.split(r"[，,]", sentence)
                          if _CHANNEL.search(sentence) else [sentence])]
            for sentence_index, segment in enumerate(fragments):
                text = _clean(segment)
                if not text:
                    continue
                category = _kind(text)
                facts.append({
                    "evidence_id": "EV-" + digest([
                        "SOURCE_SEGMENT", source_id, block_index, sentence_index, text])[:24],
                    "source_evidence_id": source_id,
                    "evidence_kind": block_kind,
                    "category": category,
                    "text": text,
                    "original_text": text,
                    "source_order": [source_index, block_index, sentence_index],
                })
    return facts
