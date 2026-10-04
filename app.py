"""口述史发布许可校验台 —— Flask 后端（本地运行，SQLite 持久化，无外部服务）。"""
import json
import os
import sqlite3
from datetime import date

from flask import Flask, g, jsonify, render_template, request

import credentials
import placements
from evaluator import MEDIA_LABELS, evaluate_timeline
from remediation import run_remediation

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "oral_history.db"))
MEDIA = tuple(MEDIA_LABELS.keys())

SCHEMA = """
CREATE TABLE IF NOT EXISTS interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE IF NOT EXISTS licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    allow_media TEXT NOT NULL DEFAULT '[]',
    deny_media TEXT NOT NULL DEFAULT '[]',
    embargo_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
-- 发行凭据：封存当时的完整输入（snapshot）与校验结果（result），
-- 不设外键 —— 原访谈删除后历史凭据仍可查看（状态「来源缺失」）。
CREATE TABLE IF NOT EXISTS release_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    interview_title TEXT NOT NULL,
    medium TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- 凭据不可修改、不可删除（可追溯）
CREATE TRIGGER IF NOT EXISTS release_credentials_no_update
BEFORE UPDATE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可修改'); END;
CREATE TRIGGER IF NOT EXISTS release_credentials_no_delete
BEFORE DELETE ON release_credentials
BEGIN SELECT RAISE(ABORT, '发行凭据不可删除'); END;
-- 渠道投放台账：已封存凭据的投放记录（投放中 / 已撤下）。
CREATE TABLE IF NOT EXISTS channel_placements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    credential_id INTEGER NOT NULL REFERENCES release_credentials(id),
    channel TEXT NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'withdrawn')),
    withdraw_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    withdrawn_at TEXT
);
-- 台账只增不删；登记时的凭据、渠道、链接与登记时间不可修改；
-- 已撤下的记录为最终状态，不可再改动（撤下时间随之保留）。
CREATE TRIGGER IF NOT EXISTS channel_placements_no_delete
BEFORE DELETE ON channel_placements
BEGIN SELECT RAISE(ABORT, '投放记录不可删除'); END;
CREATE TRIGGER IF NOT EXISTS channel_placements_sealed_fields
BEFORE UPDATE ON channel_placements
WHEN OLD.credential_id <> NEW.credential_id
  OR OLD.channel <> NEW.channel
  OR OLD.url <> NEW.url
  OR OLD.created_at <> NEW.created_at
BEGIN SELECT RAISE(ABORT, '投放记录的凭据、渠道、链接与登记时间不可修改'); END;
CREATE TRIGGER IF NOT EXISTS channel_placements_no_reopen
BEFORE UPDATE ON channel_placements
WHEN OLD.status = 'withdrawn'
BEGIN SELECT RAISE(ABORT, '投放记录已撤下，不可再修改'); END;
"""

app = Flask(__name__)


# ---------------------------------------------------------------- 基础设施

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    conn.close()


init_db()


def error(message, status=400):
    return jsonify(error=message), status


def payload():
    try:
        data = request.get_json(force=True)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def as_int(value):
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def get_interview(iid):
    return get_db().execute(
        "SELECT * FROM interviews WHERE id = ?", (iid,)).fetchone()


def get_speaker(sid):
    return get_db().execute(
        "SELECT * FROM speakers WHERE id = ?", (sid,)).fetchone()


def license_dict(row):
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


def parse_media_list(value):
    """校验媒介列表，合法返回去重后的 list，非法返回 None。"""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if item not in MEDIA:
            return None
        if item not in result:
            result.append(item)
    return result


def parse_date_field(value):
    """空 -> None；合法 ISO 日期 -> 原字符串；非法 -> 'invalid'。"""
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        return "invalid"
    try:
        date.fromisoformat(value)
    except ValueError:
        return "invalid"
    return value


def validate_range(start, end, duration):
    if start is None or end is None:
        return "开始与结束时间必须为整数秒"
    if start < 0:
        return "开始时间不能为负"
    if end <= start:
        return "结束时间必须大于开始时间"
    if duration is not None and end > duration:
        return f"结束时间不能超过访谈时长（{duration} 秒）"
    return None


