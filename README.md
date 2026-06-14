# QBP Meeting MGMT

QBP Meeting MGMT is a Flask/Jinja application for Quarterly Business Review meeting management. It supports meetings, independent topics, attachments, in-page previews, meeting minutes, QBP review workflows, and a simple admin login.

## Quick Start

```powershell
python start.py
```

The start script creates a local virtual environment, installs dependencies, initializes the SQLite database, starts the Flask app, and opens the login page.

For offline installation, download `wheels.zip` from GitHub Releases and extract it into the project root so the `wheels/` directory sits next to `requirements.txt`.

Default login:

```text
admin / admin123
```

Default local URL:

```text
http://127.0.0.1:5008/auth/login
```

## Notes

- Local development uses SQLite under `data/db/`.
- Uploaded files are stored under `data/uploads/`.
- `data/`, `logs/`, virtual environments, caches, and `.env` files are ignored by Git.
- Offline dependency wheels are distributed as `wheels.zip` in GitHub Releases, not committed as individual wheel files.
- Office/PDF preview uses kkFileView when the offline bundle is installed and configured.
- AI Copilot uses Zhishu/Open WebUI through `ZHISHU_BASE_URL` and `ZHISHU_API_KEY`.
- To let Zhishu call QBP tools, configure an OpenAPI Tool Server in Zhishu with URL `QBP_PUBLIC_BASE_URL`, path `/copilot/tools/openapi.json`, bearer key `QBP_TOOL_SERVER_TOKEN`.

## Manual Commands

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
.\venv\Scripts\python.exe -m backend.init_db
.\venv\Scripts\python.exe -m backend.app
```
