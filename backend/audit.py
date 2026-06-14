from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, has_request_context, request
from flask_login import current_user
from sqlalchemy import cast, func, or_, String

from .models import AuditLog, db


ACTIVE_ACTIONS = {
    "login",
    "create_meeting",
    "update_meeting",
    "delete_meeting",
    "create_topic",
    "update_topic",
    "delete_topic",
    "delete_attachment",
    "submit_topic",
    "withdraw_topic",
    "approve_topic",
    "reject_topic",
    "decide_topic",
    "upload_attachment",
    "decrypt_attachment_failed",
    "download_attachment",
    "preview_attachment",
    "review_material",
    "update_minutes",
    "create_user",
    "update_user",
    "delete_user",
    "reset_password",
    "create_knowhow",
    "update_knowhow",
    "delete_knowhow",
    "create_ai_prompt",
    "update_ai_prompt",
    "delete_ai_prompt",
    "apply_copilot_proposal",
    "reject_copilot_proposal",
}


ACTION_LABELS = {
    "login": "登录",
    "logout": "登出",
    "create_meeting": "创建会议",
    "update_meeting": "更新会议",
    "delete_meeting": "删除会议",
    "create_topic": "创建议题",
    "update_topic": "更新议题",
    "delete_topic": "删除议题",
    "delete_attachment": "删除附件",
    "submit_topic": "提交议题",
    "withdraw_topic": "撤回议题",
    "approve_topic": "通过议题",
    "reject_topic": "驳回议题",
    "decide_topic": "会议决策议题",
    "share_topic": "共享议题",
    "revoke_topic_share": "撤销议题共享",
    "reorder_topic": "调整议题顺序",
    "update_agenda": "更新会议议题",
    "upload_attachment": "上传附件",
    "decrypt_attachment_failed": "附件解密失败",
    "download_attachment": "下载附件",
    "preview_attachment": "预览附件",
    "review_material": "材料 Review",
    "update_minutes": "保存会议纪要",
    "create_user": "创建用户",
    "update_user": "更新用户",
    "delete_user": "删除用户",
    "reset_password": "重置密码",
    "create_group": "创建用户组",
    "update_group": "更新用户组",
    "delete_group": "删除用户组",
    "update_config": "更新系统配置",
    "create_knowhow": "新增 know-how",
    "update_knowhow": "更新 know-how",
    "delete_knowhow": "删除 know-how",
    "create_ai_prompt": "新增 AI 提示词模板",
    "update_ai_prompt": "更新 AI 提示词模板",
    "delete_ai_prompt": "删除 AI 提示词模板",
    "apply_copilot_proposal": "应用 Copilot 提案",
    "reject_copilot_proposal": "拒绝 Copilot 提案",
}


TARGET_TYPE_LABELS = {
    "meeting": "会议",
    "topic": "议题",
    "attachment": "附件",
    "minutes": "会议纪要",
    "user": "用户",
    "group": "用户组",
    "config": "系统配置",
    "knowhow": "know-how",
    "ai_prompt": "AI 提示词模板",
    "copilot_proposal": "Copilot 提案",
}


CONFIG_KEY_LABELS = {
    "meeting_readiness": "会议成熟度",
    "topic_completeness": "议题完善度",
    "ai_review_prompt": "AI Review 提示词",
    "lark": "飞书配置",
}


SENSITIVE_KEY_PARTS = ("password", "secret", "token", "key")


METADATA_LABELS = {
    "meeting_no": "归属会议",
    "meeting_id": "会议",
    "topic_id": "议题",
    "topic_ids": "议题顺序",
    "topic_title": "议题标题",
    "topic_count": "议题数",
    "requested_meeting_id": "申请上会",
    "title": "标题",
    "status": "状态",
    "meeting_status": "会议状态",
    "workflow_status": "审批状态",
    "decision_status": "会议决策",
    "file_type": "文件类型",
    "source": "来源",
    "result": "Review 结果",
    "score": "评分",
    "review_comment": "审批意见",
    "shared_with": "共享给",
    "revoked_from": "取消共享",
    "role": "角色",
    "enabled": "启用",
    "group_id": "组",
    "member_count": "成员数",
    "weights": "评分权重",
    "thresholds": "分档阈值",
    "scope": "适用范围",
    "default": "默认模板",
    "active": "启用状态",
    "outputs": "输出要求",
    "knowledge_sources": "知识来源",
    "approved": "本次通过",
    "unbound": "本次解绑",
    "reorder": "议题顺序",
    "auto_rejected_topics": "自动驳回数",
    "reverted_topics": "回退数",
}


NESTED_LABELS = {
    "weights": {
        "basic_info": "基础信息",
        "background_purpose": "背景目的",
        "attachment": "附件",
        "review": "Review",
    },
    "thresholds": {
        "ready": "已就绪",
        "mostly_ready": "基本就绪",
        "preparing": "准备中",
    },
}


