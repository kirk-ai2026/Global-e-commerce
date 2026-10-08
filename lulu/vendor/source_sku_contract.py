"""Lossless supplier SKU axes and Taiwan consumer-only projections.

No Cartesian products are manufactured. Procurement evidence is never rewritten.
"""
from __future__ import annotations

import json
import re
import calendar
from datetime import date
from typing import Any

VERSION = "source-sku-contract-v8-partial-publication"
AXIS = re.compile(r"(?:(?:-?\d+:-?\d+:))?([^:;；]+):([^;；]*)")
SERVICE = re.compile(r"(?:[發发寄]?[順顺][豐丰]|快[遞递]|物流|保[稅税](?:[倉仓])?|客服|[運运][費费]|包[郵邮]|[發发][貨货]|[貨货]到付款|旗[艦舰]店|天[貓猫]|[優优]惠|促[銷销]|[贈赠]品|送[貨货]上[門门]|入[會会])")
MERCH_PROMO = re.compile(
    r"(?:日期新[鮮鲜]|主[圖图]款|[推薦推荐]營養[組组]合|"
    r"\d+\s*倍代[謝谢]|成分升[級级]|體[態态][輕轻]盈|"
    r"[新新]客[推薦推荐]|限時|熱賣|爆款|價[效性]比|"
    r"送(?:品牌)?(?:搖|摇){2}杯|送禮|贈禮|"
    r"(?:濃度|配方)?升[級级]|高含量|黃金配比|黄金配比|"
    r"深度呵[護护]|日常[養养][護护]|長期囤貨|长期囤货|"
    r"多瓶更[划劃]算|更[划劃]算|囤[貨货]|大容量|"
    r"能(?:吃|用)\s*\d+\s*(?:個|个)?月|性[價价]比|實惠|实惠|超值|"
    r"喚醒|唤醒|熬夜急救|煥活氣色|焕活气色|週期[養养][護护]|"
    r"強健骨骼|强健骨骼|嘗鮮裝|尝鲜装)", re.I,
)
PHYSICAL = re.compile(r"\d+(?:\.\d+)?\s*(?:ml|kg|mg|g|l|片|枚|粒|瓶|盒|袋|包|罐|支|入|顆|颗)|乳|霜|餅|饼|糖|粉|巧克力", re.I)
BATCH = re.compile(r"(?:(?:新?至)|(?:(?:效期|到期|有效期)[:：]?(?:至|到)?))\s*(\d{2,4})年(\d{1,2})月(?:(\d{1,2})(?:日|號|号)?)?(?:中旬|上旬|下旬)?")
NON_PRODUCT_OPTION = re.compile(
    r"(?:下拉詳情|下拉详情|淘金幣|淘金币|入[會会]|勿拍|僅供說明|仅供说明|"
    r"[選选]哪款\s*看[這这]里|對照檢測|对照检测|成分含量檢測\s*看[這这]里|"
    r"成分含量检测\s*看[這这]里|三甲醫院專家推薦|三甲医院专家推荐|"
    r"[你您]體檢\s*我買單|[你您]体检\s*我买单|報銷檢測費用|报销检测费用|"
    r"無理由退|无理由退|官方正品|假一[罰罚]十|養腸好護肝|养肠好护肝)"
)


