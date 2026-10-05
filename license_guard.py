"""授权变更提交前复核：在投投放关联版本的整段重校验。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

访谈已投放渠道后，修改或删除说话人授权可能让公开内容失去许可。
更新（update_license）与删除（delete_license）在同一 SQLite 写事务
（BEGIN IMMEDIATE）中：

1. 读取授权归属（授权 -> 说话人 -> 访谈）；
2. 试用变更（UPDATE / DELETE，暂不提交）；
3. 按该访谈每条仍在投记录所关联凭据的发布日期与媒介，
   重新校验整段时间轴；
4. 只要有一个版本出现不可发布区间，就回滚整笔变更（HTTP 409，
   返回受影响的投放 ID、链接、阻断区间及原因）；否则照常提交。

已撤下的投放不参与复核（不阻拦）；封存凭据不改动；变更提交后，
资料变化仍按凭据 / 台账原逻辑显示「待复核」。
"""
import json

import credentials
from evaluator import evaluate_timeline

# 该访谈全部「投放中」记录及其关联凭据的发布日期与媒介
FETCH_ACTIVE_PLACEMENTS = (
    "SELECT p.id, p.credential_id, p.channel, p.url,"
    " c.publication_date, c.medium"
    " FROM channel_placements p"
    " JOIN release_credentials c ON c.id = p.credential_id"
    " WHERE c.interview_id = ? AND p.status = 'active'"
    " ORDER BY p.id")


def _license_row(conn, license_id):
    return conn.execute(
        "SELECT * FROM licenses WHERE id = ?", (license_id,)).fetchone()


def _ownership(conn, row):
    """授权归属：授权 -> 说话人 -> 访谈主键。"""
    speaker = conn.execute(
        "SELECT * FROM speakers WHERE id = ?",
        (row["speaker_id"],)).fetchone()
    return speaker["interview_id"] if speaker is not None else None


def _affected_placements(conn, interview_id):
    """按每条在投投放关联凭据的发布日期与媒介重校验整段时间轴。

    在调用方事务内读取（含试用变更）。返回受影响投放列表
    [{placement_id, credential_id, channel, url, publication_date,
      medium, blocked}]；无在投投放或全部版本仍可发布时为空列表。
    """
    placements = conn.execute(
        FETCH_ACTIVE_PLACEMENTS, (interview_id,)).fetchall()
    if not placements:
        return []
    inputs = credentials._fetch_inputs(conn, interview_id)
    speaker_names = {s["id"]: s["name"] for s in inputs["speakers"]}
    trials = {}  # (publication_date, medium) -> 合并后的区间列表
    affected = []
    for p in placements:
        key = (p["publication_date"], p["medium"])
        if key not in trials:
            trials[key] = evaluate_timeline(
                duration=inputs["duration_seconds"],
                speaker_names=speaker_names,
                segments=inputs["segments"],
                licenses=inputs["licenses"],
                publication_date=p["publication_date"],
                medium=p["medium"])
        blocked = [i for i in trials[key] if i["status"] == "blocked"]
        if blocked:
            affected.append({
                "placement_id": p["id"],
                "credential_id": p["credential_id"],
                "channel": p["channel"],
                "url": p["url"],
                "publication_date": p["publication_date"],
                "medium": p["medium"],
                "blocked": blocked,
            })
    return affected


def _blocked_result(license_id, interview_id, action, affected):
    """409 响应体：受影响投放的 ID、链接、阻断区间及原因。"""
    return {
        action: False,
        "license_id": license_id,
        "interview_id": interview_id,
        "error": (
            f"该访谈有 {len(affected)} 条在投投放关联的版本"
            "在变更后出现不可发布区间，已回滚整笔变更"),
        "affected_placements": affected,
    }, 409


def update_license(conn, license_id, values):
    """在同一 SQLite 写事务中试用授权修改并按在投投放复核。

    values: {"start_seconds", "end_seconds", "allow_media", "deny_media",
             "embargo_until", "note"}（媒介为列表，落库时 JSON 序列化）。

    返回 (result, status)：
        200 —— 已提交，result 为更新后的授权（含 id 与 speaker_id）；
        404 —— 授权记录不存在；
        409 —— 在投投放关联版本出现不可发布区间，整笔回滚
               （result 含 affected_placements：投放 ID、链接、
               阻断区间及原因）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _license_row(conn, license_id)
        if row is None:
            conn.rollback()
            return {"error": "授权记录不存在"}, 404
        interview_id = _ownership(conn, row)
        speaker_id = row["speaker_id"]
        # 试用变更：同一事务内更新，复核不通过则随事务整体回滚
        conn.execute(
            "UPDATE licenses SET start_seconds = ?, end_seconds = ?,"
            " allow_media = ?, deny_media = ?, embargo_until = ?, note = ?"
            " WHERE id = ?",
            (values["start_seconds"], values["end_seconds"],
             json.dumps(values["allow_media"]),
             json.dumps(values["deny_media"]),
             values["embargo_until"], values["note"], license_id))
        affected = _affected_placements(conn, interview_id)
        if affected:
            conn.rollback()  # 整笔回滚
            return _blocked_result(
                license_id, interview_id, "updated", affected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": license_id, "speaker_id": speaker_id, **values}, 200


def delete_license(conn, license_id):
    """在同一 SQLite 写事务中试用授权删除并按在投投放复核。

    返回 (result, status)：
        200 —— 已提交，result 为 {"deleted": license_id}；
        404 —— 授权记录不存在；
        409 —— 在投投放关联版本出现不可发布区间，整笔回滚
               （result 含 affected_placements：投放 ID、链接、
               阻断区间及原因）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _license_row(conn, license_id)
        if row is None:
            conn.rollback()
            return {"error": "授权记录不存在"}, 404
        interview_id = _ownership(conn, row)
        # 试用变更：同一事务内删除，复核不通过则随事务整体回滚
        conn.execute("DELETE FROM licenses WHERE id = ?", (license_id,))
        affected = _affected_placements(conn, interview_id)
        if affected:
            conn.rollback()  # 整笔回滚
            return _blocked_result(
                license_id, interview_id, "deleted", affected)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"deleted": license_id}, 200
