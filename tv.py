"""Samsung Smart TV client — WebSocket + UPnP + SSDP + WoL."""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import xml.etree.ElementTree as ET
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from samsungtvws import SamsungTVWS

log = logging.getLogger("samsung-tv")

WS_TIMEOUT = 5


def _state_dir() -> str:
    """Where the pairing token is persisted (a secret). RENFIELD_STATE_DIR keeps
    it consistent with the sibling DLNA server; falls back to an XDG-ish path."""
    env = os.getenv("RENFIELD_STATE_DIR")
    if env:
        return os.path.expanduser(env)
    base = os.getenv("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "renfield-mcp-samsung")


# Pairing token file. The token is bound to CLIENT_NAME (the identity the TV
# shows in its "Allow?" popup and Device Connect Manager → Device List), so the
# name must stay constant once paired.
TOKEN_FILE = os.getenv("SAMSUNG_TOKEN_FILE") or os.path.join(_state_dir(), "token.json")
CLIENT_NAME = os.getenv("SAMSUNG_CLIENT_NAME", "Renfield")
# Pin a TV by IP (env) so control doesn't depend on SSDP, which misses a TV
# whose DLNA renderer (:9197) isn't currently advertising (it often isn't).
DEFAULT_TV_HOST = os.getenv("SAMSUNG_TV_HOST") or None
_UPNP_NS = "urn:schemas-upnp-org:service"
_SOAP_ENV = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    "<s:Body>{body}</s:Body></s:Envelope>"
)

APP_ALIASES: dict[str, list[str]] = {
    "netflix": ["11101200001", "3201907018807"],
    "youtube": ["111299001912"],
    "prime": ["3201512006785", "3201910019365"],
    "disney": ["3201901017640"],
    "spotify": ["3201606009684"],
    "apple tv": ["3201807016597"],
    "hbo": ["3201601007230", "3202301029760"],
    "max": ["3202301029760", "3201601007230"],
    "plex": ["3201512006963"],
    "browser": ["org.tizen.browser", "3201907018784"],
    "steam link": ["3201702011851"],
    "twitch": ["3202203026841"],
    "tiktok": ["3202008021577"],
    "tubi": ["3201504001965"],
    "pluto": ["3201808016802"],
    "paramount": ["3201710014981"],
    "gallery": ["3201710015037"],
    "smartthings": ["3201710015016"],
}

NAVIGATE_KEYS = {
    "home": "KEY_HOME", "back": "KEY_RETURN", "exit": "KEY_EXIT",
    "menu": "KEY_MENU", "source": "KEY_SOURCE", "guide": "KEY_GUIDE",
    "info": "KEY_INFO", "tools": "KEY_TOOLS",
    "up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
    "enter": "KEY_ENTER", "ok": "KEY_ENTER",
    "play": "KEY_PLAY", "pause": "KEY_PAUSE", "stop": "KEY_STOP",
    "ff": "KEY_FF", "rewind": "KEY_REWIND",
}


# ── SSDP Discovery ──────────────────────────────────────────────


# SSDP search targets. A Samsung TV only advertises its MediaRenderer (:9197)
# while its DLNA renderer is up (often it isn't). But it ALWAYS advertises its
# DIAL receiver and Samsung remote/IP-control even in standby — so search those
# too, then identify each candidate host via its :8001 REST endpoint, which
# answers regardless of the DLNA renderer's state.
_SSDP_TARGETS = (
    "urn:schemas-upnp-org:device:MediaRenderer:1",
    "urn:dial-multiscreen-org:device:dialreceiver:1",
    "urn:samsung.com:device:RemoteControlReceiver:1",
)


def _ssdp_hosts(timeout: float) -> set[str]:
    """Collect hosts that answer any Samsung-ish SSDP target."""
    hosts: set[str] = set()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    for st in _SSDP_TARGETS:
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: {int(timeout)}\r\n"
            f"ST: {st}\r\n\r\n"
        )
        try:
            sock.sendto(msg.encode(), ("239.255.255.250", 1900))
        except OSError:
            continue
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            data, addr = sock.recvfrom(4096)
            text = data.decode(errors="ignore")
            host = addr[0]
            # Prefer the LOCATION host; fall back to the responder address.
            for line in text.splitlines():
                if line.upper().startswith("LOCATION:"):
                    host = urlparse(line.split(":", 1)[1].strip()).hostname or host
            hosts.add(host)
    except socket.timeout:
        pass
    finally:
        sock.close()
    return hosts


