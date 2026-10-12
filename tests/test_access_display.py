import base64
import re
import socket
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from office_terminal.main import create_app
from office_terminal.services import access_display_service
from office_terminal.services.access_display_service import (
    InterfaceDiscoveryError,
    NetworkCandidate,
    resolve_access_display,
)


def _candidates(*items):
    return tuple(NetworkCandidate(interface, address) for interface, address in items)


def test_loopback_listener_hides_lan_url_and_qr():
    display = resolve_access_display(
        listen_host="127.0.0.1",
        listen_port=8000,
        candidates_provider=lambda: pytest.fail("Loopback must not expose interface candidates"),
    )
    assert display.status == "disabled"
    assert display.selected_url == ""
    assert display.qr_data_uri == ""


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("192.168.20.10", 8000, "http://192.168.20.10:8000/pc/attendees"),
        ("0.0.0.0", 9100, "http://192.168.20.10:9100/pc/attendees"),
    ],
)
def test_listener_address_and_port_form_the_display_url(host, port, expected):
    display = resolve_access_display(
        listen_host=host,
        listen_port=port,
        candidates_provider=lambda: _candidates(("enp3s0", "192.168.20.10"), ("wlan0", "192.168.20.11")),
    )
    assert display.selected_url == expected
    assert expected in display.qr_data_uri or display.qr_data_uri.startswith(
        "data:image/svg+xml;base64,"
    )


def test_multiple_candidates_are_identified_and_selectable():
    candidates = _candidates(
        ("docker0", "172.18.0.1"),
        ("wlan0", "192.168.10.21"),
        ("enp2s0", "192.168.10.20"),
    )
    display = resolve_access_display(
        listen_host="0.0.0.0",
        listen_port=8123,
        selected_key="wlan0|192.168.10.21",
        candidates_provider=lambda: candidates,
    )
    assert [(item.interface, item.ip) for item in display.candidates] == [
        ("enp2s0", "192.168.10.20"),
        ("wlan0", "192.168.10.21"),
        ("docker0", "172.18.0.1"),
    ]
    assert display.selected_key == "wlan0|192.168.10.21"
    assert display.selected_url == "http://192.168.10.21:8123/pc/attendees"


def test_candidate_selection_is_revalidated_against_current_addresses():
    current_candidates = [NetworkCandidate("enp2s0", "192.168.10.20")]
    display = resolve_access_display(
        listen_host="0.0.0.0",
        listen_port=8000,
        selected_key="wlan0|192.168.10.21",
        candidates_provider=lambda: tuple(current_candidates),
    )
    assert display.selected_url == "http://192.168.10.20:8000/pc/attendees"

    current_candidates[:] = [NetworkCandidate("wlan0", "192.168.10.21")]
    refreshed = resolve_access_display(
        listen_host="0.0.0.0",
        listen_port=8000,
        candidates_provider=lambda: tuple(current_candidates),
    )
    assert refreshed.selected_url == "http://192.168.10.21:8000/pc/attendees"


def test_explicit_display_address_is_used_when_interface_enumeration_fails():
    def unavailable():
        raise InterfaceDiscoveryError("unavailable")

    display = resolve_access_display(
        listen_host="0.0.0.0",
        listen_port=8000,
        display_ip="192.168.30.40",
        candidates_provider=unavailable,
    )
    assert display.selected_url == "http://192.168.30.40:8000/pc/attendees"
    assert display.candidates[0].interface == "明示設定"


@pytest.mark.parametrize(
    ("host", "port", "expected_status"),
    [
        (None, None, "unknown"),
        ("::", 8000, "unsupported"),
        ("0.0.0.0", 8000, "unavailable"),
    ],
)
def test_unavailable_listener_or_interfaces_produces_japanese_message(
    host,
    port,
    expected_status,
):
    display = resolve_access_display(
        listen_host=host,
        listen_port=port,
        candidates_provider=lambda: (),
    )
    assert display.status == expected_status
    assert display.message
    assert not display.qr_data_uri


