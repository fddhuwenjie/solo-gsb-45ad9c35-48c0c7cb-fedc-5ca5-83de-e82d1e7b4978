"""发行凭据：同一 SQLite 事务中校验并封存不可修改的发行凭据。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

创建（create_credential）：BEGIN IMMEDIATE -> 读取时长 / 说话人 /
发言 / 授权 -> 按既有规则校验 -> 仅当整条时间轴全部可发布时，把当时的
完整输入与校验结果一并封存为不可修改的凭据并提交；否则回滚，
不落任何记录。

查阅（credential_review / list_credentials）：保留发行时结论，并在
一致的读取快照（同一事务）中比较现有资料、重新校验 —— 时长、说话人、
发言或授权有任何变化即标为「待复核」（即便仍可发布），并列出当前
阻断区间；原访谈已删除时标为「来源缺失」，历史凭据仍可查看。
"""
import json

from evaluator import evaluate_timeline

# 凭据复核状态
STATUS_VALID = "valid"      # 与发行时一致
STATUS_REVIEW = "review"    # 待复核：资料有变化
STATUS_MISSING = "missing"  # 来源缺失：原访谈已删除

STATUS_LABELS = {
    STATUS_VALID: "与发行时一致",
    STATUS_REVIEW: "待复核",
    STATUS_MISSING: "来源缺失",
}

# 参与「是否有变化」比较的封存资料类别
COMPARE_FIELDS = (
    ("duration_seconds", "时长"),
    ("speakers", "说话人"),
    ("segments", "发言"),
    ("licenses", "授权"),
)

FETCH_LICENSES = (
    "SELECT l.* FROM licenses l JOIN speakers s ON s.id = l.speaker_id"
    " WHERE s.interview_id = ? ORDER BY l.id")


def _fetch_inputs(conn, interview_id):
    """读取访谈当前的完整校验输入；访谈不存在时返回 None。"""
    interview = conn.execute(
        "SELECT * FROM interviews WHERE id = ?", (interview_id,)).fetchone()
    if interview is None:
        return None
    speakers = conn.execute(
        "SELECT * FROM speakers WHERE interview_id = ? ORDER BY id",
        (interview_id,)).fetchall()
    segments = conn.execute(
        "SELECT * FROM segments WHERE interview_id = ? ORDER BY id",
        (interview_id,)).fetchall()
    licenses = conn.execute(FETCH_LICENSES, (interview_id,)).fetchall()
    return {
        "title": interview["title"],
        "duration_seconds": interview["duration_seconds"],
        "speakers": [{"id": s["id"], "name": s["name"]} for s in speakers],
        "segments": [{"id": g["id"], "speaker_id": g["speaker_id"],
                      "start_seconds": g["start_seconds"],
                      "end_seconds": g["end_seconds"]} for g in segments],
        "licenses": [{
            "id": l["id"],
            "speaker_id": l["speaker_id"],
            "start_seconds": l["start_seconds"],
            "end_seconds": l["end_seconds"],
            "allow_media": json.loads(l["allow_media"]),
            "deny_media": json.loads(l["deny_media"]),
            "embargo_until": l["embargo_until"],
            "note": l["note"],
        } for l in licenses],
    }


def _snapshot_of(inputs):
    """从完整输入中提取参与封存 / 比较的资料（标题不参与比较）。"""
    return {key: inputs[key] for key, _ in COMPARE_FIELDS}


def _evaluate(inputs, publication_date, medium):
    return evaluate_timeline(
        duration=inputs["duration_seconds"],
        speaker_names={s["id"]: s["name"] for s in inputs["speakers"]},
        segments=inputs["segments"],
        licenses=inputs["licenses"],
        publication_date=publication_date,
        medium=medium)


def _summary(duration, intervals):
    blocked = sum(i["end_seconds"] - i["start_seconds"]
                  for i in intervals if i["status"] == "blocked")
    return {
        "duration_seconds": duration,
        "blocked_seconds": blocked,
        "publishable_seconds": duration - blocked,
    }


def _credential_dict(row):
    return {
        "id": row["id"],
        "interview_id": row["interview_id"],
        "interview_title": row["interview_title"],
        "medium": row["medium"],
        "publication_date": row["publication_date"],
        "created_at": row["created_at"],
        "snapshot": json.loads(row["snapshot"]),
        "result": json.loads(row["result"]),
    }


