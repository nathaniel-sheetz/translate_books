"""Tests for the pieces that let the app run as an unattended service.

Covers the /healthz probe, the tailnet-vs-Wi-Fi labelling in the startup
banner, and the BOOKS_DEBUG gate that keeps Werkzeug's debugger opt-in.
"""

import socket
from pathlib import Path

import pytest

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
