import json
import zipfile
from pathlib import Path

import pytest

from backend import models
from backend.app import create_app
from backend.copilot import ZhishuClient
from backend.models import Attachment, MaterialChunk, MaterialDocument, Meeting, Topic, User, db


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeZhishuClient:
    def __init__(self):
        self.last_payload = None

    def list_models(self):
        return [
            {"id": "qbp-agent", "name": "QBP Agent"},
            {"id": "general-model"},
        ]

    def stream_chat(self, payload):
        self.last_payload = payload
        yield 'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n'
        yield 'data: [DONE]\n\n'

    def chat(self, payload):
        self.last_payload = payload
        return {"choices": [{"message": {"content": "Hello full chat"}}]}


def test_zhishu_client_chat_uses_three_minute_timeout(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers=None, json=None, timeout=None, **_kwargs):
        captured["url"] = url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("backend.copilot.requests.post", fake_post)

    client = ZhishuClient("http://zhishu.local", "sk-test")
    client.chat({"model": "review-model", "messages": []})

    assert captured["url"] == "http://zhishu.local/api/v1/chat/completions"
    assert captured["timeout"] == 180


@pytest.fixture()
def app(tmp_path):
    fake_client = FakeZhishuClient()
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
            "POWERPOINT_PREVIEW_FOLDER": tmp_path / "previews",
            "ZHISHU_API_KEY": "sk-test",
            "ZHISHU_CLIENT": fake_client,
            "COPILOT_DEFAULT_MODEL": "qbp-agent",
            "COPILOT_TOOL_IDS": ["server:qbp_meeting_mgmt"],
            "QBP_PUBLIC_BASE_URL": "http://qbp.local",
            "QBP_TOOL_SERVER_TOKEN": "tool-secret",
            "WTF_CSRF_ENABLED": False,
        }
    )
    with app.app_context():
        db.create_all()
        User.create_default_admin()
        Meeting.seed_demo()
        meeting = Meeting(
            meeting_no="CM20260002",
            title="Next QBP Meeting",
            meeting_date=Meeting.query.first().meeting_date,
            location="Room B",
            host="Procurement",
            status="draft",
            created_by=1,
        )
        db.session.add(meeting)
        db.session.flush()
        db.session.add(
            Topic(
                meeting_id=meeting.id,
                title="Equipment Renewal",
                category="Equipment",
                owner="Equipment Team",
                present_order=1,
                background="Legacy tools need renewal",
                purpose="Confirm sourcing direction",
            )
        )
        db.session.commit()
    app.fake_client = fake_client
    yield app


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client):
    return client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )


