"""口述史发布许可校验核心逻辑。

按发言区间与授权区间的边界切分时间轴，逐段判定是否可发布
（拒绝优先；冲突 / 缺项 / 未解禁均不可发布并给出原因），
最后将相邻且结果相同的区间合并。

另提供补件批次校验（validate_supplement_batch）：补件并复核流程
在落库前对整批待补授权做纯数据校验，任一补件非法即整批失败。
"""
from datetime import date

MEDIA_LABELS = {"audio": "音频", "text": "文字"}

STATUS_LABELS = {
    "ok": "可发布",
    "blocked": "不可发布",
    "no_speech": "无发言",
}


def format_timecode(seconds):
    """秒 -> HH:MM:SS 时间码。"""
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _clamp(value, duration):
    return min(max(value, 0), duration)


def evaluate_timeline(duration, speaker_names, segments, licenses,
                      publication_date, medium):
    """切分并校验时间轴，返回合并后的区间列表。

    参数:
        duration: 访谈总时长（秒，正整数）。
        speaker_names: {speaker_id: 姓名}。
        segments: [{"speaker_id", "start_seconds", "end_seconds"}, ...]。
        licenses: [{"id", "speaker_id", "start_seconds", "end_seconds",
                    "allow_media": [...], "deny_media": [...],
                    "embargo_until": "YYYY-MM-DD" 或 None}, ...]。
        publication_date: 发布日期 "YYYY-MM-DD"（ISO 字符串可直接比较）。
        medium: "audio" 或 "text"。

    返回: [{"start_seconds", "end_seconds", "start_timecode", "end_timecode",
            "status": ok|blocked|no_speech, "publishable": bool,
            "speakers": [姓名...], "reasons": [原因...]}, ...]
    """
    # 1. 收集所有边界点：访谈首尾 + 发言区间边界 + 授权区间边界
    boundaries = {0, duration}
    for seg in segments:
        boundaries.add(_clamp(seg["start_seconds"], duration))
        boundaries.add(_clamp(seg["end_seconds"], duration))
    for lic in licenses:
        boundaries.add(_clamp(lic["start_seconds"], duration))
        boundaries.add(_clamp(lic["end_seconds"], duration))
    points = sorted(boundaries)

    medium_label = MEDIA_LABELS.get(medium, medium)
    elementary = []

    # 2. 逐基本区间判定
    for start, end in zip(points, points[1:]):
        if end <= start:
            continue
        active_ids = sorted({
            seg["speaker_id"] for seg in segments
            if seg["start_seconds"] < end and seg["end_seconds"] > start
        })
        if not active_ids:
            elementary.append({
                "start_seconds": start,
                "end_seconds": end,
                "status": "no_speech",
                "speakers": [],
                "reasons": [],
            })
            continue

        reasons = []
        for sp_id in active_ids:
            name = speaker_names.get(sp_id, f"说话人#{sp_id}")
            covering = [
                lic for lic in licenses
                if lic["speaker_id"] == sp_id
                and lic["start_seconds"] < end
                and lic["end_seconds"] > start
            ]
            if not covering:
                reasons.append(f"{name}：缺少覆盖该区间的授权记录（缺项）")
                continue
            denies = [lic for lic in covering if medium in lic["deny_media"]]
            allows = [lic for lic in covering if medium in lic["allow_media"]]
            if denies and allows:
                reasons.append(
                    f"{name}：授权冲突，许可#{allows[0]['id']} 允许{medium_label}、"
                    f"许可#{denies[0]['id']} 拒绝{medium_label}，按拒绝优先处理"
                )
                continue
            if denies:
                ids = "、".join(f"许可#{lic['id']}" for lic in denies)
                reasons.append(f"{name}：{ids} 明确拒绝{medium_label}媒介（拒绝优先）")
                continue
            if not allows:
                reasons.append(f"{name}：现有授权均未允许{medium_label}媒介（缺项）")
                continue
            usable = [
                lic for lic in allows
                if not lic["embargo_until"] or lic["embargo_until"] <= publication_date
            ]
            if not usable:
                latest = max(lic["embargo_until"] for lic in allows)
                reasons.append(
                    f"{name}：{medium_label}授权尚未解禁"
                    f"（解禁日 {latest}，晚于发布日期 {publication_date}）"
                )

        elementary.append({
            "start_seconds": start,
            "end_seconds": end,
            "status": "ok" if not reasons else "blocked",
            "speakers": [speaker_names.get(i, f"说话人#{i}") for i in active_ids],
            "reasons": reasons,
        })

    # 3. 相邻同结果（状态 + 原因一致）区间合并
    merged = []
    for item in elementary:
        if (merged
                and merged[-1]["status"] == item["status"]
                and merged[-1]["reasons"] == item["reasons"]):
            merged[-1]["end_seconds"] = item["end_seconds"]
            for name in item["speakers"]:
                if name not in merged[-1]["speakers"]:
                    merged[-1]["speakers"].append(name)
        else:
            merged.append({
                **item,
                "speakers": list(item["speakers"]),
                "reasons": list(item["reasons"]),
            })

    for item in merged:
        item["start_timecode"] = format_timecode(item["start_seconds"])
        item["end_timecode"] = format_timecode(item["end_seconds"])
        item["status_label"] = STATUS_LABELS[item["status"]]
        item["publishable"] = item["status"] != "blocked"
    return merged


