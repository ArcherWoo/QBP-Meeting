import base64
import csv
import io
import json
import re
import shutil
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import requests
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import and_, inspect, or_, text
from sqlalchemy.orm import joinedload

from .audit import (
    ACTION_LABELS,
    CONFIG_KEY_LABELS,
    TARGET_TYPE_LABELS,
    activity_summary,
    audit_query_from_request,
    default_audit_end_date,
    default_audit_start_date,
    format_audit_metadata,
    format_local_datetime,
    local_date_end,
    local_date_start,
    record_audit,
)
from .config import config
from .copilot import extract_chat_answer, register_copilot_routes, zhishu_client
from . import decryption_service
from .file_utils import is_allowed, save_topic_file
from .lark_service import (
    LARK_CONFIG_KEY,
    LarkAPIError,
    LarkClient,
    lark_secret_status,
    normalize_lark_config,
)
from .material_rag import (
    index_attachment_material,
    mark_attachment_material_deleted,
    retrieve_topic_material_chunks,
)
from .models import (
    AI_REVIEW_PROMPT_KEY,
    AIKnowHow,
    AIKnowHowCategory,
    AIPrompt,
    AppConfig,
    BUSINESS_GROUP_CODES,
    DEFAULT_AI_REVIEW_PROMPT,
    DEFAULT_BUSINESS_GROUP_NAMES,
    AI_PROMPT_OUTPUT_OPTIONS,
    AI_PROMPT_SCOPE_OPTIONS,
    DEFAULT_PLAN_VERSION,
    DEFAULT_TOPIC_COMPLETENESS_RULE,
    GLOBAL_KNOWHOW_LABEL,
    GLOBAL_KNOWHOW_SCOPE,
    LEGACY_TOPIC_CATEGORY_MAP,
    LEGACY_PLAN_VERSION_MAP,
    MEETING_READINESS_CONFIG_KEY,
    PLAN_VERSION_OPTIONS,
    TOPIC_COMPLETENESS_CONFIG_KEY,
    TOPIC_CATEGORY_OPTIONS,
    TOPIC_DURATION_DEFAULT_MINUTES,
    Attachment,
    AuditLog,
    Group,
    Meeting,
    MeetingFavorite,
    MeetingMinutes,
    MaterialChunk,
    MaterialDocument,
    MaterialRetrievalLog,
    PLAN_VERSION_CODE_MAP,
    PlanRound,
    PlanVersion,
    Topic,
    TopicMaterialReview,
    TopicShare,
    User,
    db,
    default_meeting_readiness_config,
    default_topic_completeness_config,
    normalize_plan_version,
    normalize_topic_duration,
    normalize_meeting_readiness_config,
    normalize_topic_completeness_config,
    normalize_topic_category,
)
from .powerpoint_preview import PowerPointPreviewError, build_powerpoint_pdf_preview


MEETING_STATUS_LABELS = {
    "draft": "草稿",
    "preparing": "准备中",
    "reporting": "汇报中",
    "completed": "已完成",
}

TOPIC_STATUS_LABELS = {
    "pending": "待准备",
    "ready": "已就绪",
    "presented": "已汇报",
}

WORKFLOW_STATUS_LABELS = {
    "draft": "草稿",
    "submitted": "待审批",
    "approved": "已通过",
    "rejected": "已驳回",
    "withdrawn": "已撤回",
}

TOPIC_DECISION_STATUS_LABELS = {
    "pending": "待决策",
    "approved": "已通过",
    "conditional_approved": "有条件通过",
    "delayed": "延期",
    "rejected": "已驳回",
}

DECRYPTABLE_OFFICE_EXTENSIONS = {"pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx"}