def create_user(username, password="user123", role="user"):
    user = User(username=username, display_name=username.title(), role=role, enabled=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def login_as(client, username, password="user123"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def auth_headers():
    return {"Authorization": "Bearer tool-secret"}


def test_copilot_models_proxy_requires_login_and_returns_models(client):
    anonymous = client.get("/copilot/models")
    assert anonymous.status_code == 302

    login(client)
    response = client.get("/copilot/models")

    assert response.status_code == 200
    assert response.get_json()["default_model"] == "qbp-agent"
    assert response.get_json()["models"][0]["id"] == "qbp-agent"


def test_copilot_context_search_matches_meeting_and_topic(client):
    login(client)

    response = client.get("/copilot/context/search?q=Equipment")

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert results[0]["meeting_no"] == "CM20260002"
    assert results[0]["topic_count"] == 1


def test_copilot_chat_stream_injects_referenced_and_current_meetings(client, app):
    login(client)
    with app.app_context():
        current = Meeting.query.order_by(Meeting.id).first()

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "Compare risks",
            "model": "qbp-agent",
            "current_meeting_no": current.meeting_no,
            "referenced_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    assert "Hello" in response.get_data(as_text=True)
    payload = app.fake_client.last_payload
    assert payload["model"] == "qbp-agent"
    assert payload["tool_ids"] == ["server:qbp_meeting_mgmt"]
    system_content = payload["messages"][0]["content"]
    assert current.meeting_no in system_content
    assert "CM20260002" in system_content
    assert "Equipment Renewal" in system_content


def test_copilot_chat_json_endpoint_supports_ie_fallback(client, app):
    login(client)
    with app.app_context():
        current = Meeting.query.order_by(Meeting.id).first()

    response = client.post(
        "/copilot/chat",
        json={
            "message": "IE fallback",
            "model": "qbp-agent",
            "current_meeting_no": current.meeting_no,
            "referenced_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["answer"] == "Hello full chat"
    assert data["model"] == "qbp-agent"
    assert current.meeting_no in app.fake_client.last_payload["messages"][0]["content"]


def test_copilot_chat_stream_injects_visible_page_meetings(client, app):
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "本周都有哪些会议",
            "model": "qbp-agent",
            "page_context": "meeting_list",
            "page_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "CM20260002" in system_content
    assert "Next QBP Meeting" in system_content
    assert "Equipment Renewal" in system_content


def test_copilot_chat_stream_ignores_page_meetings_without_meeting_list_context(client, app):
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "议题池里问一个普通问题",
            "model": "qbp-agent",
            "page_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "CM20260002" not in system_content
    assert "Next QBP Meeting" not in system_content
    assert "Equipment Renewal" not in system_content


def test_copilot_context_filters_other_users_topics_for_normal_user(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = Meeting.query.filter_by(meeting_no="CM20260002").one()
        meeting_no = meeting.meeting_no
        db.session.add(
            Topic(
                meeting_id=meeting.id,
                title="Alice Visible Topic",
                category="Service",
                owner="Alice",
                present_order=2,
                background="Alice visible background",
                created_by=alice.id,
                workflow_status="approved",
            )
        )
        db.session.commit()

    login_as(client, "alice")
    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "summarize meeting",
            "model": "qbp-agent",
            "referenced_meeting_nos": [meeting_no],
        },
    )

    assert response.status_code == 200
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "Alice Visible Topic" in system_content
    assert "Alice visible background" in system_content
    assert "Equipment Renewal" not in system_content
    assert "Legacy tools need renewal" not in system_content


def test_copilot_attachment_text_filters_other_users_topics_for_normal_user(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = Meeting.query.filter_by(meeting_no="CM20260002").one()
        hidden_topic = Topic.query.filter_by(title="Equipment Renewal").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(hidden_topic.meeting_id) / str(hidden_topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        create_pptx(upload_dir / "hidden-equipment.pptx", ["Hidden equipment attachment text"])
        hidden_attachment = Attachment(
            topic_id=hidden_topic.id,
            original_filename="hidden-equipment.pptx",
            stored_filename="hidden-equipment.pptx",
            file_type="pptx",
            file_size=1234,
            uploaded_by=1,
        )
        db.session.add(hidden_attachment)
        db.session.flush()
        hidden_document = MaterialDocument(
            attachment_id=hidden_attachment.id,
            topic_id=hidden_topic.id,
            meeting_id=hidden_topic.meeting_id,
            status="indexed",
            chunk_count=1,
        )
        db.session.add(hidden_document)
        db.session.flush()
        db.session.add(
            MaterialChunk(
                document_id=hidden_document.id,
                attachment_id=hidden_attachment.id,
                topic_id=hidden_topic.id,
                meeting_id=hidden_topic.meeting_id,
                chunk_index=1,
                source_label="Slide 1",
                text="Slide 1: Hidden equipment attachment text",
                text_hash="hidden-hash",
                char_count=37,
            )
        )
        visible_topic = Topic(
            meeting_id=meeting.id,
            title="Alice Visible Topic",
            category="Service",
            owner="Alice",
            present_order=2,
            background="Alice visible background",
            created_by=alice.id,
            workflow_status="approved",
        )
        db.session.add(visible_topic)
        db.session.flush()
        visible_dir = app.config["UPLOAD_FOLDER"] / str(meeting.id) / str(visible_topic.id)
        visible_dir.mkdir(parents=True, exist_ok=True)
        create_pptx(visible_dir / "alice-visible.pptx", ["Alice visible attachment text"])
        visible_attachment = Attachment(
            topic_id=visible_topic.id,
            original_filename="alice-visible.pptx",
            stored_filename="alice-visible.pptx",
            file_type="pptx",
            file_size=1234,
            uploaded_by=alice.id,
        )
        db.session.add(visible_attachment)
        db.session.flush()
        visible_document = MaterialDocument(
            attachment_id=visible_attachment.id,
            topic_id=visible_topic.id,
            meeting_id=meeting.id,
            status="indexed",
            chunk_count=1,
        )
        db.session.add(visible_document)
        db.session.flush()
        db.session.add(
            MaterialChunk(
                document_id=visible_document.id,
                attachment_id=visible_attachment.id,
                topic_id=visible_topic.id,
                meeting_id=meeting.id,
                chunk_index=1,
                source_label="Slide 1",
                text="Slide 1: Alice visible attachment text",
                text_hash="visible-hash",
                char_count=36,
            )
        )
        db.session.commit()
    login_as(client, "alice")
    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "summarize PPT attachments",
            "model": "qbp-agent",
            "referenced_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    response.get_data(as_text=True)
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "Alice visible attachment text" in system_content
    assert "Hidden equipment attachment text" not in system_content


def test_copilot_chat_only_injects_attachment_text_when_requested(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="Equipment Renewal").one()
        topic_id = topic.id
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        create_pptx(upload_dir / "equipment-deck.pptx", ["Equipment deck risk summary"])
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="equipment-deck.pptx",
            stored_filename="equipment-deck.pptx",
            file_type="pptx",
            file_size=1234,
            uploaded_by=1,
        )
        db.session.add(attachment)
        db.session.flush()
        document = MaterialDocument(
            attachment_id=attachment.id,
            topic_id=topic.id,
            meeting_id=topic.meeting_id,
            status="indexed",
            chunk_count=1,
        )
        db.session.add(document)
        db.session.flush()
        db.session.add(
            MaterialChunk(
                document_id=document.id,
                attachment_id=attachment.id,
                topic_id=topic.id,
                meeting_id=topic.meeting_id,
                chunk_index=1,
                source_label="Slide 1",
                text="Slide 1: Equipment deck risk summary",
                text_hash="equipment-hash",
                char_count=36,
            )
        )
        db.session.commit()

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "总结这个会议",
            "model": "qbp-agent",
            "referenced_meeting_nos": ["CM20260002"],
        },
    )
    response.get_data(as_text=True)
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "attachment_texts" not in system_content

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "总结这个 PPT 附件",
            "model": "qbp-agent",
            "referenced_meeting_nos": ["CM20260002"],
            "current_topic_id": topic_id,
        },
    )
    response.get_data(as_text=True)
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "retrieved_material_chunks" in system_content
    assert "Equipment deck risk summary" in system_content


