import base64
import io
import json
import re
import sqlite3
import subprocess
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from sqlalchemy import event, text

from backend.app import build_ai_material_review, create_app, meeting_readiness_summary, topic_completeness, topic_readiness
from backend.material_rag import (
    ZhishuKnowledgeClient,
    material_embedding_client,
    parse_embedding_response,
    retrieve_topic_material_chunks,
    sanitized_zhishu_metadata,
    zhishu_knowledge_client,
)
from backend.models import (
    AI_REVIEW_PROMPT_KEY,
    AIKnowHow,
    AIKnowHowCategory,
    AIPrompt,
    AppConfig,
    Attachment,
    AuditLog,
    Group,
    Meeting,
    MeetingFavorite,
    MeetingMinutes,
    MaterialChunk,
    MaterialDocument,
    MaterialRetrievalLog,
    Topic,
    PlanRound,
    PlanVersion,
    TopicMaterialReview,
    TopicShare,
    User,
    db,
)


@pytest.fixture()
def app(tmp_path):
    def fake_powerpoint_converter(source_path, output_dir):
        output_path = output_dir / f"{source_path.stem}.pdf"
        output_path.write_bytes(b"%PDF-1.4 converted from powerpoint")
        return output_path

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
            "POWERPOINT_PREVIEW_FOLDER": tmp_path / "previews",
            "POWERPOINT_CONVERTER": fake_powerpoint_converter,
            "KKFILEVIEW_BASE_URL": "http://kk.example:8012",
            "QBP_FILEVIEW_BASE_URL": "http://qbp.example:5008",
            "FILEVIEW_TOKEN_TTL_SECONDS": 300,
            "WTF_CSRF_ENABLED": False,
        }
    )
    with app.app_context():
        db.create_all()
        User.create_default_admin()
        Meeting.seed_demo()
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


def create_user(username, password="user123", role="user", enabled=True, display_name=None, group_id=None):
    user = User(
        username=username,
        display_name=display_name or username.title(),
        role=role,
        enabled=enabled,
        group_id=group_id,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def login_as(client, username, password="user123", follow_redirects=True):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=follow_redirects,
    )


def decode_kk_preview_source(location):
    query = parse_qs(urlsplit(location).query)
    encoded = query["url"][0]
    return base64.b64decode(unquote(encoded)).decode("utf-8")


def demo_meeting():
    return Meeting.query.order_by(Meeting.meeting_date.asc(), Meeting.id.asc()).first()


def test_login_reaches_meeting_list(client):
    response = login(client)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<title>会议列表 - QBP Meeting MGMT</title>" in html
    assert "会议列表" in html
    assert "全部会议列表" not in html
    assert "Q3 26BP POR Review" in html


def test_login_page_hides_demo_credentials(client):
    response = client.get("/auth/login")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "测试账号" not in html
    assert "admin123" not in html


def test_authenticated_layout_enables_soft_navigation(client):
    login(client)

    response = client.get("/admin")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "static/js/app-shell.js" in html
    assert 'data-soft-nav-root' in html
    assert 'data-soft-nav-sidebar' in html


def test_user_facing_copy_uses_issue_term_instead_of_agenda_term():
    forbidden = "\u8bae\u7a0b"
    checked_suffixes = {".html", ".py", ".js", ".md"}
    tracked_files = subprocess.check_output(["git", "ls-files"], text=True, encoding="utf-8").splitlines()
    offenders = []

    for relative_path in tracked_files:
        path = Path(relative_path)
        if path.suffix not in checked_suffixes:
            continue
        if any(part in {"venv", "uploads", "__pycache__"} for part in path.parts):
            continue
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if forbidden in text:
            offenders.append(relative_path)

    assert offenders == []


def test_new_qbp_repo_does_not_track_rebrand_or_historical_report_docs():
    docs_dir = Path("docs")
    process_docs_dir = "super" + "powers"
    recent_summary = "recent-work-" + "summary.md"
    weekly_report_pattern = "weekly-" + "report-*.md"

    assert not (docs_dir / process_docs_dir).exists()
    assert not (docs_dir / recent_summary).exists()
    assert not list(docs_dir.glob(weekly_report_pattern))


def test_seed_meeting_has_three_procurement_topics(app):
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()
        topics = [topic.title for topic in meeting.topics]

    assert topics == ["OP Cum Yields", "NPP New Product CS时间", "BE IE产能扩建"]


def test_meeting_detail_preloads_selected_topic_data(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20269991",
            title="Query Budget Meeting",
            meeting_date=datetime(2026, 6, 2).date(),
            location="PMD 531",
            host="PLN/BP",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
        )
        db.session.add(meeting)
        db.session.flush()
        selected_topic_id = None
        for order in range(1, 6):
            topic = Topic(
                meeting_id=meeting.id,
                title=f"Budget Topic {order}",
                category="Kick Off",
                plan_version="Q2 27BP",
                owner="Buyer",
                background="Background",
                purpose="Purpose",
                present_order=order,
                status="pending",
                workflow_status="approved",
                created_by=admin.id,
            )
            db.session.add(topic)
            db.session.flush()
            db.session.add(
                Attachment(
                    topic_id=topic.id,
                    original_filename=f"material-{order}.pdf",
                    stored_filename=f"material-{order}.pdf",
                    file_type="pdf",
                    file_size=1024,
                    uploaded_by=admin.id,
                )
            )
            db.session.add(
                TopicMaterialReview(
                    topic_id=topic.id,
                    source="hoster",
                    result="approved",
                    score=90,
                    summary=f"Review {order}",
                    reviewed_by=admin.id,
                )
            )
            if order == 3:
                selected_topic_id = topic.id
        db.session.commit()
        engine = db.engine

    statements = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        response = client.get(f"/meetings/CM20269991?topic_id={selected_topic_id}")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)

    assert response.status_code == 200
    assert "Budget Topic 3" in response.get_data(as_text=True)
    assert len(statements) <= 30