def create_app(config_name="default"):
    project_root = Path(__file__).resolve().parents[1]
    app = Flask(
        __name__,
        template_folder=str(project_root / "frontend" / "templates"),
        static_folder=str(project_root / "frontend" / "static"),
        static_url_path="/static",
    )
    if isinstance(config_name, dict):
        app.config.from_object(config["testing"])
        app.config.update(config_name)
    else:
        app.config.from_object(config[config_name])

    app.config["UPLOAD_FOLDER"] = resolve_project_path(app.config["UPLOAD_FOLDER"], project_root)
    app.config["POWERPOINT_PREVIEW_FOLDER"] = resolve_project_path(
        app.config["POWERPOINT_PREVIEW_FOLDER"],
        project_root,
    )
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["POWERPOINT_PREVIEW_FOLDER"]).mkdir(parents=True, exist_ok=True)
    ensure_sqlite_parent(app.config["SQLALCHEMY_DATABASE_URI"])
    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "login"
    login_manager.login_message = "请先登录"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    register_copilot_routes(app)

    @app.context_processor
    def inject_status_labels():
        return {
            "meeting_status_labels": MEETING_STATUS_LABELS,
            "topic_status_labels": TOPIC_STATUS_LABELS,
            "workflow_status_labels": WORKFLOW_STATUS_LABELS,
            "topic_decision_status_labels": TOPIC_DECISION_STATUS_LABELS,
            "action_labels": ACTION_LABELS,
            "target_type_labels": TARGET_TYPE_LABELS,
            "config_key_labels": CONFIG_KEY_LABELS,
            "topic_category_options": TOPIC_CATEGORY_OPTIONS,
            "plan_version_options": plan_version_options(),
            "plan_round_options": plan_round_options(),
            "round_options_for_version": plan_round_options,
            "topic_duration_default_minutes": TOPIC_DURATION_DEFAULT_MINUTES,
            "is_admin": is_admin,
            "can_manage_meetings": can_manage_meetings,
            "can_access_agenda": can_access_agenda,
            "can_manage_config": can_manage_config,
            "can_manage_ai_prompt": can_manage_ai_prompt,
            "can_review_topic_material": can_review_topic_material,
            "can_review_meeting": can_review_meeting,
            "can_decide_meeting_topic": can_decide_meeting_topic,
            "can_edit_topic": can_edit_topic,
            "topic_readiness": topic_readiness,
            "topic_completeness": topic_completeness,
            "format_local_datetime": format_local_datetime,
        }

    def ensure_database():
        db.create_all()
        ensure_lightweight_schema()
        PlanVersion.seed_defaults()
        Group.seed_defaults()
        admin = User.create_default_admin()
        ensure_default_ai_prompt(admin)
        backfill_existing_data(admin)
        Meeting.seed_demo()

    app.ensure_database = ensure_database

    @app.route("/")
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("meeting_list"))
        return redirect(url_for("login"))

    @app.route("/auth/login", methods=["GET", "POST"])
    def login():
        if current_user.is_authenticated:
            return redirect(url_for("meeting_list"))
        if request.method == "POST":
            user = User.query.filter_by(username=request.form.get("username", "").strip()).first()
            if user and not user.enabled:
                flash("账号已禁用，请联系管理员", "danger")
            elif user and user.check_password(request.form.get("password", "")):
                login_user(user)
                record_audit("login", target_type="user", target_id=user.id, target_label=user.username)
                flash(f"欢迎回来，{user.display_name}！", "success")
                return redirect(url_for("meeting_list"))
            else:
                flash("用户名或密码错误", "danger")
        return render_template("auth/login.html")

    @app.route("/auth/logout")
    @login_required
    def logout():
        record_audit("logout", target_type="user", target_id=current_user.id, target_label=current_user.username)
        logout_user()
        flash("您已成功登出", "info")
        return redirect(url_for("login"))

    @app.route("/plan-versions", methods=["POST"])
    @login_required
    def plan_version_create():
        require_meeting_manager()
        payload = request.get_json(silent=True) or request.form
        try:
            version = PlanVersion.create_with_default_round((payload.get("name") or "").strip())
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return plan_version_payload(version), 201

    @app.route("/plan-versions/<int:plan_version_id>/rounds", methods=["GET"])
    @login_required
    def plan_version_rounds(plan_version_id):
        version = db.session.get(PlanVersion, plan_version_id) or abort(404)
        return {"rounds": [plan_round_payload(round_item) for round_item in PlanRound.active_for_version(version.id)]}

    @app.route("/plan-versions/<int:plan_version_id>/rounds", methods=["POST"])
    @login_required
    def plan_round_create(plan_version_id):
        require_meeting_manager()
        version = db.session.get(PlanVersion, plan_version_id) or abort(404)
        payload = request.get_json(silent=True) or request.form
        try:
            round_item = PlanRound.create_next(version, payload.get("name"))
        except ValueError as exc:
            return {"error": str(exc)}, 400
        return plan_round_payload(round_item), 201

    @app.route("/plan-versions/<int:plan_version_id>", methods=["DELETE"])
    @login_required
    def plan_version_delete(plan_version_id):
        require_meeting_manager()
        version = db.session.get(PlanVersion, plan_version_id) or abort(404)
        if version.name == DEFAULT_PLAN_VERSION:
            return {"error": "默认 Plan Version 不能删除"}, 400
        if Meeting.query.filter_by(plan_version_id=version.id).first() or Topic.query.filter_by(plan_version_id=version.id).first():
            return {"error": "Plan Version 已被会议或议题使用，不能删除"}, 400
        db.session.delete(version)
        db.session.commit()
        return {"ok": True}

    @app.route("/plan-rounds/<int:plan_round_id>", methods=["DELETE"])
    @login_required
    def plan_round_delete(plan_round_id):
        require_meeting_manager()
        round_item = db.session.get(PlanRound, plan_round_id) or abort(404)
        if Meeting.query.filter_by(plan_round_id=round_item.id).first() or Topic.query.filter_by(plan_round_id=round_item.id).first():
            return {"error": "Round 已被会议或议题使用，不能删除"}, 400
        active_count = PlanRound.query.filter_by(plan_version_id=round_item.plan_version_id, is_active=True).count()
        if active_count <= 1:
            return {"error": "每个 Plan Version 至少保留一个 Round"}, 400
        db.session.delete(round_item)
        db.session.commit()
        return {"ok": True}

    @app.route("/auth/profile")
    @login_required
    def profile():
        return render_template("auth/profile.html")

    @app.route("/auth/change_password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            old_password = request.form.get("old_password", "")
            new_password = request.form.get("new_password", "")
            confirm_password = request.form.get("confirm_password", "")
            if not current_user.check_password(old_password):
                flash("旧密码错误", "danger")
            elif len(new_password) < 6:
                flash("新密码长度至少 6 位", "danger")
            elif new_password != confirm_password:
                flash("两次输入的新密码不一致", "danger")
            else:
                current_user.set_password(new_password)
                db.session.commit()
                logout_user()
                flash("密码修改成功，请重新登录", "success")
                return redirect(url_for("login"))
        return redirect(url_for("profile"))

    @app.route("/admin")
    @login_required
    def admin_index():
        require_admin()
        pending_topics = Topic.query.filter_by(workflow_status="submitted").count()
        summary = activity_summary()
        return render_template(
            "admin/index.html",
            meeting_count=Meeting.query.count(),
            topic_count=Topic.query.count(),
            pending_count=pending_topics,
            user_count=User.query.count(),
            activity=summary,
            recent_logs=AuditLog.query.order_by(AuditLog.created_at.desc()).limit(5).all(),
        )

    @app.route("/admin/audit-logs")
    @login_required
    def admin_audit_logs():
        require_admin()
        filters = request.args.copy()
        filters.pop("page", None)
        if not filters.get("start_date") and not filters.get("end_date"):
            filters["start_date"] = default_audit_start_date()
            filters["end_date"] = default_audit_end_date()
        page = request.args.get("page", 1, type=int)
        pagination = (
            audit_query_from_request(filters)
            .order_by(AuditLog.created_at.desc())
            .paginate(page=page, per_page=30, error_out=False)
        )
        target_types = (
            db.session.query(AuditLog.target_type)
            .filter(AuditLog.target_type.isnot(None))
            .distinct()
            .order_by(AuditLog.target_type.asc())
            .all()
        )
        return render_template(
            "admin/audit_logs.html",
            logs=pagination.items,
            pagination=pagination,
            users=User.query.order_by(User.display_name.asc(), User.username.asc()).all(),
            actions=ACTION_LABELS,
            target_type_options=[row[0] for row in target_types],
            filters=filters,
            format_audit_metadata=format_audit_metadata,
        )

    @app.route("/admin/activity")
    @login_required
    def admin_activity():
        require_admin()
        return render_template("admin/activity.html", activity=activity_summary())

    @app.route("/admin/lark", methods=["GET", "POST"])
    @login_required
    def admin_lark():
        require_admin()
        config_value = lark_config()
        if request.method == "POST":
            form_config = lark_config_from_form(request.form, config_value)
            save_lark_config(form_config)
            record_audit(
                "update_config",
                target_type="config",
                target_label=LARK_CONFIG_KEY,
                metadata={
                    "enabled": form_config["enabled"],
                    "app_id": form_config["app_id"],
                    "reminder_days": form_config["reminder_days"],
                    "has_secret": bool(form_config["app_secret"]),
                },
            )
            flash("飞书配置已保存", "success")
            return redirect(url_for("admin_lark"))
        users = User.query.order_by(User.display_name.asc(), User.username.asc()).all()
        return render_template(
            "admin/lark.html",
            lark_config=config_value,
            lark_secret_status=lark_secret_status(config_value),
            users=users,
        )

    @app.route("/admin/lark/test", methods=["POST"])
    @login_required
    def admin_lark_test():
        require_admin()
        try:
            LarkClient(ready_lark_config()).tenant_access_token()
        except (ValueError, LarkAPIError, requests.RequestException) as exc:
            flash(f"飞书连接失败：{exc}", "danger")
        else:
            flash("飞书连接正常", "success")
        return redirect(url_for("admin_lark"))

    @app.route("/admin/lark/sync-users", methods=["POST"])
    @login_required
    def admin_lark_sync_users():
        require_admin()
        try:
            synced_count = sync_lark_users(LarkClient(ready_lark_config()))
        except (ValueError, LarkAPIError, requests.RequestException) as exc:
            flash(f"人员同步失败：{exc}", "danger")
        else:
            record_audit(
                "update_config",
                target_type="config",
                target_label=LARK_CONFIG_KEY,
                metadata={"synced_users": synced_count},
            )
            flash(f"已同步 {synced_count} 个用户", "success")
        return redirect(url_for("admin_lark"))

    @app.route("/admin/lark/send-reminders", methods=["POST"])
    @login_required
    def admin_lark_send_reminders():
        require_admin()
        try:
            sent_count = send_lark_missing_material_reminders(LarkClient(ready_lark_config()))
        except (ValueError, LarkAPIError, requests.RequestException) as exc:
            flash(f"提醒发送失败：{exc}", "danger")
        else:
            record_audit(
                "update_config",
                target_type="config",
                target_label=LARK_CONFIG_KEY,
                metadata={"sent_messages": sent_count},
            )
            flash(f"已发送 {sent_count} 条提醒", "success")
        return redirect(url_for("admin_lark"))

    @app.route("/admin/config", methods=["GET"])
    @login_required
    def admin_config():
        require_config_manager()
        selected_scope = normalize_plan_version(request.values.get("scope") or DEFAULT_PLAN_VERSION)
        return render_config_table(selected_scope)

    @app.route("/admin/config/readiness", methods=["GET", "POST"])
    @login_required
    def admin_readiness_config():
        require_config_manager()
        if request.method == "POST":
            form_config, error = readiness_config_from_form(request.form)
            if error:
                flash(error, "danger")
            else:
                save_meeting_readiness_config(form_config)
                record_audit(
                    "update_config",
                    target_type="config",
                    target_label=MEETING_READINESS_CONFIG_KEY,
                    metadata=form_config,
                )
                flash("配置已保存", "success")
                return redirect(url_for("admin_config"))
        return render_config_table()

    @app.route("/admin/config/topic-completeness", methods=["GET", "POST"])
    @login_required
    def admin_topic_completeness_config():
        require_config_manager()
        selected_scope = normalize_plan_version(request.values.get("scope") or DEFAULT_PLAN_VERSION)
        config_value = topic_completeness_config()
        if request.method == "POST":
            form_config, error = topic_completeness_config_from_form(request.form, config_value)
            selected_scope = normalize_plan_version(request.form.get("scope") or selected_scope)
            if error:
                flash(error, "danger")
            else:
                save_topic_completeness_config(form_config)
                record_audit(
                    "update_config",
                    target_type="config",
                    target_label=TOPIC_COMPLETENESS_CONFIG_KEY,
                    metadata={"scope": selected_scope, "rule": form_config["rules"][selected_scope]},
                )
                flash("配置已保存", "success")
                return redirect(url_for("admin_config", scope=selected_scope))
            config_value = form_config or config_value
        return render_config_table(selected_scope, config_value)

    @app.route("/ai-workshop/prompt", methods=["GET", "POST"])
    @login_required
    def ai_workshop_prompt():
        require_ai_prompt_manager()
        if request.method == "POST":
            try:
                prompt = save_ai_prompt_from_form(request.form, current_user)
            except ValueError as exc:
                flash(str(exc), "danger")
            else:
                record_audit(
                    "update_config",
                    target_type="ai_prompt",
                    target_id=prompt.id,
                    target_label=prompt.name,
                    metadata={
                        "scope": prompt.scope,
                        "default": prompt.is_default,
                        "active": prompt.is_active,
                        "knowledge_sources": prompt.normalized_knowledge_sources,
                        "outputs": prompt.normalized_output_options,
                    },
                )
                flash("提示词模板已保存", "success")
                return redirect(url_for("ai_workshop_prompt", prompt_id=prompt.id))
        prompt_query = scoped_ai_prompt_query()
        prompts = prompt_query.order_by(
            AIPrompt.scope.asc(),
            AIPrompt.is_default.desc(),
            AIPrompt.updated_at.desc(),
            AIPrompt.id.desc(),
        ).all()
        selected_prompt = None
        prompt_id = request.args.get("prompt_id", type=int)
        is_new_mode = request.args.get("mode") == "new"
        if prompt_id:
            selected_prompt = db.session.get(AIPrompt, prompt_id)
            if selected_prompt and not can_manage_ai_prompt_scope(selected_prompt.scope):
                abort(403)
        if selected_prompt is None and prompts and not is_new_mode:
            selected_prompt = prompts[0]
        history = (
            AuditLog.query.filter(
                AuditLog.action == "update_config",
                or_(
                    AuditLog.target_type == "ai_prompt",
                    AuditLog.target_label == AI_REVIEW_PROMPT_KEY,
                ),
            )
            .order_by(AuditLog.created_at.desc())
            .limit(8)
            .all()
        )
        return render_template(
            "admin/ai_workshop_prompt.html",
            prompts=prompts,
            selected_prompt=selected_prompt,
            output_defaults=AI_PROMPT_OUTPUT_OPTIONS,
            scope_options=(GLOBAL_KNOWHOW_SCOPE, *ai_prompt_business_scope_options(), "CUSTOM"),
            business_scope_options=ai_prompt_business_scope_options(),
            knowledge_source_options=ai_prompt_knowledge_source_options(),
            selected_knowledge_source_scopes=selected_ai_prompt_knowledge_source_scopes(selected_prompt),
            selected_knowledge_source_categories=selected_ai_prompt_knowledge_source_categories(selected_prompt),
            rendered_preview=render_ai_prompt_message(
                selected_prompt,
                "POR Review 材料评审",
                selected_prompt.scope
                if selected_prompt and selected_prompt.scope in ai_business_scope_names(include_stored=True)
                else (ai_business_scope_names()[0] if ai_business_scope_names() else GLOBAL_KNOWHOW_SCOPE),
                sample_knowhow_text_for_prompt(selected_prompt),
            ) if selected_prompt else "",
            history=history,
        )

    @app.route("/ai-workshop/prompt/<int:prompt_id>/delete", methods=["POST"])
    @login_required
    def ai_workshop_prompt_delete(prompt_id):
        require_ai_prompt_manager()
        prompt = db.session.get(AIPrompt, prompt_id) or abort(404)
        if not can_manage_ai_prompt_scope(prompt.scope):
            abort(403)
        prompt_name = prompt.name
        prompt_scope = prompt.scope
        was_default = prompt.is_default
        db.session.delete(prompt)
        db.session.flush()
        fallback = None
        if was_default:
            fallback = (
                AIPrompt.query.filter_by(scope=prompt_scope, is_active=True)
                .order_by(AIPrompt.updated_at.desc(), AIPrompt.id.desc())
                .first()
            )
            if fallback:
                fallback.is_default = True
        db.session.commit()
        record_audit(
            "delete_ai_prompt",
            target_type="ai_prompt",
            target_id=prompt_id,
            target_label=prompt_name,
            metadata={
                "scope": prompt_scope,
                "was_default": was_default,
                "fallback_prompt_id": fallback.id if fallback else None,
            },
        )
        ensure_default_ai_prompt(current_user)
        flash("提示词模板已删除", "success")
        if fallback:
            return redirect(url_for("ai_workshop_prompt", prompt_id=fallback.id))
        return redirect(url_for("ai_workshop_prompt"))

    @app.route("/ai-workshop/knowhow")
    @login_required
    def ai_workshop_knowhow():
        scope = normalize_knowhow_scope_input(request.args.get("scope"))
        if scope not in knowhow_scope_options():
            scope = _default_knowhow_scope_for_user()
        if not _can_view_knowhow_scope(scope):
            abort(403)
        categories = knowhow_categories_for_scope(scope)
        entries = (
            AIKnowHow.query.filter_by(scope=scope)
            .outerjoin(AIKnowHowCategory, AIKnowHow.category_id == AIKnowHowCategory.id)
            .order_by(AIKnowHowCategory.name.asc(), AIKnowHow.updated_at.desc())
            .all()
        )
        uncategorized_entries = [
            entry for entry in entries if entry.category_id is None
        ]
        categorized_entries = [
            entry for entry in entries if entry.category_id is not None
        ]
        entries = categorized_entries + uncategorized_entries
        category_counts = {
            category.id: AIKnowHow.query.filter_by(scope=scope, category_id=category.id).count()
            for category in categories
        }
        uncategorized_count = AIKnowHow.query.filter_by(scope=scope, category_id=None).count()
        categories = list(categories)
        category_filter = request.args.get("category_id", type=int)
        if category_filter:
            entries = [
                entry for entry in entries
                if entry.category_id == category_filter
            ]
        elif category_filter == 0:
            entries = [
                entry for entry in entries
                if entry.category_id is None
            ]
        scopes = visible_knowhow_scopes()
        counts = {
            s: AIKnowHow.query.filter_by(scope=s).count()
            for s in scopes
        }
        return render_template(
            "admin/ai_workshop_knowhow.html",
            scope=scope,
            scopes=scopes,
            counts=counts,
            entries=entries,
            categories=categories,
            category_counts=category_counts,
            uncategorized_count=uncategorized_count,
            selected_category_id=category_filter,
            can_edit=_can_edit_knowhow_scope(scope),
            can_manage_categories=_can_manage_knowhow_categories(scope),
            scope_label=knowhow_scope_label,
        )

    @app.route("/ai-workshop/knowhow/create", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_create():
        scope = normalize_knowhow_scope_input(request.form.get("scope"))
        category_id = request.form.get("category_id", type=int)
        content = (request.form.get("content") or "").strip()
        require_knowhow_scope(scope, _can_edit_knowhow_scope)
        category = db.session.get(AIKnowHowCategory, category_id) if category_id else None
        if not category or category.scope != scope:
            flash("请先选择子分类", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))
        if not content:
            flash("内容不能为空", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))
        entry = AIKnowHow(
            scope=scope,
            category_id=category.id,
            content=content,
            is_active=True,
            created_by=current_user.id,
            updated_by=current_user.id,
        )
        db.session.add(entry)
        db.session.commit()
        record_audit(
            "create_knowhow",
            target_type="knowhow",
            target_id=entry.id,
            target_label=scope,
            metadata={"preview": content[:80]},
        )
        flash("已新增 know-how", "success")
        return redirect(url_for("ai_workshop_knowhow", scope=scope))

    @app.route("/ai-workshop/knowhow/export")
    @login_required
    def ai_workshop_knowhow_export():
        scope = normalize_knowhow_scope_input(request.args.get("scope"))
        require_knowhow_scope(scope, _can_view_knowhow_scope)
        category_filter = request.args.get("category_id", type=int)
        query = (
            AIKnowHow.query.filter_by(scope=scope)
            .outerjoin(AIKnowHowCategory, AIKnowHow.category_id == AIKnowHowCategory.id)
            .order_by(AIKnowHowCategory.name.asc(), AIKnowHow.updated_at.desc())
        )
        if category_filter:
            query = query.filter(AIKnowHow.category_id == category_filter)
        elif category_filter == 0:
            query = query.filter(AIKnowHow.category_id.is_(None))

        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["category", "content", "is_active"])
        for entry in query.all():
            writer.writerow([
                entry.category.name if entry.category else "",
                entry.content,
                "1" if entry.is_active else "0",
            ])
        data = output.getvalue().encode("utf-8-sig")
        return send_file(
            io.BytesIO(data),
            mimetype="text/csv",
            as_attachment=True,
            download_name=f"knowhow-{scope.lower()}.csv",
        )

    @app.route("/ai-workshop/knowhow/import", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_import():
        scope = normalize_knowhow_scope_input(request.form.get("scope"))
        require_knowhow_scope(scope, _can_edit_knowhow_scope)
        file = request.files.get("file")
        if not file or not file.filename:
            flash("请选择 CSV 文件", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))
        if not file.filename.lower().endswith(".csv"):
            flash("只支持 CSV 文件", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))

        try:
            text_stream = io.StringIO(file.read().decode("utf-8-sig"))
        except UnicodeDecodeError:
            flash("CSV 文件编码需为 UTF-8", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))

        reader = csv.DictReader(text_stream)
        imported = 0
        skipped = 0
        categories_by_name = {
            category.name: category
            for category in AIKnowHowCategory.query.filter_by(scope=scope).all()
        }
        for row in reader:
            category_name = (row.get("category") or row.get("分类") or "").strip()
            content = (row.get("content") or row.get("内容") or "").strip()
            active_value = (row.get("is_active") or row.get("启用") or "1").strip().lower()
            if not category_name or not content:
                skipped += 1
                continue
            category = categories_by_name.get(category_name)
            if category is None:
                if not _can_manage_knowhow_categories(scope):
                    skipped += 1
                    continue
                category = AIKnowHowCategory(
                    scope=scope,
                    name=category_name,
                    created_by=current_user.id,
                    updated_by=current_user.id,
                )
                db.session.add(category)
                db.session.flush()
                categories_by_name[category_name] = category
            entry = AIKnowHow(
                scope=scope,
                category_id=category.id,
                content=content,
                is_active=active_value not in {"0", "false", "no", "n", "否", "停用"},
                created_by=current_user.id,
                updated_by=current_user.id,
            )
            db.session.add(entry)
            imported += 1
        db.session.commit()
        record_audit(
            "create_knowhow",
            target_type="knowhow",
            target_label=f"{scope}/csv",
            metadata={"imported": imported, "skipped": skipped, "filename": file.filename},
        )
        message = f"已导入 {imported} 条知识"
        if skipped:
            message += f"，跳过 {skipped} 行"
        flash(message, "success" if imported else "info")
        return redirect(url_for("ai_workshop_knowhow", scope=scope))

    @app.route("/ai-workshop/knowhow/<int:entry_id>/update", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_update(entry_id):
        entry = db.session.get(AIKnowHow, entry_id) or abort(404)
        if not _can_edit_knowhow_scope(entry.scope):
            abort(403)
        content = (request.form.get("content") or "").strip()
        if not content:
            flash("内容不能为空", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=entry.scope))
        entry.content = content
        category_id = request.form.get("category_id", type=int)
        category = db.session.get(AIKnowHowCategory, category_id) if category_id else None
        if category and category.scope == entry.scope:
            entry.category_id = category.id
        entry.is_active = bool(request.form.get("is_active"))
        entry.updated_by = current_user.id
        db.session.commit()
        record_audit(
            "update_knowhow",
            target_type="knowhow",
            target_id=entry.id,
            target_label=entry.scope,
            metadata={"preview": content[:80]},
        )
        flash("已更新", "success")
        return redirect(url_for("ai_workshop_knowhow", scope=entry.scope))

    @app.route("/ai-workshop/knowhow/<int:entry_id>/delete", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_delete(entry_id):
        entry = db.session.get(AIKnowHow, entry_id) or abort(404)
        if not _can_edit_knowhow_scope(entry.scope):
            abort(403)
        scope = entry.scope
        entry_id_value = entry.id
        db.session.delete(entry)
        db.session.commit()
        record_audit(
            "delete_knowhow",
            target_type="knowhow",
            target_id=entry_id_value,
            target_label=scope,
        )
        flash("已删除", "success")
        return redirect(url_for("ai_workshop_knowhow", scope=scope))

    @app.route("/ai-workshop/knowhow/categories/create", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_category_create():
        scope = normalize_knowhow_scope_input(request.form.get("scope"))
        name = (request.form.get("name") or "").strip()
        require_knowhow_scope(scope, _can_manage_knowhow_categories)
        if not name:
            flash("子分类名称不能为空", "danger")
            return redirect(url_for("ai_workshop_knowhow", scope=scope))
        existing = AIKnowHowCategory.query.filter_by(scope=scope, name=name).first()
        if existing:
            flash("该子分类已存在", "info")
            return redirect(url_for("ai_workshop_knowhow", scope=scope, category_id=existing.id))
        category = AIKnowHowCategory(
            scope=scope,
            name=name,
            created_by=current_user.id,
            updated_by=current_user.id,
        )
        db.session.add(category)
        db.session.commit()
        record_audit(
            "create_knowhow",
            target_type="knowhow",
            target_label=f"{scope}/{name}",
            metadata={"category_id": category.id, "category": name},
        )
        flash("子分类已新增", "success")
        return redirect(url_for("ai_workshop_knowhow", scope=scope, category_id=category.id))

    @app.route("/ai-workshop/knowhow/categories/<int:category_id>/delete", methods=["POST"])
    @login_required
    def ai_workshop_knowhow_category_delete(category_id):
        category = db.session.get(AIKnowHowCategory, category_id) or abort(404)
        if not _can_manage_knowhow_categories(category.scope):
            abort(403)
        scope = category.scope
        category_name = category.name
        AIKnowHow.query.filter_by(category_id=category.id).update({"category_id": None})
        db.session.delete(category)
        db.session.commit()
        record_audit(
            "delete_knowhow",
            target_type="knowhow",
            target_label=f"{scope}/{category_name}",
            metadata={"category_id": category_id, "category": category_name},
        )
        flash("子分类已删除，原知识已移至未分类", "success")
        return redirect(url_for("ai_workshop_knowhow", scope=scope))


    @app.route("/admin/topic-approvals")
    @login_required
    def admin_topic_approvals():
        require_admin()
        topics = (
            Topic.query.filter_by(workflow_status="submitted")
            .order_by(Topic.submitted_at.asc(), Topic.updated_at.asc())
            .all()
        )
        return render_template("admin/topic_approvals.html", topics=topics)

    @app.route("/admin/users")
    @login_required
    def admin_users():
        require_user_manager()
        query = User.query
        if not is_admin():
            query = query.filter(User.group_id == current_user.group_id, User.role != "admin")
        users = query.order_by(User.role.asc(), User.username.asc()).all()
        groups = Group.query.order_by(Group.is_admin_group.desc(), Group.name.asc()).all()
        return render_template(
            "admin/users.html",
            users=users,
            groups=groups,
            is_admin_view=is_admin(),
        )

    @app.route("/admin/groups/create", methods=["POST"])
    @login_required
    def admin_group_create():
        require_admin()
        name = request.form.get("name", "").strip()
        if not name:
            flash("用户组名称不能为空", "danger")
            return redirect(url_for("admin_users"))
        if Group.query.filter_by(name=name).first():
            flash("用户组名称已存在", "danger")
            return redirect(url_for("admin_users"))
        group = Group(code=next_custom_group_code(), name=name, is_admin_group=False)
        db.session.add(group)
        db.session.commit()
        record_audit("create_group", target_type="group", target_id=group.id, target_label=group.name)
        flash("用户组已创建", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/groups/<int:group_id>/update", methods=["POST"])
    @login_required
    def admin_group_update(group_id):
        require_admin()
        group = db.session.get(Group, group_id) or abort(404)
        if not is_custom_group(group):
            flash("默认用户组不能修改名称", "danger")
            return redirect(url_for("admin_users"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("用户组名称不能为空", "danger")
            return redirect(url_for("admin_users"))
        if Group.query.filter(Group.name == name, Group.id != group.id).first():
            flash("用户组名称已存在", "danger")
            return redirect(url_for("admin_users"))
        group.name = name
        db.session.commit()
        record_audit("update_group", target_type="group", target_id=group.id, target_label=group.name)
        flash("用户组名称已更新", "success")
        return redirect(url_for("admin_users", group_status="updated") + "#group-manager")

    @app.route("/admin/groups/<int:group_id>/delete", methods=["POST"])
    @login_required
    def admin_group_delete(group_id):
        require_admin()
        group = db.session.get(Group, group_id) or abort(404)
        if not is_custom_group(group):
            flash("默认用户组不能删除", "danger")
            return redirect(url_for("admin_users"))
        group_name = group.name
        member_count = User.query.filter_by(group_id=group.id).update({"group_id": None})
        db.session.delete(group)
        db.session.commit()
        record_audit(
            "delete_group",
            target_type="group",
            target_id=group_id,
            target_label=group_name,
            metadata={"member_count": member_count},
        )
        flash("用户组已删除", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/create", methods=["POST"])
    @login_required
    def admin_user_create():
        require_user_manager()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or len(password) < 6:
            flash("用户名和至少 6 位密码都必填", "danger")
            return redirect(url_for("admin_users"))
        if User.query.filter_by(username=username).first():
            flash("用户名已存在", "danger")
            return redirect(url_for("admin_users"))

        if is_admin():
            role = safe_role(request.form.get("role"))
            group_id = form_int("group_id")
            enabled = bool(request.form.get("enabled"))
        else:
            role = "user"
            group_id = current_user.group_id
            enabled = True
        if role == "group_leader" and not group_id:
            flash("组长必须指定所属用户组", "danger")
            return redirect(url_for("admin_users"))
        if role == "admin":
            group_id, error = admin_group_id_or_error(group_id)
            if error:
                flash(error, "danger")
                return redirect(url_for("admin_users"))
        elif role == "user" and not group_id:
            flash("普通用户必须指定所属用户组", "danger")
            return redirect(url_for("admin_users"))
        else:
            group_id, error = business_group_id_or_error(group_id)
            if error:
                flash(error, "danger")
                return redirect(url_for("admin_users"))

        user = User(
            username=username,
            display_name=request.form.get("display_name", "").strip() or username,
            role=role,
            enabled=enabled,
            group_id=group_id,
            email=request.form.get("email", "").strip() or None,
        )
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        record_audit(
            "create_user",
            target_type="user",
            target_id=user.id,
            target_label=user.username,
            metadata={"role": user.role, "enabled": user.enabled, "group_id": user.group_id},
        )
        flash("用户已创建", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/update", methods=["POST"])
    @login_required
    def admin_user_update(user_id):
        require_user_manager()
        user = db.session.get(User, user_id) or abort(404)
        if not can_manage_user(user):
            abort(403)
        user.display_name = request.form.get("display_name", "").strip() or user.username
        user.email = request.form.get("email", "").strip() or None
        if is_admin():
            user.role = safe_role(request.form.get("role"))
            user.enabled = bool(request.form.get("enabled"))
            group_id = form_int("group_id")
            if user.role == "group_leader" and not group_id:
                flash("组长必须指定所属用户组", "danger")
                return redirect(url_for("admin_users"))
            if user.role == "admin":
                group_id, error = admin_group_id_or_error(group_id)
                if error:
                    flash(error, "danger")
                    return redirect(url_for("admin_users"))
            user.group_id = group_id
        else:
            user.enabled = bool(request.form.get("enabled"))
            new_group_id = form_int("group_id")
            if new_group_id and new_group_id != current_user.group_id:
                user.group_id = new_group_id
        if user.id == current_user.id:
            user.role = current_user.role
            user.enabled = True
        db.session.commit()
        record_audit(
            "update_user",
            target_type="user",
            target_id=user.id,
            target_label=user.username,
            metadata={"role": user.role, "enabled": user.enabled, "group_id": user.group_id},
        )
        flash("用户权限已更新", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/reset_password", methods=["POST"])
    @login_required
    def admin_user_reset_password(user_id):
        require_user_manager()
        user = db.session.get(User, user_id) or abort(404)
        if not can_manage_user(user):
            abort(403)
        password = request.form.get("password", "")
        if len(password) < 6:
            flash("新密码长度至少 6 位", "danger")
            return redirect(url_for("admin_users"))
        user.set_password(password)
        db.session.commit()
        record_audit("reset_password", target_type="user", target_id=user.id, target_label=user.username)
        flash("密码已重置", "success")
        return redirect(url_for("admin_users"))

    @app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
    @login_required
    def admin_user_delete(user_id):
        require_admin()
        user = db.session.get(User, user_id) or abort(404)
        if user.id == current_user.id:
            flash("不能删除当前登录账号", "danger")
            return redirect(url_for("admin_users"))
        if user_has_business_records(user):
            flash("该用户已有会议、议题或附件等业务数据，请先禁用账号或转移数据", "danger")
            return redirect(url_for("admin_users"))
        username = user.username
        role = user.role
        group_id = user.group_id
        db.session.delete(user)
        db.session.commit()
        record_audit(
            "delete_user",
            target_type="user",
            target_id=user_id,
            target_label=username,
            metadata={"role": role, "group_id": group_id},
        )
        flash("用户已删除", "success")
        return redirect(url_for("admin_users"))

    @app.route("/meetings")
    @login_required
    def meeting_list():
        filters = meeting_list_filters()
        search = filters["search"]
        status = filters["status"]
        query = Meeting.query
        if status:
            query = query.filter_by(status=status)
        if filters["meeting_date_start"]:
            query = query.filter(Meeting.meeting_date >= filters["meeting_date_start"])
        if filters["meeting_date_end"]:
            query = query.filter(Meeting.meeting_date <= filters["meeting_date_end"])
        if filters["created_start"]:
            query = query.filter(Meeting.created_at >= filters["created_start"])
        if filters["created_end"]:
            query = query.filter(Meeting.created_at < filters["created_end"])
        if filters["location"]:
            query = query.filter(Meeting.location == filters["location"])
        if filters["hoster_name"]:
            hoster_name = filters["hoster_name"]
            query = query.outerjoin(User, Meeting.host_user_id == User.id).filter(
                db.func.coalesce(db.func.nullif(Meeting.host, ""), User.display_name) == hoster_name
            )
        if should_limit_meetings_to_own_approved_topics():
            query = query.join(
                Topic,
                db.and_(
                    Topic.meeting_id == Meeting.id,
                    Topic.created_by == current_user.id,
                    Topic.workflow_status == "approved",
                ),
            ).distinct()
        topic_join_condition = Meeting.id == Topic.meeting_id
        if not is_admin():
            topic_join_condition = db.and_(topic_join_condition, Topic.created_by == current_user.id)
        if search:
            query = query.filter(Meeting.title.like(f"%{search}%"))
        if filters["favorite"]:
            query = query.join(
                MeetingFavorite,
                db.and_(
                    MeetingFavorite.meeting_id == Meeting.id,
                    MeetingFavorite.user_id == current_user.id,
                ),
            )
        if filters["topic_category"]:
            query = query.filter(Meeting.category.in_(topic_category_query_values(filters["topic_category"])))
        if filters["plan_version_id"]:
            query = query.filter(Meeting.plan_version_id == filters["plan_version_id"])
        if filters["plan_round_id"]:
            query = query.filter(Meeting.plan_round_id == filters["plan_round_id"])
        meetings = query.order_by(Meeting.meeting_date.desc(), Meeting.updated_at.desc()).all()
        meeting_readiness = {
            meeting.id: meeting_readiness_summary(meeting, visible_topics_for_readiness(meeting))
            for meeting in meetings
        }
        if filters["readiness_status"]:
            meetings = [
                meeting for meeting in meetings
                if meeting_readiness[meeting.id]["status_key"] == filters["readiness_status"]
            ]
        location_options = [
            row[0]
            for row in db.session.query(Meeting.location)
            .filter(Meeting.location.isnot(None), Meeting.location != "")
            .distinct()
            .order_by(Meeting.location.asc())
            .all()
        ]
        favorite_meeting_ids = {
            row[0]
            for row in db.session.query(MeetingFavorite.meeting_id)
            .filter(MeetingFavorite.user_id == current_user.id)
            .all()
        }
        hoster_names_query = (
            db.session.query(
                db.func.coalesce(db.func.nullif(Meeting.host, ""), User.display_name).label("name")
            )
            .select_from(Meeting)
            .outerjoin(User, Meeting.host_user_id == User.id)
        )
        hoster_options = sorted({row.name for row in hoster_names_query.all() if row.name})
        return render_template(
            "meetings/list.html",
            meetings=meetings,
            filters=filters,
            search=search,
            status=status,
            location_options=location_options,
            hoster_options=hoster_options,
            meeting_readiness=meeting_readiness,
            favorite_meeting_ids=favorite_meeting_ids,
        )

    @app.route("/meetings/<meeting_no>/favorite", methods=["POST"])
    @login_required
    def meeting_favorite_toggle(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        if not (can_manage_meetings() or can_review_meeting(meeting) or visible_topics_for_meeting(meeting)):
            abort(403)
        favorite = MeetingFavorite.query.filter_by(
            user_id=current_user.id,
            meeting_id=meeting.id,
        ).first()
        if favorite:
            db.session.delete(favorite)
        else:
            db.session.add(MeetingFavorite(user_id=current_user.id, meeting_id=meeting.id))
        db.session.commit()
        return redirect(request.form.get("next") or url_for("meeting_list"))

    @app.route("/meetings/create", methods=["GET", "POST"])
    @login_required
    def meeting_create():
        require_meeting_manager()
        if request.method == "POST":
            host_user = db.session.get(User, form_int("host_user_id") or current_user.id) or current_user
            if host_user.role != "admin":
                host_user = current_user
            plan_version = parse_plan_version_id(request.form.get("plan_version_id"))
            plan_round = parse_plan_round_id(request.form.get("plan_round_id"), plan_version)
            meeting_category = normalize_topic_category(request.form.get("category"))
            meeting = Meeting(
                meeting_no=Meeting.next_meeting_no(),
                title=request.form.get("title", "").strip(),
                meeting_date=datetime.strptime(request.form.get("meeting_date"), "%Y-%m-%d").date(),
                location=request.form.get("location", "").strip(),
                host=request.form.get("host", "").strip() or host_user.display_name,
                status=request.form.get("status", "draft"),
                plan_version_id=plan_version.id,
                plan_round_id=plan_round.id,
                category=meeting_category,
                created_by=current_user.id,
                host_user_id=host_user.id,
            )
            db.session.add(meeting)
            db.session.flush()

            titles = request.form.getlist("topic_title[]")
            owners = request.form.getlist("topic_owner[]")
            orders = request.form.getlist("topic_order[]")
            durations = request.form.getlist("topic_duration_minutes[]")
            for index, title in enumerate(titles):
                title = title.strip()
                if not title:
                    continue
                db.session.add(
                    Topic(
                        meeting_id=meeting.id,
                        title=title,
                        category=meeting.category,
                        plan_version=meeting.plan_version_name,
                        plan_version_id=meeting.plan_version_id,
                        plan_round_id=meeting.plan_round_id,
                        owner=value_at(owners, index),
                        duration_minutes=normalize_topic_duration(value_at(durations, index)),
                        present_order=int(value_at(orders, index) or index + 1),
                        status="pending",
                        created_by=current_user.id,
                        workflow_status="approved",
                    )
                )
            db.session.commit()
            flash("会议创建成功", "success")
            record_audit(
                "create_meeting",
                target_type="meeting",
                target_id=meeting.id,
                target_label=meeting.meeting_no,
                metadata={"title": meeting.title, "topic_count": len([title for title in titles if title.strip()])},
            )
            return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no))

        admin_users = User.query.filter_by(role="admin", enabled=True).order_by(User.display_name).all()
        return render_template("meetings/create.html", admin_users=admin_users)

    @app.route("/meetings/<meeting_no>/edit", methods=["GET", "POST"])
    @login_required
    def meeting_edit(meeting_no):
        require_meeting_manager()
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        if request.method == "POST":
            if meeting.topics.count():
                posted_version, posted_round = current_or_posted_plan_scope(request.form, meeting)
                posted_category = normalize_topic_category(request.form.get("category") or meeting.category)
                if (
                    posted_version.id != meeting.plan_version_id
                    or posted_round.id != meeting.plan_round_id
                    or posted_category != normalize_topic_category(meeting.category)
                ):
                    flash("已有议题的会议不能修改 Plan Version、Round 或类别", "danger")
                    return redirect(url_for("meeting_edit", meeting_no=meeting.meeting_no))
            meeting.title = request.form.get("title", "").strip()
            meeting.meeting_date = datetime.strptime(request.form.get("meeting_date"), "%Y-%m-%d").date()
            meeting.location = request.form.get("location", "").strip()
            meeting.host = request.form.get("host", "").strip()
            meeting.status = request.form.get("status", meeting.status)
            if not meeting.topics.count():
                plan_version, plan_round = current_or_posted_plan_scope(request.form, meeting)
                meeting.plan_version_id = plan_version.id
                meeting.plan_round_id = plan_round.id
                meeting.category = normalize_topic_category(request.form.get("category") or meeting.category)
            host_user = db.session.get(User, form_int("host_user_id") or 0)
            if host_user and host_user.role == "admin":
                meeting.host_user_id = host_user.id
                meeting.host = meeting.host or host_user.display_name
            db.session.commit()
            metadata = {"title": meeting.title, "status": meeting.status}
            record_audit(
                "update_meeting",
                target_type="meeting",
                target_id=meeting.id,
                target_label=meeting.meeting_no,
                metadata=metadata,
            )
            messages = ["会议信息已保存"]
            flash("，".join(messages), "success")
            return redirect(url_for("meeting_list"))

        admin_users = User.query.filter_by(role="admin", enabled=True).order_by(User.display_name).all()
        return render_template("meetings/create.html", meeting=meeting, admin_users=admin_users)

    @app.route("/meetings/<meeting_no>/delete", methods=["POST"])
    @login_required
    def meeting_delete(meeting_no):
        require_meeting_manager()
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        meeting_id = meeting.id
        meeting_label = meeting.meeting_no
        rejected_count, reverted_count = release_meeting_topics(
            meeting, "已删除", current_user.id
        )
        db.session.delete(meeting)
        db.session.commit()
        metadata = {}
        if rejected_count:
            metadata["auto_rejected_topics"] = rejected_count
        if reverted_count:
            metadata["reverted_topics"] = reverted_count
        record_audit(
            "delete_meeting",
            target_type="meeting",
            target_id=meeting_id,
            target_label=meeting_label,
            metadata=metadata or None,
        )
        messages = ["会议已删除"]
        if rejected_count:
            messages.append(f"{rejected_count} 个待审批议题已自动退回议题池")
        if reverted_count:
            messages.append(f"{reverted_count} 个已通过议题已回退到待审批")
        flash("，".join(messages), "success")
        return redirect(url_for("meeting_list"))

    @app.route("/meetings/<meeting_no>")
    @login_required
    def meeting_detail(meeting_no):
        meeting = (
            Meeting.query.options(joinedload(Meeting.minutes))
            .filter_by(meeting_no=meeting_no)
            .first_or_404()
        )
        topics = visible_topics_for_meeting(meeting)
        if not topics and not can_review_meeting(meeting) and not can_manage_meetings():
            abort(403)
        pending_topics = pending_topics_for_meeting(meeting) if can_review_meeting(meeting) or can_manage_meetings() else []
        view = request.args.get("view", "").strip()
        selected_topic = None
        selected_topic_attachments = []
        selected_topic_readiness = None
        selected_topic_review_prompts = []
        selected_topic_review_prompt = None
        if view == "minutes":
            pass
        else:
            view = ""
            selected_topic = select_topic(topics + pending_topics, request.args.get("topic_id", type=int))
            if selected_topic and not can_view_topic(selected_topic):
                abort(403)
            if selected_topic:
                selected_topic_attachments = (
                    Attachment.query.options(joinedload(Attachment.material_document))
                    .filter_by(topic_id=selected_topic.id)
                    .order_by(Attachment.uploaded_at.desc())
                    .all()
                )
                selected_topic_readiness = topic_completeness_from_values(
                    selected_topic,
                    attachment_count=len(selected_topic_attachments),
                    latest_review=selected_topic.latest_material_review,
                )
                selected_topic_review_prompts = available_ai_prompts_for_topic(selected_topic)
                selected_topic_review_prompt = selected_ai_prompt_from_available(
                    selected_topic,
                    selected_topic_review_prompts,
                )
        return render_template(
            "meetings/detail.html",
            meeting=meeting,
            topics=topics,
            pending_topics=pending_topics,
            selected_topic=selected_topic,
            selected_topic_attachments=selected_topic_attachments,
            selected_topic_readiness=selected_topic_readiness,
            selected_topic_review_prompts=selected_topic_review_prompts,
            selected_topic_review_prompt=selected_topic_review_prompt,
            minutes=meeting.minutes,
            view=view,
        )

    @app.route("/meetings/<meeting_no>/topics", methods=["POST"])
    @login_required
    def topic_create(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_reviewer(meeting)
        topic = Topic(
            meeting_id=meeting.id,
            title=request.form.get("title", "").strip(),
            category=meeting.category,
            plan_version=meeting.plan_version_name,
            plan_version_id=meeting.plan_version_id,
            plan_round_id=meeting.plan_round_id,
            owner=request.form.get("owner", "").strip(),
            duration_minutes=normalize_topic_duration(request.form.get("duration_minutes")),
            present_order=next_topic_order(meeting),
            status="pending",
            created_by=current_user.id,
            workflow_status="approved",
        )
        if not topic.title:
            flash("议题标题不能为空", "danger")
            return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no))
        db.session.add(topic)
        db.session.commit()
        flash("议题已添加", "success")
        record_audit(
            "create_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"meeting_no": meeting.meeting_no, "workflow_status": topic.workflow_status},
        )
        return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no, topic_id=topic.id))

    @app.route("/meetings/<meeting_no>/topics/reorder", methods=["POST"])
    @login_required
    def topic_reorder(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_reviewer(meeting)
        data = request.get_json(silent=True) or {}
        ordered_ids = [int(value) for value in data.get("topic_ids", []) if str(value).isdigit()]
        topics = meeting.topics.filter_by(workflow_status="approved").all()
        topic_by_id = {topic.id: topic for topic in topics}
        if len(ordered_ids) != len(topic_by_id) or set(ordered_ids) != set(topic_by_id):
            abort(400)
        for index, topic_id in enumerate(ordered_ids, start=1):
            topic_by_id[topic_id].present_order = index
        db.session.commit()
        record_audit(
            "reorder_topic",
            target_type="meeting",
            target_id=meeting.id,
            target_label=meeting.meeting_no,
            metadata={"topic_ids": ordered_ids},
        )
        return {"topic_orders": [{"topic_id": topic_id, "present_order": index} for index, topic_id in enumerate(ordered_ids, start=1)]}

    @app.route("/topics/drafts")
    @login_required
    def topic_drafts():
        filters = topic_draft_filters()

        def base_visible_topics():
            q = Topic.query
            if not is_admin():
                shared_ids = [row.topic_id for row in TopicShare.query.filter_by(user_id=current_user.id).all()]
                if shared_ids:
                    q = q.filter(or_(Topic.created_by == current_user.id, Topic.id.in_(shared_ids)))
                else:
                    q = q.filter(Topic.created_by == current_user.id)
            return q

        query = base_visible_topics()
        if filters["search"]:
            query = query.filter(Topic.title.like(f"%{filters['search']}%"))
        if filters["status"]:
            query = query.filter(Topic.workflow_status == filters["status"])
        if filters["plan_version_id"]:
            query = query.filter(Topic.plan_version_id == filters["plan_version_id"])
        if filters["plan_round_id"]:
            query = query.filter(Topic.plan_round_id == filters["plan_round_id"])
        if filters["topic_category"]:
            query = query.filter(Topic.category.in_(topic_category_query_values(filters["topic_category"])))
        if filters["creator_name"]:
            query = query.outerjoin(User, Topic.created_by == User.id).filter(
                User.display_name == filters["creator_name"]
            )
        if filters["target_meeting_no"]:
            meeting_ids = [
                m.id for m in Meeting.query.filter_by(meeting_no=filters["target_meeting_no"]).all()
            ]
            if meeting_ids:
                query = query.filter(
                    or_(Topic.requested_meeting_id.in_(meeting_ids), Topic.meeting_id.in_(meeting_ids))
                )
            else:
                query = query.filter(Topic.id == -1)
        if filters["created_start"]:
            query = query.filter(Topic.created_at >= filters["created_start"])
        if filters["created_end"]:
            query = query.filter(Topic.created_at < filters["created_end"])
        if filters["updated_start"]:
            query = query.filter(Topic.updated_at >= filters["updated_start"])
        if filters["updated_end"]:
            query = query.filter(Topic.updated_at < filters["updated_end"])
        topics = query.order_by(Topic.updated_at.desc(), Topic.created_at.desc()).all()
        topic_completeness_map = {topic.id: topic_completeness(topic) for topic in topics}

        scope_topics = base_visible_topics().all()
        creator_options = sorted({t.creator.display_name for t in scope_topics if t.creator})
        target_meeting_options = sorted(
            {
                (t.requested_meeting.meeting_no if t.requested_meeting
                 else (t.meeting.meeting_no if t.meeting else None))
                for t in scope_topics
            } - {None}
        )
        meetings = selectable_meetings_for_current_user().all()
        return render_template(
            "topics/drafts.html",
            topics=topics,
            meetings=meetings,
            status=filters["status"],
            filters=filters,
            creator_options=creator_options,
            target_meeting_options=target_meeting_options,
            topic_completeness_map=topic_completeness_map,
        )

    @app.route("/topics/drafts/create", methods=["GET", "POST"])
    @login_required
    def topic_draft_create():
        if request.method == "POST":
            initial_file = request.files.get("file")
            if initial_file and initial_file.filename and not is_allowed(
                initial_file.filename,
                current_app.config["ALLOWED_EXTENSIONS"],
            ):
                allowed = ", ".join(sorted(current_app.config["ALLOWED_EXTENSIONS"]))
                flash(f"不支持的文件类型，仅支持：{allowed}", "danger")
                return redirect(url_for("topic_draft_create"))
            plan_version = parse_plan_version_id(request.form.get("plan_version_id"))
            plan_round = parse_plan_round_id(request.form.get("plan_round_id"), plan_version)
            topic = Topic(
                title=request.form.get("title", "").strip(),
                category=normalize_topic_category(request.form.get("category")),
                plan_version=plan_version.name,
                plan_version_id=plan_version.id,
                plan_round_id=plan_round.id,
                owner=request.form.get("owner", "").strip() or current_user.display_name,
                duration_minutes=normalize_topic_duration(request.form.get("duration_minutes")),
                background=request.form.get("background", "").strip(),
                purpose=request.form.get("purpose", "").strip(),
                present_order=1,
                status="pending",
                created_by=current_user.id,
                requested_meeting_id=form_int("requested_meeting_id"),
                workflow_status="draft",
            )
            if not topic.title:
                flash("议题标题不能为空", "danger")
                return redirect(url_for("topic_draft_create"))
            if topic.requested_meeting_id:
                target_meeting = db.session.get(Meeting, topic.requested_meeting_id)
                if not target_meeting or not meeting_scope_matches_topic(target_meeting, topic):
                    flash(scope_mismatch_message(), "danger")
                    return redirect(url_for("topic_draft_create"))
            db.session.add(topic)
            db.session.flush()
            attachment = None
            if initial_file and initial_file.filename:
                saved, error = save_topic_file(
                    initial_file,
                    attachment_dir(app, topic),
                    app.config["ALLOWED_EXTENSIONS"],
                )
                if error:
                    db.session.rollback()
                    flash(error, "danger")
                    return redirect(url_for("topic_draft_create"))
                decrypt_saved_attachment_file(saved, topic)
                attachment = Attachment(
                    topic_id=topic.id,
                    original_filename=saved["original_filename"],
                    stored_filename=saved["stored_filename"],
                    file_type=saved["file_type"],
                    file_size=saved["file_size"],
                    uploaded_by=current_user.id,
                )
                db.session.add(attachment)
            db.session.commit()
            flash("议题已创建", "success")
            record_audit(
                "create_topic",
                target_type="topic",
                target_id=topic.id,
                target_label=topic.title,
                metadata={"requested_meeting_id": topic.requested_meeting_id, "workflow_status": topic.workflow_status},
            )
            if attachment:
                record_audit(
                    "upload_attachment",
                    target_type="attachment",
                    target_id=attachment.id,
                    target_label=attachment.original_filename,
                    metadata={
                        "topic_id": topic.id,
                        "topic_title": topic.title,
                        "file_type": attachment.file_type,
                    },
                )
                record_decryption_failure_if_needed(saved, topic, attachment)
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))

        return render_template("topics/edit.html", topic=None, meetings=selectable_meetings_for_topic(None))

    @app.route("/topics/drafts/<int:topic_id>/edit", methods=["GET", "POST"])
    @login_required
    def topic_draft_edit(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_view_topic(topic):
            abort(403)
        if request.method == "POST":
            if not can_edit_draft(topic):
                abort(403)
            draft_action = request.form.get("draft_action", "save")
            topic.title = request.form.get("title", "").strip()
            topic.category = normalize_topic_category(request.form.get("category"))
            plan_version = parse_plan_version_id(request.form.get("plan_version_id"))
            plan_round = parse_plan_round_id(request.form.get("plan_round_id"), plan_version)
            topic.plan_version = plan_version.name
            topic.plan_version_id = plan_version.id
            topic.plan_round_id = plan_round.id
            topic.owner = request.form.get("owner", "").strip()
            topic.duration_minutes = normalize_topic_duration(request.form.get("duration_minutes"))
            topic.background = request.form.get("background", "").strip()
            topic.purpose = request.form.get("purpose", "").strip()
            topic.requested_meeting_id = form_int("requested_meeting_id")
            if not topic.title:
                flash("议题标题不能为空", "danger")
                return redirect(url_for("topic_draft_edit", topic_id=topic.id))
            if topic.requested_meeting_id:
                target_meeting = db.session.get(Meeting, topic.requested_meeting_id)
                if not target_meeting or not meeting_scope_matches_topic(target_meeting, topic):
                    flash(scope_mismatch_message(), "danger")
                    return redirect(url_for("topic_draft_edit", topic_id=topic.id))
            if topic.workflow_status in {"rejected", "withdrawn"}:
                topic.workflow_status = "draft"
                topic.review_comment = ""
                topic.reviewed_by = None
                topic.reviewed_at = None
            if draft_action == "submit":
                if not can_submit_draft(topic):
                    abort(403)
                if topic.requested_meeting_id:
                    target_meeting = db.session.get(Meeting, topic.requested_meeting_id)
                    if not target_meeting:
                        topic.requested_meeting_id = None
                        db.session.commit()
                        flash("目标会议不存在或已被删除，请重新选择", "danger")
                        return redirect(url_for("topic_draft_edit", topic_id=topic.id))
                    if target_meeting.status == "completed":
                        db.session.commit()
                        flash(f"会议 {target_meeting.meeting_no} 已结束，无法再提交议题", "danger")
                        return redirect(url_for("topic_draft_edit", topic_id=topic.id))
                topic.workflow_status = "submitted"
                topic.submitted_at = datetime.utcnow()
                topic.meeting_id = None
                topic.review_comment = ""
                topic.reviewed_by = None
                topic.reviewed_at = None
                db.session.commit()
                flash("议题已提交审批", "success")
                record_audit(
                    "submit_topic",
                    target_type="topic",
                    target_id=topic.id,
                    target_label=topic.title,
                    metadata={"requested_meeting_id": topic.requested_meeting_id},
                )
                return redirect(url_for("topic_drafts"))
            db.session.commit()
            flash("议题草稿已保存", "success")
            record_audit(
                "update_topic",
                target_type="topic",
                target_id=topic.id,
                target_label=topic.title,
                metadata={"requested_meeting_id": topic.requested_meeting_id, "workflow_status": topic.workflow_status},
            )
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))

        shareable_users = []
        if can_share_topic(topic):
            shared_ids = {share.user_id for share in topic.shares}
            q = User.query.filter(User.id != topic.created_by, User.enabled.is_(True))
            if shared_ids:
                q = q.filter(User.id.notin_(shared_ids))
            shareable_users = q.order_by(User.display_name.asc()).all()
        return render_template(
            "topics/edit.html",
            topic=topic,
            meetings=selectable_meetings_for_topic(topic),
            shareable_users=shareable_users,
            can_share=can_share_topic(topic),
        )

    @app.route("/topics/drafts/<int:topic_id>/submit", methods=["POST"])
    @login_required
    def topic_draft_submit(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_submit_draft(topic):
            abort(403)
        if "requested_meeting_id" in request.form:
            topic.requested_meeting_id = form_int("requested_meeting_id")
        if topic.requested_meeting_id:
            target_meeting = db.session.get(Meeting, topic.requested_meeting_id)
            if not target_meeting:
                topic.requested_meeting_id = None
                db.session.commit()
                flash("目标会议不存在或已被删除，请重新选择", "danger")
                return redirect(url_for("topic_draft_edit", topic_id=topic.id))
            if target_meeting.status == "completed":
                flash(f"会议 {target_meeting.meeting_no} 已结束，无法再提交议题", "danger")
                return redirect(url_for("topic_draft_edit", topic_id=topic.id))
            if not meeting_scope_matches_topic(target_meeting, topic):
                flash(scope_mismatch_message(), "danger")
                return redirect(url_for("topic_draft_edit", topic_id=topic.id))
        topic.workflow_status = "submitted"
        topic.submitted_at = datetime.utcnow()
        topic.meeting_id = None
        topic.review_comment = ""
        topic.reviewed_by = None
        topic.reviewed_at = None
        db.session.commit()
        flash("议题已提交审批", "success")
        record_audit(
            "submit_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"requested_meeting_id": topic.requested_meeting_id},
        )
        return redirect(url_for("topic_drafts"))

    @app.route("/topics/drafts/<int:topic_id>/withdraw", methods=["POST"])
    @login_required
    def topic_draft_withdraw(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if topic.created_by != current_user.id or topic.workflow_status != "submitted":
            abort(403)
        topic.workflow_status = "withdrawn"
        topic.meeting_id = None
        db.session.commit()
        flash("议题申请已撤回", "success")
        record_audit("withdraw_topic", target_type="topic", target_id=topic.id, target_label=topic.title)
        return redirect(url_for("topic_drafts"))

    @app.route("/topics/<int:topic_id>/share", methods=["POST"])
    @login_required
    def topic_share(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_share_topic(topic):
            abort(403)
        user_id = form_int("user_id")
        target = db.session.get(User, user_id) if user_id else None
        if not target or target.id == topic.created_by:
            flash("请选择有效的授权对象", "danger")
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))
        existing = TopicShare.query.filter_by(topic_id=topic.id, user_id=target.id).first()
        if existing:
            flash("该用户已拥有查看权限", "info")
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))
        share = TopicShare(topic_id=topic.id, user_id=target.id, granted_by=current_user.id)
        db.session.add(share)
        db.session.commit()
        record_audit(
            "share_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"shared_with": target.username},
        )
        flash(f"已将议题授权给 {target.display_name}", "success")
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/topics/<int:topic_id>/share/<int:user_id>/revoke", methods=["POST"])
    @login_required
    def topic_share_revoke(topic_id, user_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_share_topic(topic):
            abort(403)
        share = TopicShare.query.filter_by(topic_id=topic.id, user_id=user_id).first() or abort(404)
        target_label = share.user.username if share.user else str(user_id)
        db.session.delete(share)
        db.session.commit()
        record_audit(
            "revoke_topic_share",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"revoked_from": target_label},
        )
        flash("已撤销该用户的查看权限", "success")
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/topics/<int:topic_id>/approve", methods=["POST"])
    @login_required
    def topic_approve(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        meeting = topic.requested_meeting or topic.meeting
        require_reviewer(meeting)
        if topic.workflow_status != "submitted" or not topic.requested_meeting:
            abort(400)
        if meeting.status == "completed":
            flash(f"会议 {meeting.meeting_no} 已结束，无法再通过新的议题", "danger")
            return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no, topic_id=topic.id))
        if not meeting_scope_matches_topic(meeting, topic):
            flash(scope_mismatch_message(), "danger")
            return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no, topic_id=topic.id))
        topic.meeting_id = topic.requested_meeting_id
        topic.requested_meeting_id = None
        topic.workflow_status = "approved"
        topic.reviewed_by = current_user.id
        topic.reviewed_at = datetime.utcnow()
        topic.review_comment = request.form.get("review_comment", "").strip()
        topic.present_order = next_topic_order(meeting)
        db.session.commit()
        flash("议题已通过并加入会议", "success")
        record_audit(
            "approve_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"meeting_no": meeting.meeting_no, "review_comment": topic.review_comment},
        )
        return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no, topic_id=topic.id))

    @app.route("/topics/<int:topic_id>/reject", methods=["POST"])
    @login_required
    def topic_reject(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        meeting = topic.requested_meeting or topic.meeting
        require_reviewer(meeting)
        if topic.workflow_status != "submitted":
            abort(400)
        topic.workflow_status = "rejected"
        topic.meeting_id = None
        topic.reviewed_by = current_user.id
        topic.reviewed_at = datetime.utcnow()
        topic.review_comment = request.form.get("review_comment", "").strip()
        db.session.commit()
        flash("议题已驳回", "info")
        record_audit(
            "reject_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"meeting_no": meeting.meeting_no if meeting else None, "review_comment": topic.review_comment},
        )
        return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no))

    @app.route("/topics/<int:topic_id>/update", methods=["POST"])
    @login_required
    def topic_update(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_edit_topic(topic):
            abort(403)
        topic.title = request.form.get("title", "").strip()
        if topic.meeting:
            apply_meeting_scope_to_topic(topic, topic.meeting)
        else:
            topic.category = normalize_topic_category(request.form.get("category"))
            plan_version = parse_plan_version_id(request.form.get("plan_version_id"))
            plan_round = parse_plan_round_id(request.form.get("plan_round_id"), plan_version)
            topic.plan_version = plan_version.name
            topic.plan_version_id = plan_version.id
            topic.plan_round_id = plan_round.id
        topic.owner = request.form.get("owner", "").strip()
        topic.duration_minutes = normalize_topic_duration(request.form.get("duration_minutes"))
        topic.status = request.form.get("status", "pending")
        topic.background = request.form.get("background", "").strip()
        topic.purpose = request.form.get("purpose", "").strip()
        db.session.commit()
        flash("议题信息已保存", "success")
        record_audit(
            "update_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"meeting_no": topic.meeting.meeting_no if topic.meeting else None, "status": topic.status},
        )
        if topic.meeting:
            return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/topics/<int:topic_id>/meeting-decision", methods=["POST"])
    @login_required
    def topic_meeting_decision(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if topic.workflow_status != "approved" or not topic.meeting_id:
            abort(400)
        if not can_decide_meeting_topic(topic):
            abort(403)
        decision = request.form.get("decision", "").strip()
        if decision not in TOPIC_DECISION_STATUS_LABELS or decision == "pending":
            abort(400)
        topic.decision_status = decision
        topic.decision_by = current_user.id
        topic.decision_at = datetime.utcnow()
        topic.decision_comment = request.form.get("decision_comment", "").strip()
        if decision == "conditional_approved" and not topic.decision_comment:
            flash("请填写有条件通过的条件", "danger")
            return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id, mode="report"))
        db.session.commit()
        flash("现场决策已保存", "success")
        record_audit(
            "decide_topic",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={
                "meeting_no": topic.meeting.meeting_no if topic.meeting else None,
                "decision_status": topic.decision_status,
            },
        )
        redirect_args = {"meeting_no": topic.meeting.meeting_no, "topic_id": topic.id}
        if request.form.get("mode") == "report":
            redirect_args["mode"] = "report"
        return redirect(url_for("meeting_detail", **redirect_args))

    @app.route("/topics/<int:topic_id>/delete", methods=["POST"])
    @login_required
    def topic_delete(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_delete_topic(topic):
            abort(403)
        meeting_no = topic.meeting.meeting_no if topic.meeting else None
        topic_title = topic.title
        topic_id_value = topic.id
        db.session.delete(topic)
        db.session.commit()
        record_audit(
            "delete_topic",
            target_type="topic",
            target_id=topic_id_value,
            target_label=topic_title,
            metadata={"meeting_no": meeting_no},
        )
        flash("议题已删除", "success")
        if meeting_no:
            return redirect(url_for("meeting_detail", meeting_no=meeting_no))
        return redirect(url_for("topic_drafts"))

    @app.route("/topics/<int:topic_id>/attachments", methods=["POST"])
    @login_required
    def attachment_upload(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        if not can_edit_topic(topic):
            abort(403)
        saved, error = save_topic_file(
            request.files.get("file"),
            attachment_dir(app, topic),
            app.config["ALLOWED_EXTENSIONS"],
        )
        if error:
            flash(error, "danger")
        else:
            decrypt_saved_attachment_file(saved, topic)
            attachment = Attachment(
                topic_id=topic.id,
                original_filename=saved["original_filename"],
                stored_filename=saved["stored_filename"],
                file_type=saved["file_type"],
                file_size=saved["file_size"],
                uploaded_by=current_user.id,
            )
            db.session.add(attachment)
            db.session.commit()
            try:
                material_document = index_attachment_material(attachment, saved.get("file_path"))
            except Exception as exc:
                current_app.logger.warning("Material RAG indexing failed for attachment %s: %s", attachment.id, exc)
                flash(f"附件上传成功，但材料索引失败：{exc}", "warning")
            else:
                if material_document.status == "indexed":
                    flash("附件上传成功，材料已索引", "success")
                elif material_document.status == "unsupported":
                    flash("附件上传成功；该格式暂不支持材料索引", "info")
                else:
                    flash(f"附件上传成功，但材料索引未完成：{material_document.error_message or material_document.status_label}", "warning")
            record_audit(
                "upload_attachment",
                target_type="attachment",
                target_id=attachment.id,
                target_label=attachment.original_filename,
                metadata={"topic_id": topic.id, "topic_title": topic.title, "file_type": attachment.file_type},
            )
            record_decryption_failure_if_needed(saved, topic, attachment)
        if topic.meeting:
            return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/attachments/<int:attachment_id>/download")
    @login_required
    def attachment_download(attachment_id):
        attachment = db.session.get(Attachment, attachment_id) or abort(404)
        if not can_view_topic(attachment.topic):
            abort(403)
        record_audit(
            "download_attachment",
            target_type="attachment",
            target_id=attachment.id,
            target_label=attachment.original_filename,
            metadata={"topic_id": attachment.topic_id},
        )
        return send_file(
            attachment_path(app, attachment),
            as_attachment=True,
            download_name=attachment.original_filename,
        )

    @app.route("/attachments/<int:attachment_id>/preview")
    @login_required
    def attachment_preview(attachment_id):
        attachment = db.session.get(Attachment, attachment_id) or abort(404)
        if not can_view_topic(attachment.topic):
            abort(403)
        if not attachment.can_preview_inline:
            return redirect(url_for("attachment_download", attachment_id=attachment.id))
        record_audit(
            "preview_attachment",
            target_type="attachment",
            target_id=attachment.id,
            target_label=attachment.original_filename,
            metadata={"topic_id": attachment.topic_id, "file_type": attachment.effective_file_type},
        )
        if attachment.is_fileview_document and current_app.config.get("KKFILEVIEW_ENABLED", True):
            return redirect(fileview_preview_url(attachment))
        if attachment.is_powerpoint:
            return powerpoint_pdf_preview_response(attachment)
        return send_file(attachment_path(app, attachment), mimetype=mimetype_for(attachment))

    @app.route("/attachments/<int:attachment_id>/fileview-source")
    def attachment_fileview_source(attachment_id):
        attachment = db.session.get(Attachment, attachment_id) or abort(404)
        user = verify_fileview_token(request.args.get("token", ""), attachment.id)
        if not user or not can_view_topic(attachment.topic, user):
            abort(403)
        return send_file(
            attachment_path(app, attachment),
            as_attachment=False,
            download_name=fileview_filename_for_attachment(attachment),
            mimetype=mimetype_for(attachment),
        )

    @app.route("/attachments/<int:attachment_id>/delete", methods=["POST"])
    @login_required
    def attachment_delete(attachment_id):
        attachment = db.session.get(Attachment, attachment_id) or abort(404)
        topic = attachment.topic
        if not can_edit_topic(topic):
            abort(403)
        topic_id = topic.id
        topic_title = topic.title
        meeting_no = topic.meeting.meeting_no if topic.meeting else None
        filename = attachment.original_filename
        file_path = attachment_path(app, attachment)
        preview_dir = Path(app.config["POWERPOINT_PREVIEW_FOLDER"]) / str(attachment.id)
        mark_attachment_material_deleted(attachment)
        db.session.delete(attachment)
        db.session.commit()
        try:
            if file_path.exists():
                file_path.unlink()
            if preview_dir.exists():
                for child in preview_dir.iterdir():
                    if child.is_file():
                        child.unlink()
                preview_dir.rmdir()
        except OSError as exc:
            current_app.logger.warning("Failed to remove attachment file: %s", exc)
        record_audit(
            "delete_attachment",
            target_type="attachment",
            target_id=attachment_id,
            target_label=filename,
            metadata={"topic_id": topic_id, "topic_title": topic_title},
        )
        flash("附件已删除", "success")
        if meeting_no:
            return redirect(url_for("meeting_detail", meeting_no=meeting_no, topic_id=topic_id))
        return redirect(url_for("topic_draft_edit", topic_id=topic_id))

    @app.route("/topics/<int:topic_id>/material-reviews", methods=["POST"])
    @login_required
    def topic_material_review_create(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        meeting = topic.meeting or topic.requested_meeting
        require_reviewer(meeting)
        result = request.form.get("result", "needs_revision")
        if result not in {"approved", "needs_revision"}:
            result = "needs_revision"
        prompt = ai_prompt_from_review_form(topic, request.form)
        bind_topic_review_prompt(topic, prompt)
        review = TopicMaterialReview(
            topic_id=topic.id,
            source="hoster",
            result=result,
            score=clamp_int(request.form.get("score"), 0, 100),
            summary=request.form.get("summary", "").strip(),
            issues=request.form.get("issues", "").strip(),
            suggestions=request.form.get("suggestions", "").strip(),
            reviewed_by=current_user.id,
            **prompt_snapshot_values(prompt, topic),
        )
        db.session.add(review)
        db.session.commit()
        record_audit(
            "review_material",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"source": review.source, "result": review.result, "score": review.score},
        )
        flash("材料 Review 已保存", "success")
        if topic.meeting:
            return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/topics/<int:topic_id>/material-reviews/ai", methods=["POST"])
    @login_required
    def topic_material_review_ai(topic_id):
        topic = db.session.get(Topic, topic_id) or abort(404)
        meeting = topic.meeting or topic.requested_meeting
        require_topic_material_reviewer(topic)
        model = current_app.config.get("COPILOT_DEFAULT_MODEL")
        if not model or (not current_app.config.get("ZHISHU_API_KEY") and not current_app.config.get("ZHISHU_CLIENT")):
            flash("AI Review 暂不可用：请先确认智枢 API Key 和模型配置，当前可使用 Hoster 手动 Review。", "info")
            if topic.meeting:
                return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))
        try:
            prompt = ai_prompt_from_review_form(topic, request.form)
            review_data = build_ai_material_review(topic, model, prompt=prompt)
        except Exception as exc:
            flash(f"AI Review 暂不可用：{exc}。当前可使用 Hoster 手动 Review。", "info")
            if topic.meeting:
                return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
            return redirect(url_for("topic_draft_edit", topic_id=topic.id))
        bind_topic_review_prompt(topic, prompt)
        review = TopicMaterialReview(
            topic_id=topic.id,
            source="ai",
            result=review_data["result"],
            score=review_data["score"],
            summary=review_data["summary"],
            issues=review_data["issues"],
            suggestions=review_data["suggestions"],
            reviewed_by=current_user.id,
            knowhow_snapshot=review_data.get("knowhow_snapshot") or [],
            material_chunk_snapshot=review_data.get("material_chunk_snapshot") or [],
            **prompt_snapshot_values(prompt, topic, review_data.get("prompt_content_snapshot")),
        )
        db.session.add(review)
        db.session.commit()
        record_audit(
            "review_material",
            target_type="topic",
            target_id=topic.id,
            target_label=topic.title,
            metadata={"source": review.source, "result": review.result, "score": review.score},
        )
        flash("AI Review 已生成", "success")
        if topic.meeting:
            return redirect(url_for("meeting_detail", meeting_no=topic.meeting.meeting_no, topic_id=topic.id))
        return redirect(url_for("topic_draft_edit", topic_id=topic.id))

    @app.route("/meetings/<meeting_no>/minutes", methods=["POST"])
    @login_required
    def minutes_update(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_reviewer(meeting)
        minutes = meeting.minutes or MeetingMinutes(meeting_id=meeting.id, created_by=current_user.id)
        minutes.summary = request.form.get("summary", "").strip()
        minutes.decisions = request.form.get("decisions", "").strip()
        minutes.action_items = request.form.get("action_items", "").strip()
        meeting.status = request.form.get("meeting_status", meeting.status)
        db.session.add(minutes)
        db.session.commit()
        flash("会议纪要已保存", "success")
        record_audit(
            "update_minutes",
            target_type="meeting",
            target_id=meeting.id,
            target_label=meeting.meeting_no,
            metadata={"meeting_status": meeting.status},
        )
        return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no, view="minutes"))

    @app.route("/agenda")
    @login_required
    def agenda_index():
        query = Meeting.query.filter(Meeting.status.in_(["draft", "preparing", "reporting"]))
        if not can_access_agenda():
            query = query.filter(Meeting.host_user_id == current_user.id)
        meetings = query.order_by(Meeting.meeting_date.asc()).all()
        rows = []
        for meeting in meetings:
            bound_count = Topic.query.filter_by(
                meeting_id=meeting.id, workflow_status="approved"
            ).count()
            pool_count = len(agenda_left_pool(meeting))
            rows.append(
                {
                    "meeting": meeting,
                    "bound_count": bound_count,
                    "pool_count": pool_count,
                }
            )
        return render_template("agenda/index.html", rows=rows)

    @app.route("/agenda/<meeting_no>")
    @login_required
    def agenda_board(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_agenda_manager(meeting)
        if meeting.status == "completed":
            flash("会议已结束，议题不可编辑", "warning")
            return redirect(url_for("meeting_detail", meeting_no=meeting.meeting_no))
        return render_template("agenda/board.html", meeting=meeting)

    @app.route("/agenda/<meeting_no>/data")
    @login_required
    def agenda_data(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_agenda_manager(meeting)
        left = [serialize_agenda_topic(t, meeting.id) for t in agenda_left_pool(meeting)]
        right = [serialize_agenda_topic(t, meeting.id) for t in agenda_right_column(meeting)]
        return jsonify(
            {
                "meeting": {
                    "meeting_no": meeting.meeting_no,
                    "title": meeting.title,
                    "status": meeting.status,
                    "editable": meeting.status in ("draft", "preparing", "reporting"),
                    "updated_at": meeting.updated_at.isoformat(),
                },
                "left_pool": left,
                "right_column": right,
            }
        )

    @app.route("/agenda/<meeting_no>", methods=["PATCH"])
    @login_required
    def agenda_update(meeting_no):
        meeting = Meeting.query.filter_by(meeting_no=meeting_no).first_or_404()
        require_agenda_manager(meeting)
        if meeting.status not in ("draft", "preparing", "reporting"):
            return jsonify({"ok": False, "error": "会议状态不允许编辑议题"}), 403
        payload = request.get_json(silent=True) or {}
        expected = payload.get("expected_updated_at")
        if expected and expected != meeting.updated_at.isoformat():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "议题已被他人更新，请刷新后重试",
                        "current_updated_at": meeting.updated_at.isoformat(),
                    }
                ),
                409,
            )
        approve_items = payload.get("approve") or []
        unbind_items = payload.get("unbind") or []
        reorder_ids = payload.get("reorder") or []
        result = _apply_agenda_changes(
            meeting, approve_items, unbind_items, reorder_ids, current_user.id
        )
        if not result["ok"]:
            db.session.rollback()
            return jsonify(result), 400
        db.session.commit()
        record_audit(
            "update_agenda",
            target_type="meeting",
            target_id=meeting.id,
            target_label=meeting.meeting_no,
            metadata={
                "approved": result["approved_ids"],
                "unbound": result["unbound_ids"],
                "reorder": result["final_order"],
            },
        )
        return jsonify(
            {
                "ok": True,
                "updated_at": meeting.updated_at.isoformat(),
                "approved_ids": result["approved_ids"],
                "unbound_ids": result["unbound_ids"],
                "final_order": result["final_order"],
                "skipped": result["skipped"],
            }
        )

    return app


