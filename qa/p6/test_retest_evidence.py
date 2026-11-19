"""P6 复测证据回归，使用临时文件、独立 Git 仓库和离线服务替身。"""

import copy
import csv
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest

import build_test_doc
import run_acceptance
import run_model_compare as runner
import run_p6
import summarize_model_compare as scoring


def test_retest_report_preserves_archive_and_previous_run(tmp_path, monkeypatch):
    record = tmp_path / "acceptance-latest.json"
    args = ["--acceptance", str(record), "--no-docx"]
    folder = tmp_path / "archive"
    folder.mkdir()
    archive = {folder / path.name: b"frozen fixture" for path in build_test_doc.ARCHIVED_FILES}
    for path, saved in archive.items():
        path.write_bytes(saved)
    monkeypatch.setattr(build_test_doc, "ARCHIVED_FILES", set(archive))
    for day in (19, 20):
        record.write_text(json.dumps({"meta": {"startedAt": f"2026-11-{day}T01:00:00Z"},
                                      "results": []}), encoding="utf-8")
        assert build_test_doc.main(args) == 0
    reports = list((tmp_path / "retests").glob("*.md"))
    assert len(reports) == 2
    assert {line for report in reports for line in report.read_text("utf-8").splitlines()
            if line.startswith("| 本轮测试日期")} == {
                "| 本轮测试日期 | 2026-11-19 |", "| 本轮测试日期 | 2026-11-20 |"}
    for path, saved in archive.items():
        with pytest.raises(SystemExit):
            build_test_doc.main([*args, "--markdown", str(path)])
        assert path.read_bytes() == saved
    custom = tmp_path / "custom.md"
    assert build_test_doc.main([*args, "--markdown", str(custom)]) == 0
    assert "| 本轮测试日期 | 2026-11-20 |" in custom.read_text("utf-8")