# ---------------------------------------------------------------- 页面

@app.get("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------- 访谈

@app.get("/api/interviews")
def list_interviews():
    rows = get_db().execute(
        "SELECT * FROM interviews ORDER BY id").fetchall()
    return jsonify([
        {"id": r["id"], "title": r["title"],
         "duration_seconds": r["duration_seconds"]}
        for r in rows
    ])


@app.post("/api/interviews")
def create_interview():
    data = payload()
    title = str(data.get("title") or "").strip()
    duration = as_int(data.get("duration_seconds"))
    if not title:
        return error("访谈标题不能为空")
    if duration is None or duration <= 0:
        return error("访谈时长必须为正整数秒")
    db = get_db()
    cur = db.execute(
        "INSERT INTO interviews (title, duration_seconds) VALUES (?, ?)",
        (title, duration))
    db.commit()
    return jsonify(id=cur.lastrowid, title=title,
                   duration_seconds=duration), 201


@app.get("/api/interviews/<int:iid>")
def interview_detail(iid):
    db = get_db()
    interview = get_interview(iid)
    if not interview:
        return error("访谈不存在", 404)
    speakers = db.execute(
        "SELECT * FROM speakers WHERE interview_id = ? ORDER BY id",
        (iid,)).fetchall()
    segments = db.execute(
        "SELECT * FROM segments WHERE interview_id = ?"
        " ORDER BY start_seconds, id", (iid,)).fetchall()
    licenses = db.execute(
        "SELECT l.* FROM licenses l JOIN speakers s ON s.id = l.speaker_id"
        " WHERE s.interview_id = ? ORDER BY l.start_seconds, l.id",
        (iid,)).fetchall()
    return jsonify(
        id=interview["id"],
        title=interview["title"],
        duration_seconds=interview["duration_seconds"],
        speakers=[{"id": s["id"], "name": s["name"]} for s in speakers],
        segments=[{"id": s["id"], "speaker_id": s["speaker_id"],
                   "start_seconds": s["start_seconds"],
                   "end_seconds": s["end_seconds"]} for s in segments],
        licenses=[license_dict(l) for l in licenses],
    )


@app.put("/api/interviews/<int:iid>")
def update_interview(iid):
    if not get_interview(iid):
        return error("访谈不存在", 404)
    data = payload()
    title = str(data.get("title") or "").strip()
    duration = as_int(data.get("duration_seconds"))
    if not title:
        return error("访谈标题不能为空")
    if duration is None or duration <= 0:
        return error("访谈时长必须为正整数秒")
    db = get_db()
    db.execute(
        "UPDATE interviews SET title = ?, duration_seconds = ? WHERE id = ?",
        (title, duration, iid))
    db.commit()
    return jsonify(id=iid, title=title, duration_seconds=duration)


@app.delete("/api/interviews/<int:iid>")
def delete_interview(iid):
    db = get_db()
    cur = db.execute("DELETE FROM interviews WHERE id = ?", (iid,))
    db.commit()
    if cur.rowcount == 0:
        return error("访谈不存在", 404)
    return jsonify(deleted=iid)


# ---------------------------------------------------------------- 说话人

@app.post("/api/interviews/<int:iid>/speakers")
def create_speaker(iid):
    if not get_interview(iid):
        return error("访谈不存在", 404)
    name = str(payload().get("name") or "").strip()
    if not name:
        return error("说话人姓名不能为空")
    db = get_db()
    cur = db.execute(
        "INSERT INTO speakers (interview_id, name) VALUES (?, ?)",
        (iid, name))
    db.commit()
    return jsonify(id=cur.lastrowid, interview_id=iid, name=name), 201


@app.put("/api/speakers/<int:sid>")
def update_speaker(sid):
    speaker = get_speaker(sid)
    if not speaker:
        return error("说话人不存在", 404)
    name = str(payload().get("name") or "").strip()
    if not name:
        return error("说话人姓名不能为空")
    db = get_db()
    db.execute("UPDATE speakers SET name = ? WHERE id = ?", (name, sid))
    db.commit()
    return jsonify(id=sid, interview_id=speaker["interview_id"], name=name)


@app.delete("/api/speakers/<int:sid>")
def delete_speaker(sid):
    db = get_db()
    cur = db.execute("DELETE FROM speakers WHERE id = ?", (sid,))
    db.commit()
    if cur.rowcount == 0:
        return error("说话人不存在", 404)
    return jsonify(deleted=sid)


# ---------------------------------------------------------------- 发言区间

@app.post("/api/interviews/<int:iid>/segments")
def create_segment(iid):
    interview = get_interview(iid)
    if not interview:
        return error("访谈不存在", 404)
    data = payload()
    speaker_id = as_int(data.get("speaker_id"))
    speaker = get_speaker(speaker_id) if speaker_id is not None else None
    if not speaker or speaker["interview_id"] != iid:
        return error("说话人不属于该访谈")
    start = as_int(data.get("start_seconds"))
    end = as_int(data.get("end_seconds"))
    problem = validate_range(start, end, interview["duration_seconds"])
    if problem:
        return error(problem)
    db = get_db()
    cur = db.execute(
        "INSERT INTO segments (interview_id, speaker_id, start_seconds,"
        " end_seconds) VALUES (?, ?, ?, ?)",
        (iid, speaker_id, start, end))
    db.commit()
    return jsonify(id=cur.lastrowid, speaker_id=speaker_id,
                   start_seconds=start, end_seconds=end), 201


@app.put("/api/segments/<int:seg_id>")
def update_segment(seg_id):
    db = get_db()
    segment = db.execute(
        "SELECT * FROM segments WHERE id = ?", (seg_id,)).fetchone()
    if not segment:
        return error("发言区间不存在", 404)
    interview = get_interview(segment["interview_id"])
    data = payload()
    speaker_id = as_int(data.get("speaker_id"))
    speaker = get_speaker(speaker_id) if speaker_id is not None else None
    if not speaker or speaker["interview_id"] != segment["interview_id"]:
        return error("说话人不属于该访谈")
    start = as_int(data.get("start_seconds"))
    end = as_int(data.get("end_seconds"))
    problem = validate_range(start, end, interview["duration_seconds"])
    if problem:
        return error(problem)
    db.execute(
        "UPDATE segments SET speaker_id = ?, start_seconds = ?,"
        " end_seconds = ? WHERE id = ?",
        (speaker_id, start, end, seg_id))
    db.commit()
    return jsonify(id=seg_id, speaker_id=speaker_id,
                   start_seconds=start, end_seconds=end)


@app.delete("/api/segments/<int:seg_id>")
def delete_segment(seg_id):
    db = get_db()
    cur = db.execute("DELETE FROM segments WHERE id = ?", (seg_id,))
    db.commit()
    if cur.rowcount == 0:
        return error("发言区间不存在", 404)
    return jsonify(deleted=seg_id)


# ---------------------------------------------------------------- 授权区间

def read_license_payload(data, duration):
    """解析授权请求体，返回 (values dict, 错误消息)。"""
    start = as_int(data.get("start_seconds"))
    end = as_int(data.get("end_seconds"))
    problem = validate_range(start, end, duration)
    if problem:
        return None, problem
    allow = parse_media_list(data.get("allow_media"))
    deny = parse_media_list(data.get("deny_media"))
    if allow is None or deny is None:
        return None, "媒介取值仅限 audio（音频）/ text（文字）"
    embargo = parse_date_field(data.get("embargo_until"))
    if embargo == "invalid":
        return None, "解禁日期格式应为 YYYY-MM-DD"
    note = str(data.get("note") or "").strip()
    return {
        "start_seconds": start,
        "end_seconds": end,
        "allow_media": allow,
        "deny_media": deny,
        "embargo_until": embargo,
        "note": note,
    }, None


@app.post("/api/speakers/<int:sid>/licenses")
def create_license(sid):
    speaker = get_speaker(sid)
    if not speaker:
        return error("说话人不存在", 404)
    interview = get_interview(speaker["interview_id"])
    values, problem = read_license_payload(
        payload(), interview["duration_seconds"])
    if problem:
        return error(problem)
    db = get_db()
    cur = db.execute(
        "INSERT INTO licenses (speaker_id, start_seconds, end_seconds,"
        " allow_media, deny_media, embargo_until, note)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sid, values["start_seconds"], values["end_seconds"],
         json.dumps(values["allow_media"]),
         json.dumps(values["deny_media"]),
         values["embargo_until"], values["note"]))
    db.commit()
    return jsonify(id=cur.lastrowid, speaker_id=sid, **values), 201


