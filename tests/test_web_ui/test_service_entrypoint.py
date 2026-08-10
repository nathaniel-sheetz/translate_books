"""Tests for the pieces that let the app run as an unattended service.

Covers the /healthz probe, the tailnet-vs-Wi-Fi labelling in the startup
banner, the BOOKS_DEBUG gate that keeps Werkzeug's debugger opt-in, and the
cwd the entry point establishes for cwd-relative paths in src/.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

requires_powershell = pytest.mark.skipif(
    sys.platform != "win32", reason="reader.ps1 drives the Windows Task Scheduler"
)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_ui.app import (
    _debug_enabled,
    _is_tailnet_addr,
    _print_access_urls,
    app,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_reports_ok_and_version(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True

    version_file = Path(__file__).resolve().parent.parent.parent / "VERSION"
    assert body["version"] == version_file.read_text(encoding="utf-8").strip()


def test_healthz_does_not_scan_projects(client, monkeypatch):
    """The probe must stay cheap: no project-directory walking."""

    def boom(*args, **kwargs):
        raise AssertionError("/healthz touched the filesystem")

    monkeypatch.setattr(Path, "iterdir", boom)
    monkeypatch.setattr(Path, "glob", boom)

    assert client.get("/healthz").status_code == 200


# ---------------------------------------------------------------------------
# Tailnet address detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("100.64.0.1", True),
        ("100.101.102.103", True),
        ("100.127.255.255", True),
        ("100.63.255.255", False),  # just below the CGNAT range
        ("100.128.0.0", False),  # just above it
        ("192.168.1.20", False),
        ("127.0.0.1", False),
        ("not-an-address", False),
        ("", False),
    ],
)
def test_is_tailnet_addr(addr, expected):
    assert _is_tailnet_addr(addr) is expected


# ---------------------------------------------------------------------------
# Startup banner
# ---------------------------------------------------------------------------


def test_banner_labels_tailnet_separately_from_wifi(capsys, monkeypatch):
    monkeypatch.setattr(
        socket,
        "gethostbyname_ex",
        lambda _host: ("host", [], ["192.168.1.20", "100.101.102.103"]),
    )

    _print_access_urls(5000, host="0.0.0.0")
    out = capsys.readouterr().out

    assert "Network:  http://192.168.1.20:5000" in out
    assert "Tailnet:  http://100.101.102.103:5000" in out
    # The tailnet address must not be sold as a same-Wi-Fi address.
    wifi_line = next(line for line in out.splitlines() if "same Wi-Fi" in line)
    assert "100.101.102.103" not in wifi_line


def test_banner_omits_wifi_caption_when_only_tailnet_present(capsys, monkeypatch):
    monkeypatch.setattr(
        socket, "gethostbyname_ex", lambda _host: ("host", [], ["100.101.102.103"])
    )

    _print_access_urls(5000, host="0.0.0.0")
    out = capsys.readouterr().out

    assert "same Wi-Fi" not in out
    assert "Tailnet:  http://100.101.102.103:5000" in out


def test_banner_survives_dns_failure(capsys, monkeypatch):
    def raise_gaierror(_host):
        raise socket.gaierror("no resolver")

    monkeypatch.setattr(socket, "gethostbyname_ex", raise_gaierror)

    _print_access_urls(5000, host="0.0.0.0")
    out = capsys.readouterr().out

    assert "http://localhost:5000" in out


def test_banner_for_loopback_bind_points_at_tailscale_serve(capsys, monkeypatch):
    def boom(_host):
        raise AssertionError("loopback bind should not enumerate interfaces")

    monkeypatch.setattr(socket, "gethostbyname_ex", boom)

    _print_access_urls(5000, host="127.0.0.1")
    out = capsys.readouterr().out

    assert "loopback only" in out
    assert "tailscale serve status" in out
    assert "same Wi-Fi" not in out


# ---------------------------------------------------------------------------
# BOOKS_DEBUG gate
# ---------------------------------------------------------------------------


def test_debug_is_off_when_unset(monkeypatch):
    monkeypatch.delenv("BOOKS_DEBUG", raising=False)
    assert _debug_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_debug_opt_in_values(monkeypatch, value):
    monkeypatch.setenv("BOOKS_DEBUG", value)
    assert _debug_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_debug_stays_off_for_other_values(monkeypatch, value):
    monkeypatch.setenv("BOOKS_DEBUG", value)
    assert _debug_enabled() is False


# ---------------------------------------------------------------------------
# Working directory
# ---------------------------------------------------------------------------


def test_entrypoint_chdirs_to_repo_root(tmp_path):
    """The task registers no "Start in" directory, so serve.py must set one.

    Run in a subprocess started somewhere else entirely: importing the module
    in-process would move the test session's cwd out from under everything
    else. Importing is enough -- the chdir is module level, main() is not.
    """
    repo_root = Path(__file__).resolve().parents[2]
    serve_py = repo_root / "scripts" / "serve.py"

    code = "\n".join(
        [
            "import importlib.util, os",
            f"spec = importlib.util.spec_from_file_location('serve', {str(serve_py)!r})",
            "mod = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(mod)",
            "print(os.getcwd())",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).resolve() == repo_root


def test_prompt_template_resolves_from_a_foreign_cwd(tmp_path, monkeypatch):
    """The failure this guards: batch translation from the service died on
    "Template file not found: prompts\\translation.txt" because the default
    resolved against C:\\Windows\\system32."""
    from src.utils.file_io import load_prompt_template

    monkeypatch.chdir(tmp_path)
    assert not (Path(os.getcwd()) / "prompts").exists()

    assert "{{source_text}}" in load_prompt_template()


# ---------------------------------------------------------------------------
# Scheduled task definition
# ---------------------------------------------------------------------------


def _reader_spec():
    """The task definition `reader.ps1 install` would register, as a dict.

    `spec` exists so this can be asserted on without registering anything --
    the alternative is a live Task Scheduler round-trip in the test suite.
    """
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-File",
            str(repo_root / "scripts" / "reader.ps1"),
            "spec",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@requires_powershell
def test_install_spec_sets_working_directory():
    """The regression lock for the bug this whole thread started with: a task
    registered without a WorkingDirectory starts in system32, and every
    cwd-relative path in src/ resolves against it."""
    repo_root = Path(__file__).resolve().parents[2]
    spec = _reader_spec()

    assert Path(spec["WorkingDirectory"]).resolve() == repo_root
    assert "scripts\\serve.py" in spec["Arguments"]


@requires_powershell
def test_install_spec_allows_battery_start():
    """The hand-made task disallowed it, so a reboot on battery left the reader
    down -- the exact case docs/design/tailscale.md Step 3 promises to survive."""
    spec = _reader_spec()

    assert spec["DisallowStartIfOnBatteries"] is False
    assert spec["StopIfGoingOnBatteries"] is False


@requires_powershell
def test_install_spec_keeps_the_service_durable():
    """No run-time cap, one instance, and a restart watchdog."""
    spec = _reader_spec()

    assert spec["ExecutionTimeLimit"] == "PT0S"  # PT0S means no limit
    assert spec["MultipleInstances"] == "IgnoreNew"
    assert spec["RestartCount"] == 60
    assert spec["TriggerClass"] == "MSFT_TaskBootTrigger"
