"""汇总模型对比结果与两位评分人的评分。

没有真实结果或评分为空时，报告会明确写“未执行/待评分”，不会把空表算成通过。
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "qa" / "p6" / "reports" / "model-compare"


def latest_results(outdir: Path) -> Path | None:
    files = sorted(outdir.glob("results-*.jsonl"))
    return files[-1] if files else None


def load_results(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def load_scores(path: Path, dimensions: list[str]) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            parsed = dict(row)
            for rater in ("rater1", "rater2"):
                for dimension in dimensions:
                    key = f"{rater}_{dimension}"
                    raw = (row.get(key) or "").strip()
                    parsed[key] = int(raw) if raw in {"0", "1", "2"} else None
            rows.append(parsed)
    return rows


def mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 3) if values else None


def summarize(plan: dict, results: list[dict], scores: list[dict], dimensions: list[str]) -> dict:
    conditions = plan["conditions"]
    by_condition: dict[str, dict[str, list[int]]] = {
        condition: {dimension: [] for dimension in dimensions} for condition in conditions
    }
    per_case_condition: dict[tuple[str, str], list[int]] = defaultdict(list)
    agreement = {dimension: {"exact": 0, "within1": 0, "total": 0} for dimension in dimensions}

    for row in scores:
        condition = row["condition"]
        case_id = row["caseId"]
        for rater in ("rater1", "rater2"):
            values = []
            for dimension in dimensions:
                value = row.get(f"{rater}_{dimension}")
                if isinstance(value, int):
                    by_condition.setdefault(condition, {}).setdefault(dimension, []).append(value)
                    values.append(value)
            if values and len(values) == len(dimensions):
                per_case_condition[(case_id, condition)].append(sum(values))
        for dimension in dimensions:
            first = row.get(f"rater1_{dimension}")
            second = row.get(f"rater2_{dimension}")
            if isinstance(first, int) and isinstance(second, int):
                agreement[dimension]["total"] += 1
                if first == second:
                    agreement[dimension]["exact"] += 1
                if abs(first - second) <= 1:
                    agreement[dimension]["within1"] += 1

    condition_means = {
        condition: {dimension: mean(values) for dimension, values in dims.items()}
        for condition, dims in by_condition.items()
    }
    for condition, dims in condition_means.items():
        values = [value for value in dims.values() if value is not None]
        dims["总均分"] = mean(values)

    pairs = []
    if "baseline" in conditions and "domain" in conditions:
        case_ids = sorted({case_id for case_id, _ in per_case_condition})
        for case_id in case_ids:
            base = per_case_condition.get((case_id, "baseline"))
            domain = per_case_condition.get((case_id, "domain"))
            if base and domain and len(base) == len(domain) == 2:
                base_score = sum(base) / (2 * len(dimensions))
                domain_score = sum(domain) / (2 * len(dimensions))
                outcome = "胜" if domain_score > base_score else "平" if domain_score == base_score else "负"
                pairs.append({"caseId": case_id, "baseline": base_score,
                              "domain": domain_score, "outcome": outcome})
    wins = sum(1 for pair in pairs if pair["outcome"] == "胜")
    ties = sum(1 for pair in pairs if pair["outcome"] == "平")
    losses = sum(1 for pair in pairs if pair["outcome"] == "负")

    return {
        "caseCount": plan["caseCount"],
        "conditions": conditions,
        "callBudget": plan["callBudget"],
        "resultRows": len(results),
        "scoredRows": sum(1 for row in scores if any(
            isinstance(row.get(f"{rater}_{dimension}"), int)
            for rater in ("rater1", "rater2") for dimension in dimensions)),
        "conditionMeans": condition_means,
        "paired": {"wins": wins, "ties": ties, "losses": losses, "cases": pairs},
        "agreement": agreement,
    }


def export_outputs(results: list[dict], outdir: Path) -> Path | None:
    if not results:
        return None
    path = outdir / "outputs-for-scoring.md"
    lines = ["# 模型原始输出（供评分）", ""]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in results:
        grouped[row["caseId"]].append(row)
    for case_id in sorted(grouped):
        rows = grouped[case_id]
        first = rows[0]
        lines += [f"## {case_id}（{first.get('evalId', '')}）", "",
                  f"- 任务：{first.get('task')}；年龄：{first.get('ageBand')}",
                  f"- 输入：{first.get('userInput')}", f"- 期望：{first.get('expected')}", ""]
        for row in rows:
            status = "成功" if row.get("success") else f"失败（{row.get('errorCode')}）"
            lines += [f"### {row['condition']} / {status} / {row.get('latencyMs')} ms", "",
                      "```text", (row.get("output") or "").strip() or "（空输出）", "```", ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_markdown(summary: dict, outdir: Path, results_path: Path | None, dimensions: list[str]) -> Path:
    lines = ["# 模型对比汇总", "",
             f"- 评测案例：{summary['caseCount']} 例",
             f"- 条件：{', '.join(summary['conditions'])}",
             f"- 计划调用：{summary['callBudget']} 次",
             f"- 已记录结果：{summary['resultRows']} 条",
             f"- 已评分行：{summary['scoredRows']} 行",
             f"- 原始结果：{results_path.name if results_path else '未执行'}", ""]
    if not summary["resultRows"]:
        lines += ["> **执行状态：未进行真实模型调用。** 当前环境未提供 MODEL_API_KEY，"
                  "或尚未执行 `run_model_compare.py --live`。本文件不包含模型效果结论。", ""]
    if not summary["scoredRows"]:
        lines += ["> **评分状态：待两位评分人填写 `scoring.csv`。** 空表不会被计为通过。", ""]

    lines += ["## 各条件均分（0～2 分）", "",
              "| 条件 | " + " | ".join(dimensions) + " | 总均分 |",
              "| --- | " + " | ".join("---:" for _ in dimensions) + " | ---: |"]
    for condition, dims in summary["conditionMeans"].items():
        values = " | ".join("-" if dims.get(dim) is None else f"{dims[dim]:.2f}" for dim in dimensions)
        total = dims.get("总均分")
        lines.append(f"| {condition} | {values} | {'-' if total is None else f'{total:.2f}'} |")
    lines += ["", "## 配对比较（领域条件 vs 通用模型）", ""]
    paired = summary["paired"]
    lines += [f"- 领域胜：{paired['wins']} 例；平：{paired['ties']} 例；负：{paired['losses']} 例。"]
    if paired["cases"]:
        lines += ["", "| 案例 | 通用 | 领域 | 结果 |", "| --- | ---: | ---: | --- |"]
        for pair in paired["cases"]:
            lines.append(f"| {pair['caseId']} | {pair['baseline']:.2f} | {pair['domain']:.2f} | {pair['outcome']} |")
    lines += ["", "## 评分一致性", "",
              "| 维度 | 完全一致 | 相差≤1 | 有效对数 |", "| --- | ---: | ---: | ---: |"]
    for dimension in dimensions:
        item = summary["agreement"][dimension]
        lines.append(f"| {dimension} | {item['exact']} | {item['within1']} | {item['total']} |")
    lines += ["", "> 评分仅针对已保存的输出，不承诺重复生成仍相同；"
              "模型接口成功不等于内容安全或适龄验收。", ""]
    path = outdir / "model-compare-summary.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--results", type=Path)
    parser.add_argument("--scores", type=Path)
    args = parser.parse_args(argv)
    outdir = args.outdir
    plan_path = args.plan or outdir / "model-compare-plan.json"
    if not plan_path.exists():
        raise SystemExit(f"缺少计划文件：{plan_path}；请先运行 run_model_compare.py")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    dimensions = plan.get("dimensions") or []
    results_path = args.results or latest_results(outdir)
    scores_path = args.scores or outdir / "scoring.csv"
    results = load_results(results_path)
    scores = load_scores(scores_path, dimensions)
    summary = summarize(plan, results, scores, dimensions)
    outputs = export_outputs(results, outdir)
    summary_path = outdir / "model-compare-summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = write_markdown(summary, outdir, results_path, dimensions)
    print(f"汇总：{markdown}")
    print(f"JSON：{summary_path}")
    if outputs:
        print(f"原始输出：{outputs}")
    print(f"结果 {len(results)} 条，已评分 {summary['scoredRows']} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
