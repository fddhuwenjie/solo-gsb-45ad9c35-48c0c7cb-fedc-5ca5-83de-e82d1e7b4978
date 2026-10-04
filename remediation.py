"""补件并复核：同一 SQLite 事务中的补件试算与一次性写入。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

流程：BEGIN IMMEDIATE -> 复制当前授权并按既有规则试算 -> 校验整批
补件（重叠 / 互相冲突 / 越界令整批失败）-> 合并补件重算 ->
仅当整条时间轴全部可发布且 commit 为真时一次性写入并提交，
否则回滚，不落任何记录。
"""
import json

from evaluator import evaluate_timeline, validate_supplement_batch

FETCH_LICENSES = (
    "SELECT l.* FROM licenses l JOIN speakers s ON s.id = l.speaker_id"
    " WHERE s.interview_id = ? ORDER BY l.start_seconds, l.id")


def _license_dict(row):
    return {
        "id": row["id"],
        "speaker_id": row["speaker_id"],
        "start_seconds": row["start_seconds"],
        "end_seconds": row["end_seconds"],
        "allow_media": json.loads(row["allow_media"]),
        "deny_media": json.loads(row["deny_media"]),
        "embargo_until": row["embargo_until"],
        "note": row["note"],
    }


def _summary(duration, intervals):
    blocked = sum(i["end_seconds"] - i["start_seconds"]
                  for i in intervals if i["status"] == "blocked")
    return {
        "duration_seconds": duration,
        "blocked_seconds": blocked,
        "publishable_seconds": duration - blocked,
    }


def run_remediation(conn, interview_id, duration, publication_date, medium,
                    raw_supplements, commit):
    """在同一事务中试算并（满足条件时）写入整批补件。

    参数:
        conn: sqlite3 连接（row_factory 为 sqlite3.Row，外键已开启）。
        interview_id / duration: 访谈主键与时长（秒）。
        publication_date: 发布日期 "YYYY-MM-DD"。
        medium: "audio" 或 "text"。
        raw_supplements: 待补授权列表（未校验的原始字典）。
        commit: 为真且合并后全时间轴可发布时一次性写入。

    返回 (result, status)：
        200 —— 试算完成（committed 标记是否已写入）；
        400 —— 批次非法（重叠 / 冲突 / 越界等），未落任何记录；
        409 —— commit 请求但仍有阻断区间，未落任何记录。
        result 含 before / after 的区间与汇总、blocked 列表及原因。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 同一事务内读取发言数据并复制当前授权，按既有规则试算
        speakers = conn.execute(
            "SELECT * FROM speakers WHERE interview_id = ?",
            (interview_id,)).fetchall()
        segments = conn.execute(
            "SELECT * FROM segments WHERE interview_id = ?",
            (interview_id,)).fetchall()
        speaker_names = {s["id"]: s["name"] for s in speakers}
        seg_dicts = [{"speaker_id": s["speaker_id"],
                      "start_seconds": s["start_seconds"],
                      "end_seconds": s["end_seconds"]} for s in segments]

        def trial(licenses):
            return evaluate_timeline(
                duration=duration,
                speaker_names=speaker_names,
                segments=seg_dicts,
                licenses=licenses,
                publication_date=publication_date,
                medium=medium,
            )

        current = [_license_dict(r) for r in
                   conn.execute(FETCH_LICENSES, (interview_id,)).fetchall()]
        before = trial(current)
        # 2. 校验整批补件（越界 / 互相冲突 / 重叠 -> 整批失败）
        supplements, problem = validate_supplement_batch(
            raw_supplements, set(speaker_names), duration)
        if problem:
            conn.rollback()
            return {"error": problem}, 400
        # 3. 合并整批补件后重算（补件以临时编号参与试算）
        merged = current + [
            {"id": f"补件{i}", **v} for i, v in enumerate(supplements, 1)]
        after = trial(merged)
        blocked = [i for i in after if i["status"] == "blocked"]
        inserted_ids = []
        committed = False
        if commit and not blocked:
            # 4. 整条时间轴全部可发布 -> 一次性写入，按真实许可编号复算
            for v in supplements:
                cur = conn.execute(
                    "INSERT INTO licenses (speaker_id, start_seconds,"
                    " end_seconds, allow_media, deny_media, embargo_until,"
                    " note) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (v["speaker_id"], v["start_seconds"], v["end_seconds"],
                     json.dumps(v["allow_media"]),
                     json.dumps(v["deny_media"]),
                     v["embargo_until"], v["note"]))
                inserted_ids.append(cur.lastrowid)
            after = trial([_license_dict(r) for r in conn.execute(
                FETCH_LICENSES, (interview_id,)).fetchall()])
            conn.commit()
            committed = True
        else:
            conn.rollback()  # 仅试算或仍有阻断：不落任何记录
    except Exception:
        conn.rollback()
        raise

    result = {
        "interview_id": interview_id,
        "medium": medium,
        "publication_date": publication_date,
        "commit_requested": commit,
        "committed": committed,
        "supplement_count": len(supplements),
        "before": {"intervals": before,
                   "summary": _summary(duration, before)},
        "after": {"intervals": after,
                  "summary": _summary(duration, after)},
        "blocked": [i for i in after if i["status"] == "blocked"],
    }
    if committed:
        result["inserted_license_ids"] = inserted_ids
        return result, 200
    if commit and result["blocked"]:
        result["error"] = (
            f"合并补件后仍有 {len(result['blocked'])} 个不可发布区间，"
            "未写入任何补件记录")
        return result, 409
    return result, 200
