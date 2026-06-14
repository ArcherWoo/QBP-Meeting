import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from flask import current_app


ATTACHMENT_INTENT_KEYWORDS = {
    "附件",
    "材料",
    "文件",
    "文档",
    "ppt",
    "pptx",
    "pdf",
    "word",
    "doc",
    "docx",
    "excel",
    "xlsx",
    "deck",
    "presentation",
    "slides",
    "slide",
}

MAX_ATTACHMENT_TEXT_CHARS = 6000
MAX_ATTACHMENT_CONTEXT_FILES = 4


def attachment_path(attachment):
    meeting_bucket = attachment.topic.meeting_id or attachment.topic.requested_meeting_id or "draft"
    return (
        Path(current_app.config["UPLOAD_FOLDER"])
        / str(meeting_bucket)
        / str(attachment.topic_id)
        / attachment.stored_filename
    )


def message_requests_attachment_text(message):
    lowered = (message or "").lower()
    return any(keyword.lower() in lowered for keyword in ATTACHMENT_INTENT_KEYWORDS)


def attachment_matches_message(attachment, message):
    lowered = (message or "").lower()
    filename = (attachment.original_filename or "").lower()
    if filename and filename in lowered:
        return True
    filename_tokens = [token for token in re.split(r"[\s._\-()]+", filename) if len(token) >= 4]
    return any(token in lowered for token in filename_tokens)


def attachment_text_context(attachment, max_chars=MAX_ATTACHMENT_TEXT_CHARS):
    path = attachment_path(attachment)
    text, warning = extract_attachment_text(path, attachment.effective_file_type)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[已截断]"
    return {
        "attachment_id": attachment.id,
        "filename": attachment.original_filename,
        "file_type": attachment.effective_file_type,
        "file_size": attachment.file_size,
        "topic_id": attachment.topic_id,
        "topic_title": attachment.topic.title,
        "meeting_no": attachment.topic.meeting.meeting_no,
        "text": text,
        "warning": warning,
    }


def extract_attachment_text(path, file_type):
    file_type = (file_type or "").lower()
    path = Path(path)
    if not path.exists():
        return "", "附件文件不存在，无法抽取正文。"
    try:
        if file_type in {"pptx"}:
            return extract_pptx_text(path), ""
        if file_type in {"docx"}:
            return extract_docx_text(path), ""
        if file_type in {"xlsx"}:
            return extract_xlsx_text(path), ""
        if file_type == "pdf":
            return extract_pdf_text(path), ""
        if file_type in {"ppt", "doc", "xls"}:
            return "", "旧版 Office 格式暂不支持正文抽取，请转为 pptx/docx/xlsx 后再试。"
        return "", "该附件类型暂不支持正文抽取。"
    except Exception as exc:
        return "", f"附件正文抽取失败：{exc}"


def extract_pptx_text(path):
    entries = []
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for index, name in enumerate(slide_names, start=1):
            text = text_from_xml(archive.read(name), text_tag_suffix="}t")
            if text:
                entries.append(f"Slide {index}: {text}")
    return "\n".join(entries).strip()


def extract_docx_text(path):
    with zipfile.ZipFile(path) as archive:
        if "word/document.xml" not in archive.namelist():
            return ""
        return text_from_xml(archive.read("word/document.xml"), text_tag_suffix="}t")


def extract_xlsx_text(path):
    values = []
    with zipfile.ZipFile(path) as archive:
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.iter():
                if item.tag.endswith("}t") and item.text:
                    shared_strings.append(item.text)
        for name in sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")):
            root = ElementTree.fromstring(archive.read(name))
            for cell in root.iter():
                if not cell.tag.endswith("}c"):
                    continue
                value_node = next((child for child in cell if child.tag.endswith("}v")), None)
                if value_node is None or value_node.text is None:
                    continue
                if cell.attrib.get("t") == "s":
                    try:
                        values.append(shared_strings[int(value_node.text)])
                    except (ValueError, IndexError):
                        continue
                else:
                    values.append(value_node.text)
    return "\n".join(value for value in values if value).strip()


def extract_pdf_text(path):
    try:
        from PyPDF2 import PdfReader
    except ImportError:
        return "", "PDF 正文抽取需要安装 PyPDF2。"

    reader = PdfReader(str(path))
    pages = []
    for index, page in enumerate(reader.pages[:20], start=1):
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(f"Page {index}: {page_text.strip()}")
    return "\n".join(pages).strip()


def text_from_xml(raw_xml, text_tag_suffix):
    root = ElementTree.fromstring(raw_xml)
    parts = [node.text for node in root.iter() if node.tag.endswith(text_tag_suffix) and node.text]
    return " ".join(part.strip() for part in parts if part and part.strip())
