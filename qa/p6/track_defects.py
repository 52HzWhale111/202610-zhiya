"""生成 P6 缺陷跟踪表。

输入：
  defects_manual.json        人工确认的缺陷与缺口
  reports/acceptance-latest.json  最近一次黑盒验收结果

输出：
  reports/defect-ledger.md / defect-ledger.csv

自动失败会自动生成候选缺陷；已由人工缺陷覆盖的 BLOCKED/WARN 不重复造单。
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "qa" / "p6" / "reports"
DEFAULT_MANUAL = Path(__file__).with_name("defects_manual.json")

SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
STATUS_ORDER = {"待修复": 0, "修复中": 1, "待验证": 2, "已修复": 3, "已验证": 4, "不修复": 5}


def load_manual(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    defects = payload.get("defects") or []
    for defect in defects:
        for field in ("id", "title", "severity", "category", "ac", "status", "owner"):
            if field not in defect:
                raise SystemExit(f"缺陷 {defect.get('id', '?')} 缺少字段 {field}")
        defect.setdefault("relatedCases", [])
    return defects


def load_acceptance(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def auto_defects(acceptance: dict[str, Any] | None, existing: list[dict]) -> list[dict]:
    if not acceptance:
        return []
    covered = {case for defect in existing for case in defect.get("relatedCases", [])}
    created = []
    for result in acceptance.get("results", []):
        case_id = result.get("caseId") or result.get("case_id") or "UNKNOWN"
        if result.get("status") != "FAIL" or case_id in covered:
            continue
        created.append({
            "id": f"DEF-P6-AUTO-{case_id.split('-')[-1]}",
            "title": f"自动验收失败：{result.get('title', '')}",
            "severity": "P1",
            "category": "自动化发现",
            "ac": result.get("ac", ""),
            "status": "待修复",
            "owner": "待指派",
            "foundAt": acceptance.get("meta", {}).get("finishedAt", ""),
            "environment": acceptance.get("meta", {}).get("baseUrl", ""),
            "description": result.get("detail", ""),
            "reproduce": f"运行 qa/p6/run_acceptance.py，用例 {case_id}",
            "expected": "用例通过",
            "actual": result.get("detail", ""),
            "evidence": json.dumps(result.get("evidence", {}), ensure_ascii=False)[:500],
            "fixVersion": "待定",
            "relatedCases": [case_id],
        })
    return created


def write_outputs(defects: list[dict], acceptance: dict | None, outdir: Path) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    defects = sorted(defects, key=lambda d: (SEVERITY_ORDER.get(d["severity"], 9),
                                             STATUS_ORDER.get(d["status"], 9), d["id"]))
    counts = Counter(defect["severity"] for defect in defects)
    status_counts = Counter(defect["status"] for defect in defects)
    status_by_case = {}
    if acceptance:
        status_by_case = {
            (item.get("caseId") or item.get("case_id")): item["status"]
            for item in acceptance.get("results", [])
        }

    csv_path = outdir / "defect-ledger.csv"
    fields = ["id", "title", "severity", "category", "ac", "status", "owner", "foundAt",
              "environment", "description", "reproduce", "expected", "actual", "evidence",
              "fixVersion", "relatedCases"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for defect in defects:
            row = {key: defect.get(key, "") for key in fields}
            row["relatedCases"] = ",".join(defect.get("relatedCases", []))
            writer.writerow(row)

    lines = [
        "# 知芽 P6 缺陷跟踪表",
        "",
        f"- 缺陷总数：{len(defects)}",
        "- 等级分布：" + "，".join(f"{level} {counts.get(level, 0)}" for level in ("P0", "P1", "P2")),
        "- 状态分布：" + "，".join(f"{status} {count}" for status, count in status_counts.items()),
        f"- 关联自动验收：{'acceptance-latest.json' if acceptance else '未运行'}",
        "",
        "> 缺陷编号 DEF-P6-xxx 为本轮测试建立；P0 = 阻断核心流程/严重越权，"
        "P1 = 重要功能失效但有回退，P2 = 文案、兼容性或工程细节。"
        "“待修复”表示本轮已确认但尚无修复版本，不等于已验收通过。",
        "",
        "## 缺陷清单",
        "",
        "| 编号 | 等级 | 标题 | 验收项 | 状态 | 负责人 | 关联用例 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for defect in defects:
        cases = defect.get("relatedCases", [])
        case_text = "、".join(
            f"{case}({status_by_case.get(case, '未运行')})" for case in cases
        ) or "-"
        lines.append(f"| {defect['id']} | {defect['severity']} | {defect['title']} | "
                     f"{defect['ac']} | {defect['status']} | {defect['owner']} | {case_text} |")

    lines += ["", "## 缺陷详情", ""]
    for defect in defects:
        lines += [
            f"### {defect['id']} {defect['title']}",
            "",
            f"- 等级 / 类别：{defect['severity']} / {defect['category']}",
            f"- 关联验收项：{defect['ac']}",
            f"- 状态 / 负责人：{defect['status']} / {defect['owner']}",
            f"- 发现日期 / 环境：{defect.get('foundAt', '')} / {defect.get('environment', '')}",
            f"- 复现步骤：{defect.get('reproduce', '')}",
            f"- 预期结果：{defect.get('expected', '')}",
            f"- 实际结果：{defect.get('actual', '')}",
            f"- 证据：{defect.get('evidence', '')}",
            f"- 修复版本：{defect.get('fixVersion', '待定')}",
            "",
        ]
    md_path = outdir / "defect-ledger.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, csv_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manual", type=Path, default=DEFAULT_MANUAL)
    parser.add_argument("--acceptance", type=Path, default=DEFAULT_OUT / "acceptance-latest.json")
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    manual = load_manual(args.manual)
    acceptance = load_acceptance(args.acceptance)
    defects = manual + auto_defects(acceptance, manual)
    md_path, csv_path = write_outputs(defects, acceptance, args.outdir)
    print(f"缺陷 {len(defects)} 条：{md_path}")
    print(f"CSV：{csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
