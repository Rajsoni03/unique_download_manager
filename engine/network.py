"""Network interface discovery, classification and speed sampling."""

import os
import platform
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import psutil

# Virtual / non-routable interfaces we never want to bind downloads to.
_EXCLUDE_PREFIXES = (
    "lo", "docker", "br-", "veth", "virbr", "vmnet", "vboxnet",
    "awdl", "llw", "gif", "stf", "tun", "utun", "apml", "ipsec",
    "ppp", "bluetooth", "ap", "hotspot", "tailscale",
)
_EXCLUDE_EXACT = {"lo", "awdl0", "llw0"}


def _is_excluded_interface(name: str) -> bool:
    low = name.lower()
    return low in _EXCLUDE_EXACT or low.startswith(_EXCLUDE_PREFIXES)

_KIND_USB = ("rndis", "usb", "tether", "cdc", "ncm", "usbnet", "hardlink", "lan78")
_KIND_WIFI = ("wl", "wifi", "airport", "wlan", "p2p", "hotspot", "ap")


@dataclass
class Interface:
    name: str
    ip: str
    kind: str = "Network"
    mac_kind: str = ""
    up: bool = True
    enabled: bool = True
    speed_mbps: int = 0          # link speed reported by OS (0 = unknown)
    workers: int = 0             # active download workers bound to this NIC (global)
    app_speed: float = 0.0       # bytes/s delivered by this app over this NIC
    ema_speed: float = 0.0       # smoothed app speed (drives load balancing)
    rx_speed: float = 0.0        # system-wide receive bytes/s
    tx_speed: float = 0.0        # system-wide transmit bytes/s
    total_app_bytes: int = 0     # cumulative bytes sent through this app

    @property
    def label(self) -> str:
        return f"{self.kind} · {self.name}"


def _wifi_names_macos() -> set:
    names = set()
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=3,
        ).stdout
        port = None
        for line in out.splitlines():
            if line.startswith("Hardware Port:"):
                port = line.split(":", 1)[1].strip()
            elif line.startswith("Device:") and port:
                dev = line.split(":", 1)[1].strip()
                if "wi-fi" in port.lower() or "airport" in port.lower():
                    names.add(dev)
                port = None
    except Exception:
        pass
    return names


def _wifi_names_linux() -> set:
    names = set()
    base = "/sys/class/net"
    try:
        for name in os.listdir(base):
            if os.path.exists(os.path.join(base, name, "wireless")):
                names.add(name)
    except Exception:
        pass
    return names


def _classify(name: str) -> str:
    n = name.lower()
    if any(k in n for k in _KIND_USB):
        return "USB Tethering"
    if any(n.startswith(p) or p in n for p in _KIND_WIFI):
        return "Wi-Fi"
    if n.startswith(("eth", "eno", "ens", "enp", "en", "em", "p4p", "lan")):
        return "Ethernet"
    return "Network"


