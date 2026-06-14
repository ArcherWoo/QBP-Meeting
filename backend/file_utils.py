import uuid
from pathlib import Path

from werkzeug.utils import secure_filename


def extension_for(filename):
    return filename.rsplit(".", 1)[1].lower() if "." in filename else ""


def is_allowed(filename, allowed_extensions):
    return extension_for(filename) in allowed_extensions


def save_topic_file(file_storage, upload_root, allowed_extensions):
    if not file_storage or not file_storage.filename:
        return None, "请选择要上传的文件"
    if not is_allowed(file_storage.filename, allowed_extensions):
        allowed = ", ".join(sorted(allowed_extensions))
        return None, f"不支持的文件类型，仅支持：{allowed}"

    original = secure_filename(file_storage.filename)
    ext = extension_for(original)
    stored = f"{uuid.uuid4().hex}.{ext}"
    upload_root = Path(upload_root)
    upload_root.mkdir(parents=True, exist_ok=True)
    file_path = upload_root / stored
    file_storage.save(file_path)
    return {
        "original_filename": original,
        "stored_filename": stored,
        "file_type": ext,
        "file_size": file_path.stat().st_size,
        "file_path": file_path,
    }, None
