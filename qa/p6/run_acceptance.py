"""P6 黑盒验收测试运行器。

用法示例：
    python qa/p6/run_acceptance.py --base-url http://127.0.0.1:8000 --allow-http --profile local
    python qa/p6/run_acceptance.py --base-url https://zhiya.example.com --profile public \
        --account p6demo --password 'Zhiya-demo-2026'

脚本只通过 HTTP 访问被测系统，不导入后端代码，因此同一套用例既可用于本地，
也可用于公网部署。结果写入 qa/p6/reports/ 下的 JSON、Markdown 与 JUnit XML。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from common import (
    BLOCKED,
    FAIL,
    PASS,
    SKIP,
    WARN,
    ApiSession,
    Blocked,
    Recorder,
    Warn,
    error_code,
    cookie_flags,
    loopback_host,
    loopback_http,
    response_json,
    unique_suffix,
    write_reports,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "qa" / "p6" / "reports"

CONTRACT_PATHS = {
    "/api/v1/auth/registrations", "/api/v1/auth/sessions", "/api/v1/auth/sessions/current",
    "/api/v1/auth/parent-unlocks", "/api/v1/child-profiles", "/api/v1/child-profiles/{profile_id}",
    "/api/v1/storybooks", "/api/v1/story-versions/{version_id}", "/api/v1/recommendations",
    "/api/v1/reading-sessions", "/api/v1/reading-sessions/{session_id}",
    "/api/v1/reading-sessions/{session_id}/events", "/api/v1/reading-sessions/{session_id}/questions",
    "/api/v1/reading-sessions/{session_id}/guidance", "/api/v1/reading-sessions/{session_id}/branches",
    "/api/v1/reading-sessions/{session_id}/completions", "/api/v1/reading-sessions/{session_id}/mode-settings",
    "/api/v1/voice/transcriptions", "/api/v1/voice/syntheses", "/api/v1/media/{resource_key}",
    "/api/v1/story-generations", "/api/v1/story-versions/{version_id}/approvals",
    "/api/v1/admin/storybooks", "/api/v1/admin/story-versions/{version_id}",
    "/api/v1/admin/knowledge-items", "/api/v1/admin/knowledge-items/{knowledge_id}", "/api/v1/health",
}

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy": None,
    "referrer-policy": None,
}


class RateLimited(Exception):
    """登录限流（429）不是功能失败，但会阻断依赖登录的用例。"""


def expect(response: httpx.Response, status: int, code: str | None = None, label: str = "") -> Any:
    body = response_json(response)
    if response.status_code != status:
        snippet = json.dumps(body, ensure_ascii=False)[:500] if body is not None else response.text[:500]
        raise AssertionError(f"{label} 期望 HTTP {status}，实际 {response.status_code}：{snippet}")
    if code is not None:
        actual = error_code(response)
        if actual != code:
            raise AssertionError(f"{label} 期望错误码 {code}，实际 {actual}")
    return body


def expect_envelope(body: Any, response: httpx.Response, label: str) -> None:
    if not isinstance(body, dict):
        raise AssertionError(f"{label} 响应不是 JSON 对象")
    request_id = body.get("requestId")
    if not request_id:
        raise AssertionError(f"{label} 响应缺少 requestId")
    header_id = response.headers.get("X-Request-ID")
    if header_id != request_id:
        raise AssertionError(f"{label} requestId 与 X-Request-ID 不一致：{request_id} != {header_id}")


def p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(0.95 * (len(ordered) - 1)))))
    return ordered[index]


@dataclass
class Demo:
    account: str
    password: str
    profile_id: int
    story: dict[str, Any]
    story_id: int
    session_id: int
    revision: int


class Acceptance:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rec = Recorder()
        self.rec.meta.update({"baseUrl": args.base_url, "profile": args.profile,
                              "mode": "black-box-http"})
        if args.tested_commit:
            self.rec.meta["testedCommit"] = args.tested_commit
        self.a = ApiSession(args.base_url, timeout=args.timeout,
                            verify=not args.insecure_tls,
                            allow_insecure_cookies=args.allow_http)
        self.anon = ApiSession(args.base_url, timeout=args.timeout,
                               verify=not args.insecure_tls,
                               allow_insecure_cookies=args.allow_http)
        self.b: ApiSession | None = None
        self.demo: Demo | None = None
        self.failures: list[str] = []

    # ---------- 基础断言 ----------

    def require_demo(self) -> Demo:
        if self.demo is None:
            raise AssertionError("前置登录/档案/绘本准备失败，无法执行该用例")
        return self.demo

    def login(self, client: ApiSession, account: str, password: str) -> httpx.Response:
        response = client.post("/api/v1/auth/sessions", json={"account": account, "password": password})
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "?")
            raise RateLimited(f"登录被限流（429，Retry-After={retry_after}s）；"
                              "同一出口 IP 在 15 分钟内最多 10 次登录，请等待或更换出口后重跑")
        return response

    def register(self, client: ApiSession, account: str, password: str) -> httpx.Response:
        return client.post("/api/v1/auth/registrations", json={"account": account, "password": password})

    # ---------- 环境与健康 ----------

    def test_environment(self) -> str:
        target = httpx.URL(self.args.base_url)
        scheme = target.scheme
        if self.args.profile != "public" and self.args.insecure_tls and not loopback_host(target.host):
            raise AssertionError("非回环 HTTPS 服务必须校验证书")
        if scheme != "https" and not (
            self.args.profile == "local" and self.args.allow_http and loopback_http(self.args.base_url)
        ):
            raise AssertionError("验收要求 HTTPS；仅本地回环地址可显式使用 --allow-http")
        return f"scheme={scheme}, profile={self.args.profile}"

    def test_health(self) -> tuple[str, dict]:
        started = time.perf_counter()
        response = self.anon.get("/api/v1/health")
        elapsed = (time.perf_counter() - started) * 1000
        body = expect(response, 200, label="健康检查")
        expect_envelope(body, response, "健康检查")
        if body.get("data", {}).get("status") != "ok":
            raise AssertionError(f"健康状态不是 ok：{body}")
        return f"status=ok, {elapsed:.0f}ms", {"latencyMs": round(elapsed, 2)}

    def test_unknown_route(self) -> str:
        response = self.anon.get("/api/v1/p6-not-found")
        body = expect(response, 404, "NOT_FOUND", "未知路由")
        expect_envelope(body, response, "未知路由")
        return "404 NOT_FOUND，envelope 完整"

    def test_method_not_allowed(self) -> str:
        response = self.anon.post("/api/v1/health")
        body = expect(response, 405, "METHOD_NOT_ALLOWED", "方法不允许")
        expect_envelope(body, response, "方法不允许")
        return "405 METHOD_NOT_ALLOWED"

    # ---------- 账号与授权 ----------

    def test_registration(self) -> tuple[str, dict]:
        suffix = unique_suffix()
        account = self.args.account or f"p6a{suffix}"
        password = self.args.password
        if self.args.account:
            self.rec.record("TC-P6-AUTH-001", "注册新账号", "AC-01", SKIP,
                            "使用已有演示账号，跳过注册；如需验证注册请不传 --account")
        else:
            response = self.register(self.a, account, password)
            body = expect(response, 201, label="注册")
            expect_envelope(body, response, "注册")
            if not body.get("data", {}).get("accountId"):
                raise AssertionError("注册响应缺少 accountId")
            self.rec.record("TC-P6-AUTH-001", "注册新账号", "AC-01", PASS,
                            f"account={account}，201 且返回 accountId")
            # 重复账号
            duplicate = self.register(self.anon, account, password)
            expect(duplicate, 409, "ACCOUNT_EXISTS", "重复注册")
            self.rec.record("TC-P6-AUTH-002", "重复账号注册被拒绝", "AC-01", PASS,
                            "409 ACCOUNT_EXISTS")
        # 错误密码
        wrong = ApiSession(self.args.base_url, timeout=self.args.timeout,
                           verify=not self.args.insecure_tls, allow_insecure_cookies=self.args.allow_http)
        try:
            bad = self.login(wrong, account, "wrong-password-000")
            expect(bad, 401, "INVALID_CREDENTIALS", "错误密码登录")
            self.rec.record("TC-P6-AUTH-003", "错误密码登录被拒绝", "AC-01", PASS,
                            "401 INVALID_CREDENTIALS")
        finally:
            wrong.close()
        # 正确登录，主会话
        response = self.login(self.a, account, password)
        body = expect(response, 201, label="登录")
        expect_envelope(body, response, "登录")
        session_flags = cookie_flags(response, "zhiya_session")
        csrf_flags = cookie_flags(response, "zhiya_csrf")
        if not all(session_flags.values()):
            raise AssertionError(f"会话 Cookie 属性不符：{session_flags}")
        if not csrf_flags["secure"] or not csrf_flags["sameSiteLax"] or csrf_flags["httpOnly"]:
            raise AssertionError(f"CSRF Cookie 属性不符：{csrf_flags}")
        self.account = account
        self.password = password
        return f"account={account}", {"cookieFlags": {"session": session_flags, "csrf": csrf_flags}}

    def test_anonymous_protected(self) -> str:
        response = self.anon.get("/api/v1/child-profiles")
        body = expect(response, 401, "AUTH_REQUIRED", "匿名访问受保护接口")
        expect_envelope(body, response, "匿名访问受保护接口")
        return "401 AUTH_REQUIRED"

    def test_logout(self) -> str:
        client = ApiSession(self.args.base_url, timeout=self.args.timeout,
                            verify=not self.args.insecure_tls, allow_insecure_cookies=self.args.allow_http)
        try:
            try:
                expect(self.login(client, self.account, self.password), 201, label="退出用例登录")
            except RateLimited as exc:
                raise Blocked(str(exc)) from None
            response = client.delete("/api/v1/auth/sessions/current", csrf=True)
            expect(response, 204, label="退出")
            after = client.get("/api/v1/child-profiles")
            expect(after, 401, "AUTH_REQUIRED", "退出后访问")
        finally:
            client.close()
        return "退出 204，旧会话失效"

    # ---------- 档案 ----------

    def test_profile_create(self) -> tuple[str, dict]:
        payload = {"nickname": "P6小芽", "ageBand": "AGE_4_5",
                   "interests": ["动物", "友谊"], "preferredMinutes": 10}
        no_csrf = self.a.post("/api/v1/child-profiles", json=payload)
        expect(no_csrf, 403, "CSRF_INVALID", "档案写请求缺 CSRF")
        response = self.a.post("/api/v1/child-profiles", csrf=True, json=payload)
        body = expect(response, 201, label="创建档案")
        expect_envelope(body, response, "创建档案")
        data = body["data"]
        profile_id = data.get("childProfileId")
        if not profile_id:
            raise AssertionError("创建档案响应缺少 childProfileId")
        if data.get("ageBand") != "AGE_4_5" or data.get("preferredMinutes") != 10:
            raise AssertionError(f"档案字段回显不一致：{data}")
        listed = self.a.get("/api/v1/child-profiles")
        body = expect(listed, 200, label="档案列表")
        ids = [item["childProfileId"] for item in body["data"]["items"]]
        if profile_id not in ids:
            raise AssertionError("新建档案未出现在列表中")
        self.profile = data
        return f"childProfileId={profile_id}", {"profileId": profile_id}

    def test_profile_update(self) -> tuple[str, dict]:
        profile = getattr(self, "profile", None)
        if not profile:
            raise AssertionError("前置创建档案失败")
        response = self.a.patch(f"/api/v1/child-profiles/{profile['childProfileId']}", csrf=True,
                                json={"preferredMinutes": 15, "interests": ["动物"]})
        body = expect(response, 200, label="修改档案")
        data = body["data"]
        if data["preferredMinutes"] != 15 or data["interests"] != ["动物"]:
            raise AssertionError(f"修改未持久化：{data}")
        reloaded = self.a.get("/api/v1/child-profiles")
        row = next(item for item in reloaded.json()["data"]["items"]
                   if item["childProfileId"] == profile["childProfileId"])
        if row["preferredMinutes"] != 15:
            raise AssertionError("重新读取后修改丢失")
        return "preferredMinutes=15, interests=[动物]"

    def test_profile_validation(self) -> str:
        profile_id = self.profile["childProfileId"]
        bad_minutes = self.a.patch(f"/api/v1/child-profiles/{profile_id}", csrf=True,
                                   json={"preferredMinutes": 0})
        expect(bad_minutes, 422, "VALIDATION_ERROR", "非法阅读时长")
        extra = self.a.post("/api/v1/child-profiles", csrf=True,
                            json={"ageBand": "AGE_4_5", "preferredMinutes": 10, "unknown": 1})
        expect(extra, 422, "VALIDATION_ERROR", "多余字段")
        return "0 分钟与未知字段均 422"

    def test_cross_account_profile(self) -> str:
        if self.b is None:
            raise AssertionError("账号 B 未就绪")
        profile_id = self.profile["childProfileId"]
        patch = self.b.patch(f"/api/v1/child-profiles/{profile_id}", csrf=True,
                             json={"nickname": "越权"})
        expect(patch, 404, "RESOURCE_NOT_FOUND", "B 修改 A 的档案")
        delete = self.b.delete(f"/api/v1/child-profiles/{profile_id}", csrf=True)
        expect(delete, 404, "RESOURCE_NOT_FOUND", "B 删除 A 的档案")
        return "跨账号修改/删除均 404"

    # ---------- 绘本 ----------

    def test_storybook_list(self) -> tuple[str, dict]:
        response = self.a.get("/api/v1/storybooks", params={"pageSize": 50})
        body = expect(response, 200, label="绘本列表")
        expect_envelope(body, response, "绘本列表")
        items = body["data"]["items"]
        if len(items) < self.args.min_storybooks:
            raise AssertionError(
                f"可用绘本 {len(items)} 本，少于 AC-03 要求的 {self.args.min_storybooks} 本；"
                "本地请先运行 prepare_demo_data.py --yes 审核发布种子")
        story = None
        if self.args.story_version_id:
            story = next((item for item in items if item["storyVersionId"] == self.args.story_version_id), None)
        if story is None:
            story = next((item for item in items if len(item.get("pages", [])) >= 4), items[0])
        if not story.get("pages"):
            raise AssertionError("绘本详情没有页面")
        self.story = story
        return f"可用绘本 {len(items)} 本，选用 {story['title']}（v{story['storyVersionId']}）", {
            "storybookCount": len(items), "storyVersionId": story["storyVersionId"]}

    def test_storybook_filters(self) -> tuple[str, dict]:
        empty = self.a.get("/api/v1/storybooks", params={"topic": "P6不存在的主题"})
        body = expect(empty, 200, label="无匹配主题")
        if body["data"]["items"]:
            raise AssertionError("不存在的主题应返回空列表")
        oversized = self.a.get("/api/v1/storybooks", params={"pageSize": 51})
        expect(oversized, 422, "VALIDATION_ERROR", "pageSize 上限")
        no_auth = self.anon.get("/api/v1/storybooks")
        expect(no_auth, 401, "AUTH_REQUIRED", "匿名绘本列表")
        return "无匹配为空、pageSize>50 为 422、匿名为 401"

    def test_storybook_detail(self) -> tuple[str, dict]:
        story_id = self.story["storyVersionId"]
        response = self.a.get(f"/api/v1/story-versions/{story_id}")
        body = expect(response, 200, label="绘本详情")
        data = body["data"]
        pages = data.get("pages", [])
        if not pages:
            raise AssertionError("详情缺少 pages")
        if any(not page.get("text", "").strip() for page in pages):
            raise AssertionError("存在正文为空的页面")
        if "coverRef" not in data or "branches" not in data:
            raise AssertionError("详情缺少 coverRef 或 branches 字段")
        missing = self.a.get("/api/v1/story-versions/999999999")
        expect(missing, 404, "RESOURCE_NOT_FOUND", "不存在的绘本")
        return f"{len(pages)} 页，缺图字段为 null，越界 ID 404", {"pageCount": len(pages)}

    def test_recommendation(self) -> tuple[str, dict]:
        profile_id = self.profile["childProfileId"]
        no_csrf = self.a.post("/api/v1/recommendations", json={"childProfileId": profile_id})
        expect(no_csrf, 403, "CSRF_INVALID", "推荐接口缺 CSRF")
        response = self.a.post("/api/v1/recommendations", csrf=True,
                               json={"childProfileId": profile_id})
        body = expect(response, 200, label="绘本推荐")
        expect_envelope(body, response, "绘本推荐")
        items = body["data"]["items"]
        if not items:
            raise AssertionError("推荐结果为空；请确认已审核发布绘本")
        for item in items:
            if not item.get("storyVersionId") or not str(item.get("reason", "")).strip():
                raise AssertionError(f"推荐项缺少 storyVersionId 或 reason：{item}")
        if self.b is not None:
            cross = self.b.post("/api/v1/recommendations", csrf=True,
                                json={"childProfileId": profile_id})
            expect(cross, 404, "RESOURCE_NOT_FOUND", "B 使用 A 的档案推荐")
        return f"{len(items)} 条推荐，均含故事版本与理由", {"count": len(items)}

    # ---------- 阅读会话 ----------

    def _create_session(self, client: ApiSession | None = None) -> dict:
        client = client or self.a
        profile = getattr(self, "profile", None) or self.demo.profile_id
        story = getattr(self, "story", None) or self.demo.story
        body = {"childProfileId": profile["childProfileId"] if isinstance(profile, dict) else profile,
                "storyVersionId": story["storyVersionId"], "mode": "PARENT_CHILD"}
        response = client.post("/api/v1/reading-sessions", csrf=True, json=body)
        payload = expect(response, 201, label="创建阅读会话")
        return payload["data"]

    def test_session_create(self) -> tuple[str, dict]:
        profile = getattr(self, "profile", None)
        if not profile:
            raise AssertionError("前置档案未创建")
        body = {"childProfileId": profile["childProfileId"],
                "storyVersionId": self.story["storyVersionId"], "mode": "PARENT_CHILD"}
        no_csrf = self.a.post("/api/v1/reading-sessions", json=body)
        expect(no_csrf, 403, "CSRF_INVALID", "会话创建缺 CSRF")
        session = self._create_session()
        if (session["status"], session["revision"], session["currentPage"]) != ("CREATED", 0, 1):
            raise AssertionError(f"新会话初始状态不正确：{session}")
        self.session = session
        self.session_id = session["readingSessionId"]
        self.revision = 0
        return f"readingSessionId={self.session_id}, status=CREATED, revision=0"

    def test_session_events(self) -> tuple[str, dict]:
        sid = self.session_id
        started = {"eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0,
                   "clientEventId": f"p6-start-{uuid4().hex[:8]}"}
        response = self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True, json=started)
        body = expect(response, 201, label="开始阅读")
        data = body["data"]
        if data.get("revision") != 1:
            raise AssertionError(f"STARTED 后 revision 应为 1，实际 {data.get('revision')}")
        replay = self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True, json=started)
        replay_body = expect(replay, 200, label="STARTED 重放")
        if replay_body["data"] != data:
            raise AssertionError("幂等重放返回的数据不一致")
        conflict = self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True,
                               json={**started, "pageNumber": 2})
        expect(conflict, 409, "IDEMPOTENCY_CONFLICT", "同键不同请求")
        stale = self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True,
                            json={"eventType": "PAGE_CHANGED", "pageNumber": 2, "expectedRevision": 0,
                                  "clientEventId": f"p6-stale-{uuid4().hex[:8]}"})
        expect(stale, 409, "SESSION_CONFLICT", "过期 revision")
        self.revision = 1
        return "STARTED=201、重放=200、同键不同体=409、过期 revision=409"

    def test_page_change(self) -> tuple[str, dict]:
        sid = self.session_id
        first_page = self.story["pages"][0]
        next_page = first_page.get("defaultNextPageNumber")
        if not next_page:
            return "本绘本没有下一跳页，跳过翻页事件"
        response = self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True, json={
            "eventType": "PAGE_CHANGED", "pageNumber": next_page,
            "expectedRevision": self.revision, "clientEventId": f"p6-page-{uuid4().hex[:8]}"})
        body = expect(response, 201, label="翻页")
        self.revision = body["data"]["revision"]
        detail = expect(self.a.get(f"/api/v1/reading-sessions/{sid}"), 200, label="翻页后会话")
        session = detail["data"]["session"]
        if session["currentPage"] != next_page or next_page not in session["visitedPageNumbers"]:
            raise AssertionError(f"翻页未生效：{session}")
        return f"currentPage={next_page}, revision={self.revision}"

    def test_branch_choice(self) -> Warn:
        branches = [item for item in (self.story.get("branches") or [])
                    if item.get("reviewStatus") == "APPROVED"]
        if not branches:
            raise Blocked("当前绘本没有已审核分支；请运行 prepare_demo_data.py 准备测试分支")
        branch = branches[0]
        session = self._create_session()
        sid = session["readingSessionId"]
        expect(self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True, json={
            "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0,
            "clientEventId": f"p6-branch-start-{uuid4().hex[:8]}",
        }), 201, label="分支用例开始会话")
        payload = {"branchId": branch["branchId"], "expectedRevision": 1,
                   "clientEventId": f"p6-branch-{uuid4().hex[:8]}"}
        response = self.a.post(f"/api/v1/reading-sessions/{sid}/branches", csrf=True, json=payload)
        body = expect(response, 201, label="选择分支")
        data = body["data"]
        if data.get("currentPage") != branch["nextPageNumber"] or data.get("revision") != 2:
            raise AssertionError(f"分支结果不正确：{data}")
        replay = self.a.post(f"/api/v1/reading-sessions/{sid}/branches", csrf=True, json=payload)
        expect(replay, 200, label="分支重放")
        stale = self.a.post(f"/api/v1/reading-sessions/{sid}/branches", csrf=True, json={
            "branchId": branch["branchId"], "expectedRevision": 1,
            "clientEventId": f"p6-branch-stale-{uuid4().hex[:8]}",
        })
        expect(stale, 409, "SESSION_CONFLICT", "过期 revision 选分支")
        return Warn("已验证单条审核分支、重放与过期修订冲突。AC-14 部分通过，"
                    "待验证同一决策点两种不同后续、节点上限与无环约束。",
                    {"branchId": branch["branchId"], "nextPage": data["currentPage"]})

    def test_cross_account_session(self) -> str:
        if self.b is None:
            raise AssertionError("账号 B 未就绪")
        response = self.b.get(f"/api/v1/reading-sessions/{self.session_id}")
        expect(response, 404, "RESOURCE_NOT_FOUND", "B 读取 A 的会话")
        history = self.b.get("/api/v1/reading-sessions")
        ids = [item["readingSessionId"] for item in history.json()["data"]["items"]]
        if self.session_id in ids:
            raise AssertionError("B 的历史列表出现了 A 的会话")
        return "跨账号读取/列表均不可见"

    def test_finish_and_report(self) -> tuple[str, dict]:
        sid = self.session_id
        response = self.a.post(f"/api/v1/reading-sessions/{sid}/completions", csrf=True, json={
            "reason": "USER_FINISH", "expectedRevision": self.revision,
            "clientEventId": f"p6-finish-{uuid4().hex[:8]}"})
        body = expect(response, 200, label="结束阅读")
        data = body["data"]
        if data["session"]["status"] != "FINISHED":
            raise AssertionError(f"结束后状态不是 FINISHED：{data['session']}")
        report = data.get("report")
        if not isinstance(report, dict):
            raise AssertionError("结束后没有返回小结")
        status = report.get("status")
        activities = report.get("activities") or []
        self.report = report
        detail = f"status={status}, activities={len(activities)}"
        if status == "READY" and not activities:
            return detail + "（FR-10/AC-09 活动仍为空，记警告）", {"reportStatus": status}
        if status in ("PENDING", "FAILED"):
            return detail + "（模型未配置或生成失败，记警告）", {"reportStatus": status}
        return detail, {"reportStatus": status}

    def test_history(self) -> str:
        response = self.a.get("/api/v1/reading-sessions", params={"pageSize": 50})
        body = expect(response, 200, label="阅读历史")
        items = body["data"]["items"]
        row = next((item for item in items if item["readingSessionId"] == self.session_id), None)
        if row is None:
            raise AssertionError("已结束会话未出现在历史列表")
        if row["status"] != "FINISHED":
            raise AssertionError(f"历史状态不正确：{row['status']}")
        detail = self.a.get(f"/api/v1/reading-sessions/{self.session_id}")
        expect(detail, 200, label="历史详情")
        return "历史列表与详情均可查询，状态 FINISHED"

    def test_event_after_finish(self) -> str:
        response = self.a.post(f"/api/v1/reading-sessions/{self.session_id}/events", csrf=True, json={
            "eventType": "PAGE_CHANGED", "pageNumber": 2, "expectedRevision": self.revision,
            "clientEventId": f"p6-after-{uuid4().hex[:8]}"})
        expect(response, 409, "SESSION_CONFLICT", "结束后继续翻页")
        return "结束后新事件 409"

    def test_delete_session(self) -> str:
        temp = self._create_session()
        temp_id = temp["readingSessionId"]
        expect(self.a.delete(f"/api/v1/reading-sessions/{temp_id}", csrf=True), 204, label="删除会话")
        expect(self.a.get(f"/api/v1/reading-sessions/{temp_id}"), 404, "RESOURCE_NOT_FOUND", "删除后读取")
        return f"删除 {temp_id} 后 404"

    # ---------- 儿童模式 ----------

    def test_child_mode(self) -> tuple[str, dict]:
        guardian = ApiSession(self.args.base_url, timeout=self.args.timeout,
                              verify=not self.args.insecure_tls, allow_insecure_cookies=self.args.allow_http)
        child_token_before = None
        try:
            try:
                expect(self.login(guardian, self.account, self.password), 201, label="儿童模式登录")
            except RateLimited as exc:
                raise Blocked(str(exc)) from None
            profile = self.profile["childProfileId"]
            created = guardian.post("/api/v1/reading-sessions", csrf=True, json={
                "childProfileId": profile, "storyVersionId": self.story["storyVersionId"],
                "mode": "PARENT_CHILD"})
            session = expect(created, 201, label="儿童模式会话创建")["data"]
            sid = session["readingSessionId"]
            response = guardian.put(f"/api/v1/reading-sessions/{sid}/mode-settings", csrf=True, json={
                "mode": "CHILD_SOLO", "allowedStoryVersionIds": [self.story["storyVersionId"]],
                "timeLimitSeconds": 120})
            body = expect(response, 200, label="进入儿童模式")
            if body["data"]["mode"] != "CHILD_SOLO":
                raise AssertionError("未进入 CHILD_SOLO")
            child_token_before = guardian.session_cookie
            if not child_token_before:
                raise AssertionError("进入儿童模式后没有儿童 Cookie")
            # 儿童可读授权范围
            expect(guardian.get("/api/v1/storybooks"), 200, label="儿童绘本列表")
            expect(guardian.get(f"/api/v1/reading-sessions/{sid}"), 200, label="儿童会话详情")
            # 儿童不能改设置 / 看档案
            forbidden = guardian.put(f"/api/v1/reading-sessions/{sid}/mode-settings", csrf=True, json={
                "mode": "CHILD_SOLO", "allowedStoryVersionIds": [self.story["storyVersionId"]],
                "timeLimitSeconds": 600})
            expect(forbidden, 403, "FORBIDDEN", "儿童修改模式设置")
            expect(guardian.get("/api/v1/child-profiles"), 403, "FORBIDDEN", "儿童读取家长档案")
            # 错误密码解锁
            wrong = guardian.post("/api/v1/auth/parent-unlocks", csrf=True,
                                  json={"account": self.account, "password": "wrong-password-000"})
            expect(wrong, 401, "INVALID_CREDENTIALS", "错误密码解锁")
            # 正确解锁
            unlock = guardian.post("/api/v1/auth/parent-unlocks", csrf=True,
                                   json={"account": self.account, "password": self.password})
            expect(unlock, 200, label="家长解锁")
            expect(guardian.get("/api/v1/child-profiles"), 200, label="解锁后家长档案")
            # 旧儿童 token 已吊销
            revoked = ApiSession(self.args.base_url, timeout=self.args.timeout,
                                 verify=not self.args.insecure_tls, allow_insecure_cookies=self.args.allow_http)
            try:
                revoked.set_cookie("zhiya_session", child_token_before)
                expect(revoked.get("/api/v1/child-profiles"), 401, "AUTH_REQUIRED", "旧儿童凭证")
            finally:
                revoked.close()
        finally:
            guardian.close()
        return "儿童可读受限内容、不可改设置、错误密码 401、解锁后旧凭证失效"

    # ---------- 语音 ----------

    def test_voice_auth(self) -> str:
        anonymous = self.anon.post("/api/v1/voice/transcriptions", files={"file": ("x.wav", b"RIFF", "audio/wav")})
        expect(anonymous, 401, "AUTH_REQUIRED", "匿名语音上传")
        # 新建进行中的会话，供鉴权与格式检查使用
        session = self._create_session()
        sid = session["readingSessionId"]
        expect(self.a.post(f"/api/v1/reading-sessions/{sid}/events", csrf=True, json={
            "eventType": "STARTED", "pageNumber": 1, "expectedRevision": 0,
            "clientEventId": f"p6-voice-{uuid4().hex[:8]}"}), 201, label="语音用例开始会话")
        self.voice_session = sid
        no_csrf = self.a.post("/api/v1/voice/transcriptions",
                              files={"file": ("x.wav", b"RIFF", "audio/wav")})
        expect(no_csrf, 403, "CSRF_INVALID", "语音上传缺 CSRF")
        return "匿名 401、缺 CSRF 403"

    def test_voice_validation(self) -> str:
        sid = getattr(self, "voice_session", None)
        if not sid:
            raise AssertionError("语音前置会话未创建")
        if not (self.args.voice_live or self.args.voice_expect_unconfigured):
            raise Blocked("未启用 --voice-live 或 --voice-expect-unconfigured；"
                          "为避免未知环境下的付费合成，默认不调用 TTS")
        response = self.a.post("/api/v1/voice/syntheses", csrf=True,
                               json={"readingSessionId": sid, "pageNumber": 1, "revision": 1})
        if self.args.voice_live:
            if response.status_code == 503 and error_code(response) == "TTS_NOT_CONFIGURED":
                raise Blocked("真实语音合成未执行，服务返回 503 TTS_NOT_CONFIGURED")
            body = expect(response, 201, label="真实语音合成")
            if "audioUrl" not in body.get("data", {}):
                raise AssertionError("合成响应缺少 audioUrl")
            return "真实合成成功（已产生一次供应商调用）"
        expect(response, 503, "TTS_NOT_CONFIGURED", "TTS 未配置")
        bad_type = self.a.post("/api/v1/voice/syntheses", csrf=True,
                               headers={"Content-Type": "text/plain"}, content=b"x")
        if bad_type.status_code not in (415, 422):
            raise AssertionError(f"错误 Content-Type 期望 415/422，实际 {bad_type.status_code}")
        bad_multipart = self.a.post("/api/v1/voice/transcriptions", csrf=True,
                                    files={"file": ("x.wav", b"RIFF", "audio/wav")})
        if bad_multipart.status_code != 422:
            raise AssertionError(f"缺字段上传期望 422，实际 {bad_multipart.status_code}")
        return "TTS 未配置 503；错误类型 415/422；缺字段 422"

    # ---------- 非功能 ----------

    def test_performance(self) -> tuple[str, dict]:
        health_times = []
        for _ in range(10):
            started = time.perf_counter()
            expect(self.anon.get("/api/v1/health"), 200, label="健康性能采样")
            health_times.append((time.perf_counter() - started) * 1000)
        list_times = []
        for _ in range(5):
            started = time.perf_counter()
            expect(self.a.get("/api/v1/storybooks", params={"pageSize": 20}), 200, label="列表性能采样")
            list_times.append((time.perf_counter() - started) * 1000)
        health_p95, list_p95 = p95(health_times), p95(list_times)
        threshold = self.args.performance_ms
        if health_p95 > threshold:
            raise AssertionError(f"健康检查 p95={health_p95:.0f}ms 超过 {threshold}ms")
        if list_p95 > threshold:
            raise AssertionError(f"绘本列表 p95={list_p95:.0f}ms 超过 {threshold}ms")
        return f"health p95={health_p95:.0f}ms, list p95={list_p95:.0f}ms（阈值 {threshold}ms）", {
            "healthP95Ms": round(health_p95, 2), "listP95Ms": round(list_p95, 2),
            "healthSamples": [round(x, 2) for x in health_times],
            "listSamples": [round(x, 2) for x in list_times]}

    def test_security_headers(self) -> tuple[str, dict]:
        response = self.anon.get("/api/v1/health")
        present = {name: response.headers.get(name) for name in SECURITY_HEADERS}
        missing = [name for name, value in present.items() if value is None]
        detail = ("已设置: " + ", ".join(name for name, value in present.items() if value)) if len(missing) < 4 \
            else "未设置 " + ", ".join(missing)
        return Warn(detail + "（安全响应头不作为 P6 阻塞项，记录为警告）",
                    {"headers": present, "missing": missing})

    def test_openapi_coverage(self) -> tuple[str, dict]:
        response = self.anon.get("/openapi.json")
        body = expect(response, 200, label="OpenAPI")
        actual = set(body.get("paths", {}))
        self.openapi_paths = actual
        missing = sorted(CONTRACT_PATHS - actual)
        extra = sorted(actual - CONTRACT_PATHS)
        if not missing:
            return f"全部 {len(CONTRACT_PATHS)} 个契约路径已注册", {"missingPaths": [], "extraPaths": extra}
        return Warn(f"契约路径 {len(CONTRACT_PATHS)} 个，已实现 {len(CONTRACT_PATHS & actual)} 个；"
                    f"未实现 {len(missing)} 个", {"missingPaths": missing, "extraPaths": extra})

    def test_frontend(self) -> str:
        if not self.args.frontend_url:
            raise AssertionError("未提供 --frontend-url，前端可用性未验收")
        response = self.anon.client.get(self.args.frontend_url)
        if response.status_code != 200:
            raise AssertionError(f"前端返回 {response.status_code}")
        text = response.text.lower()
        if "<div id=\"app\"" not in text and "知芽" not in text:
            raise AssertionError("前端页面未发现应用挂载点或项目名称")
        return f"HTTP 200, {len(response.content)} bytes"

    # ---------- 公网专项 ----------

    def test_public_tls(self) -> tuple[str, dict]:
        if self.args.profile != "public":
            raise AssertionError("仅公网 profile 执行")
        if not self.args.base_url.startswith("https://"):
            raise AssertionError("公网地址不是 HTTPS")
        if self.args.insecure_tls:
            raise Blocked("证书校验已关闭，无法验收公网 TLS")
        response = self.anon.get("/api/v1/health")
        expect(response, 200, label="HTTPS 健康检查")
        hsts = response.headers.get("strict-transport-security")
        detail = "TLS 校验通过" + ("，HSTS 已设置" if hsts else "，未设置 HSTS（建议项）")
        return detail, {"hsts": hsts}

    def test_http_redirect(self) -> str:
        if not self.args.http_url:
            raise AssertionError("未提供 --http-url，跳过 HTTP 跳转检查")
        client = httpx.Client(timeout=self.args.timeout, verify=not self.args.insecure_tls,
                              follow_redirects=False)
        try:
            response = client.get(self.args.http_url)
        finally:
            client.close()
        if response.status_code not in (301, 302, 307, 308):
            raise AssertionError(f"HTTP 入口期望 3xx 跳转，实际 {response.status_code}")
        location = response.headers.get("location", "")
        if not location.startswith("https://"):
            raise AssertionError(f"跳转目标不是 HTTPS：{location}")
        return f"{response.status_code} -> {location}"

    # ---------- 主流程 ----------

    def run(self) -> int:
        args = self.args
        environment = self.rec.case("TC-P6-ENV-001", "测试环境与协议检查", "NFR-02", self.test_environment)
        if environment.status != PASS:
            return self._finish()
        if args.profile == "public" and args.insecure_tls:
            self.rec.case("TC-P6-PUB-001", "公网 HTTPS 与证书", "AC-11", self.test_public_tls)
            self._finish()
            return 2
        self.rec.case("TC-P6-API-001", "健康检查与 requestId", "NFR-08", self.test_health)
        self.rec.case("TC-P6-API-002", "未知路由错误结构", "FR-12", self.test_unknown_route)
        self.rec.case("TC-P6-API-003", "方法不允许错误结构", "FR-12", self.test_method_not_allowed)

        setup_ok = True
        try:
            detail, evidence = self.test_registration()
            self.rec.record("TC-P6-AUTH-004", "登录成功与 Cookie 安全属性", "AC-01", PASS, detail, evidence)
        except RateLimited as exc:
            self.rec.record("TC-P6-AUTH-004", "登录成功与 Cookie 安全属性", "AC-01", BLOCKED, str(exc))
            setup_ok = False
        except Exception as exc:  # noqa: BLE001
            self.rec.record("TC-P6-AUTH-004", "登录成功与 Cookie 安全属性", "AC-01", FAIL, str(exc))
            setup_ok = False
        if not setup_ok:
            for case_id, title, ac in (
                    ("TC-P6-AUTH-005", "匿名访问受保护接口", "AC-01"),
                    ("TC-P6-PROF-001", "创建并读取幼儿档案", "AC-02"),
                    ("TC-P6-BOOK-001", "绘本列表至少 8 本", "AC-03"),
            ):
                self.rec.record(case_id, title, ac, BLOCKED, "登录前置失败")
            return self._finish()

        self.rec.case("TC-P6-AUTH-005", "匿名访问受保护接口", "AC-01", self.test_anonymous_protected)
        self.rec.case("TC-P6-AUTH-006", "退出后旧会话失效", "AC-01", self.test_logout)

        # 账号 B：跨账号隔离
        try:
            suffix = unique_suffix()
            account_b = args.account_b or f"p6b{suffix}"
            password_b = args.password_b
            self.b = ApiSession(args.base_url, timeout=args.timeout, verify=not args.insecure_tls,
                                allow_insecure_cookies=args.allow_http)
            if not args.account_b:
                expect(self.register(self.b, account_b, password_b), 201, label="账号 B 注册")
            expect(self.login(self.b, account_b, password_b), 201, label="账号 B 登录")
        except Exception as exc:  # noqa: BLE001
            self.rec.record("TC-P6-AUTH-007", "第二账号建立", "AC-01", BLOCKED, str(exc))
            self.b = None

        self.rec.case("TC-P6-PROF-001", "创建并读取幼儿档案", "AC-02", self.test_profile_create)
        self.rec.case("TC-P6-PROF-002", "修改档案并持久化", "AC-02", self.test_profile_update)
        self.rec.case("TC-P6-PROF-003", "档案字段校验", "AC-02", self.test_profile_validation)
        if self.b is not None:
            self.rec.case("TC-P6-PROF-004", "跨账号档案隔离", "NFR-04", self.test_cross_account_profile)

        self.rec.case("TC-P6-BOOK-001", "绘本列表至少 8 本", "AC-03", self.test_storybook_list)
        self.rec.case("TC-P6-BOOK-002", "绘本筛选与分页边界", "AC-03", self.test_storybook_filters)
        self.rec.case("TC-P6-BOOK-003", "绘本详情与越界访问", "AC-04", self.test_storybook_detail)
        if getattr(self, "profile", None):
            self.rec.case("TC-P6-REC-001", "绘本推荐与理由", "AC-03", self.test_recommendation)

        if getattr(self, "story", None) and getattr(self, "profile", None):
            self.rec.case("TC-P6-SESS-001", "创建阅读会话与 CSRF", "AC-08", self.test_session_create)
            if getattr(self, "session_id", None):
                self.rec.case("TC-P6-SESS-002", "开始阅读、幂等与冲突", "AC-08", self.test_session_events)
                self.rec.case("TC-P6-SESS-003", "翻页与进度持久化", "AC-04", self.test_page_change)
                if self.b is not None:
                    self.rec.case("TC-P6-SESS-004", "跨账号会话隔离", "NFR-04", self.test_cross_account_session)
                self.rec.case("TC-P6-BRANCH-001", "受控故事分支选择", "AC-14", self.test_branch_choice)
                self.rec.case("TC-P6-SESS-005", "结束阅读与小结", "AC-08", self.test_finish_and_report)
                self.rec.case("TC-P6-SESS-006", "阅读历史可查询", "AC-08", self.test_history)
                self.rec.case("TC-P6-SESS-007", "结束后禁止继续写入", "AC-10", self.test_event_after_finish)
                self.rec.case("TC-P6-SESS-008", "删除本人阅读记录", "FR-14", self.test_delete_session)
        else:
            for case_id, title, ac in (
                    ("TC-P6-SESS-001", "创建阅读会话与 CSRF", "AC-08"),
                    ("TC-P6-SESS-002", "开始阅读、幂等与冲突", "AC-08"),
                    ("TC-P6-SESS-005", "结束阅读与小结", "AC-08"),
            ):
                self.rec.record(case_id, title, ac, BLOCKED, "缺少档案或可用绘本")

        if getattr(self, "session_id", None):
            self.rec.case("TC-P6-CHILD-001", "儿童模式授权与家长解锁", "AC-12", self.test_child_mode)
            self.rec.case("TC-P6-VOICE-001", "语音接口鉴权", "AC-07", self.test_voice_auth)
            self.rec.case("TC-P6-VOICE-002", "语音格式与未配置回退", "AC-07", self.test_voice_validation)
        else:
            self.rec.record("TC-P6-CHILD-001", "儿童模式授权与家长解锁", "AC-12", BLOCKED, "缺少可用会话")
            self.rec.record("TC-P6-VOICE-001", "语音接口鉴权", "AC-07", BLOCKED, "缺少可用会话")

        self.rec.case("TC-P6-NFR-001", "接口响应时间抽测", "NFR-03", self.test_performance)
        self.rec.case("TC-P6-SEC-001", "安全响应头检查", "NFR-04", self.test_security_headers)
        self.rec.case("TC-P6-API-004", "接口契约覆盖情况", "NFR-07", self.test_openapi_coverage)
        if args.frontend_url:
            self.rec.case("TC-P6-FE-001", "前端页面可访问", "NFR-01", self.test_frontend)
        else:
            self.rec.record("TC-P6-FE-001", "前端页面可访问", "NFR-01", BLOCKED,
                            "未提供 --frontend-url，前端与移动端兼容性未纳入本轮")
        if args.profile == "public":
            self.rec.case("TC-P6-PUB-001", "公网 HTTPS 与证书", "AC-11", self.test_public_tls)
            if args.http_url:
                self.rec.case("TC-P6-PUB-002", "HTTP 强制跳转 HTTPS", "AC-11", self.test_http_redirect)
            else:
                self.rec.record("TC-P6-PUB-002", "HTTP 强制跳转 HTTPS", "AC-11", BLOCKED,
                                "未提供 --http-url")
        else:
            self.rec.record("TC-P6-PUB-001", "公网 HTTPS 与证书", "AC-11", BLOCKED,
                            "本轮为本地 profile；公网验收需 --profile public 与真实域名")
            self.rec.record("TC-P6-PUB-002", "HTTP 强制跳转 HTTPS", "AC-11", BLOCKED,
                            "本轮为本地 profile")

        # 契约可能比验收驱动更新，以本轮路径观测区分缺失与功能未测。
        paths = getattr(self, "openapi_paths", None)

        def pending(*routes: str) -> str:
            if paths is None:
                return "未取得本轮 OpenAPI，功能用例尚未执行"
            missing = [route for route in routes if route not in paths]
            return "本轮契约路径缺失：" + "、".join(missing) if missing else "路由已注册，本轮未执行功能用例"

        for case_id, title, ac, reason in (
                ("TC-P6-AGENT-001", "情境提问 API-016 与适龄问题", "AC-05",
                 pending("/api/v1/reading-sessions/{session_id}/questions")),
                ("TC-P6-AGENT-002", "自适应引导 API-017 两种分支", "AC-06",
                 pending("/api/v1/reading-sessions/{session_id}/guidance")),
                ("TC-P6-AGENT-003", "原创故事生成与审核 AC-13/15", "AC-13",
                 pending("/api/v1/story-generations", "/api/v1/story-versions/{version_id}/approvals")),
                ("TC-P6-ACT-001", "亲子活动材料与步骤 AC-09", "AC-09",
                 "本轮未验收活动材料与步骤"),
        ):
            self.rec.record(case_id, title, ac, BLOCKED, reason)

        return self._finish()

    def _finish(self) -> int:
        # 报告生成后小结可能仍为 PENDING；补记一条可复核的警告
        report = getattr(self, "report", None)
        if report is not None:
            if report.get("status") in ("PENDING", "FAILED"):
                self.rec.record("TC-P6-REPORT-001", "小结生成状态", "FR-09", WARN,
                                f"report.status={report.get('status')}；"
                                "未配置文本模型时按 FAILED 保留事实摘要，属预期降级")
            elif not (report.get("activities") or []):
                self.rec.record("TC-P6-REPORT-001", "小结生成状态", "FR-09", WARN,
                                "READY 但 activities 为空；FR-10/AC-09 仍未完成")
        summary = self.rec.summary()
        paths = write_reports(self.rec, self.args.outdir, title="知芽 P6 黑盒验收测试报告")
        print("\n" + "=" * 60)
        print(f"合计 {summary['total']} 项：{summary['counts']}")
        for key, path in paths.items():
            print(f"{key}: {path}")
        print("=" * 60)
        return 1 if summary["counts"][FAIL] else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="知芽 P6 黑盒验收测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--profile", choices=["local", "public"], default="local")
    parser.add_argument("--account", help="已有演示账号；不传则自动注册")
    parser.add_argument("--password", default=os.environ.get("P6_ACCEPTANCE_PASSWORD", "P6-test-pass-2026"))
    parser.add_argument("--account-b", help="第二账号；不传则自动注册")
    parser.add_argument("--password-b", default="P6-test-pass-2026")
    parser.add_argument("--story-version-id", type=int)
    parser.add_argument("--min-storybooks", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--performance-ms", type=float, default=3000.0)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--tested-commit", help="被测服务的 Git 提交")
    parser.add_argument("--frontend-url")
    parser.add_argument("--http-url", help="公网 HTTP 入口，用于检查是否强制跳转 HTTPS")
    parser.add_argument("--allow-http", action="store_true", help="允许本地 HTTP 回传 Secure Cookie")
    parser.add_argument("--insecure-tls", action="store_true", help="跳过 TLS 证书校验（仅自签名测试）")
    voice = parser.add_mutually_exclusive_group()
    voice.add_argument("--voice-live", action="store_true", help="允许真实 TTS 合成（可能产生费用）")
    voice.add_argument("--voice-expect-unconfigured", action="store_true",
                        help="确认环境未配置 TTS，调用一次并断言 503（不产生费用）")
    parser.add_argument("--list", action="store_true", help="只列出用例编号并退出")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.list:
        print("\n".join([
            "TC-P6-ENV-001 测试环境与协议检查", "TC-P6-API-001 健康检查与 requestId",
            "TC-P6-API-002/003 错误结构", "TC-P6-AUTH-001..007 账号、Cookie、退出、跨账号",
            "TC-P6-PROF-001..004 档案 CRUD、校验、隔离",
            "TC-P6-BOOK-001..003 绘本列表、筛选、详情",
            "TC-P6-SESS-001..008 会话创建、幂等、翻页、结束、历史、删除",
            "TC-P6-CHILD-001 儿童模式与家长解锁", "TC-P6-VOICE-001..002 语音鉴权与格式",
            "TC-P6-NFR-001 性能抽测", "TC-P6-SEC-001 安全响应头",
            "TC-P6-API-004 契约覆盖", "TC-P6-FE-001 前端", "TC-P6-PUB-001..002 公网",
        ]))
        return 0
    if not args.base_url.startswith(("http://", "https://")):
        print("base-url 必须以 http:// 或 https:// 开头", file=sys.stderr)
        return 2
    acceptance = Acceptance(args)
    try:
        return acceptance.run()
    finally:
        for client in (acceptance.a, acceptance.b, acceptance.anon):
            if client is not None:
                client.close()


if __name__ == "__main__":
    raise SystemExit(main())