def value_at(values, index):
    return values[index].strip() if index < len(values) else ""


def form_int(name):
    value = request.form.get(name, "").strip()
    return int(value) if value.isdigit() else None


def form_or_query_int(name):
    value = request.values.get(name, "").strip()
    return int(value) if value.isdigit() else None


def parse_date_arg(name):
    value = request.args.get(name, "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_datetime_start_arg(name):
    return local_date_start(request.args.get(name))


def parse_datetime_exclusive_end_arg(name):
    return local_date_end(request.args.get(name))


def meeting_list_filters():
    topic_category = filter_topic_category(request.args.get("topic_category"))
    plan_version_id = form_or_query_int("plan_version_id")
    plan_round_id = form_or_query_int("plan_round_id")
    readiness_status = request.args.get("readiness_status", "").strip()
    if readiness_status not in {"ready", "mostly_ready", "preparing", "risk", "not_started"}:
        readiness_status = ""
    return {
        "search": request.args.get("search", "").strip(),
        "status": request.args.get("status", "").strip(),
        "meeting_date_start": parse_date_arg("meeting_date_start"),
        "meeting_date_end": parse_date_arg("meeting_date_end"),
        "created_start": parse_datetime_start_arg("created_start"),
        "created_end": parse_datetime_exclusive_end_arg("created_end"),
        "location": request.args.get("location", "").strip(),
        "hoster_name": request.args.get("hoster_name", "").strip(),
        "topic_category": topic_category,
        "plan_version_id": plan_version_id,
        "plan_round_id": plan_round_id,
        "readiness_status": readiness_status,
        "favorite": request.args.get("favorite") == "1",
        "raw": request.args,
    }


def topic_draft_filters():
    topic_category = filter_topic_category(request.args.get("topic_category"))
    plan_version_id = form_or_query_int("plan_version_id")
    plan_round_id = form_or_query_int("plan_round_id")
    status = request.args.get("status", "").strip()
    if status not in WORKFLOW_STATUS_LABELS:
        status = ""
    return {
        "search": request.args.get("search", "").strip(),
        "status": status,
        "topic_category": topic_category,
        "plan_version_id": plan_version_id,
        "plan_round_id": plan_round_id,
        "creator_name": request.args.get("creator_name", "").strip(),
        "target_meeting_no": request.args.get("target_meeting_no", "").strip(),
        "created_start": parse_datetime_start_arg("created_start"),
        "created_end": parse_datetime_exclusive_end_arg("created_end"),
        "updated_start": parse_datetime_start_arg("updated_start"),
        "updated_end": parse_datetime_exclusive_end_arg("updated_end"),
        "raw": request.args,
    }


def clamp_int(value, minimum=0, maximum=100):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def safe_role(value):
    return value if value in {"admin", "group_leader", "user"} else "user"


def is_admin(user=None):
    user = user or current_user
    return bool(getattr(user, "is_authenticated", False) and user.role == "admin")


def user_group_code(user=None):
    user = user or current_user
    group = getattr(user, "group", None)
    return (getattr(group, "code", "") or "").lower()


def is_qbp_group_user(user=None):
    user = user or current_user
    return bool(getattr(user, "is_authenticated", False) and user_group_code(user) == "qbp")


def is_builtin_procurement_group_user(user=None):
    user = user or current_user
    return bool(
        getattr(user, "is_authenticated", False)
        and user_group_code(user) in BUSINESS_GROUP_CODES
    )


def can_manage_meetings(user=None):
    user = user or current_user
    return is_admin(user) or is_qbp_group_user(user)


def can_access_agenda(user=None):
    user = user or current_user
    return is_admin(user) or is_qbp_group_user(user)


def can_manage_config(user=None):
    user = user or current_user
    return is_admin(user) or is_qbp_group_user(user)


def can_manage_ai_prompt(user=None):
    user = user or current_user
    return is_admin(user) or is_qbp_group_user(user) or is_builtin_procurement_group_user(user)


def ai_business_scope_names(include_stored=False):
    return tuple(DEFAULT_BUSINESS_GROUP_NAMES)


def manageable_ai_business_scope(user=None):
    user = user or current_user
    group = getattr(user, "group", None)
    if not group or group.is_admin_group:
        return None
    return group.name if user_group_code(user) in BUSINESS_GROUP_CODES else None


def can_manage_ai_prompt_scope(scope, user=None):
    user = user or current_user
    scope = normalize_ai_prompt_scope(scope)
    if is_admin(user) or is_qbp_group_user(user):
        return True
    own_scope = manageable_ai_business_scope(user)
    return bool(own_scope and scope == own_scope)


def ai_prompt_business_scope_options(user=None):
    user = user or current_user
    own_scope = manageable_ai_business_scope(user)
    if own_scope and not (is_admin(user) or is_qbp_group_user(user)):
        return (own_scope,)
    if is_builtin_procurement_group_user(user) and not (is_admin(user) or is_qbp_group_user(user)):
        return ()
    return ai_business_scope_names()


def plan_version_names(include_stored=False):
    names = [version.name for version in plan_version_options()]
    if include_stored:
        stored_scopes = set()
        for model in (AIPrompt, AIKnowHow, AIKnowHowCategory):
            try:
                rows = db.session.query(model.scope).distinct().all()
            except Exception:
                rows = []
            for (scope,) in rows:
                scope = (scope or "").strip()
                if scope and scope.upper() not in {GLOBAL_KNOWHOW_SCOPE, "CUSTOM"}:
                    stored_scopes.add(scope)
        names.extend(sorted(stored_scopes - set(names)))
    return tuple(names)


def scoped_ai_prompt_query(user=None):
    user = user or current_user
    query = AIPrompt.query
    own_scope = manageable_ai_business_scope(user)
    if own_scope and not (is_admin(user) or is_qbp_group_user(user)):
        query = query.filter(AIPrompt.scope == own_scope)
    elif is_builtin_procurement_group_user(user) and not (is_admin(user) or is_qbp_group_user(user)):
        query = query.filter(AIPrompt.scope == "__none__")
    return query


def can_manage_agenda(meeting=None, user=None):
    user = user or current_user
    return can_access_agenda(user) or (meeting is not None and can_review_meeting(meeting, user))


def should_limit_meetings_to_own_approved_topics(user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return False
    return not can_manage_meetings(user)


def is_group_leader(user=None):
    user = user or current_user
    return bool(
        getattr(user, "is_authenticated", False)
        and user.role == "group_leader"
        and user.group_id is not None
    )


def can_manage_user(target, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not target:
        return False
    if user.role == "admin":
        return True
    if is_group_leader(user):
        if target.role == "admin":
            return False
        if target.id == user.id:
            return False
        return target.group_id == user.group_id
    return False


def admin_group_id_or_error(group_id):
    if not group_id:
        return None, None
    admin_group = Group.admin_group()
    if admin_group and group_id == admin_group.id:
        return group_id, None
    return None, "管理员只能归属 PLN/BP 组或不归属用户组"


def business_group_id_or_error(group_id):
    group = db.session.get(Group, group_id) if group_id else None
    if group and not group.is_admin_group:
        return group_id, None
    return None, "普通用户和组长只能归属用户组管理中的业务用户组"


def filter_topic_category(value):
    value = (value or "").strip()
    if not value:
        return ""
    if value in TOPIC_CATEGORY_OPTIONS:
        return value
    return LEGACY_TOPIC_CATEGORY_MAP.get(value, "")


def topic_category_query_values(category):
    values = {category}
    values.update(old for old, new in LEGACY_TOPIC_CATEGORY_MAP.items() if new == category)
    return sorted(values)


def plan_version_options():
    versions = PlanVersion.active_options()
    if not versions:
        return [PlanVersion.default()]
    return versions


def plan_round_options(plan_version_id=None):
    version = db.session.get(PlanVersion, plan_version_id) if plan_version_id else PlanVersion.default()
    if not version:
        return []
    return PlanRound.active_for_version(version.id)


def plan_version_payload(version):
    return {
        "id": version.id,
        "name": version.name,
        "rounds": [plan_round_payload(round_item) for round_item in version.rounds.order_by(PlanRound.sort_order.asc(), PlanRound.id.asc())],
    }


def plan_round_payload(round_item):
    return {
        "id": round_item.id,
        "name": round_item.name,
        "plan_version_id": round_item.plan_version_id,
    }


def default_plan_scope():
    version = PlanVersion.default()
    return version, version.default_round()


def parse_plan_version_id(value):
    try:
        plan_version_id = int(value or 0)
    except (TypeError, ValueError):
        plan_version_id = 0
    version = db.session.get(PlanVersion, plan_version_id) if plan_version_id else None
    if version and version.is_active:
        return version
    return PlanVersion.default()


def parse_plan_round_id(value, version):
    try:
        plan_round_id = int(value or 0)
    except (TypeError, ValueError):
        plan_round_id = 0
    round_item = db.session.get(PlanRound, plan_round_id) if plan_round_id else None
    if round_item and round_item.is_active and round_item.plan_version_id == version.id:
        return round_item
    return version.default_round()


def current_or_posted_plan_scope(form, meeting=None):
    if meeting and not form.get("plan_version_id"):
        meeting.ensure_scope_defaults()
        return meeting.plan_version_ref, meeting.plan_round_ref
    version = parse_plan_version_id(form.get("plan_version_id"))
    round_item = parse_plan_round_id(form.get("plan_round_id"), version)
    return version, round_item


def meeting_scope_matches_topic(meeting, topic):
    if not meeting or not topic:
        return False
    meeting.ensure_scope_defaults()
    topic.ensure_scope_defaults()
    return (
        topic.plan_version_id == meeting.plan_version_id
        and topic.plan_round_id == meeting.plan_round_id
        and normalize_topic_category(topic.category) == normalize_topic_category(meeting.category)
    )


def scope_mismatch_message():
    return "议题与会议的 Plan Version、Round 或类别不一致"


def apply_meeting_scope_to_topic(topic, meeting):
    topic.apply_meeting_scope(meeting)


def meeting_scope_label(meeting):
    meeting.ensure_scope_defaults()
    return f"{meeting.plan_version_name} / {meeting.plan_round_name} / {normalize_topic_category(meeting.category)}"


def meeting_scope_filters_match(meeting, plan_version_id=None, plan_round_id=None, category=None):
    if plan_version_id and meeting.plan_version_id != plan_version_id:
        return False
    if plan_round_id and meeting.plan_round_id != plan_round_id:
        return False
    if category and normalize_topic_category(meeting.category) != normalize_topic_category(category):
        return False
    return True


def render_config_table(selected_scope=None, completeness_config=None):
    selected_scope = normalize_plan_version(selected_scope or DEFAULT_PLAN_VERSION)
    return render_template(
        "admin/config_table.html",
        readiness_config=meeting_readiness_config(),
        completeness_config=completeness_config or topic_completeness_config(),
        selected_scope=selected_scope,
    )


def is_custom_group(group):
    return bool(group and group.code.startswith("custom-") and not group.is_admin_group)


def next_custom_group_code():
    prefix = "custom-"
    codes = [
        group.code
        for group in Group.query.filter(Group.code.like(f"{prefix}%")).all()
    ]
    numbers = []
    for code in codes:
        suffix = code[len(prefix):]
        if suffix.isdigit():
            numbers.append(int(suffix))
    return f"{prefix}{(max(numbers) if numbers else 0) + 1:04d}"


def user_has_business_records(user):
    return any(
        (
            Meeting.query.filter(or_(Meeting.created_by == user.id, Meeting.host_user_id == user.id)).first(),
            Topic.query.filter(or_(Topic.created_by == user.id, Topic.reviewed_by == user.id)).first(),
            Attachment.query.filter_by(uploaded_by=user.id).first(),
            MeetingMinutes.query.filter_by(created_by=user.id).first(),
            TopicMaterialReview.query.filter_by(reviewed_by=user.id).first(),
            TopicShare.query.filter(or_(TopicShare.user_id == user.id, TopicShare.granted_by == user.id)).first(),
            MeetingFavorite.query.filter_by(user_id=user.id).first(),
        )
    )


def can_review_meeting(meeting, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not meeting:
        return False
    return can_manage_meetings(user) or meeting.host_user_id == user.id


def require_admin():
    if not is_admin():
        abort(403)


def require_meeting_manager():
    if not can_manage_meetings():
        abort(403)


def require_config_manager():
    if not can_manage_config():
        abort(403)


def require_ai_prompt_manager():
    if not can_manage_ai_prompt():
        abort(403)


def require_agenda_manager(meeting):
    if not can_manage_agenda(meeting):
        abort(403)


def require_user_manager():
    if not (is_admin() or is_group_leader()):
        abort(403)


def require_reviewer(meeting):
    if not can_review_meeting(meeting):
        abort(403)


def can_review_topic_material(topic, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not topic:
        return False
    return can_manage_meetings(user) or can_review_meeting(topic.meeting, user) or can_review_meeting(topic.requested_meeting, user)


def can_decide_meeting_topic(topic, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not topic or not topic.meeting:
        return False
    return can_manage_meetings(user) or can_review_meeting(topic.meeting, user)


def require_topic_material_reviewer(topic):
    if not can_review_topic_material(topic):
        abort(403)


def visible_topics_for_meeting(meeting):
    topic_load_options = (
        joinedload(Topic.plan_version_ref),
        joinedload(Topic.plan_round_ref),
        joinedload(Topic.creator),
        joinedload(Topic.requested_meeting),
    )
    if can_review_meeting(meeting) or can_manage_meetings():
        return meeting.topics.options(*topic_load_options).order_by(Topic.present_order.asc()).all()
    return (
        Topic.query.filter(
            db.or_(
                db.and_(
                    Topic.meeting_id == meeting.id,
                    Topic.created_by == current_user.id,
                    Topic.workflow_status == "approved",
                ),
                db.and_(
                    Topic.requested_meeting_id == meeting.id,
                    Topic.created_by == current_user.id,
                    Topic.workflow_status == "submitted",
                ),
            )
        )
        .options(*topic_load_options)
        .order_by(Topic.present_order.asc(), Topic.updated_at.desc())
        .all()
    )


def pending_topics_for_meeting(meeting):
    return (
        Topic.query.filter_by(requested_meeting_id=meeting.id, workflow_status="submitted")
        .options(
            joinedload(Topic.plan_version_ref),
            joinedload(Topic.plan_round_ref),
            joinedload(Topic.creator),
            joinedload(Topic.requested_meeting),
        )
        .order_by(Topic.submitted_at.asc(), Topic.updated_at.asc())
        .all()
    )


def release_meeting_topics(meeting, reason_suffix, reviewer_id):
    """Detach topics from a meeting that is being deleted or marked completed.

    Invariant: approved ⟺ meeting_id IS NOT NULL. When a meeting ends, all of its
    approved topics revert to `submitted` so the next host can pick them up.

      - approved topics bound to the meeting → submitted (meeting_id=NULL, requested=NULL)
      - submitted topics requesting the meeting → rejected (with review_comment)
      - other states still referencing the meeting → FK cleared

    Returns (rejected_count, reverted_count).
    """
    rejected = 0
    reverted = 0
    now = datetime.utcnow()
    label = meeting.meeting_no
    for topic in Topic.query.filter_by(meeting_id=meeting.id).all():
        topic.meeting_id = None
        topic.present_order = 0
        if topic.workflow_status == "approved":
            topic.workflow_status = "submitted"
            topic.requested_meeting_id = None
            topic.submitted_at = now
            topic.reviewed_by = None
            topic.reviewed_at = None
            topic.review_comment = (
                f"{topic.review_comment or ''}\n[{label} {reason_suffix}] 已退回待审批"
            ).strip()
            reverted += 1
    submitted_topics = Topic.query.filter_by(
        requested_meeting_id=meeting.id, workflow_status="submitted"
    ).all()
    for topic in submitted_topics:
        topic.workflow_status = "rejected"
        topic.requested_meeting_id = None
        topic.reviewed_by = reviewer_id
        topic.reviewed_at = now
        topic.review_comment = (
            f"系统自动驳回：目标会议 {label} {reason_suffix}，请在议题池重新选择会议"
        )
        rejected += 1
    for topic in Topic.query.filter(
        Topic.requested_meeting_id == meeting.id,
        Topic.workflow_status != "submitted",
    ).all():
        topic.requested_meeting_id = None
    return rejected, reverted


def agenda_left_pool(meeting):
    """Topics eligible to be picked into the agenda of `meeting`.

    Includes:
      - submitted topics directly requesting this meeting (targeted)
      - submitted topics with no target meeting (open submission)
    """
    return (
        Topic.query.filter(
            Topic.meeting_id.is_(None),
            Topic.workflow_status == "submitted",
            Topic.plan_version_id == meeting.plan_version_id,
            Topic.plan_round_id == meeting.plan_round_id,
            Topic.category.in_(topic_category_query_values(meeting.category)),
            db.or_(
                Topic.requested_meeting_id == meeting.id,
                Topic.requested_meeting_id.is_(None),
            ),
        )
        .order_by(Topic.submitted_at.asc(), Topic.updated_at.asc())
        .all()
    )


def agenda_right_column(meeting):
    return (
        Topic.query.filter_by(meeting_id=meeting.id, workflow_status="approved")
        .order_by(Topic.present_order.asc(), Topic.updated_at.asc())
        .all()
    )


def agenda_topic_kind(topic, meeting_id):
    if topic.workflow_status == "submitted":
        if topic.requested_meeting_id == meeting_id:
            return "target"
        if topic.requested_meeting_id is None:
            return "open"
    return "other"


def serialize_agenda_topic(topic, meeting_id):
    readiness = topic_completeness(topic)
    requester = topic.creator
    return {
        "id": topic.id,
        "title": topic.title,
        "category": topic.category or "",
        "plan_version": topic.plan_version or "",
        "owner": topic.owner or "",
        "background_len": len(topic.background or ""),
        "purpose_len": len(topic.purpose or ""),
        "duration_minutes": topic.duration_minutes,
        "attachment_count": topic.attachments.count(),
        "kind": agenda_topic_kind(topic, meeting_id),
        "workflow_status": topic.workflow_status,
        "present_order": topic.present_order,
        "requester": requester.display_name if requester else "",
        "submitted_at": topic.submitted_at.isoformat() if topic.submitted_at else None,
        "readiness": {
            "score": readiness["score"],
            "missing": readiness["missing"],
            "review_passed": readiness["review_passed"],
        },
    }


def _apply_agenda_changes(meeting, approve_items, unbind_items, reorder_ids, reviewer_id):
    """Apply approve/unbind/reorder mutations atomically (no commit here).

    Returns dict with ok, approved_ids, unbound_ids, final_order, skipped, error?.
    Caller is responsible for db.session.commit() / rollback().
    """
    approved_ids = []
    unbound_ids = []
    skipped = []
    now = datetime.utcnow()
    label = meeting.meeting_no

    for item in approve_items:
        topic_id = item.get("id")
        topic = db.session.get(Topic, topic_id) if topic_id else None
        if not topic or topic.meeting_id is not None:
            skipped.append({"id": topic_id, "reason": "topic_unavailable"})
            continue
        if topic.workflow_status != "submitted":
            skipped.append({"id": topic_id, "reason": "invalid_state"})
            continue
        if topic.requested_meeting_id not in (None, meeting.id):
            skipped.append({"id": topic_id, "reason": "requested_other_meeting"})
            continue
        if not meeting_scope_matches_topic(meeting, topic):
            skipped.append({"id": topic_id, "reason": "scope_mismatch"})
            continue
        review_comment = (item.get("review_comment") or "").strip()
        if not review_comment:
            return {
                "ok": False,
                "error": f"议题 #{topic_id} 缺少审批意见",
                "approved_ids": approved_ids,
                "unbound_ids": unbound_ids,
                "final_order": [],
                "skipped": skipped,
            }
        topic.workflow_status = "approved"
        topic.requested_meeting_id = None
        topic.reviewed_by = reviewer_id
        topic.reviewed_at = now
        topic.review_comment = review_comment
        topic.meeting_id = meeting.id
        approved_ids.append(topic.id)

    for item in unbind_items:
        topic_id = item.get("id")
        topic = db.session.get(Topic, topic_id) if topic_id else None
        if (
            not topic
            or topic.meeting_id != meeting.id
            or topic.workflow_status != "approved"
        ):
            skipped.append({"id": topic_id, "reason": "topic_unavailable"})
            continue
        reason = (item.get("review_comment") or "").strip()
        if not reason:
            return {
                "ok": False,
                "error": f"议题 #{topic_id} 缺少移出理由",
                "approved_ids": approved_ids,
                "unbound_ids": unbound_ids,
                "final_order": [],
                "skipped": skipped,
            }
        topic.workflow_status = "submitted"
        topic.submitted_at = now
        topic.meeting_id = None
        topic.requested_meeting_id = None
        topic.present_order = 0
        topic.reviewed_by = None
        topic.reviewed_at = None
        topic.review_comment = f"{topic.review_comment or ''}\n[{label} 移出议题] {reason}".strip()
        unbound_ids.append(topic.id)

    # Final dense renumbering of agenda.
    db.session.flush()
    current_bound = (
        Topic.query.filter_by(meeting_id=meeting.id, workflow_status="approved")
        .order_by(Topic.present_order.asc(), Topic.updated_at.asc())
        .all()
    )
    current_ids = [t.id for t in current_bound]
    if reorder_ids:
        if set(reorder_ids) != set(current_ids):
            return {
                "ok": False,
                "error": "排序列表与当前议题不一致，请刷新",
                "approved_ids": approved_ids,
                "unbound_ids": unbound_ids,
                "final_order": current_ids,
                "skipped": skipped,
            }
        ordered_ids = list(reorder_ids)
    else:
        ordered_ids = current_ids
    id_to_topic = {t.id: t for t in current_bound}
    for index, topic_id in enumerate(ordered_ids, start=1):
        id_to_topic[topic_id].present_order = index

    meeting.updated_at = now
    return {
        "ok": True,
        "approved_ids": approved_ids,
        "unbound_ids": unbound_ids,
        "final_order": ordered_ids,
        "skipped": skipped,
    }


def visible_topics_for_readiness(meeting):
    if can_review_meeting(meeting):
        return meeting.topics.filter_by(workflow_status="approved").order_by(Topic.present_order.asc()).all()
    return (
        Topic.query.filter_by(
            meeting_id=meeting.id,
            created_by=current_user.id,
            workflow_status="approved",
        )
        .order_by(Topic.present_order.asc())
        .all()
    )


def meeting_readiness_config():
    stored = AppConfig.query.filter_by(key=MEETING_READINESS_CONFIG_KEY).first()
    if not stored:
        return default_meeting_readiness_config()
    return normalize_meeting_readiness_config(stored.value_json)


def topic_completeness_config():
    stored = AppConfig.query.filter_by(key=TOPIC_COMPLETENESS_CONFIG_KEY).first()
    value = stored.value_json if stored else default_topic_completeness_config()
    config_value = normalize_topic_completeness_config(value)
    for version in plan_version_options():
        config_value["rules"].setdefault(version.name, deepcopy(DEFAULT_TOPIC_COMPLETENESS_RULE))
    return config_value


def lark_config():
    stored = AppConfig.query.filter_by(key=LARK_CONFIG_KEY).first()
    if not stored:
        return normalize_lark_config({})
    return normalize_lark_config(stored.value_json)


def lark_config_from_form(form, current_config=None):
    existing = normalize_lark_config(current_config or {})
    app_secret = (form.get("app_secret") or "").strip() or existing.get("app_secret", "")
    return normalize_lark_config(
        {
            "enabled": bool(form.get("enabled")),
            "app_id": form.get("app_id"),
            "app_secret": app_secret,
            "api_base": form.get("api_base") or existing.get("api_base"),
            "reminder_days": form.get("reminder_days") or existing.get("reminder_days"),
        }
    )


def save_lark_config(config_value):
    stored = AppConfig.query.filter_by(key=LARK_CONFIG_KEY).first()
    if not stored:
        stored = AppConfig(key=LARK_CONFIG_KEY, value_json={})
        db.session.add(stored)
    stored.value_json = normalize_lark_config(config_value)
    stored.updated_by = current_user.id
    db.session.commit()


def ready_lark_config():
    config_value = lark_config()
    if not config_value["enabled"]:
        raise ValueError("请先启用飞书集成")
    if not config_value["app_id"] or not config_value["app_secret"]:
        raise ValueError("请先填写 App ID 和 App Secret")
    return config_value


def sync_lark_users(client):
    users = (
        User.query.filter(User.enabled.is_(True), User.email.isnot(None))
        .order_by(User.id.asc())
        .all()
    )
    emails = sorted(
        {
            (user.email or "").strip().lower()
            for user in users
            if (user.email or "").strip()
        }
    )
    mapping = client.batch_get_user_ids(emails=emails)
    synced_count = 0
    now = datetime.utcnow()
    for user in users:
        email_key = (user.email or "").strip().lower()
        info = mapping.get(email_key)
        if not info:
            continue
        user.lark_open_id = info.get("open_id") or user.lark_open_id
        user.lark_user_id = info.get("user_id") or user.lark_user_id
        user.lark_synced_at = now
        synced_count += 1
    db.session.commit()
    return synced_count


def send_lark_missing_material_reminders(client, today=None):
    config_value = lark_config()
    target_date = (today or datetime.utcnow().date()) + timedelta(days=config_value["reminder_days"])
    meetings = (
        Meeting.query.filter(Meeting.meeting_date == target_date, Meeting.status != "completed")
        .order_by(Meeting.meeting_date.asc(), Meeting.meeting_no.asc())
        .all()
    )
    sent_count = 0
    for meeting in meetings:
        topics = (
            meeting.topics.filter_by(workflow_status="approved")
            .order_by(Topic.present_order.asc(), Topic.id.asc())
            .all()
        )
        for topic in topics:
            if topic.attachments.count():
                continue
            recipients = []
            if meeting.host_user and meeting.host_user.lark_open_id:
                recipients.append(meeting.host_user)
            if topic.creator and topic.creator.lark_open_id:
                recipients.append(topic.creator)
            seen_open_ids = set()
            for recipient in recipients:
                if recipient.lark_open_id in seen_open_ids:
                    continue
                seen_open_ids.add(recipient.lark_open_id)
                client.send_text(
                    recipient.lark_open_id,
                    build_missing_material_message(meeting, topic, config_value["reminder_days"]),
                )
                sent_count += 1
    return sent_count


def build_missing_material_message(meeting, topic, reminder_days):
    return (
        f"【QBP Meeting 提醒】{meeting.title} 将在 {reminder_days} 天后召开，"
        f"会议编号 {meeting.meeting_no}，议题「{topic.title}」附件材料尚未上传。"
        "请尽快补充材料。"
    )


def readiness_config_from_form(form):
    try:
        weights = {
            "meeting_info": clamp_int(form.get("meeting_info_weight"), 0, 100),
            "topic_completeness": clamp_int(form.get("topic_completeness_weight"), 0, 100),
        }
        thresholds = {
            "ready": clamp_int(form.get("ready_threshold"), 0, 100),
            "mostly_ready": clamp_int(form.get("mostly_ready_threshold"), 0, 100),
            "preparing": clamp_int(form.get("preparing_threshold"), 0, 100),
        }
    except (TypeError, ValueError):
        return None, "配置值必须是 0-100 的整数"
    if sum(weights.values()) != 100:
        return None, "权重合计必须等于 100"
    if not (thresholds["ready"] > thresholds["mostly_ready"] > thresholds["preparing"]):
        return None, "阈值必须满足：已就绪 > 基本就绪 > 准备中"
    return {"weights": weights, "thresholds": thresholds}, None


def topic_scoring_rule_from_form(form):
    try:
        weights = {
            "basic_info": clamp_int(form.get("basic_info_weight"), 0, 100),
            "background_purpose": clamp_int(form.get("background_purpose_weight"), 0, 100),
            "attachment": clamp_int(form.get("attachment_weight"), 0, 100),
            "review": clamp_int(form.get("review_weight"), 0, 100),
        }
        thresholds = {
            "ready": clamp_int(form.get("ready_threshold"), 0, 100),
            "mostly_ready": clamp_int(form.get("mostly_ready_threshold"), 0, 100),
            "preparing": clamp_int(form.get("preparing_threshold"), 0, 100),
        }
    except (TypeError, ValueError):
        return None, "配置值必须是 0-100 的整数"
    if sum(weights.values()) != 100:
        return None, "权重合计必须等于 100"
    if not (thresholds["ready"] > thresholds["mostly_ready"] > thresholds["preparing"]):
        return None, "阈值必须满足：已就绪 > 基本就绪 > 准备中"
    return {"weights": weights, "thresholds": thresholds}, None


def topic_completeness_config_from_form(form, current_config=None):
    selected_scope = normalize_plan_version(form.get("scope") or DEFAULT_PLAN_VERSION)
    rule, error = topic_scoring_rule_from_form(form)
    if error:
        return None, f"{selected_scope}：{error}"
    config_value = normalize_topic_completeness_config(current_config or default_topic_completeness_config())
    config_value["rules"][selected_scope] = rule
    return config_value, None


def save_meeting_readiness_config(config_value):
    stored = AppConfig.query.filter_by(key=MEETING_READINESS_CONFIG_KEY).first()
    if not stored:
        stored = AppConfig(key=MEETING_READINESS_CONFIG_KEY, value_json=config_value)
        db.session.add(stored)
    stored.value_json = normalize_meeting_readiness_config(config_value)
    stored.updated_by = current_user.id
    db.session.commit()


def save_topic_completeness_config(config_value):
    stored = AppConfig.query.filter_by(key=TOPIC_COMPLETENESS_CONFIG_KEY).first()
    if not stored:
        stored = AppConfig(key=TOPIC_COMPLETENESS_CONFIG_KEY, value_json=config_value)
        db.session.add(stored)
    stored.value_json = normalize_topic_completeness_config(config_value)
    stored.updated_by = current_user.id
    db.session.commit()


def ai_review_prompt():
    prompt = default_ai_prompt()
    if prompt:
        return render_ai_prompt_message(
            prompt,
            topic_title="{topic_title}",
            scope="{scope}",
            knowhow_text="{knowhow}",
        )
    stored = AppConfig.query.filter_by(key=AI_REVIEW_PROMPT_KEY).first()
    if stored and isinstance(stored.value_json, dict):
        content = (stored.value_json.get("content") or "").strip()
        if content:
            return content
    return DEFAULT_AI_REVIEW_PROMPT


def save_ai_review_prompt(content):
    prompt = default_ai_prompt() or AIPrompt(
        name="通用默认模板",
        scope="GLOBAL",
        is_active=True,
        is_default=True,
    )
    review_goal, focus_points = split_legacy_prompt_content(content)
    prompt.review_goal = review_goal
    prompt.focus_points = focus_points
    prompt.output_options = dict(AI_PROMPT_OUTPUT_OPTIONS)
    prompt.updated_by = current_user.id
    if not prompt.id:
        prompt.created_by = current_user.id
        db.session.add(prompt)
    db.session.commit()


def normalize_ai_prompt_scope(value):
    scope = (value or GLOBAL_KNOWHOW_SCOPE).strip()
    upper_scope = scope.upper()
    if upper_scope in {GLOBAL_KNOWHOW_SCOPE, "CUSTOM"}:
        return upper_scope
    return scope if scope in ai_business_scope_names(include_stored=True) else GLOBAL_KNOWHOW_SCOPE


def normalize_knowhow_scope_input(value):
    scope = (value or "").strip()
    if scope.upper() == GLOBAL_KNOWHOW_SCOPE:
        return GLOBAL_KNOWHOW_SCOPE
    return scope


def require_knowhow_scope(scope, permission_check):
    if scope not in knowhow_scope_options():
        if not permission_check(scope):
            abort(403)
        abort(400)
    if not permission_check(scope):
        abort(403)


def ai_prompt_output_options_from_form(form):
    return {
        "include_score": bool(form.get("include_score")),
        "include_issues": bool(form.get("include_issues")),
        "include_suggestions": bool(form.get("include_suggestions")),
        "include_risk_points": bool(form.get("include_risk_points")),
    }


def ai_prompt_knowledge_sources_from_form(form, scope):
    values = [value.strip() for value in form.getlist("knowledge_sources")]
    sources = []
    allowed_sources = set(visible_knowhow_scopes())
    for value in values:
        if value.upper() == GLOBAL_KNOWHOW_SCOPE:
            value = GLOBAL_KNOWHOW_SCOPE
        if value not in allowed_sources:
            continue
        category_id = ai_prompt_category_id_from_form(form, value)
        item = {"scope": value, "category_id": category_id}
        if item not in sources:
            sources.append(item)
    if sources:
        return sources
    if scope in ai_business_scope_names(include_stored=True):
        return [
            {"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None},
            {"scope": scope, "category_id": None},
        ]
    return [{"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None}]


def ai_prompt_category_id_from_form(form, scope):
    raw_value = (form.get(f"knowledge_category_{scope}") or "").strip()
    if raw_value in {"", "ALL"}:
        return None
    try:
        category_id = int(raw_value)
    except ValueError:
        return None
    if category_id == 0:
        return 0
    category = db.session.get(AIKnowHowCategory, category_id)
    return category_id if category and category.scope == scope else None


def ai_prompt_knowledge_source_options():
    return [
        {
            "scope": scope,
            "label": f"{knowhow_scope_label(scope)}知识",
            "categories": knowhow_categories_for_scope(scope),
        }
        for scope in visible_knowhow_scopes()
    ]


def selected_ai_prompt_knowledge_source_items(prompt):
    if prompt:
        return with_global_knowhow_source_items(prompt.normalized_knowledge_source_items)
    business_scopes = ai_business_scope_names()
    items = [{"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None}]
    if business_scopes:
        items.append({"scope": business_scopes[0], "category_id": None})
    return items


def selected_ai_prompt_knowledge_source_scopes(prompt):
    return {
        item["scope"]
        for item in selected_ai_prompt_knowledge_source_items(prompt)
    }


def selected_ai_prompt_knowledge_source_categories(prompt):
    categories = {}
    for item in selected_ai_prompt_knowledge_source_items(prompt):
        categories[item["scope"]] = item.get("category_id")
    return categories


def default_ai_prompt():
    return (
        AIPrompt.query.filter_by(scope="GLOBAL", is_active=True, is_default=True)
        .order_by(AIPrompt.updated_at.desc(), AIPrompt.id.desc())
        .first()
    )


def available_ai_prompts_for_topic(topic):
    allowed_scopes = {"GLOBAL", "CUSTOM", *ai_business_scope_names(include_stored=True)}
    prompts = (
        AIPrompt.query.filter(AIPrompt.is_active.is_(True), AIPrompt.scope.in_(allowed_scopes))
        .order_by(AIPrompt.scope.asc(), AIPrompt.is_default.desc(), AIPrompt.updated_at.desc(), AIPrompt.id.desc())
        .all()
    )
    scope_rank = {"GLOBAL": 0, "CUSTOM": 1}
    scope_rank.update({scope: index + 2 for index, scope in enumerate(ai_business_scope_names(include_stored=True))})
    return sorted(
        prompts,
        key=lambda prompt: (
            scope_rank.get(prompt.scope, 9),
            0 if prompt.is_default else 1,
            (prompt.name or "").lower(),
            prompt.id or 0,
        ),
    )


def ai_prompt_is_available_for_topic(prompt, topic):
    if not prompt or not prompt.is_active:
        return False
    return prompt.scope in {"GLOBAL", "CUSTOM", *ai_business_scope_names(include_stored=True)}


def ai_prompt_from_review_form(topic, form):
    prompt_id = form.get("prompt_id", type=int)
    return selected_ai_prompt_for_topic(topic, prompt_id=prompt_id)


def selected_ai_prompt_for_topic(topic, prompt_id=None):
    if prompt_id:
        prompt = db.session.get(AIPrompt, prompt_id)
        if ai_prompt_is_available_for_topic(prompt, topic):
            return prompt
    if topic.review_prompt_id:
        prompt = db.session.get(AIPrompt, topic.review_prompt_id)
        if ai_prompt_is_available_for_topic(prompt, topic):
            return prompt
    custom = (
        AIPrompt.query.filter_by(scope="CUSTOM", is_active=True, is_default=True)
        .order_by(AIPrompt.updated_at.desc(), AIPrompt.id.desc())
        .first()
    )
    if custom:
        return custom
    return default_ai_prompt()


def selected_ai_prompt_from_available(topic, prompts):
    prompts = list(prompts or [])
    by_id = {prompt.id: prompt for prompt in prompts}
    if topic.review_prompt_id and topic.review_prompt_id in by_id:
        return by_id[topic.review_prompt_id]
    for prompt in prompts:
        if prompt.scope == "CUSTOM" and prompt.is_default:
            return prompt
    for prompt in prompts:
        if prompt.scope == "GLOBAL" and prompt.is_default:
            return prompt
    return prompts[0] if prompts else None


def bind_topic_review_prompt(topic, prompt):
    topic.review_prompt_id = prompt.id if prompt else None


def prompt_snapshot_values(prompt, topic, prompt_content=None):
    if prompt is None:
        return {
            "prompt_id": None,
            "prompt_name_snapshot": "未记录",
            "prompt_scope_snapshot": "",
            "prompt_content_snapshot": prompt_content or "",
        }
    content = prompt_content
    if content is None:
        content = render_ai_prompt_message(
            prompt,
            topic.title or "",
            topic.plan_version or "",
            "（人工 Review 未展开 know-how）",
        )
    return {
        "prompt_id": prompt.id,
        "prompt_name_snapshot": prompt.name,
        "prompt_scope_snapshot": prompt.scope,
        "prompt_content_snapshot": content,
    }


def save_ai_prompt_from_form(form, user):
    prompt_id = form.get("prompt_id", type=int)
    scope = normalize_ai_prompt_scope(form.get("scope"))
    if not can_manage_ai_prompt_scope(scope, user):
        abort(403)
    prompt = db.session.get(AIPrompt, prompt_id) if prompt_id else None
    if prompt and not can_manage_ai_prompt_scope(prompt.scope, user):
        abort(403)
    if prompt is None:
        prompt = AIPrompt(created_by=user.id)
        db.session.add(prompt)

    prompt.name = (form.get("name") or "").strip()
    prompt.scope = scope
    prompt.special_label = (form.get("special_label") or "").strip() if prompt.scope == "CUSTOM" else ""
    prompt.review_goal = (form.get("review_goal") or "").strip()
    prompt.focus_points = (form.get("focus_points") or "").strip()
    prompt.knowledge_sources = ai_prompt_knowledge_sources_from_form(form, prompt.scope)
    prompt.output_options = ai_prompt_output_options_from_form(form)
    prompt.is_active = bool(form.get("is_active"))
    set_default = bool(form.get("set_default"))
    prompt.is_default = set_default
    prompt.updated_by = user.id
    if not prompt.name:
        raise ValueError("模板名称不能为空")
    if not prompt.review_goal:
        raise ValueError("评审目标不能为空")
    if prompt.scope == "CUSTOM" and not prompt.special_label:
        raise ValueError("专项模板需要填写专项名称")

    db.session.flush()
    if set_default:
        (
            AIPrompt.query.filter(
                AIPrompt.scope == prompt.scope,
                AIPrompt.id != prompt.id,
            )
            .update({"is_default": False}, synchronize_session=False)
        )
        prompt.is_default = True
    db.session.commit()
    return prompt


def output_schema_for_options(options):
    normalized = dict(AI_PROMPT_OUTPUT_OPTIONS)
    if isinstance(options, dict):
        normalized.update({key: bool(options.get(key, normalized[key])) for key in normalized})
    fields = {
        "result": "approved 或 needs_revision",
        "summary": "一句话总结",
    }
    if normalized["include_score"]:
        fields["score"] = "0-100 的整数评分"
    if normalized["include_issues"]:
        fields["issues"] = "主要问题或材料缺口"
    if normalized["include_suggestions"]:
        fields["suggestions"] = "建议补充或修改的动作"
    if normalized["include_risk_points"]:
        fields["risk_points"] = "关键风险点列表或说明"
    return fields


def render_output_requirement(options):
    fields = output_schema_for_options(options)
    schema = json.dumps(fields, ensure_ascii=False)
    return (
        "【输出格式要求】\n"
        "请严格输出 JSON，且只输出 JSON，不要追加解释文字。\n"
        f"JSON 字段要求：{schema}"
    )


def render_ai_prompt_message(prompt, topic_title, scope, knowhow_text):
    if prompt is None:
        return (
            DEFAULT_AI_REVIEW_PROMPT.replace("{knowhow}", knowhow_text)
            .replace("{scope}", scope)
            .replace("{topic_title}", topic_title or "")
        )
    focus_points = (prompt.focus_points or "").strip() or "按模板知识来源和议题材料完整度进行评审。"
    return "\n\n".join(
        [
            "你是 QBP Meeting 材料 Review 助手。",
            f"当前议题「{topic_title or ''}」的业务上下文为 {scope}。",
            f"【评审目标】\n{prompt.review_goal.strip()}",
            f"【补充评审口径】\n{focus_points}",
            f"【模板导入的 know-how】\n{knowhow_text}",
            render_output_requirement(prompt.normalized_output_options),
        ]
    )


def sample_knowhow_text_for_prompt(prompt):
    if prompt is None:
        business_scopes = ai_business_scope_names()
        source_items = [
            {"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None},
        ]
        if business_scopes:
            source_items.append({"scope": business_scopes[0], "category_id": None})
    else:
        source_items = with_global_knowhow_source_items(prompt.normalized_knowledge_source_items)
    sample_by_scope = {
        GLOBAL_KNOWHOW_SCOPE: "所有BP材料必须说明数据来源、口径、gap和风险兜底",
    }
    for scope in ai_business_scope_names(include_stored=True):
        sample_by_scope[scope] = f"{scope} 材料需写清目标、关键假设、风险和待决策事项"
    lines = []
    for item in source_items:
        scope = item["scope"]
        if scope not in sample_by_scope:
            continue
        category_label = source_category_label(item)
        lines.append(f"- [{knowhow_scope_label(scope)}/{category_label}] {sample_by_scope[scope]}")
    return "\n".join(lines)


def source_category_label(source_item):
    category_id = source_item.get("category_id")
    if category_id is None:
        return "全部"
    if category_id == 0:
        return "未分类"
    category = db.session.get(AIKnowHowCategory, category_id)
    return category.name if category else "全部"


def split_legacy_prompt_content(content):
    text_value = (content or "").strip()
    if not text_value:
        return "评估材料是否完整，判断是否可以进入 QBP Meeting。", ""
    marker = "请基于以上 know-how"
    if marker in text_value:
        head = text_value.split(marker, 1)[0].strip()
        return head, ""
    return text_value, ""


def ensure_default_ai_prompt(admin_user=None):
    if AIPrompt.query.first():
        return
    legacy_content = ""
    stored = AppConfig.query.filter_by(key=AI_REVIEW_PROMPT_KEY).first()
    if stored and isinstance(stored.value_json, dict):
        legacy_content = (stored.value_json.get("content") or "").strip()
    review_goal, focus_points = split_legacy_prompt_content(legacy_content or DEFAULT_AI_REVIEW_PROMPT)
    user_id = admin_user.id if admin_user else None
    db.session.add(
        AIPrompt(
            name="通用默认模板",
            scope="GLOBAL",
            review_goal=review_goal,
            focus_points=focus_points,
            knowledge_sources=list(ai_business_scope_names()),
            output_options=dict(AI_PROMPT_OUTPUT_OPTIONS),
            is_active=True,
            is_default=True,
            created_by=user_id,
            updated_by=user_id,
        )
    )
    db.session.commit()


def _can_edit_knowhow_scope(scope, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return False
    if is_admin(user) or is_qbp_group_user(user):
        return True
    if (scope or "").upper() == GLOBAL_KNOWHOW_SCOPE:
        return False
    own_scope = manageable_ai_business_scope(user)
    return bool(own_scope and own_scope == (scope or "").strip())


def _can_manage_knowhow_categories(scope, user=None):
    user = user or current_user
    if is_admin(user) or is_qbp_group_user(user):
        return True
    if (scope or "").upper() == GLOBAL_KNOWHOW_SCOPE:
        return False
    own_scope = manageable_ai_business_scope(user)
    return bool(is_group_leader(user) and own_scope and own_scope == (scope or "").strip())


def _can_view_knowhow_scope(scope, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False):
        return False
    if (scope or "").upper() == GLOBAL_KNOWHOW_SCOPE:
        return can_manage_ai_prompt(user)
    if is_admin(user) or is_qbp_group_user(user):
        return True
    own_scope = manageable_ai_business_scope(user)
    return bool(own_scope and own_scope == (scope or "").strip())


def visible_knowhow_scopes(user=None):
    user = user or current_user
    own_scope = manageable_ai_business_scope(user)
    if own_scope and not (is_admin(user) or is_qbp_group_user(user)):
        return (GLOBAL_KNOWHOW_SCOPE, own_scope)
    return (GLOBAL_KNOWHOW_SCOPE, *ai_business_scope_names(include_stored=True))


def _default_knowhow_scope_for_user():
    user = current_user
    own_scope = manageable_ai_business_scope(user)
    if own_scope:
        return own_scope
    return GLOBAL_KNOWHOW_SCOPE


def knowhow_scope_options():
    return (GLOBAL_KNOWHOW_SCOPE, *ai_business_scope_names(include_stored=True))


def knowhow_scope_label(scope):
    return GLOBAL_KNOWHOW_LABEL if (scope or "").upper() == GLOBAL_KNOWHOW_SCOPE else scope


def knowhow_categories_for_scope(scope):
    return (
        AIKnowHowCategory.query.filter_by(scope=scope)
        .order_by(AIKnowHowCategory.name.asc())
        .all()
    )



def topic_readiness(topic, config_value=None):
    if config_value:
        return topic_score_from_weights(topic, config_value["weights"])
    return topic_completeness(topic)


def topic_completeness(topic, config_value=None):
    return topic_completeness_from_values(topic, config_value=config_value)


def topic_completeness_from_values(topic, attachment_count=None, latest_review=None, config_value=None):
    config_value = config_value or topic_completeness_config()
    scope = normalize_plan_version(topic.plan_version)
    rule = config_value["rules"].get(scope, config_value["rules"][DEFAULT_PLAN_VERSION])
    return topic_score_from_weights(
        topic,
        rule["weights"],
        attachment_count=attachment_count,
        latest_review=latest_review,
    )


def topic_score_from_weights(topic, weights, attachment_count=None, latest_review=None):
    missing = []
    score = 0
    if topic.title and normalize_topic_category(topic.category) in TOPIC_CATEGORY_OPTIONS and normalize_plan_version(topic.plan_version) and topic.owner:
        score += weights["basic_info"]
    else:
        missing.append("基础信息")
    if topic.background and topic.purpose:
        score += weights["background_purpose"]
    else:
        missing.append("背景/目的")
    if attachment_count is None:
        attachment_count = topic.attachments.count()
    if attachment_count > 0:
        score += weights["attachment"]
    else:
        missing.append("附件")
    if latest_review is None:
        latest_review = topic.latest_material_review
    review_passed = bool(latest_review and latest_review.result == "approved")
    if review_passed:
        score += weights["review"]
    else:
        missing.append("材料 Review")
    return {
        "score": score,
        "missing": missing,
        "review_passed": review_passed,
        "latest_review": latest_review,
    }


def completion_ratio(values):
    values = list(values)
    if not values:
        return 0
    return sum(1 for value in values if value) / len(values)


def topic_review_passed(topic):
    latest_review = topic.latest_material_review
    return bool(latest_review and latest_review.result == "approved")


def average_ratio(items):
    items = list(items)
    if not items:
        return 0
    return sum(items) / len(items)


def meeting_info_ratio(meeting):
    return completion_ratio(
        [
            meeting.title,
            meeting.meeting_date,
            meeting.location,
            meeting.host or (meeting.host_user.display_name if meeting.host_user else ""),
        ]
    )


def meeting_topic_completeness_ratio(topics):
    return average_ratio(topic_completeness(topic)["score"] / 100 for topic in topics)


def readiness_band(score, has_topics=True, config_value=None):
    config_value = config_value or meeting_readiness_config()
    thresholds = config_value["thresholds"]
    if not has_topics:
        return "not_started", "未开始"
    if score >= thresholds["ready"]:
        return "ready", "已就绪"
    if score >= thresholds["mostly_ready"]:
        return "mostly_ready", "基本就绪"
    if score >= thresholds["preparing"]:
        return "preparing", "准备中"
    return "risk", "风险"


def meeting_readiness_summary(meeting, topics):
    approved_topics = list(topics)
    topic_count = len(approved_topics)
    if not topic_count:
        return {
            "score": 0,
            "status_key": "not_started",
            "status_label": "未开始",
            "review_missing": 0,
            "topic_count": 0,
            "plan_count": 0,
            "decision_count": 0,
            "st_meeting_count": 0,
            "scope_labels": [],
            "topic_mix_label": "无议题",
            "gap_label": "暂无议题",
        }
    config_value = meeting_readiness_config()
    weights = config_value["weights"]
    component_scores = {
        "meeting_info": meeting_info_ratio(meeting),
        "topic_completeness": meeting_topic_completeness_ratio(approved_topics),
    }
    score = round(sum(component_scores[key] * weights[key] for key in weights))
    status_key, status_label = readiness_band(score, config_value=config_value)
    review_missing = sum(1 for topic in approved_topics if not topic_review_passed(topic))
    kick_off_count = sum(1 for topic in approved_topics if normalize_topic_category(topic.category) == "Kick Off")
    por_review_count = sum(1 for topic in approved_topics if normalize_topic_category(topic.category) == "POR Review")
    st_meeting_count = sum(1 for topic in approved_topics if normalize_topic_category(topic.category) == "ST Meeting")
    known_scopes = plan_version_names(include_stored=True)
    scope_rank = {scope: index for index, scope in enumerate(known_scopes)}
    scope_labels = sorted(
        {normalize_plan_version(topic.plan_version) for topic in approved_topics if normalize_plan_version(topic.plan_version)},
        key=lambda value: (scope_rank.get(value, len(scope_rank)), value),
    )
    gaps = []
    if component_scores["meeting_info"] < 1:
        gaps.append("会议基础信息")
    if component_scores["topic_completeness"] < 1:
        gaps.append("议题完善度")
    return {
        "score": score,
        "status_key": status_key,
        "status_label": status_label,
        "review_missing": review_missing,
        "topic_count": topic_count,
        "plan_count": kick_off_count,
        "decision_count": por_review_count,
        "st_meeting_count": st_meeting_count,
        "scope_labels": scope_labels,
        "topic_mix_label": (
            f"总 {topic_count} / Kick Off {kick_off_count} / "
            f"POR Review {por_review_count} / ST Meeting {st_meeting_count}"
        ),
        "gap_label": "、".join(gaps) if gaps else "无",
    }


def render_ai_review_system_prompt(topic, prompt=None):
    scope = topic.plan_version or ""
    prompt = prompt or selected_ai_prompt_for_topic(topic)
    source_items = selected_ai_review_knowhow_sources(prompt, scope)
    filters = knowhow_filters_for_source_items(source_items)
    knowhow_entries = []
    if filters:
        knowhow_entries = (
            AIKnowHow.query.filter(or_(*filters))
            .filter(AIKnowHow.is_active.is_(True))
            .outerjoin(AIKnowHowCategory, AIKnowHow.category_id == AIKnowHowCategory.id)
            .order_by(AIKnowHow.scope.asc(), AIKnowHow.created_at.asc())
            .all()
        )
    if knowhow_entries:
        knowhow_text = "\n".join(
            f"- [{entry.scope}/{entry.category.name if entry.category else '未分类'}] {entry.content}"
            for entry in knowhow_entries
        )
    else:
        knowhow_text = "（暂无知识沉淀）"
    prompt_text = render_ai_prompt_message(prompt, topic.title or "", scope, knowhow_text)
    snapshot = [
        {
            "id": entry.id,
            "scope": entry.scope,
            "category_id": entry.category_id,
            "category_name": entry.category.name if entry.category else "未分类",
            "content": entry.content,
        }
        for entry in knowhow_entries
    ]
    return prompt_text, snapshot


def with_global_knowhow_sources(sources):
    ordered = [GLOBAL_KNOWHOW_SCOPE]
    for source in sources or []:
        value = (source or "").strip()
        if value.upper() == GLOBAL_KNOWHOW_SCOPE:
            continue
        if value and value not in ordered:
            ordered.append(value)
    return ordered


def with_global_knowhow_source_items(source_items):
    ordered = []
    for item in source_items or []:
        if isinstance(item, dict):
            scope = (item.get("scope") or "").strip()
            category_id = item.get("category_id")
        else:
            scope = (item or "").strip()
            category_id = None
        if scope.upper() == GLOBAL_KNOWHOW_SCOPE:
            scope = GLOBAL_KNOWHOW_SCOPE
        if not scope:
            continue
        normalized = {"scope": scope, "category_id": category_id}
        if normalized not in ordered:
            ordered.append(normalized)
    if not any(item["scope"] == GLOBAL_KNOWHOW_SCOPE for item in ordered):
        ordered.insert(0, {"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None})
    return ordered


def selected_ai_review_knowhow_sources(prompt, topic_scope):
    if prompt:
        return with_global_knowhow_source_items(prompt.normalized_knowledge_source_items)
    return with_global_knowhow_source_items([
        {"scope": topic_scope, "category_id": None},
    ])


def knowhow_filters_for_source_items(source_items):
    filters = []
    for item in source_items:
        scope = item["scope"]
        category_id = item.get("category_id")
        scope_filter = AIKnowHow.scope == scope
        if category_id is None:
            filters.append(scope_filter)
        elif category_id == 0:
            filters.append(and_(scope_filter, AIKnowHow.category_id.is_(None)))
        else:
            filters.append(and_(scope_filter, AIKnowHow.category_id == category_id))
    return filters



def build_ai_material_review(topic, model, prompt=None):
    system_prompt, knowhow_snapshot = render_ai_review_system_prompt(topic, prompt=prompt)
    attachments = topic.attachments.all()
    material_query = build_material_review_query(topic, system_prompt)
    retrieved_chunks = retrieve_topic_material_chunks(topic, material_query, source="ai_review")
    material_index_status = [
        {
            "attachment_id": attachment.id,
            "filename": attachment.original_filename,
            "index_status": attachment.material_document.status if attachment.material_document else "not_indexed",
            "index_status_label": attachment.material_document.status_label if attachment.material_document else "未进入材料知识库",
            "warning": attachment.material_document.error_message if attachment.material_document else "该附件未进入材料知识库。",
        }
        for attachment in attachments
    ]
    payload = {
        "model": model,
        "stream": False,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "topic_title": topic.title,
                        "category": topic.category,
                        "plan_version": topic.plan_version,
                        "owner": topic.owner,
                        "background": topic.background,
                        "purpose": topic.purpose,
                        "attachments": [
                            {
                                "filename": attachment.original_filename,
                                "file_type": attachment.effective_file_type,
                                "file_size": attachment.file_size,
                            }
                            for attachment in attachments
                        ],
                        "material_index_status": material_index_status,
                        "retrieved_material_chunks": retrieved_chunks,
                        "strict_evidence_rule": (
                            "只能基于 retrieved_material_chunks 判断材料内容；没有对应 chunk 时必须说"
                            "未在已索引材料中找到，不能猜测。"
                        ),
                    },
                    ensure_ascii=False,
                ),
            },
        ],
        "params": {"function_calling": current_app.config.get("COPILOT_FUNCTION_CALLING", "default")},
    }
    answer = extract_chat_answer(zhishu_client().chat(payload))
    data = parse_ai_review_json(answer)
    result = data.get("result") if isinstance(data, dict) else ""
    if result not in {"approved", "needs_revision"}:
        result = "needs_revision"
    return {
        "result": result,
        "score": clamp_int(data.get("score") if isinstance(data, dict) else 0, 0, 100),
        "summary": str(data.get("summary", "") if isinstance(data, dict) else "").strip(),
        "issues": str(data.get("issues", "") if isinstance(data, dict) else "").strip(),
        "suggestions": str(data.get("suggestions", "") if isinstance(data, dict) else "").strip(),
        "knowhow_snapshot": knowhow_snapshot,
        "material_chunk_snapshot": retrieved_chunks,
        "prompt_content_snapshot": system_prompt,
    }


def build_material_review_query(topic, system_prompt=""):
    parts = [
        topic.topic_no or "",
        topic.title or "",
        topic.category or "",
        topic.plan_version or "",
        topic.owner or "",
        topic.background or "",
        topic.purpose or "",
        system_prompt or "",
    ]
    return "\n".join(part for part in parts if part).strip()


def parse_ai_review_json(answer):
    text_value = (answer or "").strip()
    if not text_value:
        return {}
    try:
        return json.loads(text_value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text_value, flags=re.S)
        if not match:
            raise ValueError("AI 返回内容不是 JSON")
        return json.loads(match.group(0))


def can_view_topic(topic, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not topic:
        return False
    if user.role == "admin" or is_qbp_group_user(user) or topic.created_by == user.id:
        return True
    if can_review_meeting(topic.meeting, user) or can_review_meeting(topic.requested_meeting, user):
        return True
    return user.id in topic.shared_user_ids


def can_share_topic(topic, user=None):
    user = user or current_user
    if not getattr(user, "is_authenticated", False) or not topic:
        return False
    return user.role == "admin" or topic.created_by == user.id


def can_edit_topic(topic, user=None):
    user = user or current_user
    if not can_view_topic(topic, user):
        return False
    if topic.workflow_status == "submitted":
        return False
    if user.role == "admin":
        return True
    if topic.meeting and can_review_meeting(topic.meeting, user):
        return True
    return topic.created_by == user.id and topic.workflow_status in {"draft", "rejected", "withdrawn"}


def can_edit_draft(topic):
    if topic.workflow_status == "submitted":
        return False
    return topic.created_by == current_user.id and topic.workflow_status in {"draft", "rejected", "withdrawn"} or is_admin()


def can_submit_draft(topic):
    return topic.created_by == current_user.id and topic.workflow_status in {"draft", "rejected", "withdrawn"}


def can_delete_topic(topic):
    if is_admin():
        return True
    if topic.meeting and can_review_meeting(topic.meeting):
        return True
    return topic.created_by == current_user.id and topic.workflow_status in {"draft", "rejected", "withdrawn"}


def select_topic(topics, topic_id):
    if not topics:
        return None
    if topic_id:
        for topic in topics:
            if topic.id == topic_id:
                return topic
    return topics[0]


def next_topic_order(meeting):
    latest = (
        Topic.query.filter(Topic.meeting_id == meeting.id)
        .order_by(Topic.present_order.desc())
        .first()
    )
    return (latest.present_order if latest else 0) + 1


def selectable_meetings_for_topic(topic):
    if topic:
        topic.ensure_scope_defaults()
        plan_version_id = topic.plan_version_id
        plan_round_id = topic.plan_round_id
        category = normalize_topic_category(topic.category)
    else:
        version, round_item = default_plan_scope()
        plan_version_id = version.id
        plan_round_id = round_item.id
        category = TOPIC_CATEGORY_OPTIONS[0]
    for meeting in Meeting.query.filter(
        db.or_(Meeting.plan_version_id.is_(None), Meeting.plan_round_id.is_(None), Meeting.category.is_(None))
    ).all():
        meeting.ensure_scope_defaults()
    db.session.flush()
    cond = Meeting.status.in_(["draft", "preparing", "reporting"])
    if topic and topic.requested_meeting_id:
        cond = db.or_(cond, Meeting.id == topic.requested_meeting_id)
    return (
        Meeting.query.filter(
            cond,
            Meeting.plan_version_id == plan_version_id,
            Meeting.plan_round_id == plan_round_id,
            Meeting.category.in_(topic_category_query_values(category)),
        )
        .order_by(Meeting.meeting_date.desc())
        .all()
    )


def visible_meeting_ids_for_current_user():
    return {
        row[0]
        for row in db.session.query(Topic.meeting_id)
        .filter(
            Topic.meeting_id.isnot(None),
            Topic.created_by == current_user.id,
            Topic.workflow_status == "approved",
        )
        .distinct()
        .all()
    }


def selectable_meetings_for_current_user():
    query = Meeting.query.order_by(Meeting.meeting_date.desc())
    if should_limit_meetings_to_own_approved_topics():
        visible_meeting_ids = visible_meeting_ids_for_current_user()
        if visible_meeting_ids:
            query = query.filter(Meeting.id.in_(visible_meeting_ids))
        else:
            query = query.filter(Meeting.id == -1)
    return query


def attachment_dir(app, topic):
    meeting_bucket = topic.meeting_id or topic.requested_meeting_id or "draft"
    return Path(app.config["UPLOAD_FOLDER"]) / str(meeting_bucket) / str(topic.id)


def attachment_path(app, attachment):
    return attachment_dir(app, attachment.topic) / attachment.stored_filename


def decrypt_saved_attachment_file(saved, topic):
    file_type = (saved.get("file_type") or "").lower()
    if file_type not in DECRYPTABLE_OFFICE_EXTENSIONS:
        current_app.logger.info(
            "Attachment decryption skipped: filename=%s type=%s path=%s reason=unsupported_type",
            saved.get("original_filename"),
            file_type or "-",
            saved.get("file_path"),
        )
        return saved

    original_path = Path(saved["file_path"])
    try:
        original_size = original_path.stat().st_size
        current_app.logger.info(
            "Attachment decryption started: filename=%s type=%s path=%s size=%s",
            saved.get("original_filename"),
            file_type,
            original_path,
            original_size,
        )
        decrypted_path = Path(decryption_service.decrypt_attachment(original_path))
        replaced = decrypted_path.resolve() != original_path.resolve()
        if decrypted_path.resolve() != original_path.resolve():
            shutil.copyfile(decrypted_path, original_path)
            try:
                decrypted_path.unlink()
            except OSError:
                current_app.logger.warning("Failed to remove temporary decrypted file: %s", decrypted_path)
        saved["file_size"] = original_path.stat().st_size
        current_app.logger.info(
            "Attachment decryption completed: filename=%s type=%s path=%s size=%s replaced=%s",
            saved.get("original_filename"),
            file_type,
            original_path,
            saved["file_size"],
            replaced,
        )
    except Exception as exc:
        current_app.logger.warning("Attachment decryption failed for %s: %s", original_path, exc)
        saved["decryption_error"] = str(exc)
    return saved


def record_decryption_failure_if_needed(saved, topic, attachment):
    if not saved.get("decryption_error"):
        return
    record_audit(
        "decrypt_attachment_failed",
        target_type="attachment",
        target_id=attachment.id if attachment else None,
        target_label=saved.get("original_filename"),
        metadata={
            "topic_id": topic.id if topic else None,
            "topic_title": topic.title if topic else "",
            "file_type": (saved.get("file_type") or "").lower(),
            "error": saved["decryption_error"],
        },
    )


def fileview_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="fileview-source")


def fileview_token_for_attachment(attachment, user):
    return fileview_serializer().dumps({"attachment_id": attachment.id, "user_id": user.id})


def verify_fileview_token(token, attachment_id):
    if not token:
        return None
    try:
        payload = fileview_serializer().loads(
            token,
            max_age=current_app.config.get("FILEVIEW_TOKEN_TTL_SECONDS", 300),
        )
    except (BadSignature, SignatureExpired):
        return None
    if payload.get("attachment_id") != attachment_id:
        return None
    user_id = payload.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def powerpoint_pdf_preview_response(attachment):
    preview_dir = Path(current_app.config["POWERPOINT_PREVIEW_FOLDER"]) / str(attachment.id)
    try:
        pdf_path = build_powerpoint_pdf_preview(
            attachment_path(current_app, attachment),
            preview_dir,
            attachment.effective_file_type,
            converter=current_app.config.get("POWERPOINT_CONVERTER"),
        )
    except PowerPointPreviewError as exc:
        return render_template("attachments/powerpoint_preview.html", attachment=attachment, error=str(exc))

    preview_name = f"{Path(fileview_filename_for_attachment(attachment)).stem}.pdf"
    return send_file(
        pdf_path,
        as_attachment=False,
        download_name=preview_name,
        mimetype="application/pdf",
    )


def fileview_preview_url(attachment):
    token = fileview_token_for_attachment(attachment, current_user)
    source_path = url_for(
        "attachment_fileview_source",
        attachment_id=attachment.id,
        token=token,
        fullfilename=fileview_filename_for_attachment(attachment),
    )
    source_url = f"{current_app.config['QBP_FILEVIEW_BASE_URL'].rstrip('/')}{source_path}"
    encoded = quote(base64.b64encode(source_url.encode("utf-8")).decode("ascii"), safe="")
    return f"{current_app.config['KKFILEVIEW_BASE_URL'].rstrip('/')}/onlinePreview?url={encoded}"


def fileview_filename_for_attachment(attachment):
    filename = attachment.original_filename or attachment.stored_filename or f"attachment-{attachment.id}"
    suffix = Path(filename).suffix
    file_type = attachment.effective_file_type
    if not suffix and file_type in {"pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx"}:
        return f"{filename}.{file_type}"
    return filename


def mimetype_for(attachment):
    file_type = attachment.effective_file_type
    if file_type == "pdf":
        return "application/pdf"
    if file_type in {"doc", "docx"}:
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if file_type in {"xls", "xlsx"}:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if file_type in {"ppt", "pptx"}:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if file_type in {"jpg", "jpeg"}:
        return "image/jpeg"
    if file_type == "png":
        return "image/png"
    return "application/octet-stream"


def ensure_sqlite_parent(database_uri):
    if not database_uri.startswith("sqlite:///") or database_uri == "sqlite:///:memory:":
        return
    raw_path = database_uri.replace("sqlite:///", "", 1)
    Path(raw_path).parent.mkdir(parents=True, exist_ok=True)


def resolve_project_path(value, project_root):
    path = Path(value)
    if path.is_absolute():
        return path
    return Path(project_root) / path


def ensure_lightweight_schema():
    if db.engine.dialect.name != "sqlite":
        return
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())
    if "groups" not in tables:
        Group.__table__.create(db.engine)
    if "plan_versions" not in tables:
        PlanVersion.__table__.create(db.engine)
    if "plan_rounds" not in tables:
        PlanRound.__table__.create(db.engine)
    if "users" in tables:
        add_column_if_missing("users", "enabled", "BOOLEAN NOT NULL DEFAULT 1")
        add_column_if_missing("users", "group_id", "INTEGER")
        add_column_if_missing("users", "email", "VARCHAR(120)")
        add_column_if_missing("users", "lark_open_id", "VARCHAR(80)")
        add_column_if_missing("users", "lark_user_id", "VARCHAR(80)")
        add_column_if_missing("users", "lark_synced_at", "DATETIME")
    if "meetings" in tables:
        add_column_if_missing("meetings", "host_user_id", "INTEGER")
        add_column_if_missing("meetings", "plan_version_id", "INTEGER")
        add_column_if_missing("meetings", "plan_round_id", "INTEGER")
        add_column_if_missing("meetings", "category", f"VARCHAR(100) NOT NULL DEFAULT '{TOPIC_CATEGORY_OPTIONS[0]}'")
    if "topics" in tables:
        add_column_if_missing("topics", "topic_no", "VARCHAR(20)")
        add_column_if_missing("topics", "requested_meeting_id", "INTEGER")
        add_column_if_missing("topics", "created_by", "INTEGER")
        add_column_if_missing(
            "topics",
            "plan_version",
            f"VARCHAR(50) NOT NULL DEFAULT '{DEFAULT_PLAN_VERSION}'",
        )
        add_column_if_missing("topics", "plan_version_id", "INTEGER")
        add_column_if_missing("topics", "plan_round_id", "INTEGER")
        add_column_if_missing(
            "topics",
            "duration_minutes",
            f"INTEGER NOT NULL DEFAULT {TOPIC_DURATION_DEFAULT_MINUTES}",
        )
        add_column_if_missing("topics", "workflow_status", "VARCHAR(20) NOT NULL DEFAULT 'approved'")
        add_column_if_missing("topics", "submitted_at", "DATETIME")
        add_column_if_missing("topics", "reviewed_by", "INTEGER")
        add_column_if_missing("topics", "reviewed_at", "DATETIME")
        add_column_if_missing("topics", "review_comment", "TEXT")
        add_column_if_missing("topics", "review_prompt_id", "INTEGER")
        add_column_if_missing("topics", "decision_status", "VARCHAR(20) NOT NULL DEFAULT 'pending'")
        add_column_if_missing("topics", "decision_by", "INTEGER")
        add_column_if_missing("topics", "decision_at", "DATETIME")
        add_column_if_missing("topics", "decision_comment", "TEXT NOT NULL DEFAULT ''")
        backfill_topic_numbers()
        rebuild_topics_for_nullable_meeting_id_if_needed()
        rename_legacy_topic_categories()
    if "audit_logs" not in tables:
        AuditLog.__table__.create(db.engine)
    if "app_configs" not in tables:
        AppConfig.__table__.create(db.engine)
    if "topic_material_reviews" not in tables:
        TopicMaterialReview.__table__.create(db.engine)
    else:
        add_column_if_missing("topic_material_reviews", "knowhow_snapshot", "TEXT")
        add_column_if_missing("topic_material_reviews", "material_chunk_snapshot", "TEXT")
        add_column_if_missing("topic_material_reviews", "prompt_id", "INTEGER")
        add_column_if_missing("topic_material_reviews", "prompt_name_snapshot", "VARCHAR(120)")
        add_column_if_missing("topic_material_reviews", "prompt_scope_snapshot", "VARCHAR(120)")
        add_column_if_missing("topic_material_reviews", "prompt_content_snapshot", "TEXT")
    if "material_documents" not in tables:
        MaterialDocument.__table__.create(db.engine)
    if "material_chunks" not in tables:
        MaterialChunk.__table__.create(db.engine)
    else:
        add_column_if_missing("material_chunks", "embedding_status", "VARCHAR(30) NOT NULL DEFAULT 'pending'")
        add_column_if_missing("material_chunks", "embedding_model", "VARCHAR(120)")
        add_column_if_missing("material_chunks", "embedding_dim", "INTEGER")
        add_column_if_missing("material_chunks", "embedding_error", "TEXT")
    if "material_retrieval_logs" not in tables:
        MaterialRetrievalLog.__table__.create(db.engine)
    else:
        add_column_if_missing("material_retrieval_logs", "retrieval_mode", "VARCHAR(30) NOT NULL DEFAULT 'keyword'")
        add_column_if_missing("material_retrieval_logs", "scores_json", "TEXT NOT NULL DEFAULT '[]'")
    if "ai_knowhow_categories" not in tables:
        AIKnowHowCategory.__table__.create(db.engine)
    if "ai_knowhow" not in tables:
        AIKnowHow.__table__.create(db.engine)
    else:
        add_column_if_missing("ai_knowhow", "category_id", "INTEGER")
        add_column_if_missing("ai_knowhow", "is_active", "BOOLEAN NOT NULL DEFAULT 1")
    if "ai_prompts" not in tables:
        AIPrompt.__table__.create(db.engine)
    else:
        add_column_if_missing("ai_prompts", "knowledge_sources", "TEXT NOT NULL DEFAULT '[]'")
    if "topic_shares" not in tables:
        TopicShare.__table__.create(db.engine)
    if "meeting_favorites" not in tables:
        MeetingFavorite.__table__.create(db.engine)


def add_column_if_missing(table, column, definition):
    existing = {item["name"] for item in inspect(db.engine).get_columns(table)}
    if column not in existing:
        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        db.session.commit()


def rename_legacy_topic_categories():
    from .models import LEGACY_TOPIC_CATEGORY_MAP
    for old, new in LEGACY_TOPIC_CATEGORY_MAP.items():
        db.session.execute(
            text("UPDATE topics SET category=:new WHERE category=:old"),
            {"new": new, "old": old},
        )
    db.session.commit()


def backfill_topic_numbers():
    db.session.execute(text("UPDATE topics SET topic_no = printf('T%08d', id) WHERE topic_no IS NULL OR topic_no = ''"))
    db.session.commit()


def rebuild_topics_for_nullable_meeting_id_if_needed():
    rows = db.session.execute(text("PRAGMA table_info(topics)")).fetchall()
    meeting_info = next((row for row in rows if row[1] == "meeting_id"), None)
    if not meeting_info or not meeting_info[3]:
        return
    db.session.execute(text("PRAGMA foreign_keys=off"))
    db.session.execute(
        text(
            """
            CREATE TABLE topics_new (
                id INTEGER PRIMARY KEY,
                topic_no VARCHAR(20),
                meeting_id INTEGER,
                requested_meeting_id INTEGER,
                created_by INTEGER,
                title VARCHAR(200) NOT NULL,
                category VARCHAR(100),
                plan_version VARCHAR(50) NOT NULL DEFAULT 'Q3 26BP',
                plan_version_id INTEGER,
                plan_round_id INTEGER,
                owner VARCHAR(100),
                background TEXT,
                purpose TEXT,
                duration_minutes INTEGER NOT NULL DEFAULT 15,
                present_order INTEGER NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                workflow_status VARCHAR(20) NOT NULL DEFAULT 'approved',
                submitted_at DATETIME,
                reviewed_by INTEGER,
                reviewed_at DATETIME,
                review_comment TEXT,
                review_prompt_id INTEGER,
                decision_status VARCHAR(20) NOT NULL DEFAULT 'pending',
                decision_by INTEGER,
                decision_at DATETIME,
                decision_comment TEXT NOT NULL DEFAULT '',
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    db.session.execute(
        text(
            """
            INSERT INTO topics_new (
                id, topic_no, meeting_id, requested_meeting_id, created_by, title, category, plan_version, plan_version_id, plan_round_id, owner,
                background, purpose, duration_minutes, present_order, status, workflow_status, submitted_at,
                reviewed_by, reviewed_at, review_comment, review_prompt_id, decision_status, decision_by, decision_at, decision_comment,
                created_at, updated_at
            )
            SELECT
                id, COALESCE(topic_no, printf('T%08d', id)), meeting_id, requested_meeting_id, created_by, title, category,
                COALESCE(plan_version, 'Q3 26BP'), plan_version_id, plan_round_id, owner,
                background, purpose, COALESCE(duration_minutes, 15), present_order, status, workflow_status, submitted_at,
                reviewed_by, reviewed_at, review_comment, review_prompt_id,
                COALESCE(decision_status, 'pending'), decision_by, decision_at, COALESCE(decision_comment, ''),
                created_at, updated_at
            FROM topics
            """
        )
    )
    db.session.execute(text("DROP TABLE topics"))
    db.session.execute(text("ALTER TABLE topics_new RENAME TO topics"))
    db.session.execute(text("PRAGMA foreign_keys=on"))
    db.session.commit()


def backfill_existing_data(admin):
    User.query.filter(User.enabled.is_(None)).update({"enabled": True})
    Meeting.query.filter(Meeting.host_user_id.is_(None)).update({"host_user_id": admin.id})
    default_version = PlanVersion.default()
    default_round = default_version.default_round()
    Meeting.query.filter(Meeting.plan_version_id.is_(None)).update({"plan_version_id": default_version.id})
    Meeting.query.filter(Meeting.plan_round_id.is_(None)).update({"plan_round_id": default_round.id})
    Meeting.query.filter(Meeting.category.is_(None)).update({"category": TOPIC_CATEGORY_OPTIONS[0]})
    for legacy_scope, new_scope in LEGACY_PLAN_VERSION_MAP.items():
        Topic.query.filter(Topic.plan_version == legacy_scope).update(
            {"plan_version": new_scope}
        )
    Topic.query.filter(
        db.or_(Topic.plan_version.is_(None), Topic.plan_version == "")
    ).update({"plan_version": DEFAULT_PLAN_VERSION})
    Topic.query.filter(Topic.plan_version_id.is_(None)).update({"plan_version_id": default_version.id})
    Topic.query.filter(Topic.plan_round_id.is_(None)).update({"plan_round_id": default_round.id})
    Topic.query.filter(Topic.duration_minutes.is_(None)).update(
        {"duration_minutes": TOPIC_DURATION_DEFAULT_MINUTES}
    )
    Topic.query.filter(Topic.workflow_status.is_(None)).update({"workflow_status": "approved"})
    Topic.query.filter(Topic.decision_status.is_(None)).update({"decision_status": "pending"})
    Topic.query.filter(Topic.decision_comment.is_(None)).update({"decision_comment": ""})
    Topic.query.filter(Topic.created_by.is_(None)).update({"created_by": admin.id})
    Topic.query.filter(Topic.meeting_id.isnot(None), Topic.workflow_status == "draft").update(
        {"workflow_status": "approved"}
    )
    for meeting in Meeting.query.all():
        meeting.ensure_scope_defaults()
    for topic in Topic.query.all():
        topic.ensure_scope_defaults()
    db.session.commit()


if __name__ == "__main__":
    app = create_app("development")
    with app.app_context():
        app.ensure_database()
    app.run(debug=True, host="0.0.0.0", port=5008)
