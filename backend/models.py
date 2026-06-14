from copy import deepcopy
from datetime import date, datetime
import re

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON, event
from werkzeug.security import check_password_hash, generate_password_hash


db = SQLAlchemy()

TOPIC_CATEGORY_OPTIONS = ("Kick Off", "POR Review", "ST Meeting")
LEGACY_TOPIC_CATEGORY_MAP = {}
DEMO_MEETING_TITLE = "Q3 26BP POR Review"
DEMO_MEETING_LOCATION = "QBP War Room / Teams"
DEMO_MEETING_HOST = "PLN/BP"
DEMO_MEETING_CATEGORY = "POR Review"
DEMO_TOPIC_ROWS = (
    ("OP Cum Yields", "OP"),
    ("NPP New Product CS时间", "PLN/NPP"),
    ("BE IE产能扩建", "PLN/BE IE"),
)
LEGACY_DEMO_MEETING_TITLE = "本周三 QBP Meeting"
DEFAULT_PLAN_VERSION_NAME = "Q3 26BP"
DEFAULT_PLAN_ROUND_NAME = "Round 1"
PLAN_VERSION_OPTIONS = (DEFAULT_PLAN_VERSION_NAME,)
GLOBAL_KNOWHOW_SCOPE = "GLOBAL"
GLOBAL_KNOWHOW_LABEL = "通用"
PLAN_VERSION_CODE_MAP = {
    version.lower().replace(" ", "_"): version
    for version in PLAN_VERSION_OPTIONS
}
DEFAULT_BUSINESS_GROUP_NAMES = (
    "MC",
    "OP",
    "PDC",
    "TD",
    "PLN/SP",
    "PLN/NPP",
    "PLN/BE IE",
    "PLN/AP CIE",
)
DEFAULT_BUSINESS_GROUP_CODES = tuple(
    name.lower().replace("/", "_").replace(" ", "_")
    for name in DEFAULT_BUSINESS_GROUP_NAMES
)
LEGACY_BUSINESS_GROUP_CODES = tuple(PLAN_VERSION_CODE_MAP)
LEGACY_BUSINESS_GROUP_CODE_PATTERN = re.compile(r"^q[1-4]_\d{2}bp$", re.IGNORECASE)
BUSINESS_GROUP_CODES = DEFAULT_BUSINESS_GROUP_CODES
DEFAULT_PLAN_VERSION = PLAN_VERSION_OPTIONS[0]
TOPIC_DURATION_DEFAULT_MINUTES = 15
TOPIC_DURATION_MIN_MINUTES = 5
TOPIC_DURATION_MAX_MINUTES = 180
AI_PROMPT_SCOPE_OPTIONS = ("GLOBAL", *DEFAULT_BUSINESS_GROUP_NAMES, "CUSTOM")
AI_PROMPT_OUTPUT_OPTIONS = {
    "include_score": True,
    "include_issues": True,
    "include_suggestions": True,
    "include_risk_points": False,
}
LEGACY_PLAN_VERSION_MAP = {}
PLAN_VERSION_NAME_PATTERN = re.compile(r"^Q[1-4]\s+\d{2}BP$", re.IGNORECASE)

def normalize_topic_category(value):
    value = (value or "").strip()
    value = LEGACY_TOPIC_CATEGORY_MAP.get(value, value)
    return value if value in TOPIC_CATEGORY_OPTIONS else TOPIC_CATEGORY_OPTIONS[0]


def normalize_plan_version(value):
    value = (value or "").strip()
    value = LEGACY_PLAN_VERSION_MAP.get(value, value)
    try:
        if value and PlanVersion.query.filter_by(name=value, is_active=True).first():
            return value
    except Exception:
        pass
    if value in PLAN_VERSION_OPTIONS or PLAN_VERSION_NAME_PATTERN.match(value):
        return value
    return DEFAULT_PLAN_VERSION


def active_plan_version_names(include_default=True):
    names = []
    if include_default:
        names.append(DEFAULT_PLAN_VERSION)
    try:
        names.extend(
            version.name
            for version in PlanVersion.query.filter_by(is_active=True).order_by(PlanVersion.sort_order.asc(), PlanVersion.id.asc()).all()
        )
    except Exception:
        pass
    return tuple(dict.fromkeys(names))


def normalize_topic_duration(value):
    value = (value or "").strip()
    if not value:
        return TOPIC_DURATION_DEFAULT_MINUTES
    try:
        minutes = int(value)
    except ValueError:
        return TOPIC_DURATION_DEFAULT_MINUTES
    return max(TOPIC_DURATION_MIN_MINUTES, min(TOPIC_DURATION_MAX_MINUTES, minutes))