def test_create_meeting_with_three_topics(client, app):
    login(client)

    response = client.post(
        "/meetings/create",
        data={
            "title": "下周四 QBP Meeting",
            "meeting_date": "2026-06-04",
            "location": "上海会议室 A",
            "host": "PLN/BP",
            "status": "preparing",
            "topic_title[]": ["包装采购", "物流服务", "市场物料"],
            "topic_category[]": ["服务", "服务", "物料"],
            "topic_owner[]": ["Alice", "Bob", "Cindy"],
            "topic_order[]": ["1", "2", "3"],
            "topic_duration_minutes[]": ["10", "", "500"],
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "下周四 QBP Meeting" in response.get_data(as_text=True)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="下周四 QBP Meeting").one()
        assert meeting.topics.count() == 3
        durations = [
            topic.duration_minutes
            for topic in meeting.topics.order_by(Topic.present_order.asc()).all()
        ]
        assert durations == [10, 15, 180]


def test_meeting_create_page_does_not_require_default_topics(client):
    login(client)

    response = client.get("/meetings/create")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "议题列表" not in html
    assert "可选议题" in html
    assert "议题可以稍后再添加" not in html
    assert "没有议题也可以直接创建会议" not in html
    assert '<div class="topic-row">' not in html
    assert "range(3)" not in html
    assert 'name="topic_title[]" placeholder="议题标题" required' not in html


def test_create_meeting_without_topics(client, app):
    login(client)

    response = client.post(
        "/meetings/create",
        data={
            "title": "无议题 QBP Meeting",
            "meeting_date": "2026-06-12",
            "location": "PMD 531",
            "host": "PLN/BP",
            "status": "preparing",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "无议题 QBP Meeting" in response.get_data(as_text=True)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="无议题 QBP Meeting").one()
        assert meeting.topics.count() == 0
        log = AuditLog.query.filter_by(action="create_meeting", target_id=meeting.id).one()
        assert log.metadata_json["topic_count"] == 0


def test_meeting_status_reporting_is_available_in_forms_filters_and_minutes(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()
        meeting_no = meeting.meeting_no
        admin_id = User.query.filter_by(username="admin").one().id

    create_page = client.get("/meetings/create")
    create_html = create_page.get_data(as_text=True)
    assert create_page.status_code == 200
    assert '<option value="reporting"' in create_html
    assert ">汇报中</option>" in create_html

    edit_response = client.post(
        f"/meetings/{meeting_no}/edit",
        data={
            "title": "Q3 26BP POR Review",
            "meeting_date": "2026-05-27",
            "location": "QBP War Room / Teams",
            "host": "PLN/BP",
            "status": "reporting",
            "host_user_id": str(admin_id),
        },
        follow_redirects=True,
    )
    edit_html = edit_response.get_data(as_text=True)
    assert edit_response.status_code == 200
    assert "会议信息已保存" in edit_html
    assert "汇报中" in edit_html

    list_html = client.get("/meetings?status=reporting").get_data(as_text=True)
    assert '<option value="reporting" selected>汇报中</option>' in list_html
    assert meeting_no in list_html

    detail_html = client.get(f"/meetings/{meeting_no}?view=minutes").get_data(as_text=True)
    assert '<option value="reporting" selected>汇报中</option>' in detail_html

    minutes_response = client.post(
        f"/meetings/{meeting_no}/minutes",
        data={
            "summary": "进入现场汇报。",
            "decisions": "",
            "action_items": "",
            "meeting_status": "reporting",
        },
        follow_redirects=True,
    )
    assert minutes_response.status_code == 200
    with app.app_context():
        assert Meeting.query.filter_by(meeting_no=meeting_no).one().status == "reporting"


def test_meeting_list_shows_info_fields_and_admin_edit_delete_actions(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert "会议信息" in html
    assert "创建时间" not in html
    assert "更新时间" in html
    assert "会议地址" in html
    assert meeting.location in html
    assert f'<a class="meeting-code-link" href="/meetings/{meeting.meeting_no}">{meeting.meeting_no}</a>' in html
    assert url_for_path(f"/meetings/{meeting.meeting_no}/edit") in html
    assert url_for_path(f"/meetings/{meeting.meeting_no}/delete") in html


def test_meeting_list_can_toggle_and_filter_favorite_meetings(client, app):
    login(client)
    with app.app_context():
        favorite_meeting = demo_meeting()
        other_meeting = Meeting(
            meeting_no=Meeting.next_meeting_no(),
            title="Unfavorited Meeting",
            meeting_date=datetime(2026, 6, 12).date(),
            location="PMD 531",
            host="Other Hoster",
            status="preparing",
            created_by=User.query.filter_by(username="admin").one().id,
        )
        db.session.add(other_meeting)
        db.session.commit()
        favorite_no = favorite_meeting.meeting_no
        favorite_title = favorite_meeting.title
        other_title = other_meeting.title

    list_html = client.get("/meetings").get_data(as_text=True)
    assert 'href="/meetings?favorite=1"' in list_html
    assert "我的收藏" in list_html
    assert url_for_path(f"/meetings/{favorite_no}/favorite") in list_html

    favorited_response = client.post(
        f"/meetings/{favorite_no}/favorite",
        data={"next": "/meetings"},
        follow_redirects=True,
    )
    favorited_html = favorited_response.get_data(as_text=True)
    assert 'meeting-favorite-btn active' in favorited_html
    assert 'aria-label="取消收藏"' in favorited_html

    favorite_filter_html = client.get("/meetings?favorite=1").get_data(as_text=True)
    assert favorite_title in favorite_filter_html
    assert other_title not in favorite_filter_html
    assert 'meeting-favorite-filter active' in favorite_filter_html

    client.post(
        f"/meetings/{favorite_no}/favorite",
        data={"next": "/meetings"},
        follow_redirects=True,
    )
    with app.app_context():
        assert MeetingFavorite.query.count() == 0


def test_meeting_filter_date_inputs_have_clear_visible_labels(client):
    login(client)

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert "会议日期" in html
    assert "创建时间" not in html
    assert "年/月/日" not in html
    assert "全部日期" not in html
    assert "全部时间" not in html
    assert 'aria-label="会议日期 从"' in html
    assert 'aria-label="会议日期 到"' in html
    assert 'aria-label="创建时间 从"' not in html
    assert 'aria-label="创建时间 到"' not in html


def test_meeting_filter_is_meeting_focused_and_uses_compact_date_ranges(client):
    login(client)

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert "会议日期" in html
    assert "创建时间" not in html
    assert "date-range-picker" in html
    assert 'data-date-range-label="会议日期"' in html
    assert 'data-date-range-label="创建时间"' not in html
    assert "全部 Topic 类型" not in html
    assert "全部Plan Version" not in html
    assert 'name="topic_category"' in html
    assert "全部类别" in html
    assert 'name="plan_version"' not in html
    assert 'name="plan_version_id"' in html
    assert 'name="plan_round_id"' in html


def test_meeting_filter_layout_uses_title_search_and_equal_controls(client):
    login(client)

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert 'placeholder="搜索会议标题"' in html
    assert 'placeholder="搜索会议标题、Topic、采购事项"' not in html
    assert html.index('data-date-range-label="会议日期"') < html.index('name="location"')
    assert 'class="meeting-filter-control meeting-title-search"' in html
    assert html.count("meeting-filter-control") >= 6


def test_topic_drafts_filter_layout_keeps_actions_on_single_row(client):
    login(client)

    response = client.get("/topics/drafts")
    html = response.get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert 'class="meeting-filter-grid topic-draft-filter-grid"' in html
    assert ".topic-draft-filter-grid { grid-template-columns:" in css
    assert "minmax(142px, auto)" in css
    assert ".topic-draft-filter-grid .split-actions { grid-column: auto;" in css
    assert ".topic-draft-filter-grid .split-actions .btn { min-width: 66px;" in css


def test_meeting_list_search_only_matches_meeting_title(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        topic_title = meeting.topics.first().title

    topic_search = client.get(f"/meetings?search={topic_title}")
    meeting_title_search = client.get("/meetings?search=QBP")

    assert "暂无会议信息" in topic_search.get_data(as_text=True)
    assert "Q3 26BP POR Review" in meeting_title_search.get_data(as_text=True)


def test_meeting_list_uses_chinese_host_label(client):
    login(client)

    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert "主持人" in html
    assert "全部主持人" in html
    assert "Hoster" not in html


def test_admin_can_edit_and_delete_meeting(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting(
            meeting_no=Meeting.next_meeting_no(),
            title="Editable Meeting",
            meeting_date=datetime(2026, 6, 10).date(),
            location="Old Room",
            host="Old Host",
            status="draft",
            created_by=User.query.filter_by(username="admin").one().id,
        )
        db.session.add(meeting)
        db.session.commit()
        meeting_no = meeting.meeting_no

    edit_response = client.post(
        f"/meetings/{meeting_no}/edit",
        data={
            "title": "Edited Meeting",
            "meeting_date": "2026-06-11",
            "location": "New Room / Teams",
            "host": "New Host",
            "status": "preparing",
        },
        follow_redirects=True,
    )

    assert edit_response.status_code == 200
    assert "Edited Meeting" in edit_response.get_data(as_text=True)
    with app.app_context():
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).one()
        assert meeting.title == "Edited Meeting"
        assert meeting.location == "New Room / Teams"
        assert meeting.status == "preparing"

    delete_response = client.post(f"/meetings/{meeting_no}/delete", follow_redirects=True)

    assert delete_response.status_code == 200
    with app.app_context():
        assert Meeting.query.filter_by(meeting_no=meeting_no).first() is None


def url_for_path(path):
    return path


def test_topic_detail_panel_and_update(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        meeting_no = topic.meeting.meeting_no
        topic_id = topic.id

    page = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    assert "OP Cum Yields" in page.get_data(as_text=True)
    assert "议题信息" in page.get_data(as_text=True)

    response = client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "OP Cum Yields",
            "category": "服务采购",
            "owner": "Facility Team",
            "present_order": "1",
            "status": "ready",
            "background": "园区物业合同即将到期，需要重新比价。",
            "purpose": "确认续约策略和预算边界。",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "园区物业合同即将到期" in response.get_data(as_text=True)
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.status == "ready"
        assert topic.purpose == "确认续约策略和预算边界。"


def test_reviewer_can_decide_meeting_topic_without_changing_agenda_membership(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        meeting = topic.meeting
        meeting_no = meeting.meeting_no
        topic_id = topic.id
        original_meeting_id = topic.meeting_id
        original_workflow_status = topic.workflow_status
        original_present_order = topic.present_order

    response = client.post(
        f"/topics/{topic_id}/meeting-decision",
        data={"decision": "approved"},
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "现场决策已保存" in html
    assert "已通过" in html
    assert "决策人" in html
    assert "决策时间" in html
    with app.app_context():
        decided = db.session.get(Topic, topic_id)
        assert decided.decision_status == "approved"
        assert decided.decision_comment == ""
        assert decided.decision_by == User.query.filter_by(username="admin").one().id
        assert decided.decision_at is not None
        assert decided.meeting_id == original_meeting_id
        assert decided.workflow_status == original_workflow_status
        assert decided.present_order == original_present_order
        audit = AuditLog.query.filter_by(action="decide_topic", target_id=topic_id).one()
        assert audit.metadata_json["meeting_no"] == meeting_no
        assert audit.metadata_json["decision_status"] == "approved"

    reject_response = client.post(
        f"/topics/{topic_id}/meeting-decision",
        data={"decision": "rejected"},
        follow_redirects=True,
    )
    assert reject_response.status_code == 200
    assert "已驳回" in reject_response.get_data(as_text=True)
    with app.app_context():
        rejected = db.session.get(Topic, topic_id)
        assert rejected.decision_status == "rejected"
        assert rejected.meeting_id == original_meeting_id


def test_meeting_decision_supports_conditional_approval_and_keeps_report_mode(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    response = client.post(
        f"/topics/{topic_id}/meeting-decision",
        data={
            "decision": "conditional_approved",
            "decision_comment": "补充老板签核邮件",
            "mode": "report",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/meetings/{meeting_no}?topic_id={topic_id}&mode=report")
    with app.app_context():
        decided = db.session.get(Topic, topic_id)
        assert decided.decision_status == "conditional_approved"
        assert decided.decision_comment == "补充老板签核邮件"
        audit = AuditLog.query.filter_by(action="decide_topic", target_id=topic_id).order_by(AuditLog.id.desc()).first()
        assert audit.metadata_json["decision_status"] == "conditional_approved"

    page = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}&mode=report")
    html = page.get_data(as_text=True)

    assert 'data-meeting-detail-mode-root data-mode="report"' in html
    assert "有条件通过" in html
    assert "补充老板签核邮件" in html


def test_meeting_topic_decision_requires_reviewer_and_bound_approved_topic(client, app):
    with app.app_context():
        alice = create_user("decision_alice")
        meeting = demo_meeting()
        approved_topic = meeting.topics.first()
        draft_topic = Topic(
            title="Draft Decision Guard",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            background="Background",
            purpose="Purpose",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        submitted_topic = Topic(
            title="Submitted Decision Guard",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            background="Background",
            purpose="Purpose",
            created_by=alice.id,
            requested_meeting_id=meeting.id,
            workflow_status="submitted",
            status="pending",
        )
        db.session.add_all([draft_topic, submitted_topic])
        db.session.commit()
        approved_topic_id = approved_topic.id
        draft_topic_id = draft_topic.id
        submitted_topic_id = submitted_topic.id

    login_as(client, "decision_alice")
    forbidden = client.post(
        f"/topics/{approved_topic_id}/meeting-decision",
        data={"decision": "approved"},
    )
    assert forbidden.status_code == 403

    client.get("/auth/logout")
    login(client)
    assert client.post(
        f"/topics/{draft_topic_id}/meeting-decision",
        data={"decision": "approved"},
    ).status_code == 400
    assert client.post(
        f"/topics/{submitted_topic_id}/meeting-decision",
        data={"decision": "rejected"},
    ).status_code == 400


def test_meeting_detail_renders_topic_decision_controls_and_badges(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    page = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "保存议题" in html
    assert f'action="/topics/{topic_id}/meeting-decision"' in html
    assert 'name="decision" value="rejected"' in html
    assert 'name="decision" value="approved"' in html
    assert 'name="decision" value="conditional_approved"' in html
    assert 'name="decision" value="delayed"' in html
    assert "待决策" in html
    assert "现场决策" in html
    assert "有条件通过" in html
    assert "延期" in html


def test_meeting_detail_mode_toggle_scopes_status_and_decision_ui(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    response = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = response.get_data(as_text=True)
    header = html[html.index('class="page-header"') : html.index('class="detail-layout"')]
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]
    info_card = html[html.index('id="topic-info-card"') : html.index('id="topic-attachments-card"')]

    assert response.status_code == 200
    assert 'data-meeting-detail-mode-root data-mode="prepare"' in html
    assert 'data-meeting-mode-toggle' in header
    assert 'data-meeting-mode-option="prepare"' in header
    assert 'data-meeting-mode-option="report"' in header
    assert "准备模式" in header
    assert "汇报模式" in header
    assert 'data-meeting-mode-panel="prepare"' in topic_list_block
    assert 'data-meeting-mode-panel="report"' in topic_list_block
    assert '<span class="badge pending" data-meeting-mode-panel="prepare">' in topic_list_block
    assert '<span class="badge decision-pending" data-meeting-mode-panel="report">' in topic_list_block
    assert '<div class="form-group" data-meeting-mode-panel="prepare"><label>状态</label>' in info_card
    assert '<div class="form-group" data-meeting-mode-panel="report"><label>现场决策</label>' in info_card
    assert '<button class="btn btn-success" data-meeting-mode-panel="prepare" type="submit" form="topic-update-form">' in info_card
    assert '<div class="topic-decision-buttons" data-meeting-mode-panel="report">' in info_card
    assert 'data-report-mode-lock' in info_card
    assert 'data-lock-original-disabled' in html


def test_meeting_detail_report_mode_survives_topic_navigation(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        topics = meeting.topics.order_by(Topic.present_order.asc()).all()
        first_topic_id = topics[0].id
        second_topic_id = topics[1].id
        meeting_no = meeting.meeting_no

    response = client.get(f"/meetings/{meeting_no}?topic_id={first_topic_id}&mode=report")
    html = response.get_data(as_text=True)
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]

    assert response.status_code == 200
    assert 'data-meeting-detail-mode-root data-mode="report"' in html
    assert f'href="/meetings/{meeting_no}?topic_id={second_topic_id}&amp;mode=report"' in topic_list_block
    assert f'href="/meetings/{meeting_no}?topic_id={second_topic_id}"' not in topic_list_block
    assert 'data-preserve-meeting-mode-link' in topic_list_block
    assert "function syncMeetingDetailModeLinks(mode) {" in html
    assert 'link.setAttribute("href", url.toString());' in html


def test_topic_category_supports_plan_review_categories(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.order_by(Topic.id.asc()).first()
        meeting_no = topic.meeting.meeting_no
        topic_id = topic.id

    page = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = page.get_data(as_text=True)
    assert "<label>类别</label>" in html
    assert "readonly" in html
    assert 'name="category"' not in html
    assert '<option value="采购策略"' not in html
    assert '<option value="采购决策"' not in html
    assert '<option value="提案"' not in html
    assert '<option value="决策"' not in html

    client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "POR Review Topic",
            "category": "POR Review",
            "owner": "Buyer",
            "present_order": "1",
            "status": "ready",
            "background": "background",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )

    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.title == "POR Review Topic"
        assert topic.category == topic.meeting.category

    client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "ST Meeting Topic",
            "category": "ST Meeting",
            "owner": "Buyer",
            "present_order": "1",
            "status": "ready",
            "background": "background",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )

    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.title == "ST Meeting Topic"
        assert topic.category == topic.meeting.category

    client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "Category Guard Topic",
            "category": "随便乱填",
            "owner": "Buyer",
            "present_order": "1",
            "status": "ready",
            "background": "background",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )

    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.title == "Category Guard Topic"
        assert topic.category == topic.meeting.category


def test_attachment_upload_preview_and_download_are_login_protected(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    anonymous = client.get(f"/attachments/{topic_id}/download")
    assert anonymous.status_code == 302
    assert "/auth/login" in anonymous.headers["Location"]

    login(client)
    upload = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"%PDF-1.4 demo"), "proposal.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert upload.status_code == 200
    upload_html = upload.get_data(as_text=True)
    assert "proposal.pdf" in upload_html
    attachments_area = upload_html[upload_html.index('id="topic-attachments-card"') :]
    assert "file-upload-shell" in attachments_area
    assert "file-upload-button" in attachments_area
    assert "未选择任何文件" in attachments_area
    assert "上传</button>" not in attachments_area
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="proposal.pdf").one()
        preview = client.get(f"/attachments/{attachment.id}/preview")
        download = client.get(f"/attachments/{attachment.id}/download")

    assert preview.status_code == 302
    assert preview.headers["Location"].startswith("http://kk.example:8012/onlinePreview?url=")
    source_url = decode_kk_preview_source(preview.headers["Location"])
    assert f"/attachments/{attachment.id}/fileview-source" in source_url
    assert "fullfilename=proposal.pdf" in source_url
    assert download.status_code == 200
    assert "attachment" in download.headers["Content-Disposition"]


def test_office_attachment_upload_invokes_decryption_service(client, app, monkeypatch, caplog):
    calls = []

    def fake_decrypt(path):
        calls.append(Path(path))
        return path

    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    login(client)
    with caplog.at_level("INFO"):
        response = client.post(
            f"/topics/{topic_id}/attachments",
            data={"file": (io.BytesIO(b"encrypted pptx bytes"), "encrypted-deck.pptx")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    assert response.status_code == 200
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "Attachment decryption started" in log_text
    assert "Attachment decryption completed" in log_text
    assert "encrypted-deck.pptx" in log_text
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="encrypted-deck.pptx").one()
        saved_path = app.config["UPLOAD_FOLDER"] / str(attachment.topic.meeting_id) / str(topic_id) / attachment.stored_filename
        assert calls == [saved_path]


def test_image_attachment_upload_skips_decryption_service(client, app, monkeypatch):
    calls = []

    def fake_decrypt(path):
        calls.append(Path(path))
        return path

    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    login(client)
    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"png bytes"), "diagram.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert calls == []


def test_decryption_failure_does_not_break_attachment_upload_and_is_audited(client, app, monkeypatch):
    def failing_decrypt(_path):
        raise RuntimeError("decrypt server unavailable")

    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", failing_decrypt)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    login(client)
    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted pdf bytes"), "encrypted-proposal.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "附件上传成功" in response.get_data(as_text=True)
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="encrypted-proposal.pdf").one()
        log = AuditLog.query.filter_by(action="decrypt_attachment_failed", target_type="attachment").one()
        assert log.target_label == "encrypted-proposal.pdf"
        assert log.metadata_json["topic_id"] == topic_id
        assert log.metadata_json["file_type"] == "pdf"
        assert "decrypt server unavailable" in log.metadata_json["error"]
        assert attachment.file_size == len(b"encrypted pdf bytes")


def test_decryption_result_replaces_saved_file_without_changing_attachment_reference(client, app, monkeypatch):
    def fake_decrypt(path):
        decrypted_path = Path(path).with_name("decrypted-output.pdf")
        decrypted_path.write_bytes(b"%PDF-1.4 decrypted bytes")
        return decrypted_path

    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    login(client)
    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted bytes"), "replace-me.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="replace-me.pdf").one()
        saved_path = app.config["UPLOAD_FOLDER"] / str(attachment.topic.meeting_id) / str(topic_id) / attachment.stored_filename
        assert attachment.stored_filename != "decrypted-output.pdf"
        assert attachment.file_size == len(b"%PDF-1.4 decrypted bytes")
        assert saved_path.read_bytes() == b"%PDF-1.4 decrypted bytes"
        assert not saved_path.with_name("decrypted-output.pdf").exists()


def test_attachment_upload_indexes_decrypted_material_chunks(client, app, monkeypatch):
    class FakeKnowledgeClient:
        def __init__(self):
            self.uploaded_paths = []
            self.knowledge_ids = []
            self.processed_before_add = False
            self.add_saw_processed_file = False

        def ensure_knowledge_base(self, scope_type, scope_id, title):
            knowledge_id = f"{scope_type}-{scope_id}"
            self.knowledge_ids.append(knowledge_id)
            return knowledge_id

        def upload_file(self, path, metadata=None):
            self.uploaded_paths.append(Path(path))
            return f"file-{len(self.uploaded_paths)}"

        def add_file_to_knowledge(self, knowledge_id, file_id):
            self.add_saw_processed_file = self.processed_before_add
            return None

        def wait_for_file_processed(self, file_id, timeout_seconds=60):
            self.processed_before_add = True
            return True

    def fake_decrypt(source_path):
        decrypted_path = source_path.with_name("decrypted-index-source.pptx")
        create_pptx(decrypted_path, ["解密后的材料写了供应商A锁价三个月"])
        return decrypted_path

    fake_client = FakeKnowledgeClient()
    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = True
    app.config["ZHISHU_KNOWLEDGE_CLIENT"] = fake_client
    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic_id = topic.id

    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted placeholder"), "encrypted-index.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="encrypted-index.pptx").one()
        document = MaterialDocument.query.filter_by(attachment_id=attachment.id).one()
        chunks = MaterialChunk.query.filter_by(document_id=document.id).all()
        assert document.status == "indexed"
        assert document.zhishu_topic_knowledge_id == f"topic-{attachment.topic_id}"
        assert document.zhishu_meeting_knowledge_id == f"meeting-{attachment.topic.meeting_id}"
        assert chunks
        assert any("解密后的材料写了供应商A锁价三个月" in chunk.text for chunk in chunks)
        assert fake_client.add_saw_processed_file is True


def test_attachment_upload_indexes_material_chunks_into_local_vector_store(client, app, monkeypatch):
    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed_texts(self, texts):
            return [[float(index + 1), 0.0] for index, _text in enumerate(texts)]

    class FakeVectorStore:
        def __init__(self):
            self.upserted = []

        def upsert_chunks(self, chunks, vectors):
            self.upserted.extend((chunk.id, vector) for chunk, vector in zip(chunks, vectors))

        def delete_document(self, document_id):
            return None

    def fake_decrypt(source_path):
        decrypted_path = source_path.with_name("vector-index-source.pptx")
        create_pptx(decrypted_path, ["本地向量索引应该保存这段供应商证据"])
        return decrypted_path

    fake_store = FakeVectorStore()
    app.config["MATERIAL_RAG_VECTOR_ENABLED"] = True
    app.config["MATERIAL_RAG_EMBEDDING_CLIENT"] = FakeEmbeddingClient()
    app.config["MATERIAL_RAG_VECTOR_STORE"] = fake_store
    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = False
    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    login(client)
    with app.app_context():
        topic_id = Topic.query.first().id

    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted placeholder"), "vector-index.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="vector-index.pptx").one()
        document = MaterialDocument.query.filter_by(attachment_id=attachment.id).one()
        chunks = MaterialChunk.query.filter_by(document_id=document.id).all()
        assert document.status == "indexed"
        assert document.error_message == ""
        assert chunks
        assert all(chunk.embedding_status == "indexed" for chunk in chunks)
        assert all(chunk.embedding_model == "fake-embedding" for chunk in chunks)
        assert all(chunk.embedding_dim == 2 for chunk in chunks)
        assert fake_store.upserted == [(chunk.id, [float(index + 1), 0.0]) for index, chunk in enumerate(chunks)]


def test_material_rag_client_uses_dedicated_knowledge_api_key(app):
    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = True
    app.config["ZHISHU_API_KEY"] = "chat-key"
    app.config["ZHISHU_KNOWLEDGE_API_KEY"] = "knowledge-key"

    with app.app_context():
        client = zhishu_knowledge_client()

    assert isinstance(client, ZhishuKnowledgeClient)
    assert client.headers["Authorization"] == "Bearer knowledge-key"


def test_material_rag_client_can_use_x_api_key_header():
    client = ZhishuKnowledgeClient(
        "http://zhishu.example",
        "knowledge-key",
        auth_header="X-API-Key",
    )

    assert client.headers == {"X-API-Key": "knowledge-key"}
    assert client.json_headers == {
        "X-API-Key": "knowledge-key",
        "Content-Type": "application/json",
    }


def test_zhishu_metadata_is_flat_vector_store_safe():
    metadata = sanitized_zhishu_metadata(
        {
            "attachment_id": 12,
            "filename": "报价材料.pptx",
            "debug": {"nested": True},
            "tags": ["Q3 26BP", "供应商"],
            "empty": None,
        }
    )

    assert metadata == {
        "attachment_id": 12,
        "filename": "报价材料.pptx",
        "debug": '{"nested": true}',
        "tags": '["Q3 26BP", "供应商"]',
        "empty": "",
    }
    assert all(not isinstance(value, (dict, list)) for value in metadata.values())


def test_parse_embedding_response_accepts_openai_and_ollama_shapes():
    assert parse_embedding_response({"data": [{"embedding": [0.1, 0.2]}]}) == [[0.1, 0.2]]
    assert parse_embedding_response({"embeddings": [[0.3, 0.4]]}) == [[0.3, 0.4]]
    assert parse_embedding_response({"embedding": [0.5, 0.6]}) == [[0.5, 0.6]]


def test_material_embedding_client_prefers_local_sentence_transformer(app, tmp_path, monkeypatch):
    class FakeSentenceTransformer:
        loaded_path = None

        def __init__(self, model_path):
            self.model_path = str(model_path)
            FakeSentenceTransformer.loaded_path = self.model_path

        def encode(self, texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False):
            assert batch_size == 7
            assert normalize_embeddings is True
            assert show_progress_bar is False
            return [[float(len(text)), 1.0] for text in texts]

    model_dir = tmp_path / "embedding_model" / "sentence-transformers" / "all-MiniLM-L6-v2"
    model_dir.mkdir(parents=True)
    app.config["MATERIAL_RAG_VECTOR_ENABLED"] = True
    app.config["MATERIAL_RAG_EMBEDDING_CLIENT"] = None
    app.config["MATERIAL_RAG_LOCAL_EMBEDDING_ENABLED"] = True
    app.config["MATERIAL_RAG_LOCAL_EMBEDDING_MODEL_PATH"] = model_dir
    app.config["MATERIAL_RAG_LOCAL_EMBEDDING_BATCH_SIZE"] = 7
    app.config["ZHISHU_API_KEY"] = ""
    monkeypatch.setattr("backend.material_rag.SentenceTransformer", FakeSentenceTransformer)

    with app.app_context():
        client = material_embedding_client()
        vectors = client.embed_texts(["abc", "abcd"])

    assert FakeSentenceTransformer.loaded_path == str(model_dir)
    assert getattr(client, "model", "") == str(model_dir)
    assert vectors == [[3.0, 1.0], [4.0, 1.0]]


def test_attachment_material_local_index_survives_zhishu_unauthorized(client, app, monkeypatch):
    class UnauthorizedKnowledgeClient:
        def upload_file(self, path, metadata=None):
            raise RuntimeError("智枢知识库接口鉴权失败，请检查 ZHISHU_KNOWLEDGE_API_KEY")

    def fake_decrypt(source_path):
        decrypted_path = source_path.with_name("local-index-source.pptx")
        create_pptx(decrypted_path, ["本地索引仍然应该保留这段供应商风险证据"])
        return decrypted_path

    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = True
    app.config["ZHISHU_KNOWLEDGE_CLIENT"] = UnauthorizedKnowledgeClient()
    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    login(client)
    with app.app_context():
        topic_id = Topic.query.first().id

    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted placeholder"), "local-index.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="local-index.pptx").one()
        document = MaterialDocument.query.filter_by(attachment_id=attachment.id).one()
        chunks = MaterialChunk.query.filter_by(document_id=document.id).all()
        assert document.status == "indexed"
        assert "本地索引可用" in document.error_message
        assert chunks
        assert "本地索引仍然应该保留这段供应商风险证据" in chunks[0].text


def test_attachment_material_hides_known_openwebui_metadata_sync_error(client, app, monkeypatch):
    class MetadataFailingKnowledgeClient:
        def ensure_knowledge_base(self, scope_type, scope_id, title):
            return f"{scope_type}-{scope_id}"

        def upload_file(self, path, metadata=None):
            return "file-metadata-error"

        def wait_for_file_processed(self, file_id, timeout_seconds=60):
            return True

        def add_file_to_knowledge(self, knowledge_id, file_id):
            raise RuntimeError("400 Bad Request: argument 'metadatas': Cannot convert Python object to MetadataValue")

    def fake_decrypt(source_path):
        decrypted_path = source_path.with_name("metadata-safe-source.pptx")
        create_pptx(decrypted_path, ["本地索引可以继续用于材料证据"])
        return decrypted_path

    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = True
    app.config["ZHISHU_KNOWLEDGE_CLIENT"] = MetadataFailingKnowledgeClient()
    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    login(client)
    with app.app_context():
        topic_id = Topic.query.first().id

    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted placeholder"), "metadata-safe.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="metadata-safe.pptx").one()
        document = MaterialDocument.query.filter_by(attachment_id=attachment.id).one()
        assert document.status == "indexed"
        assert document.error_message == ""
        assert MaterialChunk.query.filter_by(document_id=document.id).count() > 0


def test_attachment_upload_keeps_text_index_when_vector_index_fails(client, app, monkeypatch):
    class FailingEmbeddingClient:
        def embed_texts(self, texts):
            raise RuntimeError("embedding offline")

    def fake_decrypt(source_path):
        decrypted_path = source_path.with_name("vector-fail-source.pptx")
        create_pptx(decrypted_path, ["即使向量失败，本地文本索引仍然可用"])
        return decrypted_path

    app.config["MATERIAL_RAG_VECTOR_ENABLED"] = True
    app.config["MATERIAL_RAG_EMBEDDING_CLIENT"] = FailingEmbeddingClient()
    app.config["ZHISHU_KNOWLEDGE_ENABLED"] = False
    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    login(client)
    with app.app_context():
        topic_id = Topic.query.first().id

    response = client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"encrypted placeholder"), "vector-fail.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="vector-fail.pptx").one()
        document = MaterialDocument.query.filter_by(attachment_id=attachment.id).one()
        chunks = MaterialChunk.query.filter_by(document_id=document.id).all()
        assert document.status == "indexed"
        assert "文本索引可用；向量索引失败：embedding offline" in document.error_message
        assert chunks
        assert all(chunk.embedding_status == "failed" for chunk in chunks)
        assert all(chunk.embedding_error == "embedding offline" for chunk in chunks)


def test_draft_initial_material_upload_invokes_decryption_service(client, app, monkeypatch):
    calls = []

    def fake_decrypt(path):
        calls.append(Path(path))
        return path

    monkeypatch.setattr("backend.decryption_service.decrypt_attachment", fake_decrypt)
    with app.app_context():
        create_user("alice")

    login_as(client, "alice")
    response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Encrypted Draft Material",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "background": "背景",
            "purpose": "目的",
            "file": (io.BytesIO(b"encrypted docx bytes"), "initial-material.docx"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        topic = Topic.query.filter_by(title="Encrypted Draft Material").one()
        attachment = Attachment.query.filter_by(topic_id=topic.id).one()
        saved_path = app.config["UPLOAD_FOLDER"] / "draft" / str(topic.id) / attachment.stored_filename
        assert calls == [saved_path]


def test_attachment_delete_is_available_for_meeting_and_draft_topics(client, app):
    login(client)
    with app.app_context():
        meeting_topic = Topic.query.filter(Topic.meeting_id.isnot(None)).order_by(Topic.id.asc()).first()
        draft_topic = Topic(
            title="Draft attachment topic",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Admin",
            created_by=User.query.filter_by(username="admin").one().id,
            workflow_status="draft",
        )
        db.session.add(draft_topic)
        db.session.commit()
        meeting_topic_id = meeting_topic.id
        meeting_no = meeting_topic.meeting.meeting_no
        draft_topic_id = draft_topic.id

    client.post(
        f"/topics/{meeting_topic_id}/attachments",
        data={"file": (io.BytesIO(b"%PDF-1.4 meeting"), "meeting-delete.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    client.post(
        f"/topics/{draft_topic_id}/attachments",
        data={"file": (io.BytesIO(b"%PDF-1.4 draft"), "draft-delete.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    with app.app_context():
        meeting_attachment = Attachment.query.filter_by(original_filename="meeting-delete.pdf").one()
        draft_attachment = Attachment.query.filter_by(original_filename="draft-delete.pdf").one()
        meeting_attachment_id = meeting_attachment.id
        draft_attachment_id = draft_attachment.id

    meeting_page = client.get(f"/meetings/{meeting_no}?topic_id={meeting_topic_id}")
    draft_page = client.get(f"/topics/drafts/{draft_topic_id}/edit")
    assert f"/attachments/{meeting_attachment_id}/delete" in meeting_page.get_data(as_text=True)
    assert f"/attachments/{draft_attachment_id}/delete" in draft_page.get_data(as_text=True)

    delete_response = client.post(f"/attachments/{meeting_attachment_id}/delete", follow_redirects=True)

    assert delete_response.status_code == 200
    assert "meeting-delete.pdf" not in delete_response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Attachment, meeting_attachment_id) is None
        assert db.session.get(Attachment, draft_attachment_id) is not None


def test_minutes_save_and_status_transition(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()
        meeting_no = meeting.meeting_no

    response = client.post(
        f"/meetings/{meeting_no}/minutes",
        data={
            "summary": "会议完成三个采购 topic 汇报。",
            "decisions": "NPP New Product CS时间进入二轮报价。",
            "action_items": "BE IE产能扩建补充供应商资质。",
            "meeting_status": "completed",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "NPP New Product CS时间进入二轮报价" in response.get_data(as_text=True)
    with app.app_context():
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).one()
        minutes = MeetingMinutes.query.filter_by(meeting_id=meeting.id).one()
        assert meeting.status == "completed"
        assert minutes.action_items == "BE IE产能扩建补充供应商资质。"


def test_minutes_entry_lives_in_left_sidebar_and_topic_order_is_info_attachments_review(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()

    response = client.get(f"/meetings/{meeting.meeting_no}")
    html = response.get_data(as_text=True)
    left_column = html[html.index('<div class="detail-layout">') : html.index('<div class="detail-main">')]
    right_column = html[html.index('<div class="detail-main">') :]

    assert 'id="minutes-list-card"' in left_column
    assert 'id="minutes-card"' not in right_column
    assert html.index('id="topic-list-card"') < html.index('id="minutes-list-card"')
    assert html.index('id="topic-info-card"') < html.index('id="topic-attachments-card"')
    assert html.index('id="topic-attachments-card"') < html.index('id="topic-material-review-card"')

    minutes_response = client.get(f"/meetings/{meeting.meeting_no}?view=minutes")
    minutes_html = minutes_response.get_data(as_text=True)
    minutes_right = minutes_html[minutes_html.index('<div class="detail-main">') :]
    assert 'id="minutes-card"' in minutes_right
    assert 'id="topic-info-card"' not in minutes_right
    assert 'id="topic-attachments-card"' not in minutes_right


def test_existing_meeting_can_add_and_delete_topics(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()
        meeting_no = meeting.meeting_no

    add_response = client.post(
        f"/meetings/{meeting_no}/topics",
        data={
            "title": "IT 服务采购",
            "category": "服务采购",
            "owner": "IT Team",
            "present_order": "4",
        },
        follow_redirects=True,
    )

    assert add_response.status_code == 200
    assert "IT 服务采购" in add_response.get_data(as_text=True)

    with app.app_context():
        topic = Topic.query.filter_by(title="IT 服务采购").one()
        topic_id = topic.id

    delete_response = client.post(f"/topics/{topic_id}/delete", follow_redirects=True)

    assert delete_response.status_code == 200
    assert "IT 服务采购" not in delete_response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Topic, topic_id) is None


def test_topic_create_uses_compact_modal_entry(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()

    response = client.get(f"/meetings/{meeting.meeting_no}")
    html = response.get_data(as_text=True)
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]

    assert 'id="topic-create-modal"' in html
    assert 'id="open-topic-create-modal"' in topic_list_block
    assert 'aria-label="新增议题"' in topic_list_block
    assert 'class="add-topic-form"' not in topic_list_block
    assert 'class="modal-form"' not in topic_list_block
    assert '<i class="fas' not in topic_list_block
    assert 'svg-icon' in topic_list_block


def test_topic_duration_defaults_and_updates_from_meeting_detail(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()
        meeting_no = meeting.meeting_no

    create_response = client.post(
        f"/meetings/{meeting_no}/topics",
        data={
            "title": "Duration Default Topic",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Buyer",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    with app.app_context():
        topic = Topic.query.filter_by(title="Duration Default Topic").one()
        topic_id = topic.id
        assert topic.duration_minutes == 15

    update_response = client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "Duration Updated Topic",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Buyer",
            "duration_minutes": "25",
            "status": "ready",
            "background": "background",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )
    assert update_response.status_code == 200
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.duration_minutes == 25


def test_topic_duration_clamps_and_saves_from_draft_form(client, app):
    with app.app_context():
        create_user("alice")
        meeting = demo_meeting()
        meeting_id = meeting.id

    login_as(client, "alice")
    create_response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Timed Draft Topic",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "duration_minutes": "500",
            "background": "background",
            "purpose": "purpose",
            "requested_meeting_id": str(meeting_id),
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    with app.app_context():
        topic = Topic.query.filter_by(title="Timed Draft Topic").one()
        topic_id = topic.id
        assert topic.duration_minutes == 180

    edit_response = client.post(
        f"/topics/drafts/{topic_id}/edit",
        data={
            "title": "Timed Draft Topic",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "duration_minutes": "0",
            "background": "background",
            "purpose": "purpose",
            "requested_meeting_id": str(meeting_id),
            "draft_action": "save",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    with app.app_context():
        assert db.session.get(Topic, topic_id).duration_minutes == 5


def test_meeting_detail_shows_topic_duration_and_timer_controls(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        topic = meeting.topics.order_by(Topic.present_order.asc()).first()
        topic.duration_minutes = 20
        upload_dir = app.config["UPLOAD_FOLDER"] / str(meeting.id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "timer-preview.pdf").write_bytes(b"%PDF-1.4 timer preview")
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="timer-preview.pdf",
                stored_filename="timer-preview.pdf",
                file_type="pdf",
                file_size=22,
            )
        )
        db.session.commit()
        meeting_no = meeting.meeting_no
        topic_id = topic.id

    response = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = response.get_data(as_text=True)
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]
    minutes_list_block = html[html.index('id="minutes-list-card"') : html.index('<div class="detail-main">')]
    topic_info_block = html[html.index('id="topic-info-card"') : html.index('id="topic-attachments-card"')]

    assert "20 分钟" in topic_info_block
    assert 'name="duration_minutes"' in topic_info_block
    assert "<label>汇报人</label>" in topic_info_block
    assert "<label>负责人</label>" not in topic_info_block
    assert 'class="duration-stepper duration-stepper-integrated"' in topic_info_block
    assert 'name="duration_minutes" type="text"' in topic_info_block
    assert f'data-topic-duration="20"' in topic_list_block
    assert 'class="topic-timer-button"' in topic_list_block
    assert 'data-topic-timer-button' in topic_list_block
    assert 'topic-duration-chip' not in topic_list_block
    assert 'data-topic-timer-action' not in topic_list_block
    assert '<button class="topic-timer-button" type="button" data-topic-timer-button' in topic_list_block
    assert '<span class="topic-timer-remaining" data-topic-timer-remaining>' in topic_list_block
    assert 'id="attachment-preview-topic-timer"' in html
    assert 'data-preview-topic-timer' in html
    assert 'id="attachment-preview-topic-title"' in html
    assert 'data-preview-topic-timer-remaining' in html
    assert 'data-topic-id="{{ selected_topic.id }}"' not in html
    assert f'data-topic-id="{topic_id}"' in html
    assert 'data-topic-title=' in html
    assert 'syncPreviewTopicTimer' in html
    assert 'setPreviewTopicTimer' in html
    first_topic_item = topic_list_block[
        topic_list_block.index('class="topic-item active"') : topic_list_block.index('class="topic-delete-form"')
    ]
    footer_start = first_topic_item.index('class="topic-item-footer"')
    timer_start = first_topic_item.index('data-topic-timer')
    badge_start = first_topic_item.index('class="badge ')
    assert footer_start < timer_start < badge_start
    assert 'data-topic-timer' in first_topic_item
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")
    assert ".topic-item-footer { position: absolute;" in css
    assert "font-family: \"Segoe UI\", Arial, sans-serif;" in css
    assert "input[type=number][data-duration-input]::-webkit-inner-spin-button" in css
    assert ".duration-stepper-integrated { display: grid;" in css
    assert ".topic-item { display: block; min-height: 74px; padding: 8px 42px 42px 13px;" in css
    assert ".topic-item-footer { position: absolute; left: 10px; right: 10px;" in css
    assert "text-rendering: geometricPrecision;" in css
    assert 'class="topic-item-footer minutes-item-footer"' in minutes_list_block
    assert "event.stopPropagation()" in html
    assert "setInterval" in html


def test_topic_timer_continues_negative_and_marks_overdue(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no

    html = client.get(f"/meetings/{meeting_no}").get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert 'var sign = seconds < 0 ? "-" : "";' in html
    assert "seconds = Math.abs(seconds);" in html
    assert 'return sign + String(minutes).padStart(2, "0") + ":" + String(rest).padStart(2, "0");' in html
    assert "Math.max(0, Math.ceil((state.targetEnd - Date.now()) / 1000))" not in html
    assert "if (state.remaining <= 0) stopTopicTimer(state);" not in html
    assert 'container.classList.toggle("overdue", state.remaining < 0);' in html
    assert 'previewTopicTimer.classList.toggle("overdue", state.remaining < 0);' in html
    assert ".topic-timer.overdue .topic-timer-remaining" in css
    assert ".preview-topic-timer.overdue .topic-timer-remaining" in css


def test_topic_timer_resume_advances_overdue_display_without_skip(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no

    html = client.get(f"/meetings/{meeting_no}").get_data(as_text=True)

    assert "function startTopicTimer(container, state) {" in html
    assert "if (state.remaining <= 0) {" in html
    assert "state.remaining -= 1;" in html
    assert "state.targetEnd = Date.now() + state.remaining * 1000;" in html
    assert "renderTopicTimer(container, state);" in html
    assert "startTopicTimer(container, state);" in html
    assert "tickTopicTimer(container, state);\n        });" not in html



def test_primary_create_actions_use_green_buttons(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()

    list_html = client.get("/meetings").get_data(as_text=True)
    detail_html = client.get(f"/meetings/{meeting.meeting_no}").get_data(as_text=True)
    drafts_html = client.get("/topics/drafts").get_data(as_text=True)

    assert 'href="/meetings/create" class="btn btn-success"' in list_html
    assert 'class="icon-btn icon-btn-success"' in detail_html
    assert 'id="open-topic-create-modal"' in detail_html
    assert 'href="/topics/drafts/create" class="btn btn-success"' in drafts_html
    assert 'class="btn btn-success" type="submit"' in detail_html
    assert "添加议题" in detail_html

def test_topic_list_uses_small_icon_delete_buttons(client, app):
    login(client)
    with app.app_context():
        meeting = Meeting.query.filter_by(title="Q3 26BP POR Review").one()

    response = client.get(f"/meetings/{meeting.meeting_no}")
    html = response.get_data(as_text=True)

    assert 'class="topic-delete-icon"' in html
    assert 'class="btn btn-secondary btn-sm topic-delete"' not in html



def test_topic_list_only_shows_title_and_status_not_duplicate_details(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()

    response = client.get(f"/meetings/{meeting.meeting_no}")
    html = response.get_data(as_text=True)
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]

    assert "topic-item-meta" not in topic_list_block
    assert "topic-order-number" not in topic_list_block
    assert "?????" not in topic_list_block
    assert " | " not in topic_list_block
    assert "badge" in topic_list_block


def test_topic_order_is_changed_by_reorder_endpoint_not_manual_input(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        topics = meeting.topics.order_by(Topic.present_order.asc()).all()
        first_id = topics[0].id
        second_id = topics[1].id
        third_id = topics[2].id

    page = client.get(f"/meetings/{meeting_no}?topic_id={second_id}")
    html = page.get_data(as_text=True)
    topic_list_block = html[html.index('id="topic-list-card"') : html.index('id="minutes-list-card"')]
    topic_info_block = html[html.index('id="topic-info-card"') : html.index('id="topic-attachments-card"')]

    assert 'data-reorder-url=' in topic_list_block
    assert 'draggable="true"' in topic_list_block
    assert 'name="present_order"' not in topic_info_block
    assert 'class="topic-order-display"' not in topic_info_block
    assert "汇报顺序" not in topic_info_block

    update_response = client.post(
        f"/topics/{second_id}/update",
        data={
            "title": "Manual Order Should Not Win",
            "category": "方案",
            "plan_version": "Q2 27BP",
            "owner": "Buyer",
            "present_order": "99",
            "status": "ready",
            "background": "background",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )
    assert update_response.status_code == 200
    with app.app_context():
        assert db.session.get(Topic, second_id).present_order == 2

    reorder_response = client.post(
        f"/meetings/{meeting_no}/topics/reorder",
        json={"topic_ids": [third_id, first_id, second_id]},
    )
    assert reorder_response.status_code == 200
    with app.app_context():
        assert db.session.get(Topic, third_id).present_order == 1
        assert db.session.get(Topic, first_id).present_order == 2
        assert db.session.get(Topic, second_id).present_order == 3


def test_normal_user_cannot_reorder_meeting_topics(client, app):
    with app.app_context():
        create_user("alice")
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        topic_ids = [topic.id for topic in meeting.topics.order_by(Topic.present_order.asc()).all()]

    login_as(client, "alice")
    response = client.post(
        f"/meetings/{meeting_no}/topics/reorder",
        json={"topic_ids": list(reversed(topic_ids))},
    )

    assert response.status_code == 403


def test_main_chrome_css_keeps_top_bar_visible_while_scrolling():
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert ":root {" in css
    assert "--sidebar-width: clamp(156px, 11vw, 200px)" in css
    assert "--content-padding: clamp(14px, 1.5vw, 26px)" in css
    assert "body { font-family:" in css
    assert "padding-top: 60px" in css
    assert ".navbar { position: fixed; top: 0; left: 0; right: 0;" in css
    assert ".sidebar { position: fixed; top: 60px;" in css
    assert "width: var(--sidebar-width)" in css
    assert "margin-left: var(--sidebar-width)" in css
    assert "width: calc(100vw - var(--sidebar-width))" in css
    assert "overflow-y: auto" in css
    assert "clamp(12px, 1.6vh, 20px)" in css
    assert "clamp(8px, 1.35vh, 14px)" in css
    assert "@media (max-height: 820px)" in css
    assert ".sidebar-label" in css
    assert "text-overflow: ellipsis" in css


def test_main_chrome_uses_fixed_top_bar_and_sidebar_layout(client):
    login(client)
    response = client.get("/meetings")
    html = response.get_data(as_text=True)

    assert 'class="navbar"' in html
    assert 'class="sidebar"' in html


def test_top_bar_labels_business_and_pln_bp_user_groups(client, app):
    with app.app_context():
        business_group = Group.query.filter_by(code="mc").one()
        qbp_group = Group.query.filter_by(code="qbp").one()
        custom_group = Group(code="custom-review", name="Review Custom")
        db.session.add(custom_group)
        db.session.flush()
        create_user("business_badge_user", display_name="MC User", group_id=business_group.id)
        create_user("qbp_badge_user", display_name="PLN/BP User", group_id=qbp_group.id)
        create_user("custom_badge_user", display_name="Custom User", group_id=custom_group.id)

    login_as(client, "business_badge_user")
    procurement_html = client.get("/meetings").get_data(as_text=True)
    client.get("/auth/logout")

    login_as(client, "qbp_badge_user")
    qbp_html = client.get("/meetings").get_data(as_text=True)
    client.get("/auth/logout")

    login_as(client, "custom_badge_user")
    custom_html = client.get("/meetings").get_data(as_text=True)

    assert '<span class="user-group-role-label">采购组</span>' in procurement_html
    assert '<span class="user-group-role-label">评审组</span>' not in procurement_html
    assert '<span class="user-group-role-label">评审组</span>' in qbp_html
    assert '<span class="user-group-role-label">采购组</span>' not in qbp_html
    assert 'class="user-group-role-label"' not in custom_html


def test_sidebar_footer_shows_version_and_powered_by_copy(client):
    login(client)
    response = client.get("/meetings")
    html = response.get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'class="sidebar-nav"' in html
    assert 'class="sidebar-footer"' in html
    assert 'class="sidebar-footer-version">Version 0.0.1</div>' in html
    assert 'class="sidebar-footer-date">Update By 2026-06-05</div>' in html
    assert 'class="sidebar-footer-powered">Powered By PLN &amp; CPSCI</div>' in html
    assert "margin: auto -4px 28px" in css
    assert "padding: 18px 0 0" in css
    assert "text-align: center" in css
    assert "border-top: 1px solid rgba(255,255,255,.52)" in css
    assert ".sidebar-footer-version" in css
    assert ".sidebar-footer-date" in css
    assert ".sidebar-footer-powered" in css
    assert "font-size: 13px" in css
    assert "white-space: nowrap" in css


def test_meeting_list_uses_adaptive_filters_and_scrollable_table(client):
    login(client)
    response = client.get("/meetings")
    html = response.get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'class="table-scroll meeting-table-wrap"' in html
    assert 'class="meeting-table"' in html
    assert ".meeting-filter-grid { display: grid;" in css
    assert "grid-template-columns: repeat(7, minmax(132px, 1fr))" in css
    assert ".meeting-filter-grid .split-actions { align-self: stretch; display: flex; flex-wrap: nowrap; justify-content: flex-end;" in css
    assert "flex: 0 0 200px" not in css
    assert ".meeting-table-wrap" in css
    assert "overflow-x: auto" in css
    assert ".meeting-table { table-layout: fixed;" in css
    assert ".meeting-table th { white-space: nowrap; vertical-align: middle; text-align: center;" in css
    assert ".meeting-table td { vertical-align: middle; text-align: center;" in css
    assert ".meeting-table .topic-mix-cell { justify-items: center; text-align: center; }" in css
    assert ".meeting-table .meeting-action-group { justify-content: center; }" in css


def test_admin_sidebar_orders_agenda_before_drafts(client):
    login(client)
    response = client.get("/meetings")
    html = response.get_data(as_text=True)
    primary_nav = html[html.index("主要功能") : html.index("系统管理")]

    assert primary_nav.index("会议列表") < primary_nav.index("议题编排") < primary_nav.index("议题池")


def test_agenda_board_meta_renders_topic_duration_like_attachment_count(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()

    response = client.get(f"/agenda/{meeting.meeting_no}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "cls: 'agenda-meta-requester'" in html
    assert "cls: 'agenda-meta-attachments'" in html
    assert "cls: 'agenda-meta-duration'" in html
    assert 'class="agenda-meta-cell ${f.cls}"' in html
    assert "${escapeHtml(t.requester || '-')}" in html
    assert "附件 ${t.attachment_count}" in html
    assert "时长 ${t.duration_minutes} 分钟" in html
    assert ".agenda-card .agenda-meta { display: grid;" in html
    assert "grid-template-columns:" in html


def test_admin_sidebar_shows_single_config_table_under_system_management(client):
    login(client)
    response = client.get("/meetings")
    html = response.get_data(as_text=True)
    system_nav = html[html.index("系统管理") : html.index("</aside>")]

    assert response.status_code == 200
    assert 'href="/admin/config"' in system_nav
    assert "配置表" in system_nav
    assert "会议准备度" not in system_nav
    assert "议题完善度" not in system_nav


def test_qbp_group_user_can_access_quasi_admin_modules(client, app):
    class FakeZhishuClient:
        def chat(self, payload):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":88,"summary":"qbp ok",'
                                '"issues":"-","suggestions":"-"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    with app.app_context():
        qbp_group = Group.query.filter_by(code="qbp").one()
        user = create_user("qbp_user", group_id=qbp_group.id)
        username = user.username
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        topic = meeting.topics.order_by(Topic.present_order.asc()).first()
        topic_title = topic.title
        topic_id = topic.id

    login_as(client, username)

    meetings_page = client.get("/meetings").get_data(as_text=True)
    assert 'href="/agenda"' in meetings_page
    assert 'href="/ai-workshop/prompt"' in meetings_page
    assert 'href="/admin/config"' in meetings_page
    assert 'href="/meetings/create"' in meetings_page
    assert f'href="/meetings/{meeting_no}/edit"' in meetings_page
    assert f'action="/meetings/{meeting_no}/delete"' in meetings_page
    assert 'href="/admin/users"' not in meetings_page
    assert 'href="/admin/audit-logs"' not in meetings_page

    create_page = client.get("/meetings/create")
    assert create_page.status_code == 200
    create_response = client.post(
        "/meetings/create",
        data={
            "title": "QBP Created Meeting",
            "meeting_date": "2099-03-01",
            "location": "QBP Room",
            "host": "QBP Host",
            "status": "draft",
            "topic_title[]": ["QBP Topic"],
            "topic_category[]": ["Kick Off"],
            "topic_plan_version[]": ["Q2 27BP"],
            "topic_owner[]": ["QBP"],
            "topic_order[]": ["1"],
            "topic_duration_minutes[]": ["15"],
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200
    assert "QBP Created Meeting" in create_response.get_data(as_text=True)
    assert client.get("/agenda").status_code == 200
    assert client.get(f"/agenda/{meeting_no}").status_code == 200
    assert client.get(f"/agenda/{meeting_no}/data").status_code == 200
    assert client.get("/admin/config").status_code == 200
    assert client.get("/ai-workshop/prompt").status_code == 200
    assert client.get("/admin/users").status_code == 403
    detail = client.get(f"/meetings/{meeting_no}")
    detail_html = detail.get_data(as_text=True)
    assert detail.status_code == 200
    assert topic_title in detail_html
    assert f'action="/topics/{topic_id}/material-reviews/ai"' in detail_html
    ai_review_response = client.post(
        f"/topics/{topic_id}/material-reviews/ai",
        follow_redirects=True,
    )
    assert ai_review_response.status_code == 200
    with app.app_context():
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id, source="ai").one()
        assert review.score == 88
        assert review.summary == "qbp ok"

    edit_response = client.post(
        f"/meetings/{meeting_no}/edit",
        data={
            "title": "QBP Edited Meeting",
            "meeting_date": "2099-03-02",
            "location": "QBP Edited Room",
            "host": "QBP Host",
            "status": "preparing",
        },
        follow_redirects=True,
    )
    assert edit_response.status_code == 200
    assert "QBP Edited Meeting" in edit_response.get_data(as_text=True)

    with app.app_context():
        created_meeting_no = Meeting.query.filter_by(title="QBP Created Meeting").one().meeting_no
    delete_response = client.post(f"/meetings/{created_meeting_no}/delete", follow_redirects=True)
    assert delete_response.status_code == 200
    with app.app_context():
        assert Meeting.query.filter_by(meeting_no=created_meeting_no).first() is None


def test_builtin_procurement_groups_can_manage_prompts_only(client, app):
    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        group_ids = {
            group.code: group.id
            for group in Group.query.filter(Group.code.in_(["tpe", "tpm", "bep", "idp"])).all()
        }

    for code, group_id in group_ids.items():
        with client.session_transaction() as session:
            session.clear()
        username = f"{code}_prompt_user"
        with app.app_context():
            create_user(username, group_id=group_id)
        login_as(client, username)

        meetings_page = client.get("/meetings").get_data(as_text=True)
        assert 'href="/ai-workshop/prompt"' in meetings_page
        assert 'href="/agenda"' not in meetings_page
        assert 'href="/admin/config"' not in meetings_page

        assert client.get("/ai-workshop/prompt").status_code == 200
        save_response = client.post(
            "/ai-workshop/prompt",
            data={
                "name": f"{code.upper()} Prompt",
                "scope": code.upper(),
                "review_goal": "判断材料是否可以上会",
                "focus_points": "关注价格和供应风险",
                "knowledge_sources": [code.upper()],
                "include_score": "1",
                "include_issues": "1",
                "include_suggestions": "1",
                "is_active": "1",
                "set_default": "1",
            },
            follow_redirects=True,
        )
        assert save_response.status_code == 200
        assert f"{code.upper()} Prompt" in save_response.get_data(as_text=True)
        assert client.get("/admin/config").status_code == 403
        assert client.get("/agenda").status_code == 200
        assert client.get(f"/agenda/{meeting_no}").status_code == 403


def test_pln_bp_group_ai_workshop_can_manage_business_group_scopes(client, app):
    with app.app_context():
        tpe_group = Group.query.filter_by(code="qbp").one()
        admin = User.query.filter_by(username="admin").one()
        create_user("tpe_scope_user", group_id=tpe_group.id)
        tpe_prompt = AIPrompt(
            name="MC Only Prompt",
            scope="MC",
            review_goal="MC goal",
            focus_points="MC focus",
            knowledge_sources=["MC"],
            created_by=admin.id,
            updated_by=admin.id,
        )
        bep_prompt = AIPrompt(
            name="OP Hidden Prompt",
            scope="OP",
            review_goal="OP goal",
            focus_points="OP focus",
            knowledge_sources=["OP"],
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.session.add_all(
            [
                tpe_prompt,
                bep_prompt,
                AIKnowHow(scope="MC", content="MC visible know-how", created_by=admin.id),
                AIKnowHow(scope="OP", content="OP hidden know-how", created_by=admin.id),
            ]
        )
        db.session.commit()
        tpe_prompt_id = tpe_prompt.id
        bep_prompt_id = bep_prompt.id

    login_as(client, "tpe_scope_user")

    prompt_page = client.get("/ai-workshop/prompt")
    prompt_html = prompt_page.get_data(as_text=True)
    assert prompt_page.status_code == 200
    assert "MC Only Prompt" in prompt_html
    assert "OP Hidden Prompt" in prompt_html
    assert 'value="MC"' in prompt_html
    assert 'value="OP"' in prompt_html
    assert client.get(f"/ai-workshop/prompt?prompt_id={tpe_prompt_id}").status_code == 200
    assert client.get(f"/ai-workshop/prompt?prompt_id={bep_prompt_id}").status_code == 200

    cross_scope_save = client.post(
        "/ai-workshop/prompt",
        data={
            "name": "MC can save OP from PLN/BP",
            "scope": "OP",
            "review_goal": "cross scope",
            "knowledge_sources": ["OP"],
            "include_score": "1",
            "is_active": "1",
        },
        follow_redirects=False,
    )
    assert cross_scope_save.status_code == 302

    knowhow_page = client.get("/ai-workshop/knowhow?scope=MC")
    knowhow_html = knowhow_page.get_data(as_text=True)
    assert knowhow_page.status_code == 200
    assert "MC visible know-how" in knowhow_html
    assert 'name="scope"' in knowhow_html
    assert '<option value="MC" selected' in knowhow_html
    assert '<option value="OP"' in knowhow_html
    assert "aiw-scope-tab" not in knowhow_html
    q1_knowhow = client.get("/ai-workshop/knowhow?scope=OP")
    assert q1_knowhow.status_code == 200
    assert "OP hidden know-how" in q1_knowhow.get_data(as_text=True)


def test_non_admin_user_only_sees_meetings_and_detail_topics_for_own_approved_topics(client, app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        tpe_group = Group.query.filter_by(code="mc").one()
        alice = create_user("alice_scope", display_name="Alice Scope", group_id=tpe_group.id)
        bob = create_user("bob_scope", display_name="Bob Scope", group_id=tpe_group.id)
        own_meeting = Meeting(
            meeting_no="CM20990101",
            title="Alice Visible Meeting",
            meeting_date=datetime(2099, 1, 1).date(),
            location="Room A",
            host="Admin",
            status="preparing",
            created_by=admin.id,
            host_user_id=admin.id,
        )
        other_meeting = Meeting(
            meeting_no="CM20990102",
            title="Bob Hidden Meeting",
            meeting_date=datetime(2099, 1, 2).date(),
            location="Room B",
            host="Admin",
            status="preparing",
            created_by=admin.id,
            host_user_id=admin.id,
        )
        db.session.add_all([own_meeting, other_meeting])
        db.session.flush()
        db.session.add_all(
            [
                Topic(
                    meeting_id=own_meeting.id,
                    title="Alice Approved Topic",
                    category="Kick Off",
                    plan_version="Q3 26BP",
                    owner="Alice",
                    created_by=alice.id,
                    workflow_status="approved",
                    present_order=1,
                ),
                Topic(
                    meeting_id=own_meeting.id,
                    title="Bob Co-located Topic",
                    category="Kick Off",
                    plan_version="Q3 26BP",
                    owner="Bob",
                    created_by=bob.id,
                    workflow_status="approved",
                    present_order=2,
                ),
                Topic(
                    meeting_id=other_meeting.id,
                    title="Bob Other Meeting Topic",
                    category="Kick Off",
                    plan_version="Q3 26BP",
                    owner="Bob",
                    created_by=bob.id,
                    workflow_status="approved",
                    present_order=1,
                ),
            ]
        )
        db.session.commit()

    login_as(client, "alice_scope")

    meeting_list_html = client.get("/meetings").get_data(as_text=True)
    assert "Alice Visible Meeting" in meeting_list_html
    assert "Bob Hidden Meeting" not in meeting_list_html
    assert "Q3 26BP POR Review" not in meeting_list_html

    own_detail_html = client.get("/meetings/CM20990101").get_data(as_text=True)
    assert "Alice Approved Topic" in own_detail_html
    assert "Bob Co-located Topic" not in own_detail_html

    assert client.get("/meetings/CM20990102").status_code == 403


def test_custom_group_user_scope_matches_owned_topics_and_target_meetings(client, app):
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        custom_group = Group(code="custom-ops", name="Ops")
        db.session.add(custom_group)
        db.session.flush()
        alice = create_user("custom_alice", display_name="Custom Alice", group_id=custom_group.id)
        bob = create_user("custom_bob", display_name="Custom Bob", group_id=custom_group.id)
        own_meeting = Meeting(
            meeting_no="CM20990201",
            title="Custom Alice Meeting",
            meeting_date=datetime(2099, 2, 1).date(),
            location="Room C",
            host="Admin",
            status="preparing",
            created_by=admin.id,
            host_user_id=admin.id,
        )
        other_meeting = Meeting(
            meeting_no="CM20990202",
            title="Custom Bob Meeting",
            meeting_date=datetime(2099, 2, 2).date(),
            location="Room D",
            host="Admin",
            status="preparing",
            created_by=admin.id,
            host_user_id=admin.id,
        )
        db.session.add_all([own_meeting, other_meeting])
        db.session.flush()
        db.session.add_all(
            [
                Topic(
                    meeting_id=own_meeting.id,
                    title="Custom Alice Approved",
                    category="Kick Off",
                    plan_version="Q2 27BP",
                    owner="Alice",
                    created_by=alice.id,
                    workflow_status="approved",
                    present_order=1,
                ),
                Topic(
                    requested_meeting_id=own_meeting.id,
                    title="Custom Alice Draft",
                    category="Kick Off",
                    plan_version="Q2 27BP",
                    owner="Alice",
                    created_by=alice.id,
                    workflow_status="draft",
                    present_order=1,
                ),
                Topic(
                    meeting_id=other_meeting.id,
                    title="Custom Bob Approved",
                    category="Kick Off",
                    plan_version="Q2 27BP",
                    owner="Bob",
                    created_by=bob.id,
                    workflow_status="approved",
                    present_order=1,
                ),
            ]
        )
        db.session.commit()

    login_as(client, "custom_alice")

    meeting_list_html = client.get("/meetings").get_data(as_text=True)
    assert "Custom Alice Meeting" in meeting_list_html
    assert "Custom Bob Meeting" not in meeting_list_html

    drafts_html = client.get("/topics/drafts").get_data(as_text=True)
    assert "Custom Alice Draft" in drafts_html
    assert "Custom Bob Approved" not in drafts_html
    assert "CM20990201" in drafts_html
    assert "CM20990202" not in drafts_html


def test_powerpoint_upload_previews_as_pdf(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    login(client)
    client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"pptx demo content"), "deck.pptx")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    with app.app_context():
        attachment = Attachment.query.filter_by(original_filename="deck.pptx").one()

    preview = client.get(f"/attachments/{attachment.id}/preview")

    assert preview.status_code == 302
    assert preview.headers["Location"].startswith("http://kk.example:8012/onlinePreview?url=")
    assert "deck.pptx" in decode_kk_preview_source(preview.headers["Location"])


def test_powerpoint_preview_copies_extensionless_upload_before_conversion(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id

    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic_id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "legacy").write_bytes(b"powerpoint content without extension")
        attachment = Attachment(
            topic_id=topic_id,
            original_filename="PPT",
            stored_filename="legacy",
            file_type="pptx",
            file_size=35,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login(client)
    preview = client.get(f"/attachments/{attachment_id}/preview")

    assert preview.status_code == 302
    assert preview.headers["Location"].startswith("http://kk.example:8012/onlinePreview?url=")
    assert "fullfilename=PPT.pptx" in decode_kk_preview_source(preview.headers["Location"])


def test_existing_powerpoint_attachment_without_file_type_still_previews(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "legacy.pptx").write_bytes(b"legacy powerpoint bytes")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="PPT",
            stored_filename="legacy.pptx",
            file_type="",
            file_size=24,
        )
        db.session.add(attachment)
        db.session.commit()
        meeting_no = topic.meeting.meeting_no
        attachment_id = attachment.id

    login(client)
    detail = client.get(f"/meetings/{meeting_no}")
    html = detail.get_data(as_text=True)
    attachment_block = html[html.index("legacy.pptx") if "legacy.pptx" in html else html.index("PPT") :]

    assert f"/attachments/{attachment_id}/preview" in attachment_block
    assert "打开" not in attachment_block[:600]

    preview = client.get(f"/attachments/{attachment_id}/preview")
    assert preview.status_code == 302
    assert preview.headers["Location"].startswith("http://kk.example:8012/onlinePreview?url=")
    assert "fullfilename=PPT.pptx" in decode_kk_preview_source(preview.headers["Location"])


def test_attachment_preview_uses_page_modal_without_present_area(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    login(client)
    client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"%PDF-1.4 demo"), "proposal.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    response = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = response.get_data(as_text=True)

    assert "Present 预览区" not in html
    assert 'id="attachment-preview-modal"' in html
    assert 'data-preview-url="/attachments/' in html
    assert 'class="preview-frame"' not in html
    assert 'target="_blank" href="/attachments/' not in html


def test_attachment_preview_modal_is_fullscreen_and_edge_resizable(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    login(client)
    client.post(
        f"/topics/{topic_id}/attachments",
        data={"file": (io.BytesIO(b"%PDF-1.4 demo"), "proposal.pdf")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    response = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = response.get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert 'data-resizable-preview="1"' in html
    for direction in ("n", "s", "e", "w", "ne", "nw", "se", "sw"):
        assert f'data-preview-resize="{direction}"' in html
    assert ".preview-modal { position: fixed; inset: 0; z-index: 50; display: none; align-items: center; justify-content: center; padding: 0;" in css
    assert "width: 100vw;" in css
    assert "height: 100vh;" in css
    assert "max-width: 100vw;" in css
    assert "max-height: 100vh;" in css
    assert "border-radius: 0;" in css
    assert ".preview-modal-body { background: #eef1f5; min-height: 0; padding: 8px;" in css
    assert ".preview-resize-handle" in css
    assert "startPreviewResize" in html
    assert "applyPreviewResize" in html


def test_office_attachment_previews_with_kkfileview_and_source_token(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "brief.docx").write_bytes(b"docx bytes")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="brief.docx",
            stored_filename="brief.docx",
            file_type="docx",
            file_size=9,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login(client)
    preview = client.get(f"/attachments/{attachment_id}/preview")

    assert preview.status_code == 302
    assert preview.headers["Location"].startswith("http://kk.example:8012/onlinePreview?url=")
    source_url = decode_kk_preview_source(preview.headers["Location"])
    assert source_url.startswith(f"http://qbp.example:5008/attachments/{attachment_id}/fileview-source")
    assert "fullfilename=brief.docx" in source_url

    token = parse_qs(urlsplit(source_url).query)["token"][0]
    source = client.get(f"/attachments/{attachment_id}/fileview-source?token={token}&fullfilename=brief.docx")

    assert source.status_code == 200
    assert source.get_data() == b"docx bytes"
    assert "inline" in source.headers["Content-Disposition"]


def test_office_attachment_falls_back_to_direct_inline_when_kkfileview_disabled(client, app):
    app.config["KKFILEVIEW_ENABLED"] = False
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "fallback.pdf").write_bytes(b"%PDF-1.4 fallback")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="fallback.pdf",
            stored_filename="fallback.pdf",
            file_type="pdf",
            file_size=17,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login(client)
    preview = client.get(f"/attachments/{attachment_id}/preview")

    assert preview.status_code == 200
    assert preview.mimetype == "application/pdf"
    assert b"%PDF-1.4 fallback" in preview.data


def test_powerpoint_attachment_converts_to_inline_pdf_when_kkfileview_disabled(client, app):
    app.config["KKFILEVIEW_ENABLED"] = False
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "deck.pptx").write_bytes(b"pptx bytes")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="deck.pptx",
            stored_filename="deck.pptx",
            file_type="pptx",
            file_size=10,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login(client)
    preview = client.get(f"/attachments/{attachment_id}/preview")

    assert preview.status_code == 200
    assert preview.mimetype == "application/pdf"
    assert b"%PDF-1.4 converted from powerpoint" in preview.data
    assert "inline" in preview.headers["Content-Disposition"]
    assert "deck.pdf" in preview.headers["Content-Disposition"]


def test_fileview_source_rejects_invalid_token_and_revoked_permission(client, app):
    with app.app_context():
        alice = create_user("alice")
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic.created_by = alice.id
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "private.pdf").write_bytes(b"%PDF-1.4 private")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="private.pdf",
            stored_filename="private.pdf",
            file_type="pdf",
            file_size=16,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login_as(client, "alice")
    preview = client.get(f"/attachments/{attachment_id}/preview")
    source_url = decode_kk_preview_source(preview.headers["Location"])
    token = parse_qs(urlsplit(source_url).query)["token"][0]
    assert client.get(f"/attachments/{attachment_id}/fileview-source?token=bad").status_code == 403
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic.created_by = User.query.filter_by(username="admin").one().id
        db.session.commit()
    assert client.get(f"/attachments/{attachment_id}/fileview-source?token={token}").status_code == 403


def test_image_attachment_preview_stays_local(client, app):
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "diagram.png").write_bytes(b"png bytes")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="diagram.png",
            stored_filename="diagram.png",
            file_type="png",
            file_size=9,
        )
        db.session.add(attachment)
        db.session.commit()
        attachment_id = attachment.id

    login(client)
    preview = client.get(f"/attachments/{attachment_id}/preview")

    assert preview.status_code == 200
    assert preview.mimetype == "image/png"


def test_create_app_creates_sqlite_parent_directory(tmp_path):
    db_path = tmp_path / "missing_instance" / "startup.db"
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
        }
    )

    with app.app_context():
        db.create_all()

    assert db_path.exists()


def test_create_app_resolves_relative_storage_paths_from_project_root():
    app = create_app(
        {
            "UPLOAD_FOLDER": "tests/.pytest-tmp/relative-uploads",
            "POWERPOINT_PREVIEW_FOLDER": "tests/.pytest-tmp/relative-previews",
        }
    )

    assert app.config["UPLOAD_FOLDER"].is_absolute()
    assert app.config["POWERPOINT_PREVIEW_FOLDER"].is_absolute()
    assert app.config["UPLOAD_FOLDER"] == Path("tests/.pytest-tmp/relative-uploads").resolve()
    assert app.config["POWERPOINT_PREVIEW_FOLDER"] == Path("tests/.pytest-tmp/relative-previews").resolve()


def test_upload_limit_is_100mb(app):
    assert app.config["MAX_CONTENT_LENGTH"] == 100 * 1024 * 1024


def test_existing_sqlite_database_gets_lightweight_schema_upgrade(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            display_name VARCHAR(100) NOT NULL,
            role VARCHAR(30) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            meeting_no VARCHAR(20) NOT NULL UNIQUE,
            title VARCHAR(200) NOT NULL,
            meeting_date DATE NOT NULL,
            location VARCHAR(200),
            host VARCHAR(100),
            status VARCHAR(20) NOT NULL,
            created_by INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY,
            meeting_id INTEGER NOT NULL,
            title VARCHAR(200) NOT NULL,
            category VARCHAR(100),
            owner VARCHAR(100),
            background TEXT,
            purpose TEXT,
            present_order INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
        }
    )

    with app.app_context():
        app.ensure_database()
        user_columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(users)")}
        meeting_columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(meetings)")}
        topic_columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(topics)")}
        tables = {row[0] for row in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
        legacy_topic_no = sqlite3.connect(db_path).execute("SELECT topic_no FROM topics WHERE id = 1").fetchone()[0]

    assert {
        "enabled",
        "email",
        "lark_open_id",
        "lark_user_id",
        "lark_synced_at",
    } <= user_columns
    assert "host_user_id" in meeting_columns
    assert {
        "requested_meeting_id",
        "created_by",
        "workflow_status",
        "submitted_at",
        "reviewed_by",
        "reviewed_at",
        "review_comment",
        "topic_no",
        "duration_minutes",
        "decision_status",
        "decision_by",
        "decision_at",
        "decision_comment",
    } <= topic_columns
    assert "meeting_favorites" in tables
    assert legacy_topic_no == "T00000001"


def test_disabled_user_cannot_login(client, app):
    with app.app_context():
        create_user("disabled_buyer", enabled=False)

    response = login_as(client, "disabled_buyer", follow_redirects=False)

    assert response.status_code == 200
    protected = client.get("/meetings")
    assert protected.status_code == 302
    assert "/auth/login" in protected.headers["Location"]


def test_normal_user_cannot_view_meetings_or_details_without_own_topics(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = demo_meeting()
        admin_topic = meeting.topics.first()
        admin_topic.title = "Admin Confidential Supplier Review"
        admin_topic.background = "SECRET_BACKGROUND_FROM_ADMIN"
        admin_topic.purpose = "SECRET_PURPOSE_FROM_ADMIN"
        attachment = Attachment(
            topic_id=admin_topic.id,
            original_filename="admin-secret.pdf",
            stored_filename="admin-secret.pdf",
            file_type="pdf",
            file_size=7,
        )
        upload_dir = app.config["UPLOAD_FOLDER"] / str(meeting.id) / str(admin_topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "admin-secret.pdf").write_bytes(b"%PDF-1.4")
        db.session.add(attachment)
        db.session.commit()
        meeting_no = meeting.meeting_no
        attachment_id = attachment.id

    login_as(client, "alice")

    meeting_list = client.get("/meetings")
    meeting_list_html = meeting_list.get_data(as_text=True)
    assert meeting_list.status_code == 200
    assert meeting_no not in meeting_list_html

    assert client.get("/meetings/create").status_code == 403
    assert client.get("/admin/users").status_code == 403
    assert f"/meetings/{meeting_no}/edit" not in meeting_list_html
    assert f"/meetings/{meeting_no}/delete" not in meeting_list_html
    assert client.get(f"/meetings/{meeting_no}/edit").status_code == 403
    assert client.post(f"/meetings/{meeting_no}/delete").status_code == 403

    detail = client.get(f"/meetings/{meeting_no}")
    assert detail.status_code == 403

    assert client.get(f"/attachments/{attachment_id}/download").status_code == 403


def test_topic_draft_submit_approve_and_visibility_flow(client, app):
    with app.app_context():
        alice = create_user("alice")
        bob = create_user("bob")
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        meeting_id = meeting.id
        alice_id = alice.id
        bob_id = bob.id

    login_as(client, "alice")
    create_response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Alice Cost Saving Topic",
            "category": "Service",
            "owner": "Alice",
            "background": "Alice private background",
            "purpose": "Alice private purpose",
            "requested_meeting_id": str(meeting_id),
        },
        follow_redirects=True,
    )

    assert create_response.status_code == 200
    with app.app_context():
        draft = Topic.query.filter_by(title="Alice Cost Saving Topic").one()
        assert draft.created_by == alice_id
        assert draft.meeting_id is None
        assert draft.workflow_status == "draft"
        draft_id = draft.id

    submit_response = client.post(f"/topics/drafts/{draft_id}/submit", follow_redirects=True)
    assert submit_response.status_code == 200
    with app.app_context():
        draft = db.session.get(Topic, draft_id)
        assert draft.workflow_status == "submitted"
        assert draft.requested_meeting_id == meeting_id
        assert draft.meeting_id is None

    client.get("/auth/logout")
    login(client)
    review_page = client.get(f"/meetings/{meeting_no}")
    review_html = review_page.get_data(as_text=True)
    assert "Alice Cost Saving Topic" in review_html
    assert f"/topics/{draft_id}/approve" in review_html

    approve_response = client.post(f"/topics/{draft_id}/approve", follow_redirects=True)
    assert approve_response.status_code == 200
    with app.app_context():
        approved = db.session.get(Topic, draft_id)
        assert approved.workflow_status == "approved"
        assert approved.meeting_id == meeting_id

        bob_topic = Topic(
            meeting_id=meeting_id,
            title="Bob Confidential Topic",
            category="Raw Material",
            owner="Bob",
            background="Bob hidden background",
            purpose="Bob hidden purpose",
            present_order=99,
            status="pending",
            created_by=bob_id,
            workflow_status="approved",
        )
        db.session.add(bob_topic)
        db.session.commit()

    client.get("/auth/logout")
    login_as(client, "alice")
    detail = client.get(f"/meetings/{meeting_no}")
    html = detail.get_data(as_text=True)

    assert "Alice Cost Saving Topic" in html
    assert "Alice private background" in html
    assert "Bob Confidential Topic" not in html
    assert "Bob hidden background" not in html


def test_topic_draft_workspace_create_page_has_optional_material_upload(client, app):
    with app.app_context():
        create_user("alice")
        meeting = demo_meeting()

    login_as(client, "alice")
    page = client.get("/topics/drafts/create")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert 'class="draft-topic-workspace"' in html
    assert 'id="topic-editor-form"' in html
    assert 'enctype="multipart/form-data"' in html
    assert "可选上传材料" in html
    assert 'type="file" name="file"' in html
    assert "创建议题" in html
    assert "创建议题后可继续上传附件、授权查看并提交审批" in html
    main_area = html[html.index('class="draft-topic-main"') : html.index('<aside class="draft-topic-side">')]
    assert "议题附件" in main_area
    assert "可选上传材料" in main_area
    assert "draft-create-upload-row" in main_area
    assert 'id="draft-initial-file"' in main_area
    assert 'for="draft-initial-file"' in main_area
    assert "file-upload-shell" in main_area
    assert "file-upload-button" in main_area
    assert "未选择任何文件" in main_area
    assert "上传材料" in main_area
    assert "上传并创建议题" not in main_area

    create_without_file = client.post(
        "/topics/drafts/create",
        data={
            "title": "No Material Draft",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "background": "background",
            "purpose": "purpose",
            "requested_meeting_id": str(meeting.id),
        },
        follow_redirects=True,
    )
    assert create_without_file.status_code == 200
    with app.app_context():
        topic = Topic.query.filter_by(title="No Material Draft").one()
        assert topic.attachments.count() == 0


def test_topic_draft_create_can_attach_initial_material(client, app):
    with app.app_context():
        create_user("alice")
        meeting = demo_meeting()
        meeting_id = meeting.id

    login_as(client, "alice")
    response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Initial Material Draft",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "background": "background",
            "purpose": "purpose",
            "requested_meeting_id": str(meeting_id),
            "file": (io.BytesIO(b"%PDF-1.4 initial material"), "initial-material.pdf"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Initial Material Draft" in html
    assert "initial-material.pdf" in html
    with app.app_context():
        topic = Topic.query.filter_by(title="Initial Material Draft").one()
        attachment = Attachment.query.filter_by(topic_id=topic.id, original_filename="initial-material.pdf").one()
        assert attachment.file_type == "pdf"
        assert (
            app.config["UPLOAD_FOLDER"]
            / str(topic.meeting_id or topic.requested_meeting_id or "draft")
            / str(topic.id)
            / attachment.stored_filename
        ).exists()


def test_topic_draft_create_rejects_invalid_initial_material_without_creating_topic(client, app):
    with app.app_context():
        create_user("alice")

    login_as(client, "alice")
    response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Bad Material Draft",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "Alice",
            "background": "background",
            "purpose": "purpose",
            "file": (io.BytesIO(b"not allowed"), "material.exe"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "不支持的文件类型" in html
    with app.app_context():
        assert Topic.query.filter_by(title="Bad Material Draft").first() is None


def test_topic_draft_workspace_existing_page_unifies_actions_and_sections(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = demo_meeting()
        topic = Topic(
            title="Workspace Draft",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            background="background",
            purpose="purpose",
            created_by=alice.id,
            requested_meeting_id=meeting.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id

    login_as(client, "alice")
    response = client.get(f"/topics/drafts/{topic_id}/edit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'class="draft-topic-workspace"' in html
    assert 'class="draft-action-panel"' in html
    assert 'name="draft_action" value="save"' in html
    assert 'name="draft_action" value="submit"' in html
    assert "保存草稿" in html
    assert "提交审批" in html
    assert "议题附件" in html
    assert "授权查看" in html
    assert "提交流程" not in html
    main_area = html[html.index('class="draft-topic-main"') : html.index('<aside class="draft-topic-side">')]
    side_area = html[html.index('<aside class="draft-topic-side">') :]
    assert "议题附件" in main_area
    assert "议题附件" not in side_area
    assert "draft-upload-form" in main_area
    assert "file-upload-shell" in main_area
    assert "file-upload-button" in main_area
    assert "未选择任何文件" in main_area
    assert 'type="file" name="file"' in main_area
    assert 'class="visually-hidden-file"' in main_area
    assert "上传</button>" not in main_area


def test_topic_draft_submit_action_saves_current_fields_before_submitting(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = demo_meeting()
        topic = Topic(
            title="Old Submit Draft",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            background="old background",
            purpose="old purpose",
            created_by=alice.id,
            requested_meeting_id=meeting.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id
        meeting_id = meeting.id
        plan_version_id = meeting.plan_version_id
        plan_round_id = meeting.plan_round_id
        meeting_category = meeting.category

    login_as(client, "alice")
    response = client.post(
        f"/topics/drafts/{topic_id}/edit",
        data={
            "draft_action": "submit",
            "title": "Updated Submit Draft",
            "category": meeting_category,
            "plan_version_id": str(plan_version_id),
            "plan_round_id": str(plan_round_id),
            "owner": "Alice Updated",
            "background": "new background before submit",
            "purpose": "new purpose before submit",
            "requested_meeting_id": str(meeting_id),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.workflow_status == "submitted"
        assert topic.title == "Updated Submit Draft"
        assert topic.category == meeting_category
        assert topic.plan_version == "Q3 26BP"
        assert topic.plan_version_id == plan_version_id
        assert topic.plan_round_id == plan_round_id
        assert topic.owner == "Alice Updated"
        assert topic.background == "new background before submit"
        assert topic.purpose == "new purpose before submit"
        assert topic.requested_meeting_id == meeting_id


def test_topic_drafts_list_shows_completeness_with_agenda_style(client, app):
    with app.app_context():
        alice = create_user("alice")
        topic = Topic(
            title="Completeness Visible Draft",
            category="Kick Off",
            plan_version="Q3 26BP",
            owner="Alice",
            background="Background",
            purpose="Purpose",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.flush()
        topic.created_at = datetime(2026, 1, 2, 3, 4)
        topic.updated_at = datetime(2026, 2, 3, 4, 5)
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="deck.pdf",
                stored_filename="deck.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.commit()
        created_at_text = "2026-01-02 11:04"
        updated_at_text = "2026-02-03 12:05"

    login_as(client, "alice")
    response = client.get("/topics/drafts")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "完善度" in html
    assert "Completeness Visible Draft" in html
    assert 'class="agenda-readiness ready"' in html
    assert "完善度 80" in html
    assert 'class="readiness-bar"' in html
    assert "创建时间" not in html
    assert 'aria-label="创建时间 从"' not in html
    assert 'data-date-range-label="创建时间"' not in html
    assert created_at_text not in html
    assert "更新时间" in html
    assert updated_at_text in html
    assert 'aria-label="更新时间 从"' in html


def test_topic_drafts_list_shows_topic_number(client, app):
    with app.app_context():
        alice = create_user("topic_no_alice")
        topic = Topic(
            title="Numbered Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            owner="Alice",
            background="Background",
            purpose="Purpose",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id
        topic_no = topic.topic_no

    login_as(client, "topic_no_alice")
    response = client.get("/topics/drafts")
    html = response.get_data(as_text=True)
    row_html = html[html.index("Numbered Topic") - 260 : html.index("Numbered Topic") + 260]

    assert topic_no == f"T{topic_id:08d}"
    assert "<th>议题编号</th>" in html
    assert topic_no in row_html
    assert row_html.index(topic_no) < row_html.index("Numbered Topic")


def test_topic_drafts_list_does_not_show_shared_access_badge(client, app):
    with app.app_context():
        alice = create_user("alice_shared")
        bob = create_user("bob_shared")
        topic = Topic(
            title="Shared Topic Without List Badge",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            background="Background",
            purpose="Purpose",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.flush()
        db.session.add(TopicShare(topic_id=topic.id, user_id=bob.id, granted_by=alice.id))
        db.session.commit()
        topic_id = topic.id

    login_as(client, "bob_shared")
    list_response = client.get("/topics/drafts")
    list_html = list_response.get_data(as_text=True)

    assert list_response.status_code == 200
    assert "Shared Topic Without List Badge" in list_html
    assert "授权查看" not in list_html

    detail_response = client.get(f"/topics/drafts/{topic_id}/edit")
    detail_html = detail_response.get_data(as_text=True)
    assert detail_response.status_code == 200
    assert "授权查看" in detail_html


def test_topic_drafts_list_shows_workflow_status_as_plain_text(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.filter_by(title="OP Cum Yields").one()
        topic.decision_status = "rejected"
        topic_id = topic.id
        db.session.commit()

    response = client.get("/topics/drafts")
    html = response.get_data(as_text=True)
    row_start = html.index("OP Cum Yields")
    row_end = html.index("</tr>", row_start)
    row_html = html[row_start:row_end]

    assert response.status_code == 200
    assert f'href="/topics/drafts/{topic_id}/edit"' in row_html
    assert "<td>已通过</td>" in row_html
    assert "入会审批：" not in row_html
    assert "badge approved" not in row_html
    assert "现场决策" not in row_html
    assert "已驳回" not in row_html


def test_topic_draft_detail_shows_completeness_and_missing_items(client, app):
    with app.app_context():
        alice = create_user("alice")
        topic = Topic(
            title="Completeness Detail Draft",
            category="Kick Off",
            plan_version="Q4 26BP",
            owner="Alice",
            background="Background",
            purpose="",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id

    login_as(client, "alice")
    response = client.get(f"/topics/drafts/{topic_id}/edit")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "完善度" in html
    assert "完善度 30" in html
    assert "待补充：背景/目的、附件、材料 Review" in html
    assert 'class="agenda-readiness risk"' in html
    assert 'class="readiness-bar"' in html
    assert "draft-completeness-box" not in html
    assert html.count("draft-completeness-summary") == 1


def test_admin_home_links_to_topic_approval_queue(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = demo_meeting()
        meeting_id = meeting.id
        topic = Topic(
            title="Approval Queue Topic",
            category="Kick Off",
            owner="Alice",
            background="Needs approval from admin page",
            purpose="Make approval easy to find",
            created_by=alice.id,
            requested_meeting_id=meeting_id,
            workflow_status="submitted",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id

    login(client)
    dashboard = client.get("/admin")
    dashboard_html = dashboard.get_data(as_text=True)
    assert dashboard.status_code == 200
    assert "/admin/topic-approvals" in dashboard_html
    assert "去审批" in dashboard_html

    approvals = client.get("/admin/topic-approvals")
    approvals_html = approvals.get_data(as_text=True)
    assert approvals.status_code == 200
    assert "Approval Queue Topic" in approvals_html
    assert "Needs approval from admin page" in approvals_html
    assert f"/topics/{topic_id}/approve" in approvals_html

    approve = client.post(f"/topics/{topic_id}/approve", follow_redirects=True)
    assert approve.status_code == 200
    with app.app_context():
        approved = db.session.get(Topic, topic_id)
        assert approved.workflow_status == "approved"
        assert approved.meeting_id == meeting_id


def test_user_can_withdraw_submitted_draft(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = demo_meeting()
        topic = Topic(
            title="Withdraw Me",
            category="Service",
            owner="Alice",
            background="draft",
            purpose="draft",
            created_by=alice.id,
            requested_meeting_id=meeting.id,
            workflow_status="submitted",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id

    login_as(client, "alice")
    response = client.post(f"/topics/drafts/{topic_id}/withdraw", follow_redirects=True)

    assert response.status_code == 200
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.workflow_status == "withdrawn"
        assert topic.meeting_id is None


def test_admin_user_management_create_update_reset_password(client, app):
    login(client)
    with app.app_context():
        tpe_group = Group.query.filter_by(code="mc").one()
        tpe_group_id = tpe_group.id

    create_response = client.post(
        "/admin/users/create",
        data={
            "username": "charlie",
            "display_name": "Charlie Buyer",
            "password": "charlie123",
            "role": "user",
            "group_id": str(tpe_group_id),
            "enabled": "1",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200

    with app.app_context():
        charlie = User.query.filter_by(username="charlie").one()
        assert charlie.role == "user"
        assert charlie.enabled is True
        assert charlie.group_id == tpe_group_id
        charlie_id = charlie.id

    update_response = client.post(
        f"/admin/users/{charlie_id}/update",
        data={"display_name": "Charlie Admin", "role": "admin"},
        follow_redirects=True,
    )
    assert update_response.status_code == 200

    reset_response = client.post(
        f"/admin/users/{charlie_id}/reset_password",
        data={"password": "newpass123"},
        follow_redirects=True,
    )
    assert reset_response.status_code == 200

    with app.app_context():
        charlie = db.session.get(User, charlie_id)
        assert charlie.role == "admin"
        assert charlie.enabled is False

    client.get("/auth/logout")
    disabled_login = login_as(client, "charlie", "newpass123", follow_redirects=False)
    assert disabled_login.status_code == 200

    login(client)
    client.post(
        f"/admin/users/{charlie_id}/update",
        data={"display_name": "Charlie Admin", "role": "admin", "enabled": "1"},
        follow_redirects=True,
    )
    client.get("/auth/logout")
    enabled_login = login_as(client, "charlie", "newpass123", follow_redirects=False)
    assert enabled_login.status_code == 302


def test_admin_user_create_requires_business_group_for_business_roles(client, app):
    login(client)

    for role, username, message in [
        ("user", "user_without_group", "普通用户必须指定所属用户组"),
        ("group_leader", "leader_without_group", "组长必须指定所属用户组"),
    ]:
        response = client.post(
            "/admin/users/create",
            data={
                "username": username,
                "display_name": username,
                "password": "user123",
                "role": role,
                "enabled": "1",
            },
            follow_redirects=True,
        )

        assert message in response.get_data(as_text=True)
        with app.app_context():
            assert User.query.filter_by(username=username).first() is None


def test_admin_user_create_rejects_qbp_group_for_business_roles(client, app):
    login(client)
    with app.app_context():
        qbp_group = Group.query.filter_by(code="qbp").one()
        qbp_group_id = qbp_group.id

    response = client.post(
        "/admin/users/create",
        data={
            "username": "user_in_qbp",
            "display_name": "User In QBP",
            "password": "user123",
            "role": "user",
            "group_id": str(qbp_group_id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    assert "普通用户和组长只能归属用户组管理中的业务用户组" in response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="user_in_qbp").first() is None


def test_admin_user_create_form_scopes_group_options_by_selected_role(client, app):
    login(client)

    response = client.get("/admin/users")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert '<select name="group_id" data-role-scoped-group-select>' in html
    assert '<option value="" data-role-scope="admin" hidden disabled>不归属用户组</option>' in html
    assert re.search(r'<option[^>]+data-role-scope="admin"[^>]*>PLN/BP</option>', html)
    for group_name in ["MC", "OP", "PDC", "TD", "PLN/SP", "PLN/NPP", "PLN/BE IE", "PLN/AP CIE"]:
        assert re.search(rf'<option[^>]+data-role-scope="business"[^>]*>{re.escape(group_name)}</option>', html)
    assert not re.search(r'<option[^>]+data-role-scope="business"[^>]*>Q3 26BP</option>', html)
    assert not re.search(r'<option[^>]+data-role-scope="business"[^>]*>Q4 26BP</option>', html)
    assert not re.search(r'<option[^>]+data-role-scope="business"[^>]*>Q1 27BP</option>', html)
    assert not re.search(r'<option[^>]+data-role-scope="business"[^>]*>Q2 27BP</option>', html)
    assert 'roleSelect.value === "admin"' in html
    assert 'data-role-scope") === desiredScope' in html


def test_legacy_bp_version_groups_are_removed_from_defaults(app):
    with app.app_context():
        legacy = Group(code="q1_27bp", name="Q1 27BP", is_admin_group=False)
        db.session.add(legacy)
        db.session.flush()
        user = create_user("legacy_bp_user", group_id=legacy.id)
        legacy_id = legacy.id
        user_id = user.id
        db.session.commit()

        Group.seed_defaults()

        assert db.session.get(Group, legacy_id) is None
        assert db.session.get(User, user_id).group_id is None
        names = [group.name for group in Group.query.order_by(Group.name.asc()).all()]
        assert "Q1 27BP" not in names


def test_admin_user_group_is_limited_to_qbp_for_admin_role(client, app):
    login(client)
    with app.app_context():
        qbp_group = Group.query.filter_by(code="qbp").one()
        business_group = Group.query.filter_by(code="mc").one()

    create_response = client.post(
        "/admin/users/create",
        data={
            "username": "admin_bad_group",
            "display_name": "Bad Admin Group",
            "password": "admin123",
            "role": "admin",
            "group_id": str(business_group.id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    assert "管理员只能归属 PLN/BP 组或不归属用户组" in create_response.get_data(as_text=True)
    with app.app_context():
        assert User.query.filter_by(username="admin_bad_group").first() is None

    allowed_response = client.post(
        "/admin/users/create",
        data={
            "username": "admin_qbp_group",
            "display_name": "PLN/BP Admin",
            "password": "admin123",
            "role": "admin",
            "group_id": str(qbp_group.id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    assert allowed_response.status_code == 200
    with app.app_context():
        created = User.query.filter_by(username="admin_qbp_group").one()
        assert created.role == "admin"
        assert created.group_id == qbp_group.id


def test_admin_user_update_rejects_non_qbp_group_for_admin_role(client, app):
    login(client)
    with app.app_context():
        admin_user = create_user("future_admin")
        qbp_group = Group.query.filter_by(code="qbp").one()
        business_group = Group.query.filter_by(code="mc").one()
        user_id = admin_user.id

    invalid_response = client.post(
        f"/admin/users/{user_id}/update",
        data={
            "display_name": "Future Admin",
            "role": "admin",
            "group_id": str(business_group.id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    assert "管理员只能归属 PLN/BP 组或不归属用户组" in invalid_response.get_data(as_text=True)
    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.role == "user"
        assert user.group_id is None

    valid_response = client.post(
        f"/admin/users/{user_id}/update",
        data={
            "display_name": "Future Admin",
            "role": "admin",
            "group_id": str(qbp_group.id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    assert valid_response.status_code == 200
    with app.app_context():
        user = db.session.get(User, user_id)
        assert user.role == "admin"
        assert user.group_id == qbp_group.id


def test_admin_users_page_prioritizes_create_user_and_compacts_actions(client, app):
    login(client)
    with app.app_context():
        custom_group = Group(name="Legal", code="custom-legal")
        db.session.add(custom_group)
        db.session.commit()
        group_id = custom_group.id

    response = client.get("/admin/users")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert html.index("+ 新增用户") < html.index("用户组管理")
    assert 'class="user-create-inline"' in html
    assert 'class="btn btn-sm user-create-toggle"' in html
    assert 'class="permission-layout"' in html
    assert 'class="user-list"' in html
    assert 'class="card group-manager"' in html
    assert 'class="user-row"' in html
    assert "/admin/users/create" in html
    assert "/reset_password" in html
    assert "/delete" in html
    assert "/admin/groups/create" in html
    assert f"/admin/groups/{group_id}/update" in html
    assert f"/admin/groups/{group_id}/delete" in html


def test_admin_can_create_and_rename_custom_user_group(client, app):
    login(client)

    create_response = client.post(
        "/admin/groups/create",
        data={"name": "Legal"},
        follow_redirects=True,
    )

    assert create_response.status_code == 200
    html = create_response.get_data(as_text=True)
    assert "用户组管理" in html
    assert "Legal" in html
    assert "/admin/groups/create" in html
    with app.app_context():
        group = Group.query.filter_by(name="Legal").one()
        assert group.code.startswith("custom-")
        assert group.is_admin_group is False
        group_id = group.id
    assert f"/admin/groups/{group_id}/update" in html
    assert f"/admin/groups/{group_id}/delete" in html

    duplicate_response = client.post(
        "/admin/groups/create",
        data={"name": "Legal"},
        follow_redirects=True,
    )
    assert "用户组名称已存在" in duplicate_response.get_data(as_text=True)

    rename_redirect = client.post(
        f"/admin/groups/{group_id}/update",
        data={"name": "Legal Ops"},
        follow_redirects=False,
    )

    assert rename_redirect.status_code == 302
    assert rename_redirect.headers["Location"].endswith(
        "/admin/users?group_status=updated#group-manager"
    )
    rename_response = client.get(rename_redirect.headers["Location"], follow_redirects=True)
    assert rename_response.status_code == 200
    rename_html = rename_response.get_data(as_text=True)
    assert "Legal Ops" in rename_html
    assert 'id="group-manager"' in rename_html
    assert 'class="group-manager-notice alert-success"' not in rename_html
    assert rename_html.count("用户组名称已更新") == 1
    assert 'class="alert-close"' in rename_html
    assert 'aria-label="关闭提示"' in rename_html
    with app.app_context():
        group = db.session.get(Group, group_id)
        assert group.name == "Legal Ops"


def test_default_user_groups_cannot_be_renamed_or_deleted(client, app):
    login(client)
    with app.app_context():
        default_group = Group.query.filter_by(code="mc").one()
        group_id = default_group.id

    rename_response = client.post(
        f"/admin/groups/{group_id}/update",
        data={"name": "New MC"},
        follow_redirects=True,
    )
    delete_response = client.post(
        f"/admin/groups/{group_id}/delete",
        follow_redirects=True,
    )

    assert "默认用户组不能修改名称" in rename_response.get_data(as_text=True)
    assert "默认用户组不能删除" in delete_response.get_data(as_text=True)
    with app.app_context():
        group = db.session.get(Group, group_id)
        assert group.name == "MC"


def test_deleting_custom_user_group_unassigns_members(client, app):
    login(client)
    client.post("/admin/groups/create", data={"name": "Legal"}, follow_redirects=True)
    with app.app_context():
        group = Group.query.filter_by(name="Legal").one()
        user = create_user("legal_user", group_id=group.id)
        group_id = group.id
        user_id = user.id

    delete_response = client.post(
        f"/admin/groups/{group_id}/delete",
        follow_redirects=True,
    )

    assert delete_response.status_code == 200
    assert "用户组已删除" in delete_response.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Group, group_id) is None
        user = db.session.get(User, user_id)
        assert user.group_id is None


def test_admin_can_delete_created_user_without_business_records(client, app):
    login(client)
    with app.app_context():
        tpe_group = Group.query.filter_by(code="mc").one()
        tpe_group_id = tpe_group.id
    client.post(
        "/admin/users/create",
        data={
            "username": "deleteme",
            "display_name": "Delete Me",
            "password": "delete123",
            "role": "user",
            "group_id": str(tpe_group_id),
            "enabled": "1",
        },
        follow_redirects=True,
    )

    with app.app_context():
        user = User.query.filter_by(username="deleteme").one()
        user_id = user.id

    users_page = client.get("/admin/users")
    assert f"/admin/users/{user_id}/delete" in users_page.get_data(as_text=True)

    delete_response = client.post(f"/admin/users/{user_id}/delete", follow_redirects=True)

    assert delete_response.status_code == 200
    with app.app_context():
        assert db.session.get(User, user_id) is None
        audit = AuditLog.query.filter_by(action="delete_user", target_id=user_id).one()
        assert audit.target_label == "deleteme"


def test_admin_audit_pages_are_admin_only(client, app):
    with app.app_context():
        create_user("alice")

    login_as(client, "alice")

    assert client.get("/admin/audit-logs").status_code == 403
    assert client.get("/admin/activity").status_code == 403

    client.get("/auth/logout")
    login(client)
    dashboard = client.get("/admin")
    dashboard_html = dashboard.get_data(as_text=True)

    assert dashboard.status_code == 200
    assert "/admin/audit-logs" in dashboard_html
    assert "/admin/activity" in dashboard_html
    assert "最近操作" in dashboard_html


def test_admin_recent_operations_table_is_centered_and_balanced(client):
    login(client)

    dashboard_html = client.get("/admin").get_data(as_text=True)
    css = Path("frontend/static/css/app.css").read_text(encoding="utf-8")

    assert "admin-recent-card" in dashboard_html
    assert 'class="admin-recent-table"' in dashboard_html
    assert 'class="admin-recent-target"' in dashboard_html
    assert ".admin-recent-table { table-layout: fixed; width: 100%;" in css
    assert ".admin-recent-table th, .admin-recent-table td { width: 25%; vertical-align: middle; text-align: center;" in css
    assert ".admin-recent-table td { padding: 13px 14px;" in css
    assert ".admin-recent-target { display: block; max-width: 100%; overflow: hidden; text-overflow: ellipsis;" in css


def test_audit_log_records_key_business_actions_without_sensitive_data(client, app):
    login(client)
    with app.app_context():
        tpe_group = Group.query.filter_by(code="mc").one()
        tpe_group_id = tpe_group.id

    create_response = client.post(
        "/admin/users/create",
        data={
            "username": "audit_user",
            "display_name": "Audit User",
            "password": "secret123",
            "role": "user",
            "group_id": str(tpe_group_id),
            "enabled": "1",
        },
        follow_redirects=True,
    )
    assert create_response.status_code == 200

    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        meeting_id = meeting.id

    topic_response = client.post(
        f"/meetings/{meeting_no}/topics",
        data={
            "title": "Audit Trail Topic",
            "category": "Kick Off",
            "owner": "Admin",
            "present_order": "9",
        },
        follow_redirects=True,
    )
    assert topic_response.status_code == 200

    minutes_response = client.post(
        f"/meetings/{meeting_no}/minutes",
        data={
            "summary": "Audit summary",
            "decisions": "Audit decision",
            "action_items": "Audit action",
            "meeting_status": "preparing",
        },
        follow_redirects=True,
    )
    assert minutes_response.status_code == 200

    with app.app_context():
        actions = {log.action for log in AuditLog.query.all()}
        user_log = AuditLog.query.filter_by(action="create_user", target_label="audit_user").one()
        topic_log = AuditLog.query.filter_by(action="create_topic", target_label="Audit Trail Topic").one()
        minutes_log = AuditLog.query.filter_by(action="update_minutes", target_id=meeting_id).one()

    assert {"login", "create_user", "create_topic", "update_minutes"} <= actions
    assert "secret123" not in str(user_log.metadata_json)
    assert topic_log.target_type == "topic"
    assert minutes_log.target_type == "meeting"


def test_activity_dashboard_counts_unique_active_users(client, app):
    with app.app_context():
        alice = create_user("alice")
        bob = create_user("bob")
        today = datetime.utcnow()
        this_month = today
        last_month = today - timedelta(days=40)
        db.session.add_all(
            [
                AuditLog(
                    user_id=alice.id,
                    username_snapshot="alice",
                    display_name_snapshot="Alice",
                    role_snapshot="user",
                    action="login",
                    created_at=today,
                ),
                AuditLog(
                    user_id=alice.id,
                    username_snapshot="alice",
                    display_name_snapshot="Alice",
                    role_snapshot="user",
                    action="create_topic",
                    created_at=today,
                ),
                AuditLog(
                    user_id=bob.id,
                    username_snapshot="bob",
                    display_name_snapshot="Bob",
                    role_snapshot="user",
                    action="login",
                    created_at=this_month,
                ),
                AuditLog(
                    user_id=None,
                    username_snapshot="anonymous",
                    display_name_snapshot="anonymous",
                    role_snapshot="anonymous",
                    action="tool_call",
                    created_at=today,
                ),
                AuditLog(
                    user_id=bob.id,
                    username_snapshot="bob",
                    display_name_snapshot="Bob",
                    role_snapshot="user",
                    action="login",
                    created_at=last_month,
                ),
            ]
        )
        db.session.commit()

    login(client)
    response = client.get("/admin/activity")
    html = response.get_data(as_text=True)
    css = (Path(app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert "今日活跃用户" in html
    assert "本月活跃用户" in html
    assert "历史活跃用户" in html
    assert "历史操作总数" in html
    assert 'data-testid="dau">3</' in html
    assert 'data-testid="mau">3</' in html
    assert 'data-testid="total-active-users">3</' in html
    assert 'data-testid="total-operations">5</' in html
    assert 'class="activity-dashboard-grid"' in html
    assert 'class="card activity-chart-card"' in html
    assert 'class="activity-combo-chart"' in html
    assert 'class="activity-axis activity-axis-left"' in html
    assert 'class="activity-axis activity-axis-right"' in html
    assert 'class="activity-operation-bar"' in html
    assert 'class="activity-active-line"' in html
    assert ".activity-user-table { table-layout: fixed;" in css
    assert ".activity-user-table th, .activity-user-table td { width: 25%; vertical-align: middle; text-align: center;" in css
    assert ".activity-user-table td strong, .activity-user-table td .muted { display: block; max-width: 100%;" in css
    assert "Alice" in html
    assert "Bob" in html


def test_audit_log_list_filters_by_user_action_and_keyword(client, app):
    with app.app_context():
        alice = create_user("alice")
        bob = create_user("bob")
        db.session.add_all(
            [
                AuditLog(
                    user_id=alice.id,
                    username_snapshot="alice",
                    display_name_snapshot="Alice",
                    role_snapshot="user",
                    action="create_topic",
                    target_type="topic",
                    target_label="Supplier Risk Topic",
                    request_path="/topics/drafts/create",
                    metadata_json={"title": "Supplier Risk Topic"},
                ),
                AuditLog(
                    user_id=bob.id,
                    username_snapshot="bob",
                    display_name_snapshot="Bob",
                    role_snapshot="user",
                    action="update_minutes",
                    target_type="meeting",
                    target_label="Weekly Meeting",
                    request_path="/meetings/CM/minutes",
                    metadata_json={"meeting_no": "CM"},
                ),
            ]
        )
        db.session.commit()
        alice_id = alice.id

    login(client)
    response = client.get(f"/admin/audit-logs?user_id={alice_id}&action=create_topic&keyword=Supplier")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Supplier Risk Topic" in html
    assert "Alice" in html
    assert "Weekly Meeting" not in html


def test_audit_log_table_centers_rows_and_prioritizes_metadata_column(client, app):
    login(client)
    response = client.get("/admin/audit-logs")
    html = response.get_data(as_text=True)
    css = (Path(app.static_folder) / "css" / "app.css").read_text(encoding="utf-8")

    assert response.status_code == 200
    assert 'class="audit-target-cell"' in html
    assert 'class="audit-target-label"' in html
    assert 'class="audit-target-name muted"' in html
    assert ".audit-table { table-layout: fixed;" in css
    assert ".audit-table th { white-space: nowrap; vertical-align: middle; text-align: center;" in css
    assert ".audit-table td { vertical-align: middle; text-align: center;" in css
    assert ".audit-table { table-layout: fixed; width: 100%; min-width: 1280px;" in css
    assert ".audit-table th:nth-child(1), .audit-table td:nth-child(1) { width: 150px; white-space: nowrap; }" in css
    assert ".audit-table th:nth-child(4), .audit-table td:nth-child(4) { width: 150px; white-space: nowrap; }" in css
    assert ".audit-table th:nth-child(5), .audit-table td:nth-child(5) { width: 210px;" in css
    assert ".audit-table .audit-metadata { width: 46%;" in css
    assert ".audit-target-name { display: block; max-width: 100%;" in css


def test_topic_plan_version_and_round_follow_meeting_scope(client, app):
    login(client)
    with app.app_context():
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        meeting_version_name = meeting.plan_version_name
        meeting_round_name = meeting.plan_round_name

    detail = client.get(f"/meetings/{meeting_no}")
    detail_html = detail.get_data(as_text=True)

    assert "Plan Version" in detail_html
    assert "Round" in detail_html
    assert meeting_version_name in detail_html
    assert meeting_round_name in detail_html
    assert '<select name="procurement_scope">' not in detail_html

    create_response = client.post(
        f"/meetings/{meeting_no}/topics",
        data={
            "title": "Plan Version Topic",
            "category": "POR Review",
            "owner": "Equipment Buyer",
            "present_order": "4",
        },
        follow_redirects=True,
    )

    assert create_response.status_code == 200
    with app.app_context():
        topic = Topic.query.filter_by(title="Plan Version Topic").one()
        assert topic.plan_version == meeting_version_name
        assert topic.plan_round_name == meeting_round_name
        topic_id = topic.id

    client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "Plan Version Topic",
            "category": "ST Meeting",
            "owner": "Equipment Buyer",
            "present_order": "4",
            "status": "ready",
            "background": "背景",
            "purpose": "目的",
        },
        follow_redirects=True,
    )

    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        assert topic.category == meeting.category
        assert topic.plan_version == meeting_version_name
        assert topic.plan_round_name == meeting_round_name


def test_plan_version_and_round_dimensions_are_dynamic(client, app):
    login(client)
    with app.app_context():
        versions = PlanVersion.query.order_by(PlanVersion.sort_order).all()
        assert [version.name for version in versions] == ["Q3 26BP"]
        q3 = versions[0]
        assert [round_item.name for round_item in q3.rounds.order_by(PlanRound.sort_order)] == ["Round 1"]

    create_version = client.post("/plan-versions", json={"name": "Q4 26BP"})
    assert create_version.status_code == 201
    created = create_version.get_json()
    assert created["name"] == "Q4 26BP"
    assert [round_item["name"] for round_item in created["rounds"]] == ["Round 1"]

    create_round = client.post(f"/plan-versions/{created['id']}/rounds", json={})
    assert create_round.status_code == 201
    assert create_round.get_json()["name"] == "Round 2"

    duplicate_version = client.post("/plan-versions", json={"name": "Q4 26BP"})
    assert duplicate_version.status_code == 400
    duplicate_round = client.post(f"/plan-versions/{created['id']}/rounds", json={"name": "Round 2"})
    assert duplicate_round.status_code == 400

    delete_round = client.delete(f"/plan-rounds/{create_round.get_json()['id']}")
    assert delete_round.status_code == 200
    delete_version = client.delete(f"/plan-versions/{created['id']}")
    assert delete_version.status_code == 200
    with app.app_context():
        q4 = db.session.get(PlanVersion, created["id"])
        assert q4 is None


def test_used_plan_version_and_round_cannot_be_deleted(client, app):
    login(client)
    with app.app_context():
        version = PlanVersion.create_with_default_round("Q4 26BP")
        round_item = PlanRound.create_next(version, "Round 2")
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20269992",
            title="Delete Guard Meeting",
            meeting_date=datetime(2026, 6, 15).date(),
            location="PMD 531",
            host="PLN/BP",
            host_user_id=admin.id,
            status="preparing",
            plan_version_id=version.id,
            plan_round_id=round_item.id,
            category="Kick Off",
            created_by=admin.id,
        )
        db.session.add(meeting)
        db.session.commit()
        plan_version_id = version.id
        plan_round_id = round_item.id

    round_response = client.delete(f"/plan-rounds/{plan_round_id}")
    assert round_response.status_code == 400
    assert "已被会议或议题使用" in round_response.get_json()["error"]

    version_response = client.delete(f"/plan-versions/{plan_version_id}")
    assert version_response.status_code == 400
    assert "已被会议或议题使用" in version_response.get_json()["error"]


def test_meeting_and_topic_scope_requires_plan_version_round_and_category(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        q3 = PlanVersion.query.filter_by(name="Q3 26BP").one()
        q3_round = PlanRound.query.filter_by(plan_version_id=q3.id, name="Round 1").one()
        q4 = PlanVersion.create_with_default_round("Q4 26BP")
        q4_round_1 = q4.rounds.filter_by(name="Round 1").one()
        q4_round_2 = PlanRound.create_next(q4)
        q4_meeting = Meeting(
            meeting_no="CM20264001",
            title="Q4 POR Round 2",
            meeting_date=datetime(2026, 7, 1).date(),
            location="PMD 401",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
            plan_version_id=q4.id,
            plan_round_id=q4_round_2.id,
            category="POR Review",
        )
        q3_meeting = Meeting(
            meeting_no="CM20264002",
            title="Q3 POR Round 1",
            meeting_date=datetime(2026, 7, 2).date(),
            location="PMD 402",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
            plan_version_id=q3.id,
            plan_round_id=q3_round.id,
            category="POR Review",
        )
        db.session.add_all([q4_meeting, q3_meeting])
        db.session.commit()
        q4_meeting_id = q4_meeting.id
        q4_round_2_id = q4_round_2.id
        q4_id = q4.id
        q3_meeting_id = q3_meeting.id
        q4_round_1_id = q4_round_1.id

    valid_response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Q4 Round 2 POR Topic",
            "category": "POR Review",
            "plan_version_id": str(q4_id),
            "plan_round_id": str(q4_round_2_id),
            "owner": "Buyer",
            "requested_meeting_id": str(q4_meeting_id),
            "background": "bg",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )
    assert valid_response.status_code == 200

    invalid_response = client.post(
        "/topics/drafts/create",
        data={
            "title": "Wrong Round Topic",
            "category": "POR Review",
            "plan_version_id": str(q4_id),
            "plan_round_id": str(q4_round_1_id),
            "owner": "Buyer",
            "requested_meeting_id": str(q4_meeting_id),
            "background": "bg",
            "purpose": "purpose",
        },
        follow_redirects=True,
    )
    assert "议题与会议的 Plan Version、Round 或类别不一致" in invalid_response.get_data(as_text=True)

    with app.app_context():
        valid_topic = Topic.query.filter_by(title="Q4 Round 2 POR Topic").one()
        assert valid_topic.plan_version_id == q4_id
        assert valid_topic.plan_round_id == q4_round_2_id
        assert valid_topic.requested_meeting_id == q4_meeting_id
        assert Topic.query.filter_by(title="Wrong Round Topic").first() is None

    draft_page = client.get("/topics/drafts/create")
    draft_html = draft_page.get_data(as_text=True)
    assert "Q4 POR Round 2" not in draft_html
    assert "Q3 POR Round 1" not in draft_html

    with app.app_context():
        valid_topic = Topic.query.filter_by(title="Q4 Round 2 POR Topic").one()
        valid_topic.workflow_status = "draft"
        db.session.commit()
        valid_topic_id = valid_topic.id
    edit_page = client.get(f"/topics/drafts/{valid_topic_id}/edit")
    edit_html = edit_page.get_data(as_text=True)
    assert "Q4 POR Round 2" in edit_html
    assert "Q3 POR Round 1" not in edit_html

    response = client.get(
        f"/meetings?plan_version_id={q4_id}&plan_round_id={q4_round_2_id}&topic_category=POR+Review"
    )
    html = response.get_data(as_text=True)
    assert "Q4 POR Round 2" in html
    assert "Q3 POR Round 1" not in html
    assert "Round 2" in html

    with app.app_context():
        valid_topic = db.session.get(Topic, valid_topic_id)
        valid_topic.workflow_status = "submitted"
        db.session.commit()
    approve_response = client.post(f"/topics/{valid_topic_id}/approve", follow_redirects=True)
    assert approve_response.status_code == 200
    with app.app_context():
        valid_topic = db.session.get(Topic, valid_topic_id)
        assert valid_topic.meeting_id == q4_meeting_id
        assert valid_topic.plan_version_id == q4_id
        assert valid_topic.plan_round_id == q4_round_2_id


def test_meeting_list_filters_by_qbp_dimensions_and_shows_readiness(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        q3 = PlanVersion.query.filter_by(name="Q3 26BP").one()
        q3_round = PlanRound.query.filter_by(plan_version_id=q3.id, name="Round 1").one()
        ready_meeting = Meeting(
            meeting_no=Meeting.next_meeting_no(),
            title="Ready QBP Meeting",
            meeting_date=datetime(2026, 6, 3).date(),
            location="PMD 555",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
            plan_version_id=q3.id,
            plan_round_id=q3_round.id,
            category="POR Review",
            created_at=datetime(2026, 5, 20, 10, 0),
            updated_at=datetime(2026, 5, 21, 10, 0),
        )
        risk_meeting = Meeting(
            meeting_no="CM20269999",
            title="Risk QBP Meeting",
            meeting_date=datetime(2026, 6, 10).date(),
            location="QBP War Room / Teams",
            host="管理员",
            host_user_id=admin.id,
            status="draft",
            created_by=admin.id,
            plan_version_id=q3.id,
            plan_round_id=q3_round.id,
            category="Kick Off",
            created_at=datetime(2026, 5, 25, 10, 0),
            updated_at=datetime(2026, 5, 26, 10, 0),
        )
        db.session.add_all([ready_meeting, risk_meeting])
        db.session.flush()
        ready_topic = Topic(
            meeting_id=ready_meeting.id,
            title="Lithography Tool Decision",
            category="POR Review",
            plan_version="Q3 26BP",
            plan_version_id=q3.id,
            plan_round_id=q3_round.id,
            owner="Equipment Buyer",
            background="Background ready",
            purpose="Purpose ready",
            present_order=1,
            status="ready",
            created_by=admin.id,
            workflow_status="approved",
        )
        risk_topic = Topic(
            meeting_id=risk_meeting.id,
            title="Incomplete Q2 27BP Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            plan_version_id=q3.id,
            plan_round_id=q3_round.id,
            owner="",
            background="",
            purpose="",
            present_order=1,
            status="pending",
            created_by=admin.id,
            workflow_status="approved",
        )
        db.session.add_all([ready_topic, risk_topic])
        db.session.flush()
        db.session.add(
            Attachment(
                topic_id=ready_topic.id,
                original_filename="ready-deck.pdf",
                stored_filename="ready-deck.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.add(
            TopicMaterialReview(
                topic_id=ready_topic.id,
                source="hoster",
                result="approved",
                score=95,
                summary="材料完整",
                reviewed_by=admin.id,
            )
        )
        db.session.commit()
        q3_id = q3.id
        q3_round_id = q3_round.id

    response = client.get(
        f"/meetings?location=PMD+555&plan_version_id={q3_id}&plan_round_id={q3_round_id}&topic_category=POR+Review&readiness_status=ready"
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Ready QBP Meeting" in html
    assert "Risk QBP Meeting" not in html
    assert "会议准备度" in html
    assert "Q3 26BP" in html
    assert "<strong>1</strong> 个议题" in html
    assert "POR Review 1" in html
    assert "100%" in html
    assert "已就绪" in html
    assert "全部已审" in html


def test_meeting_list_displays_utc_timestamps_in_local_timezone(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20267777",
            title="Local Time Meeting",
            meeting_date=datetime(2026, 6, 12).date(),
            location="PMD 777",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
            created_at=datetime(2026, 6, 11, 16, 45),
            updated_at=datetime(2026, 6, 11, 16, 45),
        )
        db.session.add(meeting)
        db.session.commit()

    response = client.get("/meetings?search=Local+Time+Meeting")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Local Time Meeting" in html
    assert "2026-06-12 00:45" in html
    assert "2026-06-11 16:45" not in html


def test_meeting_created_date_filter_uses_local_timezone_boundaries(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        included = Meeting(
            meeting_no="CM20267778",
            title="Included Local Midnight Meeting",
            meeting_date=datetime(2026, 6, 12).date(),
            status="preparing",
            created_by=admin.id,
            created_at=datetime(2026, 6, 11, 16, 45),
            updated_at=datetime(2026, 6, 11, 16, 45),
        )
        excluded = Meeting(
            meeting_no="CM20267779",
            title="Excluded Previous Local Day Meeting",
            meeting_date=datetime(2026, 6, 11).date(),
            status="preparing",
            created_by=admin.id,
            created_at=datetime(2026, 6, 11, 15, 59),
            updated_at=datetime(2026, 6, 11, 15, 59),
        )
        db.session.add_all([included, excluded])
        db.session.commit()

    response = client.get("/meetings?created_start=2026-06-12&created_end=2026-06-12")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Included Local Midnight Meeting" in html
    assert "Excluded Previous Local Day Meeting" not in html


def test_meeting_readiness_scores_meeting_setup_topic_materials_and_review(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20264444",
            title="Progressive Readiness Meeting",
            meeting_date=datetime(2026, 6, 12).date(),
            location="PMD 555",
            host="PLN/BP",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
        )
        topic = Topic(
            meeting=meeting,
            title="Progressive Topic",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Buyer",
            present_order=1,
            status="pending",
            workflow_status="approved",
            created_by=admin.id,
        )
        db.session.add_all([meeting, topic])
        db.session.commit()

        summary = meeting_readiness_summary(meeting, [topic])
        assert summary["score"] == 44
        assert summary["status_key"] == "preparing"
        assert summary["gap_label"] == "议题完善度"

        topic.background = "供应商合同即将到期"
        topic.purpose = "确认续约策略"
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="progressive.pdf",
                stored_filename="progressive.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.commit()

        summary = meeting_readiness_summary(meeting, [topic])
        assert summary["score"] == 84
        assert summary["status_key"] == "mostly_ready"
        assert summary["gap_label"] == "议题完善度"

        db.session.add(
            TopicMaterialReview(
                topic_id=topic.id,
                source="hoster",
                result="approved",
                score=96,
                summary="材料完整",
                reviewed_by=admin.id,
            )
        )
        db.session.commit()

        summary = meeting_readiness_summary(meeting, [topic])
        assert summary["score"] == 100
        assert summary["status_key"] == "ready"
        assert summary["gap_label"] == "无"


def test_meeting_readiness_uses_topic_completeness_scores(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20264445",
            title="Completeness Linked Meeting",
            meeting_date=datetime(2026, 6, 13).date(),
            location="PMD 555",
            host="PLN/BP",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
        )
        topic = Topic(
            meeting=meeting,
            title="Completeness Linked Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            owner="Buyer",
            background="Background",
            purpose="Purpose",
            present_order=1,
            status="pending",
            workflow_status="approved",
            created_by=admin.id,
        )
        db.session.add_all([meeting, topic])
        db.session.flush()
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="linked.pdf",
                stored_filename="linked.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.add(
            AppConfig(
                key="topic_completeness",
                value_json={
                    "rules": {
                        "Q3 26BP": {
                            "weights": {
                                "basic_info": 10,
                                "background_purpose": 20,
                                "attachment": 60,
                                "review": 10,
                            },
                            "thresholds": {"ready": 90, "mostly_ready": 70, "preparing": 40},
                        }
                    }
                },
            )
        )
        db.session.commit()

        summary = meeting_readiness_summary(meeting, [topic])

        assert topic_completeness(topic)["score"] == 90
        assert summary["score"] == 92
        assert summary["status_key"] == "ready"
        assert summary["gap_label"] == "议题完善度"


def test_admin_readiness_config_page_is_admin_only_and_shows_defaults(client, app):
    with app.app_context():
        create_user("alice")

    login_as(client, "alice")
    assert client.get("/admin/config/readiness").status_code == 403
    client.get("/auth/logout")

    login(client)
    meetings_html = client.get("/meetings").get_data(as_text=True)
    response = client.get("/admin/config")
    html = response.get_data(as_text=True)

    assert "会议准备度" in meetings_html
    assert response.status_code == 200
    assert "配置表" in html
    assert "会议准备度配置" in html
    assert "议题完善度评分规则" in html
    assert 'class="config-page-divider"' in html
    assert "Plan Version" in html
    assert 'name="scope"' in html
    assert "背景/目的" in html
    assert 'name="meeting_info_weight" value="20"' in html
    assert 'name="topic_list_weight"' not in html
    assert 'name="topic_completeness_weight" value="80"' in html
    assert "平均议题完善度" in html
    assert "所有已审批议题完善度的普通平均值" in html
    assert "议题清单" not in html
    assert 'name="review_weight"' in html
    assert "Topic 编排" not in html
    assert "议题材料" not in html
    assert "材料 Review" in html
    assert 'data-testid="weight-total">100</' in html
    assert 'name="ready_threshold" value="90"' in html
    assert 'name="mostly_ready_threshold" value="70"' in html
    assert 'name="preparing_threshold" value="40"' in html


def test_admin_can_update_readiness_config_and_invalid_values_are_rejected(client, app):
    login(client)

    valid = client.post(
        "/admin/config/readiness",
        data={
            "meeting_info_weight": "25",
            "topic_completeness_weight": "75",
            "ready_threshold": "85",
            "mostly_ready_threshold": "65",
            "preparing_threshold": "35",
        },
        follow_redirects=True,
    )
    html = valid.get_data(as_text=True)

    assert valid.status_code == 200
    assert "配置已保存" in html
    assert 'name="topic_completeness_weight" value="75"' in html
    assert 'name="ready_threshold" value="85"' in html
    with app.app_context():
        config = AppConfig.query.filter_by(key="meeting_readiness").one()
        assert config.value_json["weights"] == {"meeting_info": 25, "topic_completeness": 75}
        assert config.value_json["thresholds"]["ready"] == 85
        assert AuditLog.query.filter_by(action="update_config", target_label="meeting_readiness").one()

    invalid_weight = client.post(
        "/admin/config/readiness",
        data={
            "meeting_info_weight": "20",
            "topic_completeness_weight": "20",
            "ready_threshold": "85",
            "mostly_ready_threshold": "65",
            "preparing_threshold": "35",
        },
        follow_redirects=True,
    )
    assert "权重合计必须等于 100" in invalid_weight.get_data(as_text=True)
    with app.app_context():
        config = AppConfig.query.filter_by(key="meeting_readiness").one()
        assert config.value_json["weights"]["topic_completeness"] == 75

    invalid_threshold = client.post(
        "/admin/config/readiness",
        data={
            "meeting_info_weight": "25",
            "topic_completeness_weight": "75",
            "ready_threshold": "60",
            "mostly_ready_threshold": "70",
            "preparing_threshold": "35",
        },
        follow_redirects=True,
    )
    assert "阈值必须满足：已就绪 > 基本就绪 > 准备中" in invalid_threshold.get_data(as_text=True)
    with app.app_context():
        config = AppConfig.query.filter_by(key="meeting_readiness").one()
        assert config.value_json["thresholds"]["ready"] == 85


def test_admin_config_page_includes_topic_completeness_scope_selector(client, app):
    login(client)
    client.post("/plan-versions", json={"name": "Q4 26BP"})

    page = client.get("/admin/config?scope=Q4+26BP")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "会议准备度配置" in html
    assert "议题完善度评分规则" in html
    assert "选择 Plan Version" in html
    assert 'name="scope"' in html
    assert "切换" not in html
    assert re.search(r'<option value="Q4 26BP"[^>]*selected[^>]*>Q4 26BP</option>', html)
    assert "Q4 26BP 完善度评分规则" in html
    assert "Q1 27BP 完善度评分规则" not in html
    assert 'name="basic_info_weight" value="30"' in html
    assert 'name="attachment_weight" value="20"' in html
    assert 'name="Q4 26BP_basic_info_weight"' not in html

    response = client.post(
        "/admin/config/topic-completeness",
        data={
            "scope": "Q4 26BP",
            "basic_info_weight": "10",
            "background_purpose_weight": "20",
            "attachment_weight": "60",
            "review_weight": "10",
            "ready_threshold": "88",
            "mostly_ready_threshold": "70",
            "preparing_threshold": "40",
        },
        follow_redirects=True,
    )

    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "配置已保存" in html
    assert '<option value="Q4 26BP" selected>Q4 26BP</option>' in html
    assert 'name="attachment_weight" value="60"' in html
    assert 'name="ready_threshold" value="88"' in html
    with app.app_context():
        completeness_config = AppConfig.query.filter_by(key="topic_completeness").one()
        assert completeness_config.value_json["rules"]["Q4 26BP"]["weights"]["attachment"] == 60
        assert completeness_config.value_json["rules"]["Q4 26BP"]["thresholds"]["ready"] == 88
        assert completeness_config.value_json["rules"]["Q3 26BP"]["weights"]["attachment"] == 20
        assert AppConfig.query.filter_by(key="meeting_readiness").first() is None
        assert AuditLog.query.filter_by(action="update_config", target_label="topic_completeness").one()


def test_lark_config_page_is_admin_only_and_saves_masked_credentials(client, app):
    with app.app_context():
        create_user("normal_lark_user")

    login_as(client, "normal_lark_user")
    assert client.get("/admin/lark").status_code == 403
    client.get("/auth/logout")

    login(client)
    page = client.get("/admin/lark")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "飞书配置" in html
    assert 'name="app_id"' in html
    assert 'name="app_secret"' in html
    assert 'href="/admin/lark"' in html

    response = client.post(
        "/admin/lark",
        data={
            "enabled": "1",
            "app_id": "cli_test_app",
            "app_secret": "secret-value",
            "reminder_days": "3",
        },
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "飞书配置已保存" in html
    assert "cli_test_app" in html
    assert "secret-value" not in html
    assert "已配置" in html
    with app.app_context():
        stored = AppConfig.query.filter_by(key="lark").one()
        assert stored.value_json["enabled"] is True
        assert stored.value_json["app_id"] == "cli_test_app"
        assert stored.value_json["app_secret"] == "secret-value"
        assert stored.value_json["reminder_days"] == 3


def test_lark_sync_users_matches_email_only(client, app, monkeypatch):
    class FakeLarkClient:
        def __init__(self, config):
            self.config = config

        def batch_get_user_ids(self, emails=None):
            assert emails == ["buyer@example.com"]
            return {
                "buyer@example.com": {"open_id": "ou_email", "user_id": "u_email"},
            }

    import backend.app as app_module

    monkeypatch.setattr(app_module, "LarkClient", FakeLarkClient)
    login(client)
    with app.app_context():
        email_user = create_user("buyer_email", display_name="Email Buyer")
        email_user.email = "buyer@example.com"
        create_user("buyer_without_email", display_name="No Email Buyer")
        AppConfig.query.filter_by(key="lark").delete()
        db.session.add(AppConfig(key="lark", value_json={"enabled": True, "app_id": "cli_test", "app_secret": "secret"}))
        db.session.commit()
        email_user_id = email_user.id

    response = client.post("/admin/lark/sync-users", follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "已同步 1 个用户" in html
    assert "手机号" not in html
    with app.app_context():
        assert db.session.get(User, email_user_id).lark_open_id == "ou_email"
        assert db.session.get(User, email_user_id).lark_user_id == "u_email"


def test_user_admin_forms_only_show_email_for_lark_lookup(client):
    login(client)

    html = client.get("/admin/users").get_data(as_text=True)

    assert "用于飞书同步" in html
    assert 'name="email"' in html
    assert 'name="mobile"' not in html
    assert "手机号" not in html


def test_lark_missing_material_reminder_sends_to_host_and_topic_creator(client, app, monkeypatch):
    sent_messages = []

    class FakeLarkClient:
        def __init__(self, config):
            self.config = config

        def send_text(self, open_id, text):
            sent_messages.append((open_id, text))
            return {"message_id": f"msg_{open_id}"}

    import backend.app as app_module

    monkeypatch.setattr(app_module, "LarkClient", FakeLarkClient)
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        admin.lark_open_id = "ou_host"
        creator = create_user("topic_creator", display_name="Topic Creator")
        creator.lark_open_id = "ou_creator"
        meeting = Meeting(
            meeting_no=Meeting.next_meeting_no(),
            title="三天后会议",
            meeting_date=(datetime.utcnow() + timedelta(days=3)).date(),
            location="Teams",
            host="Host",
            status="preparing",
            created_by=admin.id,
            host_user_id=admin.id,
        )
        db.session.add(meeting)
        db.session.flush()
        db.session.add(
            Topic(
                meeting_id=meeting.id,
                title="缺附件议题",
                category="Kick Off",
                plan_version="Q3 26BP",
                owner="Topic Creator",
                present_order=1,
                status="pending",
                workflow_status="approved",
                created_by=creator.id,
            )
        )
        db.session.add(AppConfig(key="lark", value_json={"enabled": True, "app_id": "cli_test", "app_secret": "secret", "reminder_days": 3}))
        db.session.commit()

    response = client.post("/admin/lark/send-reminders", follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "已发送 2 条提醒" in html
    assert {open_id for open_id, _ in sent_messages} == {"ou_host", "ou_creator"}
    assert all("三天后会议" in text and "缺附件议题" in text and "附件材料尚未上传" in text for _, text in sent_messages)


def test_topic_completeness_scoring_uses_plan_version_specific_rule(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        PlanVersion.create_with_default_round("Q2 27BP")
        tpe_topic = Topic(
            title="Q3 26BP Attachment Weighted",
            category="Kick Off",
            plan_version="Q3 26BP",
            owner="Buyer",
            background="Background",
            purpose="Purpose",
            created_by=admin.id,
            workflow_status="draft",
            status="pending",
        )
        idp_topic = Topic(
            title="Q2 27BP Attachment Weighted",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Buyer",
            background="Background",
            purpose="Purpose",
            created_by=admin.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add_all([tpe_topic, idp_topic])
        db.session.flush()
        for topic in (tpe_topic, idp_topic):
            db.session.add(
                Attachment(
                    topic_id=topic.id,
                    original_filename=f"{topic.plan_version.lower()}.pdf",
                    stored_filename=f"{topic.plan_version.lower()}.pdf",
                    file_type="pdf",
                    file_size=1024,
                )
            )
        db.session.add(
            AppConfig(
                key="topic_completeness",
                value_json={
                    "rules": {
                        "Q3 26BP": {
                            "weights": {
                                "basic_info": 10,
                                "background_purpose": 20,
                                "attachment": 60,
                                "review": 10,
                            },
                            "thresholds": {"ready": 90, "mostly_ready": 70, "preparing": 40},
                        },
                        "Q2 27BP": {
                            "weights": {
                                "basic_info": 40,
                                "background_purpose": 20,
                                "attachment": 20,
                                "review": 20,
                            },
                            "thresholds": {"ready": 90, "mostly_ready": 70, "preparing": 40},
                        },
                    }
                },
            )
        )
        db.session.commit()
        tpe_id = tpe_topic.id
        idp_id = idp_topic.id

    with app.app_context():
        assert topic_completeness(db.session.get(Topic, tpe_id))["score"] == 90
        assert topic_completeness(db.session.get(Topic, idp_id))["score"] == 80
        assert topic_readiness(db.session.get(Topic, tpe_id))["score"] == 90


def test_agenda_data_uses_topic_completeness_config(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20265555",
            title="Agenda Completeness Meeting",
            meeting_date=datetime(2026, 6, 20).date(),
            location="PMD 555",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
        )
        db.session.add(meeting)
        db.session.flush()
        topic = Topic(
            meeting_id=meeting.id,
            title="Agenda Q3 26BP Completeness",
            category="Kick Off",
            plan_version="Q3 26BP",
            owner="Buyer",
            background="Background",
            purpose="Purpose",
            present_order=1,
            status="pending",
            created_by=admin.id,
            workflow_status="approved",
        )
        db.session.add(topic)
        db.session.flush()
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="agenda.pdf",
                stored_filename="agenda.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.add(
            AppConfig(
                key="topic_completeness",
                value_json={
                    "rules": {
                        "Q3 26BP": {
                            "weights": {
                                "basic_info": 10,
                                "background_purpose": 20,
                                "attachment": 60,
                                "review": 10,
                            },
                            "thresholds": {"ready": 90, "mostly_ready": 70, "preparing": 40},
                        }
                    }
                },
            )
        )
        db.session.commit()

    response = client.get("/agenda/CM20265555/data")
    data = response.get_json()

    assert response.status_code == 200
    assert data["right_column"][0]["title"] == "Agenda Q3 26BP Completeness"
    assert data["right_column"][0]["readiness"]["score"] == 90


def test_readiness_config_changes_scores_and_status_bands(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        meeting = Meeting(
            meeting_no="CM20261234",
            title="Configurable Readiness Meeting",
            meeting_date=datetime(2026, 6, 18).date(),
            location="PMD 555",
            host="管理员",
            host_user_id=admin.id,
            status="preparing",
            created_by=admin.id,
        )
        db.session.add(meeting)
        db.session.flush()
        topic = Topic(
            meeting_id=meeting.id,
            title="Attachment Heavy Topic",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Buyer",
            background="Background",
            purpose="Purpose",
            present_order=1,
            status="pending",
            created_by=admin.id,
            workflow_status="approved",
        )
        db.session.add(topic)
        db.session.flush()
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="deck.pdf",
                stored_filename="deck.pdf",
                file_type="pdf",
                file_size=1024,
            )
        )
        db.session.commit()

    before = client.get("/meetings?readiness_status=mostly_ready")
    assert "84%" in before.get_data(as_text=True)

    client.post(
        "/admin/config/readiness",
        data={
            "meeting_info_weight": "25",
            "topic_completeness_weight": "75",
            "ready_threshold": "85",
            "mostly_ready_threshold": "65",
            "preparing_threshold": "35",
        },
        follow_redirects=True,
    )

    after = client.get("/meetings?readiness_status=ready")
    html = after.get_data(as_text=True)
    assert "Configurable Readiness Meeting" in html
    assert "85%" in html
    assert "已就绪" in html


def test_normal_user_meeting_list_readiness_only_uses_own_topics(client, app):
    with app.app_context():
        alice = create_user("alice")
        bob = create_user("bob")
        meeting = demo_meeting()
        meeting_no = meeting.meeting_no
        alice_topic = Topic(
            meeting_id=meeting.id,
            title="Alice Visible Topic",
            category="Kick Off",
            plan_version="Q1 27BP",
            owner="Alice",
            background="Alice background",
            purpose="Alice purpose",
            present_order=10,
            status="pending",
            created_by=alice.id,
            workflow_status="approved",
        )
        bob_topic = Topic(
            meeting_id=meeting.id,
            title="Bob Hidden Decision",
            category="POR Review",
            plan_version="Q4 26BP",
            owner="Bob",
            background="Bob background",
            purpose="Bob purpose",
            present_order=11,
            status="pending",
            created_by=bob.id,
            workflow_status="approved",
        )
        db.session.add_all([alice_topic, bob_topic])
        db.session.commit()

    login_as(client, "alice")
    response = client.get("/meetings")
    html = response.get_data(as_text=True)
    table_block = html[html.index("会议信息") :]

    assert response.status_code == 200
    assert meeting_no in html
    assert "我的" in table_block
    assert "<strong>1</strong> 个议题" in table_block
    assert "Q3 26BP" in table_block
    assert "Bob Hidden Decision" not in table_block
    assert "Q4 26BP" not in table_block


def test_topic_material_review_permissions_and_readiness_score(client, app):
    with app.app_context():
        create_user("alice")
        meeting = demo_meeting()
        topic = meeting.topics.first()
        topic.category = "提案"
        topic.plan_version = "Q3 26BP"
        topic.owner = "Buyer"
        topic.background = "Background"
        topic.purpose = "Purpose"
        db.session.commit()
        meeting_no = meeting.meeting_no
        topic_id = topic.id

    login_as(client, "alice")
    forbidden = client.post(
        f"/topics/{topic_id}/material-reviews",
        data={"result": "approved", "summary": "普通用户不应能审核"},
    )
    assert forbidden.status_code == 403

    client.get("/auth/logout")
    login(client)
    detail = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    detail_html = detail.get_data(as_text=True)

    assert "材料 Review" in detail_html
    assert f"/topics/{topic_id}/material-reviews" in detail_html
    assert "AI Review" in detail_html

    review_response = client.post(
        f"/topics/{topic_id}/material-reviews",
        data={
            "result": "approved",
            "score": "92",
            "summary": "材料完整，可以上会。",
            "issues": "",
            "suggestions": "保留报价备份。",
        },
        follow_redirects=True,
    )

    assert review_response.status_code == 200
    assert "材料完整，可以上会。" in review_response.get_data(as_text=True)
    with app.app_context():
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id).one()
        assert review.source == "hoster"
        assert review.result == "approved"
        assert review.score == 92


def test_meeting_detail_review_prompt_selector_defaults_to_topic_prompt(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        prompt = AIPrompt(
            name="MC 材料评审模板",
            scope="MC",
            review_goal="按 MC 口径评审材料",
            focus_points="关注独供和 TCO",
            output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
            is_active=True,
            is_default=True,
        )
        db.session.add(prompt)
        db.session.commit()
        topic.review_prompt_id = prompt.id
        db.session.commit()
        meeting_no = topic.meeting.meeting_no
        topic_id = topic.id
        prompt_id = prompt.id

    response = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'name="prompt_id"' in html
    assert "MC 材料评审模板" in html
    assert f'value="{prompt_id}" selected' in html


def test_manual_material_review_binds_prompt_and_persists_snapshot(client, app):
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        prompt = AIPrompt(
            name="MC 人工评审模板",
            scope="MC",
            review_goal="人工也按 MC 模板留痕",
            focus_points="人工重点看报价依据",
            output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
            is_active=True,
            is_default=True,
        )
        db.session.add(prompt)
        db.session.commit()
        topic_id = topic.id
        prompt_id = prompt.id

    response = client.post(
        f"/topics/{topic_id}/material-reviews",
        data={
            "prompt_id": str(prompt_id),
            "result": "approved",
            "score": "93",
            "summary": "按模板看过，材料可上会。",
            "issues": "",
            "suggestions": "保留价格依据。",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id).one()
        assert topic.review_prompt_id == prompt_id
        assert review.prompt_id == prompt_id
        assert review.prompt_name_snapshot == "MC 人工评审模板"
        assert review.prompt_scope_snapshot == "MC"
        assert "人工也按 MC 模板留痕" in review.prompt_content_snapshot


def test_ai_review_route_gracefully_reports_unconfigured(client, app):
    app.config["COPILOT_DEFAULT_MODEL"] = ""
    app.config["ZHISHU_API_KEY"] = ""
    app.config["ZHISHU_CLIENT"] = None
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic_id = topic.id
        meeting_no = topic.meeting.meeting_no

    response = client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert meeting_no in html
    assert "AI Review 暂不可用" in html


def test_ai_review_uses_selected_prompt_and_binds_topic(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":89,"summary":"使用了所选模板",'
                                '"issues":"","suggestions":"无"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        default_prompt = AIPrompt(
            name="MC 默认模板",
            scope="MC",
            review_goal="默认模板不应命中",
            focus_points="默认关注点",
            output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
            is_active=True,
            is_default=True,
        )
        selected_prompt = AIPrompt(
            name="显式选择模板",
            scope="GLOBAL",
            review_goal="显式选择模板目标",
            focus_points="显式选择关注点",
            output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
            is_active=True,
            is_default=False,
        )
        db.session.add_all([default_prompt, selected_prompt])
        db.session.commit()
        topic_id = topic.id
        prompt_id = selected_prompt.id

    response = client.post(
        f"/topics/{topic_id}/material-reviews/ai",
        data={"prompt_id": str(prompt_id)},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "显式选择模板目标" in captured["system"]
    assert "默认模板不应命中" not in captured["system"]
    with app.app_context():
        topic = db.session.get(Topic, topic_id)
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id, source="ai").one()
        assert topic.review_prompt_id == prompt_id
        assert review.prompt_id == prompt_id
        assert review.prompt_name_snapshot == "显式选择模板"
        assert review.prompt_scope_snapshot == "GLOBAL"
        assert "显式选择模板目标" in review.prompt_content_snapshot


def test_ai_review_creates_material_review_when_zhishu_is_configured(client, app):
    class FakeZhishuClient:
        def chat(self, payload):
            assert payload["model"] == "review-model"
            assert "只输出 JSON" in payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":88,"summary":"AI 判断材料基本完整",'
                                '"issues":"缺少备选供应商报价","suggestions":"补充报价对比页"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.background = "背景完整"
        topic.purpose = "目的完整"
        db.session.commit()
        topic_id = topic.id

    response = client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "AI 判断材料基本完整" in html
    with app.app_context():
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id).one()
        assert review.source == "ai"
        assert review.result == "approved"
        assert review.score == 88
        assert review.issues == "缺少备选供应商报价"


def test_ai_review_includes_extracted_attachment_text(app):
    class FakeZhishuClient:
        def __init__(self):
            self.payload = None

        def chat(self, payload):
            self.payload = payload
            user_payload = json.loads(payload["messages"][1]["content"])
            assert any(item["filename"] == "review-deck.pptx" for item in user_payload["attachments"])
            assert "retrieved_material_chunks" in user_payload
            assert "attachment_texts" not in user_payload
            review_text = next(
                item["text"]
                for item in user_payload["retrieved_material_chunks"]
                if item["filename"] == "review-deck.pptx"
            )
            assert "材料中明确写了供应商A价格锁定三个月" in review_text
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":91,"summary":"已读取附件正文",'
                                '"issues":"","suggestions":"无"}'
                            )
                        }
                    }
                ]
            }

    fake_client = FakeZhishuClient()
    app.config["ZHISHU_CLIENT"] = fake_client
    with app.app_context():
        topic = Topic.query.first()
        topic.background = "背景完整"
        topic.purpose = "目的完整"
        upload_dir = app.config["UPLOAD_FOLDER"] / str(topic.meeting_id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        create_pptx(upload_dir / "review-deck.pptx", ["材料中明确写了供应商A价格锁定三个月"])
        db.session.add(
            Attachment(
                topic_id=topic.id,
                original_filename="review-deck.pptx",
                stored_filename="review-deck.pptx",
                file_type="pptx",
                file_size=1024,
            )
        )
        db.session.commit()
        attachment = Attachment.query.filter_by(original_filename="review-deck.pptx").one()
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
                text="Slide 1: 材料中明确写了供应商A价格锁定三个月",
                text_hash="review-hash",
                char_count=28,
            )
        )
        db.session.commit()
        review_data = build_ai_material_review(topic, "review-model")

    assert review_data["summary"] == "已读取附件正文"
    with app.app_context():
        log = MaterialRetrievalLog.query.filter_by(source="ai_review", topic_id=topic.id).one()
        assert log.chunk_ids


def test_ai_review_material_retrieval_is_scoped_to_current_topic(app):
    class FakeZhishuClient:
        def chat(self, payload):
            user_payload = json.loads(payload["messages"][1]["content"])
            serialized = json.dumps(user_payload, ensure_ascii=False)
            assert "本议题材料证据" in serialized
            assert "其他议题材料不应出现" not in serialized
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":90,"summary":"只看到了本议题材料",'
                                '"issues":"","suggestions":"无"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    with app.app_context():
        meeting = Meeting.query.first()
        topic = meeting.topics.first()
        other_topic = Topic(
            meeting_id=meeting.id,
            title="Other Indexed Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            present_order=99,
            background="背景",
            purpose="目的",
        )
        db.session.add(other_topic)
        db.session.flush()
        for target_topic, filename, text_value in (
            (topic, "current-topic.pptx", "本议题材料证据"),
            (other_topic, "other-topic.pptx", "其他议题材料不应出现"),
        ):
            attachment = Attachment(
                topic_id=target_topic.id,
                original_filename=filename,
                stored_filename=filename,
                file_type="pptx",
                file_size=128,
            )
            db.session.add(attachment)
            db.session.flush()
            document = MaterialDocument(
                attachment_id=attachment.id,
                topic_id=target_topic.id,
                meeting_id=meeting.id,
                status="indexed",
                chunk_count=1,
            )
            db.session.add(document)
            db.session.flush()
            db.session.add(
                MaterialChunk(
                    document_id=document.id,
                    attachment_id=attachment.id,
                    topic_id=target_topic.id,
                    meeting_id=meeting.id,
                    chunk_index=1,
                    source_label="Slide 1",
                    text=f"Slide 1: {text_value}",
                    text_hash=f"hash-{target_topic.id}",
                    char_count=len(text_value),
                )
            )
        topic.background = "本议题材料证据"
        topic.purpose = "目的"
        db.session.commit()
        review_data = build_ai_material_review(topic, "review-model")

    assert review_data["summary"] == "只看到了本议题材料"


def test_material_retrieval_prefers_lancedb_vector_matches(app):
    class FakeEmbeddingClient:
        model = "fake-embedding"

        def embed_texts(self, texts):
            return [[0.0, 1.0]]

    class FakeVectorStore:
        def search(self, query_vector, top_k, scope_type, scope_id, visible_topic_ids=None):
            assert query_vector == [0.0, 1.0]
            assert scope_type == "topic"
            assert scope_id == selected_topic_id
            return [
                {"chunk_id": selected_chunk_id, "vector_score": 0.98},
                {"chunk_id": ignored_chunk_id, "vector_score": 0.97},
            ]

    app.config["MATERIAL_RAG_VECTOR_ENABLED"] = True
    app.config["MATERIAL_RAG_EMBEDDING_CLIENT"] = FakeEmbeddingClient()
    app.config["MATERIAL_RAG_VECTOR_STORE"] = FakeVectorStore()
    with app.app_context():
        meeting = Meeting.query.first()
        selected_topic = meeting.topics.first()
        ignored_topic = Topic(
            meeting_id=meeting.id,
            title="Ignored Topic",
            category="Kick Off",
            plan_version="Q3 26BP",
            present_order=88,
        )
        db.session.add(ignored_topic)
        db.session.flush()
        selected_topic_id = selected_topic.id
        selected_attachment = Attachment(
            topic_id=selected_topic.id,
            original_filename="vector-hit.pptx",
            stored_filename="vector-hit.pptx",
            file_type="pptx",
            file_size=128,
        )
        ignored_attachment = Attachment(
            topic_id=ignored_topic.id,
            original_filename="vector-leak.pptx",
            stored_filename="vector-leak.pptx",
            file_type="pptx",
            file_size=128,
        )
        db.session.add_all([selected_attachment, ignored_attachment])
        db.session.flush()
        selected_document = MaterialDocument(
            attachment_id=selected_attachment.id,
            topic_id=selected_topic.id,
            meeting_id=meeting.id,
            status="indexed",
            chunk_count=1,
        )
        ignored_document = MaterialDocument(
            attachment_id=ignored_attachment.id,
            topic_id=ignored_topic.id,
            meeting_id=meeting.id,
            status="indexed",
            chunk_count=1,
        )
        db.session.add_all([selected_document, ignored_document])
        db.session.flush()
        selected_chunk = MaterialChunk(
            document_id=selected_document.id,
            attachment_id=selected_attachment.id,
            topic_id=selected_topic.id,
            meeting_id=meeting.id,
            chunk_index=1,
            source_label="Slide 1",
            text="Slide 1: 语义向量命中的证据",
            text_hash="vector-hit-hash",
            char_count=20,
            embedding_status="indexed",
        )
        ignored_chunk = MaterialChunk(
            document_id=ignored_document.id,
            attachment_id=ignored_attachment.id,
            topic_id=ignored_topic.id,
            meeting_id=meeting.id,
            chunk_index=1,
            source_label="Slide 1",
            text="Slide 1: 其他议题不应泄露",
            text_hash="vector-leak-hash",
            char_count=20,
            embedding_status="indexed",
        )
        db.session.add_all([selected_chunk, ignored_chunk])
        db.session.commit()
        selected_chunk_id = selected_chunk.id
        ignored_chunk_id = ignored_chunk.id

        chunks = retrieve_topic_material_chunks(selected_topic, "语义问题", source="ai_review")

        assert [item["chunk_id"] for item in chunks] == [selected_chunk_id]
        assert chunks[0]["vector_score"] == 0.98
        assert chunks[0]["retrieval_mode"] == "vector"
        log = MaterialRetrievalLog.query.filter_by(source="ai_review", topic_id=selected_topic.id).one()
        assert log.retrieval_mode == "vector"
        assert log.chunk_ids == [selected_chunk_id]
        assert log.scores_json[0]["vector_score"] == 0.98


def test_material_retrieval_falls_back_to_keyword_when_vector_search_fails(app):
    class FakeEmbeddingClient:
        def embed_texts(self, texts):
            return [[1.0, 0.0]]

    class FailingVectorStore:
        def search(self, *args, **kwargs):
            raise RuntimeError("lancedb offline")

    app.config["MATERIAL_RAG_VECTOR_ENABLED"] = True
    app.config["MATERIAL_RAG_EMBEDDING_CLIENT"] = FakeEmbeddingClient()
    app.config["MATERIAL_RAG_VECTOR_STORE"] = FailingVectorStore()
    with app.app_context():
        meeting = Meeting.query.first()
        topic = meeting.topics.first()
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="keyword-fallback.pptx",
            stored_filename="keyword-fallback.pptx",
            file_type="pptx",
            file_size=128,
        )
        db.session.add(attachment)
        db.session.flush()
        document = MaterialDocument(
            attachment_id=attachment.id,
            topic_id=topic.id,
            meeting_id=meeting.id,
            status="indexed",
            chunk_count=1,
        )
        db.session.add(document)
        db.session.flush()
        chunk = MaterialChunk(
            document_id=document.id,
            attachment_id=attachment.id,
            topic_id=topic.id,
            meeting_id=meeting.id,
            chunk_index=1,
            source_label="Slide 1",
            text="Slide 1: 关键词兜底证据",
            text_hash="keyword-fallback-hash",
            char_count=20,
            embedding_status="indexed",
        )
        db.session.add(chunk)
        db.session.commit()

        chunks = retrieve_topic_material_chunks(topic, "关键词兜底", source="ai_review")

        assert chunks[0]["chunk_id"] == chunk.id
        assert chunks[0]["retrieval_mode"] == "keyword_fallback"
        log = MaterialRetrievalLog.query.filter_by(source="ai_review", topic_id=topic.id).one()
        assert log.retrieval_mode == "keyword_fallback"


def test_ai_workshop_prompt_admin_can_save(client, app):
    login(client)
    with app.app_context():
        global_category = AIKnowHowCategory(scope="GLOBAL", name="通用口径")
        tpe_category = AIKnowHowCategory(scope="MC", name="涨价依据")
        db.session.add_all([global_category, tpe_category])
        db.session.commit()
        global_category_id = global_category.id
        tpe_category_id = tpe_category.id

    response = client.post(
        "/ai-workshop/prompt",
        data={
            "name": "MC 设备评审",
            "scope": "MC",
            "review_goal": "判断涨价材料是否可上会",
            "focus_points": "重点看独家供应商和 TCO",
            "knowledge_sources": ["GLOBAL", "MC"],
            "knowledge_category_GLOBAL": str(global_category_id),
            "knowledge_category_MC": str(tpe_category_id),
            "include_score": "1",
            "include_issues": "1",
            "include_suggestions": "1",
            "is_active": "1",
            "set_default": "1",
        },
        follow_redirects=True,
    )
    assert response.status_code == 200
    with app.app_context():
        prompt = AIPrompt.query.filter_by(name="MC 设备评审").one()
        assert prompt.scope == "MC"
        assert prompt.review_goal == "判断涨价材料是否可上会"
        assert prompt.focus_points == "重点看独家供应商和 TCO"
        assert prompt.knowledge_sources == [
            {"scope": "GLOBAL", "category_id": global_category_id},
            {"scope": "MC", "category_id": tpe_category_id},
        ]
        assert prompt.output_options["include_score"] is True
        assert prompt.is_default is True
        log = AuditLog.query.filter_by(action="update_config", target_type="ai_prompt").first()
        assert log is not None


def test_ai_workshop_prompt_page_is_business_form_not_json_editor(client, app):
    login(client)
    with app.app_context():
        db.session.add(AIKnowHowCategory(scope="GLOBAL", name="通用口径"))
        db.session.add(AIKnowHowCategory(scope="MC", name="涨价依据"))
        db.session.commit()

    response = client.get("/ai-workshop/prompt")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "评审目标" in html
    assert "知识来源" in html
    assert "补充评审口径" in html
    assert "输出要求" in html
    assert "通用知识" not in html
    assert "aiw-source-code" in html
    assert "aiw-business-source-select" in html
    assert "aiw-business-category-select" in html
    assert "aiw-source-global" in html
    assert "aiw-business-source-row" in html
    assert "aiw-business-source-option" not in html
    assert "aiw-template-meta" not in html
    assert html.count('class="aiw-business-source-select"') == 1
    assert "涨价依据" in html
    assert 'name="knowledge_category_GLOBAL"' in html
    assert 'name="knowledge_category_MC"' in html
    assert "aiw-source-picker" in html
    assert "全部沉淀" not in html
    assert "aiw-editor-hero" in html
    assert "aiw-editor-grid" in html
    assert "aiw-prompt-section" in html
    assert "aiw-editor-footer" in html
    assert "关注重点（Know-how 注入）" not in html
    assert 'name="include_score"' in html
    assert 'name="include_issues"' in html
    assert 'name="include_suggestions"' in html
    assert 'name="prompt"' not in html
    assert "严格输出 JSON" not in html


def test_ai_workshop_prompt_new_mode_creates_separate_template(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        existing = AIPrompt(
            name="现有模板",
            scope="GLOBAL",
            review_goal="现有目标",
            focus_points="现有口径",
            knowledge_sources=["MC"],
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.session.add(existing)
        db.session.commit()

    new_page = client.get("/ai-workshop/prompt?mode=new")
    html = new_page.get_data(as_text=True)

    assert new_page.status_code == 200
    assert 'name="prompt_id"' not in html
    assert "现有模板" in html

    response = client.post(
        "/ai-workshop/prompt?mode=new",
        data={
            "name": "新建模板",
            "scope": "GLOBAL",
            "review_goal": "新模板目标",
            "focus_points": "新模板口径",
            "knowledge_sources": ["MC", "OP"],
            "include_score": "1",
            "include_issues": "1",
            "include_suggestions": "1",
            "is_active": "1",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with app.app_context():
        assert AIPrompt.query.count() == 2
        assert AIPrompt.query.filter_by(name="现有模板").one()
        created = AIPrompt.query.filter_by(name="新建模板").one()
        assert created.review_goal == "新模板目标"


def test_ai_workshop_prompt_can_delete_selected_template(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        prompt = AIPrompt(
            name="待删除模板",
            scope="MC",
            review_goal="删除前目标",
            focus_points="删除前口径",
            knowledge_sources=["MC"],
            is_active=True,
            is_default=True,
            created_by=admin.id,
            updated_by=admin.id,
        )
        fallback = AIPrompt(
            name="保留模板",
            scope="MC",
            review_goal="保留目标",
            focus_points="保留口径",
            knowledge_sources=["MC"],
            is_active=True,
            is_default=False,
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.session.add_all([prompt, fallback])
        db.session.commit()
        prompt_id = prompt.id

    page = client.get(f"/ai-workshop/prompt?prompt_id={prompt_id}")
    html = page.get_data(as_text=True)

    assert page.status_code == 200
    assert "删除模板" in html
    assert f'action="/ai-workshop/prompt/{prompt_id}/delete"' in html

    response = client.post(f"/ai-workshop/prompt/{prompt_id}/delete", follow_redirects=True)
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "提示词模板已删除" in html
    assert "待删除模板" not in html
    assert "保留模板" in html
    with app.app_context():
        assert db.session.get(AIPrompt, prompt_id) is None
        assert AIPrompt.query.filter_by(name="保留模板").one().is_default is True
        log = AuditLog.query.filter_by(action="delete_ai_prompt", target_type="ai_prompt").one()
        assert log.target_label == "待删除模板"


def test_ai_review_uses_global_default_instead_of_business_group_prompt_by_default(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":91,"summary":"ok",'
                                '"issues":"risk","suggestions":"fix"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        db.session.add(
            AIPrompt(
                name="全局默认",
                scope="GLOBAL",
                review_goal="全局目标默认命中",
                focus_points="全局关注点",
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.add(
            AIPrompt(
                name="MC 专属",
                scope="MC",
                review_goal="MC 专属评审目标",
                focus_points="MC 专属关注点",
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)

    assert "全局目标默认命中" in captured["system"]
    assert "MC 专属评审目标" not in captured["system"]
    assert "JSON" in captured["system"]


def test_ai_review_falls_back_to_global_default_prompt(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"needs_revision","score":51,"summary":"s",'
                                '"issues":"i","suggestions":"x"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q1 27BP"
        topic.background = "背景"
        topic.purpose = "目的"
        db.session.add(
            AIPrompt(
                name="全局默认",
                scope="GLOBAL",
                review_goal="全局兜底目标",
                focus_points="全局兜底关注点",
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)

    assert "全局兜底目标" in captured["system"]
    assert "Q1 27BP" in captured["system"]


def test_ai_review_does_not_auto_match_business_group_prompt_without_explicit_selection(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":82,"summary":"ok",'
                                '"issues":"","suggestions":""}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q4 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        db.session.add(
            AIPrompt(
                name="全局默认",
                scope="GLOBAL",
                review_goal="全局模板应命中",
                focus_points="全局关注点",
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.add(
            AIPrompt(
                name="OP 启用模板",
                scope="OP",
                review_goal="OP 启用模板目标",
                focus_points="OP 关注原材料价格波动",
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=False,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)

    assert "全局模板应命中" in captured["system"]
    assert "OP 启用模板目标" not in captured["system"]


def test_custom_prompt_imports_selected_knowhow_sources(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":86,"summary":"ok",'
                                '"issues":"","suggestions":""}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q2 27BP"
        topic.background = "背景"
        topic.purpose = "目的"
        db.session.add(AIKnowHow(scope="MC", content="MC 设备独供需替代方案"))
        db.session.add(AIKnowHow(scope="OP", content="OP 外包关注 SLA"))
        db.session.add(AIKnowHow(scope="PDC", content="PDC 不应导入"))
        db.session.add(
            AIPrompt(
                name="供应风险专项",
                scope="CUSTOM",
                special_label="供应风险专项",
                review_goal="专项判断供应风险",
                focus_points="只看跨方向供应连续性",
                knowledge_sources=["MC", "OP"],
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)

    assert "专项判断供应风险" in captured["system"]
    assert "MC 设备独供需替代方案" in captured["system"]
    assert "OP 外包关注 SLA" in captured["system"]
    assert "PDC 不应导入" not in captured["system"]
    with app.app_context():
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id, source="ai").first()
        snapshot_scopes = {item["scope"] for item in review.knowhow_snapshot}
        assert snapshot_scopes == {"MC", "OP"}


def test_ai_review_prompt_filters_knowhow_by_selected_category(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":88,"summary":"ok",'
                                '"issues":"-","suggestions":"-"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        global_category = AIKnowHowCategory(scope="GLOBAL", name="通用口径")
        price_category = AIKnowHowCategory(scope="MC", name="涨价依据")
        tco_category = AIKnowHowCategory(scope="MC", name="TCO")
        db.session.add_all([global_category, price_category, tco_category])
        db.session.flush()
        db.session.add(AIKnowHow(scope="GLOBAL", category_id=global_category.id, content="通用材料口径", is_active=True))
        db.session.add(AIKnowHow(scope="MC", category_id=price_category.id, content="涨价必须有指数依据", is_active=True))
        db.session.add(AIKnowHow(scope="MC", category_id=tco_category.id, content="TCO 不应被导入", is_active=True))
        prompt = AIPrompt(
            name="MC 涨价专项",
            scope="MC",
            review_goal="只看涨价依据",
            knowledge_sources=[
                {"scope": "GLOBAL", "category_id": global_category.id},
                {"scope": "MC", "category_id": price_category.id},
            ],
            output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
            is_active=True,
            is_default=True,
            created_by=admin.id,
            updated_by=admin.id,
        )
        db.session.add(prompt)
        db.session.commit()
        topic_id = topic.id
        prompt_id = prompt.id

    client.post(
        f"/topics/{topic_id}/material-reviews/ai",
        data={"prompt_id": str(prompt_id)},
        follow_redirects=True,
    )

    assert "通用材料口径" in captured["system"]
    assert "涨价必须有指数依据" in captured["system"]
    assert "TCO 不应被导入" not in captured["system"]
    with app.app_context():
        review = TopicMaterialReview.query.filter_by(topic_id=topic_id, source="ai").first()
        snapshot = review.knowhow_snapshot
        assert {item["category_name"] for item in snapshot} == {"通用口径", "涨价依据"}


def test_ai_prompt_seed_migrates_legacy_single_prompt(tmp_path):
    db_path = tmp_path / "legacy_prompt.db"
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
            "POWERPOINT_PREVIEW_FOLDER": tmp_path / "previews",
        }
    )
    with app.app_context():
        db.create_all()
        User.create_default_admin()
        db.session.add(
            AppConfig(
                key=AI_REVIEW_PROMPT_KEY,
                value_json={"content": "旧提示词目标 {topic_title} {scope} {knowhow}"},
            )
        )
        db.session.commit()
        app.ensure_database()

        prompt = AIPrompt.query.filter_by(scope="GLOBAL", is_default=True).one()
        assert prompt.name == "通用默认模板"
        assert "旧提示词目标" in prompt.review_goal
        assert prompt.output_options["include_score"] is True


def test_existing_ai_prompt_table_gets_knowledge_sources_column(tmp_path):
    db_path = tmp_path / "legacy_ai_prompt.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            display_name VARCHAR(100) NOT NULL,
            role VARCHAR(30) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE ai_prompts (
            id INTEGER PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            scope VARCHAR(20) NOT NULL,
            special_label VARCHAR(120),
            review_goal TEXT NOT NULL,
            focus_points TEXT,
            output_options TEXT NOT NULL,
            is_active BOOLEAN NOT NULL,
            is_default BOOLEAN NOT NULL,
            created_by INTEGER,
            updated_by INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
            "POWERPOINT_PREVIEW_FOLDER": tmp_path / "previews",
        }
    )
    with app.app_context():
        app.ensure_database()
        columns = {row[1] for row in db.session.execute(text("PRAGMA table_info(ai_prompts)")).fetchall()}

    assert "knowledge_sources" in columns


def test_pln_bp_can_manage_knowhow_scope(client, app):
    with app.app_context():
        tpe_group = Group.query.filter_by(code="qbp").one()
        admin = User.query.filter_by(username="admin").one()
        db.session.add(AIKnowHow(scope="GLOBAL", content="通用沉淀所有人可见", created_by=admin.id))
        category = AIKnowHowCategory(scope="MC", name="价格")
        db.session.add(category)
        create_user("tpe_user", role="user", group_id=tpe_group.id)
        db.session.commit()
        category_id = category.id
    login_as(client, "tpe_user")

    page = client.get("/ai-workshop/knowhow")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert 'name="scope"' in html
    assert '<option value="GLOBAL"' in html
    assert "通用" in html
    assert "aiw-scope-tab" not in html

    global_page = client.get("/ai-workshop/knowhow?scope=GLOBAL")
    global_html = global_page.get_data(as_text=True)
    assert global_page.status_code == 200
    assert "通用沉淀所有人可见" in global_html
    assert "只读模式" not in global_html
    assert 'name="content"' in global_html

    ok = client.post(
        "/ai-workshop/knowhow/create",
        data={"scope": "MC", "category_id": category_id, "content": "MC 关注价格波动"},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    forbidden = client.post(
        "/ai-workshop/knowhow/create",
        data={"scope": "OP", "content": "PLN/BP 尝试写入 OP 但缺分类"},
        follow_redirects=False,
    )
    assert forbidden.status_code in {302, 400}
    ok_global = client.post(
        "/ai-workshop/knowhow/create",
        data={"scope": "GLOBAL", "content": "PLN/BP 可以新增通用"},
        follow_redirects=False,
    )
    assert ok_global.status_code in {302, 400}
    with app.app_context():
        assert AIKnowHow.query.filter_by(scope="MC").count() == 1
        assert AIKnowHow.query.filter_by(scope="OP").count() == 0
        assert AIKnowHow.query.filter_by(scope="GLOBAL").count() == 1


def test_admin_can_manage_global_knowhow(client, app):
    login(client)
    with app.app_context():
        category = AIKnowHowCategory(scope="GLOBAL", name="通用评审口径")
        db.session.add(category)
        db.session.commit()
        category_id = category.id

    response = client.post(
        "/ai-workshop/knowhow/create",
        data={
            "scope": "GLOBAL",
            "category_id": str(category_id),
            "content": "所有Plan Version都要核验数据来源",
            "is_active": "1",
        },
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "所有Plan Version都要核验数据来源" in html
    assert "通用" in html
    with app.app_context():
        entry = AIKnowHow.query.filter_by(scope="GLOBAL").one()
        assert entry.content == "所有Plan Version都要核验数据来源"
        assert entry.category.name == "通用评审口径"
        assert entry.is_active is True


def test_ai_workshop_knowhow_uses_compact_scope_select(client, app):
    login(client)

    response = client.get("/ai-workshop/knowhow?scope=OP")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "aiw-scope-select-form" in html
    assert 'select class="meeting-filter-control aiw-scope-select" name="scope"' in html
    assert '<option value="GLOBAL"' in html
    assert '<option value="OP" selected' in html
    assert "OP 领域知识" in html
    assert "通用领域知识" in html
    assert "aiw-scope-tabs" not in html
    assert "aiw-scope-tab" not in html


def test_ai_workshop_knowhow_uses_visual_category_workspace(client):
    login(client)
    with client.application.app_context():
        category = AIKnowHowCategory(scope="GLOBAL", name="通用口径")
        admin = User.query.filter_by(username="admin").one()
        db.session.add(category)
        db.session.flush()
        db.session.add(
            AIKnowHow(
                scope="GLOBAL",
                category_id=category.id,
                content="通用知识结构测试",
                created_by=admin.id,
                updated_by=admin.id,
            )
        )
        db.session.commit()

    response = client.get("/ai-workshop/knowhow?scope=GLOBAL")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "aiw-knowledge-workspace" in html
    assert "aiw-workspace-bar" in html
    assert "aiw-filter-rail" in html
    assert "aiw-current-line" not in html
    assert "aiw-composer-shell" in html
    assert "aiw-composer-actions" in html
    assert "aiw-entry-compact-row" in html
    assert "aiw-entry-toolbar" in html
    assert "aiw-entry-edit-meta" in html
    assert "记录新知识" in html
    assert "通用 know-how" not in html
    assert "通用口径 <em>" not in html
    assert ">子分类<" not in html
    assert "归属子分类" not in html
    assert "全部子分类" not in html
    assert "aiw-csv-actions" in html
    assert "aiw-icon-only-csv" in html
    assert "导入 CSV</label>" not in html
    assert "导出 CSV</a>" not in html
    assert 'aria-label="导入 CSV"' in html
    assert 'aria-label="导出 CSV"' in html


def test_ai_workshop_knowhow_exports_current_scope_csv(client, app):
    login(client)
    with app.app_context():
        admin = User.query.filter_by(username="admin").one()
        category = AIKnowHowCategory(scope="MC", name="价格")
        db.session.add(category)
        db.session.flush()
        db.session.add(
            AIKnowHow(
                scope="MC",
                category_id=category.id,
                content="MC 导出测试知识",
                is_active=True,
                created_by=admin.id,
                updated_by=admin.id,
            )
        )
        db.session.add(
            AIKnowHow(
                scope="OP",
                content="OP 不应导出",
                is_active=True,
                created_by=admin.id,
                updated_by=admin.id,
            )
        )
        db.session.commit()

    response = client.get("/ai-workshop/knowhow/export?scope=MC")
    text = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert "attachment" in response.headers["Content-Disposition"]
    assert "category,content,is_active" in text.splitlines()[0]
    assert "价格,MC 导出测试知识,1" in text
    assert "OP 不应导出" not in text


def test_ai_workshop_knowhow_imports_csv_and_creates_categories(client, app):
    login(client)
    csv_bytes = "category,content,is_active\n通用口径,所有材料要写明数据来源,1\n风险,未启用知识,0\n".encode("utf-8-sig")

    response = client.post(
        "/ai-workshop/knowhow/import",
        data={"scope": "GLOBAL", "file": (io.BytesIO(csv_bytes), "knowhow.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "已导入 2 条知识" in html
    with app.app_context():
        categories = {category.name: category for category in AIKnowHowCategory.query.filter_by(scope="GLOBAL").all()}
        assert {"通用口径", "风险"} <= set(categories)
        active = AIKnowHow.query.filter_by(scope="GLOBAL", content="所有材料要写明数据来源").one()
        inactive = AIKnowHow.query.filter_by(scope="GLOBAL", content="未启用知识").one()
        assert active.category_id == categories["通用口径"].id
        assert active.is_active is True
        assert inactive.category_id == categories["风险"].id
        assert inactive.is_active is False


def test_group_leader_can_manage_knowhow_subcategories_for_own_scope(client, app):
    with app.app_context():
        tpe_group = Group.query.filter_by(code="mc").one()
        create_user("tpe_leader", role="group_leader", group_id=tpe_group.id)
    login_as(client, "tpe_leader")

    response = client.post(
        "/ai-workshop/knowhow/categories/create",
        data={"scope": "MC", "name": "涨价依据"},
        follow_redirects=False,
    )
    assert response.status_code == 302

    forbidden = client.post(
        "/ai-workshop/knowhow/categories/create",
        data={"scope": "OP", "name": "越权分类"},
        follow_redirects=False,
    )
    assert forbidden.status_code == 403


def test_knowhow_create_requires_subcategory_and_entry_can_toggle_active(client, app):
    login(client)
    with app.app_context():
        category = AIKnowHowCategory(scope="MC", name="TCO")
        db.session.add(category)
        db.session.commit()
        category_id = category.id

    missing_category = client.post(
        "/ai-workshop/knowhow/create",
        data={"scope": "MC", "content": "没有分类不能保存"},
        follow_redirects=True,
    )
    assert "请先选择子分类" in missing_category.get_data(as_text=True)

    create_response = client.post(
        "/ai-workshop/knowhow/create",
        data={"scope": "MC", "category_id": str(category_id), "content": "MC 需要看 TCO"},
        follow_redirects=True,
    )
    html = create_response.get_data(as_text=True)
    assert "MC 需要看 TCO" in html
    assert "TCO" in html
    assert 'name="is_active"' in html

    with app.app_context():
        entry = AIKnowHow.query.filter_by(content="MC 需要看 TCO").one()
        entry_id = entry.id
        assert entry.category_id == category_id
        assert entry.is_active is True

    update_response = client.post(
        f"/ai-workshop/knowhow/{entry_id}/update",
        data={"content": "MC 需要看全生命周期 TCO"},
        follow_redirects=True,
    )
    assert update_response.status_code == 200
    with app.app_context():
        entry = db.session.get(AIKnowHow, entry_id)
        assert entry.content == "MC 需要看全生命周期 TCO"
        assert entry.is_active is False


def test_ai_review_payload_includes_knowhow(client, app):
    captured = {}

    class FakeZhishuClient:
        def chat(self, payload):
            captured["system"] = payload["messages"][0]["content"]
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"approved","score":90,"summary":"ok",'
                                '"issues":"-","suggestions":"-"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        common_category = AIKnowHowCategory(scope="GLOBAL", name="通用口径")
        tpe_category = AIKnowHowCategory(scope="MC", name="价格")
        bep_category = AIKnowHowCategory(scope="OP", name="外包")
        db.session.add_all([common_category, tpe_category, bep_category])
        db.session.flush()
        db.session.add(AIKnowHow(scope="GLOBAL", category_id=common_category.id, content="通用要求：所有材料都要核验数据来源", is_active=True))
        db.session.add(AIKnowHow(scope="MC", category_id=tpe_category.id, content="老板关注 MC 价格", is_active=True))
        db.session.add(AIKnowHow(scope="MC", category_id=tpe_category.id, content="禁用 MC 不应出现", is_active=False))
        db.session.add(AIKnowHow(scope="OP", category_id=bep_category.id, content="不应出现的 OP 条目", is_active=True))
        db.session.add(
            AIPrompt(
                name="全局默认",
                scope="GLOBAL",
                review_goal="全局目标",
                knowledge_sources=["GLOBAL", "MC"],
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)
    assert "通用要求：所有材料都要核验数据来源" in captured["system"]
    assert "老板关注 MC 价格" in captured["system"]
    assert "禁用 MC 不应出现" not in captured["system"]
    assert "不应出现的 OP 条目" not in captured["system"]
    assert "Q3 26BP" in captured["system"]


def test_ai_review_persists_knowhow_snapshot(client, app):
    class FakeZhishuClient:
        def chat(self, payload):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"result":"needs_revision","score":40,"summary":"s",'
                                '"issues":"i","suggestions":"x"}'
                            )
                        }
                    }
                ]
            }

    app.config["ZHISHU_CLIENT"] = FakeZhishuClient()
    app.config["COPILOT_DEFAULT_MODEL"] = "review-model"
    login(client)
    with app.app_context():
        topic = Topic.query.first()
        topic.plan_version = "Q3 26BP"
        topic.background = "背景"
        topic.purpose = "目的"
        common_category = AIKnowHowCategory(scope="GLOBAL", name="通用口径")
        tpe_category = AIKnowHowCategory(scope="MC", name="供应商")
        db.session.add_all([common_category, tpe_category])
        db.session.flush()
        db.session.add(AIKnowHow(scope="GLOBAL", category_id=common_category.id, content="通用沉淀进入快照"))
        db.session.add(AIKnowHow(scope="MC", category_id=tpe_category.id, content="MC 独家供应商需附替代方案"))
        db.session.add(AIKnowHow(scope="MC", category_id=tpe_category.id, content="MC 关注 TCO"))
        db.session.add(AIKnowHow(scope="OP", content="OP 条目不应入快照"))
        db.session.add(
            AIPrompt(
                name="全局默认",
                scope="GLOBAL",
                review_goal="全局目标",
                knowledge_sources=["GLOBAL", "MC"],
                output_options={"include_score": True, "include_issues": True, "include_suggestions": True},
                is_active=True,
                is_default=True,
            )
        )
        db.session.commit()
        topic_id = topic.id

    client.post(f"/topics/{topic_id}/material-reviews/ai", follow_redirects=True)

    with app.app_context():
        review = (
            TopicMaterialReview.query.filter_by(topic_id=topic_id, source="ai")
            .order_by(TopicMaterialReview.id.desc())
            .first()
        )
        assert review is not None
        snapshot = review.knowhow_snapshot or []
        assert isinstance(snapshot, list)
        assert len(snapshot) == 3
        contents = [item["content"] for item in snapshot]
        scopes = {item["scope"] for item in snapshot}
        assert "通用沉淀进入快照" in contents
        assert "MC 独家供应商需附替代方案" in contents
        assert "MC 关注 TCO" in contents
        assert scopes == {"GLOBAL", "MC"}
        category_names = {item["category_name"] for item in snapshot}
        assert "通用口径" in category_names
        assert "供应商" in category_names
        assert all("id" in item for item in snapshot)


def test_existing_sqlite_database_gets_procurement_and_material_review_schema(tmp_path):
    db_path = tmp_path / "legacy_review.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(50) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            display_name VARCHAR(100) NOT NULL,
            role VARCHAR(30) NOT NULL,
            created_at DATETIME NOT NULL
        );
        CREATE TABLE meetings (
            id INTEGER PRIMARY KEY,
            meeting_no VARCHAR(20) NOT NULL UNIQUE,
            title VARCHAR(200) NOT NULL,
            meeting_date DATE NOT NULL,
            location VARCHAR(200),
            host VARCHAR(100),
            status VARCHAR(20) NOT NULL,
            created_by INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE topics (
            id INTEGER PRIMARY KEY,
            meeting_id INTEGER,
            title VARCHAR(200) NOT NULL,
            category VARCHAR(100),
            owner VARCHAR(100),
            background TEXT,
            purpose TEXT,
            present_order INTEGER NOT NULL,
            status VARCHAR(20) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE ai_knowhow (
            id INTEGER PRIMARY KEY,
            scope VARCHAR(10) NOT NULL,
            content TEXT NOT NULL,
            created_by INTEGER,
            updated_by INTEGER,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{db_path}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
        }
    )

    with app.app_context():
        app.ensure_database()
        topic_columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(topics)")}
        knowhow_columns = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(ai_knowhow)")}
        tables = {row[0] for row in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert "plan_version" in topic_columns
    assert "duration_minutes" in topic_columns
    assert "topic_material_reviews" in tables
    assert "material_documents" in tables
    assert "material_chunks" in tables
    assert "material_retrieval_logs" in tables
    assert "ai_knowhow_categories" in tables
    assert "category_id" in knowhow_columns
    assert "is_active" in knowhow_columns


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
