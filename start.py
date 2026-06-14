import argparse
from dataclasses import dataclass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5008
DEFAULT_KK_PORT = 8012
DEFAULT_KK_START_TIMEOUT = 45
OFFLINE_KK_DIR = "kkfileview"
INSTALL_MANIFEST = "installed_manifest.json"


@dataclass
class KkRuntime:
    runtime_dir: Path
    installed: bool


def venv_python_path(project_root=PROJECT_ROOT, platform_name=os.name):
    scripts_dir = "Scripts" if platform_name == "nt" else "bin"
    executable = "python.exe" if platform_name == "nt" else "python"
    return Path(project_root) / "venv" / scripts_dir / executable


def login_url(host=DEFAULT_HOST, port=DEFAULT_PORT):
    return f"http://{host}:{port}/auth/login"


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def build_parser():
    parser = argparse.ArgumentParser(description="Start or manage QBP Meeting MGMT.")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind/check. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port to bind/check. Default: 5008")
    parser.add_argument("--public-host", help="Host/IP that browsers and kkFileView use to reach this server.")
    parser.add_argument("--kk-port", type=int, default=DEFAULT_KK_PORT, help="kkFileView port. Default: 8012")
    parser.add_argument(
        "--kk-start-timeout",
        type=int,
        default=DEFAULT_KK_START_TIMEOUT,
        help=f"Seconds to wait for kkFileView HTTP readiness. Default: {DEFAULT_KK_START_TIMEOUT}",
    )
    parser.add_argument("--no-kkfileview", action="store_true", help="Skip kkFileView install/start management.")
    parser.add_argument("--status", action="store_true", help="Only show whether the service is running.")
    parser.add_argument("--stop", action="store_true", help="Stop the process listening on the configured port.")
    return parser