DEFAULT_GROUPS = (
    ("qbp", "PLN/BP", True),
    *((code, name, False) for code, name in zip(DEFAULT_BUSINESS_GROUP_CODES, DEFAULT_BUSINESS_GROUP_NAMES)),
)
ADMIN_GROUP_CODE = "qbp"
MEETING_READINESS_CONFIG_KEY = "meeting_readiness"
TOPIC_COMPLETENESS_CONFIG_KEY = "topic_completeness"
DEFAULT_MEETING_READINESS_CONFIG = {
    "weights": {
        "meeting_info": 20,
        "topic_completeness": 80,
    },
    "thresholds": {
        "ready": 90,
        "mostly_ready": 70,
        "preparing": 40,
    },
}
DEFAULT_TOPIC_COMPLETENESS_RULE = {
    "weights": {
        "basic_info": 30,
        "background_purpose": 30,
        "attachment": 20,
        "review": 20,
    },
    "thresholds": {
        "ready": 90,
        "mostly_ready": 70,
        "preparing": 40,
    },
}
DEFAULT_TOPIC_COMPLETENESS_CONFIG = {
    "rules": {
        scope: deepcopy(DEFAULT_TOPIC_COMPLETENESS_RULE)
        for scope in PLAN_VERSION_OPTIONS
    },
}


def _normalize_scoring_rule(value, default_rule=None):
    rule = deepcopy(default_rule or DEFAULT_MEETING_READINESS_CONFIG)
    if not isinstance(value, dict):
        return rule
    weights = value.get("weights") if isinstance(value.get("weights"), dict) else {}
    thresholds = value.get("thresholds") if isinstance(value.get("thresholds"), dict) else {}
    for key, default in rule["weights"].items():
        try:
            rule["weights"][key] = int(weights.get(key, default))
        except (TypeError, ValueError):
            rule["weights"][key] = default
    for key, default in rule["thresholds"].items():
        try:
            rule["thresholds"][key] = int(thresholds.get(key, default))
        except (TypeError, ValueError):
            rule["thresholds"][key] = default
    return rule


AI_REVIEW_PROMPT_KEY = "ai_review_prompt"
DEFAULT_AI_REVIEW_PROMPT = (
    "你是 QBP Meeting 材料 Review 助手。\n"
    "当前议题「{topic_title}」的业务上下文为 {scope}。\n\n"
    "【该方向的 know-how / 关注点】\n"
    "{knowhow}\n\n"
    "【材料证据规则】\n"
    "用户消息中的 retrieved_material_chunks 是从已索引附件中检索出的材料证据。"
    "你只能依据这些 chunk 判断附件是否写明；每个材料结论都要引用 filename + source_label。"
    "如果 retrieved_material_chunks 为空，或某个问题没有对应 chunk，请明确说明未在已索引材料中找到，不能臆测附件内容。\n\n"
    "请基于以上 know-how 评估材料，并严格输出 JSON，且只输出 JSON："
    '{"result":"approved 或 needs_revision","score":0-100,'
    '"summary":"一句话总结","issues":"主要问题","suggestions":"补充建议"}'
)
LEGACY_GROUP_CODE_MAP = {
    "admin_office": "qbp",
}


def default_meeting_readiness_config():
    return deepcopy(DEFAULT_MEETING_READINESS_CONFIG)


def normalize_meeting_readiness_config(value):
    if isinstance(value, dict) and isinstance(value.get("weights"), dict):
        weights = dict(value["weights"])
        if "topic_completeness" not in weights:
            weights["topic_completeness"] = (
                weights.get("topic_material", 0)
                + weights.get("review", 0)
            )
        if "topic_list" in weights:
            weights["topic_completeness"] = weights.get("topic_completeness", 0) + weights.get("topic_list", 0)
            weights.pop("topic_list", None)
        if "topic_plan" in weights:
            weights["topic_completeness"] = weights.get("topic_completeness", 0) + weights.get("topic_plan", 0)
            weights.pop("topic_plan", None)
        value = {**value, "weights": weights}
    return _normalize_scoring_rule(value, DEFAULT_MEETING_READINESS_CONFIG)


def default_topic_completeness_config():
    return deepcopy(DEFAULT_TOPIC_COMPLETENESS_CONFIG)


def normalize_topic_completeness_config(value):
    config = default_topic_completeness_config()
    stored_rules = value.get("rules") if isinstance(value, dict) and isinstance(value.get("rules"), dict) else {}
    scopes = set(PLAN_VERSION_OPTIONS)
    try:
        scopes.update(version.name for version in PlanVersion.query.filter_by(is_active=True).all())
    except Exception:
        pass
    for scope in scopes:
        config["rules"][scope] = _normalize_scoring_rule(stored_rules.get(scope), DEFAULT_TOPIC_COMPLETENESS_RULE)
    return config


