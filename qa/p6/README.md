# P6 测试与验收程序

本目录是知芽 P6 的测试程序与证据生成入口，覆盖：

- 黑盒接口验收：账号、档案、绘本、推荐、阅读会话、分支、儿童模式、语音鉴权、异常与性能；
- 领域模型对比：24 个案例，通用模型 vs 知芽领域条件，支持两位评分人独立打分；
- 缺陷跟踪：人工确认的缺陷 + 自动失败用例，输出 Markdown / CSV；
- 公网验收：HTTPS、证书、Cookie 属性、安全响应头、契约覆盖与演示账号全流程；
- 演示脚本：`docs/testing/p6/P6-演示脚本.md`。

## 快速开始（本地）

```bash
# 1. 在仓库根目录创建虚拟环境并安装后端依赖
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements-dev.txt

# 2. 一键：准备演示数据 → 起服务 → 跑验收 → 生成模型对比模板与缺陷表
.venv/Scripts/python.exe qa/p6/run_p6.py --start-local --db qa/p6/instance/zhiya.sqlite3
```

首次运行会创建数据库、审核发布 12 本种子绘本与 20 条知识、创建演示账号
`p6demo / Zhiya-demo-2026`。数据库默认位于 `database/instance/zhiya.sqlite3`，
也可用 `--db` 指定到仓库外，避免提交数据库文件。

## 分步执行

```bash
# 准备数据（会修改数据库，必须显式 --yes）
python qa/p6/prepare_demo_data.py --db qa/p6/instance/zhiya.sqlite3 --yes

# 启动后端（另开终端）
ZHIYA_DB_PATH=qa/p6/instance/zhiya.sqlite3 \
  .venv/Scripts/python.exe -m uvicorn app.main:create_app --factory \
  --app-dir backend --host 127.0.0.1 --port 8765 --no-access-log

# 黑盒验收（本地 HTTP 需 --allow-http 才能回传 Secure Cookie）
# 本地未配置 TTS 时加 --voice-expect-unconfigured 断言 503；不调用付费服务
python qa/p6/run_acceptance.py --base-url http://127.0.0.1:8765 \
  --profile local --allow-http --voice-expect-unconfigured

# 模型对比：默认 dry-run，不产生调用
python qa/p6/run_model_compare.py
python qa/p6/summarize_model_compare.py

# 缺陷表
python qa/p6/track_defects.py
```

## 公网验收

```bash
python qa/p6/run_acceptance.py \
  --base-url https://your-domain.example \
  --http-url http://your-domain.example \
  --profile public \
  --account p6demo --password '演示账号密码' \
  --frontend-url https://your-domain.example
```

公网模式要求 HTTPS；脚本会检查证书链、HTTP→HTTPS 跳转、HSTS、Cookie 的
`Secure/HttpOnly/SameSite` 属性、演示账号主流程与响应时间。`--frontend-url`
用于检查前端页面是否可访问；未提供时前端验收记为 BLOCKED，不会写成通过。前端代码另有 71 项 Vitest，可通过 `npm --prefix frontend test` 执行。

## 模型对比

`model_cases.json` 含 24 个案例（EVAL-01～08 的扩展集），覆盖 3～6 岁三个年龄段、
事实问答、跨页未知、未理解引导、安全边界、原创故事与分支连贯性。

真实执行需要：

```bash
export MODEL_API_KEY='...'       # 不要写进仓库
python qa/p6/run_model_compare.py --live --db qa/p6/instance/zhiya.sqlite3
python qa/p6/summarize_model_compare.py
```

默认条件为 `baseline,domain`（通用模型 vs 领域条件）。如需消融实验，加
`--conditions baseline,page_only,domain`。每次调用都会计费，脚本默认最多 48 次，
超过需显式调高 `--max-calls`。结果写入 `reports/model-compare/results-*.jsonl`，
两位评分人独立填写 `scoring.csv` 后再汇总；空表不会被算成通过。

## 产物

| 路径 | 内容 |
| --- | --- |
| `reports/acceptance-latest.json` | 最近一次验收的机器可读结果 |
| `reports/acceptance-latest.md` | 用例明细与验收项汇总 |
| `reports/acceptance-*.xml` | JUnit 格式，可接入 CI |
| `reports/model-compare/` | 评测计划、评分模板、原始输出与汇总 |
| `reports/defect-ledger.md/.csv` | 缺陷跟踪表 |
| `docs/testing/p6/P6-测试文档.md` | 提交用测试文档 |

## 边界与注意

- 黑盒脚本不导入后端代码，不读取 `.env`，不接触真实密钥。
- 语音用例默认只验证鉴权、CSRF 和格式，不调用合成；本地一键脚本在未检测到
  `TTS_API_URL` / `DASHSCOPE_API_KEY` 时会加 `--voice-expect-unconfigured`，
  调用一次并断言 503（不产生费用）。只有 `--voice-live` 才执行真实 TTS，可能计费。
- 登录限流为同一 IP 15 分钟 10 次；连续多次执行验收会触发 429，属于预期安全行为。
  等待窗口结束、重启本地服务或更换出口 IP 后重跑。
- 儿童模式 API-020 会吊销当前设备的家长会话，脚本为此使用独立客户端；手工演示时
  请先用另一台设备/另一个浏览器保留家长入口。
- 种子默认全部 DRAFT，未执行 `prepare_demo_data.py` 时书架为空；这是数据状态，
  不是接口故障。

- Windows 系统代理可能接管本机 POST 请求并返回 422；测试程序默认 `trust_env=False`。如确需代理，设置 `ZHIYA_USE_ENV_PROXY=1`。
