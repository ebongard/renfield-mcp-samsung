"""Samsung Smart TV MCP Server — Control your TV with natural language."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from tv import SamsungTV, discover

# Log to stderr — stdout is reserved for the MCP stdio protocol.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("samsung-tv")

mcp = FastMCP(
    "samsung-tv",
    host=os.getenv("MCP_HOST", "0.0.0.0"),
    port=int(os.getenv("MCP_PORT", "9092")),
)
_tv = SamsungTV()
_pool = ThreadPoolExecutor(max_workers=2)
TOOL_TIMEOUT = 10


def _ok(message: str = "Done", **data: Any) -> dict[str, Any]:
    return {"success": True, "message": message, **data}


def _err(message: str) -> dict[str, Any]:
    return {"success": False, "message": message}


async def _safe(fn, *args, timeout: float = TOOL_TIMEOUT, **kwargs) -> dict[str, Any]:
    """Run sync function in thread with hard timeout. Never blocks the MCP."""
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(_pool, partial(fn, *args, **kwargs)),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        log.error("Tool timed out after %ss", timeout)
        return _err(f"TV did not respond within {timeout}s. Is it on and reachable?")
    except Exception as e:
        log.error("Tool error: %s", e)
        return _err(str(e))


# ── Discovery & Info ─────────────────────────────────────────────


@mcp.tool()
async def tv_discover() -> dict[str, Any]:
    """Scan the local network for Samsung Smart TVs via SSDP.

    Returns a list of found TVs with their IP, name, and model.
    """
    return await _safe(lambda: _ok("Scan complete", tvs=discover()))


@mcp.tool()
async def tv_info() -> dict[str, Any]:
    """Get TV status: model, IP, power state, current volume, resolution.

    Use this first to verify the TV is reachable and powered on.
    """
    return await _safe(lambda: _ok("TV info retrieved", **_tv.info()))


# ── Power ────────────────────────────────────────────────────────


@mcp.tool()
async def tv_power(action: str = "off") -> dict[str, Any]:
    """Turn the TV on or off.

    Args:
        action: "on" (Wake-on-LAN) or "off" (power key). Default "off".
    """
    def _do():
        if action.lower() == "on":
            _tv.power_on()
            return _ok("Wake-on-LAN packet sent. TV should turn on shortly.")
        _tv.power_off()
        return _ok("TV power off command sent")
    return await _safe(_do)


# ── Remote Keys ──────────────────────────────────────────────────


@mcp.tool()
async def tv_key(key: str, times: int = 1) -> dict[str, Any]:
    """Send a remote control key press to the TV.

    Args:
        key: Key name like "VOLUP", "MUTE", "POWER", "HDMI", "PLAY", etc.
             The KEY_ prefix is added automatically if missing.
        times: Number of times to press the key (default 1).

    Common keys: POWER, VOLUP, VOLDOWN, MUTE, CHUP, CHDOWN, SOURCE, HDMI,
    UP, DOWN, LEFT, RIGHT, ENTER, RETURN, EXIT, HOME, MENU, GUIDE, INFO,
    PLAY, PAUSE, STOP, FF, REWIND, RED, GREEN, YELLOW, BLUE, 0-9,
    PMODE, DYNAMIC, STANDARD, MOVIE1, GAME, SLEEP, CAPTION, APP_LIST.
    """
    def _do():
        _tv.send_key(key, times)
        return _ok(f"Sent {key} x{times}")
    t = max(TOOL_TIMEOUT, times * 0.3 + 5)
    return await _safe(_do, timeout=t)


@mcp.tool()
async def tv_keys(keys: str, delay: float = 0.3) -> dict[str, Any]:
    """Send a sequence of key presses with configurable delay between them.

    Args:
        keys: Comma-separated key names, e.g. "HOME, DOWN, DOWN, ENTER".
        delay: Seconds between each key press (default 0.3).

    Use this for menu navigation or complex sequences.
    """
    key_list = [k.strip() for k in keys.split(",") if k.strip()]
    # Allow more time for sequences
    t = max(TOOL_TIMEOUT, len(key_list) * delay + 5)
    def _do():
        _tv.send_keys(key_list, delay)
        return _ok(f"Sent {len(key_list)} keys: {keys}")
    return await _safe(_do, timeout=t)


@mcp.tool()
async def tv_navigate(action: str) -> dict[str, Any]:
    """Quick navigation with semantic names instead of raw key codes.

    Args:
        action: One of: home, back, exit, menu, source, guide, info, tools,
                up, down, left, right, enter/ok, play, pause, stop, ff, rewind.
    """
    def _do():
        _tv.navigate(action)
        return _ok(f"Navigated: {action}")
    return await _safe(_do)


# ── Volume & Channel ─────────────────────────────────────────────


@mcp.tool()
async def tv_volume(
    level: Optional[int] = None,
    action: Optional[str] = None,
) -> dict[str, Any]:
    """Control TV volume. Get current volume, set to exact level, or mute/unmute.

    Args:
        level: Set volume to this exact value (0-100). Omit to just read.
        action: "up", "down", "mute", "unmute". Omit to just read or use level.

    Examples: tv_volume() -> get current, tv_volume(level=25) -> set to 25,
    tv_volume(action="mute") -> toggle mute.
    """
    def _do():
        if level is not None:
            _tv.set_volume(level)
            return _ok(f"Volume set to {level}", volume=level)
        if action:
            a = action.lower()
            if a == "up":
                _tv.send_key("KEY_VOLUP")
                return _ok("Volume up")
            if a == "down":
                _tv.send_key("KEY_VOLDOWN")
                return _ok("Volume down")
            if a == "mute":
                _tv.set_mute(True)
                return _ok("Muted")
            if a == "unmute":
                _tv.set_mute(False)
                return _ok("Unmuted")
            return _err(f"Unknown action '{a}'. Use: up, down, mute, unmute")
        vol = _tv.get_volume()
        muted = _tv.get_mute()
        return _ok(f"Volume: {vol}, Muted: {muted}", volume=vol, muted=muted)
    return await _safe(_do)


@mcp.tool()
async def tv_channel(
    number: Optional[int] = None,
    direction: Optional[str] = None,
) -> dict[str, Any]:
    """Change TV channel by number or direction.

    Args:
        number: Channel number to switch to (e.g. 42).
        direction: "up" or "down" to go to next/previous channel.

    Provide either number or direction, not both.
    """
    def _do():
        _tv.channel(number, direction)
        return _ok("Channel changed")
    return await _safe(_do)


# ── Apps ─────────────────────────────────────────────────────────


@mcp.tool()
async def tv_apps() -> dict[str, Any]:
    """List all installed apps on the TV with their IDs.

    Returns app names and IDs that can be used with tv_launch.
    """
    return _ok("Apps retrieved", apps=_tv.list_apps())


@mcp.tool()
async def tv_launch(
    app: str,
    deep_link: Optional[str] = None,
) -> dict[str, Any]:
    """Launch an app on the TV by name or ID.

    Args:
        app: App name ("netflix", "youtube", "spotify", "disney", "prime",
             "browser", "plex", "hbo", "max") or app ID.
        deep_link: Optional deep link parameter to open specific content.

    The app name is case-insensitive and supports fuzzy matching.
    """
    def _do():
        _tv.launch_app(app, deep_link)
        return _ok(f"Launched {app}")
    return await _safe(_do)


@mcp.tool()
async def tv_close_app(app: str) -> dict[str, Any]:
    """Close a running app on the TV.

    Args:
        app: App name or ID (same as tv_launch).
    """
    def _do():
        _tv.close_app(app)
        return _ok(f"Closed {app}")
    return await _safe(_do)


# ── Current App ─────────────────────────────────────────────


@mcp.tool()
async def tv_current_app() -> dict[str, Any]:
    """Detect which app is currently running on the TV.

    Checks all known apps via REST API. Returns the running app's
    name, ID, and visibility, or a message if no known app is active.
    """
    def _do():
        app = _tv.current_app()
        if app:
            return _ok(f"Running: {app['name']}", **app)
        return _ok("No known app is currently running")
    return await _safe(_do, timeout=40)


# ── Aspect Ratio ────────────────────────────────────────────


@mcp.tool()
async def tv_aspect_ratio(ratio: Optional[str] = None) -> dict[str, Any]:
    """Get or set the TV's aspect ratio.

    Args:
        ratio: Set to this value. Known values: "Default", "16:9", "Zoom",
               "4:3", "Screen Fit". Omit to just read current ratio.
    """
    def _do():
        if ratio:
            _tv.set_aspect_ratio(ratio)
            return _ok(f"Aspect ratio set to {ratio}", ratio=ratio)
        current = _tv.get_aspect_ratio()
        return _ok(f"Aspect ratio: {current}", ratio=current)
    return await _safe(_do)


# ── Captions ────────────────────────────────────────────────


@mcp.tool()
async def tv_captions(toggle: bool = False) -> dict[str, Any]:
    """Get caption/subtitle state or toggle captions on/off.

    Args:
        toggle: If True, sends the CAPTION key to toggle subtitles.
                If False (default), just returns current caption state.
    """
    def _do():
        if toggle:
            _tv.send_key("KEY_CAPTION")
            return _ok("Caption toggled")
        state = _tv.get_captions()
        return _ok("Caption state", **state)
    return await _safe(_do)


# ── Browser & Text ───────────────────────────────────────────────


@mcp.tool()
async def tv_browser(url: str) -> dict[str, Any]:
    """Open a URL in the TV's built-in web browser.

    Args:
        url: The full URL to open (e.g. "https://google.com").
    """
    def _do():
        _tv.open_browser(url)
        return _ok(f"Opened {url}")
    return await _safe(_do)


@mcp.tool()
async def tv_text(text: str) -> dict[str, Any]:
    """Type text into the currently active input field on the TV.

    Args:
        text: The text to type. Only works when a text input is active
              (virtual keyboard is visible on the TV screen).
    """
    def _do():
        _tv.send_text(text)
        return _ok(f"Typed text ({len(text)} chars)")
    return await _safe(_do)


# ── Cursor ───────────────────────────────────────────────────────


@mcp.tool()
async def tv_cursor(x: int, y: int, duration: int = 500) -> dict[str, Any]:
    """Move the virtual cursor/pointer on the TV screen.

    Args:
        x: Horizontal position (pixels from left).
        y: Vertical position (pixels from top).
        duration: Movement duration in milliseconds (default 500).
    """
    def _do():
        _tv.move_cursor(x, y, duration)
        return _ok(f"Cursor moved to ({x}, {y})")
    return await _safe(_do)


# ── DLNA Media ───────────────────────────────────────────────────


@mcp.tool()
async def tv_media(
    action: str = "status",
    url: Optional[str] = None,
    title: Optional[str] = None,
    seek_to: Optional[str] = None,
) -> dict[str, Any]:
    """Play media on the TV via DLNA or control current playback.

    Args:
        action: "play_url" to start playing from URL, or "play", "pause",
                "stop", "seek", "status" to control current media.
        url: Media URL (required for "play_url"). Supports video, audio, images.
        title: Display title for the media (optional, default "Media").
        seek_to: Time position for seek, format "HH:MM:SS" (e.g. "00:05:30").

    Examples:
        tv_media(action="play_url", url="http://server/video.mp4")
        tv_media(action="pause")
        tv_media(action="seek", seek_to="00:10:00")
        tv_media(action="status") -> returns current position and state.
    """
    def _do():
        if action == "play_url":
            if not url:
                return _err("URL required for play_url action")
            _tv.play_media(url, title or "Media")
            return _ok(f"Playing: {url}")
        if action == "seek" and not seek_to:
            return _err("seek_to required for seek action (format HH:MM:SS)")
        result = _tv.media_control(action, target=seek_to)
        if action == "status":
            return _ok("Playback status", **result)
        return _ok(f"Media {action} executed", **result)
    return await _safe(_do)


def main() -> None:
    """Entry point. Transport via MCP_TRANSPORT:
      - "stdio" (default): MCP stdio protocol (subprocess for a local client)
      - "streamable-http": HTTP service so the Renfield backend (in Docker) can
        reach it on the host at host.docker.internal:$MCP_PORT/mcp
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        log.info("Starting Samsung TV MCP on %s:%s (streamable-http)",
                 mcp.settings.host, mcp.settings.port)
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