def is_port_open(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def is_http_ready(url, timeout=1.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code < 500
    except (OSError, urllib.error.URLError):
        return False


def listening_pid(port):
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
            try:
                return int(parts[-1])
            except ValueError:
                return None
    return None


def unique_pids(values):
    pids = []
    for value in values:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in pids:
            pids.append(pid)
    return pids


def project_app_pids():
    if os.name != "nt":
        return []
    try:
        script = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*QBP_MGMT*backend.app*' } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    return unique_pids(result.stdout.split())


def describe_existing_server(host, port, pid=None):
    pid_text = f" (PID {pid})" if pid else ""
    return (
        f"Service already running{pid_text}: {login_url(host, port)}\n"
        "The terminal can safely exit; the service is still running in the background.\n"
        f"如需停止，请执行：python start.py --stop --port {port}"
    )


def print_status(host, port, kk_port=DEFAULT_KK_PORT, include_kk=True):
    pid = listening_pid(port)
    ok = True
    if is_port_open(host, port):
        print(describe_existing_server(host, port, pid))
    else:
        print(f"QBP 服务未运行（{host}:{port}）。")
        ok = False
    if include_kk:
        kk_pid = listening_pid(kk_port)
        if is_port_open(host, kk_port):
            print(f"kkFileView already running{f' (PID {kk_pid})' if kk_pid else ''}: http://{host}:{kk_port}")
        else:
            print(f"kkFileView 未运行（{host}:{kk_port}）。")
            ok = False
    return 0 if ok else 1


def stop_server(host, port, kk_port=DEFAULT_KK_PORT, include_kk=True):
    pids = unique_pids([listening_pid(port), *project_app_pids(), listening_pid(kk_port) if include_kk else None])
    if not pids:
        print(f"未检测到运行中的服务（{host}:{port}）。")
        return 0
    print(f"正在停止服务（PID {', '.join(str(pid) for pid in pids)}）...")
    for pid in pids:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
            output = f"{result.stdout}\n{result.stderr}".strip()
            if result.returncode != 0 and output and "not found" not in output.lower():
                print(output)
        else:
            subprocess.run(["kill", str(pid)], check=False)
    print("服务已停止。")
    return 0


def terminate_process_tree(process, label="process", timeout=8):
    if process is None:
        return
    pid = getattr(process, "pid", None)
    if os.name == "nt" and pid:
        result = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        output = f"{result.stdout}\n{result.stderr}".strip()
        if result.returncode != 0 and output and "not found" not in output.lower():
            print(f"{label} 停止失败：{output}")
        return

    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"{label} 停止超时，请手动检查残留进程。")


def run_command(command, cwd=PROJECT_ROOT):
    subprocess.check_call(command, cwd=str(cwd))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def file_sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_download(downloads_dir, item):
    path = downloads_dir / item["filename"]
    if not path.exists():
        raise FileNotFoundError(f"缺少 kkFileView 离线文件：{path}")
    expected = item.get("sha256")
    if expected and expected != file_sha256(path):
        raise RuntimeError(f"离线文件校验失败：{path.name}")
    return path


def zip_extract_path(path, platform_name=os.name):
    resolved = str(Path(path).resolve())
    if platform_name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    if resolved.startswith("\\\\"):
        return "\\\\?\\UNC\\" + resolved.lstrip("\\")
    return "\\\\?\\" + resolved


def extract_zip(source, target):
    extract_target = zip_extract_path(target)
    if target.exists():
        shutil.rmtree(extract_target)
    os.makedirs(extract_target, exist_ok=True)
    with zipfile.ZipFile(source) as archive:
        archive.extractall(extract_target)


def ensure_kkfileview_runtime(project_root=PROJECT_ROOT):
    offline_root = Path(project_root) / OFFLINE_KK_DIR
    manifest_path = offline_root / "manifest.json"
    runtime_dir = offline_root / "runtime"
    installed_manifest = runtime_dir / INSTALL_MANIFEST
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"缺少 {offline_root / 'manifest.json'}。请先在联网环境运行 prepare_kkfileview_offline.py。"
        )

    manifest = read_json(manifest_path)
    if installed_manifest.exists() and read_json(installed_manifest) == manifest:
        print("[3/6] kkFileView 已安装，跳过。")
        return KkRuntime(runtime_dir=runtime_dir, installed=True)

    downloads_dir = offline_root / "downloads"
    kk_zip = validate_download(downloads_dir, manifest["kkfileview"])
    jre_zip = validate_download(downloads_dir, manifest["jre"])
    print("[3/6] 正在安装 kkFileView 离线运行环境...")
    runtime_dir.mkdir(parents=True, exist_ok=True)
    extract_zip(jre_zip, runtime_dir / "jre")
    extract_zip(kk_zip, runtime_dir / "kkfileview")
    installed_manifest.write_text(json_dumps(manifest), encoding="utf-8")
    print("      kkFileView 离线运行环境安装完成。")
    return KkRuntime(runtime_dir=runtime_dir, installed=False)


def find_java_executable(runtime_dir):
    candidates = list((runtime_dir / "jre").glob("**/bin/java.exe")) + list((runtime_dir / "jre").glob("**/bin/java"))
    if candidates:
        return candidates[0]
    return "java.exe" if os.name == "nt" else "java"


def find_kkfileview_jar(runtime_dir):
    candidates = [path for path in (runtime_dir / "kkfileview").glob("**/*.jar") if "kk" in path.name.lower()]
    if not candidates:
        candidates = list((runtime_dir / "kkfileview").glob("**/*.jar"))
    if not candidates:
        raise FileNotFoundError("kkFileView 离线包中没有找到可启动 jar。")
    return candidates[0]


def qbp_environment(public_host, port, kk_port, kkfileview_enabled=True):
    host = public_host or DEFAULT_HOST
    env = os.environ.copy()
    env.setdefault("QBP_PUBLIC_BASE_URL", f"http://{host}:{port}")
    env.setdefault("QBP_FILEVIEW_BASE_URL", env["QBP_PUBLIC_BASE_URL"])
    env.setdefault("KKFILEVIEW_BASE_URL", f"http://{host}:{kk_port}")
    env["KKFILEVIEW_ENABLED"] = "1" if kkfileview_enabled else "0"
    return env


