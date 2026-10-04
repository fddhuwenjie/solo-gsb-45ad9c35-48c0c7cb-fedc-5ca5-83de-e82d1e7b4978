"""渠道投放台账：已封存发行凭据的投放登记、查阅与撤下。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为
sqlite3.Row），不依赖 Flask，可独立测试。

登记（register_placement）：BEGIN IMMEDIATE -> 读取凭据并在同一事务中
复核来源（原访谈仍在、资料与封存时一致、当前整段可发布）-> 仅当全部
满足才保存「投放中」记录（含登记时间）并提交；否则回滚，说明原因且
不留任何记录。

查阅（placement_review / list_placements）：一致的读取快照（同一事务）
中展示投放状态与凭据当前风险 —— 资料后来变化即「需处理」（即便仍可
发布），来源删除即「来源缺失」；历史链接、时间与封存结论仍保留。

撤下（withdraw_placement）：BEGIN IMMEDIATE -> 确认记录存在且未撤下
-> 填写原因并标记「已撤下」，保留撤下时间；风险提示仍可查，凭据不改动。

来源复核直接复用 credentials 模块的复核视图（_review），保证台账的
风险判定与凭据查阅口径完全一致。
"""
import credentials

# 投放状态
STATUS_ACTIVE = "active"        # 投放中
STATUS_WITHDRAWN = "withdrawn"  # 已撤下

STATUS_LABELS = {
    STATUS_ACTIVE: "投放中",
    STATUS_WITHDRAWN: "已撤下",
}

# 凭据当前风险（由凭据复核状态映射而来）
RISK_OK = "ok"          # 正常：资料与封存时一致
RISK_REVIEW = "review"  # 需处理：资料后来有变化（即便仍可发布）
RISK_MISSING = "missing"  # 来源缺失：原访谈已删除

RISK_LABELS = {
    RISK_OK: "正常",
    RISK_REVIEW: "需处理",
    RISK_MISSING: "来源缺失",
}

_RISK_OF_CREDENTIAL_STATUS = {
    credentials.STATUS_VALID: RISK_OK,
    credentials.STATUS_REVIEW: RISK_REVIEW,
    credentials.STATUS_MISSING: RISK_MISSING,
}

FETCH_PLACEMENT = "SELECT * FROM channel_placements WHERE id = ?"


def _placement_dict(row):
    return {
        "id": row["id"],
        "credential_id": row["credential_id"],
        "channel": row["channel"],
        "url": row["url"],
        "status": row["status"],
        "withdraw_reason": row["withdraw_reason"],
        "created_at": row["created_at"],
        "withdrawn_at": row["withdrawn_at"],
    }


def _get_credential(conn, credential_id):
    """读取凭据（含封存的完整输入与发行时结论）；不存在时返回 None。"""
    row = conn.execute(
        "SELECT * FROM release_credentials WHERE id = ?",
        (credential_id,)).fetchone()
    return credentials._credential_dict(row) if row is not None else None


def _review_credential(conn, credential_id):
    """在调用方事务（一致读取快照）内复核凭据来源现状。"""
    credential = _get_credential(conn, credential_id)
    if credential is None:
        return None
    return credentials._review(conn, credential)


# ---------------------------------------------------------------- 登记

def _registration_problem(review):
    """根据凭据复核视图判断能否登记投放；不能时返回说明原因的响应体。

    仅当原访谈仍在、资料与封存时一致且当前整段可发布时才允许登记
    （返回 None）；其余情况说明原因，由调用方回滚、不留记录。
    """
    base = {
        "credential_id": review["id"],
        "interview_title": review["interview_title"],
        "created": False,
    }
    if review["status"] == credentials.STATUS_MISSING:
        return {**base,
                "reason": "missing",
                "error": "来源缺失：原访谈已删除，不能登记投放"}
    if review["changes"]:
        return {**base,
                "reason": "changed",
                "changes": review["changes"],
                "error": "资料与封存时不一致（"
                         + "、".join(review["changes"])
                         + "有变化），需处理后才能登记投放"}
    blocked = review["current"]["blocked"]
    if blocked:
        return {**base,
                "reason": "blocked",
                "blocked": blocked,
                "error": f"当前仍有 {len(blocked)} 个不可发布区间，不能登记投放"}
    return None


