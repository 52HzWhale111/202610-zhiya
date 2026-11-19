"""依据同一份黑盒验收记录生成 P6 测试文档。"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
TEMPLATE = ROOT / "docs" / "testing" / "p6" / "P6-测试文档-模板.md"
OUTPUT_MD = ROOT / "docs" / "testing" / "p6" / "P6-测试文档-完整.md"
OUTPUT_DOCX = ROOT / "docs" / "testing" / "p6" / "知芽-P6-软件测试文档-刘俊延-102401133.docx"

MODULE_DESCRIPTIONS = OrderedDict([
    ("ENV", "协议、地址与测试配置"),
    ("API", "健康检查、错误结构、requestId、契约覆盖"),
    ("AUTH", "注册、登录、退出、Cookie、CSRF、限流、跨账号"),
    ("PROF", "档案创建、修改、校验、隔离"),
    ("BOOK", "绘本列表、分页、筛选、详情、越界"),
    ("SESS", "会话创建、开始、幂等、翻页、结束、历史、删除"),
    ("CHILD", "儿童模式授权、范围、家长解锁、旧凭证"),
    ("VOICE", "语音鉴权、CSRF、格式、未配置回退"),
    ("NFR", "响应时间抽测"),
    ("BRANCH", "受控分支、重放和冲突"),
    ("REC", "绘本推荐与理由"),
    ("SEC", "安全响应头"),
    ("FE", "前端页面可访问"),
    ("PUB", "公网 HTTPS、证书、跳转"),
    ("AGENT", "情境提问、引导、原创故事、分支"),
    ("ACT", "亲子活动材料与步骤"),
    ("REPORT", "小结生成状态"),
])


def statuses(results: list[dict]) -> str:
    counts: dict[str, int] = {}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return "、".join(f"{key} {value}" for key, value in counts.items())


def case_module(item: dict) -> str:
    parts = item["case_id"].split("-")
    return parts[2] if len(parts) > 2 else "OTHER"


def module_table(results: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for item in results:
        module = case_module(item)
        groups.setdefault(module, []).append(item)
    lines = ["| 模块 | 用例数 | 覆盖内容 | 结果 |", "| --- | ---: | --- | --- |"]
    for module, items in groups.items():
        lines.append(f"| {module} | {len(items)} | {MODULE_DESCRIPTIONS.get(module, '')} | {statuses(items)} |")
    return "\n".join(lines)


def detail_table(results: list[dict]) -> str:
    lines = ["| 用例编号 | 验收项 | 用例名称 | 结果 | 说明 |", "| --- | --- | --- | --- | --- |"]
    for item in results:
        detail = item["detail"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['case_id']} | {item['ac']} | {item['title']} | {item['status']} | {detail} |")
    return "\n".join(lines)


STATES = {"PASS": "通过", "FAIL": "失败", "BLOCKED": "阻塞", "SKIP": "跳过", "WARN": "警告"}
PARTIAL_SCOPE = {"AC-04": "浏览器端到端", "AC-07": "实际语音质量与前端",
                 "AC-10": "模型与语音超时", "AC-12": "浏览器儿童模式"}


def cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render(template: str, payload: dict, commit: str | None = None) -> tuple[str, str, str]:
    results = payload["results"]
    meta = payload.get("meta", {})
    recorded = meta.get("testedCommit")
    if commit and recorded and commit != recorded:
        raise ValueError("指定的被测提交与验收记录不一致")
    commit = recorded or commit or "未记录"
    by_id = {item["case_id"]: item for item in results}
    counts = Counter(item["status"] for item in results)
    if counts.keys() - STATES.keys():
        raise ValueError("验收报告含未知状态")
    summary = f"{len(results)} 项，" + "、".join(f"{counts[s]} {label}" for s, label in STATES.items())
    if not results:
        conclusion = "本轮没有黑盒用例记录，无法判定。"
    elif counts["FAIL"]:
        conclusion = "本轮黑盒验收有失败，须修复后复测。"
    elif counts["BLOCKED"] or counts["WARN"] or counts["SKIP"]:
        conclusion = "已有记录包含阻塞、警告或跳过，完整 P6 尚不能判定通过。"
    else:
        conclusion = "本轮记录的黑盒用例均通过；完整 P6 还需核对需求覆盖及独立验收项。"
    status_table = ["| 状态 | 数量 |", "| --- | ---: |"]
    status_table.extend(f"| {label}（{s}） | {counts[s]} |" for s, label in STATES.items())
    status_table.append(f"| **合计** | **{len(results)}** |")
    problems = [item for item in results if item["status"] != "PASS"]
    security = [item for item in results if case_module(item) in {"AUTH", "SEC"}
                or item["ac"] == "NFR-04" or item["case_id"] in {"TC-P6-CHILD-001", "TC-P6-SESS-001"}]
    values = {"summary": summary, "conclusion": conclusion, "tested_commit": commit,
              "base_url": meta.get("baseUrl") or "未记录", "profile": meta.get("profile") or "未记录",
              "status_table": "\n".join(status_table),
              "exceptions": detail_table(problems) if problems else "本轮未记录异常用例。",
              "security_table": detail_table(security) if security else "本轮未执行。"}

    def field(match: re.Match) -> str:
        name = match.group(1)
        if name in values:
            return values[name]
        kind, key = name.split(":", 1)
        if kind == "module":
            items = [item for item in results if case_module(item) == key]
            return statuses(items) if items else "本轮未执行"
        if kind == "ac":
            items = [item for item in results if item["ac"] == key]
            if key == "AC-15" and not items and "TC-P6-AGENT-003" in by_id:
                items = [by_id["TC-P6-AGENT-003"]]
            if not items:
                return "本轮未执行"
            group = statuses(items)
            if key == "AC-14" and any(item["status"] == "WARN" for item in items):
                return group + "（部分通过，" + cell(next(i["detail"] for i in items if i["status"] == "WARN")) + "）"
            if key in PARTIAL_SCOPE and all(i["status"] == "PASS" for i in items):
                return group + f"（仅所列用例，{PARTIAL_SCOPE[key]}需专项验收）"
            return group
        item = by_id.get(key)
        if not item:
            return "本轮未执行"
        if kind == "detail":
            return cell(item["detail"]) or "未记录说明"
        if kind == "status":
            return f"{item['status']}（{STATES[item['status']]}）"
        raise ValueError(f"未知模板字段 {name}")

    if template.count("<!-- CASE_TABLE -->") != 2:
        raise ValueError("模板应有两个 CASE_TABLE 标记")
    text = re.sub(r"\{\{([^{}]+)\}\}", field, template)
    text = text.replace("<!-- CASE_TABLE -->", module_table(results), 1)
    text = text.replace("<!-- CASE_TABLE -->", detail_table(results), 1)
    return text, commit, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--acceptance", type=Path, default=HERE / "reports" / "acceptance-latest.json")
    parser.add_argument("--tested-commit", help="旧报告未记录时补充经确认的被测提交")
    parser.add_argument("--markdown", type=Path, default=OUTPUT_MD)
    parser.add_argument("--docx", type=Path)
    parser.add_argument("--no-docx", action="store_true")
    args = parser.parse_args()
    payload = json.loads(args.acceptance.read_text(encoding="utf-8"))
    text, commit, summary = render(args.template.read_text(encoding="utf-8"), payload, args.tested_commit)
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(text, encoding="utf-8")
    official = args.markdown.resolve() == OUTPUT_MD.resolve()
    if official:
        folder = OUTPUT_MD.parent
        (folder / "P6-测试文档.md").write_text(text, encoding="utf-8")
        for name in ("P6-交付说明.md", "P6-演示脚本.md"):
            path = folder / name
            notes = path.read_text(encoding="utf-8")
            for label, value in (("被测提交", chr(96) + commit + chr(96)), ("黑盒验收", summary + "。")):
                notes, n = re.subn(rf"(?m)^- {label}：.*$", lambda _: f"- {label}：{value}", notes)
                if n != 1:
                    raise ValueError(f"{name} 缺少唯一的{label}字段")
            path.write_text(notes, encoding="utf-8")
    if not args.no_docx:
        docx = args.docx or (OUTPUT_DOCX if official else args.markdown.with_suffix(".docx"))
        subprocess.run([sys.executable, str(HERE / "md_to_docx.py"), str(args.markdown), str(docx)], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
