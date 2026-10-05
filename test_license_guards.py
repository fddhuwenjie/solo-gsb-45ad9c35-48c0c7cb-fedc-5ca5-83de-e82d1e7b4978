"""授权变更保护（license_guards 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证修改 / 删除说话人授权时的
提交前复核 —— 在同一写事务中读取授权归属并试用变更，按该访谈每条
仍在投记录所关联凭据的发布日期与媒介重新校验整段；只要有一个版本
出现不可发布区间就回滚整笔变更，返回受影响投放 ID、链接、阻断区间
及原因；已撤下的投放不阻拦；无阻断时照常提交，封存凭据不改动。
"""
import os
import sqlite3
import tempfile
import unittest

from credentials import create_credential
from license_guards import delete_license, update_license
from placements import register_placement, withdraw_placement

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

VALUES_AUDIO = {
    "start_seconds": 0,
    "end_seconds": 600,
    "allow_media": ["audio"],
    "deny_media": [],
    "embargo_until": None,
    "note": "",
}


class LicenseGuardTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频
    -> 凭据已封存且有一条投放中记录。"""

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
        result, status = register_placement(
            self.conn, credential_id=self.credential_id,
            channel="官网专题页", url="https://example.com/f/1")
        assert status == 201
        self.placement_id = result["id"]

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def license_row(self):
        return self.conn.execute(
            "SELECT * FROM licenses WHERE id = 1").fetchone()

    # ---------------------------------------------------------- 修改

    def test_update_that_keeps_publishable_is_committed(self):
        values = {**VALUES_AUDIO, "note": "补了备注"}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 200)
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["speaker_id"], 1)
        self.assertEqual(result["note"], "补了备注")
        self.assertEqual(self.license_row()["note"], "补了备注")

    def test_update_blocking_rolls_back_with_placement_details(self):
        values = {**VALUES_AUDIO, "allow_media": [],
                  "deny_media": ["audio"]}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 409)
        self.assertFalse(result["allowed"])
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["action"], "update")
        self.assertEqual(result["license_id"], 1)
        affected = result["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], self.placement_id)
        self.assertEqual(item["credential_id"], self.credential_id)
        self.assertEqual(item["channel"], "官网专题页")
        self.assertEqual(item["url"], "https://example.com/f/1")
        self.assertEqual(item["medium"], "audio")
        self.assertEqual(item["medium_label"], "音频")
        self.assertEqual(item["publication_date"], PUB_DATE)
        blocked = item["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (0, 600))
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))
        # 整笔变更已回滚：授权仍是原样
        row = self.license_row()
        self.assertEqual(row["allow_media"], '["audio"]')
        self.assertEqual(row["deny_media"], "[]")
        # 投放记录仍是投放中，凭据封存内容不改动
        placement = self.conn.execute(
            "SELECT * FROM channel_placements WHERE id = ?",
            (self.placement_id,)).fetchone()
        self.assertEqual(placement["status"], "active")

    def test_update_shrinking_range_blocks_on_uncovered_interval(self):
        # 授权缩短为 0-300：300-600 缺项不可发布
        values = {**VALUES_AUDIO, "end_seconds": 300}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 409)
        blocked = result["affected_placements"][0]["blocked"]
        self.assertEqual([(b["start_seconds"], b["end_seconds"])
                          for b in blocked], [(300, 600)])
        self.assertTrue(any("缺项" in r for r in blocked[0]["reasons"]))
        # 回滚：仍是 0-600
        row = self.license_row()
        self.assertEqual((row["start_seconds"], row["end_seconds"]), (0, 600))

    def test_update_unknown_license_returns_404(self):
        result, status = update_license(self.conn, 9999, VALUES_AUDIO)
        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def test_withdrawn_placement_does_not_block_update(self):
        result, status = withdraw_placement(
            self.conn, self.placement_id, "内容已下线")
        self.assertEqual(status, 200)
        values = {**VALUES_AUDIO, "allow_media": [],
                  "deny_media": ["audio"]}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 200)  # 已撤下不阻拦，照常提交
        self.assertEqual(self.license_row()["deny_media"], '["audio"]')

    def test_update_evaluates_each_active_placement_own_credential(self):
        # 再封存一份文字凭据并登记投放：改成只允许音频时仅文字投放被阻断，
        # 但整笔变更仍要回滚，明细只列文字那一条。
        c = self.conn
        c.execute("UPDATE licenses SET allow_media = '[\"audio\", \"text\"]'")
        c.commit()
        result, status = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="text")
        assert status == 201
        text_cid = result["id"]
        result, status = register_placement(
            self.conn, credential_id=text_cid,
            channel="文字专栏", url="https://example.com/t/1")
        assert status == 201
        text_pid = result["id"]
        values = {**VALUES_AUDIO, "allow_media": ["audio"]}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 409)
        affected = result["affected_placements"]
        self.assertEqual([a["placement_id"] for a in affected], [text_pid])
        self.assertEqual(affected[0]["medium"], "text")
        # 回滚后授权仍允许音频 + 文字
        self.assertEqual(self.license_row()["allow_media"],
                         '["audio", "text"]')

    def test_update_revalidates_against_each_credential_publication_date(self):
        # 授权加解禁日 2027-01-01：发布日期为 2026 的在投版本不可发布。
        # 直接为同一凭据再登记一条投放（不同链接），两条都应列入明细。
        result, status = register_placement(
            self.conn, credential_id=self.credential_id,
            channel="镜像站", url="https://example.com/m/1")
        assert status == 201
        second_pid = result["id"]
        values = {**VALUES_AUDIO, "embargo_until": "2027-01-01"}
        result, status = update_license(self.conn, 1, values)
        self.assertEqual(status, 409)
        pids = {a["placement_id"] for a in result["affected_placements"]}
        self.assertEqual(pids, {self.placement_id, second_pid})
        reasons = result["affected_placements"][0]["blocked"][0]["reasons"]
        self.assertTrue(any("尚未解禁" in r for r in reasons))
        self.assertIsNone(self.license_row()["embargo_until"])  # 已回滚

    # ---------------------------------------------------------- 删除

    def test_delete_that_keeps_publishable_is_committed(self):
        # 先撤下唯一在投投放，删除不再被阻拦
        withdraw_placement(self.conn, self.placement_id, "下线")
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"deleted": 1})
        self.assertIsNone(self.license_row())

    def test_delete_blocking_rolls_back_with_placement_details(self):
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 409)
        self.assertTrue(result["rolled_back"])
        self.assertEqual(result["action"], "delete")
        self.assertEqual(result["license_id"], 1)
        affected = result["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], self.placement_id)
        self.assertEqual(item["url"], "https://example.com/f/1")
        blocked = item["blocked"]
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (0, 600))
        self.assertTrue(any("缺少覆盖该区间的授权记录" in r
                            for r in blocked[0]["reasons"]))
        # 整笔删除已回滚：授权仍在
        self.assertIsNotNone(self.license_row())

    def test_delete_unknown_license_returns_404(self):
        result, status = delete_license(self.conn, 9999)
        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def test_withdrawn_placement_does_not_block_delete(self):
        withdraw_placement(self.conn, self.placement_id, "下线")
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result["deleted"], 1)

    def test_sealed_credential_is_not_modified_by_guard(self):
        before = self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (self.credential_id,)).fetchone()
        values = {**VALUES_AUDIO, "allow_media": [],
                  "deny_media": ["audio"]}
        update_license(self.conn, 1, values)  # 回滚
        after = self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (self.credential_id,)).fetchone()
        self.assertEqual(dict(before), dict(after))


if __name__ == "__main__":
    unittest.main()
