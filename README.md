# renfield-mcp-samsung

A dedicated MCP server for **Samsung Smart TV (Tizen)** control — the companion
to `renfield-mcp-dlna`. Where the DLNA server speaks pure UPnP/AVTransport (great
for always-on streamers like Linn/Sonos/HiFiBerry), Samsung TVs need their own
vendor channels: a **websocket remote** (`:8002`) for power/keys/apps, plus
**Wake-on-LAN** to power on from standby, with **DLNA** (`:9197`) only for media
playback and volume. This server isolates all of that.

> **Why a separate server?** Waking and controlling a TV is vendor-specific and
> is not a DLNA operation — see the architecture notes in
> `../renfield-mcp-dlna/tasks/todo.md`. Home Assistant and Homebridge drive
> Samsung TVs the same way (websocket + WoL), via the same `samsungtvws` library
> this server is built on.

## Attribution

Adapted from [**andresgarcia0313/samsung-tv-mcp**](https://github.com/andresgarcia0313/samsung-tv-mcp)
(MIT). Built on [`samsungtvws`](https://github.com/xchwarze/samsung-tv-ws-api),
the library Home Assistant's Samsung integration uses.

Adaptations in this fork:
- **Env-driven config** — `SAMSUNG_TV_HOST` (pin a TV by IP, no SSDP needed),
  `SAMSUNG_CLIENT_NAME` (the identity shown in the TV's pairing popup; default
  `Renfield`), `SAMSUNG_TOKEN_FILE` / `RENFIELD_STATE_DIR` (token persistence
  consistent with the DLNA server).
- **streamable-http transport** (`MCP_TRANSPORT=streamable-http`, `MCP_PORT`,
  default 9092) so the Renfield backend in Docker can reach it on the host.
- **Robust discovery** — finds a TV even when its DLNA renderer (`:9197`) isn't
  advertising (common!), by also searching the DIAL/remote SSDP targets and
  identifying each host via its always-on `:8001` REST endpoint.
- Logging pinned to stderr (stdio-safe).

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"

# stdio (default) — as a subprocess for a local MCP client
.venv/bin/python main.py

# HTTP service for the Renfield backend (Docker → host.docker.internal:9092/mcp)
MCP_TRANSPORT=streamable-http MCP_PORT=9092 .venv/bin/python main.py

# pin a specific TV (skip SSDP)
SAMSUNG_TV_HOST=192.168.1.47 .venv/bin/python main.py

.venv/bin/python -m pytest   # tests
```

## One-time pairing (required)

Samsung TVs require a one-time authorization. On the first websocket connection
the TV shows an **"Allow &lt;CLIENT_NAME&gt;?"** popup; approving it issues a token
saved to `$RENFIELD_STATE_DIR/token.json` (default
`~/.local/state/renfield-mcp-samsung/`).

For the popup to appear and the DLNA renderer to be available, on the TV enable:
**Settings → General → External Device Manager → Device Connect Manager**
- **Access Notification → On** (lets the "Allow?" popup show)
- **Device List** → remove any stale entry for the client name
- **Device access → Allow** (brings up the `:9197` DLNA renderer for media/volume)

Pair while the TV is **on and on the Smart Hub home screen** — Samsung suppresses
the popup during fullscreen playback.

## Tools

Discovery/info, power (WoL on / key off), remote keys & navigation, volume &
mute, channel, app list/launch/close, current app, aspect ratio, captions,
browser, text input, cursor, and DLNA media (`play_url`/play/pause/stop/seek/
status). Control tools work over the websocket even when `:9197` is down; media
and volume need the DLNA renderer up (Device access → Allow).

## Validation status

Tested live on two real sets:

| TV | Discovery | Info | Volume/mute (DLNA) | **Media playback (DLNA)** | Websocket (keys/apps) |
|---|---|---|---|---|---|
| **Samsung UE65MU8009** (2018, 65") | ✅ | ✅ | ✅ | ✅ **play→PLAYING→stop** | ⛔ pairing (needs Access Notification On) |
| Samsung Q60CA 43 (2023) | ✅ | ✅ | n/a (`:9197` down) | n/a (`:9197` down) | ⛔ pairing |

Key finding: a Samsung's DLNA renderer (`:9197`) is **per-set unreliable** — up on the
2018 MU8009 (full media + volume work *without* any pairing), down on the 2023 Q60CA.
Websocket control (keys/apps/power-off) needs the one-time pairing above; **Wake-on-LAN
power-on and DLNA media/volume do not.**

## Architecture

```
main.py  →  FastMCP tool layer (thin); transport from MCP_TRANSPORT
tv.py    →  SamsungTV client: websocket (samsungtvws) + REST + UPnP SOAP + WoL + SSDP
```

- **WebSocket** (8002/8001): remote keys, apps, browser, text, cursor, power-off
- **REST** (8001): device info, app launch/close, current app, discovery identity
- **UPnP SOAP** (9197): volume/mute, DLNA media playback + transport
- **Wake-on-LAN**: power on from standby

## Known limitations

- WoL requires "Power On with Mobile" enabled on the TV.
- `:9197` (media/volume) is only up when Device Connect → Device access is on;
  control tools degrade gracefully without it.
- Tested upstream on a 2020 TU7000; this fork targets a 2023 Q60CA. DRM content
  can't be streamed via DLNA.