class NetworkMonitor:
    """Detects local IPv4 interfaces and samples per-interface throughput."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.interfaces: Dict[str, Interface] = {}   # name -> Interface
        self.app_window: Dict[str, int] = {}         # ip -> bytes since last sample
        self.history: List[float] = []               # combined app speed history
        self.history_len = 90
        self._prev_sys: Dict[str, tuple] = {}        # name -> (ts, rx, tx)
        self._wifi_names = set()
        if platform.system() == "Darwin":
            self._wifi_names = _wifi_names_macos()
        elif platform.system() == "Linux":
            self._wifi_names = _wifi_names_linux()
        self.refresh_interfaces()

    # ------------------------------------------------------------------ discovery

    def refresh_interfaces(self) -> None:
        """(Re)discover IPv4 interfaces. Preserves enabled flags of known ones."""
        found: Dict[str, Interface] = {}
        try:
            addrs = psutil.net_if_addrs()
            stats = psutil.net_if_stats()
        except Exception:
            addrs, stats = {}, {}

        # Try to grab link speed (nic speed) where the OS exposes it.
        speeds: Dict[str, int] = {}
        try:
            if hasattr(psutil, "net_if_stats"):
                for name, st in stats.items():
                    if getattr(st, "speed", None) and st.speed > 0:
                        speeds[name] = int(st.speed)
        except Exception:
            pass

        for name, entries in addrs.items():
            if _is_excluded_interface(name):
                continue
            for e in entries:
                if e.family.name != "AF_INET":
                    continue
                ip = e.address
                if not ip or ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                st = stats.get(name)
                up = bool(st.isup) if st else True
                kind = self._kind_for(name)
                found[name] = Interface(
                    name=name, ip=ip, kind=kind, up=up, speed_mbps=speeds.get(name, 0)
                )
                break

        with self.lock:
            for name, iface in found.items():
                old = self.interfaces.get(name)
                if old is not None:
                    iface.enabled = old.enabled
                    iface.workers = old.workers
                    iface.ema_speed = old.ema_speed
                    iface.total_app_bytes = old.total_app_bytes
            self.interfaces = found

    def _kind_for(self, name: str) -> str:
        if name in self._wifi_names:
            return "Wi-Fi"
        n = name.lower()
        if any(k in n for k in _KIND_USB):
            return "USB Tethering"
        if platform.system() == "Darwin" and re.fullmatch(r"en\d+", n):
            # en0 is usually built-in Wi-Fi on Mac; other enX often USB/Ethernet adapters.
            return "Wi-Fi" if name in self._wifi_names or name == "en0" else "Ethernet"
        return _classify(name)

    # ------------------------------------------------------------------ counters

    def add_app_bytes(self, ip: str, nbytes: int) -> None:
        with self.lock:
            self.app_window[ip] = self.app_window.get(ip, 0) + nbytes
            for iface in self.interfaces.values():
                if iface.ip == ip:
                    iface.total_app_bytes += nbytes
                    break

    def sample(self) -> None:
        """Called once per second: converts windows into speeds, refreshes links."""
        now = time.time()

        # System-wide per-NIC speeds
        try:
            counters = psutil.net_io_counters(pernic=True)
        except Exception:
            counters = {}
        with self.lock:
            for name, c in counters.items():
                prev = self._prev_sys.get(name)
                self._prev_sys[name] = (now, c.bytes_recv, c.bytes_sent)
                if not prev:
                    continue
                ts, prx, ptx = prev
                dt = now - ts
                if dt <= 0:
                    continue
                rx = max(0.0, (c.bytes_recv - prx) / dt)
                tx = max(0.0, (c.bytes_sent - ptx) / dt)
                iface = self.interfaces.get(name)
                if iface:
                    iface.rx_speed, iface.tx_speed = rx, tx

            # App throughput per interface (window → speed → EMA)
            total = 0.0
            for name, iface in self.interfaces.items():
                window = 0
                for ip, nbytes in self.app_window.items():
                    if ip == iface.ip:
                        window = nbytes
                        break
                speed = float(window)
                iface.app_speed = speed
                iface.ema_speed = 0.6 * iface.ema_speed + 0.4 * speed
                total += speed
            self.app_window = {}

            self.history.append(total)
            if len(self.history) > self.history_len:
                del self.history[: len(self.history) - self.history_len]

        # Hot-plug detection (USB tethering plugged/unplugged, Wi-Fi switched…)
        if int(now) % 5 == 0:
            self.refresh_interfaces()

    # ------------------------------------------------------------------ balancing

    def capacity(self, iface: Interface, base: int) -> int:
        """Soft worker capacity for one interface (adaptive to measured speed)."""
        with self.lock:
            emas = [
                i.ema_speed for i in self.interfaces.values()
                if i.enabled and i.up
            ]
        total_ema = sum(emas)
        if total_ema <= 0 or iface.ema_speed <= 0:
            share = 1.0 / max(1, len(emas))
        else:
            share = iface.ema_speed / total_ema
        return max(1, min(base, round(base * (0.15 + 0.85 * share))))

    def usable(self) -> List[Interface]:
        with self.lock:
            return [i for i in self.interfaces.values() if i.enabled and i.up]

    def snapshot(self) -> List[dict]:
        with self.lock:
            out = []
            for i in self.interfaces.values():
                out.append({
                    "name": i.name, "ip": i.ip, "kind": i.kind, "up": i.up,
                    "enabled": i.enabled, "workers": i.workers,
                    "app_speed": i.app_speed, "ema": i.ema_speed,
                    "rx_speed": i.rx_speed, "tx_speed": i.tx_speed,
                    "total_app_bytes": i.total_app_bytes,
                    "speed_mbps": i.speed_mbps,
                })
            return out

    @property
    def combined_app_speed(self) -> float:
        with self.lock:
            return sum(i.app_speed for i in self.interfaces.values())

    @property
    def combined_system_rx(self) -> float:
        with self.lock:
            return sum(i.rx_speed for i in self.interfaces.values() if i.up)

    @property
    def combined_system_tx(self) -> float:
        with self.lock:
            return sum(i.tx_speed for i in self.interfaces.values() if i.up)

    @property
    def history_snapshot(self) -> List[float]:
        with self.lock:
            return list(self.history)
