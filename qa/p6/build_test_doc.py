"""把验收结果注入测试文档模板，并生成完整 Markdown 与 Word。

模板中的两个 <!-- CASE_TABLE -->：
  第一个替换为按模块汇总表；
  第二个替换为 40 项逐条结果表。
"""

from __future__ import annotations

import argparse
import json
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


def module_table(results: list[dict]) -> str:
    groups: dict[str, list[dict]] = {}
    for item in results:
        module = item["case_id"].split("-")[2] if len(item["case_id"].split("-")) > 2 else "OTHER"
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--acceptance", type=Path,
                        default=ROOT / "qa" / "p6" / "reports" / "acceptance-latest.json")
    parser.add_argument("--markdown", type=Path, default=OUTPUT_MD)
    parser.add_argument("--docx", type=Path, default=OUTPUT_DOCX)
    parser.add_argument("--no-docx", action="store_true")
    args = parser.parse_args()

    payload = json.loads(args.acceptance.read_text(encoding="utf-8"))
    results = payload["results"]
    text = args.template.read_text(encoding="utf-8")
    if text.count("<!-- CASE_TABLE -->") != 2:
        raise SystemExit("模板中应有两个 <!-- CASE_TABLE --> 标记")
    text = text.replace("<!-- CASE_TABLE -->", module_table(results), 1)
    text = text.replace("<!-- CASE_TABLE -->", detail_table(results), 1)
    args.markdown.write_text(text, encoding="utf-8")
    print(f"Markdown: {args.markdown}")

    if not args.no_docx:
        subprocess.run([sys.executable, str(HERE / "md_to_docx.py"), str(args.markdown), str(args.docx)],
                       check=True)
        print(f"Word: {args.docx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