def create_credential(conn, interview_id, publication_date, medium):
    """在同一事务中校验并封存发行凭据。

    仅当整条时间轴全部可发布时才写入不可修改的凭据（封存当时的完整
    输入与校验结果）；仍有阻断区间时回滚，不落任何记录。

    返回 (result, status)：
        201 —— 已封存，result 含凭据编号与封存的校验结果；
        404 —— 访谈不存在；
        409 —— 仍有阻断区间，未落任何记录（result 含 blocked 及原因）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        inputs = _fetch_inputs(conn, interview_id)
        if inputs is None:
            conn.rollback()
            return {"error": "访谈不存在"}, 404
        duration = inputs["duration_seconds"]
        intervals = _evaluate(inputs, publication_date, medium)
        blocked = [i for i in intervals if i["status"] == "blocked"]
        if blocked:
            conn.rollback()  # 失败不留记录
            return {
                "interview_id": interview_id,
                "medium": medium,
                "publication_date": publication_date,
                "created": False,
                "error": f"仍有 {len(blocked)} 个不可发布区间，未保存凭据",
                "intervals": intervals,
                "summary": _summary(duration, intervals),
                "blocked": blocked,
            }, 409
        result_doc = {"intervals": intervals,
                      "summary": _summary(duration, intervals)}
        cur = conn.execute(
            "INSERT INTO release_credentials (interview_id, interview_title,"
            " medium, publication_date, snapshot, result)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (interview_id, inputs["title"], medium, publication_date,
             json.dumps(_snapshot_of(inputs), ensure_ascii=False,
                        sort_keys=True),
             json.dumps(result_doc, ensure_ascii=False)))
        row = conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (cur.lastrowid,)).fetchone()
        conn.commit()
        credential = _credential_dict(row)
    except Exception:
        conn.rollback()
        raise
    return {
        "id": credential["id"],
        "interview_id": interview_id,
        "interview_title": credential["interview_title"],
        "medium": medium,
        "publication_date": publication_date,
        "created_at": credential["created_at"],
        "created": True,
        "result": credential["result"],
    }, 201


def _review(conn, credential):
    """在调用方事务（一致读取快照）内复核单条凭据。"""
    base = {
        "id": credential["id"],
        "interview_id": credential["interview_id"],
        "interview_title": credential["interview_title"],
        "medium": credential["medium"],
        "publication_date": credential["publication_date"],
        "created_at": credential["created_at"],
        # 发行时结论（封存，不可修改）
        "sealed": {
            "inputs": credential["snapshot"],
            "result": credential["result"],
        },
    }
    current = _fetch_inputs(conn, credential["interview_id"])
    if current is None:
        # 原访谈已删除：来源缺失，历史凭据仍可查看
        return {**base,
                "status": STATUS_MISSING,
                "status_label": STATUS_LABELS[STATUS_MISSING],
                "changes": [],
                "current": None}
    current_snapshot = _snapshot_of(current)
    changes = [label for key, label in COMPARE_FIELDS
               if credential["snapshot"].get(key)
               != current_snapshot.get(key)]
    intervals = _evaluate(current, credential["publication_date"],
                          credential["medium"])
    # 资料有任何变化即待复核 —— 即便重新校验仍可发布
    status = STATUS_REVIEW if changes else STATUS_VALID
    return {**base,
            "status": status,
            "status_label": STATUS_LABELS[status],
            "changes": changes,
            "current": {
                "inputs": current_snapshot,
                "intervals": intervals,
                "summary": _summary(current["duration_seconds"], intervals),
                "blocked": [i for i in intervals
                            if i["status"] == "blocked"],
            }}


def credential_review(conn, credential_id):
    """查阅单条凭据：保留发行时结论，并在一致读取快照中复核现状。

    返回凭据复核视图；凭据不存在时返回 None。
    """
    conn.execute("BEGIN")
    try:
        row = conn.execute(
            "SELECT * FROM release_credentials WHERE id = ?",
            (credential_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        review = _review(conn, _credential_dict(row))
        conn.rollback()  # 只读事务：释放快照，不落任何记录
    except Exception:
        conn.rollback()
        raise
    return review


def list_credentials(conn):
    """列出全部凭据及当前状态（同一事务内逐条复核，读取快照一致）。"""
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT * FROM release_credentials ORDER BY id").fetchall()
        items = []
        for row in rows:
            review = _review(conn, _credential_dict(row))
            items.append({
                "id": review["id"],
                "interview_id": review["interview_id"],
                "interview_title": review["interview_title"],
                "medium": review["medium"],
                "publication_date": review["publication_date"],
                "created_at": review["created_at"],
                "status": review["status"],
                "status_label": review["status_label"],
                "changes": review["changes"],
            })
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return items