TOPIC_STATUS_VALUES = {"pending": "待准备", "ready": "已就绪", "presented": "已汇报"}
MEETING_STATUS_VALUES = {"draft": "草稿", "preparing": "准备中", "reporting": "汇报中", "completed": "已完成"}
WORKFLOW_STATUS_VALUES = {
    "draft": "草稿",
    "submitted": "已提交",
    "approved": "已通过",
    "rejected": "已驳回",
    "withdrawn": "已撤回",
}
TOPIC_DECISION_STATUS_VALUES = {
    "pending": "待决策",
    "approved": "已通过",
    "conditional_approved": "有条件通过",
    "delayed": "延期",
    "rejected": "已驳回",
}
ROLE_VALUES = {"admin": "管理员", "user": "普通用户"}
REVIEW_RESULT_VALUES = {"approved": "通过", "needs_revision": "需补充", "rejected": "未通过"}
REVIEW_SOURCE_VALUES = {"hoster": "Hoster", "ai": "AI"}


def format_audit_metadata(action, metadata):
    if not metadata or not isinstance(metadata, dict):
        return []
    items = []
    for key, raw in metadata.items():
        if raw is None or raw == "" or raw == []:
            continue
        label = METADATA_LABELS.get(key, key)
        items.append({"label": label, "value": _format_audit_value(key, raw)})
    return items


def _format_audit_value(key, value):
    if isinstance(value, bool):
        return "是" if value else "否"
    if key == "status":
        return TOPIC_STATUS_VALUES.get(value, value)
    if key == "meeting_status":
        return MEETING_STATUS_VALUES.get(value, value)
    if key == "workflow_status":
        return WORKFLOW_STATUS_VALUES.get(value, value)
    if key == "decision_status":
        return TOPIC_DECISION_STATUS_VALUES.get(value, value)
    if key == "role":
        return ROLE_VALUES.get(value, value)
    if key == "result":
        return REVIEW_RESULT_VALUES.get(value, value)
    if key == "source":
        return REVIEW_SOURCE_VALUES.get(value, value)
    if key == "file_type" and isinstance(value, str):
        return value.upper()
    if isinstance(value, dict):
        sub_labels = NESTED_LABELS.get(key, {})
        parts = ["{} {}".format(sub_labels.get(k, k), v) for k, v in value.items()]
        return " · ".join(parts)
    if isinstance(value, list):
        return "、".join(str(v) for v in value[:10]) + ("…" if len(value) > 10 else "")
    return str(value)