def test_copilot_attachment_rag_retrieval_uses_meeting_scope_without_cross_meeting_leak(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(meeting_no="CM20260002").one()
        first_topic = Topic.query.filter_by(title="Equipment Renewal").one()
        second_topic = Topic(
            meeting_id=meeting.id,
            title="Supplier Entry",
            category="POR Review",
            plan_version="Q3 26BP",
            owner="QA",
            present_order=2,
            background="Need evaluate supplier",
            purpose="Approve entry",
        )
        other_meeting = Meeting(
            meeting_no="CM20269999",
            title="Other Meeting",
            meeting_date=meeting.meeting_date,
            location="Elsewhere",
            host="Other",
            status="draft",
            created_by=1,
        )
        db.session.add_all([second_topic, other_meeting])
        db.session.flush()
        other_topic = Topic(
            meeting_id=other_meeting.id,
            title="Leaky Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            present_order=1,
        )
        db.session.add(other_topic)
        db.session.flush()
        for topic, filename, text_value in (
            (first_topic, "meeting-scope-1.pptx", "同会议第一个议题的附件证据"),
            (second_topic, "meeting-scope-2.pptx", "同会议第二个议题的附件证据"),
            (other_topic, "other-meeting.pptx", "其他会议附件不能出现"),
        ):
            attachment = Attachment(
                topic_id=topic.id,
                original_filename=filename,
                stored_filename=filename,
                file_type="pptx",
                file_size=128,
                uploaded_by=1,
            )
            db.session.add(attachment)
            db.session.flush()
            document = MaterialDocument(
                attachment_id=attachment.id,
                topic_id=topic.id,
                meeting_id=topic.meeting_id,
                status="indexed",
                chunk_count=1,
            )
            db.session.add(document)
            db.session.flush()
            db.session.add(
                MaterialChunk(
                    document_id=document.id,
                    attachment_id=attachment.id,
                    topic_id=topic.id,
                    meeting_id=topic.meeting_id,
                    chunk_index=1,
                    source_label="Slide 1",
                    text=f"Slide 1: {text_value}",
                    text_hash=f"hash-{topic.id}",
                    char_count=len(text_value),
                )
            )
        db.session.commit()

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "总结这个会议的 PPT 附件材料",
            "model": "qbp-agent",
            "referenced_meeting_nos": ["CM20260002"],
        },
    )

    assert response.status_code == 200
    system_content = app.fake_client.last_payload["messages"][0]["content"]
    assert "retrieved_material_chunks" in system_content
    assert "attachment_texts" not in system_content
    assert "同会议第一个议题的附件证据" in system_content
    assert "同会议第二个议题的附件证据" in system_content
    assert "其他会议附件不能出现" not in system_content