def print_startup_diagnostics(host, public_host, port, kk_port, env, kkfileview_enabled=True):
    print("[config] 启动诊断")
    print(f"      绑定地址: {host}:{port}")
    print(f"      对外访问: {env.get('QBP_PUBLIC_BASE_URL')}")
    print(f"      文件回拉: {env.get('QBP_FILEVIEW_BASE_URL')}")
    print(
        f"      kkFileView: {env.get('KKFILEVIEW_BASE_URL') if kkfileview_enabled else 'disabled'}"
    )
    print("      预览链路: Browser -> kkFileView -> QBP fileview-source")
    print("      文档解密: enabled for pdf, doc, docx, ppt, pptx, xls, xlsx")
    print(f"      kkFileView 端口: {kk_port}")
    print(f"      kkFileView 日志: {PROJECT_ROOT / 'kkfileview' / 'logs' / 'kkfileview.log'}")


def bundled_office_home(runtime_dir):
    runtime = Path(runtime_dir)
    for soffice in runtime.glob("kkfileview/**/LibreOfficePortable/App/libreoffice/program/soffice.exe"):
        return soffice.parent.parent
    for soffice in runtime.glob("kkfileview/**/LibreOfficePortable/App/libreoffice/program/soffice"):
        return soffice.parent.parent
    for soffice in runtime.glob("kkfileview/**/program/soffice.exe"):
        return soffice.parent.parent
    for soffice in runtime.glob("kkfileview/**/program/soffice"):
        return soffice.parent.parent
    return None


def kkfileview_environment(host=DEFAULT_HOST, public_host=None, runtime_dir=None):
    env = os.environ.copy()
    trust_hosts = ["127.0.0.1", "localhost"]
    for value in (host, public_host):
        if value and value != "0.0.0.0" and value not in trust_hosts:
            trust_hosts.append(value)
    env["KK_TRUST_HOST"] = ",".join(trust_hosts)
    if runtime_dir:
        office_home = bundled_office_home(runtime_dir)
        if office_home:
            env["KK_OFFICE_HOME"] = str(office_home)
    return env


def ensure_venv():
    python_path = venv_python_path()
    if python_path.exists():
        print("[1/6] 虚拟环境已存在，跳过创建。")
        return python_path

    print("[1/6] 正在创建虚拟环境...")
    run_command([sys.executable, "-m", "venv", "venv"])
    print("      虚拟环境创建完成。")
    return python_path


def install_dependencies(python_path):
    wheels_dir = PROJECT_ROOT / "wheels"
    online_command = [str(python_path), "-m", "pip", "install", "-q", "-r", "requirements.txt"]
    if wheels_dir.is_dir():
        print("[2/6] 正在安装依赖（离线模式）...")
        try:
            run_command(
                [
                    str(python_path),
                    "-m",
                    "pip",
                    "install",
                    "-q",
                    "--no-index",
                    "--find-links",
                    str(wheels_dir),
                    "-r",
                    "requirements.txt",
                ]
            )
        except subprocess.CalledProcessError:
            print("      离线依赖安装失败，可能是本地 wheels 与当前 Python 版本不匹配。")
            print("      切换为联网模式安装依赖...")
            run_command(online_command)
    else:
        print("[2/6] 正在安装依赖（联网模式）...")
        run_command(online_command)
    print("      依赖安装完成。")


def init_database(python_path):
    print("[4/6] 正在初始化数据库...")
    run_command([str(python_path), "-m", "backend.init_db"])


def start_kkfileview(runtime_dir, host=DEFAULT_HOST, public_host=None, kk_port=DEFAULT_KK_PORT):
    if is_port_open(host, kk_port):
        pid = listening_pid(kk_port)
        print(f"[5/6] kkFileView already running{f' (PID {pid})' if pid else ''}: http://{public_host or host}:{kk_port}")
        return None

    logs_dir = Path(runtime_dir).parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    java_executable = find_java_executable(Path(runtime_dir))
    kk_jar = find_kkfileview_jar(Path(runtime_dir))
    log_file = (logs_dir / "kkfileview.log").open("a", encoding="utf-8")
    print(f"[5/6] 正在启动 kkFileView，地址：http://{public_host or host}:{kk_port}")
    env = kkfileview_environment(host, public_host, runtime_dir)
    if env.get("KK_OFFICE_HOME"):
        print(f"      LibreOffice: {env['KK_OFFICE_HOME']}")
    return subprocess.Popen(
        [
            str(java_executable),
            f"-Dserver.port={kk_port}",
            f"-Dfile.dir={Path(runtime_dir) / 'file-cache'}",
            "-jar",
            str(kk_jar),
        ],
        cwd=str(kk_jar.parent),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
    )


