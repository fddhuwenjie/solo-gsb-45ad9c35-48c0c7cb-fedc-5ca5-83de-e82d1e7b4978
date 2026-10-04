"""HTTP 层测试：POST /api/interviews/<id>/remediate。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_remediate_api.py -v
"""
import os
import sqlite3
import tempfile
import unittest

_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _DB

from app import app  # noqa: E402

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


class RemediateApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        conn = sqlite3.connect(_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        for table in ("licenses", "segments", "speakers", "interviews"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        # 夹具：访谈 600 秒；甲发言 0-600，乙发言 300-600；
        # 甲已有授权 0-300 允许音频
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp1 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "甲"}).get_json()["id"]
        self.sp2 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "乙"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp1, "start_seconds": 0, "end_seconds": 600})
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp2, "start_seconds": 300, "end_seconds": 600})
        c.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 300,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})

    def url(self, iid=None):
        return f"/api/interviews/{iid or self.iid}/remediate"

    def post(self, body, iid=None):
        return self.client.post(self.url(iid), json=body)

    def license_count(self):
        return len(self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"])

    def full_batch(self):
        return [sup(self.sp1, 300, 600), sup(self.sp2, 300, 600)]

    def body(self, supplements, commit=False, medium="audio",
             pub_date=PUB_DATE):
        return {"publication_date": pub_date, "medium": medium,
                "commit": commit, "supplements": supplements}

    def test_interview_not_found(self):
        resp = self.post(self.body(self.full_batch()), iid=9999)
        self.assertEqual(resp.status_code, 404)

    def test_bad_medium_and_date_rejected(self):
        resp = self.post(self.body(self.full_batch(), medium="video"))
        self.assertEqual(resp.status_code, 400)
        resp = self.post(self.body(self.full_batch(), pub_date=""))
        self.assertEqual(resp.status_code, 400)
        resp = self.post(self.body(self.full_batch(), pub_date="2026/10/04"))
        self.assertEqual(resp.status_code, 400)

    def test_dry_run_returns_trial_without_writing(self):
        resp = self.post(self.body(self.full_batch()))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["committed"])
        self.assertEqual(data["before"]["summary"]["blocked_seconds"], 300)
        self.assertEqual(data["after"]["summary"]["blocked_seconds"], 0)
        self.assertEqual(self.license_count(), 1)

    def test_commit_success(self):
        resp = self.post(self.body(self.full_batch(), commit=True))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["committed"])
        self.assertEqual(len(data["inserted_license_ids"]), 2)
        self.assertEqual(self.license_count(), 3)
        after = data["after"]["intervals"]
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]["status"], "ok")
        self.assertEqual(after[0]["start_timecode"], "00:00:00")
        self.assertEqual(after[0]["end_timecode"], "00:10:00")

    def test_commit_blocked_returns_409_with_reasons(self):
        resp = self.post(self.body([sup(self.sp1, 300, 600)], commit=True))
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["committed"])
        self.assertIn("error", data)
        self.assertEqual(len(data["blocked"]), 1)
        reasons = data["blocked"][0]["reasons"]
        self.assertTrue(any("乙" in r for r in reasons))
        self.assertEqual(self.license_count(), 1)  # 不落任何记录

    def test_invalid_batch_returns_400(self):
        # 同一说话人补件重叠
        resp = self.post(self.body(
            [sup(self.sp1, 300, 500), sup(self.sp1, 400, 600)], commit=True))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("重叠", resp.get_json()["error"])
        # 越界
        resp = self.post(self.body([sup(self.sp2, 300, 700)], commit=True))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("越界", resp.get_json()["error"])
        # 空批次
        resp = self.post(self.body([], commit=True))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.license_count(), 1)


if __name__ == "__main__":
    unittest.main()