@app.put("/api/licenses/<int:lic_id>")
def update_license(lic_id):
    db = get_db()
    lic = db.execute(
        "SELECT * FROM licenses WHERE id = ?", (lic_id,)).fetchone()
    if not lic:
        return error("授权记录不存在", 404)
    speaker = get_speaker(lic["speaker_id"])
    interview = get_interview(speaker["interview_id"])
    values, problem = read_license_payload(
        payload(), interview["duration_seconds"])
    if problem:
        return error(problem)
    db.execute(
        "UPDATE licenses SET start_seconds = ?, end_seconds = ?,"
        " allow_media = ?, deny_media = ?, embargo_until = ?, note = ?"
        " WHERE id = ?",
        (values["start_seconds"], values["end_seconds"],
         json.dumps(values["allow_media"]),
         json.dumps(values["deny_media"]),
         values["embargo_until"], values["note"], lic_id))
    db.commit()
    return jsonify(id=lic_id, speaker_id=lic["speaker_id"], **values)


@app.delete("/api/licenses/<int:lic_id>")
def delete_license(lic_id):
    db = get_db()
    cur = db.execute("DELETE FROM licenses WHERE id = ?", (lic_id,))
    db.commit()
    if cur.rowcount == 0:
        return error("授权记录不存在", 404)
    return jsonify(deleted=lic_id)