def test_copilot_chat_rejects_blank_message(client):
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={"message": "   ", "model": "qbp-agent"},
    )

    assert response.status_code == 400
    assert "消息不能为空" in response.get_json()["error"]


def test_copilot_chat_parses_tool_ids_string_config(client, app):
    app.config["COPILOT_TOOL_IDS"] = "server:qbp_meeting_mgmt, server:qbp_extra"
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={"message": "hello", "model": "qbp-agent"},
    )

    assert response.status_code == 200
    assert app.fake_client.last_payload["tool_ids"] == [
        "server:qbp_meeting_mgmt",
        "server:qbp_extra",
    ]


def test_copilot_chat_stream_normalizes_plain_json_lines(client, app):
    class PlainJsonClient(FakeZhishuClient):
        def stream_chat(self, payload):
            self.last_payload = payload
            yield '{"choices":[{"delta":{"content":"Plain JSON works"}}]}'
            yield '[DONE]'

    app.config["ZHISHU_CLIENT"] = PlainJsonClient()
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={"message": "hello", "model": "qbp-agent"},
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'data: {"choices"' in body
    assert "Plain JSON works" in body


def test_copilot_models_returns_json_error_when_zhishu_fails(client, app):
    class FailingClient:
        def list_models(self):
            raise RuntimeError("zhishu offline")

    app.config["ZHISHU_CLIENT"] = FailingClient()
    login(client)

    response = client.get("/copilot/models")

    assert response.status_code == 502
    assert "zhishu offline" in response.get_json()["error"]


def test_copilot_status_exposes_safe_runtime_config(client):
    anonymous = client.get("/copilot/status")
    assert anonymous.status_code == 302

    login(client)
    response = client.get("/copilot/status")

    assert response.status_code == 200
    data = response.get_json()
    assert data["api_key_configured"] is True
    assert data["tool_server_configured"] is True
    assert data["tool_ids"] == ["server:qbp_meeting_mgmt"]
    assert "api_key" not in data


def test_copilot_chat_rejects_more_than_five_referenced_meetings(client):
    login(client)

    response = client.post(
        "/copilot/chat/stream",
        json={
            "message": "Too much context",
            "model": "qbp-agent",
            "referenced_meeting_nos": [f"CM{i}" for i in range(6)],
        },
    )

    assert response.status_code == 400
    assert "最多引用 5 个会议" in response.get_json()["error"]


