from pathlib import Path
import subprocess

import start


def test_venv_python_path_is_platform_aware(tmp_path):
    assert start.venv_python_path(tmp_path, "nt") == tmp_path / "venv" / "Scripts" / "python.exe"
    assert start.venv_python_path(tmp_path, "posix") == tmp_path / "venv" / "bin" / "python"


def test_default_login_url_points_to_local_app():
    assert start.login_url("127.0.0.1", 5008) == "http://127.0.0.1:5008/auth/login"


def test_start_script_uses_project_root_for_relative_paths():
    assert start.PROJECT_ROOT == Path(start.__file__).resolve().parent


def test_parse_status_args_supports_status_and_stop():
    parser = start.build_parser()

    assert parser.parse_args(["--status"]).status is True
    assert parser.parse_args(["--stop"]).stop is True


def test_parse_kkfileview_args():
    parser = start.build_parser()

    args = parser.parse_args(["--public-host", "10.10.10.8", "--kk-port", "8012", "--no-kkfileview"])

    assert args.public_host == "10.10.10.8"
    assert args.kk_port == 8012
    assert args.no_kkfileview is True


def test_qbp_environment_uses_public_host_for_fileview(monkeypatch):
    monkeypatch.delenv("QBP_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("QBP_FILEVIEW_BASE_URL", raising=False)
    monkeypatch.delenv("KKFILEVIEW_BASE_URL", raising=False)

    env = start.qbp_environment("10.10.10.8", 5008, 8012)

    assert env["QBP_PUBLIC_BASE_URL"] == "http://10.10.10.8:5008"
    assert env["QBP_FILEVIEW_BASE_URL"] == "http://10.10.10.8:5008"
    assert env["KKFILEVIEW_BASE_URL"] == "http://10.10.10.8:8012"


def test_qbp_environment_preserves_env_file_preview_urls(monkeypatch):
    monkeypatch.setenv("QBP_PUBLIC_BASE_URL", "http://10.20.30.40:5008")
    monkeypatch.setenv("QBP_FILEVIEW_BASE_URL", "http://10.20.30.40:5008")
    monkeypatch.setenv("KKFILEVIEW_BASE_URL", "http://10.20.30.40:8012")

    env = start.qbp_environment("127.0.0.1", 5008, 8012)

    assert env["QBP_PUBLIC_BASE_URL"] == "http://10.20.30.40:5008"
    assert env["QBP_FILEVIEW_BASE_URL"] == "http://10.20.30.40:5008"
    assert env["KKFILEVIEW_BASE_URL"] == "http://10.20.30.40:8012"


def test_qbp_environment_can_disable_kkfileview():
    env = start.qbp_environment("10.10.10.8", 5008, 8012, kkfileview_enabled=False)

    assert env["KKFILEVIEW_ENABLED"] == "0"


def test_default_templates_use_localhost_not_private_machine_addresses():
    config_text = Path("backend/config.py").read_text(encoding="utf-8")
    env_example = Path(".env.example").read_text(encoding="utf-8")
    old_private_url = "http://172" + ".29.128.1"

    assert 'ZHISHU_BASE_URL = os.environ.get("ZHISHU_BASE_URL", "http://127.0.0.1:4173")' in config_text
    assert 'QBP_PUBLIC_BASE_URL = os.environ.get("QBP_PUBLIC_BASE_URL", "http://127.0.0.1:5008")' in config_text
    assert old_private_url not in config_text
    assert old_private_url not in env_example


def test_startup_diagnostics_print_preview_and_decryption_config(capsys):
    env = {
        "QBP_PUBLIC_BASE_URL": "http://10.20.30.40:5008",
        "QBP_FILEVIEW_BASE_URL": "http://10.20.30.40:5008",
        "KKFILEVIEW_BASE_URL": "http://10.20.30.40:8012",
        "KKFILEVIEW_ENABLED": "1",
    }

    start.print_startup_diagnostics("0.0.0.0", "10.20.30.40", 5008, 8012, env, kkfileview_enabled=True)

    output = capsys.readouterr().out
    assert "启动诊断" in output
    assert "绑定地址: 0.0.0.0:5008" in output
    assert "对外访问: http://10.20.30.40:5008" in output
    assert "文件回拉: http://10.20.30.40:5008" in output
    assert "kkFileView: http://10.20.30.40:8012" in output
    assert "预览链路: Browser -> kkFileView -> QBP fileview-source" in output
    assert "文档解密: enabled for pdf, doc, docx, ppt, pptx, xls, xlsx" in output


def test_install_dependencies_falls_back_to_online_when_offline_wheels_do_not_match(monkeypatch, tmp_path, capsys):
    wheels_dir = tmp_path / "wheels"
    wheels_dir.mkdir()
    commands = []

    monkeypatch.setattr(start, "PROJECT_ROOT", tmp_path)

    def fake_run_command(command, cwd=start.PROJECT_ROOT):
        commands.append(command)
        if "--no-index" in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(start, "run_command", fake_run_command)

    start.install_dependencies(Path("venv/Scripts/python.exe"))

    assert len(commands) == 2
    assert "--no-index" in commands[0]
    assert "--no-index" not in commands[1]
    assert commands[1][-2:] == ["-r", "requirements.txt"]
    output = capsys.readouterr().out
    assert "离线依赖安装失败" in output
    assert "切换为联网模式" in output


def test_kkfileview_environment_trusts_local_and_public_hosts():
    env = start.kkfileview_environment("0.0.0.0", "10.10.10.8")

    trusted = env["KK_TRUST_HOST"].split(",")
    assert "127.0.0.1" in trusted
    assert "localhost" in trusted
    assert "10.10.10.8" in trusted


def test_kkfileview_environment_points_to_bundled_libreoffice(tmp_path):
    runtime = tmp_path / "runtime"
    office_home = runtime / "kkfileview" / "kkFileView-5.0.0" / "LibreOfficePortable" / "App" / "libreoffice"
    (office_home / "program").mkdir(parents=True)
    (office_home / "program" / "soffice.exe").write_text("", encoding="utf-8")

    env = start.kkfileview_environment("127.0.0.1", runtime_dir=runtime)

    assert env["KK_OFFICE_HOME"] == str(office_home)


def test_start_kkfileview_passes_trust_host_environment(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    java = runtime / "jre" / "bin" / "java.exe"
    jar = runtime / "kkfileview" / "kkFileView.jar"
    office_home = runtime / "kkfileview" / "kkFileView-5.0.0" / "LibreOfficePortable" / "App" / "libreoffice"
    java.parent.mkdir(parents=True)
    jar.parent.mkdir(parents=True)
    (office_home / "program").mkdir(parents=True)
    java.write_text("", encoding="utf-8")
    jar.write_text("", encoding="utf-8")
    (office_home / "program" / "soffice.exe").write_text("", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(start, "is_port_open", lambda host, port: False)

    def fake_popen(command, cwd=None, stdout=None, stderr=None, env=None):
        captured["command"] = command
        captured["env"] = env
        return object()

    monkeypatch.setattr(start.subprocess, "Popen", fake_popen)

    start.start_kkfileview(runtime, host="0.0.0.0", public_host="10.10.10.8")

    trusted = captured["env"]["KK_TRUST_HOST"].split(",")
    assert "127.0.0.1" in trusted
    assert "localhost" in trusted
    assert "10.10.10.8" in trusted
    assert captured["env"]["KK_OFFICE_HOME"] == str(office_home)


def test_optional_kkfileview_runtime_failure_keeps_starting(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("venv/Scripts/python.exe"))
    monkeypatch.setattr(start, "install_dependencies", lambda python_path: calls.append(("install", python_path)))
    monkeypatch.setattr(start, "ensure_kkfileview_runtime", lambda: (_ for _ in ()).throw(FileNotFoundError("missing kk")))
    monkeypatch.setattr(start, "init_database", lambda python_path: calls.append(("init", python_path)))
    monkeypatch.setattr(start, "wait_for_server", lambda host, port: True)
    monkeypatch.setattr(start.webbrowser, "open", lambda url: calls.append(("open", url)))
    monkeypatch.setattr(start, "start_server", lambda python_path, host, port, env=None: calls.append(("server", env)) or None)

    result = start.main(["--host", "127.0.0.1", "--port", "5008"])

    assert result == 0
    server_env = [call[1] for call in calls if call[0] == "server"][0]
    assert server_env["KKFILEVIEW_ENABLED"] == "0"
    assert "回退为普通预览" in capsys.readouterr().out


def test_optional_kkfileview_start_failure_keeps_starting(monkeypatch, capsys):
    calls = []

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("venv/Scripts/python.exe"))
    monkeypatch.setattr(start, "install_dependencies", lambda python_path: None)
    monkeypatch.setattr(start, "ensure_kkfileview_runtime", lambda: start.KkRuntime(Path("kk-runtime"), installed=True))
    monkeypatch.setattr(start, "start_kkfileview", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("java missing")))
    monkeypatch.setattr(start, "init_database", lambda python_path: None)
    monkeypatch.setattr(start, "wait_for_server", lambda host, port: True)
    monkeypatch.setattr(start.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(start, "start_server", lambda python_path, host, port, env=None: calls.append(("server", env)) or None)

    result = start.main(["--host", "127.0.0.1", "--port", "5008"])

    assert result == 0
    server_env = calls[0][1]
    assert server_env["KKFILEVIEW_ENABLED"] == "0"
    assert "回退为普通预览" in capsys.readouterr().out


def test_optional_kkfileview_immediate_exit_keeps_starting(monkeypatch, capsys):
    calls = []

    class DeadProcess:
        def poll(self):
            return 1

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("venv/Scripts/python.exe"))
    monkeypatch.setattr(start, "install_dependencies", lambda python_path: None)
    monkeypatch.setattr(start, "ensure_kkfileview_runtime", lambda: start.KkRuntime(Path("kk-runtime"), installed=True))
    monkeypatch.setattr(start, "start_kkfileview", lambda *args, **kwargs: DeadProcess())
    monkeypatch.setattr(start, "init_database", lambda python_path: None)
    monkeypatch.setattr(start, "wait_for_server", lambda host, port: True)
    monkeypatch.setattr(start.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(start, "start_server", lambda python_path, host, port, env=None: calls.append(("server", env)) or None)

    result = start.main(["--host", "127.0.0.1", "--port", "5008"])

    assert result == 0
    server_env = calls[0][1]
    assert server_env["KKFILEVIEW_ENABLED"] == "0"
    assert "回退为普通预览" in capsys.readouterr().out


def test_ensure_kkfileview_started_waits_for_http_readiness(monkeypatch):
    calls = []

    class AliveProcess:
        def poll(self):
            return None

    monkeypatch.setattr(start, "is_port_open", lambda host, port: True)
    def fake_http_ready(url, timeout=1.0):
        calls.append(url)
        return len(calls) >= 2

    monkeypatch.setattr(start, "is_http_ready", fake_http_ready)
    monkeypatch.setattr(start.time, "sleep", lambda seconds: None)

    current = {"value": 0.0}

    def fake_time():
        current["value"] += 0.1
        return current["value"]

    monkeypatch.setattr(start.time, "time", fake_time)

    start.ensure_kkfileview_started(AliveProcess(), "127.0.0.1", 8012, timeout=2)

    assert calls == ["http://127.0.0.1:8012", "http://127.0.0.1:8012"]


def test_http_ready_treats_http_error_as_responsive(monkeypatch):
    class FakeHttpError(start.urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://127.0.0.1:8012", 404, "Not Found", hdrs=None, fp=None)

    def fake_urlopen(url, timeout=1.0):
        raise FakeHttpError()

    monkeypatch.setattr(start.urllib.request, "urlopen", fake_urlopen)

    assert start.is_http_ready("http://127.0.0.1:8012") is True


def test_main_ctrl_c_stops_qbp_and_kkfileview_process_trees(monkeypatch, capsys):
    calls = []

    class QbpProcess:
        def wait(self, timeout=None):
            if timeout is None:
                raise KeyboardInterrupt()
            return 0

        def terminate(self):
            calls.append(("direct-terminate", "qbp"))

    class KkProcess:
        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            calls.append(("direct-terminate", "kk"))

    qbp_process = QbpProcess()
    kk_process = KkProcess()

    monkeypatch.setattr(start, "ensure_venv", lambda: Path("venv/Scripts/python.exe"))
    monkeypatch.setattr(start, "install_dependencies", lambda python_path: None)
    monkeypatch.setattr(start, "ensure_kkfileview_runtime", lambda: start.KkRuntime(Path("kk-runtime"), installed=True))
    monkeypatch.setattr(start, "start_kkfileview", lambda *args, **kwargs: kk_process)
    monkeypatch.setattr(start, "ensure_kkfileview_started", lambda *args, **kwargs: None)
    monkeypatch.setattr(start, "init_database", lambda python_path: None)
    monkeypatch.setattr(start, "wait_for_server", lambda host, port: True)
    monkeypatch.setattr(start.webbrowser, "open", lambda url: None)
    monkeypatch.setattr(start, "start_server", lambda python_path, host, port, env=None: qbp_process)
    monkeypatch.setattr(
        start,
        "terminate_process_tree",
        lambda process, label="process", timeout=8: calls.append(("tree", label, process)),
        raising=False,
    )

    result = start.main(["--host", "127.0.0.1", "--port", "5008"])

    assert result == 0
    assert ("tree", "QBP Meeting", qbp_process) in calls
    assert ("tree", "kkFileView", kk_process) in calls
    assert not [call for call in calls if call[0] == "direct-terminate"]
    assert "服务已停止" in capsys.readouterr().out


def test_ensure_kkfileview_runtime_requires_offline_manifest(tmp_path):
    try:
        start.ensure_kkfileview_runtime(tmp_path)
    except FileNotFoundError as exc:
        assert "kkfileview" in str(exc)
    else:
        raise AssertionError("expected missing offline bundle to fail")


def test_ensure_kkfileview_runtime_skips_matching_install(tmp_path):
    offline_root = tmp_path / "kkfileview"
    runtime = offline_root / "runtime"
    runtime.mkdir(parents=True)
    manifest = {
        "kkfileview": {"version": "4.4.0", "filename": "kk.zip", "sha256": "abc"},
        "jre": {"version": "17", "filename": "jre.zip", "sha256": "def"},
    }
    (offline_root / "manifest.json").write_text(start.json_dumps(manifest), encoding="utf-8")
    (runtime / "installed_manifest.json").write_text(start.json_dumps(manifest), encoding="utf-8")

    result = start.ensure_kkfileview_runtime(tmp_path)

    assert result.runtime_dir == runtime
    assert result.installed is True


def test_describe_existing_server_tells_user_terminal_may_exit():
    message = start.describe_existing_server("127.0.0.1", 5008, 12345)

    assert "already running" in message
    assert "PID 12345" in message
    assert "terminal can safely exit" in message


def test_unique_pids_removes_empty_and_duplicates():
    assert start.unique_pids([None, 12, 12, "34", "bad"]) == [12, 34]


def test_zip_extract_path_uses_windows_long_path_prefix(tmp_path):
    target = tmp_path / "kkfileview" / "runtime" / "kkfileview"

    result = start.zip_extract_path(target, "nt")

    assert result.startswith("\\\\?\\")
    assert str(target) in result
