"""Unit tests for the SamsungTV client (network mocked)."""

import io
import json
from unittest.mock import MagicMock, patch

import pytest

import tv


def _fake_urlopen(payload: dict):
    return io.BytesIO(json.dumps(payload).encode())


# ── discovery / identify ─────────────────────────────────────────
class TestIdentify:
    def test_identify_samsung_tizen(self):
        payload = {"device": {
            "ip": "192.168.1.47", "name": "Samsung Q60CA 43",
            "modelName": "GQ43Q60CAUXZG", "OS": "Tizen",
            "type": "Samsung SmartTV", "wifiMac": "a0:d7:f3:08:66:85",
            "PowerState": "standby",
        }}
        with patch.object(tv, "urlopen", return_value=_fake_urlopen(payload)):
            info = tv._identify("192.168.1.47")
        assert info["ip"] == "192.168.1.47"
        assert info["model"] == "GQ43Q60CAUXZG"
        assert info["mac"] == "a0:d7:f3:08:66:85"
        assert info["power"] == "standby"

    def test_identify_non_samsung_returns_none(self):
        payload = {"device": {"name": "Roku", "OS": "RokuOS", "type": "Roku"}}
        with patch.object(tv, "urlopen", return_value=_fake_urlopen(payload)):
            assert tv._identify("192.168.1.5") is None

    def test_identify_unreachable_returns_none(self):
        with patch.object(tv, "urlopen", side_effect=OSError("refused")):
            assert tv._identify("192.168.1.9") is None

    def test_discover_dedupes_by_ip(self):
        info = {"ip": "192.168.1.47", "name": "Q60", "model": "X",
                "manufacturer": "Samsung Electronics", "mac": "", "power": ""}
        with patch.object(tv, "_ssdp_hosts", return_value={"192.168.1.47", "1.2.3.4"}), \
             patch.object(tv, "_identify", side_effect=lambda h: info if h == "192.168.1.47" else None):
            result = tv.discover()
        assert len(result) == 1 and result[0]["ip"] == "192.168.1.47"


# ── Wake-on-LAN ──────────────────────────────────────────────────
class TestWol:
    def test_magic_packet(self):
        captured = {}

        class _Sock:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def setsockopt(self, *a): pass
            def sendto(self, payload, addr): captured.update(payload=payload, addr=addr)

        with patch.object(tv.socket, "socket", return_value=_Sock()):
            tv.wake_on_lan("a0:d7:f3:08:66:85")
        assert captured["payload"][:6] == b"\xff" * 6
        assert captured["payload"][6:12] == bytes.fromhex("a0d7f3086685")
        assert len(captured["payload"]) == 102
        assert captured["addr"] == ("255.255.255.255", 9)


# ── app id resolution ────────────────────────────────────────────
class TestAppResolve:
    def setup_method(self):
        self.tv = tv.SamsungTV("10.0.0.1")

    def test_alias(self):
        assert self.tv._resolve_app_id("netflix") == tv.APP_ALIASES["netflix"][0]

    def test_alias_case_insensitive(self):
        assert self.tv._resolve_app_id("YouTube") == tv.APP_ALIASES["youtube"][0]

    def test_raw_numeric_id_passthrough(self):
        assert self.tv._resolve_app_id("3201907018807") == "3201907018807"

    def test_fuzzy_substring(self):
        # "apple" should resolve to the "apple tv" alias
        assert self.tv._resolve_app_id("apple") == tv.APP_ALIASES["apple tv"][0]


# ── navigate / soap parsing ──────────────────────────────────────
class TestNavigateAndSoap:
    def test_navigate_unknown_raises(self):
        t = tv.SamsungTV("10.0.0.1")
        with pytest.raises(ValueError):
            t.navigate("teleport")

    def test_navigate_sends_mapped_key(self):
        t = tv.SamsungTV("10.0.0.1")
        with patch.object(t, "_send_ws") as send:
            t.navigate("home")
        send.assert_called_once()
        assert send.call_args.kwargs.get("key") == "KEY_HOME" or "KEY_HOME" in send.call_args.args

    def test_soap_value(self):
        xml = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
               '<s:Body><u:GetVolumeResponse xmlns:u="urn:schemas-upnp-org:service:RenderingControl:1">'
               '<CurrentVolume>33</CurrentVolume></u:GetVolumeResponse>'
               '</s:Body></s:Envelope>')
        assert tv._soap_value(xml, "CurrentVolume") == "33"


# ── config / env ─────────────────────────────────────────────────
class TestConfig:
    def test_env_host_used(self, monkeypatch):
        monkeypatch.setattr(tv, "DEFAULT_TV_HOST", "192.168.9.9")
        assert tv.SamsungTV()._ip == "192.168.9.9"

    def test_explicit_ip_wins(self):
        assert tv.SamsungTV("10.0.0.5")._ip == "10.0.0.5"