def record_audit(action, target_type=None, target_id=None, target_label=None, metadata=None):
    if not getattr(current_user, "is_authenticated", False):
        return None

    try:
        user_agent = ""
        method = ""
        path = ""
        ip_address = ""
        if has_request_context():
            method = request.method
            path = request.path[:255]
            ip_address = (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()
            user_agent = (request.headers.get("User-Agent", "") or "")[:255]

        log = AuditLog(
            user_id=current_user.id,
            username_snapshot=current_user.username,
            display_name_snapshot=current_user.display_name,
            role_snapshot=current_user.role,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=(target_label or "")[:255] or None,
            request_method=method,
            request_path=path,
            ip_address=ip_address[:80],
            user_agent=user_agent,
            metadata_json=sanitize_metadata(metadata),
        )
        db.session.add(log)
        db.session.commit()
        return log
    except Exception as exc:  # pragma: no cover - defensive logging only
        db.session.rollback()
        current_app.logger.warning("Failed to write audit log: %s", exc)
        return None


def sanitize_metadata(metadata):
    if not metadata:
        return None
    sanitized = {}
    for key, value in dict(metadata).items():
        key_text = str(key)
        if any(part in key_text.lower() for part in SENSITIVE_KEY_PARTS):
            continue
        sanitized[key_text[:80]] = stringify_metadata_value(value)
    return sanitized or None


def stringify_metadata_value(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [stringify_metadata_value(item) for item in value[:20]]
    if isinstance(value, dict):
        return sanitize_metadata(value)
    text = str(value)
    return text[:500]


def audit_query_from_request(args):
    query = AuditLog.query
    user_id = args.get("user_id", type=int)
    action = args.get("action", "").strip()
    target_type = args.get("target_type", "").strip()
    keyword = args.get("keyword", "").strip()
    start = local_date_start(args.get("start_date"))
    end = local_date_end(args.get("end_date"))

    if user_id:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if target_type:
        query = query.filter(AuditLog.target_type == target_type)
    if start:
        query = query.filter(AuditLog.created_at >= start)
    if end:
        query = query.filter(AuditLog.created_at < end)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            or_(
                AuditLog.target_label.like(like),
                AuditLog.request_path.like(like),
                AuditLog.action.like(like),
                AuditLog.display_name_snapshot.like(like),
                AuditLog.username_snapshot.like(like),
                cast(AuditLog.metadata_json, String).like(like),
            )
        )
    return query


def activity_summary():
    now_local = datetime.now(app_timezone())
    today_start = local_to_utc_naive(datetime.combine(now_local.date(), time.min, tzinfo=app_timezone()))
    today_end = today_start + timedelta(days=1)
    month_start_local = datetime(now_local.year, now_local.month, 1, tzinfo=app_timezone())
    month_start = local_to_utc_naive(month_start_local)
    if now_local.month == 12:
        next_month_local = datetime(now_local.year + 1, 1, 1, tzinfo=app_timezone())
    else:
        next_month_local = datetime(now_local.year, now_local.month + 1, 1, tzinfo=app_timezone())
    month_end = local_to_utc_naive(next_month_local)

    daily = daily_trends(now_local.date(), days=14)
    return {
        "today_start": today_start,
        "today_end": today_end,
        "month_start": month_start,
        "month_end": month_end,
        "dau": active_user_count(today_start, today_end),
        "mau": active_user_count(month_start, month_end),
        "total_active_users": total_active_user_count(),
        "today_operations": operation_count(today_start, today_end),
        "month_operations": operation_count(month_start, month_end),
        "total_operations": operation_count(),
        "daily_trends": daily,
        "trend_totals": trend_totals(daily),
        "top_users": top_active_users(now_local - timedelta(days=30)),
    }


def active_user_count(start, end):
    return (
        db.session.query(func.count(func.distinct(AuditLog.user_id)))
        .filter(
            AuditLog.user_id.isnot(None),
            AuditLog.action.in_(ACTIVE_ACTIONS),
            AuditLog.created_at >= start,
            AuditLog.created_at < end,
        )
        .scalar()
        or 0
    )


def total_active_user_count():
    return (
        db.session.query(func.count(func.distinct(AuditLog.user_id)))
        .filter(AuditLog.user_id.isnot(None), AuditLog.action.in_(ACTIVE_ACTIONS))
        .scalar()
        or 0
    )


def operation_count(start=None, end=None):
    query = AuditLog.query.filter(AuditLog.user_id.isnot(None), AuditLog.action.in_(ACTIVE_ACTIONS))
    if start is not None:
        query = query.filter(AuditLog.created_at >= start)
    if end is not None:
        query = query.filter(AuditLog.created_at < end)
    return query.count()


def daily_trends(end_date, days=14):
    rows = []
    for offset in range(days - 1, -1, -1):
        day = end_date - timedelta(days=offset)
        start = local_to_utc_naive(datetime.combine(day, time.min, tzinfo=app_timezone()))
        end = start + timedelta(days=1)
        rows.append(
            {
                "date": day,
                "active_users": active_user_count(start, end),
                "operations": operation_count(start, end),
            }
        )
    max_active = max([row["active_users"] for row in rows] + [1])
    max_operations = max([row["operations"] for row in rows] + [1])
    for row in rows:
        row["active_percent"] = int(row["active_users"] / max_active * 100)
        row["operation_percent"] = int(row["operations"] / max_operations * 100)
    for index, row in enumerate(rows):
        row["line_x"] = int(index / (days - 1) * 100) if days > 1 else 50
        row["line_y"] = 100 - row["active_percent"]
    if rows:
        rows[0]["max_active"] = max_active
        rows[0]["max_operations"] = max_operations
    return rows


def trend_totals(rows):
    total_active = sum(row["active_users"] for row in rows)
    total_operations = sum(row["operations"] for row in rows)
    peak = max(rows, key=lambda row: (row["operations"], row["active_users"]), default=None)
    return {
        "active_users": total_active,
        "operations": total_operations,
        "peak_date": peak["date"] if peak else None,
    }


def top_active_users(start_local):
    start = local_to_utc_naive(start_local)
    return (
        db.session.query(
            AuditLog.user_id,
            AuditLog.username_snapshot,
            AuditLog.display_name_snapshot,
            AuditLog.role_snapshot,
            func.count(AuditLog.id).label("operation_count"),
            func.max(AuditLog.created_at).label("last_active"),
        )
        .filter(
            AuditLog.user_id.isnot(None),
            AuditLog.action.in_(ACTIVE_ACTIONS),
            AuditLog.created_at >= start,
        )
        .group_by(
            AuditLog.user_id,
            AuditLog.username_snapshot,
            AuditLog.display_name_snapshot,
            AuditLog.role_snapshot,
        )
        .order_by(func.count(AuditLog.id).desc(), func.max(AuditLog.created_at).desc())
        .limit(10)
        .all()
    )


def default_audit_start_date():
    return (datetime.now(app_timezone()).date() - timedelta(days=30)).isoformat()


def default_audit_end_date():
    return datetime.now(app_timezone()).date().isoformat()


def local_date_start(value):
    if not value:
        return None
    try:
        local_dt = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min, tzinfo=app_timezone())
    except ValueError:
        return None
    return local_to_utc_naive(local_dt)


def local_date_end(value):
    start = local_date_start(value)
    return start + timedelta(days=1) if start else None


def local_to_utc_naive(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=app_timezone())
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def format_local_datetime(value):
    if not value:
        return "-"
    return value.replace(tzinfo=timezone.utc).astimezone(app_timezone()).strftime("%Y-%m-%d %H:%M")


def app_timezone():
    timezone_name = current_app.config.get("APP_TIMEZONE", "Asia/Shanghai")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        if timezone_name == "Asia/Shanghai":
            return timezone(timedelta(hours=8), name="Asia/Shanghai")
        return timezone.utc
