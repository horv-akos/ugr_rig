"""Startup banner: which URLs the rig server says it can be opened on."""

from __future__ import annotations

import argparse

from service.controller import RigController
from web.__main__ import banner
from web.netinfo import interface_ipv4s, is_loopback, server_urls, ssh_server_ip

IFACES = [("wlan0", "10.42.0.1"), ("eth0", "192.168.1.20")]


def test_ssh_server_ip():
    assert ssh_server_ip({"SSH_CONNECTION": "10.42.0.17 51000 10.42.0.1 22"}) == "10.42.0.1"
    assert ssh_server_ip({}) is None
    assert ssh_server_ip({"SSH_CONNECTION": "garbage"}) is None


def test_specific_host_is_the_only_url():
    assert server_urls("127.0.0.1", 8000, IFACES, None, "rigpi") == [("http://127.0.0.1:8000", "")]
    assert server_urls("::1", 8000, IFACES, None, "rigpi") == [("http://[::1]:8000", "")]


def test_wildcard_lists_every_interface_ssh_first():
    urls = server_urls("0.0.0.0", 8000, IFACES, "192.168.1.20", "rigpi")
    assert urls == [
        ("http://192.168.1.20:8000", "the address you SSH'd to"),
        ("http://10.42.0.1:8000", "wlan0"),
        ("http://rigpi.local:8000", "mDNS, if the laptop resolves it"),
        ("http://127.0.0.1:8000", "this machine only"),
    ]


def test_interface_ipv4s_skips_loopback_and_link_local():
    for _, addr in interface_ipv4s():  # whatever this machine has
        assert not addr.startswith(("127.", "169.254."))


def test_is_loopback():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("10.42.0.1")


def test_banner_hints_at_0000_over_ssh(tmp_path, monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.42.0.17 51000 10.42.0.1 22")
    rig = RigController.from_config("sim", tmp_path, time_scale=0)
    try:
        args = argparse.Namespace(host="127.0.0.1", port=8000, backend="sim", time_scale=0.0,
                                  config=None)
        text = banner(args, rig)
        assert "http://127.0.0.1:8000" in text and "--host 0.0.0.0" in text
        assert "drivers off, not zeroed" in text

        args.host = "0.0.0.0"
        text = banner(args, rig)
        first = text.split("\n")[3].split()
        assert first[0] == "http://10.42.0.1:8000" and "SSH'd" in text
        assert "--host 0.0.0.0" not in text
    finally:
        rig.close()