class PlanVersion(db.Model):
    __tablename__ = "plan_versions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    sort_order = db.Column(db.Integer, default=1, nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    rounds = db.relationship(
        "PlanRound",
        back_populates="plan_version",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="PlanRound.sort_order",
    )

    @classmethod
    def seed_defaults(cls):
        version = cls.query.filter_by(name=DEFAULT_PLAN_VERSION_NAME).first()
        if not version:
            version = cls(name=DEFAULT_PLAN_VERSION_NAME, sort_order=1, is_active=True)
            db.session.add(version)
            db.session.flush()
        if not PlanRound.query.filter_by(plan_version_id=version.id, name=DEFAULT_PLAN_ROUND_NAME).first():
            db.session.add(
                PlanRound(
                    plan_version_id=version.id,
                    name=DEFAULT_PLAN_ROUND_NAME,
                    sort_order=1,
                    is_active=True,
                )
            )
        db.session.commit()
        return version

    @classmethod
    def active_options(cls):
        return cls.query.filter_by(is_active=True).order_by(cls.sort_order.asc(), cls.id.asc()).all()

    @classmethod
    def default(cls):
        return cls.seed_defaults()

    @classmethod
    def create_with_default_round(cls, name):
        name = (name or "").strip()
        if not name:
            raise ValueError("Plan Version 不能为空")
        if cls.query.filter_by(name=name).first():
            raise ValueError("Plan Version 已存在")
        max_order = db.session.query(db.func.max(cls.sort_order)).scalar() or 0
        version = cls(name=name, sort_order=max_order + 1, is_active=True)
        db.session.add(version)
        db.session.flush()
        db.session.add(
            PlanRound(
                plan_version_id=version.id,
                name=DEFAULT_PLAN_ROUND_NAME,
                sort_order=1,
                is_active=True,
            )
        )
        db.session.commit()
        return version

    def default_round(self):
        existing = self.rounds.filter_by(is_active=True).order_by(PlanRound.sort_order.asc(), PlanRound.id.asc()).first()
        if existing:
            return existing
        created = PlanRound(plan_version_id=self.id, name=DEFAULT_PLAN_ROUND_NAME, sort_order=1, is_active=True)
        db.session.add(created)
        db.session.commit()
        return created


