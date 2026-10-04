"""evaluator 核心校验逻辑的单元测试（不依赖 Flask，可直接运行）。"""
import unittest

from evaluator import evaluate_timeline, validate_supplement_batch


def lic(lic_id, sp, start, end, allow=(), deny=(), embargo=None):
    return {
        "id": lic_id,
        "speaker_id": sp,
        "start_seconds": start,
        "end_seconds": end,
        "allow_media": list(allow),
        "deny_media": list(deny),
        "embargo_until": embargo,
    }


def seg(sp, start, end):
    return {"speaker_id": sp, "start_seconds": start, "end_seconds": end}


class EvaluateTimelineTest(unittest.TestCase):
    def test_missing_license_blocks(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)], [], "2026-09-23", "audio")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "blocked")
        self.assertFalse(res[0]["publishable"])
        self.assertIn("缺项", res[0]["reasons"][0])

    def test_all_ok(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)],
            [lic(1, 1, 0, 600, allow=["audio"])], "2026-09-23", "audio")
        self.assertEqual(res[0]["status"], "ok")
        self.assertTrue(res[0]["publishable"])
        self.assertEqual(res[0]["reasons"], [])

    def test_deny_priority(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)],
            [lic(1, 1, 0, 600, deny=["audio"])], "2026-09-23", "audio")
        self.assertEqual(res[0]["status"], "blocked")
        self.assertIn("拒绝", res[0]["reasons"][0])

    def test_conflict_allow_and_deny(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)],
            [lic(1, 1, 0, 600, allow=["audio"]),
             lic(2, 1, 0, 600, deny=["audio"])],
            "2026-09-23", "audio")
        self.assertEqual(res[0]["status"], "blocked")
        self.assertIn("冲突", res[0]["reasons"][0])
        self.assertIn("拒绝优先", res[0]["reasons"][0])

    def test_medium_not_covered_is_missing(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)],
            [lic(1, 1, 0, 600, allow=["audio"])], "2026-09-23", "text")
        self.assertEqual(res[0]["status"], "blocked")
        self.assertIn("缺项", res[0]["reasons"][0])

    def test_embargo_not_lifted(self):
        licenses = [lic(1, 1, 0, 600, allow=["audio"], embargo="2030-01-01")]
        blocked = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)], licenses, "2026-09-23", "audio")
        self.assertEqual(blocked[0]["status"], "blocked")
        self.assertIn("未解禁", blocked[0]["reasons"][0])
        # 解禁日当天即可发布
        released = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)], licenses, "2030-01-01", "audio")
        self.assertEqual(released[0]["status"], "ok")

    def test_boundary_split_by_license(self):
        # 授权只覆盖前半段 -> 前半可发布，后半缺项
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 600)],
            [lic(1, 1, 0, 300, allow=["audio"])], "2026-09-23", "audio")
        self.assertEqual([(r["start_seconds"], r["end_seconds"], r["status"])
                          for r in res],
                         [(0, 300, "ok"), (300, 600, "blocked")])

    def test_no_speech_interval(self):
        res = evaluate_timeline(
            600, {1: "甲"}, [seg(1, 0, 300)],
            [lic(1, 1, 0, 300, allow=["audio"])], "2026-09-23", "audio")
        self.assertEqual(res[-1]["status"], "no_speech")
        self.assertTrue(res[-1]["publishable"])
        self.assertEqual((res[-1]["start_seconds"], res[-1]["end_seconds"]),
                         (300, 600))

    def test_merge_adjacent_same_result(self):
        # 两个说话人相邻发言、授权均有效 -> 合并为一个可发布区间
        res = evaluate_timeline(
            600, {1: "甲", 2: "乙"},
            [seg(1, 0, 300), seg(2, 300, 600)],
            [lic(1, 1, 0, 600, allow=["audio"]),
             lic(2, 2, 0, 600, allow=["audio"])],
            "2026-09-23", "audio")
        self.assertEqual(len(res), 1)
        self.assertEqual((res[0]["start_seconds"], res[0]["end_seconds"]),
                         (0, 600))
        self.assertEqual(res[0]["speakers"], ["甲", "乙"])

    def test_no_merge_when_reasons_differ(self):
        # 相邻区间原因不同 -> 不合并
        res = evaluate_timeline(
            600, {1: "甲", 2: "乙"},
            [seg(1, 0, 300), seg(2, 300, 600)],
            [lic(1, 1, 0, 300, deny=["audio"])],  # 乙完全无授权
            "2026-09-23", "audio")
        self.assertEqual(len(res), 2)
        self.assertTrue(all(r["status"] == "blocked" for r in res))
        self.assertIn("拒绝", res[0]["reasons"][0])
        self.assertIn("缺项", res[1]["reasons"][0])

    def test_multi_speaker_overlap_all_must_be_covered(self):
        # 重叠发言：两人都须有有效授权，缺一人即不可发布
        res = evaluate_timeline(
            600, {1: "甲", 2: "乙"},
            [seg(1, 0, 600), seg(2, 300, 600)],
            [lic(1, 1, 0, 600, allow=["audio"]),
             lic(2, 2, 0, 600, allow=["audio"])],
            "2026-09-23", "audio")
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["status"], "ok")
        # 乙的授权只到 450 -> 450-600 缺乙授权
        res2 = evaluate_timeline(
            600, {1: "甲", 2: "乙"},
            [seg(1, 0, 600), seg(2, 300, 600)],
            [lic(1, 1, 0, 600, allow=["audio"]),
             lic(2, 2, 0, 450, allow=["audio"])],
            "2026-09-23", "audio")
        tail = [r for r in res2 if r["start_seconds"] == 450][0]
        self.assertEqual(tail["status"], "blocked")
        self.assertTrue(any("乙" in r and "缺项" in r for r in tail["reasons"]))

    def test_timecode_format(self):
        res = evaluate_timeline(
            3661, {1: "甲"}, [seg(1, 0, 3661)],
            [lic(1, 1, 0, 3661, allow=["text"])], "2026-09-23", "text")
        self.assertEqual(res[0]["start_timecode"], "00:00:00")
        self.assertEqual(res[0]["end_timecode"], "01:01:01")


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


