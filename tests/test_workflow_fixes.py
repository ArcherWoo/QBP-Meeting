"""Regression tests for the 9 workflow bug fixes (P0-1, P0-2, P0-3, P1-1, P1-2, P2-1, R1, R2, R3)."""

from datetime import date, datetime, timedelta

import pytest

from backend.app import create_app
from backend.models import Attachment, Meeting, PlanRound, PlanVersion, Topic, User, db


@pytest.fixture()
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "UPLOAD_FOLDER": tmp_path / "uploads",
            "POWERPOINT_PREVIEW_FOLDER": tmp_path / "previews",
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


def login_admin(client):
    return client.post(
        "/auth/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=True,
    )


def login_as(client, username, password="user123"):
    return client.post(
        "/auth/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def create_user(username, password="user123", role="user"):
    user = User(username=username, display_name=username.title(), role=role, enabled=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def fresh_meeting(status="preparing", title="Fresh Meeting"):
    admin = User.query.filter_by(username="admin").one()
    version = PlanVersion.query.filter_by(name="Q3 26BP").one()
    round_item = PlanRound.query.filter_by(plan_version_id=version.id, name="Round 1").one()
    meeting = Meeting(
        meeting_no=Meeting.next_meeting_no(),
        title=title,
        meeting_date=date(2026, 7, 1),
        location="Room A",
        host="Admin",
        status=status,
        host_user_id=admin.id,
        created_by=admin.id,
        category="Kick Off",
        plan_version_id=version.id,
        plan_round_id=round_item.id,
    )
    db.session.add(meeting)
    db.session.commit()
    return meeting


def make_pending_topic(creator_id, meeting_id, title="Pending Topic"):
    meeting = db.session.get(Meeting, meeting_id)
    topic = Topic(
        title=title,
        category=meeting.category,
        plan_version=meeting.plan_version_name,
        plan_version_id=meeting.plan_version_id,
        plan_round_id=meeting.plan_round_id,
        owner="Alice",
        background="bg",
        purpose="purpose",
        created_by=creator_id,
        requested_meeting_id=meeting_id,
        workflow_status="submitted",
        submitted_at=datetime.utcnow(),
        status="pending",
    )
    db.session.add(topic)
    db.session.commit()
    return topic


# ---------- P0-1 ----------
def test_p0_1_reviewer_sees_pending_topic_detail_and_attachments(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = fresh_meeting()
        topic = make_pending_topic(alice.id, meeting.id, "Alice Pending Detail")
        upload_dir = app.config["UPLOAD_FOLDER"] / str(meeting.id) / str(topic.id)
        upload_dir.mkdir(parents=True, exist_ok=True)
        (upload_dir / "spec.pdf").write_bytes(b"%PDF-1.4 spec")
        attachment = Attachment(
            topic_id=topic.id,
            original_filename="spec.pdf",
            stored_filename="spec.pdf",
            file_type="pdf",
            file_size=13,
        )
        db.session.add(attachment)
        db.session.commit()
        meeting_no = meeting.meeting_no
        topic_id = topic.id
        attachment_id = attachment.id

    login_admin(client)
    page = client.get(f"/meetings/{meeting_no}?topic_id={topic_id}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Alice Pending Detail" in html
    assert "spec.pdf" in html
    assert f"/attachments/{attachment_id}/download" in html


# ---------- P0-2 ----------
def test_p0_2_meeting_delete_auto_rejects_pending_topics(client, app):
    with app.app_context():
        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting = fresh_meeting()
        pending = make_pending_topic(alice.id, meeting.id, "Zombie Candidate")
        approved = Topic(
            meeting_id=meeting.id,
            title="Already Approved",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            workflow_status="approved",
            present_order=1,
            status="pending",
            reviewed_by=admin.id,
            reviewed_at=datetime.utcnow(),
            review_comment="ok",
        )
        db.session.add(approved)
        db.session.commit()
        meeting_no = meeting.meeting_no
        pending_id = pending.id
        approved_id = approved.id

    login_admin(client)
    response = client.post(f"/meetings/{meeting_no}/delete", follow_redirects=True)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "1 个待审批议题" in body
    assert "1 个已通过议题已回退到待审批" in body
    with app.app_context():
        rejected = db.session.get(Topic, pending_id)
        assert rejected is not None
        assert rejected.workflow_status == "rejected"
        assert rejected.requested_meeting_id is None
        assert "系统自动驳回" in rejected.review_comment
        assert "已删除" in rejected.review_comment
        reverted = db.session.get(Topic, approved_id)
        assert reverted is not None
        assert reverted.workflow_status == "submitted"
        assert reverted.meeting_id is None
        assert reverted.requested_meeting_id is None
        assert reverted.present_order == 0
        assert reverted.reviewed_by is None
        assert reverted.reviewed_at is None
        assert "已删除" in (reverted.review_comment or "")




# ---------- P0-3 ----------
def test_p0_3_completed_meeting_blocks_submit_and_approve(client, app):
    with app.app_context():
        alice = create_user("alice")
        completed = fresh_meeting(status="completed", title="Already Done")
        draft = Topic(
            title="Submit Attempt",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            requested_meeting_id=completed.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id
        completed_id = completed.id
        alice_id = alice.id

    login_as(client, "alice")
    submit_resp = client.post(
        f"/topics/drafts/{draft_id}/submit",
        data={"requested_meeting_id": str(completed_id)},
        follow_redirects=True,
    )
    assert "已结束" in submit_resp.get_data(as_text=True)
    with app.app_context():
        assert db.session.get(Topic, draft_id).workflow_status == "draft"

    with app.app_context():
        pending = make_pending_topic(alice_id, completed_id, "Sneak Past Completed")
        pending_id = pending.id

    client.get("/auth/logout")
    login_admin(client)
    approve_resp = client.post(f"/topics/{pending_id}/approve", follow_redirects=True)
    assert "已结束" in approve_resp.get_data(as_text=True)
    with app.app_context():
        sneak = db.session.get(Topic, pending_id)
        assert sneak.workflow_status == "submitted"
        assert sneak.meeting_id is None


# ---------- P1-1 ----------
def test_p1_1_topic_approve_uses_next_topic_order(client, app):
    with app.app_context():
        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting = fresh_meeting()
        existing = Topic(
            meeting_id=meeting.id,
            title="Existing",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="X",
            present_order=5,
            status="pending",
            created_by=admin.id,
            workflow_status="approved",
        )
        db.session.add(existing)
        db.session.commit()
        pending = make_pending_topic(alice.id, meeting.id, "Will Be 6")
        pending_id = pending.id

    login_admin(client)
    client.post(f"/topics/{pending_id}/approve", follow_redirects=True)
    with app.app_context():
        assert db.session.get(Topic, pending_id).present_order == 6


# ---------- P1-2 ----------
def test_p1_2_submit_clears_review_fields_and_approve_clears_requested_meeting(client, app):
    with app.app_context():
        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting = fresh_meeting()
        topic = Topic(
            title="Reused Draft",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            requested_meeting_id=meeting.id,
            workflow_status="draft",
            status="pending",
            reviewed_by=admin.id,
            reviewed_at=datetime.utcnow() - timedelta(days=1),
            review_comment="prior reject",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id
        meeting_id = meeting.id

    login_as(client, "alice")
    client.post(
        f"/topics/drafts/{topic_id}/submit",
        data={"requested_meeting_id": str(meeting_id)},
        follow_redirects=True,
    )
    with app.app_context():
        submitted = db.session.get(Topic, topic_id)
        assert submitted.workflow_status == "submitted"
        assert submitted.reviewed_by is None
        assert submitted.reviewed_at is None
        assert submitted.review_comment == ""

    client.get("/auth/logout")
    login_admin(client)
    client.post(f"/topics/{topic_id}/approve", follow_redirects=True)
    with app.app_context():
        approved = db.session.get(Topic, topic_id)
        assert approved.workflow_status == "approved"
        assert approved.requested_meeting_id is None
        assert approved.meeting_id == meeting_id



# ---------- P2-1 ----------
def test_p2_1_submit_validates_meeting_exists(client, app):
    with app.app_context():
        alice = create_user("alice")
        draft = Topic(
            title="Submit With Bogus Meeting",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            workflow_status="draft",
            status="pending",
        )
        db.session.add(draft)
        db.session.commit()
        draft_id = draft.id

    login_as(client, "alice")
    resp = client.post(
        f"/topics/drafts/{draft_id}/submit",
        data={"requested_meeting_id": "999999"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    with app.app_context():
        assert db.session.get(Topic, draft_id).workflow_status == "draft"


# ---------- R1 ----------
def test_r1_submitted_topic_is_locked_for_everyone(client, app):
    with app.app_context():
        from backend.app import can_edit_topic, can_edit_draft

        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting = fresh_meeting()
        topic = make_pending_topic(alice.id, meeting.id, "Locked While Submitted")

        with app.test_request_context():
            from flask_login import login_user

            login_user(admin)
            assert can_edit_topic(topic) is False
            assert can_edit_draft(topic) is False

        with app.test_request_context():
            from flask_login import login_user

            login_user(alice)
            assert can_edit_topic(topic) is False
            assert can_edit_draft(topic) is False


def test_r1_submitted_topic_update_returns_403(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = fresh_meeting()
        topic = make_pending_topic(alice.id, meeting.id, "Try Update While Submitted")
        topic_id = topic.id

    login_admin(client)
    resp = client.post(
        f"/topics/{topic_id}/update",
        data={
            "title": "Hacked Title",
            "category": "Kick Off",
            "plan_version": "Q2 27BP",
            "owner": "X",
            "status": "pending",
            "background": "",
            "purpose": "",
        },
    )
    assert resp.status_code == 403
    with app.app_context():
        assert db.session.get(Topic, topic_id).title == "Try Update While Submitted"


# ---------- R2 ----------
def test_r2_meeting_edit_to_completed_preserves_meeting_detail_data(client, app):
    with app.app_context():
        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting = fresh_meeting()
        pending = make_pending_topic(alice.id, meeting.id, "Pending Request Stays Requested")
        approved = Topic(
            meeting_id=meeting.id,
            title="Approved Topic Stays In Completed Meeting",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            workflow_status="approved",
            present_order=2,
            status="pending",
            reviewed_by=admin.id,
            reviewed_at=datetime.utcnow(),
            review_comment="ok",
            decision_status="approved",
            decision_by=admin.id,
            decision_at=datetime.utcnow(),
            decision_comment="keep this decision",
        )
        db.session.add(approved)
        db.session.flush()
        attachment = Attachment(
            topic_id=approved.id,
            original_filename="completed-material.pdf",
            stored_filename="completed-material.pdf",
            file_type="pdf",
            file_size=42,
            uploaded_by=admin.id,
        )
        db.session.add(attachment)
        db.session.commit()
        meeting_no = meeting.meeting_no
        pending_id = pending.id
        approved_id = approved.id
        attachment_id = attachment.id
        admin_host_id = admin.id

    login_admin(client)
    resp = client.post(
        f"/meetings/{meeting_no}/edit",
        data={
            "title": "Fresh Meeting",
            "meeting_date": "2026-07-01",
            "location": "Room A",
            "host": "Admin",
            "status": "completed",
            "host_user_id": str(admin_host_id),
        },
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "会议信息已保存" in body
    assert "已回退到待审批" not in body
    assert "自动退回议题池" not in body
    with app.app_context():
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).one()
        assert meeting.status == "completed"
        pending = db.session.get(Topic, pending_id)
        assert pending.workflow_status == "submitted"
        assert pending.requested_meeting_id == meeting.id
        approved = db.session.get(Topic, approved_id)
        assert approved.workflow_status == "approved"
        assert approved.meeting_id == meeting.id
        assert approved.requested_meeting_id is None
        assert approved.present_order == 2
        assert approved.reviewed_by == admin_host_id
        assert approved.reviewed_at is not None
        assert approved.review_comment == "ok"
        assert approved.decision_status == "approved"
        assert approved.decision_by == admin_host_id
        assert approved.decision_at is not None
        assert approved.decision_comment == "keep this decision"
        assert db.session.get(Attachment, attachment_id).topic_id == approved_id


# ---------- R3 ----------
def test_r3_completed_meetings_filtered_from_topic_form(client, app):
    with app.app_context():
        alice = create_user("alice")
        active = fresh_meeting(status="preparing", title="Active Meeting Active")
        done = fresh_meeting(status="completed", title="Done Meeting Done")
        active_no = active.meeting_no
        done_no = done.meeting_no

    login_as(client, "alice")
    resp = client.get("/topics/drafts/create")
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert active_no in html
    assert done_no not in html


def test_r3_topic_form_keeps_bound_completed_meeting_option(client, app):
    with app.app_context():
        alice = create_user("alice")
        done = fresh_meeting(status="completed", title="Bound But Done")
        topic = Topic(
            title="Has Stale Selection",
            category="Kick Off",
            plan_version="Q2 27BP",
            owner="Alice",
            created_by=alice.id,
            requested_meeting_id=done.id,
            workflow_status="rejected",
            status="pending",
        )
        db.session.add(topic)
        db.session.commit()
        topic_id = topic.id
        done_no = done.meeting_no

    login_as(client, "alice")
    resp = client.get(f"/topics/drafts/{topic_id}/edit")
    assert resp.status_code == 200
    assert done_no in resp.get_data(as_text=True)


# ---------- T8: 开放提交跨池可见 ----------
def test_t8_open_submission_visible_in_multiple_meeting_pools(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting_a = fresh_meeting(title="Meeting A")
        meeting_b = fresh_meeting(title="Meeting B")
        meeting_c = fresh_meeting(title="Meeting C Other Category")
        meeting_c.category = "POR Review"
        open_topic = Topic(
            title="Open Idea",
            category="Kick Off",
            plan_version="Q3 26BP",
            plan_version_id=meeting_a.plan_version_id,
            plan_round_id=meeting_a.plan_round_id,
            owner="Alice",
            created_by=alice.id,
            requested_meeting_id=None,
            workflow_status="submitted",
            submitted_at=datetime.utcnow(),
            status="pending",
        )
        db.session.add(open_topic)
        db.session.commit()
        topic_id = open_topic.id
        no_a = meeting_a.meeting_no
        no_b = meeting_b.meeting_no
        no_c = meeting_c.meeting_no

    login_admin(client)
    data_a = client.get(f"/agenda/{no_a}/data").get_json()
    data_b = client.get(f"/agenda/{no_b}/data").get_json()
    data_c = client.get(f"/agenda/{no_c}/data").get_json()
    pool_a_ids = [t["id"] for t in data_a["left_pool"]]
    pool_b_ids = [t["id"] for t in data_b["left_pool"]]
    pool_c_ids = [t["id"] for t in data_c["left_pool"]]
    assert topic_id in pool_a_ids
    assert topic_id in pool_b_ids
    assert topic_id not in pool_c_ids
    kind_a = next(t["kind"] for t in data_a["left_pool"] if t["id"] == topic_id)
    assert kind_a == "open"


# ---------- T9: 乐观锁冲突 ----------
def test_t9_optimistic_lock_returns_409(client, app):
    with app.app_context():
        alice = create_user("alice")
        meeting = fresh_meeting()
        topic = make_pending_topic(alice.id, meeting.id, "Race Target")
        meeting_no = meeting.meeting_no
        topic_id = topic.id

    login_admin(client)
    payload = {
        "expected_updated_at": "1970-01-01T00:00:00",
        "approve": [{"id": topic_id, "review_comment": "ok"}],
        "unbind": [],
        "reorder": [],
    }
    resp = client.patch(f"/agenda/{meeting_no}", json=payload)
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["ok"] is False
    assert "current_updated_at" in body
    with app.app_context():
        survivor = db.session.get(Topic, topic_id)
        assert survivor.workflow_status == "submitted"
        assert survivor.meeting_id is None


# ---------- T10: unbind → 回退 submitted（开放池可见） ----------
def test_t10_unbind_reverts_topic_to_submitted(client, app):
    with app.app_context():
        alice = create_user("alice")
        admin = User.query.filter_by(username="admin").one()
        meeting_a = fresh_meeting(title="Source Meeting")
        meeting_b = fresh_meeting(title="Future Meeting")
        approved = Topic(
            meeting_id=meeting_a.id,
            title="Will Revert",
            category="Kick Off",
            plan_version=meeting_a.plan_version_name,
            plan_version_id=meeting_a.plan_version_id,
            plan_round_id=meeting_a.plan_round_id,
            owner="Alice",
            created_by=alice.id,
            workflow_status="approved",
            present_order=1,
            status="pending",
            reviewed_by=admin.id,
            reviewed_at=datetime.utcnow(),
            review_comment="ok",
        )
        db.session.add(approved)
        db.session.commit()
        topic_id = approved.id
        no_a = meeting_a.meeting_no
        no_b = meeting_b.meeting_no
        expected = meeting_a.updated_at.isoformat()

    login_admin(client)
    payload = {
        "expected_updated_at": expected,
        "approve": [],
        "unbind": [{"id": topic_id, "review_comment": "暂缓"}],
        "reorder": [],
    }
    resp = client.patch(f"/agenda/{no_a}", json=payload)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    with app.app_context():
        reverted = db.session.get(Topic, topic_id)
        assert reverted.workflow_status == "submitted"
        assert reverted.meeting_id is None
        assert reverted.requested_meeting_id is None
        assert reverted.present_order == 0
        assert reverted.reviewed_by is None
        assert reverted.reviewed_at is None
        assert "移出议题" in (reverted.review_comment or "")
    data_b = client.get(f"/agenda/{no_b}/data").get_json()
    ids = [t["id"] for t in data_b["left_pool"]]
    assert topic_id in ids
    kind = next(t["kind"] for t in data_b["left_pool"] if t["id"] == topic_id)
    assert kind == "open"
