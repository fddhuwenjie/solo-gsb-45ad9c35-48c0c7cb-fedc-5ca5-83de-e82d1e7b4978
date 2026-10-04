"""发行凭据事务编排（credentials 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
全部可发布才封存、失败不留记录、凭据不可修改 / 不可删除、
查阅时保留发行时结论并在一致读取快照中复核现状
（与发行时一致 / 待复核 / 来源缺失）。
"""
import json
import os
import sqlite3
import tempfile
import unittest

from credentials import (
    STATUS_MISSING,
    STATUS_REVIEW,
    STATUS_VALID,
    create_credential,
    credential_review,
    list_credentials,
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
"""

PUB_DATE = "2026-10-04"


class CredentialTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，乙发言 300-600；
    甲授权 0-600 允许音频，乙授权 0-600 允许音频 -> 整条时间轴可发布。"""

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
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '乙')")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 2, 300, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (2, 0, 600, '[\"audio\"]', '[]', NULL, '')")
        c.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def create(self, iid=1, medium="audio", pub_date=PUB_DATE):
        return create_credential(
            self.conn, interview_id=iid,
            publication_date=pub_date, medium=medium)

    def credential_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM release_credentials").fetchone()["n"]

    # ---------------------------------------------------------- 创建与封存

    def test_create_success_seals_inputs_and_result(self):
        result, status = self.create()
        self.assertEqual(status, 201)
        self.assertTrue(result["created"])
        self.assertEqual(result["interview_title"], "访谈")
        self.assertEqual(result["result"]["summary"]["blocked_seconds"], 0)
        self.assertEqual(self.credential_count(), 1)
        # 封存当时的完整输入与校验结果
        row = self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (result["id"],)).fetchone()
        self.assertEqual((row["interview_id"], row["medium"],
                          row["publication_date"]), (1, "audio", PUB_DATE))
        snapshot = json.loads(row["snapshot"])
        self.assertEqual(snapshot["duration_seconds"], 600)
        self.assertEqual([s["name"] for s in snapshot["speakers"]], ["甲", "乙"])
        self.assertEqual(len(snapshot["segments"]), 2)
        self.assertEqual(len(snapshot["licenses"]), 2)
        sealed = json.loads(row["result"])
        self.assertEqual(sealed["summary"]["publishable_seconds"], 600)
        self.assertTrue(all(i["status"] != "blocked"
                            for i in sealed["intervals"]))

    def test_create_blocked_returns_409_and_leaves_no_record(self):
        self.conn.execute("DELETE FROM licenses WHERE speaker_id = 2")
        self.conn.commit()
        result, status = self.create()
        self.assertEqual(status, 409)
        self.assertFalse(result["created"])
        self.assertIn("error", result)
        self.assertEqual(self.credential_count(), 0)  # 失败不留记录
        # 返回每个阻断区间及原因（含时间码）
        self.assertEqual(len(result["blocked"]), 1)
        blocked = result["blocked"][0]
        self.assertEqual((blocked["start_seconds"], blocked["end_seconds"]),
                         (300, 600))
        self.assertIn("start_timecode", blocked)
        self.assertTrue(any("乙" in r and "缺项" in r
                            for r in blocked["reasons"]))

    def test_create_missing_interview_returns_404(self):
        result, status = self.create(iid=9999)
        self.assertEqual(status, 404)
        self.assertEqual(self.credential_count(), 0)

    def test_credential_is_immutable(self):
        _, status = self.create()
        self.assertEqual(status, 201)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE release_credentials SET medium = 'text'")
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM release_credentials")
        self.conn.rollback()
        self.assertEqual(self.credential_count(), 1)

    # ---------------------------------------------------------- 查阅与复核

    def test_review_unchanged_is_valid(self):
        cid = self.create()[0]["id"]
        review = credential_review(self.conn, cid)
        self.assertEqual(review["status"], STATUS_VALID)
        self.assertEqual(review["status_label"], "与发行时一致")
        self.assertEqual(review["changes"], [])
        self.assertEqual(review["current"]["blocked"], [])
        # 发行时结论保留
        self.assertEqual(
            review["sealed"]["result"]["summary"]["publishable_seconds"], 600)

    def test_review_flags_any_change_even_if_still_publishable(self):
        cid = self.create()[0]["id"]
        # 追加一条同样允许音频的授权：重新校验仍可发布，但资料有变化
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 0, 600, '[\"audio\"]', '[]', NULL, '补充')")
        self.conn.commit()
        review = credential_review(self.conn, cid)
        self.assertEqual(review["status"], STATUS_REVIEW)
        self.assertEqual(review["status_label"], "待复核")
        self.assertEqual(review["changes"], ["授权"])
        self.assertEqual(review["current"]["blocked"], [])  # 即便仍可发布

    def test_review_detects_each_kind_of_change(self):
        cases = [
            ("UPDATE interviews SET duration_seconds = 700", "时长"),
            ("UPDATE speakers SET name = '甲某' WHERE id = 1", "说话人"),
            ("UPDATE segments SET end_seconds = 500 WHERE id = 1", "发言"),
            ("UPDATE licenses SET note = '改' WHERE id = 1", "授权"),
        ]
        for sql, label in cases:
            with self.subTest(change=label):
                cid = self.create()[0]["id"]
                self.conn.execute(sql)
                self.conn.commit()
                review = credential_review(self.conn, cid)
                self.assertEqual(review["status"], STATUS_REVIEW)
                self.assertIn(label, review["changes"])

    def test_review_lists_current_blocked_intervals(self):
        cid = self.create()[0]["id"]
        # 把乙的授权改为拒绝音频 -> 当前重新校验出现阻断
        self.conn.execute(
            "UPDATE licenses SET allow_media = '[]', deny_media = '[\"audio\"]'"
            " WHERE speaker_id = 2")
        self.conn.commit()
        review = credential_review(self.conn, cid)
        self.assertEqual(review["status"], STATUS_REVIEW)
        self.assertEqual(review["changes"], ["授权"])
        blocked = review["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (300, 600))
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))
        # 发行时结论不受影响
        self.assertEqual(
            review["sealed"]["result"]["summary"]["blocked_seconds"], 0)

    def test_review_revalidates_with_sealed_date_and_medium(self):
        cid = self.create()[0]["id"]
        # 封存后把乙的授权改为 2030 年才解禁：
        # 复核按封存的发布日期（2026-10-04）重新校验 -> 未解禁阻断
        self.conn.execute(
            "UPDATE licenses SET embargo_until = '2030-01-01'"
            " WHERE speaker_id = 2")
        self.conn.commit()
        review = credential_review(self.conn, cid)
        self.assertEqual(review["status"], STATUS_REVIEW)
        self.assertEqual(review["changes"], ["授权"])
        blocked = review["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertTrue(any("未解禁" in r for r in blocked[0]["reasons"]))
        # 复核沿用封存的发布日期与媒介
        self.assertEqual(review["publication_date"], PUB_DATE)
        self.assertEqual(review["medium"], "audio")

    def test_review_missing_source_still_viewable(self):
        cid = self.create()[0]["id"]
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        review = credential_review(self.conn, cid)
        self.assertIsNotNone(review)  # 历史凭据仍可查看
        self.assertEqual(review["status"], STATUS_MISSING)
        self.assertEqual(review["status_label"], "来源缺失")
        self.assertIsNone(review["current"])
        # 封存的完整输入与发行时结论仍在
        self.assertEqual(review["sealed"]["inputs"]["duration_seconds"], 600)
        self.assertEqual(len(review["sealed"]["inputs"]["licenses"]), 2)
        self.assertEqual(
            review["sealed"]["result"]["summary"]["publishable_seconds"], 600)

    def test_review_unknown_credential_returns_none(self):
        self.assertIsNone(credential_review(self.conn, 9999))

    # ---------------------------------------------------------- 列表

    def test_list_credentials_with_status(self):
        cid1 = self.create()[0]["id"]
        # 第二条凭据属于另一个访谈，随后删除该访谈 -> 来源缺失
        self.conn.execute(
            "INSERT INTO interviews (title, duration_seconds)"
            " VALUES ('另一访谈', 60)")
        self.conn.commit()
        cid2 = self.create(iid=2)[0]["id"]
        self.conn.execute("DELETE FROM interviews WHERE id = 2")
        # 第一条凭据的访谈改动说话人 -> 待复核
        self.conn.execute("UPDATE speakers SET name = '甲某' WHERE id = 1")
        self.conn.commit()
        items = list_credentials(self.conn)
        self.assertEqual([i["id"] for i in items], [cid1, cid2])
        by_id = {i["id"]: i for i in items}
        self.assertEqual(by_id[cid1]["status"], STATUS_REVIEW)
        self.assertEqual(by_id[cid1]["changes"], ["说话人"])
        self.assertEqual(by_id[cid2]["status"], STATUS_MISSING)
        self.assertEqual(by_id[cid2]["interview_title"], "另一访谈")
        # 列表项不含封存细节（详情接口提供）
        self.assertNotIn("sealed", by_id[cid1])


if __name__ == "__main__":
    unittest.main()
