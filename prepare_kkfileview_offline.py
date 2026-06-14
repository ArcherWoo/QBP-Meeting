import hashlib
import json
import os
import re
import urllib.error
from urllib.parse import urlparse
import urllib.request
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
OFFLINE_ROOT = PROJECT_ROOT / "kkfileview"
DOWNLOADS_DIR = OFFLINE_ROOT / "downloads"
MANIFEST_PATH = OFFLINE_ROOT / "manifest.json"
TEMURIN_JRE_URL = (
    "https://github.com/adoptium/temurin17-binaries/releases/download/"
    "jdk-17.0.19%2B10/OpenJDK17U-jre_x64_windows_hotspot_17.0.19_10.zip"
)
GITHUB_RELEASES_API = "https://api.github.com/repos/kekingcn/kkFileView/releases"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "QBP-MGMT-offline-bundler/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def download(url, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}", flush=True)
    request = urllib.request.Request(url, headers={"User-Agent": "QBP-MGMT-offline-bundler/1.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        filename = response.headers.get_filename()
        if filename and target.name == "__auto__":
            target = target.with_name(filename)
        partial = target.with_suffix(target.suffix + ".part")
        if partial.exists():
            partial.unlink()
        with partial.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
        if partial.stat().st_size == 0:
            partial.unlink(missing_ok=True)
            raise RuntimeError(f"下载失败，文件为空：{url}")
        partial.replace(target)
    return target


def find_public_kkfileview_asset():
    explicit = os.environ.get("KKFILEVIEW_DOWNLOAD_URL")
    if explicit:
        return explicit
    releases = read_json(GITHUB_RELEASES_API)
    for release in releases:
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if re.search(r"(win|windows).*\.(zip|7z)$", name, flags=re.I):
                return asset.get("browser_download_url")
    return None


def main():
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "kkfileview": {
            "version": "4.4.0",
            "filename": "",
            "sha256": "",
            "source": "",
            "office_runtime": "bundled-with-kkfileview-windows-package",
        },
        "jre": {
            "version": "17.0.19+10",
            "filename": "",
            "sha256": "",
            "source": TEMURIN_JRE_URL,
        },
    }

    jre_path = download(TEMURIN_JRE_URL, DOWNLOADS_DIR / "__auto__")
    manifest["jre"]["filename"] = jre_path.name
    manifest["jre"]["sha256"] = sha256_file(jre_path)

    kk_url = find_public_kkfileview_asset()
    if not kk_url:
        MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        raise SystemExit(
            "未找到公开可下载的 kkFileView Windows zip。"
            "请设置 KKFILEVIEW_DOWNLOAD_URL 后重试，或手动把官方 Windows 包放入 kkfileview/downloads/。"
        )

    kk_name = Path(urlparse(kk_url).path).name or "kkFileView-windows.zip"
    kk_path = download(kk_url, DOWNLOADS_DIR / kk_name)
    manifest["kkfileview"]["filename"] = kk_path.name
    manifest["kkfileview"]["sha256"] = sha256_file(kk_path)
    manifest["kkfileview"]["source"] = kk_url
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Offline manifest written: {MANIFEST_PATH}")


if __name__ == "__main__":
    main()