class PlanRound(db.Model):
    __tablename__ = "plan_rounds"

    id = db.Column(db.Integer, primary_key=True)
    plan_version_id = db.Column(db.Integer, db.ForeignKey("plan_versions.id"), nullable=False, index=True)
    name = db.Column(db.String(50), nullable=False)
    sort_order = db.Column(db.Integer, default=1, nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    plan_version = db.relationship("PlanVersion", back_populates="rounds")
    __table_args__ = (db.UniqueConstraint("plan_version_id", "name", name="uq_plan_round_version_name"),)

    @classmethod
    def active_for_version(cls, plan_version_id):
        return (
            cls.query.filter_by(plan_version_id=plan_version_id, is_active=True)
            .order_by(cls.sort_order.asc(), cls.id.asc())
            .all()
        )

    @classmethod
    def create_next(cls, plan_version, name=None):
        if not plan_version:
            raise ValueError("Plan Version 不存在")
        name = (name or "").strip()
        max_order = (
            db.session.query(db.func.max(cls.sort_order))
            .filter_by(plan_version_id=plan_version.id)
            .scalar()
            or 0
        )
        if not name:
            name = f"Round {max_order + 1}"
        if cls.query.filter_by(plan_version_id=plan_version.id, name=name).first():
            raise ValueError("Round 已存在")
        round_item = cls(
            plan_version_id=plan_version.id,
            name=name,
            sort_order=max_order + 1,
            is_active=True,
        )
        db.session.add(round_item)
        db.session.commit()
        return round_item


class Group(db.Model):
    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False, index=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    is_admin_group = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    members = db.relationship("User", backref="group", lazy="dynamic", foreign_keys="User.group_id")

    @classmethod
    def seed_defaults(cls):
        new_name_by_code = {code: name for code, name, _ in DEFAULT_GROUPS}
        for old_code, new_code in LEGACY_GROUP_CODE_MAP.items():
            legacy = cls.query.filter_by(code=old_code).first()
            if legacy and not cls.query.filter_by(code=new_code).first():
                legacy.code = new_code
                legacy.name = new_name_by_code[new_code]
        db.session.commit()
        for code, name, is_admin in DEFAULT_GROUPS:
            group = cls.query.filter_by(code=code).first()
            if group:
                group.name = name
                group.is_admin_group = is_admin
            else:
                db.session.add(cls(code=code, name=name, is_admin_group=is_admin))
        db.session.commit()
        legacy_groups = cls.query.filter(cls.is_admin_group.is_(False)).all()
        for legacy in legacy_groups:
            if (
                legacy.code in LEGACY_BUSINESS_GROUP_CODES
                or LEGACY_BUSINESS_GROUP_CODE_PATTERN.match(legacy.code or "")
                or PLAN_VERSION_NAME_PATTERN.match(legacy.name or "")
            ):
                User.query.filter_by(group_id=legacy.id).update({"group_id": None})
                db.session.delete(legacy)
        db.session.commit()

    @classmethod
    def admin_group(cls):
        return cls.query.filter_by(code=ADMIN_GROUP_CODE).first()


class AppConfig(db.Model):
    __tablename__ = "app_configs"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    value_json = db.Column("value", JSON, nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    updater = db.relationship("User", foreign_keys=[updated_by])


class AIPrompt(db.Model):
    __tablename__ = "ai_prompts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, index=True)
    scope = db.Column(db.String(20), default="GLOBAL", nullable=False, index=True)
    special_label = db.Column(db.String(120))
    review_goal = db.Column(db.Text, nullable=False)
    focus_points = db.Column(db.Text)
    knowledge_sources = db.Column(JSON, nullable=False, default=list)
    output_options = db.Column(JSON, nullable=False, default=lambda: deepcopy(AI_PROMPT_OUTPUT_OPTIONS))
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    is_default = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    creator = db.relationship("User", foreign_keys=[created_by])
    updater = db.relationship("User", foreign_keys=[updated_by])

    @property
    def normalized_output_options(self):
        options = deepcopy(AI_PROMPT_OUTPUT_OPTIONS)
        if isinstance(self.output_options, dict):
            for key in options:
                options[key] = bool(self.output_options.get(key, options[key]))
        return options

    @property
    def scope_label(self):
        if self.scope == "GLOBAL":
            return "通用模板"
        if self.scope == "CUSTOM":
            return self.special_label or "专项模板"
        return f"{self.scope} 专属模板"

    @property
    def normalized_knowledge_sources(self):
        return [item["scope"] for item in self.normalized_knowledge_source_items]

    @property
    def normalized_knowledge_source_items(self):
        sources = self.knowledge_sources if isinstance(self.knowledge_sources, list) else []
        normalized = []
        for source in sources:
            if isinstance(source, dict):
                value = (source.get("scope") or "").strip()
                raw_category_id = source.get("category_id")
            else:
                value = (source or "").strip()
                raw_category_id = None
            if value.upper() == GLOBAL_KNOWHOW_SCOPE:
                value = GLOBAL_KNOWHOW_SCOPE
            if not value:
                continue
            category_id = None
            if raw_category_id not in (None, "", "ALL"):
                try:
                    category_id = max(0, int(raw_category_id))
                except (TypeError, ValueError):
                    category_id = None
            item = {"scope": value, "category_id": category_id}
            if item not in normalized:
                normalized.append(item)
        if normalized:
            return normalized
        if self.scope and self.scope.upper() != GLOBAL_KNOWHOW_SCOPE and self.scope != "CUSTOM":
            return [
                {"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None},
                {"scope": self.scope, "category_id": None},
            ]
        return [{"scope": GLOBAL_KNOWHOW_SCOPE, "category_id": None}] + [
            {"scope": scope, "category_id": None}
            for scope in DEFAULT_BUSINESS_GROUP_NAMES
        ]


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    display_name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(30), default="admin", nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("groups.id"), index=True)
    email = db.Column(db.String(120), index=True)
    lark_open_id = db.Column(db.String(80), index=True)
    lark_user_id = db.Column(db.String(80), index=True)
    lark_synced_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def is_active(self):
        return bool(self.enabled)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def group_role_label(self):
        code = self.group.code if self.group else ""
        if code == ADMIN_GROUP_CODE:
            return "\u8bc4\u5ba1\u7ec4"
        if code in BUSINESS_GROUP_CODES:
            return "\u91c7\u8d2d\u7ec4"
        return ""

    @classmethod
    def create_default_admin(cls):
        if not Group.admin_group():
            Group.seed_defaults()
        admin = cls.query.filter_by(username="admin").first()
        if admin:
            return admin
        admin = cls(
            username="admin",
            display_name="管理员",
            role="admin",
            group_id=None,
        )
        admin.set_password("admin123")
        db.session.add(admin)
        db.session.commit()
        return admin


class Meeting(db.Model):
    __tablename__ = "meetings"

    id = db.Column(db.Integer, primary_key=True)
    meeting_no = db.Column(db.String(20), unique=True, nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    meeting_date = db.Column(db.Date, nullable=False)
    location = db.Column(db.String(200))
    host = db.Column(db.String(100))
    status = db.Column(db.String(20), default="draft", nullable=False)
    plan_version_id = db.Column(db.Integer, db.ForeignKey("plan_versions.id"), index=True)
    plan_round_id = db.Column(db.Integer, db.ForeignKey("plan_rounds.id"), index=True)
    category = db.Column(db.String(100), default=TOPIC_CATEGORY_OPTIONS[0], nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    host_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    topics = db.relationship(
        "Topic",
        backref="meeting",
        foreign_keys="Topic.meeting_id",
        lazy="dynamic",
        cascade="save-update, merge",
        order_by="Topic.present_order",
    )
    host_user = db.relationship("User", foreign_keys=[host_user_id])
    plan_version_ref = db.relationship("PlanVersion", foreign_keys=[plan_version_id])
    plan_round_ref = db.relationship("PlanRound", foreign_keys=[plan_round_id])
    minutes = db.relationship(
        "MeetingMinutes",
        backref="meeting",
        uselist=False,
        cascade="all, delete-orphan",
    )
    favorites = db.relationship(
        "MeetingFavorite",
        back_populates="meeting",
        lazy="dynamic",
        cascade="all, delete-orphan",
    )

    @classmethod
    def next_meeting_no(cls):
        prefix = f"CM{datetime.now().year}"
        last = (
            cls.query.filter(cls.meeting_no.like(f"{prefix}%"))
            .order_by(cls.meeting_no.desc())
            .first()
        )
        if not last:
            return f"{prefix}0001"
        return f"{prefix}{int(last.meeting_no[len(prefix):]) + 1:04d}"

    @property
    def plan_version_name(self):
        return self.plan_version_ref.name if self.plan_version_ref else DEFAULT_PLAN_VERSION_NAME

    @property
    def plan_round_name(self):
        return self.plan_round_ref.name if self.plan_round_ref else DEFAULT_PLAN_ROUND_NAME

    def ensure_scope_defaults(self):
        if not self.plan_version_id:
            version = PlanVersion.default()
            self.plan_version_id = version.id
        version = db.session.get(PlanVersion, self.plan_version_id) or PlanVersion.default()
        if not self.plan_round_id:
            self.plan_round_id = version.default_round().id
        self.category = normalize_topic_category(self.category)

    @classmethod
    def seed_demo(cls):
        if cls.query.filter_by(title=DEMO_MEETING_TITLE).first():
            return
        legacy_meeting = cls.query.filter_by(title=LEGACY_DEMO_MEETING_TITLE).first()
        admin = User.create_default_admin()
        version = PlanVersion.default()
        round_item = version.default_round()

        if legacy_meeting:
            meeting = legacy_meeting
            meeting.title = DEMO_MEETING_TITLE
            meeting.location = DEMO_MEETING_LOCATION
            meeting.host = DEMO_MEETING_HOST
            meeting.category = DEMO_MEETING_CATEGORY
            meeting.plan_version_id = version.id
            meeting.plan_round_id = round_item.id
            topics = (
                Topic.query.filter_by(meeting_id=meeting.id)
                .order_by(Topic.present_order.asc(), Topic.id.asc())
                .all()
            )
        else:
            meeting = cls(
                meeting_no=cls.next_meeting_no(),
                title=DEMO_MEETING_TITLE,
                meeting_date=date(2026, 5, 27),
                location=DEMO_MEETING_LOCATION,
                host=DEMO_MEETING_HOST,
                status="preparing",
                plan_version_id=version.id,
                plan_round_id=round_item.id,
                category=DEMO_MEETING_CATEGORY,
                created_by=admin.id,
                host_user_id=admin.id,
            )
            db.session.add(meeting)
            db.session.flush()
            topics = []

        for order, (title, owner) in enumerate(DEMO_TOPIC_ROWS, start=1):
            topic = topics[order - 1] if order <= len(topics) else Topic(meeting_id=meeting.id)
            topic.title = title
            topic.category = DEMO_MEETING_CATEGORY
            topic.plan_version = DEFAULT_PLAN_VERSION
            topic.plan_version_id = version.id
            topic.plan_round_id = round_item.id
            topic.owner = owner
            topic.present_order = order
            topic.status = "pending"
            topic.workflow_status = "approved"
            topic.created_by = topic.created_by or admin.id
            db.session.add(topic)
        db.session.commit()


class MeetingFavorite(db.Model):
    __tablename__ = "meeting_favorites"

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), primary_key=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    meeting = db.relationship("Meeting", back_populates="favorites", foreign_keys=[meeting_id])


class Topic(db.Model):
    __tablename__ = "topics"

    id = db.Column(db.Integer, primary_key=True)
    topic_no = db.Column(db.String(20), unique=True, index=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=True)
    requested_meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(100))
    plan_version = db.Column(db.String(50), default=DEFAULT_PLAN_VERSION, nullable=False)
    plan_version_id = db.Column(db.Integer, db.ForeignKey("plan_versions.id"), index=True)
    plan_round_id = db.Column(db.Integer, db.ForeignKey("plan_rounds.id"), index=True)
    owner = db.Column(db.String(100))
    background = db.Column(db.Text)
    purpose = db.Column(db.Text)
    duration_minutes = db.Column(
        db.Integer,
        default=TOPIC_DURATION_DEFAULT_MINUTES,
        server_default=str(TOPIC_DURATION_DEFAULT_MINUTES),
        nullable=False,
    )
    present_order = db.Column(db.Integer, default=1, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    workflow_status = db.Column(db.String(20), default="draft", nullable=False)
    decision_status = db.Column(db.String(20), default="pending", nullable=False)
    decision_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    decision_at = db.Column(db.DateTime)
    decision_comment = db.Column(db.Text, default="")
    submitted_at = db.Column(db.DateTime)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    review_comment = db.Column(db.Text)
    review_prompt_id = db.Column(db.Integer, db.ForeignKey("ai_prompts.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    attachments = db.relationship(
        "Attachment",
        backref="topic",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Attachment.uploaded_at.desc()",
    )
    requested_meeting = db.relationship("Meeting", foreign_keys=[requested_meeting_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    decision_user = db.relationship("User", foreign_keys=[decision_by])
    review_prompt = db.relationship("AIPrompt", foreign_keys=[review_prompt_id])
    plan_version_ref = db.relationship("PlanVersion", foreign_keys=[plan_version_id])
    plan_round_ref = db.relationship("PlanRound", foreign_keys=[plan_round_id])
    material_reviews = db.relationship(
        "TopicMaterialReview",
        backref="topic",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="TopicMaterialReview.created_at.desc()",
    )
    shares = db.relationship(
        "TopicShare",
        backref="topic",
        lazy="dynamic",
        cascade="all, delete-orphan",
        foreign_keys="TopicShare.topic_id",
    )

    @staticmethod
    def number_for_id(topic_id):
        return f"T{int(topic_id):08d}"

    def ensure_topic_no(self):
        if self.id and not self.topic_no:
            self.topic_no = self.number_for_id(self.id)

    @property
    def plan_version_name(self):
        if self.plan_version_ref:
            return self.plan_version_ref.name
        return self.plan_version or DEFAULT_PLAN_VERSION_NAME

    @property
    def plan_round_name(self):
        return self.plan_round_ref.name if self.plan_round_ref else DEFAULT_PLAN_ROUND_NAME

    def ensure_scope_defaults(self):
        if not self.plan_version_id:
            version = PlanVersion.query.filter_by(name=self.plan_version or DEFAULT_PLAN_VERSION_NAME).first()
            if not version:
                version = PlanVersion.default()
            self.plan_version_id = version.id
        version = db.session.get(PlanVersion, self.plan_version_id) or PlanVersion.default()
        self.plan_version = version.name
        if not self.plan_round_id:
            self.plan_round_id = version.default_round().id
        self.category = normalize_topic_category(self.category)

    def apply_meeting_scope(self, meeting):
        meeting.ensure_scope_defaults()
        self.plan_version_id = meeting.plan_version_id
        self.plan_round_id = meeting.plan_round_id
        self.plan_version = meeting.plan_version_name
        self.category = normalize_topic_category(meeting.category)

    @property
    def latest_material_review(self):
        return self.material_reviews.order_by(TopicMaterialReview.created_at.desc()).first()

    @property
    def shared_user_ids(self):
        return {share.user_id for share in self.shares}


class TopicShare(db.Model):
    __tablename__ = "topic_shares"

    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), primary_key=True)
    granted_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", foreign_keys=[user_id])
    granter = db.relationship("User", foreign_keys=[granted_by])


@event.listens_for(Topic, "after_insert")
def assign_topic_no_after_insert(mapper, connection, target):
    if target.topic_no or not target.id:
        return
    topic_no = Topic.number_for_id(target.id)
    connection.execute(Topic.__table__.update().where(Topic.id == target.id).values(topic_no=topic_no))
    target.topic_no = topic_no


class TopicMaterialReview(db.Model):
    __tablename__ = "topic_material_reviews"

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=False, index=True)
    source = db.Column(db.String(20), default="hoster", nullable=False)
    result = db.Column(db.String(30), nullable=False)
    score = db.Column(db.Integer, default=0, nullable=False)
    summary = db.Column(db.Text)
    issues = db.Column(db.Text)
    suggestions = db.Column(db.Text)
    reviewed_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    knowhow_snapshot = db.Column(JSON, default=list)
    material_chunk_snapshot = db.Column(JSON, default=list)
    prompt_id = db.Column(db.Integer, db.ForeignKey("ai_prompts.id"))
    prompt_name_snapshot = db.Column(db.String(120))
    prompt_scope_snapshot = db.Column(db.String(120))
    prompt_content_snapshot = db.Column(db.Text)

    reviewer = db.relationship("User", foreign_keys=[reviewed_by])
    prompt = db.relationship("AIPrompt", foreign_keys=[prompt_id])


class MaterialDocument(db.Model):
    __tablename__ = "material_documents"

    id = db.Column(db.Integer, primary_key=True)
    attachment_id = db.Column(db.Integer, db.ForeignKey("attachments.id"), unique=True, nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=False, index=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), index=True)
    status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    text_hash = db.Column(db.String(64))
    parser_version = db.Column(db.String(30), default="attachment_text_v1", nullable=False)
    zhishu_file_id = db.Column(db.String(120))
    zhishu_topic_knowledge_id = db.Column(db.String(120))
    zhishu_meeting_knowledge_id = db.Column(db.String(120))
    error_message = db.Column(db.Text)
    chunk_count = db.Column(db.Integer, default=0, nullable=False)
    indexed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    attachment = db.relationship("Attachment", back_populates="material_document")
    topic = db.relationship("Topic", foreign_keys=[topic_id])
    meeting = db.relationship("Meeting", foreign_keys=[meeting_id])
    chunks = db.relationship(
        "MaterialChunk",
        back_populates="document",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="MaterialChunk.chunk_index.asc()",
    )

    @property
    def status_label(self):
        return {
            "pending": "待索引",
            "indexing": "索引中",
            "indexed": "已索引",
            "failed": "索引失败",
            "unsupported": "不支持解析",
            "deleted": "已删除",
        }.get(self.status, self.status or "-")


class MaterialChunk(db.Model):
    __tablename__ = "material_chunks"

    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("material_documents.id"), nullable=False, index=True)
    attachment_id = db.Column(db.Integer, db.ForeignKey("attachments.id"), nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=False, index=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), index=True)
    chunk_index = db.Column(db.Integer, nullable=False)
    source_label = db.Column(db.String(80))
    text = db.Column(db.Text, nullable=False)
    text_hash = db.Column(db.String(64), nullable=False, index=True)
    char_count = db.Column(db.Integer, default=0, nullable=False)
    zhishu_chunk_id = db.Column(db.String(120))
    embedding_status = db.Column(db.String(30), default="pending", nullable=False, index=True)
    embedding_model = db.Column(db.String(120))
    embedding_dim = db.Column(db.Integer)
    embedding_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    document = db.relationship("MaterialDocument", back_populates="chunks")
    attachment = db.relationship("Attachment", foreign_keys=[attachment_id])
    topic = db.relationship("Topic", foreign_keys=[topic_id])
    meeting = db.relationship("Meeting", foreign_keys=[meeting_id])

    def citation_dict(self, score=None):
        return {
            "chunk_id": self.id,
            "attachment_id": self.attachment_id,
            "filename": self.attachment.original_filename if self.attachment else "",
            "topic_id": self.topic_id,
            "topic_title": self.topic.title if self.topic else "",
            "meeting_id": self.meeting_id,
            "source_label": self.source_label or f"Chunk {self.chunk_index}",
            "chunk_index": self.chunk_index,
            "text": self.text,
            "score": score,
        }


