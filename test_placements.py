"""渠道投放台账事务编排（placements 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
登记时同一事务复核来源（原访谈仍在、资料与封存时一致、当前整段可发布，
否则说明原因且不留记录）、查阅时一致读取快照中的投放状态与凭据风险
（正常 / 需处理 / 来源缺失，历史链接与封存结论保留）、
撤下保留撤下时间且凭据不改动、台账记录不可删除。
"""
import os
import sqlite3
import tempfile
import unittest

from credentials import create_credential
from placements import (
    RISK_MISSING,
    RISK_OK,
    RISK_REVIEW,
    STATUS_ACTIVE,
    STATUS_WITHDRAWN,
    list_placements,
    placement_review,
    register_placement,
    withdraw_placement,
)

# 与 app.py 中 SCHEMA 保持一致（测试夹具，独立维护）
SCHEMA = """
CREATE TABLE interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0)
);
CREATE TABLE speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    allow_media TEXT NOT NULL DEFAULT '[]',
    deny_media TEXT NOT NULL DEFAULT '[]',
    embargo_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE release_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    interview_title TEXT NOT NULL,
    medium TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER release_credentials_no_update
BEFORE UPDATE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可修改'); END;
CREATE TRIGGER release_credentials_no_delete
BEFORE DELETE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可删除'); END;
CREATE TABLE channel_placements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    channel TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    withdraw_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at TEXT
);
CREATE TRIGGER channel_placements_no_delete
BEFORE DELETE ON channel_placements
BEGIN SELECT RAISE(ABORT, '投放记录不可删除'); END;
CREATE TRIGGER channel_placements_sealed_fields
BEFORE UPDATE ON channel_placements
WHEN OLD.credential_id <> NEW.credential_id
  OR OLD.channel <> NEW.channel
  OR OLD.url <> NEW.url
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, '投放记录的凭据、渠道、链接与登记时间不可修改'); END;
CREATE TRIGGER channel_placements_no_reopen
BEFORE UPDATE ON channel_placements
WHEN OLD.status = 'withdrawn'
BEGIN SELECT RAISE(ABORT, '投放记录已撤下，不可再修改'); END;
"""

PUB_DATE = "2026-10-04"
CHANNEL = "官网专题页"
URL = "https://example.com/features/oral-history-1"


class PlacementTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频
    -> 整条时间轴可发布，凭据已封存。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        c = self.conn
        c.execute("INSERT INTO interviews (title, duration_seconds)"
                  " VALUES ('访谈', 600)")
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '甲')")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        c.commit()
        result, status = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="audio")
        assert status == 201
        self.credential_id = result["id"]

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def register(self, cid=None, channel=CHANNEL, url=URL):
        return register_placement(
            self.conn, credential_id=cid or self.credential_id,
            channel=channel, url=url)

    def placement_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM channel_placements").fetchone()["n"]

    # ---------------------------------------------------------- 登记

    def test_register_success_saves_active_record_with_timestamp(self):
        result, status = self.register()
        self.assertEqual(status, 201)
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], STATUS_ACTIVE)
        self.assertEqual(result["status_label"], "投放中")
        self.assertEqual(result["channel"], CHANNEL)
        self.assertEqual(result["url"], URL)
        self.assertTrue(result["created_at"])  # 登记时间
        self.assertEqual(self.placement_count(), 1)
        row = self.conn.execute(
            "SELECT * FROM channel_placements WHERE id = ?",
            (result["id"],)).fetchone()
        self.assertEqual(row["credential_id"], self.credential_id)
        self.assertIsNone(row["withdrawn_at"])

    def test_register_unknown_credential_returns_404(self):
        result, status = self.register(cid=9999)
        self.assertEqual(status, 404)
        self.assertIn("error", result)
        self.assertEqual(self.placement_count(), 0)

    def test_register_missing_source_leaves_no_record(self):
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "missing")
        self.assertIn("来源缺失", result["error"])
        self.assertEqual(self.placement_count(), 0)  # 不留记录

    def test_register_changed_materials_leaves_no_record(self):
        # 追加一条同样允许音频的授权：仍可发布，但资料与封存时不一致
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "changed")
        self.assertEqual(result["changes"], ["授权"])
        self.assertIn("需处理", result["error"])
        self.assertEqual(self.placement_count(), 0)  # 不留记录

    def test_register_blocked_materials_leaves_no_record(self):
        # 把授权改为拒绝音频：资料变化且当前不可发布（变化原因优先说明）
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]',"
            " deny_media = '[\"audio\"]' WHERE speaker_id = 1")
        self.conn.commit()
        result, status = self.register()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertEqual(self.placement_count(), 0)

    # ---------------------------------------------------------- 查阅

    def test_list_and_detail_show_status_and_risk(self):
        pid = self.register()[0]["id"]
        items = list_placements(self.conn)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], pid)
        self.assertEqual(item["interview_title"], "访谈")
        self.assertEqual(item["medium"], "audio")
        self.assertEqual(item["publication_date"], PUB_DATE)
        self.assertEqual(item["status_label"], "投放中")
        self.assertEqual(item["risk"], RISK_OK)
        self.assertEqual(item["risk_label"], "正常")
        self.assertEqual(item["changes"], [])
        detail = placement_review(self.conn, pid)
        self.assertEqual(detail["risk"], RISK_OK)
        self.assertEqual(detail["current"]["blocked"], [])
        # 封存结论保留
        sealed = detail["credential"]["sealed_result"]
        self.assertEqual(sealed["summary"]["publishable_seconds"], 600)

    def test_review_flags_changes_as_needs_handling(self):
        pid = self.register()[0]["id"]
        # 资料后来变化（即便仍可发布）-> 需处理
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        item = list_placements(self.conn)[0]
        self.assertEqual(item["risk"], RISK_REVIEW)
        self.assertEqual(item["risk_label"], "需处理")
        self.assertEqual(item["changes"], ["授权"])
        detail = placement_review(self.conn, pid)
        self.assertEqual(detail["risk"], RISK_REVIEW)
        self.assertEqual(detail["current"]["blocked"], [])  # 即便仍可发布

    def test_review_lists_current_blocked_intervals(self):
        pid = self.register()[0]["id"]
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]',"
            " deny_media = '[\"audio\"]' WHERE speaker_id = 1")
        self.conn.commit()
        detail = placement_review(self.conn, pid)
        self.assertEqual(detail["risk"], RISK_REVIEW)
        blocked = detail["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (0, 600))
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))

    def test_missing_source_keeps_history_and_sealed_result(self):
        pid = self.register()[0]["id"]
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        item = list_placements(self.conn)[0]
        self.assertEqual(item["risk"], RISK_MISSING)
        self.assertEqual(item["risk_label"], "来源缺失")
        # 历史链接与时间仍保留
        self.assertEqual(item["url"], URL)
        self.assertTrue(item["created_at"])
        detail = placement_review(self.conn, pid)
        self.assertEqual(detail["risk"], RISK_MISSING)
        self.assertIsNone(detail["current"])
        self.assertEqual(detail["url"], URL)
        self.assertEqual(detail["channel"], CHANNEL)
        # 封存结论仍保留
        sealed = detail["credential"]["sealed_result"]
        self.assertEqual(sealed["summary"]["publishable_seconds"], 600)

    def test_review_unknown_placement_returns_none(self):
        self.assertIsNone(placement_review(self.conn, 9999))

    # ---------------------------------------------------------- 撤下

    def test_withdraw_marks_record_and_keeps_timestamp(self):
        pid = self.register()[0]["id"]
        result, status = withdraw_placement(self.conn, pid, "内容已下线")
        self.assertEqual(status, 200)
        self.assertTrue(result["withdrawn"])
        self.assertEqual(result["status"], STATUS_WITHDRAWN)
        self.assertEqual(result["status_label"], "已撤下")
        self.assertTrue(result["withdrawn_at"])  # 撤下时间
        self.assertEqual(result["withdraw_reason"], "内容已下线")
        # 查阅：状态为已撤下，风险提示仍可查
        detail = placement_review(self.conn, pid)
        self.assertEqual(detail["status"], STATUS_WITHDRAWN)
        self.assertEqual(detail["withdrawn_at"], result["withdrawn_at"])
        self.assertEqual(detail["withdraw_reason"], "内容已下线")
        self.assertEqual(detail["risk"], RISK_OK)

    def test_withdraw_unknown_placement_returns_404(self):
        result, status = withdraw_placement(self.conn, 9999, "原因")
        self.assertEqual(status, 404)

    def test_withdraw_twice_keeps_original_timestamp(self):
        pid = self.register()[0]["id"]
        first, _ = withdraw_placement(self.conn, pid, "第一次")
        result, status = withdraw_placement(self.conn, pid, "第二次")
        self.assertEqual(status, 409)
        self.assertFalse(result["withdrawn"])
        self.assertEqual(result["withdrawn_at"], first["withdrawn_at"])
        self.assertEqual(result["withdraw_reason"], "第一次")  # 保留原记录

    def test_withdraw_does_not_touch_credential(self):
        pid = self.register()[0]["id"]
        before = self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (self.credential_id,)).fetchone()
        withdraw_placement(self.conn, pid, "内容已下线")
        after = self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (self.credential_id,)).fetchone()
        self.assertEqual(dict(before), dict(after))  # 凭据不改动

    # ---------------------------------------------------------- 台账保护

    def test_placement_records_are_append_only(self):
        pid = self.register()[0]["id"]
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM channel_placements")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_placements SET channel = '别的渠道'")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_placements SET url = 'https://x.example'")
        self.conn.rollback()
        self.assertEqual(self.placement_count(), 1)
        # 撤下后不可再修改（撤下时间随之保留）
        withdraw_placement(self.conn, pid, "内容已下线")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE channel_placements SET status = 'active'")
        self.conn.rollback()


if __name__ == "__main__":
    unittest.main()
