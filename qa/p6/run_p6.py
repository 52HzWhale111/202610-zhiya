"""P6 一键执行入口：本地起服务、准备数据、跑验收、生成模型对比模板与缺陷表。

示例：
    python qa/p6/run_p6.py --start-local
    python qa/p6/run_p6.py --base-url https://zhiya.example.com --profile public \
        --account p6demo --password 'Zhiya-demo-2026'
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "database" / "instance" / "zhiya.sqlite3"
DEFAULT_OUT = ROOT / "qa" / "p6" / "reports"


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> int:
    print("$ " + " ".join(cmd))
    completed = subprocess.run(cmd, cwd=ROOT, env=env)
    return completed.returncode


def wait_health(base_url: str, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/v1/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 启动期间连接失败是正常的
            time.sleep(0.5)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="知芽 P6 一键验收")
    parser.add_argument("--base-url", help="被测地址；不传则本地启动")
    parser.add_argument("--profile", choices=["local", "public"], default=None)
    parser.add_argument("--start-local", action="store_true", help="本地启动 uvicorn")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--account")
    parser.add_argument("--password", default="P6-test-pass-2026")
    parser.add_argument("--frontend-url", help="前端地址，例如 http://127.0.0.1:5174")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-acceptance", action="store_true")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--skip-defects", action="store_true")
    args = parser.parse_args(argv)

    base_url = args.base_url
    profile = args.profile or ("public" if base_url and base_url.startswith("https://") else "local")
    server: subprocess.Popen | None = None
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"

    if base_url is None:
        base_url = f"http://127.0.0.1:{args.port}"
        if not args.skip_prepare:
            if not args.db.exists():
                code = run([args.python, str(HERE / "prepare_demo_data.py"), "--db", str(args.db), "--yes"], env=env)
                if code:
                    return code
            else:
                print(f"数据库已存在：{args.db}（如需重新发布种子，删除后重跑或手动运行 prepare_demo_data.py）")
        env["ZHIYA_DB_PATH"] = str(args.db.resolve())
        print(f"启动本地服务：{base_url}")
        server = subprocess.Popen(
            [args.python, "-m", "uvicorn", "app.main:create_app", "--factory",
             "--app-dir", "backend", "--host", "127.0.0.1", "--port", str(args.port), "--no-access-log"],
            cwd=ROOT, env=env,
        )
        if not wait_health(base_url):
            server.terminate()
            print("服务在 40 秒内未就绪，已终止。", file=sys.stderr)
            return 2

    code = 0
    try:
        if not args.skip_acceptance:
            cmd = [args.python, str(HERE / "run_acceptance.py"), "--base-url", base_url,
                   "--profile", profile, "--outdir", str(args.outdir)]
            if profile == "local":
                cmd.append("--allow-http")
                if not os.getenv("TTS_API_URL") and not os.getenv("DASHSCOPE_API_KEY"):
                    # 本地默认没有语音配置；显式断言 503，避免把“未调用”写成通过。
                    cmd.append("--voice-expect-unconfigured")
            if args.account:
                cmd += ["--account", args.account, "--password", args.password]
            if args.frontend_url:
                cmd += ["--frontend-url", args.frontend_url]
            code = run(cmd, env=env)

        if not args.skip_model:
            run([args.python, str(HERE / "run_model_compare.py")], env=env)
            run([args.python, str(HERE / "summarize_model_compare.py")], env=env)
        if not args.skip_defects:
            run([args.python, str(HERE / "track_defects.py"), "--outdir", str(args.outdir)], env=env)
    finally:
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()

    latest = args.outdir / "acceptance-latest.json"
    if latest.exists():
        payload = json.loads(latest.read_text(encoding="utf-8"))
        print(json.dumps(payload.get("meta", {}), ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