class MaterialRetrievalLog(db.Model):
    __tablename__ = "material_retrieval_logs"

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(30), nullable=False, index=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), index=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), index=True)
    query_text = db.Column("query", db.Text)
    scope_type = db.Column(db.String(20), nullable=False)
    scope_id = db.Column(db.Integer, nullable=False)
    chunk_ids = db.Column(JSON, nullable=False, default=list)
    retrieval_mode = db.Column(db.String(30), default="keyword", nullable=False)
    scores_json = db.Column(JSON, nullable=False, default=list)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    topic = db.relationship("Topic", foreign_keys=[topic_id])
    meeting = db.relationship("Meeting", foreign_keys=[meeting_id])


class Attachment(db.Model):
    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    file_type = db.Column(db.String(20), nullable=False)
    file_size = db.Column(db.Integer, default=0, nullable=False)
    uploaded_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    material_document = db.relationship(
        "MaterialDocument",
        back_populates="attachment",
        uselist=False,
        cascade="all, delete-orphan",
    )

    @property
    def effective_file_type(self):
        if self.file_type:
            return self.file_type.lower()
        for filename in (self.original_filename, self.stored_filename):
            if filename and "." in filename:
                return filename.rsplit(".", 1)[1].lower()
        if self.original_filename and self.original_filename.lower() in {"ppt", "pptx"}:
            return self.original_filename.lower()
        return ""

    @property
    def can_preview_inline(self):
        return self.effective_file_type in {"pdf", "png", "jpg", "jpeg", "ppt", "pptx", "doc", "docx", "xls", "xlsx"}

    @property
    def is_image(self):
        return self.effective_file_type in {"png", "jpg", "jpeg"}

    @property
    def is_powerpoint(self):
        return self.effective_file_type in {"ppt", "pptx"}

    @property
    def is_fileview_document(self):
        return self.effective_file_type in {"pdf", "ppt", "pptx", "doc", "docx", "xls", "xlsx"}


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), index=True)
    username_snapshot = db.Column(db.String(50), index=True)
    display_name_snapshot = db.Column(db.String(100))
    role_snapshot = db.Column(db.String(30))
    action = db.Column(db.String(50), nullable=False, index=True)
    target_type = db.Column(db.String(50), index=True)
    target_id = db.Column(db.Integer, index=True)
    target_label = db.Column(db.String(255), index=True)
    request_method = db.Column(db.String(10))
    request_path = db.Column(db.String(255))
    ip_address = db.Column(db.String(80))
    user_agent = db.Column(db.String(255))
    metadata_json = db.Column("metadata", JSON)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = db.relationship("User", backref="audit_logs")


