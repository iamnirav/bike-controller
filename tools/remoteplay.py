#!/usr/bin/env python3
"""Open Xbox Remote Play and click through to the console, unattended.

    python3 tools/remoteplay.py                 # launch and connect
    python3 tools/remoteplay.py --dry-run       # find the button, do not click

Why this exists: the Pi runs headless next to the bike, so there is no way to
click "Click to start playing" without VNCing in from another device. The
controller cannot help either -- the bridge grabs it exclusively.

The click is dispatched through the DevTools Protocol as a real mouse event at
the button's coordinates, not `element.click()`. A synthetic DOM click is not a
trusted user gesture and some browser APIs ignore it; a dispatched input event
is indistinguishable from a real one.

Fragility, stated plainly: this depends on the button's visible text. If
Microsoft rewords it, this stops working. It fails loudly rather than silently,
and `--dry-run` tells you what it can currently see.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

# Built from XBOX_CONSOLE_ID rather than hardcoded: the console ID identifies a
# specific person's Xbox, and this repo is public.
CONSOLE_ID = os.environ.get("XBOX_CONSOLE_ID", "").strip()
DEFAULT_URL = f"https://play.xbox.com/remoteplay/{CONSOLE_ID}" if CONSOLE_ID else ""
DEFAULT_PORT = 9222
BUTTON_TEXT = "click to start playing"

# After clicking, the page negotiates a WebRTC stream. Clicking is NOT the same
# as streaming -- reporting success at the click is what made a stall look like
# a win. This checks for a <video> that is actually playing frames.
STREAM_CHECK_JS = """
(() => {
  const v = document.querySelector('video');
  if (!v) return JSON.stringify({streaming: false, why: 'no video element'});
  return JSON.stringify({
    streaming: v.readyState >= 2 && !v.paused && v.currentTime > 0,
    readyState: v.readyState, paused: v.paused,
    currentTime: Math.round(v.currentTime * 10) / 10,
    size: v.videoWidth + 'x' + v.videoHeight,
  });
})()
"""

# Finds the button by visible text and reports its centre in viewport pixels.
FIND_BUTTON_JS = """
(() => {
  const wanted = %s;
  const nodes = Array.from(document.querySelectorAll('*'));
  const hits = [];
  for (const el of nodes) {
    const text = (el.innerText || el.textContent || '').trim().toLowerCase();
    if (!text.includes(wanted)) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    // The dialog exists in the DOM behind the loading screens, so display and
    // visibility alone are not enough -- it is also hidden by opacity and by
    // overlays sitting on top of it.
    if (parseFloat(style.opacity || '1') < 0.05) continue;
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) continue;
    // Decisive test: hit-test the click point. If a loading overlay covers the
    // dialog, elementFromPoint returns the overlay and a click would hit that
    // instead -- which is exactly the bug this replaces.
    const top = document.elementFromPoint(x, y);
    if (!top) continue;
    if (!(el.contains(top) || top.contains(el))) continue;
    const tag = el.tagName.toLowerCase();
    const clickable = tag === 'button' || tag === 'a' ||
                      el.getAttribute('role') === 'button' ||
                      style.cursor === 'pointer';
    hits.push({tag, clickable, area: r.width * r.height, x, y,
               text: text.slice(0, 60)});
  }
  if (!hits.length) {
    const body = (document.body ? document.body.innerText : '').replace(/\s+/g, ' ');
    return JSON.stringify({found: false, title: document.title, body: body.slice(0, 160)});
  }
  hits.sort((a, b) => (b.clickable - a.clickable) || (a.area - b.area));
  const best = hits[0];
  best.found = true;
  best.candidates = hits.length;
  return JSON.stringify(best);
})()
"""


def cdp_targets(port: int) -> list[dict]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5) as response:
        return json.load(response)


def wait_for_cdp(port: int, timeout: float) -> list[dict] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return cdp_targets(port)
        except Exception:                          # noqa: BLE001 - not up yet
            time.sleep(0.5)
    return None


def kill_chromium() -> None:
    """Stop every Chromium process and wait for the port to free up.

    No -x: /proc/<pid>/comm is capped at 15 chars, so "chromium-browser"
    appears as "chromium-browse" and an exact match would miss it.
    """
    subprocess.run(["pkill", "chromium"], capture_output=True)
    time.sleep(2.0)


def launch_chromium(url: str, port: int) -> subprocess.Popen | None:
    binary = shutil.which("chromium") or shutil.which("chromium-browser")
    if binary is None:
        print("chromium not found on PATH")
        return None
    print(f"launching {binary}")
    return subprocess.Popen(
        [binary, f"--remote-debugging-port={port}",
         "--remote-allow-origins=*", "--start-maximized",
         # With no physical display and nobody attached over VNC, Chromium can
         # treat the window as occluded and throttle its renderer -- which
         # stalls WebRTC setup, leaving the stream stuck "warming things up".
         "--disable-backgrounding-occluded-windows",
         "--disable-renderer-backgrounding",
         "--disable-background-timer-throttling",
         "--autoplay-policy=no-user-gesture-required",
         url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def wait_for_stream(send, timeout: float, quiet: bool = False) -> int:
    """Confirm a video is genuinely playing, not merely that we clicked."""
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        reply = await send("Runtime.evaluate", {
            "expression": STREAM_CHECK_JS, "returnByValue": True,
        })
        raw = reply.get("result", {}).get("result", {}).get("value")
        last = json.loads(raw) if raw else {}
        if last.get("streaming"):
            print(f"streaming: {last.get('size')} at t={last.get('currentTime')}s")
            return 0
        await asyncio.sleep(2.0)

    if not quiet:
        print(f"clicked, but no stream after {timeout:.0f}s. Last state: {last}")
        print("The console may be slow to wake, or the stream stalled. The "
              "browser is still open -- connect over VNC to see what it says.")
    return 2


async def drive(page_ws: str, dry_run: bool, timeout: float) -> int:
    import websockets

    async with websockets.connect(page_ws, max_size=None) as ws:
        message_id = 0

        async def send(method: str, params: dict | None = None, timeout: float = 20.0):
            nonlocal message_id
            message_id += 1
            await ws.send(json.dumps({"id": message_id, "method": method,
                                      "params": params or {}}))
            # Bound every request. Without this a stalled CDP connection hangs
            # here forever, outliving --timeout; the launcher task then never
            # completes and Launcher.trigger() refuses all further attempts,
            # disabling the Konami code until the bridge is restarted.
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} timed out after {timeout:.0f}s")
                reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=remaining))
                if reply.get("id") == message_id:
                    return reply

        await send("Runtime.enable")
        await send("Page.enable")

        deadline = time.monotonic() + timeout
        attempt = 0
        clicks = 0
        info: dict = {}
        while time.monotonic() < deadline:
            attempt += 1
            try:
                reply = await send("Runtime.evaluate", {
                    "expression": FIND_BUTTON_JS % json.dumps(BUTTON_TEXT),
                    "returnByValue": True,
                })
            except TimeoutError as exc:
                # One slow evaluate should cost an iteration, not the session.
                # The retry loop exists precisely to outlast a slow page.
                print(f"  {exc}; retrying", flush=True)
                await asyncio.sleep(1.0)
                continue
            raw = reply.get("result", {}).get("result", {}).get("value")
            info = json.loads(raw) if raw else {"found": False}

            if info.get("found"):
                print(f"found after {attempt} checks: <{info['tag']}> "
                      f"clickable={bool(info.get('clickable'))} "
                      f"area={info['area']:.0f}px ({info.get('candidates', 1)} candidates)")
                print(f"  text: {info['text']!r} at ({info['x']:.0f}, {info['y']:.0f})")
                if dry_run:
                    print("dry run: not clicking")
                    return 0

                for event in ("mousePressed", "mouseReleased"):
                    await send("Input.dispatchMouseEvent", {
                        "type": event, "x": info["x"], "y": info["y"],
                        "button": "left", "clickCount": 1,
                    })
                clicks += 1
                print(f"  click {clicks} dispatched; checking for a stream")

                # Verify rather than assume. The page shows loading screens
                # first and only then the real dialog, so an early click can
                # land on nothing -- in which case no stream starts and we come
                # back round and click the real one.
                try:
                    if await wait_for_stream(send, timeout=25.0, quiet=True) == 0:
                        return 0
                except TimeoutError as exc:
                    print(f"  {exc}; retrying", flush=True)
                print("  no stream yet; looking for the button again")
            elif attempt % 10 == 0:
                print(f"  waiting... title={info.get('title','?')!r} "
                      f"page={info.get('body','')[:70]!r}", flush=True)
            await asyncio.sleep(1.0)

        print(f"gave up after {timeout:.0f}s ({clicks} click(s) attempted).")
        print(f"last page title: {info.get('title','?')!r}")
        if info.get("body"):
            print(f"page text: {info['body'][:200]!r}")
        print("If the wording changed, update BUTTON_TEXT at the top of this file.")
        return 1


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL,
                        help="Remote Play URL; defaults to "
                             "XBOX_CONSOLE_ID from config.env")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--dry-run", action="store_true",
                        help="report whether the button is there, but do not click")
    parser.add_argument("--attempt-timeout", type=float, default=75.0,
                        help="seconds to wait per browser launch before "
                             "restarting it (default 75)")
    parser.add_argument("--attempts", type=int, default=3,
                        help="how many times to relaunch the browser (default 3). "
                             "A stalled Remote Play page is not fixed by "
                             "reloading, only by a fresh browser process.")
    parser.add_argument("--no-launch", action="store_true",
                        help="attach to an already-running Chromium")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if not args.url:
        print("No Xbox console ID. Set XBOX_CONSOLE_ID in config.env, or pass "
              "--url.\n"
              "Find it at https://www.xbox.com/play/consoles -- pick your "
              "console and copy\nthe ID from the URL: "
              "https://play.xbox.com/remoteplay/<THIS_PART>")
        return 1

    if not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY"):
        os.environ.setdefault("WAYLAND_DISPLAY", "wayland-0")
        os.environ.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")

    # Remote Play can wedge on "Warming things up for you..." and stay there.
    # Reloading the page does NOT clear it -- only a fresh browser process does,
    # so a stall means relaunch rather than keep polling a dead page.
    for attempt in range(1, args.attempts + 1):
        if not args.no_launch:
            if attempt > 1:
                print(f"\n=== attempt {attempt}/{args.attempts}: restarting Chromium ===")
            kill_chromium()
            if launch_chromium(args.url, args.port) is None:
                # Otherwise we wait 30s and then blame a stale Chromium
                # instance, misleading when the binary is simply not installed.
                return 1

        targets = wait_for_cdp(args.port, timeout=30.0)
        if targets is None:
            print(f"Chromium's debug port {args.port} never opened.")
            print("If Chromium was already running WITHOUT "
                  "--remote-debugging-port, close it first: pkill chromium")
            return 1

        page = next((t for t in targets
                     if t.get("type") == "page" and "remoteplay" in t.get("url", "")), None)
        if page is None:
            page = next((t for t in targets if t.get("type") == "page"), None)
        if page is None:
            print("no page target found in Chromium")
            return 1

        print(f"attached to: {page.get('url','')[:80]}")
        try:
            result = await drive(page["webSocketDebuggerUrl"], args.dry_run,
                                 args.attempt_timeout)
        except Exception as exc:                       # noqa: BLE001
            print(f"  attach failed ({type(exc).__name__}: {exc})")
            result = 1
        if result == 0 or args.dry_run or args.no_launch:
            return result

    print(f"\nGave up after {args.attempts} browser restarts. The console may be "
          f"off or asleep;\nRemote Play cannot wake one in energy-saving mode.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(1)