# ---------------------------------------------------------------- 校验

@app.post("/api/interviews/<int:iid>/evaluate")
def evaluate(iid):
    db = get_db()
    interview = get_interview(iid)
    if not interview:
        return error("访谈不存在", 404)
    data = payload()
    medium = data.get("medium")
    if medium not in MEDIA:
        return error("媒介必须是 audio（音频）或 text（文字）")
    publication_date = parse_date_field(data.get("publication_date"))
    if not publication_date or publication_date == "invalid":
        return error("发布日期不能为空且格式应为 YYYY-MM-DD")

    speakers = db.execute(
        "SELECT * FROM speakers WHERE interview_id = ?", (iid,)).fetchall()
    segments = db.execute(
        "SELECT * FROM segments WHERE interview_id = ?", (iid,)).fetchall()
    licenses = db.execute(
        "SELECT l.* FROM licenses l JOIN speakers s ON s.id = l.speaker_id"
        " WHERE s.interview_id = ?", (iid,)).fetchall()

    intervals = evaluate_timeline(
        duration=interview["duration_seconds"],
        speaker_names={s["id"]: s["name"] for s in speakers},
        segments=[{"speaker_id": s["speaker_id"],
                   "start_seconds": s["start_seconds"],
                   "end_seconds": s["end_seconds"]} for s in segments],
        licenses=[license_dict(l) for l in licenses],
        publication_date=publication_date,
        medium=medium,
    )
    duration = interview["duration_seconds"]
    blocked = sum(i["end_seconds"] - i["start_seconds"]
                  for i in intervals if i["status"] == "blocked")
    return jsonify(
        interview_id=iid,
        medium=medium,
        publication_date=publication_date,
        intervals=intervals,
        summary={
            "duration_seconds": duration,
            "blocked_seconds": blocked,
            "publishable_seconds": duration - blocked,
        },
    )


# ---------------------------------------------------------------- 补件并复核

