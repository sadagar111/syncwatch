"""This PC's network addresses (shown in the startup checks)."""
from __future__ import annotations

import ipaddress
import socket
from typing import List, Tuple


def local_addresses() -> List[Tuple[str, str]]:
    """This PC's IPv4 addresses with a label: [('100.101.1.2', 'Tailscale'), ...]."""
    found = set()
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))  # sends nothing; just picks the outgoing interface
        found.add(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass
    result = []
    for text in found:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local:
            continue
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            label = "Tailscale"
        elif ip.is_private:
            label = "home network"
        else:
            label = "internet"
        result.append((text, label))
    order = {"Tailscale": 0, "home network": 1, "internet": 2}
    return sorted(result, key=lambda item: (order[item[1]], item[0]))