# ---------------------------------------------------------------- 补件批次校验

def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _clean_media_list(value, media):
    """媒介列表去重校验，合法返回 list，非法返回 None。"""
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    result = []
    for item in value:
        if item not in media:
            return None
        if item not in result:
            result.append(item)
    return result


def validate_supplement_batch(items, speaker_ids, duration, media=MEDIA_LABELS):
    """校验整批待补授权，返回 (规范化补件列表, 错误消息)。

    全部合法时错误消息为 None，补件规范化为
    {"speaker_id", "start_seconds", "end_seconds", "allow_media",
     "deny_media", "embargo_until", "note"} 字典列表。

    以下任一情况整批失败（返回 (None, 错误消息)）：
      - 批次为空或条目不是对象；
      - 说话人不属于该访谈；
      - 时间区间非法或越界（超出 [0, duration]）；
      - 媒介取值非法、解禁日期格式错误；
      - 同一补件同时允许并拒绝同一媒介（互相冲突）；
      - 同一说话人的补件时间区间互相重叠。
    """
    if not isinstance(items, list) or not items:
        return None, "补件批次不能为空"
    parsed = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return None, f"第 {idx} 条补件格式错误"
        speaker_id = item.get("speaker_id")
        if not _is_int(speaker_id) or speaker_id not in speaker_ids:
            return None, f"第 {idx} 条补件的说话人不属于该访谈"
        start = item.get("start_seconds")
        end = item.get("end_seconds")
        if not _is_int(start) or not _is_int(end):
            return None, f"第 {idx} 条补件：开始与结束时间必须为整数秒"
        if start < 0:
            return None, f"第 {idx} 条补件：开始时间不能为负"
        if end <= start:
            return None, f"第 {idx} 条补件：结束时间必须大于开始时间"
        if end > duration:
            return None, (
                f"第 {idx} 条补件：结束时间不能超过访谈时长"
                f"（{duration} 秒），补件越界")
        allow = _clean_media_list(item.get("allow_media"), media)
        deny = _clean_media_list(item.get("deny_media"), media)
        if allow is None or deny is None:
            labels = " / ".join(f"{k}（{v}）" for k, v in media.items())
            return None, f"第 {idx} 条补件：媒介取值仅限 {labels}"
        embargo = item.get("embargo_until")
        if embargo in (None, ""):
            embargo = None
        elif not isinstance(embargo, str):
            return None, f"第 {idx} 条补件：解禁日期格式应为 YYYY-MM-DD"
        else:
            try:
                date.fromisoformat(embargo)
            except ValueError:
                return None, f"第 {idx} 条补件：解禁日期格式应为 YYYY-MM-DD"
        if set(allow) & set(deny):
            return None, (
                f"第 {idx} 条补件同时允许并拒绝同一媒介"
                "（互相冲突），整批失败")
        parsed.append({
            "speaker_id": speaker_id,
            "start_seconds": start,
            "end_seconds": end,
            "allow_media": allow,
            "deny_media": deny,
            "embargo_until": embargo,
            "note": str(item.get("note") or "").strip(),
        })

    # 同一说话人的补件时间区间不得互相重叠（半开区间 [start, end)）
    by_speaker = {}
    for idx, values in enumerate(parsed, 1):
        for prev_idx, prev in by_speaker.get(values["speaker_id"], []):
            if (values["start_seconds"] < prev["end_seconds"]
                    and prev["start_seconds"] < values["end_seconds"]):
                return None, (
                    f"第 {idx} 条与第 {prev_idx} 条补件为同一说话人"
                    "且时间区间重叠，整批失败")
        by_speaker.setdefault(values["speaker_id"], []).append((idx, values))
    return parsed, None
