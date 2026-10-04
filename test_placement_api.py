"""HTTP 层测试：渠道投放台账接口。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_placement_api.py -v
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
CHANNEL = "官网专题页"
URL = "https://example.com/features/oral-history-1"


class PlacementApiTest(unittest.TestCase):
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
        # 凭据与投放记录不可删除（触发器保护），用数量增量断言
        self.base_count = len(self.client.get("/api/placements").get_json())
        # 夹具：访谈 600 秒；甲发言 0-600，授权 0-600 允许音频 -> 可封存凭据
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
        self.cid = c.post(f"/api/interviews/{self.iid}/credentials", json={
            "publication_date": PUB_DATE, "medium": "audio",
        }).get_json()["id"]

    def body(self, channel=CHANNEL, url=URL):
        return {"channel": channel, "url": url}

    def register(self, cid=None, **kwargs):
        return self.client.post(
            f"/api/credentials/{cid or self.cid}/placements",
            json=self.body(**kwargs))

    def placement_count(self):
        return len(self.client.get("/api/placements").get_json())

    def license_id(self):
        return self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"][0]["id"]

    # ---------------------------------------------------------- 登记

    def test_empty_channel_or_url_rejected(self):
        self.assertEqual(self.register(channel="").status_code, 400)
        self.assertEqual(self.register(channel="  ").status_code, 400)
        self.assertEqual(self.register(url="").status_code, 400)
        self.assertEqual(self.placement_count(), self.base_count)

    def test_credential_not_found(self):
        resp = self.register(cid=9999)
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(self.placement_count(), self.base_count)

    def test_register_success_saves_active_record(self):
        resp = self.register()
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertTrue(data["created"])
        self.assertEqual(data["status"], "active")
        self.assertEqual(data["status_label"], "投放中")
        self.assertEqual(data["channel"], CHANNEL)
        self.assertEqual(data["url"], URL)
        self.assertTrue(data["created_at"])  # 登记时间
        self.assertEqual(self.placement_count(), self.base_count + 1)
        # 列表可见：投放中 + 凭据风险正常
        item = [i for i in self.client.get("/api/placements").get_json()
                if i["id"] == data["id"]][0]
        self.assertEqual(item["status_label"], "投放中")
        self.assertEqual(item["risk"], "ok")
        self.assertEqual(item["risk_label"], "正常")
        self.assertEqual(item["interview_title"], "访谈")
        # 详情含封存结论
        detail = self.client.get(f"/api/placements/{data['id']}").get_json()
        self.assertEqual(detail["credential"]["id"], self.cid)
        self.assertEqual(
            detail["credential"]["sealed_result"]["summary"]
            ["publishable_seconds"], 600)
        self.assertEqual(detail["current"]["blocked"], [])

    def test_register_changed_materials_leaves_no_record(self):
        # 追加一条同样允许音频的授权：仍可发布，但资料与封存时不一致
        self.client.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        resp = self.register()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["created"])
        self.assertEqual(data["reason"], "changed")
        self.assertEqual(data["changes"], ["授权"])
        self.assertIn("需处理", data["error"])
        self.assertEqual(self.placement_count(), self.base_count)  # 不留记录

    def test_register_missing_source_leaves_no_record(self):
        self.client.delete(f"/api/interviews/{self.iid}")
        resp = self.register()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["created"])
        self.assertEqual(data["reason"], "missing")
        self.assertIn("来源缺失", data["error"])
        self.assertEqual(self.placement_count(), self.base_count)  # 不留记录

    # ---------------------------------------------------------- 查阅与风险

    def test_risk_transitions_in_list_and_detail(self):
        pid = self.register().get_json()["id"]
        # 资料后来变化（即便仍可发布）-> 需处理
        self.client.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充"})
        item = [i for i in self.client.get("/api/placements").get_json()
                if i["id"] == pid][0]
        self.assertEqual(item["risk"], "review")
        self.assertEqual(item["risk_label"], "需处理")
        self.assertEqual(item["changes"], ["授权"])
        detail = self.client.get(f"/api/placements/{pid}").get_json()
        self.assertEqual(detail["risk"], "review")
        self.assertEqual(detail["current"]["blocked"], [])  # 即便仍可发布
        # 来源删除 -> 来源缺失，历史链接、时间与封存结论仍保留
        self.client.delete(f"/api/interviews/{self.iid}")
        detail = self.client.get(f"/api/placements/{pid}").get_json()
        self.assertEqual(detail["risk"], "missing")
        self.assertEqual(detail["risk_label"], "来源缺失")
        self.assertIsNone(detail["current"])
        self.assertEqual(detail["url"], URL)
        self.assertTrue(detail["created_at"])
        self.assertEqual(
            detail["credential"]["sealed_result"]["summary"]
            ["publishable_seconds"], 600)
        item = [i for i in self.client.get("/api/placements").get_json()
                if i["id"] == pid][0]
        self.assertEqual(item["risk"], "missing")

    def test_detail_lists_current_blocked_intervals(self):
        pid = self.register().get_json()["id"]
        self.client.put(f"/api/licenses/{self.license_id()}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": [], "deny_media": ["audio"],
            "embargo_until": None, "note": ""})
        detail = self.client.get(f"/api/placements/{pid}").get_json()
        self.assertEqual(detail["risk"], "review")
        blocked = detail["current"]["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["start_timecode"], "00:00:00")
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))

    def test_placement_not_found(self):
        resp = self.client.get("/api/placements/9999")
        self.assertEqual(resp.status_code, 404)

    # ---------------------------------------------------------- 撤下

    def test_withdraw_marks_record_and_keeps_timestamp(self):
        pid = self.register().get_json()["id"]
        resp = self.client.post(f"/api/placements/{pid}/withdraw",
                                json={"reason": "内容已下线"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["withdrawn"])
        self.assertEqual(data["status_label"], "已撤下")
        self.assertTrue(data["withdrawn_at"])  # 撤下时间
        self.assertEqual(data["withdraw_reason"], "内容已下线")
        # 查阅：已撤下，风险提示仍可查
        detail = self.client.get(f"/api/placements/{pid}").get_json()
        self.assertEqual(detail["status"], "withdrawn")
        self.assertEqual(detail["withdrawn_at"], data["withdrawn_at"])
        self.assertEqual(detail["withdraw_reason"], "内容已下线")
        self.assertEqual(detail["risk"], "ok")
        # 凭据不改动
        cred = self.client.get(f"/api/credentials/{self.cid}").get_json()
        self.assertEqual(cred["status"], "valid")

    def test_withdraw_twice_keeps_original_timestamp(self):
        pid = self.register().get_json()["id"]
        first = self.client.post(f"/api/placements/{pid}/withdraw",
                                 json={"reason": "第一次"}).get_json()
        resp = self.client.post(f"/api/placements/{pid}/withdraw",
                                json={"reason": "第二次"})
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["withdrawn_at"], first["withdrawn_at"])
        self.assertEqual(data["withdraw_reason"], "第一次")  # 保留原记录

    def test_withdraw_not_found(self):
        resp = self.client.post("/api/placements/9999/withdraw",
                                json={"reason": "原因"})
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
