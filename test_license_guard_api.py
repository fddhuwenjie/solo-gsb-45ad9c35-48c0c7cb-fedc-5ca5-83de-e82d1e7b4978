"""HTTP 层测试：授权编辑 / 删除的提交前复核（在投投放保护）。

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_license_guard_api.py -v
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


class LicenseGuardApiTest(unittest.TestCase):
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
        self.base_placements = len(self.client.get("/api/placements")
                                   .get_json())
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

    def license_id(self):
        return self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"][0]["id"]

    def licenses(self):
        return self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"]

    def place(self, channel=CHANNEL, url=URL):
        resp = self.client.post(f"/api/credentials/{self.cid}/placements",
                                json={"channel": channel, "url": url})
        assert resp.status_code == 201
        return resp.get_json()["id"]

    def placement_count(self):
        return len(self.client.get("/api/placements").get_json())

    # ---------------------------------------------------------- 成功响应保持兼容

    def test_update_success_response_compatible(self):
        lid = self.license_id()
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充说明"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {
            "id": lid, "speaker_id": self.sp1,
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": "补充说明",
        })
        # 资料变化仍按原逻辑显示「待复核」
        cred = self.client.get(f"/api/credentials/{self.cid}").get_json()
        self.assertEqual(cred["status"], "review")
        self.assertEqual(cred["changes"], ["授权"])

    def test_delete_success_response_compatible(self):
        lid = self.license_id()
        resp = self.client.delete(f"/api/licenses/{lid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"deleted": lid})
        self.assertEqual(self.licenses(), [])

    def test_update_validation_still_applies(self):
        lid = self.license_id()
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 600,
            "allow_media": ["video"]})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 9999})
        self.assertEqual(resp.status_code, 400)
        # 校验失败不落任何变更
        self.assertEqual(self.licenses()[0]["end_seconds"], 600)

    def test_update_not_found(self):
        resp = self.client.put("/api/licenses/9999", json={
            "start_seconds": 0, "end_seconds": 600})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "授权记录不存在")

    def test_delete_not_found(self):
        resp = self.client.delete("/api/licenses/9999")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.get_json()["error"], "授权记录不存在")

    # ---------------------------------------------------------- 在投投放拦截

    def test_update_blocked_by_active_placement_rolls_back(self):
        pid = self.place()
        lid = self.license_id()
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 300,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["updated"])
        self.assertEqual(data["license_id"], lid)
        self.assertEqual(data["interview_id"], self.iid)
        self.assertIn("不可发布区间", data["error"])
        affected = data["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], pid)
        self.assertEqual(item["credential_id"], self.cid)
        self.assertEqual(item["channel"], CHANNEL)
        self.assertEqual(item["url"], URL)
        self.assertEqual(item["publication_date"], PUB_DATE)
        self.assertEqual(item["medium"], "audio")
        blocked = item["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["start_timecode"], "00:05:00")
        self.assertEqual(blocked[0]["end_timecode"], "00:10:00")
        self.assertTrue(any("缺" in r for r in blocked[0]["reasons"]))
        # 整笔回滚：授权记录未改动，凭据仍为「与发行时一致」
        self.assertEqual(self.licenses()[0]["end_seconds"], 600)
        cred = self.client.get(f"/api/credentials/{self.cid}").get_json()
        self.assertEqual(cred["status"], "valid")
        # 投放记录不改动
        placement = [p for p in self.client.get("/api/placements").get_json()
                     if p["id"] == pid][0]
        self.assertEqual(placement["status"], "active")

    def test_delete_blocked_by_active_placement_rolls_back(self):
        pid = self.place()
        lid = self.license_id()
        resp = self.client.delete(f"/api/licenses/{lid}")
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["deleted"])
        self.assertEqual(data["license_id"], lid)
        affected = data["affected_placements"]
        self.assertEqual(len(affected), 1)
        self.assertEqual(affected[0]["placement_id"], pid)
        self.assertEqual(affected[0]["url"], URL)
        blocked = affected[0]["blocked"]
        self.assertEqual(blocked[0]["start_timecode"], "00:00:00")
        self.assertTrue(any("缺少" in r for r in blocked[0]["reasons"]))
        self.assertEqual(len(self.licenses()), 1)  # 整笔回滚，授权仍在

    def test_change_commits_after_placement_withdrawn(self):
        pid = self.place()
        self.client.post(f"/api/placements/{pid}/withdraw",
                         json={"reason": "内容已下线"})
        lid = self.license_id()
        # 已撤下的投放不阻拦：修改照常提交
        resp = self.client.put(f"/api/licenses/{lid}", json={
            "start_seconds": 0, "end_seconds": 300,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.licenses()[0]["end_seconds"], 300)
        # 删除亦照常提交
        resp = self.client.delete(f"/api/licenses/{lid}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"deleted": lid})
        self.assertEqual(self.licenses(), [])

    def test_blocked_change_leaves_no_side_effects(self):
        self.place()
        lid = self.license_id()
        before = self.client.get(f"/api/credentials/{self.cid}").get_json()
        resp = self.client.delete(f"/api/licenses/{lid}")
        self.assertEqual(resp.status_code, 409)
        after = self.client.get(f"/api/credentials/{self.cid}").get_json()
        # 封存凭据不改动（发行时结论与当前状态均一致）
        self.assertEqual(before["sealed"], after["sealed"])
        self.assertEqual(after["status"], "valid")
        self.assertEqual(self.placement_count(), self.base_placements + 1)


if __name__ == "__main__":
    unittest.main()