class MeetingMinutes(db.Model):
    __tablename__ = "meeting_minutes"

    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), unique=True, nullable=False)
    summary = db.Column(db.Text)
    decisions = db.Column(db.Text)
    action_items = db.Column(db.Text)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class CopilotProposal(db.Model):
    __tablename__ = "copilot_proposals"

    id = db.Column(db.Integer, primary_key=True)
    proposal_type = db.Column(db.String(50), nullable=False)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meetings.id"), nullable=False)
    topic_id = db.Column(db.Integer, db.ForeignKey("topics.id"))
    payload = db.Column(JSON, nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    applied_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    applied_at = db.Column(db.DateTime)

    meeting = db.relationship("Meeting", backref="copilot_proposals")
    topic = db.relationship("Topic", backref="copilot_proposals")

    def apply(self, user_id):
        if self.status != "pending":
            return

        if self.proposal_type == "minutes_update":
            minutes = self.meeting.minutes or MeetingMinutes(
                meeting_id=self.meeting_id,
                created_by=user_id,
            )
            minutes.summary = self.payload.get("summary", "")
            minutes.decisions = self.payload.get("decisions", "")
            minutes.action_items = self.payload.get("action_items", "")
            if self.payload.get("meeting_status"):
                self.meeting.status = self.payload["meeting_status"]
            db.session.add(minutes)
        elif self.proposal_type == "topic_create":
            topic = Topic(
                meeting_id=self.meeting_id,
                title=self.payload.get("title", "").strip(),
                category=normalize_topic_category(self.payload.get("category")),
                plan_version=normalize_plan_version(self.payload.get("plan_version")),
                owner=self.payload.get("owner", "").strip(),
                duration_minutes=normalize_topic_duration(self.payload.get("duration_minutes")),
                present_order=int(self.payload.get("present_order") or self.meeting.topics.count() + 1),
                status=self.payload.get("status", "pending"),
                background=self.payload.get("background", "").strip(),
                purpose=self.payload.get("purpose", "").strip(),
            )
            db.session.add(topic)
        elif self.proposal_type == "topic_update" and self.topic:
            self.topic.title = self.payload.get("title", self.topic.title).strip()
            self.topic.category = normalize_topic_category(self.payload.get("category", self.topic.category))
            self.topic.plan_version = normalize_plan_version(
                self.payload.get("plan_version", self.topic.plan_version)
            )
            self.topic.owner = self.payload.get("owner", self.topic.owner or "").strip()
            self.topic.duration_minutes = normalize_topic_duration(
                self.payload.get("duration_minutes", self.topic.duration_minutes)
            )
            self.topic.present_order = int(self.payload.get("present_order") or self.topic.present_order)
            self.topic.status = self.payload.get("status", self.topic.status)
            self.topic.background = self.payload.get("background", self.topic.background or "").strip()
            self.topic.purpose = self.payload.get("purpose", self.topic.purpose or "").strip()

        self.status = "applied"
        self.applied_by = user_id
        self.applied_at = datetime.utcnow()



class AIKnowHowCategory(db.Model):
    __tablename__ = "ai_knowhow_categories"

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(10), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    creator = db.relationship("User", foreign_keys=[created_by])
    updater = db.relationship("User", foreign_keys=[updated_by])
    entries = db.relationship("AIKnowHow", back_populates="category", lazy="dynamic")


class AIKnowHow(db.Model):
    __tablename__ = "ai_knowhow"

    id = db.Column(db.Integer, primary_key=True)
    scope = db.Column(db.String(10), nullable=False, index=True)
    category_id = db.Column(db.Integer, db.ForeignKey("ai_knowhow_categories.id"), index=True)
    content = db.Column(db.Text, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    updated_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    creator = db.relationship("User", foreign_keys=[created_by])
    updater = db.relationship("User", foreign_keys=[updated_by])
    category = db.relationship("AIKnowHowCategory", back_populates="entries")
