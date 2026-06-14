import json
import time

import requests


LARK_CONFIG_KEY = "lark"
DEFAULT_LARK_CONFIG = {
    "enabled": False,
    "app_id": "",
    "app_secret": "",
    "reminder_days": 3,
    "api_base": "https://open.feishu.cn/open-apis",
}


class LarkAPIError(RuntimeError):
    pass


def normalize_lark_config(value):
    config = dict(DEFAULT_LARK_CONFIG)
    if isinstance(value, dict):
        config.update({key: value.get(key, config[key]) for key in config})
    config["enabled"] = bool(config.get("enabled"))
    config["app_id"] = (config.get("app_id") or "").strip()
    config["app_secret"] = (config.get("app_secret") or "").strip()
    config["api_base"] = (config.get("api_base") or DEFAULT_LARK_CONFIG["api_base"]).rstrip("/")
    try:
        config["reminder_days"] = max(1, min(30, int(config.get("reminder_days") or 3)))
    except (TypeError, ValueError):
        config["reminder_days"] = 3
    return config


def lark_secret_status(config):
    return "已配置" if normalize_lark_config(config).get("app_secret") else "未配置"


class LarkClient:
    def __init__(self, config, session=None, timeout=10):
        self.config = normalize_lark_config(config)
        self.session = session or requests.Session()
        self.timeout = timeout
        self._tenant_access_token = None
        self._token_expires_at = 0

    def tenant_access_token(self):
        if self._tenant_access_token and self._token_expires_at > time.time() + 60:
            return self._tenant_access_token
        payload = {
            "app_id": self.config["app_id"],
            "app_secret": self.config["app_secret"],
        }
        data = self._post_json("/auth/v3/tenant_access_token/internal", payload, auth=False)
        token = data.get("tenant_access_token")
        if not token:
            raise LarkAPIError("飞书未返回 tenant_access_token")
        self._tenant_access_token = token
        self._token_expires_at = time.time() + int(data.get("expire") or 7200)
        return token

    def batch_get_user_ids(self, emails=None):
        emails = [item for item in dict.fromkeys(emails or []) if item]
        if not emails:
            return {}
        data = self._post_json(
            "/contact/v3/users/batch_get_id",
            {"emails": emails, "include_resigned": False},
        )
        user_list = data.get("user_list") or data.get("items") or []
        mapping = {}
        for item in user_list:
            user_info = {
                "open_id": item.get("open_id") or item.get("open_user_id"),
                "user_id": item.get("user_id"),
            }
            for email in _as_list(item.get("email") or item.get("emails")):
                if email:
                    mapping[email.strip().lower()] = user_info
        return mapping

    def send_text(self, open_id, text):
        content = json.dumps({"text": text}, ensure_ascii=False)
        return self._post_json(
            "/im/v1/messages?receive_id_type=open_id",
            {"receive_id": open_id, "msg_type": "text", "content": content},
        )

    def _post_json(self, path, payload, auth=True):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if auth:
            headers["Authorization"] = f"Bearer {self.tenant_access_token()}"
        response = self.session.post(
            f"{self.config['api_base']}{path}",
            json=payload,
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") not in (0, None):
            raise LarkAPIError(body.get("msg") or body.get("error") or "飞书接口调用失败")
        return body.get("data") or body


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
