import shutil
import sys
from pathlib import Path


POWERPOINT_FORMAT_PDF = 32
POWERPOINT_EXTENSIONS = {"ppt", "pptx"}


class PowerPointPreviewError(RuntimeError):
    pass


def build_powerpoint_pdf_preview(source_path, output_root, effective_type="", converter=None):
    source_path = Path(source_path)
    if not source_path.exists():
        raise PowerPointPreviewError("原始 PPT 文件不存在，无法生成预览。")

    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    conversion_source = source_for_powerpoint(source_path, output_root, effective_type)
    pdf_path = output_root / f"{conversion_source.stem}.pdf"
    if pdf_path.exists() and pdf_path.stat().st_mtime >= source_path.stat().st_mtime:
        return pdf_path

    converter = converter or convert_with_powerpoint_com
    try:
        converted_path = Path(converter(conversion_source, output_root))
    except PowerPointPreviewError:
        raise
    except Exception as exc:
        raise PowerPointPreviewError(f"PowerPoint 转 PDF 失败：{exc}") from exc

    if not converted_path.exists():
        raise PowerPointPreviewError("PowerPoint 转 PDF 失败：未生成 PDF 文件。")
    return converted_path


def source_for_powerpoint(source_path, output_root, effective_type=""):
    source_path = Path(source_path)
    suffix = source_path.suffix.lower().lstrip(".")
    if suffix in POWERPOINT_EXTENSIONS:
        return source_path

    effective_type = (effective_type or "").lower()
    extension = effective_type if effective_type in POWERPOINT_EXTENSIONS else "pptx"
    copied_source = Path(output_root) / f"{source_path.stem or 'presentation'}.{extension}"
    if not copied_source.exists() or copied_source.stat().st_mtime < source_path.stat().st_mtime:
        shutil.copy2(source_path, copied_source)
    return copied_source


def convert_with_powerpoint_com(source_path, output_root):
    if sys.platform != "win32":
        raise PowerPointPreviewError("当前系统不支持 Microsoft PowerPoint COM 转换。")

    try:
        import pythoncom
        import win32com.client
    except ImportError as exc:
        raise PowerPointPreviewError("未安装 pywin32，无法调用本机 PowerPoint。") from exc

    source_path = Path(source_path).resolve()
    output_root = Path(output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    output_path = output_root / f"{source_path.stem}.pdf"

    powerpoint = None
    presentation = None
    pythoncom.CoInitialize()
    try:
        powerpoint = win32com.client.DispatchEx("PowerPoint.Application")
        presentation = powerpoint.Presentations.Open(
            str(source_path),
            ReadOnly=True,
            Untitled=False,
            WithWindow=False,
        )
        presentation.SaveAs(str(output_path), POWERPOINT_FORMAT_PDF)
    finally:
        if presentation is not None:
            presentation.Close()
        if powerpoint is not None:
            powerpoint.Quit()
        pythoncom.CoUninitialize()

    return output_path
