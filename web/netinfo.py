"""Which URLs reach the rig server, for the startup banner.

The Pi runs headless and is usually reached over SSH, so the server has to say where it can be
opened from a laptop. Bound to 0.0.0.0 it listens on every interface, and the useful answer is
each interface's IPv4 address (wlan0 when the Pi serves its own WiFi, eth0 on a bench cable).
On Linux those come from the SIOCGIFADDR ioctl per interface, which needs only the stdlib;
elsewhere we fall back to resolving the hostname. The address the current SSH session came in
on (SSH_CONNECTION) is known to reach the laptop, so it is listed first.
"""

from __future__ import annotations

import os
import socket
import struct

WILDCARD_HOSTS = {"0.0.0.0", "", "::"}
_SIOCGIFADDR = 0x8915  # Linux: get an interface's IPv4 address


def interface_ipv4s() -> list[tuple[str, str]]:
    """(interface, address) for every interface with an IPv4 address.

    Loopback and link-local (169.254.x, an interface that got no DHCP lease) are left out.
    """
    found: list[tuple[str, str]] = []
    try:
        import fcntl  # Linux / macOS only
    except ImportError:
        fcntl = None
    if fcntl is not None and hasattr(socket, "if_nameindex"):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            for _, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(s.fileno(), _SIOCGIFADDR,
                                         struct.pack("256s", name.encode()[:15]))
                except OSError:
                    continue  # interface down or without IPv4
                found.append((name, socket.inet_ntoa(packed[20:24])))
    if not found:
        try:
            infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        except OSError:
            infos = []
        for info in infos:
            found.append(("", str(info[4][0])))
    out: list[tuple[str, str]] = []
    for name, addr in found:
        if addr.startswith(("127.", "169.254.")):
            continue
        if all(addr != a for _, a in out):
            out.append((name, addr))
    return out


def ssh_server_ip(environ: dict[str, str] | None = None) -> str | None:
    """The local address of the SSH session we were started from, if any.

    SSH_CONNECTION is "client_ip client_port server_ip server_port".
    """
    parts = (environ if environ is not None else os.environ).get("SSH_CONNECTION", "").split()
    return parts[2] if len(parts) == 4 else None


def server_urls(host: str, port: int,
                interfaces: list[tuple[str, str]] | None = None,
                ssh_ip: str | None = None,
                hostname: str | None = None) -> list[tuple[str, str]]:
    """(url, note) pairs to print. Arguments after port are for tests; None means look them up."""
    def url(h: str) -> str:
        return f"http://[{h}]:{port}" if ":" in h else f"http://{h}:{port}"

    if host not in WILDCARD_HOSTS:
        return [(url(host), "")]
    interfaces = interface_ipv4s() if interfaces is None else interfaces
    hostname = socket.gethostname() if hostname is None else hostname
    out: list[tuple[str, str]] = []
    if ssh_ip:
        out.append((url(ssh_ip), "the address you SSH'd to"))
    for name, addr in interfaces:
        if addr != ssh_ip:
            out.append((url(addr), name))
    if hostname:
        out.append((url(f"{hostname}.local"), "mDNS, if the laptop resolves it"))
    out.append((url("127.0.0.1"), "this machine only"))
    return out


def is_loopback(host: str) -> bool:
    return host in {"localhost", "::1"} or host.startswith("127.")