def test_candidate_discovery_excludes_non_global_special_addresses_and_down_links(monkeypatch):
    addresses = {
        "lo": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")],
        "eth0": [
            SimpleNamespace(family=socket.AF_INET, address="192.168.1.10"),
            SimpleNamespace(family=socket.AF_INET, address="169.254.2.10"),
            SimpleNamespace(family=socket.AF_INET, address="0.0.0.0"),
        ],
        "docker0": [SimpleNamespace(family=socket.AF_INET, address="172.18.0.1")],
        "down0": [SimpleNamespace(family=socket.AF_INET, address="10.0.0.2")],
    }
    statuses = {
        "lo": SimpleNamespace(isup=True),
        "eth0": SimpleNamespace(isup=True),
        "docker0": SimpleNamespace(isup=True),
        "down0": SimpleNamespace(isup=False),
    }
    monkeypatch.setattr(access_display_service.psutil, "net_if_addrs", lambda: addresses)
    monkeypatch.setattr(access_display_service.psutil, "net_if_stats", lambda: statuses)

    assert access_display_service.discover_network_candidates() == (
        NetworkCandidate("eth0", "192.168.1.10"),
        NetworkCandidate("docker0", "172.18.0.1"),
    )


def test_menu_uses_current_interface_addresses_not_request_host_or_stale_selection(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(tmp_path / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(tmp_path / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    interfaces = [NetworkCandidate("enp2s0", "192.168.40.10")]
    monkeypatch.setattr(
        access_display_service,
        "discover_network_candidates",
        lambda: tuple(interfaces),
    )

    app = create_app(
        tmp_path / "office.db",
        listen_host="0.0.0.0",
        listen_port=9123,
    )
    with TestClient(app) as client:
        first_page = client.get("/menu", headers={"host": "attacker.example"})
        assert "http://192.168.40.10:9123/pc/attendees" in first_page.text
        assert "http://attacker.example:9123/pc/attendees" not in first_page.text
        assert "data:image/svg+xml;base64," in first_page.text

        interfaces[:] = [NetworkCandidate("wlan0", "192.168.40.11")]
        refreshed_page = client.get("/menu", headers={"host": "attacker.example"})
        assert "http://192.168.40.11:9123/pc/attendees" in refreshed_page.text
        assert "192.168.40.10" not in refreshed_page.text


def test_menu_renders_candidate_picker_and_only_generates_qr_for_selected_interface(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(tmp_path / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(tmp_path / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    monkeypatch.setattr(
        access_display_service,
        "discover_network_candidates",
        lambda: _candidates(("enp2s0", "192.168.40.10"), ("wlan0", "192.168.40.11")),
    )
    app = create_app(tmp_path / "office.db", listen_host="0.0.0.0", listen_port=8123)

    with TestClient(app) as client:
        initial = client.get("/menu")
        assert 'name="candidate"' in initial.text
        assert "enp2s0 — 192.168.40.10" in initial.text
        assert "wlan0 — 192.168.40.11" in initial.text

        selected = client.get("/menu?candidate=wlan0%7C192.168.40.11")
        assert 'href="http://192.168.40.11:8123/pc/attendees"' in selected.text
        assert re.search(
            r'<option value="wlan0\|192\.168\.40\.11"\s+selected>',
            selected.text,
        )


def test_menu_includes_qr_for_exact_selected_url():
    submitted_data = []
    original_qrcode = access_display_service.qrcode.QRCode

    class RecordingQRCode(original_qrcode):
        def add_data(self, data, optimize=20):
            submitted_data.append(data)
            return super().add_data(data, optimize)

    access_display_service.qrcode.QRCode = RecordingQRCode
    try:
        display = resolve_access_display(
            listen_host="192.168.1.20",
            listen_port=8123,
            candidates_provider=lambda: _candidates(("enp2s0", "192.168.1.20")),
        )
    finally:
        access_display_service.qrcode.QRCode = original_qrcode

    svg = base64.b64decode(display.qr_data_uri.split(",", 1)[1]).decode("utf-8")
    assert submitted_data == [display.selected_url]
    assert "<svg" in svg


def test_menu_without_shared_listener_configuration_remains_available(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("OFFICE_TERMINAL_OPERATIONS_CONFIG_PATH", str(tmp_path / "operations.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_BACKUP_STATE_PATH", str(tmp_path / "backup-state.json"))
    monkeypatch.setenv("OFFICE_TERMINAL_RUNTIME_LOCK_PATH", str(tmp_path / "runtime.lock"))
    app = create_app(tmp_path / "office.db")
    with TestClient(app) as client:
        response = client.get("/menu")
    assert response.status_code == 200
    assert "接続先を確認できません" in response.text
    assert "退室点検" in response.text
    assert "出社・在席" in response.text
    assert "data:image/svg+xml;base64," not in response.text