def test_tool_server_openapi_requires_bearer_token(client):
    unauthorized = client.get("/copilot/tools/openapi.json")
    assert unauthorized.status_code == 401

    response = client.get("/copilot/tools/openapi.json", headers=auth_headers())

    assert response.status_code == 200
    spec = response.get_json()
    assert spec["openapi"].startswith("3.")
    assert spec["security"] == [{"ToolBearer": []}]
    assert spec["components"]["securitySchemes"]["ToolBearer"]["type"] == "http"
    operation_ids = {
        operation["operationId"]
        for path in spec["paths"].values()
        for operation in path.values()
    }
    assert "search_qbp_meetings" in operation_ids
    assert "get_qbp_attachment_content" in operation_ids
    assert "propose_minutes_update" not in operation_ids
    assert "propose_topic_create" not in operation_ids


def test_tool_server_can_read_meeting_context(client, app):
    with app.app_context():
        meeting = Meeting.query.order_by(Meeting.id).first()

    response = client.get(
        f"/copilot/tools/meetings/{meeting.meeting_no}",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["meeting_no"] == meeting.meeting_no
    assert len(data["topics"]) == 3


def test_tool_server_can_extract_attachment_content(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="Equipment Renewal").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        create_pptx(upload_dir / "equipment-tool.pptx", ["Tool server attachment content"])
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="equipment-tool.pptx",
            stored_filename="equipment-tool.pptx",
            file_type="pptx",
            file_size=1234,
            uploaded_by=1,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    response = client.get(
        f"/copilot/tools/attachments/{attachment_id}/content",
        headers=auth_headers(),
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["filename"] == "equipment-tool.pptx"
    assert "Tool server attachment content" in data["text"]


def test_copilot_proposal_write_routes_are_removed(client, app):
    with app.app_context():
        meeting = Meeting.query.order_by(Meeting.id).first()
        meeting_no = meeting.meeting_no

    tool_response = client.post(
        "/copilot/tools/proposals/minutes-update",
        headers=auth_headers(),
        json={
            "meeting_no": meeting_no,
            "summary": "AI generated summary",
            "decisions": "AI generated decisions",
            "action_items": "AI generated actions",
            "meeting_status": "completed",
        },
    )
    assert tool_response.status_code == 404

    login(client)
    list_response = client.get("/copilot/proposals")
    apply_response = client.post("/copilot/proposals/1/apply")
    assert list_response.status_code == 404
    assert apply_response.status_code == 404

    with app.app_context():
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).one()
        assert meeting.minutes is None


def test_base_template_includes_copilot_widget_for_logged_in_pages(client):
    login(client)

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert 'id="ai-copilot-launcher"' not in html
    assert 'data-copilot-open' in html
    assert "copilot-header-trigger" in html
    assert "> Copilot" in html
    assert 'data-copilot-current-meeting=""' in html
    assert "ai-copilot-shell" in html
    assert 'id="ai-copilot-status"' in html
    assert 'id="ai-copilot-empty"' in html
    assert 'id="ai-copilot-clear"' in html
    assert 'id="ai-copilot-refresh-proposals"' not in html
    assert 'id="ai-copilot-proposals"' not in html
    assert "生成提案" not in html
    assert "当前环境" in html
    assert 'data-copilot-page-meetings=' in html
    assert "CM20260001" in html
    assert "ai-copilot-quick-prompts" in html
    assert "copilot.js" in html
    assert "local-icons.css" in html
    assert "cdnjs" not in html


def test_meeting_detail_exposes_copilot_trigger_and_current_meeting_context(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.order_by(Meeting.id).first()

    response = client.get(f"/meetings/{meeting.meeting_no}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'class="btn btn-outline copilot-header-trigger"' in html
    assert 'data-copilot-open' in html
    assert f'data-copilot-current-meeting="{meeting.meeting_no}"' in html


def test_topic_drafts_page_does_not_seed_copilot_meeting_context(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting.query.filter_by(meeting_no="CM20260002").one()
        db.session.add(
            Topic(
                requested_meeting_id=meeting.id,
                title="Draft With Requested Meeting",
                category="Service",
                owner="Admin",
                workflow_status="draft",
                created_by=admin.id,
            )
        )
        db.session.commit()

    response = client.get("/topics/drafts")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Draft With Requested Meeting" in html
    assert 'data-copilot-page-meetings=' in html
    assert "CM20260002" not in html[html.index('id="ai-copilot"') : html.index('id="ai-copilot-panel"')]
    assert 'data-copilot-page-key="topic_drafts"' in html


def test_copilot_frontend_supports_bare_at_meeting_search():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")

    assert "var query = mention.query;" in script
    assert "query.length === 0" in script


def test_copilot_frontend_persists_session_history():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")

    assert "sessionStorage" in script
    assert "restoreMessages" in script
    assert "persistMessages" in script


def test_copilot_open_button_uses_event_delegation_for_soft_navigation():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")

    assert 'document, "click", function (event)' in script
    assert 'closest("[data-copilot-open]")' in script
    assert "openPanel();" in script


def test_copilot_frontend_removes_proposal_panel_behavior():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")
    stylesheet = (PROJECT_ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")

    assert "loadProposals" not in script
    assert "/copilot/proposals" not in script
    assert "暂无待确认提案" not in script
    assert "刷新提案失败" not in script
    assert ".ai-proposal" not in stylesheet
    assert "ai-copilot-proposals" not in template
    assert "ai-copilot-refresh-proposals" not in template
    assert "当前环境" in template


def test_copilot_frontend_renders_assistant_markdown():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")

    assert "renderAssistantMessage" in script
    assert "renderMarkdown" in script
    assert "renderMarkdownTable" in script
    assert "setRawContent(node" in script
    assert "getRawContent(target" in script


def test_copilot_frontend_sends_visible_page_meetings():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")

    assert "pageMeetingNos" in script
    assert "page_meeting_nos: pageMeetingNos" in script
    assert "page_context: pageContext" in script
    assert "pageKey" in script
    assert "data-copilot-page-context" in template
    assert "data-copilot-page-key" in template
    assert "本页会议" in script


def test_copilot_frontend_sends_current_topic_id():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")

    assert "currentTopic" in script
    assert "current_topic_id: currentTopic" in script
    assert "data-copilot-current-topic" in template


def test_copilot_frontend_checks_status_before_loading_models():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")
    template = (PROJECT_ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'xhrJson("GET", "/copilot/status"' in script
    assert "api_key_configured" in script
    assert "sendButton.disabled = true" in script
    assert 'rel="icon"' in template


def test_copilot_frontend_is_ie11_parse_safe_and_has_json_fallback():
    script = (PROJECT_ROOT / "frontend" / "static" / "js" / "copilot.js").read_text(encoding="utf-8")
    forbidden = ["const ", "let ", "=>", "`", "async function", "await ", "?.", "fetch(", "ReadableStream", ".dataset"]

    for token in forbidden:
        assert token not in script
    assert 'xhrJson("POST", "/copilot/chat"' in script
    assert 'xhrStream("/copilot/chat/stream"' in script
    assert "supportsStreaming()" in script


def test_offline_svg_icon_system_replaces_fontawesome_markup():
    template = (PROJECT_ROOT / "frontend" / "templates" / "base.html").read_text(encoding="utf-8")
    icons = (PROJECT_ROOT / "frontend" / "static" / "css" / "local-icons.css").read_text(encoding="utf-8")

    assert "cdnjs" not in template
    assert "https://" not in template
    assert "local-icons.css" in template
    assert "fa-wand-magic-sparkles" not in template
    assert ".svg-icon" in icons
    assert ".icon-copilot" in icons


def test_copilot_css_uses_responsive_polished_panel():
    stylesheet = (PROJECT_ROOT / "frontend" / "static" / "css" / "app.css").read_text(encoding="utf-8")

    assert "width: min(460px, calc(100vw - 32px));" in stylesheet
    assert ".ai-copilot-panel" in stylesheet
    assert ".ai-message-markdown table" in stylesheet
    assert ".ai-message-table-wrap" in stylesheet


def create_pptx(path, slide_texts):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "")
        for index, text in enumerate(slide_texts, start=1):
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                (
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f"<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
                    "</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
                ),
            )
