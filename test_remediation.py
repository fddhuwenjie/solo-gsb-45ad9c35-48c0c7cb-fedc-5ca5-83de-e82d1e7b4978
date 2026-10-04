"""补件并复核事务编排（remediation.run_remediation）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
试算不落地、全部可发布才一次性写入、失败不落任何记录、
重叠 / 冲突 / 越界的补件令整批失败。
"""
import os
import sqlite3
import tempfile
import unittest

from remediation import run_remediation

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
"""

PUB_DATE = "2026-10-04"


def sup(sp, start, end, allow=("audio",), deny=(), embargo=None, note=""):
    return {
        "speaker_id": sp,
        "start_seconds": start,
        "end_seconds": end,
        "allow_media": list(allow),
        "deny_media": list(deny),
        "embargo_until": embargo,
        "note": note,
    }


class RemediationTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，乙发言 300-600；
    甲已有授权 0-300 允许音频 -> 300-600 两人均缺授权。"""

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
                  " note) VALUES (1, 0, 300, '[\"audio\"]', '[]', NULL, '')")
        c.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def run_rem(self, supplements, commit=False, medium="audio",
                pub_date=PUB_DATE):
        return run_remediation(
            self.conn, interview_id=1, duration=600,
            publication_date=pub_date, medium=medium,
            raw_supplements=supplements, commit=commit)

    def license_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM licenses").fetchone()["n"]

    def full_batch(self):
        return [sup(1, 300, 600), sup(2, 300, 600)]

    # ---------------------------------------------------------- 试算与写入

    def test_dry_run_reports_before_after_and_writes_nothing(self):
        result, status = self.run_rem(self.full_batch(), commit=False)
        self.assertEqual(status, 200)
        self.assertFalse(result["committed"])
        self.assertFalse(result["commit_requested"])
        # 试算前：300-600 阻断；合并补件后：全部可发布
        self.assertEqual(result["before"]["summary"]["blocked_seconds"], 300)
        self.assertEqual(result["after"]["summary"]["blocked_seconds"], 0)
        self.assertEqual(result["blocked"], [])
        self.assertEqual(self.license_count(), 1)  # 不落任何记录

    def test_commit_success_writes_all_and_lists_intervals(self):
        result, status = self.run_rem(self.full_batch(), commit=True)
        self.assertEqual(status, 200)
        self.assertTrue(result["committed"])
        self.assertEqual(len(result["inserted_license_ids"]), 2)
        self.assertEqual(self.license_count(), 3)
        # 成功结果列出变更后的区间：0-600 合并为一个可发布区间
        after = result["after"]["intervals"]
        self.assertEqual(len(after), 1)
        self.assertEqual((after[0]["start_seconds"], after[0]["end_seconds"]),
                         (0, 600))
        self.assertEqual(after[0]["status"], "ok")
        self.assertTrue(after[0]["publishable"])
        # 写入的授权可在库中查到
        rows = self.conn.execute(
            "SELECT * FROM licenses WHERE speaker_id = 2").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["start_seconds"], rows[0]["end_seconds"]),
                         (300, 600))

    def test_commit_with_remaining_blocked_returns_409_and_writes_nothing(self):
        # 只补甲，乙在 300-600 仍缺授权
        result, status = self.run_rem([sup(1, 300, 600)], commit=True)
        self.assertEqual(status, 409)
        self.assertFalse(result["committed"])
        self.assertIn("error", result)
        self.assertEqual(self.license_count(), 1)  # 不落任何记录
        # 返回每个阻断区间及原因
        self.assertEqual(len(result["blocked"]), 1)
        blocked = result["blocked"][0]
        self.assertEqual((blocked["start_seconds"], blocked["end_seconds"]),
                         (300, 600))
        self.assertTrue(any("乙" in r and "缺项" in r
                            for r in blocked["reasons"]))
        self.assertIn("start_timecode", blocked)

    def test_dry_run_can_show_blocked_without_failing(self):
        # 试算模式（commit=False）即使有阻断也返回 200 供页面查看
        result, status = self.run_rem([sup(1, 300, 600)], commit=False)
        self.assertEqual(status, 200)
        self.assertFalse(result["committed"])
        self.assertEqual(len(result["blocked"]), 1)
        self.assertEqual(self.license_count(), 1)

    # ---------------------------------------------------------- 既有规则在重算中生效

    def test_deny_priority_still_blocks_after_merge(self):
        # 已有拒绝音频的授权覆盖 300-600，补件允许音频 -> 冲突按拒绝处理
        self.conn.execute(
            "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
            " allow_media, deny_media, embargo_until, note)"
            " VALUES (1, 300, 600, '[]', '[\"audio\"]', NULL, '')")
        self.conn.commit()
        result, status = self.run_rem(self.full_batch(), commit=True)
        self.assertEqual(status, 409)
        self.assertEqual(self.license_count(), 2)  # 只有夹具里的两条
        reasons = result["blocked"][0]["reasons"]
        self.assertTrue(any("冲突" in r and "拒绝优先" in r for r in reasons))

    def test_embargo_still_blocks_until_lifted(self):
        batch = [sup(1, 300, 600, embargo="2030-01-01"), sup(2, 300, 600)]
        result, status = self.run_rem(batch, commit=True)
        self.assertEqual(status, 409)
        self.assertTrue(any("未解禁" in r
                            for r in result["blocked"][0]["reasons"]))
        self.assertEqual(self.license_count(), 1)
        # 解禁日当天即可发布
        result2, status2 = self.run_rem(batch, commit=True,
                                        pub_date="2030-01-01")
        self.assertEqual(status2, 200)
        self.assertTrue(result2["committed"])
        self.assertEqual(self.license_count(), 3)

    def test_all_speakers_must_be_covered(self):
        # 只补乙 -> 甲在 300-600 仍缺授权
        result, status = self.run_rem([sup(2, 300, 600)], commit=True)
        self.assertEqual(status, 409)
        self.assertTrue(any("甲" in r and "缺项" in r
                            for r in result["blocked"][0]["reasons"]))

    def test_medium_switch_uses_same_rules(self):
        # 补音频授权但按文字媒介复核 -> 仍缺项
        result, status = self.run_rem(self.full_batch(), commit=True,
                                      medium="text")
        self.assertEqual(status, 409)
        self.assertTrue(any("文字" in r
                            for r in result["blocked"][0]["reasons"]))

    # ---------------------------------------------------------- 整批失败

    def test_overlap_within_batch_fails_all(self):
        result, status = self.run_rem(
            [sup(1, 300, 500), sup(1, 400, 600)], commit=True)
        self.assertEqual(status, 400)
        self.assertIn("重叠", result["error"])
        self.assertEqual(self.license_count(), 1)

    def test_touching_intervals_in_batch_are_ok(self):
        result, status = self.run_rem(
            [sup(1, 300, 450), sup(1, 450, 600), sup(2, 300, 600)],
            commit=True)
        self.assertEqual(status, 200)
        self.assertTrue(result["committed"])
        self.assertEqual(self.license_count(), 4)

    def test_self_conflicting_supplement_fails_all(self):
        result, status = self.run_rem(
            [sup(1, 300, 600, deny=["audio"]), sup(2, 300, 600)],
            commit=True)
        self.assertEqual(status, 400)
        self.assertIn("冲突", result["error"])
        self.assertEqual(self.license_count(), 1)

    def test_out_of_bounds_supplement_fails_all(self):
        # 第一条合法、第二条越界 -> 整批失败，合法的那条也不落库
        result, status = self.run_rem(
            [sup(1, 300, 600), sup(2, 300, 700)], commit=True)
        self.assertEqual(status, 400)
        self.assertIn("越界", result["error"])
        self.assertEqual(self.license_count(), 1)

    def test_unknown_speaker_fails_all(self):
        result, status = self.run_rem([sup(99, 300, 600)], commit=True)
        self.assertEqual(status, 400)
        self.assertIn("说话人", result["error"])
        self.assertEqual(self.license_count(), 1)

    def test_empty_batch_fails(self):
        result, status = self.run_rem([], commit=True)
        self.assertEqual(status, 400)
        self.assertIn("空", result["error"])
        result, status = self.run_rem(None, commit=False)
        self.assertEqual(status, 400)

    def test_invalid_batch_rejected_even_in_dry_run(self):
        result, status = self.run_rem([sup(1, 300, 700)], commit=False)
        self.assertEqual(status, 400)
        self.assertEqual(self.license_count(), 1)


if __name__ == "__main__":
    unittest.main()