class ValidateSupplementBatchTest(unittest.TestCase):
    SPEAKERS = {1, 2}
    DURATION = 600

    def check(self, items):
        return validate_supplement_batch(items, self.SPEAKERS, self.DURATION)

    def test_valid_batch_normalized(self):
        parsed, problem = self.check([
            sup(1, 0, 300, allow=["audio", "audio"], note=" 补签 "),
            sup(2, 300, 600, embargo="2027-01-01"),
        ])
        self.assertIsNone(problem)
        self.assertEqual(len(parsed), 2)
        # 媒介去重、备注去空白、空解禁日期归一为 None
        self.assertEqual(parsed[0]["allow_media"], ["audio"])
        self.assertEqual(parsed[0]["note"], "补签")
        self.assertIsNone(parsed[0]["embargo_until"])
        self.assertEqual(parsed[1]["embargo_until"], "2027-01-01")

    def test_empty_batch_fails(self):
        for items in ([], None, "x"):
            parsed, problem = self.check(items)
            self.assertIsNone(parsed)
            self.assertIn("空", problem)

    def test_unknown_speaker_fails(self):
        parsed, problem = self.check([sup(99, 0, 100)])
        self.assertIsNone(parsed)
        self.assertIn("说话人", problem)

    def test_out_of_bounds_fails(self):
        for bad in (sup(1, 0, 601), sup(1, -1, 100), sup(1, 100, 100),
                    sup(1, 200, 100)):
            parsed, problem = self.check([bad])
            self.assertIsNone(parsed, bad)
        _, problem = self.check([sup(1, 0, 601)])
        self.assertIn("越界", problem)

    def test_non_integer_range_fails(self):
        parsed, problem = self.check([sup(1, "0", 100)])
        self.assertIsNone(parsed)
        self.assertIn("整数", problem)

    def test_bad_medium_fails(self):
        parsed, problem = self.check([sup(1, 0, 100, allow=["video"])])
        self.assertIsNone(parsed)
        self.assertIn("媒介", problem)

    def test_bad_embargo_fails(self):
        parsed, problem = self.check([sup(1, 0, 100, embargo="2027/01/01")])
        self.assertIsNone(parsed)
        self.assertIn("解禁日期", problem)

    def test_self_conflict_fails(self):
        parsed, problem = self.check([sup(1, 0, 100, deny=["audio"])])
        self.assertIsNone(parsed)
        self.assertIn("冲突", problem)

    def test_overlap_same_speaker_fails(self):
        parsed, problem = self.check([sup(1, 0, 300), sup(1, 200, 400)])
        self.assertIsNone(parsed)
        self.assertIn("重叠", problem)
        self.assertIn("第 2 条", problem)
        self.assertIn("第 1 条", problem)

    def test_touching_intervals_do_not_overlap(self):
        # 半开区间 [0,300) 与 [300,600) 首尾相接不算重叠
        parsed, problem = self.check([sup(1, 0, 300), sup(1, 300, 600)])
        self.assertIsNone(problem)
        self.assertEqual(len(parsed), 2)

    def test_overlap_different_speakers_ok(self):
        parsed, problem = self.check([sup(1, 0, 300), sup(2, 100, 400)])
        self.assertIsNone(problem)
        self.assertEqual(len(parsed), 2)


if __name__ == "__main__":
    unittest.main()