def sellable_source_skus(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the latest Apify SKU rows that are explicitly sellable.

    This is the single scope definition used by the operator queue, worker and
    rollout planner.  It deliberately does not manufacture a fallback SKU from
    a product-level price: the focused rollout contains only products whose
    latest authoritative supplier tree has exactly one real sellable SKU.
    """
    output: list[dict[str, Any]] = []
    for value in dict(raw or {}).get("skus") or []:
        if not isinstance(value, dict):
            continue
        try:
            available = value.get("quantity") is None or int(
                value.get("quantity") or 0) > 0
        except (TypeError, ValueError):
            available = True
        if available:
            output.append(dict(value))
    return output


def merchandising_scope_status(raw: dict[str, Any] | None) -> str:
    count = len(sellable_source_skus(raw))
    if count == 1:
        return "SINGLE_SKU_ACTIVE"
    if count > 1:
        return "MULTI_SKU_DEFERRED"
    return "ZERO_SKU_DEFERRED"


def resolve_property_image(sku_id: str, property_path: str, images: dict) -> tuple[str | None, str]:
    """Exact property token sets, never substring or fuzzy SKU matching."""
    if images.get(sku_id):
        return str(images[sku_id]), "SOURCE_SKU_ID_MAP"
    tokens = set(re.findall(r"-?\d+:-?\d+", property_path))
    matches = [(set(re.findall(r"-?\d+:-?\d+", str(key))), str(url))
               for key, url in images.items() if url]
    matches = [(key, url) for key, url in matches if key and key <= tokens]
    if not matches:
        return None, "SOURCE_IMAGE_MAP_ABSENT"
    width = max(len(key) for key, _ in matches)
    urls = {url for key, url in matches if len(key) == width}
    if len(urls) != 1:
        return None, "SOURCE_IMAGE_MAP_AMBIGUOUS"
    return urls.pop(), "SOURCE_PROPERTY_IMAGE_MAP"


def decode_axes(raw: Any) -> list[tuple[str, str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {"規格": raw}
    if not isinstance(raw, dict):
        return []
    result: list[tuple[str, str]] = []
    for key, value in raw.items():
        source = '' if value is None else str(value)
        if re.fullmatch(r"-?\d+:-?\d+:\s*", source):
            result.append((str(key), ""))
            continue
        # Only split semicolons when they introduce an actual property. Free
        # prose remains intact, including additional physical quantities.
        encoded = list(AXIS.finditer(source))
        if encoded and (re.match(r"^-?\d+:-?\d+:", source) or all(
                m.group(1).strip() in {"规格", "規格", "颜色分类", "顏色分類", "净含量", "淨含量",
                                     "香味", "口味", "型号", "型號", "尺寸", "尺码", "尺碼",
                                     "包装数", "包裝數", "版本", "颜色", "顏色", "材质", "材質"}
                for m in encoded)):
            result.extend((m.group(1).strip(), m.group(2).strip()) for m in encoded)
        else:
            result.append((str(key), source))
    return result


def historical_sku_image(sku_id: str, source_axes: Any, snapshots: list[dict]) -> tuple[str | None, dict]:
    """Recover an explicit old mapping only when all physical axes are unchanged.

    Callers scope snapshots to the exact product and accepted source identities.
    Price/stock always remain from the current response. Never match image order.
    """
    signature = sorted(decode_axes(source_axes))
    for snapshot in snapshots:
        raw = snapshot['raw']
        matches = [sku for sku in raw.get('skus', []) if str(sku.get('skuId')) == str(sku_id)]
        if len(matches) != 1:
            continue
        old = matches[0]
        if sorted(decode_axes({'規格': old.get('propsNames') or ''})) != signature:
            continue
        url = old.get('imageUrl') or old.get('image')
        binding = 'SOURCE_EXPLICIT_SKU_IMAGE'
        if not isinstance(url, str):
            url = None
        if not url:
            url, binding = resolve_property_image(
                sku_id,
                old.get('propsIds') or old.get('propsNames') or old.get('properties') or '',
                raw.get('propsImages') or raw.get('propImages') or {},
            )
        if url and url.startswith(('https://', 'http://', '//')):
            return ('https:' + url if url.startswith('//') else url.replace('http://', 'https://', 1)), {
                'source_observation_id': snapshot['source_observation_id'],
                'method': 'EXACT_SKU_UNCHANGED_AXES_HISTORICAL_MAP', 'binding': binding,
                'sku_id': str(sku_id), 'source_axes': signature}
    return None, {}


def consumer_text(value: Any) -> str:
    text = str(value or "")
    # Keep the physical bottle count while removing only the procurement
    # judgement wrapped around it.
    text = re.sub(r"([一兩两二三四五六七八九十\d]+\s*瓶)\s*囤[貨货]\s*[裝装]", r"\1裝", text)
    text = re.sub(r"(\d+\s*瓶)\s*大容量\s*[裝装]", r"\1", text)
    # Remove only service-bearing brackets, not adjacent physical options.
    text = re.sub(r"[【\[（(]([^】\]）)]*)[】\]）)]",
                  lambda m: " " if ((SERVICE.search(m.group(1)) or MERCH_PROMO.search(m.group(1)))
                                      and not PHYSICAL.search(m.group(1))) else m.group(0), text)
    text = BATCH.sub(" ", text)
    text = re.sub(r"[【\[（(]\s*(?:效期|到期|有效期)?\s*[】\]）)]", " ", text)
    text = re.sub(r"(?:[發发寄]?[順顺][豐丰](?:快[遞递])?|包[郵邮]|[發发][貨货]|保[稅税](?:[倉仓])?|送[貨货]上[門门]|可[選选]上[門门])", " ", text)
    text = re.sub(r"(?:均[價价]\s*\d+(?:\.\d+)?\s*/\s*[^\s;；]+|入[會会]再[減减][^;；。]*)", " ", text)
    text = re.sub(r"(?:如需|詳情|详情|請|请)?(?:聯繫|联系|諮詢|咨询)?客服[^;；。]*", " ", text)
    # Marketplace badges often wrap a real count (for example
    # ``【實惠裝200粒】``).  Remove the sales judgement but retain the physical
    # quantity that distinguishes the procurement SKU.
    text = re.sub(
        r"[【\[（(]\s*(?:升[級级]大瓶裝|升级大瓶装|實惠裝|实惠装|"
        r"日常[養养][護护]|長期囤貨|长期囤货)\s*"
        r"(\d+(?:\.\d+)?\s*(?:ml|g|kg|片|粒|瓶|袋|包|罐)(?:\s*[×xX*]\s*\d+)?)\s*[】\]）)]",
        r"\1", text, flags=re.I,
    )
    text = MERCH_PROMO.sub(" ", text)
    text = re.sub(r"[❤♥]+", " ", text)
    text = re.sub(r"(?<![A-Za-z])(\d+(?:\.\d+)?)\s*m\s*×\s*(\d+)(?![A-Za-z])", r"\1ml×\2", text, flags=re.I)
    text = re.sub(r"[✅✔☑]+", " ", text)
    # Removing a marketplace badge can leave punctuation-only option shells
    # such as ``【 】a2 milk``.  They carry no consumer meaning and should not
    # reach the Taiwan storefront.
    text = re.sub(r"[【\[（(]\s*[】\]）)]", " ", text)
    text = re.sub(r"(?<=\d)(片|粒|枚|瓶|盒|袋|包|罐|支)\s+(?=[\u4e00-\u9fff])", r"\1", text)
    text = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip(" ｜|/-_，,；;。")


def visual_variant_group_key(value: Any, product_title: Any = "") -> str:
    """Return the consumer-visible identity that actually needs a new image.

    Supplier SKUs often multiply one physical presentation into one/two/three
    bottle or different-capacity procurement choices.  Those choices still
    need distinct text and prices, but Taiwan consumers do not benefit from
    duplicate pictures.  Flavour, scent, colour, formula, form and model words
    remain in the key, so genuinely different-looking choices still require a
    representative image.
    """
    text = consumer_text(value)
    # Remove words already supplied by the SPU title (brand and shared product
    # subject).  They add no visual distinction between selectable options.
    for token in re.split(r"[\s／/｜|·,，;；:：()（）【】\[\]\-]+", consumer_text(product_title)):
        if len(token) >= 2 and not re.fullmatch(r"系列|商品|同款", token):
            text = re.sub(re.escape(token), " ", text, flags=re.I)
    title_text = consumer_text(product_title)
    shared_subject_synonyms = (
        (r"面霜|乳霜|潤膚霜|润肤霜", r"面霜|乳霜|潤膚霜|润肤霜"),
        (r"沐浴露|沐浴乳", r"沐浴露|沐浴乳"),
        (r"蛋白粉", r"(?:混合)?蛋白粉"),
    )
    for title_pattern, variant_pattern in shared_subject_synonyms:
        if re.search(title_pattern, title_text, flags=re.I):
            text = re.sub(variant_pattern, " ", text, flags=re.I)
    text = re.sub(
        r"(?:人氣推薦|人气推荐|經典款|经典款|美國產|美国产|無塑封|无塑封|"
        r"所有膚質|所有肤质|單一規格|单一规格|週期裝|周期装|家庭裝|家庭装)",
        " ", text, flags=re.I,
    )
    text = re.sub(r"\d+\s*(?:個|个)?月(?:用)?量", " ", text, flags=re.I)
    text = re.sub(r"[一二三四五六七八九十兩两]+\s*(?:瓶|盒|袋|包|罐|支|入|組|组|件)",
                  " ", text)
    text = re.sub(
        r"(?:共|總計|总计|淨重|净重|容量|規格|规格)?\s*"
        r"\d+(?:\.\d+)?\s*(?:ml|毫升|cl|dl|l|g|克|kg|公斤|mg|片|枚|粒|"
        r"瓶|盒|袋|包|罐|支|入|顆|颗|對|对|組|组|件)"
        r"(?:\s*[×xX*]\s*\d+\s*(?:瓶|盒|袋|包|罐|支|入|組|组|件)?)?",
        " ", text, flags=re.I,
    )
    text = re.sub(
        r"(?:單|单)?\s*\d+\s*(?:瓶|盒|袋|包|罐|支|入|組|组|件)\s*(?:裝|装)?",
        " ", text, flags=re.I,
    )
    text = re.sub(r"(?:[×xX*]\s*\d+|\d+\s*[×xX*])", " ", text)
    text = re.sub(r"(?:採購|采购)選項\s*\d+", " ", text)
    # A few supplier option strings contain a malformed or non-ASCII final
    # litre character.  After unit removal this can leave a meaningless
    # ``500m`` fragment; it is still quantity, never a visual identity.
    text = re.sub(r"\b\d+(?:\.\d+)?\s*m\b", " ", text, flags=re.I)
    text = re.sub(r"(?:瓶|盒|袋|包|罐|支|組|组|件)?\s*(?:裝|装)(?![\u4e00-\u9fff])", " ", text)
    text = re.sub(r"(?<![\u4e00-\u9fff])(?:瓶|盒|袋|包|罐|支|入|組|组|件)(?![\u4e00-\u9fff])", " ", text)
    text = re.sub(r"[【】\[\]（）()｜|/／,_，;；·+＋×xX*\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip().casefold()
    text = re.sub(r"(?:口味|味|花香|香型)$", "", text).strip()
    if text:
        return text
    # An empty variant label means only count/capacity changed.  One clean SPU
    # image is therefore a truthful representation for every such option.
    return "__same_product__"


def visual_variant_groups(variants: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in variants:
        variant_key = str(row.get("variant_key") or "")
        if not variant_key:
            continue
        key = str(row.get("visual_group_key") or visual_variant_group_key(
            row.get("title") or row.get("normalized_variant_title") or "",
            row.get("product_title") or "",
        ))
        groups.setdefault(key, []).append(variant_key)
    return groups


def visual_media_coverage(variants: list[dict[str, Any]], media: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate operator-useful coverage without inventing an image obligation.

    Apify is authoritative about whether a purchasable SKU has a directly bound
    image.  A directly bound image must survive localization.  If Apify did not
    provide one, the Taiwan draft may truthfully use a shared SPU image or a
    clear text option; that absence must not quarantine the whole product.
    Quantity-only variants intentionally share one visual group.
    """
    groups = visual_variant_groups(variants)
    if not variants:
        return {"groups": groups, "required_groups": set(), "covered_groups": set(),
                "missing_groups": set(), "missing_required_groups": set(),
                "missing_variant_keys": [], "missing_required_variant_keys": []}
    bound_keys = {str(row.get("variant_key") or "") for row in media if row.get("variant_key")}
    covered = {group for group, keys in groups.items() if any(key in bound_keys for key in keys)}
    # One physical visual identity may truthfully reuse the approved SPU image.
    if len(groups) == 1 and media:
        covered.update(groups)
    missing = set(groups) - covered
    required = {
        group for group, keys in groups.items()
        if any(
            str(row.get("variant_key") or "") in keys
            and bool(row.get("source_image_url"))
            and str(row.get("source_media_binding") or "").upper() in {
                "SOURCE_SKU_ID_MAP", "SOURCE_PROPERTY_IMAGE_MAP",
                "SOURCE_EXPLICIT_SKU_IMAGE", "SKU_SPECIFIC_SOURCE",
            }
            for row in variants
        )
    }
    missing_required = required - covered
    return {
        "groups": groups, "required_groups": required,
        "covered_groups": covered, "missing_groups": missing,
        "missing_required_groups": missing_required,
        "missing_variant_keys": [keys[0] for group, keys in groups.items() if group in missing],
        "missing_required_variant_keys": [
            keys[0] for group, keys in groups.items() if group in missing_required
        ],
    }


def publication_variant_projection(
    variants: list[dict[str, Any]], media: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive a safe storefront subset without changing supplier evidence.

    Apify's current sellable SKU matrix remains lossless in ``source_variants``.
    This projection only decides which rows are sufficiently complete for a
    Taiwan listing.  Quantity/package choices that share one visual identity
    may share one approved image.  Genuinely different flavours, colours,
    formulas or models require a trusted image for their visual group; an
    incomplete group is withheld rather than blocking complete sibling SKUs.
    """
    groups = visual_variant_groups(variants)
    group_by_variant = {
        variant_key: group for group, keys in groups.items() for variant_key in keys
    }
    bound_keys = {
        str(row.get("variant_key") or "") for row in media if row.get("variant_key")
    }
    bound_sku_ids = {
        str(row.get("platform_sku_id") or "") for row in media
        if row.get("platform_sku_id")
    }
    bound_source_urls = {
        str(row.get("source_url") or "") for row in media if row.get("source_url")
    }
    covered_groups = {
        group for group, keys in groups.items() if any(key in bound_keys for key in keys)
    }
    # A single physical presentation does not need duplicate per-SKU media.
    if len(groups) <= 1 and media:
        covered_groups.update(groups)

    def pack_count(row: dict[str, Any]) -> int:
        text = consumer_text(" ".join([
            str(row.get("title") or ""),
            *[str(value) for value in (row.get("options") or {}).values()],
        ]))
        explicit = re.search(r"(?<!\d)(\d+)\s*瓶(?:裝|装)?", text, re.I)
        if explicit:
            return max(1, int(explicit.group(1)))
        multiplied = re.search(
            r"(?:ml|g|kg|mg|片|粒|支|袋|包|盒|罐)\s*[×xX*]\s*(\d+)"
            r"(?=\s*(?:瓶|袋|包|盒|罐|桶|入|$|[（(]))",
            text, re.I,
        )
        return max(1, int(multiplied.group(1))) if multiplied else 1

    price_floor_by_group: dict[str, tuple[int, float]] = {}
    for row in variants:
        key = str(row.get("variant_key") or "")
        group = group_by_variant.get(key, "")
        count = pack_count(row)
        price = row.get("procurement_price_cny")
        if not group or not isinstance(price, (int, float)):
            continue
        current = price_floor_by_group.get(group)
        if current is None or count < current[0] or (count == current[0] and float(price) < current[1]):
            price_floor_by_group[group] = (count, float(price))

    published: list[dict[str, Any]] = []
    withheld: list[dict[str, Any]] = []
    for source in variants:
        row = dict(source)
        key = str(row.get("variant_key") or "")
        gaps: list[str] = []
        if not str(row.get("title") or "").strip():
            gaps.append("CONSUMER_SKU_TEXT_MISSING")
        if row.get("specification_status") == "LLM_RESOLUTION_REQUIRED":
            gaps.append("CONSUMER_SKU_TEXT_UNRESOLVED")
        if row.get("consumer_option_collision"):
            gaps.append("CONSUMER_SKU_OPTION_COLLISION")
        if row.get("procurement_price_cny") is None or row.get("shopify_price_twd") is None:
            gaps.append("SKU_PRICE_MISSING")
        if any(row.get(field) is None for field in (
            "reference_weight_g", "predicted_weight_p50_g",
            "predicted_weight_p75_g", "predicted_weight_p90_g",
        )):
            gaps.append("SKU_WEIGHT_MISSING")
        if row.get("source_availability_verification") == "REQUIRED":
            gaps.append("SOURCE_AVAILABILITY_RECHECK_REQUIRED")
        group = group_by_variant.get(key)
        count = pack_count(row)
        floor = price_floor_by_group.get(group or "")
        price = row.get("procurement_price_cny")
        if (floor and count > floor[0] and isinstance(price, (int, float))
                and float(price) <= floor[1]):
            gaps.append("PACK_PRICE_NON_MONOTONIC")
            row["pack_price_reference"] = {
                "pack_count": floor[0], "procurement_price_cny": floor[1],
            }
        row["pack_count"] = count
        if len(groups) > 1:
            # Dedicated Apify SKU media is stronger than a visual-group
            # heuristic. A sibling image must not silently satisfy it.
            has_dedicated_source = bool(row.get("source_image_url"))
            exact_media = bool(
                key in bound_keys
                or (row.get("platform_sku_id") and
                    str(row.get("platform_sku_id")) in bound_sku_ids)
                or (row.get("source_image_url") and
                    str(row.get("source_image_url")) in bound_source_urls)
            )
            if ((has_dedicated_source and not exact_media)
                    or (not has_dedicated_source and group not in covered_groups)):
                gaps.append("DISTINCT_SKU_IMAGE_NOT_LOCALIZED")
        row["publication_status"] = "WITHHELD" if gaps else "READY"
        row["publication_hold_reasons"] = gaps
        row["visual_group_key"] = group
        (withheld if gaps else published).append(row)
    return {
        "source_variants": [dict(row) for row in variants],
        "published_variants": published,
        "withheld_variants": withheld,
        "visual_groups": groups,
        "covered_visual_groups": sorted(covered_groups),
        "partial_publication": bool(published and withheld),
    }


def pure_service(raw: Any) -> bool:
    axes = decode_axes(raw)
    # Some sellers attach a common capacity axis even to a return-policy
    # option. That static capacity does not turn the instruction into goods.
    if any(re.search(r'運費險|运费险|無理由退換貨|无理由退换货|勿拍|僅供說明|仅供说明', value)
           and not PHYSICAL.search(consumer_text(value)) for _, value in axes):
        return True
    text = " ".join(value for _, value in axes)
    if NON_PRODUCT_OPTION.search(text) and not PHYSICAL.search(consumer_text(text)):
        return True
    return bool(SERVICE.search(text) and not PHYSICAL.search(consumer_text(text)))


def batch_expiry(raw: Any, today: date | None = None) -> dict:
    """Only explicit expiry phrases qualify; month-only claims use month end."""
    text = " ".join(value for _, value in decode_axes(raw))
    facts = []
    for match in BATCH.finditer(text):
        year, month, day = match.groups()
        year = int(year) + (2000 if len(year) == 2 else 0)
        try:
            end = date(year, int(month), int(day) if day else calendar.monthrange(year, int(month))[1])
        except ValueError:
            continue
        facts.append({"source_text": match.group(0), "expiry_latest_date": end.isoformat(),
                      "expired": end < (today or date.today())})
    # Conflicting batch assertions require review, never choose a convenient one.
    return {"claims": facts, "expired": bool(facts) and all(f["expired"] for f in facts),
            "conflicting": len({f["expired"] for f in facts}) > 1}


def choose_consumer_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse only identical full matrices; carry the selected source atomically.

    The caller has already excluded unbuyable/unpriced offers. Different titles
    are never fuzzy-matched here, and all alternatives remain visible backstage.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        options = row.get("normalized_options") or row.get("normalized_options_json") or row.get("option_values") or row.get("option_values_json") or {}
        if isinstance(options, str):
            options = json.loads(options)
        key = json.dumps(options or {"規格": row.get("normalized_variant_title")}, sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, []).append(row)
    selected = []
    for alternatives in groups.values():
        if len(alternatives) > 1:
            raw_texts = [" ".join(value for _, value in decode_axes(
                row.get("option_values_json") or row.get("option_values") or {})) for row in alternatives]
            delivery_duplicate = any(re.search(r"[順顺][豐丰]|快[遞递]|包[郵邮]", value) for value in raw_texts)
            visually_verified_same = all(
                (json.loads(row.get("formula_json") or "{}")
                 .get("consumer_identity_evidence") or {}).get("status") == "PASS"
                for row in alternatives
            ) and len({
                (json.loads(row.get("formula_json") or "{}")
                 .get("consumer_identity_evidence") or {}).get("label")
                for row in alternatives
            }) == 1
            fingerprints = {consumer_text(value) for value in raw_texts}
            # Identical cleaned names alone are NOT proof of physical equality.
            # Decimal-coded capacity options and lost scent axes must surface.
            # A high-confidence, per-SKU visual/OCR decision is stronger than
            # syntactic marketplace differences such as ``40ml`` versus
            # ``40.0ml``.  When every row is proved to be the same consumer
            # style, collapse them into procurement alternatives.  Delivery
            # deduplication remains conservative and still requires identical
            # cleaned source text.
            exact_same_product_offer = (
                len(fingerprints) == 1 and all(raw_texts)
                and len({str(row.get("platform_product_id") or "") for row in alternatives}) == 1
                and bool(str(alternatives[0].get("platform_product_id") or ""))
            )
            can_collapse = visually_verified_same or exact_same_product_offer or (
                delivery_duplicate and len(fingerprints) == 1 and all(raw_texts)
            )
            if not can_collapse:
                for row in alternatives:
                    collision = dict(row)
                    collision["consumer_name_collision"] = True
                    selected.append(collision)
                continue
        def rank(row: dict[str, Any]) -> tuple:
            price = row.get("procurement_price_cny", row.get("procurement_price"))
            return (row.get("price_status", row.get("quote_status")) != "READY",
                    float(price) if price is not None else float("inf"),
                    str(row.get("platform_product_id")), str(row.get("platform_sku_id")))
        winner = dict(min(alternatives, key=rank))
        if len(alternatives) == 1:
            selected.append(winner)
            continue
        winner["procurement_alternatives"] = [
            {k: row.get(k) for k in ("platform_product_id", "platform_sku_id", "variant_key",
                                    "procurement_price_cny", "procurement_price", "image_url")}
            for row in alternatives if row is not min(alternatives, key=rank)]
        selected.append(winner)
    return selected


def project_shopify_options(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Map actual supplier combinations into Shopify's three-axis model.

    The lossless source contract is not changed. Shared matrices with at most
    three axes keep their axes. Heterogeneous or wider matrices become one
    combined ``款式／規格`` option, one value per actual SKU. This avoids both
    fabricated Cartesian combinations and consumer-visible placeholder values.
    """
    parsed: list[tuple[dict[str, Any], dict[str, str]]] = []
    option_names: list[str] = []
    for row in rows:
        options = (row.get("normalized_options") or
                   row.get("normalized_options_json") or {})
        if isinstance(options, str):
            options = json.loads(options)
        clean = {str(name): consumer_text(value) for name, value in (options or {}).items()
                 if consumer_text(value)}
        parsed.append((row, clean))
        for name in clean:
            if name not in option_names:
                option_names.append(name)

    shared_axes = bool(parsed) and all(set(options) == set(option_names)
                                       for _, options in parsed)
    source_axis_count = len({name for _, options in parsed for name in options})
    collapse = not shared_axes or len(option_names) > 3
    # Common marketplace pattern: one bottle, two bottles, three bottles are
    # encoded under different source axes (sometimes even under ``香味``).
    # When every actual row has the same per-bottle capacity and a unique pack
    # count, expose one clean package option instead of repeating sales copy.
    pack_projection: list[tuple[dict[str, Any], dict[str, str]]] = []
    capacities = {options.get("淨含量") for _, options in parsed if options.get("淨含量")}
    pack_counts: list[int] = []
    if collapse and len(capacities) == 1 and len(parsed) > 1:
        for row, options in parsed:
            text = " ".join([str(row.get("normalized_variant_title") or ""), *options.values()])
            match = re.search(r"(?<!\d)(\d+)\s*瓶", text)
            pack_counts.append(int(match.group(1)) if match else 1)
        if len(set(pack_counts)) == len(parsed):
            capacity = next(iter(capacities))
            pack_projection = [
                (row, {"包裝規格": f"{count}瓶｜{capacity}/瓶"})
                for (row, _), count in zip(parsed, pack_counts)
            ]
    if pack_projection:
        option_names = ["包裝規格"]
        projected = pack_projection
        collapse = True
    elif collapse:
        option_names = ["款式／規格"]
        projected = []
        for row, options in parsed:
            title = consumer_text(row.get("normalized_variant_title"))
            if not title:
                title = "｜".join(value for value in options.values() if value)
            projected.append((row, {"款式／規格": title}))
    else:
        projected = parsed

    signatures = [json.dumps(options, ensure_ascii=False, sort_keys=True)
                  for _, options in projected]
    return {
        "option_names": option_names,
        "rows": projected,
        "collapsed": collapse,
        "source_axes_consistent": shared_axes,
        "source_axis_count": source_axis_count,
        "collision": len(signatures) != len(set(signatures)),
    }