@app.post("/api/interviews/<int:iid>/remediate")
def remediate(iid):
    """补件并复核：同一 SQLite 事务中先试算当前授权，再合并整批补件重算。

    请求体 {publication_date, medium, commit, supplements: [...]}。
    仅当合并补件后整条时间轴全部可发布且 commit 为真时才一次性写入；
    否则不落任何记录，返回每个阻断区间及原因（commit 时 HTTP 409）。
    重叠 / 互相冲突 / 越界的补件令整批失败（HTTP 400）。
    """
    interview = get_interview(iid)
    if not interview:
        return error("访谈不存在", 404)
    data = payload()
    medium = data.get("medium")
    if medium not in MEDIA:
        return error("媒介必须是 audio（音频）或 text（文字）")
    publication_date = parse_date_field(data.get("publication_date"))
    if not publication_date or publication_date == "invalid":
        return error("发布日期不能为空且格式应为 YYYY-MM-DD")
    result, status = run_remediation(
        get_db(),
        interview_id=iid,
        duration=interview["duration_seconds"],
        publication_date=publication_date,
        medium=medium,
        raw_supplements=data.get("supplements"),
        commit=bool(data.get("commit")),
    )
    return jsonify(result), status


# ---------------------------------------------------------------- 发行凭据

@app.post("/api/interviews/<int:iid>/credentials")
def create_release_credential(iid):
    """校验并封存发行凭据：同一事务内读取时长、说话人、发言与授权，
    按既有规则校验；仅当整条时间轴全部可发布时才保存不可修改的凭据
    （封存当时的完整输入与校验结果），失败不留任何记录。"""
    data = payload()
    medium = data.get("medium")
    if medium not in MEDIA:
        return error("媒介必须是 audio（音频）或 text（文字）")
    publication_date = parse_date_field(data.get("publication_date"))
    if not publication_date or publication_date == "invalid":
        return error("发布日期不能为空且格式应为 YYYY-MM-DD")
    result, status = credentials.create_credential(
        get_db(), interview_id=iid,
        publication_date=publication_date, medium=medium)
    return jsonify(result), status


@app.get("/api/credentials")
def list_release_credentials():
    """凭据列表（含当前状态：与发行时一致 / 待复核 / 来源缺失）。"""
    return jsonify(credentials.list_credentials(get_db()))


@app.get("/api/credentials/<int:cid>")
def release_credential_detail(cid):
    """凭据详情：保留发行时结论，并在一致读取快照中比较现有资料、
    重新校验 —— 资料有变化标为「待复核」并列出当前阻断区间；
    原访谈已删除时标为「来源缺失」，历史凭据仍可查看。"""
    review = credentials.credential_review(get_db(), cid)
    if review is None:
        return error("凭据不存在", 404)
    return jsonify(review)


# ---------------------------------------------------------------- 渠道投放台账

@app.post("/api/credentials/<int:cid>/placements")
def create_placement(cid):
    """登记投放：录入渠道名与投放链接，在同一 SQLite 写事务中复核来源
    —— 仅当原访谈仍在、资料与封存时一致且当前整段可发布时，才保存
    「投放中」记录及登记时间；否则说明原因且不留任何记录。"""
    data = payload()
    channel = str(data.get("channel") or "").strip()
    url = str(data.get("url") or "").strip()
    if not channel:
        return error("渠道名不能为空")
    if not url:
        return error("投放链接不能为空")
    result, status = placements.register_placement(
        get_db(), credential_id=cid, channel=channel, url=url)
    return jsonify(result), status


@app.get("/api/placements")
def list_channel_placements():
    """投放列表：一致读取快照中展示投放状态与凭据当前风险
    （正常 / 需处理 / 来源缺失），历史链接与时间始终保留。"""
    return jsonify(placements.list_placements(get_db()))


@app.get("/api/placements/<int:pid>")
def channel_placement_detail(pid):
    """投放详情：投放状态、封存结论与凭据当前风险（含当前阻断区间）。"""
    detail = placements.placement_review(get_db(), pid)
    if detail is None:
        return error("投放记录不存在", 404)
    return jsonify(detail)


@app.post("/api/placements/<int:pid>/withdraw")
def withdraw_channel_placement(pid):
    """撤下投放：确认渠道内容已撤下后填写原因，把记录标记为已撤下
    并保留撤下时间；风险提示仍可查，凭据不改动。"""
    reason = str(payload().get("reason") or "").strip()
    result, status = placements.withdraw_placement(
        get_db(), placement_id=pid, reason=reason)
    return jsonify(result), status


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
