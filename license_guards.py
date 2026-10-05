"""授权变更保护：修改 / 删除说话人授权前的在投渠道复核。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

update_license / delete_license：BEGIN IMMEDIATE -> 在同一写事务中
读取授权归属（所属说话人 / 访谈）并试用变更（先执行 UPDATE /
DELETE）-> 找出该访谈每条**仍在投**的渠道投放，按其关联凭据封存的
发布日期与媒介，对变更后的整段时间轴重新校验 -> 只要有一个版本
出现不可发布区间，就回滚整笔变更并返回受影响的投放 ID、链接、
阻断区间及原因；全部仍可发布才提交。

已撤下（withdrawn）的投放不阻拦；发行凭据封存内容始终不改动。
变更提交后资料与封存时是否一致，仍按凭据复核原逻辑显示「待复核」。

重新校验直接复用 credentials._fetch_inputs 读取事务内（含试用变更）
的完整输入，口径与凭据查阅、投放登记的复核完全一致。
"""
import json

import credentials
from evaluator import MEDIA_LABELS, evaluate_timeline

FETCH_LICENSE = "SELECT * FROM licenses WHERE id = ?"

# 该访谈全部「仍在投」记录及其关联凭据的发布日期与媒介；
# 同一凭据可能登记多条投放，故结果可能重复出现同一凭据。
FETCH_ACTIVE_PLACEMENTS = """
SELECT p.id AS placement_id, p.channel AS placement_channel,
       p.url AS placement_url, c.id AS credential_id,
       c.medium AS medium, c.publication_date AS publication_date
FROM channel_placements p
JOIN release_credentials c ON c.id = p.credential_id
WHERE c.interview_id = ? AND p.status = 'active'
ORDER BY p.id
"""

UPDATE_SQL = (
    "UPDATE licenses SET start_seconds = ?, end_seconds = ?,"
    " allow_media = ?, deny_media = ?, embargo_until = ?, note = ?"
    " WHERE id = ?"
)


def _blocked_placements(conn, interview_id):
    """在调用方事务（试用变更已落于事务内）中复核所有在投投放。

    返回受影响投放明细列表（每项含投放 ID、链接、按该凭据发布日期与
    媒介重算的阻断区间及原因）；无在投投放或全部仍可发布时返回 []。
    """
    rows = conn.execute(FETCH_ACTIVE_PLACEMENTS,
                        (interview_id,)).fetchall()
    if not rows:
        return []
    # 事务内读取：含本次试用的 UPDATE / DELETE，但尚未提交
    inputs = credentials._fetch_inputs(conn, interview_id)
    blocked_cache = {}  # 同一凭据只重算一次
    affected = []
    for row in rows:
        cid = row["credential_id"]
        if cid not in blocked_cache:
            intervals = evaluate_timeline(
                duration=inputs["duration_seconds"],
                speaker_names={s["id"]: s["name"]
                               for s in inputs["speakers"]},
                segments=inputs["segments"],
                licenses=inputs["licenses"],
                publication_date=row["publication_date"],
                medium=row["medium"])
            blocked_cache[cid] = [
                i for i in intervals if i["status"] == "blocked"]
        blocked = blocked_cache[cid]
        if blocked:
            affected.append({
                "placement_id": row["placement_id"],
                "credential_id": cid,
                "channel": row["placement_channel"],
                "url": row["placement_url"],
                "medium": row["medium"],
                "medium_label": MEDIA_LABELS[row["medium"]],
                "publication_date": row["publication_date"],
                "blocked": blocked,
            })
    return affected


def _guard_problem(action, lic_id, affected):
    """构造拦截响应体（调用方负责回滚）。"""
    return {
        "error": (
            f"该变更会令 {len(affected)} 条仍在投的渠道投放出现不可发布"
            f"区间，整笔变更已回滚，授权记录未改动（已撤下的投放不阻拦）"),
        "allowed": False,
        "rolled_back": True,
        "action": action,
        "license_id": lic_id,
        "affected_placements": affected,
    }


def update_license(conn, lic_id, values):
    """在同一写事务中试用授权修改并复核全部在投投放。

    values 为已通过 HTTP 层校验的授权字段。仅当该访谈每条仍在投记录
    按其凭据的发布日期与媒介重算后均无不可发布区间时才提交；否则
    回滚整笔变更并返回受影响投放明细。

    返回 (result, status)：
        200 —— 已提交，成功响应保持 {id, speaker_id, ...授权字段}；
        404 —— 授权记录不存在；
        409 —— 在投投放被阻断（affected_placements 含投放 ID、链接、
               阻断区间及原因），整笔变更已回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        lic = conn.execute(FETCH_LICENSE, (lic_id,)).fetchone()
        if lic is None:
            conn.rollback()
            return {"error": "授权记录不存在"}, 404
        interview_id = conn.execute(
            "SELECT interview_id FROM speakers WHERE id = ?",
            (lic["speaker_id"],)).fetchone()["interview_id"]
        conn.execute(
            UPDATE_SQL,
            (values["start_seconds"], values["end_seconds"],
             json.dumps(values["allow_media"]),
             json.dumps(values["deny_media"]),
             values["embargo_until"], values["note"], lic_id))
        affected = _blocked_placements(conn, interview_id)
        if affected:
            conn.rollback()  # 回滚整笔变更，授权未改动
            return _guard_problem("update", lic_id, affected), 409
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"id": lic_id, "speaker_id": lic["speaker_id"], **values}, 200


def delete_license(conn, lic_id):
    """在同一写事务中试用授权删除并复核全部在投投放。

    仅当该访谈每条仍在投记录按其凭据的发布日期与媒介重算后均无
    不可发布区间时才提交删除；否则回滚整笔变更并返回受影响投放明细。

    返回 (result, status)：
        200 —— 已删除，成功响应保持 {deleted: lic_id}；
        404 —— 授权记录不存在；
        409 —— 在投投放被阻断（affected_placements 含投放 ID、链接、
               阻断区间及原因），整笔变更已回滚。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        lic = conn.execute(FETCH_LICENSE, (lic_id,)).fetchone()
        if lic is None:
            conn.rollback()
            return {"error": "授权记录不存在"}, 404
        interview_id = conn.execute(
            "SELECT interview_id FROM speakers WHERE id = ?",
            (lic["speaker_id"],)).fetchone()["interview_id"]
        conn.execute("DELETE FROM licenses WHERE id = ?", (lic_id,))
        affected = _blocked_placements(conn, interview_id)
        if affected:
            conn.rollback()  # 回滚整笔删除，授权仍保留
            return _guard_problem("delete", lic_id, affected), 409
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"deleted": lic_id}, 200