def _identify(host: str) -> dict[str, Any] | None:
    """Identify a Samsung TV via its :8001 REST endpoint (works in standby)."""
    try:
        raw = urlopen(f"http://{host}:8001/api/v2/", timeout=3).read()
        dev = (json.loads(raw) or {}).get("device") or {}
    except Exception:
        return None
    if "samsung" not in (dev.get("type", "") + dev.get("OS", "")).lower() \
            and "tizen" not in dev.get("OS", "").lower():
        return None
    return {
        "ip": dev.get("ip", host),
        "name": dev.get("name", "Samsung TV"),
        "model": dev.get("modelName", "Unknown"),
        "manufacturer": "Samsung Electronics",
        "mac": dev.get("wifiMac", ""),
        "power": dev.get("PowerState", "unknown"),
    }


def discover(timeout: float = 4.0) -> list[dict[str, Any]]:
    """Discover Samsung TVs on the local network.

    Catches TVs whose DLNA renderer (:9197) isn't advertising by searching the
    DIAL/remote targets too and identifying each host via its always-on :8001
    REST endpoint.
    """
    tvs: dict[str, dict[str, Any]] = {}
    for host in _ssdp_hosts(timeout):
        info = _identify(host)
        if info:
            tvs[info["ip"]] = info
    return list(tvs.values())


# ── UPnP SOAP ───────────────────────────────────────────────────


def _soap_call(
    ip: str, control_url: str, service: str, action: str, args: str = ""
) -> str:
    body = f'<u:{action} xmlns:u="{_UPNP_NS}:{service}:1">{args}</u:{action}>'
    envelope = _SOAP_ENV.format(body=body)
    req = Request(
        f"http://{ip}:9197{control_url}",
        data=envelope.encode(),
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{_UPNP_NS}:{service}:1#{action}"',
        },
    )
    return urlopen(req, timeout=WS_TIMEOUT).read().decode()


def _soap_value(xml_text: str, tag: str) -> str | None:
    root = ET.fromstring(xml_text)
    for elem in root.iter():
        if elem.tag.endswith(tag):
            return elem.text
    return None


# ── Wake-on-LAN ─────────────────────────────────────────────────


def wake_on_lan(mac: str) -> None:
    mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(packet, ("255.255.255.255", 9))


# ── Main TV Client ──────────────────────────────────────────────