def warn_kkfileview_fallback(error):
    print(f"[3/6] kkFileView 不可用：{error}")
    print("      已回退为普通预览/下载模式，QBP 网站会继续正常启动。")


def start_server(python_path, host=DEFAULT_HOST, port=DEFAULT_PORT, env=None):
    if is_port_open(host, port):
        pid = listening_pid(port)
        print(f"[6/6] {describe_existing_server(host, port, pid).splitlines()[0]}")
        return None

    print(f"[6/6] 正在启动服务，地址：{login_url(host, port)}")
    return subprocess.Popen(
        [str(python_path), "-m", "backend.app"],
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def wait_for_server(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.4)
    return False


def ensure_kkfileview_started(process, host=DEFAULT_HOST, kk_port=DEFAULT_KK_PORT, timeout=DEFAULT_KK_START_TIMEOUT):
    if process is None:
        return
    deadline = time.time() + timeout
    health_url = f"http://{host}:{kk_port}"
    while time.time() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"kkFileView 进程已退出，退出码：{exit_code}")
        if is_port_open(host, kk_port) and is_http_ready(health_url):
            print(f"      kkFileView 已就绪：{health_url}")
            return
        time.sleep(0.4)
    raise RuntimeError(f"kkFileView 启动超时（{health_url}），请检查 kkfileview/logs/kkfileview.log")


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.status:
        return print_status(args.host, args.port, args.kk_port, not args.no_kkfileview)
    if args.stop:
        return stop_server(args.host, args.port, args.kk_port, not args.no_kkfileview)

    os.chdir(PROJECT_ROOT)
    public_host = args.public_host or ("127.0.0.1" if args.host == "0.0.0.0" else args.host)
    python_path = ensure_venv()
    install_dependencies(python_path)
    kk_process = None
    kk_runtime = None
    kkfileview_enabled = not args.no_kkfileview
    if args.no_kkfileview:
        print("[3/6] 已跳过 kkFileView。")
    else:
        try:
            kk_runtime = ensure_kkfileview_runtime()
        except Exception as exc:
            kkfileview_enabled = False
            warn_kkfileview_fallback(exc)
    init_database(python_path)
    if kk_runtime is not None:
        try:
            kk_process = start_kkfileview(kk_runtime.runtime_dir, args.host, public_host, args.kk_port)
            ensure_kkfileview_started(kk_process, args.host, args.kk_port, timeout=args.kk_start_timeout)
        except Exception as exc:
            kkfileview_enabled = False
            warn_kkfileview_fallback(exc)
    qbp_env = qbp_environment(public_host, args.port, args.kk_port, kkfileview_enabled=kkfileview_enabled)
    print_startup_diagnostics(args.host, public_host, args.port, args.kk_port, qbp_env, kkfileview_enabled)
    process = start_server(
        python_path,
        args.host,
        args.port,
        qbp_env,
    )

    if not wait_for_server(args.host, args.port):
        print("服务启动超时，请检查上方输出信息。")
        return 1

    url = login_url(args.host, args.port)
    print("")
    print("=" * 40)
    print("  QBP Meeting MGMT is ready.")
    print(f"  URL: {url}")
    print("  Login: admin / admin123")
    if kkfileview_enabled:
        print(f"  kkFileView: http://{public_host}:{args.kk_port}")
    else:
        print("  kkFileView: unavailable, using direct preview/download fallback")
    print("=" * 40)
    webbrowser.open(url)

    if process is not None:
        print("")
        print("  按 Ctrl+C 停止服务。")
        try:
            process.wait()
        except KeyboardInterrupt:
            print("")
            print("正在停止服务...")
            terminate_process_tree(process, "QBP Meeting")
            terminate_process_tree(kk_process, "kkFileView")
            print("服务已停止。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
