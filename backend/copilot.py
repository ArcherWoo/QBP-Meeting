import json
from datetime import date, datetime

import requests
from flask import Response, abort, current_app, jsonify, request, stream_with_context
from flask_login import current_user, login_required

from .attachment_text import attachment_matches_message, message_requests_attachment_text
from .material_rag import indexed_attachment_context, retrieve_meeting_material_chunks
from .models import Attachment, Meeting, Topic, db


MAX_REFERENCED_MEETINGS = 5
MAX_PAGE_MEETINGS = 10


class ZhishuClient:
    def __init__(self, base_url, api_key, timeout=180):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def list_models(self):
        response = requests.get(
            f"{self.base_url}/api/v1/models",
            headers=self.headers,
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        if isinstance(data, list):
            return data
        return []

    def stream_chat(self, payload):
        response = requests.post(
            f"{self.base_url}/api/v1/chat/completions",
            headers=self.headers,
            json=payload,
            stream=True,
            timeout=self.timeout,
        )
        response.raise_for_status()
        for line in response.iter_lines(decode_unicode=True):
            if line:
                yield f"{line}\n\n"

    def chat(self, payload):
        response = requests.post(
            f"{self.base_url}/api/v1/chat/completions",
            headers=self.headers,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


def register_copilot_routes(app):
    @app.route("/copilot/models")
    @login_required
    def copilot_models():
        if not app.config.get("ZHISHU_API_KEY") and not app.config.get("ZHISHU_CLIENT"):
            return jsonify({"error": "请先配置 ZHISHU_API_KEY"}), 400
        try:
            models = normalize_models(zhishu_client().list_models())
        except Exception as exc:
            return jsonify({"error": f"智枢模型加载失败: {exc}"}), 502
        return jsonify(
            {
                "models": models,
                "default_model": app.config.get("COPILOT_DEFAULT_MODEL", ""),
            }
        )

    @app.route("/copilot/context/search")
    @login_required
    def copilot_context_search():
        query = request.args.get("q", "").strip()
        results = search_meetings(query)
        return jsonify({"results": [meeting_search_item(meeting) for meeting in results]})

    @app.route("/copilot/status")
    @login_required
    def copilot_status():
        return jsonify(
            {
                "base_url": app.config.get("ZHISHU_BASE_URL", ""),
                "api_key_configured": bool(app.config.get("ZHISHU_API_KEY") or app.config.get("ZHISHU_CLIENT")),
                "tool_server_configured": bool(app.config.get("QBP_TOOL_SERVER_TOKEN")),
                "tool_ids": configured_tool_ids(),
                "default_model": app.config.get("COPILOT_DEFAULT_MODEL", ""),
            }
        )

    @app.route("/copilot/chat/stream", methods=["POST"])
    @login_required
    def copilot_chat_stream():
        payload = request.get_json(silent=True) or {}
        message = (payload.get("message") or "").strip()
        if not message:
            return jsonify({"error": "消息不能为空"}), 400
        referenced = unique_non_empty(payload.get("referenced_meeting_nos", []))
        page_meeting_nos = scoped_page_meeting_nos(payload)
        current_meeting_no = (payload.get("current_meeting_no") or "").strip()
        current_topic_id = int(payload.get("current_topic_id") or 0)
        if len(referenced) > MAX_REFERENCED_MEETINGS:
            return jsonify({"error": "最多引用 5 个会议"}), 400

        context_meeting_nos = []
        if current_meeting_no:
            context_meeting_nos.append(current_meeting_no)
        elif page_meeting_nos:
            context_meeting_nos.extend(page_meeting_nos)
        context_meeting_nos.extend(referenced)
        context_meetings = meeting_contexts(unique_non_empty(context_meeting_nos), viewer=current_user)
        retrieved_material_chunks = requested_attachment_contexts(message, context_meetings, current_topic_id)

        model = payload.get("model") or app.config.get("COPILOT_DEFAULT_MODEL")
        if not model:
            return jsonify({"error": "请选择智枢模型"}), 400

        chat_payload = {
            "model": model,
            "stream": True,
            "messages": [
                {"role": "system", "content": build_system_context(context_meetings, retrieved_material_chunks)},
                {"role": "user", "content": message},
            ],
            "params": {
                "function_calling": app.config.get("COPILOT_FUNCTION_CALLING", "default"),
            },
        }
        tool_ids = configured_tool_ids()
        if tool_ids:
            chat_payload["tool_ids"] = tool_ids

        def generate():
            try:
                yield from normalize_sse_chunks(zhishu_client().stream_chat(chat_payload))
            except Exception as exc:
                error = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"data: {error}\n\n"

        return Response(stream_with_context(generate()), mimetype="text/event-stream")

    @app.route("/copilot/chat", methods=["POST"])
    @login_required
    def copilot_chat():
        payload = request.get_json(silent=True) or {}
        chat_payload, error = build_chat_payload(payload, stream=False)
        if error:
            return error
        try:
            data = zhishu_client().chat(chat_payload)
        except Exception as exc:
            return jsonify({"error": f"智枢请求失败: {exc}"}), 502
        return jsonify({"answer": extract_chat_answer(data), "model": chat_payload["model"]})

    @app.route("/copilot/tools/openapi.json")
    def copilot_tools_openapi():
        require_tool_token()
        return jsonify(tool_openapi_spec())

    @app.route("/copilot/tools/meetings/search")
    def tool_search_meetings():
        require_tool_token()
        query = request.args.get("q", "").strip()
        return jsonify({"results": [meeting_search_item(meeting) for meeting in search_meetings(query)]})

    @app.route("/copilot/tools/meetings/<meeting_no>")
    def tool_get_meeting(meeting_no):
        require_tool_token()
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        return jsonify(meeting_context(meeting))

    @app.route("/copilot/tools/topics/<int:topic_id>")
    def tool_get_topic(topic_id):
        require_tool_token()
        topic = db.session.get(Topic, topic_id) or abort(404)
        return jsonify(topic_context(topic))

    @app.route("/copilot/tools/attachments/<int:attachment_id>/content")
    def tool_get_attachment_content(attachment_id):
        require_tool_token()
        attachment = db.session.get(Attachment, attachment_id) or abort(404)
        return jsonify(indexed_attachment_context(attachment))

    @app.route("/copilot/tools/meetings/<meeting_no>/minutes")
    def tool_get_minutes(meeting_no):
        require_tool_token()
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        return jsonify(minutes_context(meeting.minutes))


def zhishu_client():
    configured = current_app.config.get("ZHISHU_CLIENT")
    if configured:
        return configured
    return ZhishuClient(
        current_app.config["ZHISHU_BASE_URL"],
        current_app.config["ZHISHU_API_KEY"],
    )


def normalize_models(models):
    normalized = []
    for model in models:
        model_id = model.get("id") or model.get("model")
        if not model_id:
            continue
        normalized.append({"id": model_id, "name": model.get("name") or model_id})
    return normalized


def normalize_sse_chunks(chunks):
    for chunk in chunks:
        if chunk is None:
            continue
        text = chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
        lines = [line.strip("\r") for line in text.splitlines() if line.strip()]
        if not lines and text.strip():
            lines = [text.strip()]
        for line in lines:
            if line.startswith(("data:", "event:", "id:", "retry:")):
                yield f"{line}\n"
            else:
                yield f"data: {line}\n"
        if lines:
            yield "\n"


def build_chat_payload(payload, stream):
    message = (payload.get("message") or "").strip()
    if not message:
        return None, (jsonify({"error": "消息不能为空"}), 400)
    referenced = unique_non_empty(payload.get("referenced_meeting_nos", []))
    page_meeting_nos = scoped_page_meeting_nos(payload)
    current_meeting_no = (payload.get("current_meeting_no") or "").strip()
    current_topic_id = int(payload.get("current_topic_id") or 0)
    if len(referenced) > MAX_REFERENCED_MEETINGS:
        return None, (jsonify({"error": "最多引用 5 个会议"}), 400)

    context_meeting_nos = []
    if current_meeting_no:
        context_meeting_nos.append(current_meeting_no)
    elif page_meeting_nos:
        context_meeting_nos.extend(page_meeting_nos)
    context_meeting_nos.extend(referenced)
    context_meetings = meeting_contexts(unique_non_empty(context_meeting_nos), viewer=current_user)
    retrieved_material_chunks = requested_attachment_contexts(message, context_meetings, current_topic_id)

    model = payload.get("model") or current_app.config.get("COPILOT_DEFAULT_MODEL")
    if not model:
        return None, (jsonify({"error": "请选择智枢模型"}), 400)

    chat_payload = {
        "model": model,
        "stream": stream,
        "messages": [
            {"role": "system", "content": build_system_context(context_meetings, retrieved_material_chunks)},
            {"role": "user", "content": message},
        ],
        "params": {
            "function_calling": current_app.config.get("COPILOT_FUNCTION_CALLING", "default"),
        },
    }
    tool_ids = configured_tool_ids()
    if tool_ids:
        chat_payload["tool_ids"] = tool_ids
    return chat_payload, None


def extract_chat_answer(data):
    if not isinstance(data, dict):
        return str(data or "")
    choices = data.get("choices") or []
    if choices:
        first = choices[0] or {}
        message = first.get("message") or {}
        delta = first.get("delta") or {}
        if message.get("content"):
            return message["content"]
        if delta.get("content"):
            return delta["content"]
        if first.get("text"):
            return first["text"]
    if data.get("content"):
        return data["content"]
    if data.get("answer"):
        return data["answer"]
    return json.dumps(data, ensure_ascii=False)


def configured_tool_ids():
    tool_ids = current_app.config.get("COPILOT_TOOL_IDS") or []
    if isinstance(tool_ids, str):
        return unique_non_empty(tool_ids.split(","))
    return unique_non_empty(tool_ids)


def unique_non_empty(values):
    unique = []
    for value in values:
        value = str(value or "").strip()
        if value and value not in unique:
            unique.append(value)
    return unique


def scoped_page_meeting_nos(payload):
    page_context = (payload.get("page_context") or "").strip()
    if page_context != "meeting_list":
        return []
    return unique_non_empty(payload.get("page_meeting_nos", []))[:MAX_PAGE_MEETINGS]


def search_meetings(query):
    meeting_query = Meeting.query
    if query:
        meeting_query = (
            meeting_query.outerjoin(Topic, Meeting.id == Topic.meeting_id)
            .filter(
                db.or_(
                    Meeting.meeting_no.like(f"%{query}%"),
                    Meeting.title.like(f"%{query}%"),
                    Topic.title.like(f"%{query}%"),
                )
            )
            .distinct()
        )
    return meeting_query.order_by(Meeting.meeting_date.desc(), Meeting.updated_at.desc()).limit(10).all()


def meeting_search_item(meeting):
    return {
        "meeting_no": meeting.meeting_no,
        "title": meeting.title,
        "meeting_date": serialize_value(meeting.meeting_date),
        "status": meeting.status,
        "topic_count": meeting.topics.count(),
    }


def meeting_contexts(meeting_nos, viewer=None):
    meetings = Meeting.query.filter(Meeting.meeting_no.in_(meeting_nos)).all() if meeting_nos else []
    by_no = {meeting.meeting_no: meeting for meeting in meetings}
    return [meeting_context(by_no[meeting_no], viewer=viewer) for meeting_no in meeting_nos if meeting_no in by_no]


def meeting_context(meeting, viewer=None):
    return {
        "meeting_no": meeting.meeting_no,
        "title": meeting.title,
        "meeting_date": serialize_value(meeting.meeting_date),
        "location": meeting.location or "",
        "host": meeting.host or "",
        "status": meeting.status,
        "topics": [topic_context(topic) for topic in context_topics_for_meeting(meeting, viewer)],
        "minutes": minutes_context(meeting.minutes),
    }


def context_topics_for_meeting(meeting, viewer=None):
    if viewer is None or getattr(viewer, "role", "") == "admin" or meeting.host_user_id == getattr(viewer, "id", None):
        return meeting.topics.order_by(Topic.present_order.asc()).all()
    return (
        Topic.query.filter_by(
            meeting_id=meeting.id,
            created_by=viewer.id,
            workflow_status="approved",
        )
        .order_by(Topic.present_order.asc())
        .all()
    )


def topic_context(topic):
    return {
        "id": topic.id,
        "title": topic.title,
        "category": topic.category or "",
        "plan_version": topic.plan_version or "",
        "owner": topic.owner or "",
        "background": topic.background or "",
        "purpose": topic.purpose or "",
        "duration_minutes": topic.duration_minutes,
        "present_order": topic.present_order,
        "status": topic.status,
        "attachments": [attachment_context(attachment) for attachment in topic.attachments],
    }


def attachment_context(attachment):
    return {
        "id": attachment.id,
        "filename": attachment.original_filename,
        "file_type": attachment.effective_file_type,
        "file_size": attachment.file_size,
    }


def requested_attachment_contexts(message, context_meetings, current_topic_id=0):
    if not message_requests_attachment_text(message):
        return []

    current_topic_ids = []
    if current_topic_id:
        current_topic_ids.append(current_topic_id)

    results = []
    for meeting_value in context_meetings:
        meeting = Meeting.query.filter_by(meeting_no=meeting_value["meeting_no"]).first()
        if not meeting:
            continue
        visible_topic_ids = [
            topic["id"]
            for topic in meeting_value.get("topics", [])
            if topic.get("id") and (not current_topic_ids or topic["id"] in current_topic_ids)
        ]
        results.extend(
            retrieve_meeting_material_chunks(
                meeting,
                message,
                source="copilot",
                visible_topic_ids=visible_topic_ids,
            )
        )

    if results:
        return results

    topic_ids = current_topic_ids or [
        topic["id"]
        for meeting in context_meetings
        for topic in meeting.get("topics", [])
        if topic.get("id")
    ]
    if not topic_ids:
        return []

    attachments = Attachment.query.filter(Attachment.topic_id.in_(topic_ids)).all()
    matched = [attachment for attachment in attachments if attachment_matches_message(attachment, message)]
    selected = matched or attachments
    return [
        {
            "attachment_id": attachment.id,
            "filename": attachment.original_filename,
            "topic_id": attachment.topic_id,
            "topic_title": attachment.topic.title,
            "index_status": attachment.material_document.status if attachment.material_document else "not_indexed",
            "warning": attachment.material_document.error_message if attachment.material_document else "该附件未进入材料知识库。",
        }
        for attachment in selected
    ]


def minutes_context(minutes):
    if not minutes:
        return {"summary": "", "decisions": "", "action_items": ""}
    return {
        "summary": minutes.summary or "",
        "decisions": minutes.decisions or "",
        "action_items": minutes.action_items or "",
    }


def build_system_context(context_meetings, retrieved_material_chunks=None):
    retrieved_material_chunks = retrieved_material_chunks or []
    if not context_meetings and not retrieved_material_chunks:
        return "You are the QBP Meeting MGMT Copilot. Answer in Chinese unless the user asks otherwise."
    context = {"meetings": context_meetings}
    attachment_note = (
        "Only attachment metadata is available unless retrieved_material_chunks is included. "
        "For attachment/material claims, answer only from retrieved chunks and cite filename plus source_label."
    )
    if retrieved_material_chunks:
        context["retrieved_material_chunks"] = retrieved_material_chunks
        attachment_note = (
            "Attachment/material evidence is included under retrieved_material_chunks. "
            "Use only these chunks for material claims, cite filename plus source_label, "
            "and say 未在已索引材料中找到 when evidence is missing."
        )
    return (
        "You are the QBP Meeting MGMT Copilot. Use the following meeting context. "
        f"{attachment_note}\n\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def require_tool_token():
    expected = current_app.config.get("QBP_TOOL_SERVER_TOKEN")
    header = request.headers.get("Authorization", "")
    if not expected or header != f"Bearer {expected}":
        abort(401)


def serialize_value(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def tool_openapi_spec():
    base_url = current_app.config["QBP_PUBLIC_BASE_URL"]
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "QBP Meeting MGMT Tool Server",
            "version": "1.0.0",
            "description": "Read QBP meetings, topics, minutes, and attachment text for Copilot context.",
        },
        "servers": [{"url": base_url}],
        "security": [{"ToolBearer": []}],
        "components": {
            "securitySchemes": {
                "ToolBearer": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "QBP_TOOL_SERVER_TOKEN",
                }
            }
        },
        "paths": {
            "/copilot/tools/meetings/search": {
                "get": {
                    "operationId": "search_qbp_meetings",
                    "summary": "Search QBP meetings",
                    "parameters": [
                        {"name": "q", "in": "query", "schema": {"type": "string"}, "required": False}
                    ],
                    "responses": {"200": {"description": "Meeting search results"}},
                }
            },
            "/copilot/tools/meetings/{meeting_no}": {
                "get": {
                    "operationId": "get_qbp_meeting",
                    "summary": "Get one QBP meeting with topics and minutes",
                    "parameters": [
                        {"name": "meeting_no", "in": "path", "schema": {"type": "string"}, "required": True}
                    ],
                    "responses": {"200": {"description": "Meeting context"}},
                }
            },
            "/copilot/tools/topics/{topic_id}": {
                "get": {
                    "operationId": "get_qbp_topic",
                    "summary": "Get one QBP meeting topic",
                    "parameters": [
                        {"name": "topic_id", "in": "path", "schema": {"type": "integer"}, "required": True}
                    ],
                    "responses": {"200": {"description": "Agenda context"}},
                }
            },
            "/copilot/tools/attachments/{attachment_id}/content": {
                "get": {
                    "operationId": "get_qbp_attachment_content",
                    "summary": "Extract readable text from one QBP meeting attachment",
                    "parameters": [
                        {"name": "attachment_id", "in": "path", "schema": {"type": "integer"}, "required": True}
                    ],
                    "responses": {"200": {"description": "Attachment metadata plus extracted text when supported"}},
                }
            },
            "/copilot/tools/meetings/{meeting_no}/minutes": {
                "get": {
                    "operationId": "get_qbp_meeting_minutes",
                    "summary": "Get QBP meeting minutes",
                    "parameters": [
                        {"name": "meeting_no", "in": "path", "schema": {"type": "string"}, "required": True}
                    ],
                    "responses": {"200": {"description": "Meeting minutes"}},
                }
            },
        },
    }
