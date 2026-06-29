#!/usr/bin/env python3
"""
Kill the post-playback idle hiss caused by Chromium/Brave keeping its audio
stream open (playing silence) for ~10s after you pause.

Strategy (plays NO audio — this is not a mask):
  - While a browser is PAUSED/STOPPED, move its PulseAudio stream onto a silent
    null sink. The real analog sink then goes idle and the codec suspends
    (~2s with power_save=1), so the hiss stops.
  - When the browser is PLAYING, move the stream back to the real output.
  - While a browser CALL is active (microphone/tab capture in use, e.g. Google
    Meet/Zoom), never park — otherwise the call audio would be silenced.

Signal: MPRIS PlaybackStatus over D-Bus (busctl/playerctl), the same mechanism
the mask daemon uses, plus a capture-stream check to detect calls. Only browser
streams are ever touched; other apps (music players, system sounds) are left
alone.

Commands:
  headphone-hiss-park.py          Run daemon
  headphone-hiss-park.py stop     Stop daemon and restore streams
  headphone-hiss-park.py status   Show current state

Trade-off: resuming a paused browser may clip the first ~0.1-0.2s while the
stream is moved back. If Brave audio ever breaks, run `stop` and report it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

NULL_SINK_NAME = "hiss_park"
POLL_SEC = 0.15
APP_NAME = "headphone-hiss-park"

BROWSER_BINARIES = frozenset(
    {
        "brave",
        "brave-browser",
        "chrome",
        "google-chrome",
        "chromium",
        "chromium-browser",
        "firefox",
        "vivaldi",
        "vivaldi-bin",
        "msedge",
        "opera",
    }
)
BROWSER_RE = re.compile(r"brave|chrom|firefox|vivaldi|edge|opera", re.I)


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()


def run_safe(*args: str, timeout: float = 1.0) -> str | None:
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def log(msg: str) -> None:
    print(f"[{APP_NAME}] {msg}", flush=True)


def get_default_sink() -> str | None:
    out = run_safe("pactl", "get-default-sink")
    if not out:
        return None
    name = out.strip()
    return name or None


def parse_blocks(text: str, header: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith(header):
            if current:
                blocks.append(current)
            current = {"id": line.split("#", 1)[1].strip()}
        elif current:
            stripped = line.strip()
            if stripped.startswith("Sink:"):
                current["sink"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Source:"):
                current["source"] = stripped.split(":", 1)[1].strip()
            elif stripped.startswith("Corked:"):
                current["corked"] = stripped.split(":", 1)[1].strip()
            elif " = " in stripped:
                key, val = stripped.split(" = ", 1)
                current[key] = val.strip().strip('"')
    if current:
        blocks.append(current)
    return blocks


def list_sink_inputs() -> list[dict[str, str]]:
    out = run_safe("pactl", "list", "sink-inputs")
    if not out:
        return []
    return parse_blocks(out, "Sink Input #")


def list_source_outputs() -> list[dict[str, str]]:
    out = run_safe("pactl", "list", "source-outputs")
    if not out:
        return []
    return parse_blocks(out, "Source Output #")


def browser_recording() -> bool:
    """True if a browser is actively capturing audio (microphone/tab) — i.e. a
    call such as Google Meet, Zoom, Discord. Plain media playback (YouTube) never
    opens a capture stream, so this cleanly distinguishes a call from a paused
    video. While a call is active we must NOT park the browser's audio, or the
    call audio would be silenced."""
    for so in list_source_outputs():
        if is_browser(so) and so.get("corked") != "yes":
            return True
    return False


def is_browser(si: dict[str, str]) -> bool:
    binary = si.get("application.process.binary", "").lower()
    name = si.get("application.name", "")
    if binary in BROWSER_BINARIES:
        return True
    return bool(BROWSER_RE.search(binary) or BROWSER_RE.search(name))


def null_sink_index() -> str | None:
    out = run_safe("pactl", "list", "sinks", "short")
    if not out:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == NULL_SINK_NAME:
            return parts[0]
    return None


def ensure_null_sink() -> tuple[str | None, str | None]:
    """Return (null_sink_index, owned_module_id). owned_module_id is set only
    if we created it (so we can unload on exit)."""
    idx = null_sink_index()
    if idx is not None:
        return idx, None
    out = run_safe(
        "pactl",
        "load-module",
        "module-null-sink",
        f"sink_name={NULL_SINK_NAME}",
        "sink_properties=device.description=Hiss-Park-(silent)",
        timeout=3.0,
    )
    module_id = out.strip() if out else None
    return null_sink_index(), module_id


def _browser_mpris_players() -> list[str]:
    out = run_safe("busctl", "--user", "list", timeout=0.8)
    if not out:
        return []
    players: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        dest = parts[0]
        if dest.startswith("org.mpris.MediaPlayer2.") and BROWSER_RE.search(dest):
            players.append(dest)
    return players


def browser_playing() -> bool | None:
    """True if a browser MPRIS player is Playing; False if browser players
    exist but none playing; None if undeterminable."""
    players = _browser_mpris_players()
    if not players:
        return None
    seen = False
    for dest in players:
        st = run_safe(
            "busctl",
            "--user",
            "call",
            dest,
            "/org/mpris/MediaPlayer2",
            "org.freedesktop.DBus.Properties",
            "Get",
            "ss",
            "org.mpris.MediaPlayer2.Player",
            "PlaybackStatus",
            timeout=0.8,
        )
        if st is None:
            continue
        seen = True
        if '"Playing"' in st:
            return True
    return False if seen else None


def move_input(input_id: str, sink: str) -> bool:
    proc = subprocess.run(
        ["pactl", "move-sink-input", input_id, sink],
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


def restore_all(real_sink: str | None, nidx: str | None) -> None:
    if not real_sink or nidx is None:
        return
    for si in list_sink_inputs():
        if is_browser(si) and si.get("sink") == nidx:
            move_input(si["id"], real_sink)


def _pgrep_others() -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", "headphone-hiss-park"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    me = os.getpid()
    return [
        int(p)
        for p in proc.stdout.split()
        if p.strip().isdigit() and int(p) != me
    ]


def cmd_stop() -> int:
    nidx = null_sink_index()
    real_sink = get_default_sink()
    restore_all(real_sink, nidx)
    for pid in _pgrep_others():
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    log("stopped; browser streams restored to the real output")
    return 0


def cmd_status() -> int:
    nidx = null_sink_index()
    real_sink = get_default_sink()
    print(f"null sink ({NULL_SINK_NAME}) index: {nidx}")
    print(f"default (real) sink: {real_sink}")
    print(f"browser playing: {browser_playing()}")
    print(f"browser capturing (call): {browser_recording()}")
    print("browser streams:")
    for si in list_sink_inputs():
        if is_browser(si):
            where = "PARKED" if si.get("sink") == nidx else "live"
            print(
                f"  #{si['id']} {si.get('application.name','?')} "
                f"sink={si.get('sink')} corked={si.get('corked')} -> {where}"
            )
    return 0


def cmd_daemon() -> int:
    nidx, owned_module = ensure_null_sink()
    if nidx is None:
        log("failed to create null sink; is PulseAudio/PipeWire running?")
        return 1
    real_sink = get_default_sink()
    log(f"running. null_sink={nidx} real_sink={real_sink} (Ctrl+C to stop)")

    stopping = False

    def on_stop(_s: int, _f: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    last_state: str | None = None
    while not stopping:
        try:
            current_default = get_default_sink()
            if current_default and current_default != NULL_SINK_NAME:
                real_sink = current_default

            browser = [si for si in list_sink_inputs() if is_browser(si)]
            if browser:
                playing = browser_playing()
                if playing is True:
                    for si in browser:
                        if si.get("sink") == nidx:
                            if move_input(si["id"], real_sink):
                                log(f"unpark #{si['id']} -> {real_sink}")
                    last_state = "playing"
                elif playing is False:
                    if browser_recording():
                        # A browser call (Meet/Zoom/...) is capturing audio.
                        # Parking would silence the call, so leave streams live.
                        if last_state != "recording":
                            log("browser is capturing audio (call) -> not parking")
                        last_state = "recording"
                    else:
                        for si in browser:
                            if si.get("sink") != nidx and si.get("corked") != "yes":
                                if move_input(si["id"], nidx):
                                    log(f"park   #{si['id']} (paused) -> {NULL_SINK_NAME}")
                        last_state = "paused"
        except Exception as exc:  # keep the daemon alive
            log(f"warn: {exc}")
        time.sleep(POLL_SEC)

    restore_all(real_sink, nidx)
    if owned_module:
        run_safe("pactl", "unload-module", owned_module, timeout=3.0)
    log("exited; streams restored")
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "stop":
            return cmd_stop()
        if cmd == "status":
            return cmd_status()
        if cmd in ("-h", "--help", "help"):
            print(__doc__)
            return 0
        print(f"Unknown command: {sys.argv[1]}", file=sys.stderr)
        return 1
    return cmd_daemon()


if __name__ == "__main__":
    sys.exit(main())
