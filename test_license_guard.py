"""授权变更提交前复核（license_guard 模块）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
修改 / 删除授权时在同一 SQLite 写事务中读取授权归属并试用变更，
按该访谈每条仍在投记录所关联凭据的发布日期与媒介重校验整段 ——
任一版本出现不可发布区间即回滚整笔变更（返回受影响投放 ID、链接、
阻断区间及原因）；已撤下的投放不阻拦，其余变更照常提交，
封存凭据不改动，资料变化仍按原逻辑显示「待复核」。
"""
import os
import sqlite3
import tempfile
import unittest

from credentials import (
    STATUS_REVIEW,
    STATUS_VALID,
    create_credential,
    credential_review,
)
from license_guard import delete_license, update_license
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
CHANNEL = "官网专题页"
URL = "https://example.com/features/oral-history-1"


def values(start=0, end=600, allow=("audio", "text"), deny=(),
           embargo=None, note=""):
    return {
        "start_seconds": start,
        "end_seconds": end,
        "allow_media": list(allow),
        "deny_media": list(deny),
        "embargo_until": embargo,
        "note": note,
    }


class LicenseGuardTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频与文字
    -> 音频 / 文字版本均可发布，音频版凭据已封存。"""

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
                  " note) VALUES (1, 0, 600, '[\"audio\", \"text\"]', '[]',"
                  " NULL, '')")
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
        result, status = register_placement(
            self.conn, credential_id=cid or self.credential_id,
            channel=channel, url=url)
        assert status == 201
        return result["id"]

    def license_row(self):
        return self.conn.execute(
            "SELECT * FROM licenses WHERE id = 1").fetchone()

    def credential_row(self):
        return self.conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (self.credential_id,)).fetchone()

    def seal_text_version(self):
        """封存文字版凭据并登记投放，返回 (credential_id, placement_id)。"""
        result, status = create_credential(
            self.conn, interview_id=1,
            publication_date=PUB_DATE, medium="text")
        assert status == 201
        pid = self.register(cid=result["id"], channel="文字专栏",
                            url="https://example.com/text/oral-history-1")
        return result["id"], pid

    # ---------------------------------------------------------- 无在投投放

    def test_update_without_active_placement_commits(self):
        # 无投放记录：即使变更后出现不可发布区间也照常提交（原逻辑）
        result, status = update_license(
            self.conn, 1, values(allow=(), deny=("audio", "text")))
        self.assertEqual(status, 200)
        self.assertEqual(result["id"], 1)
        self.assertEqual(result["speaker_id"], 1)
        self.assertEqual(result["deny_media"], ["audio", "text"])
        self.assertEqual(self.license_row()["deny_media"],
                         '["audio", "text"]')

    def test_delete_without_active_placement_commits(self):
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"deleted": 1})
        self.assertIsNone(self.license_row())

    # ---------------------------------------------------------- 照常提交

    def test_update_keep_publishable_commits(self):
        self.register()
        result, status = update_license(self.conn, 1, values(note="补充说明"))
        self.assertEqual(status, 200)
        self.assertEqual(result["note"], "补充说明")
        self.assertEqual(self.license_row()["note"], "补充说明")
        # 资料变化仍按原逻辑显示「待复核」，封存凭据不改动
        review = credential_review(self.conn, self.credential_id)
        self.assertEqual(review["status"], STATUS_REVIEW)
        self.assertEqual(review["changes"], ["授权"])

    def test_update_success_response_shape(self):
        result, status = update_license(
            self.conn, 1, values(start=60, end=540, allow=("audio",),
                                 embargo="2026-01-01", note="n"))
        self.assertEqual(status, 200)
        self.assertEqual(result, {
            "id": 1, "speaker_id": 1,
            "start_seconds": 60, "end_seconds": 540,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": "2026-01-01", "note": "n",
        })

    # ---------------------------------------------------------- 拦截回滚

    def test_update_blocking_rolls_back_with_details(self):
        pid = self.register()
        # 授权收窄为 0-300：音频版 300-600 缺授权 -> 不可发布
        result, status = update_license(self.conn, 1, values(end=300))
        self.assertEqual(status, 409)
        self.assertFalse(result["updated"])
        self.assertEqual(result["license_id"], 1)
        self.assertEqual(result["interview_id"], 1)
        self.assertIn("不可发布区间", result["error"])
        affected = result["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], pid)
        self.assertEqual(item["credential_id"], self.credential_id)
        self.assertEqual(item["channel"], CHANNEL)
        self.assertEqual(item["url"], URL)
        self.assertEqual(item["publication_date"], PUB_DATE)
        self.assertEqual(item["medium"], "audio")
        blocked = item["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (300, 600))
        self.assertEqual(blocked[0]["start_timecode"], "00:05:00")
        self.assertTrue(any("缺" in r for r in blocked[0]["reasons"]))
        # 整笔回滚：授权记录未改动
        row = self.license_row()
        self.assertEqual(row["end_seconds"], 600)
        self.assertEqual(row["note"], "")

    def test_update_embargo_blocked_rolls_back(self):
        self.register()
        # 解禁日晚于凭据发布日期 -> 未解禁，不可发布
        result, status = update_license(
            self.conn, 1, values(embargo="2026-12-31"))
        self.assertEqual(status, 409)
        blocked = result["affected_placements"][0]["blocked"]
        self.assertTrue(any("解禁" in r for r in blocked[0]["reasons"]))
        self.assertIsNone(self.license_row()["embargo_until"])  # 已回滚

    def test_delete_blocking_rolls_back_with_details(self):
        pid = self.register()
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 409)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["license_id"], 1)
        affected = result["affected_placements"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["placement_id"], pid)
        self.assertEqual(affected[0]["url"], URL)
        blocked = affected[0]["blocked"]
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (0, 600))
        self.assertTrue(any("缺少" in r for r in blocked[0]["reasons"]))
        self.assertIsNotNone(self.license_row())  # 整笔回滚，授权仍在

    def test_rollback_leaves_credential_and_placement_untouched(self):
        pid = self.register()
        before = dict(self.credential_row())
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 409)
        self.assertEqual(dict(self.credential_row()), before)  # 凭据不改动
        placement = self.conn.execute(
            "SELECT * FROM channel_placements WHERE id = ?", (pid,)).fetchone()
        self.assertEqual(placement["status"], "active")  # 投放记录不改动
        # 凭据状态仍为「与发行时一致」（变更未落库）
        review = credential_review(self.conn, self.credential_id)
        self.assertEqual(review["status"], STATUS_VALID)

    # ---------------------------------------------------------- 已撤下不阻拦

    def test_withdrawn_placement_does_not_block_update(self):
        pid = self.register()
        withdraw_placement(self.conn, pid, "内容已下线")
        result, status = update_license(
            self.conn, 1, values(allow=(), deny=("audio", "text")))
        self.assertEqual(status, 200)
        self.assertEqual(self.license_row()["deny_media"], '["audio", "text"]')

    def test_withdrawn_placement_does_not_block_delete(self):
        pid = self.register()
        withdraw_placement(self.conn, pid, "内容已下线")
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 200)
        self.assertEqual(result, {"deleted": 1})
        self.assertIsNone(self.license_row())

    # ---------------------------------------------------------- 多版本复核

    def test_any_single_blocked_version_rolls_back_all(self):
        """音频、文字两个版本各有一条在投投放：变更只让文字版不可发布，
        整笔仍回滚，且受影响列表只含文字版投放。"""
        audio_pid = self.register()
        _, text_pid = self.seal_text_version()
        result, status = update_license(
            self.conn, 1, values(allow=("audio",), deny=("text",)))
        self.assertEqual(status, 409)
        affected = result["affected_placements"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["placement_id"], text_pid)
        self.assertEqual(affected[0]["medium"], "text")
        self.assertNotEqual(affected[0]["placement_id"], audio_pid)
        blocked = affected[0]["blocked"]
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))
        self.assertEqual(self.license_row()["deny_media"], "[]")  # 已回滚

    def test_multiple_placements_same_version_all_listed(self):
        pid1 = self.register()
        pid2 = self.register(channel="播客平台",
                             url="https://example.com/podcast/ep-1")
        result, status = delete_license(self.conn, 1)
        self.assertEqual(status, 409)
        ids = [p["placement_id"] for p in result["affected_placements"]]
        self.assertEqual(ids, [pid1, pid2])
        urls = [p["url"] for p in result["affected_placements"]]
        self.assertIn("https://example.com/podcast/ep-1", urls)

    def test_all_versions_publishable_commits(self):
        self.register()
        self.seal_text_version()
        result, status = update_license(self.conn, 1, values(note="更新备注"))
        self.assertEqual(status, 200)
        self.assertEqual(self.license_row()["note"], "更新备注")

    # ---------------------------------------------------------- 记录不存在

    def test_update_unknown_license_returns_404(self):
        result, status = update_license(self.conn, 9999, values())
        self.assertEqual(status, 404)
        self.assertIn("error", result)

    def test_delete_unknown_license_returns_404(self):
        result, status = delete_license(self.conn, 9999)
        self.assertEqual(status, 404)
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
