import base64
import ipaddress
import logging
import socket
from dataclasses import dataclass
from io import BytesIO

import psutil
import qrcode
from qrcode.image.svg import SvgPathImage

logger = logging.getLogger(__name__)

_VIRTUAL_INTERFACE_MARKERS = (
    "docker",
    "br-",
    "veth",
    "virbr",
    "podman",
    "cni",
    "flannel",
    "kube",
    "tun",
    "tap",
    "tailscale",
    "wireguard",
    "vbox",
    "vmnet",
    "vnet",
    "xen",
    "lxc",
    "zt",
)


@dataclass(frozen=True)
class NetworkCandidate:
    interface: str
    ip: str

    @property
    def key(self) -> str:
        return f"{self.interface}|{self.ip}"


@dataclass(frozen=True)
class AccessDisplay:
    status: str
    message: str
    candidates: tuple[NetworkCandidate, ...] = ()
    selected_key: str = ""
    selected_url: str = ""
    qr_data_uri: str = ""


class InterfaceDiscoveryError(Exception):
    """Raised when local interface addresses cannot be enumerated."""


def _candidate_sort_key(candidate: NetworkCandidate) -> tuple[bool, str, str]:
    virtual = any(
        marker in candidate.interface.lower()
        for marker in _VIRTUAL_INTERFACE_MARKERS
    )
    return virtual, candidate.interface.lower(), candidate.ip


def _is_usable_ipv4(value: str) -> bool:
    try:
        address = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError:
        return False
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
    )


def discover_network_candidates() -> tuple[NetworkCandidate, ...]:
    try:
        addresses = psutil.net_if_addrs()
        statuses = psutil.net_if_stats()
    except (OSError, psutil.Error) as error:
        raise InterfaceDiscoveryError("ネットワークインターフェースを取得できません。") from error

    candidates: list[NetworkCandidate] = []
    for interface, interface_addresses in addresses.items():
        status = statuses.get(interface)
        if status is None or not status.isup:
            continue
        for address in interface_addresses:
            if address.family != socket.AF_INET or not _is_usable_ipv4(address.address):
                continue
            candidates.append(NetworkCandidate(interface=interface, ip=address.address))

    candidates.sort(key=_candidate_sort_key)
    return tuple(candidates)


def _make_qr_data_uri(url: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(url)
    qr.make(fit=True)
    image = qr.make_image(image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def _unavailable(status: str, message: str) -> AccessDisplay:
    return AccessDisplay(status=status, message=message)


def resolve_access_display(
    *,
    listen_host: str | None,
    listen_port: int | None,
    display_ip: str | None = None,
    selected_key: str | None = None,
    candidates_provider=None,
) -> AccessDisplay:
    get_candidates = candidates_provider or discover_network_candidates
    if not listen_host or not isinstance(listen_port, int) or not 1 <= listen_port <= 65535:
        return _unavailable("unknown", "接続先を確認できません。推奨起動方法をREADMEで確認してください。")

    try:
        bind_address = ipaddress.ip_address(listen_host)
    except ValueError:
        return _unavailable("unknown", "接続先を確認できません。待受設定をIPv4アドレスで確認してください。")

    if bind_address.is_loopback:
        return _unavailable("disabled", "PCからのアクセスは無効です（ローカル限定で待ち受けています）。")
    if isinstance(bind_address, ipaddress.IPv6Address):
        return _unavailable("unsupported", "IPv6待受のため、IPv4形式のPC用URLとQRコードは表示できません。")
    if bind_address.is_unspecified:
        try:
            candidates = list(get_candidates())
        except InterfaceDiscoveryError:
            logger.exception("Could not discover local IPv4 interfaces")
            candidates = []
            discovery_failed = True
        else:
            discovery_failed = False
        candidates = [
            candidate for candidate in candidates if _is_usable_ipv4(candidate.ip)
        ]
        candidates.sort(key=_candidate_sort_key)
        if display_ip:
            if not _is_usable_ipv4(display_ip):
                return _unavailable("unknown", "表示アドレス設定が有効なLAN側IPv4アドレスではありません。")
            configured = next(
                (candidate for candidate in candidates if candidate.ip == display_ip),
                None,
            )
            if configured is None:
                candidates.insert(0, NetworkCandidate("明示設定", display_ip))
            else:
                candidates.remove(configured)
                candidates.insert(0, configured)
        if not candidates:
            message = (
                "ネットワークインターフェースから候補を取得できません。接続先を確認してください。"
                if discovery_failed
                else "利用できるLAN側IPv4アドレスがありません。"
            )
            return _unavailable("unavailable", message)
    else:
        if not _is_usable_ipv4(str(bind_address)):
            return _unavailable("unavailable", "待受IPv4アドレスはPC用接続先として利用できません。")
        try:
            discovered = get_candidates()
        except InterfaceDiscoveryError:
            logger.exception("Could not match the bound IPv4 address to a local interface")
            discovered = ()
        matching_interface = next(
            (candidate.interface for candidate in discovered if candidate.ip == str(bind_address)),
            "待受アドレス",
        )
        candidates = [NetworkCandidate(matching_interface, str(bind_address))]

    selected = next(
        (candidate for candidate in candidates if candidate.key == selected_key),
        candidates[0],
    )
    url = f"http://{selected.ip}:{listen_port}/pc/attendees"
    return AccessDisplay(
        status="available",
        message="URLとQRコードの表示は、通信到達性を保証するものではありません。",
        candidates=tuple(candidates),
        selected_key=selected.key,
        selected_url=url,
        qr_data_uri=_make_qr_data_uri(url),
    )
