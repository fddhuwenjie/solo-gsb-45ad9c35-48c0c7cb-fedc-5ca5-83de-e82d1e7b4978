"""HTTP 层测试：授权修改 / 删除的在投渠道提交前复核。

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
URL = "https://example.com/features/oral-history-1"


def put_body(**overrides):
    body = {
        "start_seconds": 0,
        "end_seconds": 600,
        "allow_media": ["audio"],
        "deny_media": [],
        "embargo_until": None,
        "note": "",
    }
    body.update(overrides)
    return body


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
        self.placement_base = len(self.client.get("/api/placements").get_json())
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp = c.post(f"/api/interviews/{self.iid}/speakers",
                         json={"name": "甲"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp, "start_seconds": 0, "end_seconds": 600})
        self.lic = c.post(f"/api/speakers/{self.sp}/licenses",
                          json=put_body()).get_json()["id"]
        self.cid = c.post(f"/api/interviews/{self.iid}/credentials", json={
            "publication_date": PUB_DATE, "medium": "audio",
        }).get_json()["id"]
        resp = c.post(f"/api/credentials/{self.cid}/placements", json={
            "channel": "官网专题页", "url": URL})
        self.pid = resp.get_json()["id"]

    def licenses(self):
        return self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"]

    def withdraw(self, pid=None, reason="内容已下线"):
        return self.client.post(
            f"/api/placements/{pid or self.pid}/withdraw",
            json={"reason": reason})

    # ---------------------------------------------------------- 成功路径兼容

    def test_update_success_response_stays_compatible(self):
        resp = self.client.put(f"/api/licenses/{self.lic}",
                               json=put_body(note="新备注"))
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["id"], self.lic)
        self.assertEqual(data["speaker_id"], self.sp)
        self.assertEqual(data["start_seconds"], 0)
        self.assertEqual(data["end_seconds"], 600)
        self.assertEqual(data["allow_media"], ["audio"])
        self.assertEqual(data["deny_media"], [])
        self.assertIsNone(data["embargo_until"])
        self.assertEqual(data["note"], "新备注")

    def test_delete_success_response_stays_compatible(self):
        self.withdraw()
        resp = self.client.delete(f"/api/licenses/{self.lic}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"deleted": self.lic})
        self.assertEqual(self.licenses(), [])

    def test_invalid_payload_still_400(self):
        resp = self.client.put(f"/api/licenses/{self.lic}",
                               json=put_body(end_seconds=0))
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(len(self.licenses()), 1)  # 未改动

    def test_unknown_license_404(self):
        self.assertEqual(
            self.client.put("/api/licenses/9999", json=put_body()).status_code,
            404)
        self.assertEqual(
            self.client.delete("/api/licenses/9999").status_code, 404)

    # ---------------------------------------------------------- 修改被拦截

    def test_update_blocked_returns_409_with_placement_details(self):
        resp = self.client.put(
            f"/api/licenses/{self.lic}",
            json=put_body(allow_media=[], deny_media=["audio"]))
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertFalse(data["allowed"])
        self.assertTrue(data["rolled_back"])
        self.assertEqual(data["action"], "update")
        self.assertEqual(data["license_id"], self.lic)
        affected = data["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], self.pid)
        self.assertEqual(item["credential_id"], self.cid)
        self.assertEqual(item["channel"], "官网专题页")
        self.assertEqual(item["url"], URL)
        self.assertEqual(item["medium"], "audio")
        self.assertEqual(item["medium_label"], "音频")
        self.assertEqual(item["publication_date"], PUB_DATE)
        blocked = item["blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["start_timecode"], "00:00:00")
        self.assertEqual(blocked[0]["end_timecode"], "00:10:00")
        self.assertTrue(any("拒绝" in r for r in blocked[0]["reasons"]))
        # 整笔变更已回滚：授权原样，投放仍是投放中
        lic = self.licenses()[0]
        self.assertEqual(lic["allow_media"], ["audio"])
        self.assertEqual(lic["deny_media"], [])
        placement = self.client.get(
            f"/api/placements/{self.pid}").get_json()
        self.assertEqual(placement["status"], "active")

    def test_update_shrink_range_blocked_on_uncovered_interval(self):
        resp = self.client.put(f"/api/licenses/{self.lic}",
                               json=put_body(end_seconds=300))
        self.assertEqual(resp.status_code, 409)
        blocked = resp.get_json()["affected_placements"][0]["blocked"]
        self.assertEqual([(b["start_seconds"], b["end_seconds"])
                          for b in blocked], [(300, 600)])
        self.assertTrue(any("缺项" in r for r in blocked[0]["reasons"]))
        self.assertEqual(self.licenses()[0]["end_seconds"], 600)  # 已回滚

    def test_update_embargo_blocked_lists_interval_and_reason(self):
        resp = self.client.put(f"/api/licenses/{self.lic}",
                               json=put_body(embargo_until="2027-01-01"))
        self.assertEqual(resp.status_code, 409)
        item = resp.get_json()["affected_placements"][0]
        self.assertEqual(item["publication_date"], PUB_DATE)
        self.assertTrue(
            any("尚未解禁" in r for r in item["blocked"][0]["reasons"]))

    # ---------------------------------------------------------- 删除被拦截

    def test_delete_blocked_returns_409_and_keeps_license(self):
        resp = self.client.delete(f"/api/licenses/{self.lic}")
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertEqual(data["action"], "delete")
        self.assertTrue(data["rolled_back"])
        affected = data["affected_placements"]
        self.assertEqual(len(affected), 1)
        item = affected[0]
        self.assertEqual(item["placement_id"], self.pid)
        self.assertEqual(item["url"], URL)
        blocked = item["blocked"]
        self.assertEqual((blocked[0]["start_seconds"],
                          blocked[0]["end_seconds"]), (0, 600))
        self.assertTrue(any("缺少覆盖该区间的授权记录" in r
                            for r in blocked[0]["reasons"]))
        self.assertEqual(len(self.licenses()), 1)  # 授权仍保留

    # ---------------------------------------------------------- 撤下不阻拦

    def test_withdrawn_placement_does_not_block_update(self):
        self.assertEqual(self.withdraw().status_code, 200)
        resp = self.client.put(
            f"/api/licenses/{self.lic}",
            json=put_body(allow_media=[], deny_media=["audio"]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.licenses()[0]["deny_media"], ["audio"])
        # 投放数量未增加
        self.assertEqual(
            len(self.client.get("/api/placements").get_json()),
            self.placement_base + 1)

    def test_withdrawn_placement_does_not_block_delete(self):
        self.withdraw()
        resp = self.client.delete(f"/api/licenses/{self.lic}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["deleted"], self.lic)
        self.assertEqual(self.licenses(), [])

    # ---------------------------------------------------------- 多版本 / 多投放

    def test_each_active_placement_checked_against_own_credential(self):
        # 扩为音频 + 文字，封存文字凭据并登记第二条投放
        c = self.client
        c.put(f"/api/licenses/{self.lic}",
              json=put_body(allow_media=["audio", "text"]))
        cid_t = c.post(f"/api/interviews/{self.iid}/credentials", json={
            "publication_date": PUB_DATE, "medium": "text",
        }).get_json()["id"]
        pid_t = c.post(f"/api/credentials/{cid_t}/placements", json={
            "channel": "文字专栏", "url": "https://example.com/t/1"}
        ).get_json()["id"]
        # 改成只允许音频：音频投放安全，文字投放阻断 -> 整笔回滚
        resp = c.put(f"/api/licenses/{self.lic}",
                     json=put_body(allow_media=["audio"]))
        self.assertEqual(resp.status_code, 409)
        affected = resp.get_json()["affected_placements"]
        self.assertEqual([a["placement_id"] for a in affected], [pid_t])
        self.assertEqual(affected[0]["medium"], "text")
        self.assertEqual(affected[0]["credential_id"], cid_t)
        self.assertEqual(self.licenses()[0]["allow_media"],
                         ["audio", "text"])  # 已回滚

    def test_two_active_placements_same_credential_both_listed(self):
        second = self.client.post(
            f"/api/credentials/{self.cid}/placements",
            json={"channel": "镜像站", "url": "https://example.com/m/1"}
        ).get_json()["id"]
        resp = self.client.delete(f"/api/licenses/{self.lic}")
        self.assertEqual(resp.status_code, 409)
        pids = {a["placement_id"]
                for a in resp.get_json()["affected_placements"]}
        self.assertEqual(pids, {self.pid, second})

    # ---------------------------------------------------------- 提交后口径不变

    def test_committed_change_still_flags_credential_review(self):
        # 撤下后做仍可发布的等价改动（改备注）：照常提交，资料变化仍按
        # 原逻辑显示「待复核」，封存凭据不改动。
        self.withdraw()
        resp = self.client.put(f"/api/licenses/{self.lic}",
                               json=put_body(note="改了备注"))
        self.assertEqual(resp.status_code, 200)
        review = self.client.get(
            f"/api/credentials/{self.cid}").get_json()
        self.assertEqual(review["status"], "review")
        self.assertEqual(review["changes"], ["授权"])
        self.assertEqual(review["current"]["blocked"], [])  # 仍可发布
        # 撤下的投放仍保留撤下状态，风险提示可查
        placement = self.client.get(
            f"/api/placements/{self.pid}").get_json()
        self.assertEqual(placement["status"], "withdrawn")
        self.assertEqual(placement["risk"], "review")


if __name__ == "__main__":
    unittest.main()
