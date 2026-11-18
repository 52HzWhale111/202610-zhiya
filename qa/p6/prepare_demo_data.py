"""准备 P6 验收/演示用的本地数据。

做三件事：
1. 初始化并导入仓库种子（种子默认全是 DRAFT，公开书架为空）；
2. 审核发布绘本与知识条目，使书架、推荐和 Agent 检索可用；
3. 创建演示家长账号和幼儿档案（已存在则复用）。

只操作本地 SQLite，不调用任何模型或语音供应商。生产库请勿直接运行。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.api.v1.auth import password_hash  # noqa: E402

SCHEMA = ROOT / "database" / "schema.sql"
INIT = ROOT / "database" / "init_db.py"
IMPORT = ROOT / "database" / "import_seed.py"
DEFAULT_DB = ROOT / "database" / "instance" / "zhiya.sqlite3"
DEFAULT_ACCOUNT = "p6demo"
DEFAULT_PASSWORD = "Zhiya-demo-2026"


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    mode = "rwc" if create else "rw"
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def ensure_database(path: Path) -> bool:
    """返回是否新建了数据库。缺失时直接执行 schema 与种子导入。"""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path, create=True)) as db, db:
        db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA.read_text(encoding="utf-8"))
    sys.path.insert(0, str(ROOT / "database"))
    from import_seed import import_seed  # noqa: WPS433 - 只在需要时导入
    import_seed(path)
    return True


def ensure_account(db: sqlite3.Connection, account: str, password: str, role: str) -> int:
    row = db.execute("SELECT id, role FROM accounts WHERE account = ?", (account,)).fetchone()
    now = int(time.time() * 1000)
    if row:
        return int(row["id"])
    cursor = db.execute(
        "INSERT INTO accounts (account, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (account, password_hash(password), role, now),
    )
    return int(cursor.lastrowid)


def ensure_branch(db: sqlite3.Connection, version_id: int) -> None:
    """为验收准备一条已审核分支（第 1 页 -> 第 2 页）。

    种子没有 story_branches 记录；没有分支就无法验证 API-018。这里只在本地
    演示库插入一条明确标注的测试分支，不修改仓库种子文件。
    """
    if db.execute("SELECT 1 FROM story_branches WHERE version_id = ? AND review_status = 'APPROVED'",
                  (version_id,)).fetchone():
        return
    if not db.execute("SELECT 1 FROM story_pages WHERE version_id = ? AND page_no = 2",
                      (version_id,)).fetchone():
        return
    db.execute(
        """INSERT INTO story_branches (version_id, from_page_no, next_page_no, choice_label, review_status)
           VALUES (?, 1, 2, 'P6测试分支', 'APPROVED')""",
        (version_id,),
    )


def approve_content(db: sqlite3.Connection, reviewer_id: int, limit: int | None) -> dict:
    now = int(time.time() * 1000)
    versions = db.execute(
        """SELECT v.id, v.title, v.age_min, v.age_max, b.source_type
           FROM story_versions v JOIN storybooks b ON b.id = v.storybook_id
           WHERE v.status = 'DRAFT' ORDER BY v.id"""
    ).fetchall()
    if limit is not None:
        versions = versions[:limit]
    approved = []
    for row in versions:
        if row["source_type"] != "LIBRARY":
            continue
        db.execute(
            """UPDATE story_versions SET status = 'PUBLISHED', reviewed_by_id = ?, reviewed_at = ?
               WHERE id = ? AND status = 'DRAFT'""",
            (reviewer_id, now, row["id"]),
        )
        approved.append({"storyVersionId": row["id"], "title": row["title"],
                         "ageMin": row["age_min"], "ageMax": row["age_max"]})
        ensure_branch(db, row["id"])
    knowledge = db.execute(
        "UPDATE knowledge_items SET status = 'APPROVED', reviewed_at = ? WHERE status = 'DRAFT'",
        (now,),
    ).rowcount
    db.commit()
    return {"storyVersions": approved, "knowledgeApproved": knowledge}


def ensure_profile(db: sqlite3.Connection, guardian_id: int, nickname: str) -> dict:
    row = db.execute(
        "SELECT * FROM child_profiles WHERE guardian_id = ? ORDER BY id LIMIT 1", (guardian_id,)
    ).fetchone()
    if row:
        return {"childProfileId": row["id"], "ageBand": row["age_band"],
                "nickname": row["nickname"], "created": False}
    now = int(time.time() * 1000)
    cursor = db.execute(
        """INSERT INTO child_profiles
           (guardian_id, nickname, age_band, interests_json, preferred_minutes, created_at, updated_at)
           VALUES (?, ?, 'AGE_4_5', ?, 10, ?, ?)""",
        (guardian_id, nickname, json.dumps(["动物", "友谊"], ensure_ascii=False), now, now),
    )
    return {"childProfileId": int(cursor.lastrowid), "ageBand": "AGE_4_5",
            "nickname": nickname, "created": True}


def check(path: Path) -> dict:
    with closing(connect(path)) as db:
        counts = {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("accounts", "child_profiles", "story_versions", "story_pages", "knowledge_items")
        }
        published = db.execute(
            "SELECT count(*) FROM story_versions WHERE status = 'PUBLISHED'"
        ).fetchone()[0]
        approved = db.execute(
            "SELECT count(*) FROM knowledge_items WHERE status = 'APPROVED'"
        ).fetchone()[0]
        return {**counts, "publishedVersions": published, "approvedKnowledge": approved}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 数据库路径")
    parser.add_argument("--account", default=DEFAULT_ACCOUNT, help="演示家长账号")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="演示家长密码")
    parser.add_argument("--nickname", default="小芽", help="演示幼儿昵称")
    parser.add_argument("--limit", type=int, default=12, help="最多发布多少本绘本；0 表示全部")
    parser.add_argument("--yes", action="store_true", help="确认修改数据库")
    parser.add_argument("--check-only", action="store_true", help="只查看当前数据库状态")
    args = parser.parse_args()

    path = args.db.resolve()
    if not path.exists() and not args.yes:
        print(json.dumps({"error": "数据库不存在；首次准备请加 --yes",
                          "path": str(path)}, ensure_ascii=False, indent=2))
        return 2
    if args.check_only:
        print(json.dumps({"path": str(path), **check(path)}, ensure_ascii=False, indent=2))
        return 0
    if not args.yes:
        print(json.dumps({"error": "该命令会审核发布种子内容；确认请加 --yes",
                          "path": str(path)}, ensure_ascii=False, indent=2))
        return 2

    created = ensure_database(path)
    with closing(connect(path)) as db, db:
        admin_id = ensure_account(db, f"{args.account}-admin", args.password, "ADMIN")
        guardian_id = ensure_account(db, args.account, args.password, "GUARDIAN")
        approved = approve_content(db, admin_id, args.limit or None)
        profile = ensure_profile(db, guardian_id, args.nickname)

    story_ids = [item["storyVersionId"] for item in approved["storyVersions"]]
    result = {
        "path": str(path),
        "databaseCreated": created,
        "demoAccount": args.account,
        "demoPassword": args.password,
        "guardianId": guardian_id,
        "profile": profile,
        "publishedCount": len(story_ids),
        "storyVersionIds": story_ids,
        "knowledgeApproved": approved["knowledgeApproved"],
        "check": check(path),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