def register_placement(conn, credential_id, channel, url):
    """在同一 SQLite 写事务中复核来源并登记投放记录。

    仅当凭据存在、原访谈仍在、资料与封存时一致且当前整段可发布时，
    才保存「投放中」记录（含登记时间）并提交；否则回滚，说明原因且
    不留任何记录。

    返回 (result, status)：
        201 —— 已登记，result 含投放记录与登记时间；
        404 —— 凭据不存在；
        409 —— 来源缺失 / 资料已变化 / 当前仍有不可发布区间
               （result 含 reason 与原因说明），未留任何记录。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        review = _review_credential(conn, credential_id)
        if review is None:
            conn.rollback()
            return {"error": "凭据不存在"}, 404
        problem = _registration_problem(review)
        if problem is not None:
            conn.rollback()  # 说明原因，不留记录
            return problem, 409
        cur = conn.execute(
            "INSERT INTO channel_placements (credential_id, channel, url)"
            " VALUES (?, ?, ?)",
            (credential_id, channel, url))
        row = conn.execute(FETCH_PLACEMENT, (cur.lastrowid,)).fetchone()
        conn.commit()
        placement = _placement_dict(row)
    except Exception:
        conn.rollback()
        raise
    return {
        "id": placement["id"],
        "credential_id": credential_id,
        "interview_title": review["interview_title"],
        "channel": placement["channel"],
        "url": placement["url"],
        "status": placement["status"],
        "status_label": STATUS_LABELS[placement["status"]],
        "created_at": placement["created_at"],
        "created": True,
    }, 201


# ---------------------------------------------------------------- 查阅

def _risk_view(review):
    """从凭据复核视图提取台账展示用的风险信息。"""
    risk = _RISK_OF_CREDENTIAL_STATUS[review["status"]]
    return {"risk": risk, "risk_label": RISK_LABELS[risk],
            "changes": review["changes"]}


def _list_item(placement, review):
    return {
        "id": placement["id"],
        "credential_id": placement["credential_id"],
        "interview_title": review["interview_title"],
        "medium": review["medium"],
        "publication_date": review["publication_date"],
        "channel": placement["channel"],
        "url": placement["url"],
        "status": placement["status"],
        "status_label": STATUS_LABELS[placement["status"]],
        "created_at": placement["created_at"],
        "withdrawn_at": placement["withdrawn_at"],
        **_risk_view(review),
    }


def _detail(placement, review):
    current = review["current"]
    return {
        **_list_item(placement, review),
        "withdraw_reason": placement["withdraw_reason"],
        # 凭据与封存结论（不可修改，始终保留）
        "credential": {
            "id": review["id"],
            "interview_id": review["interview_id"],
            "interview_title": review["interview_title"],
            "medium": review["medium"],
            "publication_date": review["publication_date"],
            "created_at": review["created_at"],
            "sealed_result": review["sealed"]["result"],
        },
        # 当前风险明细；来源缺失时为 None
        "current": None if current is None else {
            "summary": current["summary"],
            "blocked": current["blocked"],
        },
    }


def list_placements(conn):
    """列出全部投放记录（同一事务内逐条复核风险，读取快照一致）。

    列表项含投放状态与凭据当前风险；历史链接、时间始终保留。
    """
    conn.execute("BEGIN")
    try:
        rows = conn.execute(
            "SELECT * FROM channel_placements ORDER BY id").fetchall()
        items = []
        for row in rows:
            review = _review_credential(conn, row["credential_id"])
            items.append(_list_item(_placement_dict(row), review))
        conn.rollback()  # 只读事务：释放快照，不落任何记录
    except Exception:
        conn.rollback()
        raise
    return items


def placement_review(conn, placement_id):
    """查阅单条投放记录：一致读取快照中的投放状态与凭据当前风险。

    返回投放详情（含封存结论与当前阻断区间）；记录不存在时返回 None。
    """
    conn.execute("BEGIN")
    try:
        row = conn.execute(FETCH_PLACEMENT, (placement_id,)).fetchone()
        if row is None:
            conn.rollback()
            return None
        review = _review_credential(conn, row["credential_id"])
        detail = _detail(_placement_dict(row), review)
        conn.rollback()  # 只读事务：释放快照
    except Exception:
        conn.rollback()
        raise
    return detail


# ---------------------------------------------------------------- 撤下

def withdraw_placement(conn, placement_id, reason):
    """在同一事务中把投放记录标记为已撤下（保留撤下时间与原因）。

    只更新投放记录，凭据不改动；已撤下的记录不可再修改
    （数据库触发器保证），重复撤下返回 409 并保留原撤下时间。

    返回 (result, status)：
        200 —— 已撤下，result 含撤下时间与原因；
        404 —— 投放记录不存在；
        409 —— 记录此前已撤下（保留原撤下时间，不再改动）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(FETCH_PLACEMENT, (placement_id,)).fetchone()
        if row is None:
            conn.rollback()
            return {"error": "投放记录不存在"}, 404
        if row["status"] == STATUS_WITHDRAWN:
            conn.rollback()
            return {
                "id": placement_id,
                "withdrawn": False,
                "error": "投放记录此前已撤下，保留原撤下时间",
                "withdrawn_at": row["withdrawn_at"],
                "withdraw_reason": row["withdraw_reason"],
            }, 409
        conn.execute(
            "UPDATE channel_placements SET status = ?, withdraw_reason = ?,"
            " withdrawn_at = datetime('now') WHERE id = ?",
            (STATUS_WITHDRAWN, reason, placement_id))
        updated = conn.execute(FETCH_PLACEMENT, (placement_id,)).fetchone()
        conn.commit()
        placement = _placement_dict(updated)
    except Exception:
        conn.rollback()
        raise
    return {
        "id": placement["id"],
        "credential_id": placement["credential_id"],
        "status": placement["status"],
        "status_label": STATUS_LABELS[placement["status"]],
        "withdrawn": True,
        "withdrawn_at": placement["withdrawn_at"],
        "withdraw_reason": placement["withdraw_reason"],
    }, 200