class SamsungTV:
    """Unified Samsung TV controller — WebSocket, REST, UPnP."""

    def __init__(self, ip: str | None = None):
        self._ip = ip or DEFAULT_TV_HOST
        self._ws: SamsungTVWS | None = None
        self._mac: str | None = None

    # ── Connection ───────────────────────────────────────────

    def _ensure_ip(self) -> str:
        if self._ip:
            return self._ip
        tvs = discover(timeout=4.0)
        if not tvs:
            raise ConnectionError("No Samsung TV found on the network")
        self._ip = tvs[0]["ip"]
        log.info("Auto-discovered TV at %s (%s)", self._ip, tvs[0].get("name"))
        return self._ip

    def _token_file(self, ip: str) -> str:
        """Per-host token path — each TV's pairing token is bound to that TV, so
        a shared file would clobber one TV's token with another's."""
        base, ext = os.path.splitext(TOKEN_FILE)
        return f"{base}-{ip.replace(':', '_')}{ext}"

    def _ensure_ws(self) -> SamsungTVWS:
        if self._ws is not None:
            return self._ws
        ip = self._ensure_ip()
        token_file = self._token_file(ip)
        os.makedirs(os.path.dirname(token_file), exist_ok=True)
        try:
            self._ws = SamsungTVWS(
                host=ip, port=8002, token_file=token_file,
                timeout=WS_TIMEOUT, name=CLIENT_NAME,
            )
            self._ws.open()
            log.info("Connected via WSS:8002")
        except Exception:
            log.warning("WSS:8002 failed, trying WS:8001")
            self._ws = SamsungTVWS(
                host=ip, port=8001, token_file=token_file,
                timeout=WS_TIMEOUT, name=CLIENT_NAME,
            )
            self._ws.open()
        return self._ws

    def _send_ws(self, method: str, **kwargs: Any) -> Any:
        """Send WebSocket command with auto-reconnect."""
        for attempt in range(2):
            ws = self._ensure_ws()
            try:
                return getattr(ws, method)(**kwargs)
            except Exception as e:
                self._ws = None
                if attempt == 0:
                    log.warning("WS error (%s), reconnecting...", e)
                else:
                    raise ConnectionError(f"TV WebSocket failed after retry: {e}") from e

    # ── Device Info ──────────────────────────────────────────

    def info(self) -> dict[str, Any]:
        ip = self._ensure_ip()
        try:
            raw = urlopen(f"http://{ip}:8001/api/v2/", timeout=WS_TIMEOUT).read()
            data = json.loads(raw)
            device = data.get("device", {})
            self._mac = device.get("wifiMac")
            try:
                device["currentVolume"] = self.get_volume()
            except Exception:
                pass
            return {
                "name": device.get("name", "Unknown"),
                "model": device.get("modelName", "Unknown"),
                "ip": device.get("ip", ip),
                "mac": self._mac,
                "power": device.get("PowerState", "unknown"),
                "os": device.get("OS", "Tizen"),
                "resolution": device.get("resolution", "unknown"),
                "network": device.get("networkType", "unknown"),
                "volume": device.get("currentVolume"),
            }
        except (URLError, OSError):
            return {"power": "off", "ip": ip, "note": "TV appears to be off"}

    # ── Power ────────────────────────────────────────────────

    def power_off(self) -> None:
        self._send_ws("send_key", key="KEY_POWER")

    def power_on(self, mac: str | None = None) -> None:
        target_mac = mac or self._mac
        if not target_mac:
            try:
                self.info()
                target_mac = self._mac
            except Exception:
                pass
        if not target_mac:
            raise ValueError("MAC address required. Use tv_info first.")
        wake_on_lan(target_mac)

    # ── Keys ─────────────────────────────────────────────────

    def send_key(self, key: str, times: int = 1) -> None:
        normalized = key.upper()
        if not normalized.startswith("KEY_"):
            normalized = f"KEY_{normalized}"
        for i in range(times):
            self._send_ws("send_key", key=normalized)
            if i < times - 1:
                time.sleep(0.15)

    def send_keys(self, keys: list[str], delay: float = 0.3) -> None:
        for i, key in enumerate(keys):
            self.send_key(key)
            if i < len(keys) - 1:
                time.sleep(delay)

    def navigate(self, action: str) -> None:
        key = NAVIGATE_KEYS.get(action.lower())
        if not key:
            raise ValueError(f"Unknown: '{action}'. Valid: {', '.join(NAVIGATE_KEYS)}")
        self.send_key(key)

    # ── Volume ───────────────────────────────────────────────

    def get_volume(self) -> int:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "GetVolume", "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        val = _soap_value(xml, "CurrentVolume")
        return int(val) if val else -1

    def set_volume(self, level: int) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "SetVolume",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredVolume>{max(0, min(100, level))}</DesiredVolume>",
        )

    def get_mute(self) -> bool:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "GetMute", "<InstanceID>0</InstanceID><Channel>Master</Channel>",
        )
        return _soap_value(xml, "CurrentMute") == "1"

    def set_mute(self, mute: bool) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "SetMute",
            f"<InstanceID>0</InstanceID><Channel>Master</Channel>"
            f"<DesiredMute>{'1' if mute else '0'}</DesiredMute>",
        )

    # ── Channel ──────────────────────────────────────────────

    def channel(self, number: int | None = None, direction: str | None = None) -> None:
        if number is not None:
            for digit in str(number):
                self.send_key(f"KEY_{digit}")
                time.sleep(0.15)
            time.sleep(0.3)
            self.send_key("KEY_ENTER")
        elif direction:
            self.send_key("KEY_CHUP" if direction.lower() == "up" else "KEY_CHDOWN")
        else:
            raise ValueError("Provide either number or direction ('up'/'down')")

    # ── Apps ─────────────────────────────────────────────────

    def _resolve_app_id(self, name_or_id: str) -> str:
        alias = name_or_id.lower().strip()
        if alias in APP_ALIASES:
            return APP_ALIASES[alias][0]
        if name_or_id.replace(".", "").replace("_", "").isalnum() and (
            len(name_or_id) > 8 or "." in name_or_id
        ):
            return name_or_id
        for key, ids in APP_ALIASES.items():
            if alias in key or key in alias:
                return ids[0]
        return name_or_id

    def list_apps(self) -> list[dict[str, Any]]:
        """Return known launchable apps. Direct query not supported on TU7000."""
        return [{"id": ids[0], "name": name.title()} for name, ids in APP_ALIASES.items()]

    def launch_app(self, name_or_id: str, meta_tag: str | None = None) -> None:
        app_id = self._resolve_app_id(name_or_id)
        ip = self._ensure_ip()
        try:
            req = Request(f"http://{ip}:8001/api/v2/applications/{app_id}", method="POST")
            urlopen(req, timeout=WS_TIMEOUT)
            return
        except Exception:
            pass
        self._send_ws("run_app", app_id=app_id, app_type=2, meta_tag=meta_tag or "")

    def close_app(self, name_or_id: str) -> None:
        app_id = self._resolve_app_id(name_or_id)
        ip = self._ensure_ip()
        req = Request(
            f"http://{ip}:8001/api/v2/applications/{app_id}", method="DELETE"
        )
        urlopen(req, timeout=WS_TIMEOUT)

    # ── Browser ──────────────────────────────────────────────

    def open_browser(self, url: str) -> None:
        self._send_ws("open_browser", url=url)

    # ── Text & Cursor ────────────────────────────────────────

    def send_text(self, text: str) -> None:
        self._send_ws("send_text", text=text)

    def move_cursor(self, x: int, y: int, duration: int = 500) -> None:
        self._send_ws("move_cursor", x=x, y=y, duration=duration)

    # ── DLNA Media ───────────────────────────────────────────

    def play_media(self, url: str, title: str = "Media") -> None:
        ip = self._ensure_ip()
        meta = (
            f'<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/"'
            f' xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<item><dc:title>{title}</dc:title></item></DIDL-Lite>"
        )
        _soap_call(
            ip, "/upnp/control/AVTransport1", "AVTransport",
            "SetAVTransportURI",
            f"<InstanceID>0</InstanceID>"
            f"<CurrentURI>{url}</CurrentURI>"
            f"<CurrentURIMetaData>{meta}</CurrentURIMetaData>",
        )
        time.sleep(0.5)
        _soap_call(
            ip, "/upnp/control/AVTransport1", "AVTransport",
            "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>",
        )

    def media_control(self, action: str, target: str | None = None) -> dict[str, Any]:
        ip = self._ensure_ip()
        a = action.lower()
        if a == "play":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
        elif a == "pause":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Pause", "<InstanceID>0</InstanceID>")
        elif a == "stop":
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Stop", "<InstanceID>0</InstanceID>")
        elif a == "seek" and target:
            _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                       "Seek", f"<InstanceID>0</InstanceID>"
                       f"<Unit>REL_TIME</Unit><Target>{target}</Target>")
        elif a == "status":
            xml_t = _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                               "GetTransportInfo", "<InstanceID>0</InstanceID>")
            xml_p = _soap_call(ip, "/upnp/control/AVTransport1", "AVTransport",
                               "GetPositionInfo", "<InstanceID>0</InstanceID>")
            return {
                "state": _soap_value(xml_t, "CurrentTransportState"),
                "position": _soap_value(xml_p, "RelTime"),
                "duration": _soap_value(xml_p, "TrackDuration"),
                "uri": _soap_value(xml_p, "TrackURI"),
            }
        else:
            raise ValueError(f"Unknown: '{action}'. Valid: play, pause, stop, seek, status")
        return {"action": a, "done": True}

    # ── App Detection ────────────────────────────────────────

    def current_app(self) -> dict[str, Any] | None:
        """Detect which known app is currently running via REST."""
        ip = self._ensure_ip()
        for name, ids in APP_ALIASES.items():
            try:
                raw = urlopen(
                    f"http://{ip}:8001/api/v2/applications/{ids[0]}",
                    timeout=2,
                ).read()
                data = json.loads(raw)
                if data.get("running"):
                    return {
                        "name": name,
                        "id": ids[0],
                        "visible": data.get("visible", False),
                    }
            except Exception:
                continue
        return None

    # ── Aspect Ratio ──────────────────────────────────────────

    def get_aspect_ratio(self) -> str:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_GetAspectRatio", "<InstanceID>0</InstanceID>",
        )
        return _soap_value(xml, "AspectRatio") or "Unknown"

    def set_aspect_ratio(self, ratio: str) -> None:
        ip = self._ensure_ip()
        _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_SetAspectRatio",
            f"<InstanceID>0</InstanceID><AspectRatio>{ratio}</AspectRatio>",
        )

    # ── Captions ──────────────────────────────────────────────

    def get_captions(self) -> dict[str, str]:
        ip = self._ensure_ip()
        xml = _soap_call(
            ip, "/upnp/control/RenderingControl1", "RenderingControl",
            "X_GetCaptionState", "<InstanceID>0</InstanceID>",
        )
        return {
            "captions": _soap_value(xml, "Captions") or "",
            "enabled": _soap_value(xml, "EnabledCaptions") or "",
        }

    def close(self) -> None:
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
