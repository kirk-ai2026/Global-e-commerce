from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

from .core import digest


def export(store, fid, destination: Path, job_id=None):
    destination.mkdir(parents=True, exist_ok=True)
    with store.connect() as c:
        freeze = c.execute(
            "SELECT * FROM lulu_freeze WHERE freeze_id=%s", (fid,)
        ).fetchone()
        rows = c.execute(
            "SELECT * FROM lulu_product WHERE freeze_id=%s ORDER BY product_id", (fid,)
        ).fetchall()
        if job_id:
            selected = {
                r["product_id"]
                for r in c.execute(
                    "SELECT product_id FROM lulu_task WHERE job_id=%s", (job_id,)
                )
            }
            rows = [r for r in rows if r["product_id"] in selected]
    counts = Counter(r["status"] for r in rows)
    scope = Counter(r["scope_status"] for r in rows)
    issues = Counter()
    reused = set()
    sku_count = 0
    with (
        (destination / "products.jsonl").open("w") as full,
        (destination / "issues.jsonl").open("w") as gaps,
        (destination / "products.csv").open("w", newline="") as cf,
    ):
        writer = csv.DictWriter(
            cf,
            fieldnames=[
                "product_id",
                "global_spu_id",
                "scope",
                "status",
                "brand",
                "brand_region",
                "category",
                "title",
                "source_skus",
                "content_skus",
                "withheld_skus",
                "observed_at",
                "issues",
            ],
        )
        writer.writeheader()
        for row in rows:
            f = row["facts"]
            r = row.get("result") or {}
            content = r.get("content") or {}
            full.write(json.dumps({"facts": f, "result": r}, ensure_ascii=False) + "\n")
            problems = r.get("issues") or f.get("scope_reasons") or []
            issues.update(problems)
            if (
                r.get("status") == "NEEDS_EVIDENCE"
                or row["scope_status"] == "SCOPE_REVIEW"
                or r.get("withheld_skus")
            ):
                gaps.write(
                    json.dumps(
                        {
                            "product_id": row["product_id"],
                            "scope": row["scope_status"],
                            "status": row["status"],
                            "issues": problems,
                            "remediation": r.get("remediation", []),
                            "withheld_skus": r.get("withheld_skus", []),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            writer.writerow(
                {
                    "product_id": row["product_id"],
                    "global_spu_id": f.get("global_spu_id"),
                    "scope": row["scope_status"],
                    "status": row["status"],
                    "brand": f.get("brand"),
                    "brand_region": f.get("brand_region", {}).get("code"),
                    "category": f.get("category"),
                    "title": content.get("title") or f.get("title"),
                    "source_skus": len(f.get("source_skus", [])),
                    "content_skus": len(r.get("content_skus", [])),
                    "withheld_skus": len(r.get("withheld_skus", [])),
                    "observed_at": f.get("observed_at"),
                    "issues": "|".join(problems),
                }
            )
            reused.update(a["sha256"] for a in r.get("media", []))
            sku_count += len(r.get("content_skus", []))
    audit = {
        "freeze_id": fid,
        "job_id": job_id,
        "products": len(rows),
        "scope_counts": dict(scope),
        "status_counts": dict(counts),
        "included_status_counts": dict(
            Counter(r["status"] for r in rows if r["scope_status"] == "INCLUDED")
        ),
        "completed_content_skus": sku_count,
        "reused_media_files": len(reused),
        "issue_counts": dict(issues),
        "processed_products": sum(r["status"] != "PENDING" for r in rows),
        "paid_calls": 0,
        "shopify_writes": 0,
        "source_audit": freeze["audit"],
        "output_files": ["products.jsonl", "products.csv", "issues.jsonl"],
        "result_digest": digest([(r["product_id"], r.get("result")) for r in rows]),
    }
    (destination / "audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2)
    )
    text = f"# Lulu 内部商品成品报告\n\n冻结清单：`{fid}`\n\n本报告仅包含内部成品与问题清单，Shopify 写入为 0。\n\n"
    text += "| 状态 | 商品数 |\n|---|---:|\n" + "".join(
        f"| {k} | {v} |\n" for k, v in sorted(counts.items())
    )
    text += "\n`CONTENT_READY` 是通过内容和媒体校验的内部成品；`NEEDS_EVIDENCE` 未计入完成；`EXCLUDED` 保留范围或停售原因。\n\n"
    text += f"合格成品 SKU：{sku_count}；复用媒体文件：{len(reused)}；付费调用：0。\n\n源采购价和库存带历史观察时间，CAD 售价和可购买资格未配置。\n"
    (destination / "REPORT.md").write_text(text)
    return audit