def test_local_revision_covers_staged_unstaged_and_new_inputs(tmp_path):
    def git(*args):
        return subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=True).stdout

    git("init", "-q")
    assert run_p6.local_revision(tmp_path) is None
    source = tmp_path / "backend/app/main.py"
    source.parent.mkdir(parents=True)
    source.write_text("# first\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=P6", "-c", "user.email=p6@example.invalid",
        "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    head = git("rev-parse", "HEAD").decode().strip()
    report = tmp_path / "qa/p6/reports/acceptance-latest.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    assert run_p6.local_revision(tmp_path) == head
    new_source = report.parent.parent / "helper.py"
    new_source.touch()
    assert run_p6.local_revision(tmp_path) == head + "-dirty"
    new_source.unlink()
    source.write_text("# changed\n", encoding="utf-8")
    assert run_p6.local_revision(tmp_path) == head + "-dirty"
    git("add", str(source))
    assert run_p6.local_revision(tmp_path) == head + "-dirty"


@pytest.mark.parametrize("flags", [["--limit", "0"], ["--limit", "-1"], ["--conditions", ""],
                                   ["--conditions", "baseline,baseline"], ["--conditions", "domain,"],
                                   ["--conditions", "unknown"]])
def test_invalid_model_scope_stops_before_any_call(tmp_path, monkeypatch, flags):
    live = Mock(side_effect=AssertionError("不应启动模型"))
    monkeypatch.setattr(runner, "run_live", live)
    assert runner.main(["--outdir", str(tmp_path), "--live", *flags]) == 2
    live.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(("second_text", "expected_code", "valid"), [("   ", 1, 0), ("示例", 0, 1)])
def test_model_run_and_summary_report_actual_outputs(tmp_path, monkeypatch, second_text, expected_code, valid):
    from app.adapters.model import ModelAdapter
    from app.config import Settings
    from app.schemas.agent import ModelGenerateResult

    monkeypatch.setattr(Settings, "from_env", lambda: Settings(model_api_url="https://model.invalid",
                        model_api_key="offline-fixture", model_name="test"))
    monkeypatch.setattr("app.model_gateway.ModelGateway", MagicMock())
    generate = AsyncMock(side_effect=[ModelGenerateResult(success=False, error_code="TIMEOUT"),
                                      ModelGenerateResult(success=True, text=second_text)])
    monkeypatch.setattr(ModelAdapter, "generate", generate)
    args = ["--outdir", str(tmp_path)]
    assert runner.main([*args, "--limit", "1", "--db", str(tmp_path / "empty.db"), "--live"]) == expected_code
    assert generate.await_count == 2
    plan = json.loads((tmp_path / "model-compare-plan.json").read_text("utf-8"))
    meta = json.loads(next(tmp_path.glob("run-*.json")).read_text("utf-8"))
    rows = scoring.load_results(next(tmp_path.glob("results-*.jsonl")))
    assert len(rows) == 2 and {r["planHash"] for r in rows} == {meta["planHash"]} == {scoring.plan_fingerprint(plan)}
    assert scoring.main(args) == expected_code
    summary = json.loads((tmp_path / "model-compare-summary.json").read_text("utf-8"))
    assert (summary["resultRows"], summary["validOutputRows"]) == (2, valid)
    assert bool(list(tmp_path.glob("scoring-*.csv"))) == bool(valid)
    if not valid:
        assert "本轮没有可评分输出" in (tmp_path / "model-compare-summary.md").read_text("utf-8")


def test_legacy_plan_upgrade_preserves_original_date_and_preview_scores(tmp_path):
    args = ["--outdir", str(tmp_path), "--limit", "1"]
    assert runner.main(args) == 0
    path = tmp_path / "model-compare-plan.json"
    previous = json.loads(path.read_text("utf-8"))
    fingerprint = previous.pop("planHash")
    previous["generatedAt"] = "2026-11-18T01:00:00Z"
    path.write_text(json.dumps(previous), encoding="utf-8")
    preview = tmp_path / "scoring.csv"
    saved = preview.read_bytes()
    assert runner.main(args) == 0
    assert json.loads(path.read_text("utf-8")) == {**previous, "planHash": fingerprint}
    assert preview.read_bytes() == saved


def test_changed_plan_and_legacy_results_preserve_valid_summary(tmp_path, capsys):
    args = ["--outdir", str(tmp_path)]
    assert runner.main([*args, "--limit", "1"]) == scoring.main(args) == 0
    plan = json.loads((tmp_path / "model-compare-plan.json").read_text("utf-8"))
    results = tmp_path / "results-A.jsonl"
    row = {"caseId": plan["cases"][0]["caseId"], "condition": "baseline", "runId": "A",
           "model": "offline", "output": "fixture", "success": True, "planHash": plan["planHash"]}
    results.write_text(json.dumps(row), encoding="utf-8")
    assert scoring.main(args) == 0
    summary = tmp_path / "model-compare-summary.json"
    saved = summary.read_bytes()
    other = copy.deepcopy(plan)
    other["cases"][0]["pageText"] += "另一个页面"
    changed_plan = tmp_path / "other-plan.json"
    for fingerprint in (plan["planHash"], scoring.plan_fingerprint(other)):
        other["planHash"] = fingerprint
        changed_plan.write_text(json.dumps(other), encoding="utf-8")
        assert scoring.main([*args, "--plan", str(changed_plan)]) == 2
        assert not scoring.eligible_outputs(other, [row]) and summary.read_bytes() == saved
    del row["planHash"]
    results.write_text(json.dumps(row), encoding="utf-8")
    assert scoring.main(args) == 2 and "缺少计划指纹" in capsys.readouterr().err
    assert summary.read_bytes() == saved and list(tmp_path.glob("outputs-for-scoring-*.md"))
    results.write_bytes(b"")
    assert scoring.main(args) == 1 and "本轮没有可评分输出" in (tmp_path / "model-compare-summary.md").read_text("utf-8")


def test_scoring_retry_keeps_complete_rows_and_notes(tmp_path):
    def interrupted():
        yield {"key": "baseline"}
        raise OSError("模拟中断")

    with pytest.raises(OSError, match="模拟中断"):
        scoring.publish_csv(tmp_path / "interrupted.csv", ["key"], interrupted())
    assert not list(tmp_path.iterdir())
    outputs = {c: {"caseId": "C", "condition": c, "model": "stub", "output": c,
                   "runId": "A", "planHash": "plan"} for c in ("baseline", "domain")}
    def sheet():
        return scoring.score_sheet(tmp_path, Path("results-A.jsonl"), outputs, ["安全性"])

    original = sheet()
    with original.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns, first = reader.fieldnames, next(reader)
    first.update(rater1_安全性="0", rater1_note="保留这条意见")
    with original.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerow(first)
    saved = original.read_bytes()
    recovered = sheet()
    assert recovered != original and sheet() == recovered and original.read_bytes() == saved
    with recovered.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 and rows[0] == first and rows[1]["rater1_安全性"] == ""
    assert not scoring.publish_csv(original, columns, []) and original.read_bytes() == saved
    target = tmp_path / "concurrent.csv"
    with ThreadPoolExecutor(4) as pool:
        published = list(pool.map(lambda _: scoring.publish_csv(target, columns, rows), range(4)))
    assert sum(published) == 1
    with target.open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == rows


@pytest.fixture
def acceptance(monkeypatch):
    monkeypatch.setattr(run_acceptance, "ApiSession", Mock(side_effect=lambda *args, **kwargs: Mock()))
    return run_acceptance.Acceptance(run_acceptance.parse_args([]))


@pytest.mark.parametrize(("login_status", "detail_id", "status", "expected"),
                         [(201, 7, "FINISHED", "PASS"), (201, 8, "FINISHED", "FAIL"),
                          (201, 7, "ACTIVE", "FAIL"), (429, 7, "FINISHED", "BLOCKED")])
def test_history_uses_only_fresh_owner_session(acceptance, monkeypatch, login_status, detail_id, status, expected):
    client = Mock()
    monkeypatch.setattr(run_acceptance, "ApiSession", Mock(return_value=client))
    acceptance.account, acceptance.password, acceptance.session_id = "fixture", "fixture-only", 7
    client.post.return_value = httpx.Response(login_status, json={})
    client.get.side_effect = [
        httpx.Response(200, json={"data": {"items": [{"readingSessionId": 7, "status": "FINISHED"}]}}),
        httpx.Response(200, json={"data": {"session": {"readingSessionId": detail_id, "status": status}}})]
    assert acceptance.rec.case("history", "", "", acceptance.test_history).status == expected
    client.post.assert_called_once_with("/api/v1/auth/sessions", json={"account": "fixture", "password": "fixture-only"})
    client.close.assert_called_once()
    assert client.get.call_count == (0 if login_status == 429 else 2)
    assert not acceptance.a.mock_calls


def test_one_page_story_blocks_page_change(acceptance):
    acceptance.session_id, acceptance.story = 7, {"pages": [{"defaultNextPageNumber": None}]}
    assert acceptance.rec.case("page", "", "", acceptance.test_page_change).status == "BLOCKED"
    assert not acceptance.a.mock_calls


@pytest.mark.parametrize(("entry", "location", "expected"), [
    ("http://entry.example", "https://app.example:443/home", "PASS"),
    ("http://entry.example", "https://other.example/home", "FAIL"),
    ("http://entry.example", "https://app.example:8443/home", "FAIL"),
    ("http://entry.example", "/home", "FAIL"),
    ("http://entry.example", "", "FAIL"),
    ("https://entry.example", "https://app.example", "FAIL"),
])
def test_redirect_targets_configured_https_origin(acceptance, monkeypatch, entry, location, expected):
    acceptance.args.http_url, acceptance.args.base_url = entry, "https://app.example"
    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    client.get.return_value = httpx.Response(301, headers={"Location": location})
    monkeypatch.setattr(httpx, "Client", factory)
    assert acceptance.rec.case("redirect", "", "", acceptance.test_http_redirect).status == expected
    if entry.startswith("http://"):
        assert factory.call_args.kwargs["follow_redirects"] is False
        client.get.assert_called_once()
    else:
        factory.assert_not_called()


@pytest.mark.parametrize(("frame", "policy", "expected"), [("DENY", "default-src 'self'", "PASS"),
                          ("sameorigin", "default-src 'self'", "PASS"), ("unknown", "default-src 'self'", "WARN"),
                          ("DENY", "", "WARN")])
def test_security_header_status_matches_evidence(acceptance, frame, policy, expected):
    headers = {"X-Content-Type-Options": "NoSniff", "X-Frame-Options": frame,
               "Content-Security-Policy": policy, "Referrer-Policy": "same-origin"}
    acceptance.anon.get.return_value = httpx.Response(200, headers=headers)
    result = acceptance.rec.case("headers", "", "", acceptance.test_security_headers)
    assert result.status == expected
    assert result.evidence["missing"] == ([] if policy else ["content-security-policy"])
    assert result.evidence["invalid"] == (["x-frame-options"] if frame == "unknown" else [])
