"""HTTP 层测试：发行凭据接口。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_credential_api.py -v
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


class CredentialApiTest(unittest.TestCase):
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
        # 凭据不可删除（触发器保护），用数量增量断言
        self.base_count = len(self.client.get("/api/credentials").get_json())
        # 夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp1 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "甲"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp1, "start_seconds": 0, "end_seconds": 600})
        c.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})

    def body(self, medium="audio", pub_date=PUB_DATE):
        return {"publication_date": pub_date, "medium": medium}

    def create(self, iid=None, **kwargs):
        return self.client.post(
            f"/api/interviews/{iid or self.iid}/credentials",
            json=self.body(**kwargs))

    def credential_count(self):
        return len(self.client.get("/api/credentials").get_json())

    def license_id(self):
        return self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"][0]["id"]

    # ---------------------------------------------------------- 创建

    def test_interview_not_found(self):
        resp = self.create(iid=9999)
        self.assertEqual(resp.status_code, 404)

    def test_bad_medium_and_date_rejected(self):
        self.assertEqual(self.create(medium="video").status_code, 400)
        self.assertEqual(self.create(pub_date="").status_code, 400)
        self.assertEqual(self.create(pub_date="2026/10/04").status_code, 400)
        self.assertEqual(self.credential_count(), self.base_count)

    def test_create_success_seals_credential(self):
        resp = self.create()
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data["created"])
        self.assertEqual(data["interview_title"], "访谈")
        self.assertEqual(data["result"]["summary"]["blocked_seconds"], 0)
        self.assertEqual(self.credential_count(), self.base_count + 1)
        # 列表可见，状态为与发行时一致
        items = self.client.get("/api/credentials").get_json()
        item = [i for i in items if i["id"] == data["id"]][0]
        self.assertEqual(item["status"], "valid")
        self.assertEqual(item["status_label"], "与发行时一致")
        # 详情包含封存的完整输入与发行时结论
        detail = self.client.get(f"/api/credentials/{data['id']}").get_json()
        self.assertEqual(detail["status"], "valid")
        self.assertEqual(detail["changes"], [])
        self.assertEqual(detail["sealed"]["inputs"]["duration_seconds"], 600)
        self.assertEqual(len(detail["sealed"]["inputs"]["speakers"]), 1)
        self.assertEqual(len(detail["sealed"]["inputs"]["segments"]), 1)
        self.assertEqual(len(detail["sealed"]["inputs"]["licenses"]), 1)
        self.assertEqual(
            detail["sealed"]["result"]["summary"]["publishable_seconds"], 600)
        self.assertEqual(detail["current"]["blocked"], [])

    def test_create_blocked_returns_409_and_leaves_no_record(self):
        self.client.delete(f"/api/licenses/{self.license_id()}")
        resp = self.create()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["created"])
        self.assertIn("error", data)
        self.assertEqual(len(data["blocked"]), 1)
        self.assertEqual(data["blocked"][0]["start_timecode"], "00:00:00")
        self.assertEqual(self.credential_count(), self.base_count)  # 不留记录

    # ---------------------------------------------------------- 查阅与复核

    def test_review_after_change_even_if_still_publishable(self):
        cid = self.create().get_json()["id"]
        # 追加一条同样允许音频的授权 -> 仍可发布，但资料有变化
        self.client.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        detail = self.client.get(f"/api/credentials/{cid}").get_json()
        self.assertEqual(detail["status"], "review")
        self.assertEqual(detail["status_label"], "待复核")
        self.assertEqual(detail["changes"], ["授权"])
        self.assertEqual(detail["current"]["blocked"], [])  # 即便仍可发布
        # 发行时结论保留
        self.assertEqual(
            detail["sealed"]["result"]["summary"]["blocked_seconds"], 0)

    def test_review_lists_current_blocked_intervals(self):
        cid = self.create().get_json()["id"]
        self.client.put(f"/api/licenses/{self.license_id()}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"],
            "embargo_until": None, "note": ""})
        detail = self.client.get(f"/api/credentials/{cid}").get_json()
        self.assertEqual(detail["status"], "review")
        blocked = detail["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["start_timecode"], "00:00:00")
        self.assertEqual(blocked[0]["end_timecode"], "00:10:00")
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))

    def test_missing_source_still_viewable(self):
        cid = self.create().get_json()["id"]
        self.client.delete(f"/api/interviews/{self.iid}")
        resp = self.client.get(f"/api/credentials/{cid}")
        self.assertEqual(resp.status_code, 200)  # 历史凭据仍可查看
        detail = resp.get_json()
        self.assertEqual(detail["status"], "missing")
        self.assertEqual(detail["status_label"], "来源缺失")
        self.assertIsNone(detail["current"])
        self.assertEqual(detail["sealed"]["inputs"]["duration_seconds"], 600)
        item = [i for i in self.client.get("/api/credentials").get_json()
                if i["id"] == cid][0]
        self.assertEqual(item["status"], "missing")

    def test_credential_not_found(self):
        resp = self.client.get("/api/credentials/9999")
        self.assertEqual(resp.status_code, 404)

    def test_credential_immutable_via_api(self):
        cid = self.create().get_json()["id"]
        resp = self.client.put(f"/api/credentials/{cid}", json={})
        self.assertEqual(resp.status_code, 405)
        resp = self.client.delete(f"/api/credentials/{cid}")
        self.assertEqual(resp.status_code, 405)


if __name__ == "__main__":
    unittest.main()
