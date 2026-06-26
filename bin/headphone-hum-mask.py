#!/usr/bin/env python3
"""
Mask subtle headphone idle hiss (wind/white-noise type) with quiet shaped noise.

Default mode is band-limited pink noise — not tonal beeps. Use `tune` to match
volume and brightness. Legacy sine mode: set "mode": "tone" in config.

Commands:
  headphone-hum-mask.py          Run mask when headphones idle
  headphone-hum-mask.py tune     Tune noise mask (v/h/l/c keys)
  headphone-hum-mask.py stop     Stop all playback
  headphone-hum-mask.py test     3s noise sample
  headphone-hum-mask.py invert   Inverted saved noise (cancel experiment)
  headphone-hum-mask.py analyze  Inspect idle hiss

Config: ~/.config/headphone-hum-mask/config.json
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "headphone-hum-mask"
CONFIG_PATH = Path.home() / ".config/headphone-hum-mask/config.json"
POLL_SEC = 0.01
DEFAULT_PREVIEW_LEVEL = 0.01
MIN_VOLUME = 1e-7
MAX_VOLUME = 0.5
HEADPHONE_RE = re.compile(r"headphone", re.I)

BROWSER_BINARIES = frozenset(
    {"brave", "chrome", "chromium", "firefox", "vivaldi", "msedge", "opera"}
)
NOISE_COLORS = ("pink", "white", "brown", "violet")

DEFAULT_CONFIG = {
    "mode": "noise",
    "peak_threshold": 2500,
    "noise": {
        "color": "pink",
        "volume": 0.0005,
        "highpass_hz": 200,
        "lowpass_hz": 5000,
    },
    "tones": [
        {"freq": 400, "volume": 0.04, "phase": 0.0},
        {"freq": 800, "volume": 0.025, "phase": 0.0},
    ],
}


def load_config() -> dict:
    if CONFIG_PATH.is_file():
        with CONFIG_PATH.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = json.loads(json.dumps(DEFAULT_CONFIG))

    noise_in = {**DEFAULT_CONFIG["noise"], **data.get("noise", {})}
    tones = data.get("tones", DEFAULT_CONFIG["tones"])
    cfg = {
        "mode": data.get("mode", DEFAULT_CONFIG["mode"]),
        "peak_threshold": int(data.get("peak_threshold", DEFAULT_CONFIG["peak_threshold"])),
        "noise": {
            "color": str(noise_in.get("color", "pink")),
            "volume": float(noise_in.get("volume", 0.03)),
            "highpass_hz": int(noise_in.get("highpass_hz", 200)),
            "lowpass_hz": int(noise_in.get("lowpass_hz", 5000)),
        },
        "tones": [
            {
                "freq": float(t["freq"]),
                "volume": float(t["volume"]),
                "phase": float(t.get("phase", 0.0)),
            }
            for t in tones
        ],
    }
    if cfg["noise"]["color"] not in NOISE_COLORS:
        cfg["noise"]["color"] = "pink"
    if cfg["noise"]["lowpass_hz"] <= cfg["noise"]["highpass_hz"]:
        cfg["noise"]["lowpass_hz"] = cfg["noise"]["highpass_hz"] + 500
    if "tune_preview_level" in data:
        cfg["tune_preview_level"] = float(data["tune_preview_level"])
    return cfg


def save_config(cfg: dict, *, quiet: bool = False) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    if not quiet:
        print(f"Saved {CONFIG_PATH}")


def read_tty_line(prompt: str = "> ") -> str:
    """Read from the terminal (ffmpeg/paplay must not use stdin)."""
    try:
        with open("/dev/tty", "r+", encoding="utf-8", buffering=1) as tty:
            tty.write(prompt)
            tty.flush()
            return tty.readline().strip().lower()
    except OSError:
        pass
    if sys.stdin.isatty():
        return input(prompt).strip().lower()
    return input(prompt).strip().lower()


def normalize_tune_cmd(line: str) -> str:
    """Allow f- / ff- style aliases."""
    return {
        "f-": "f",
        "ff-": "ff",
        "v-": "v",
        "g-": "g",
        "p-": "p",
        "h-": "h",
        "l-": "l",
    }.get(line, line)


def run(*args: str) -> str:
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL)


def default_sink() -> str:
    return run("pactl", "get-default-sink").strip()


def default_sink_active_port() -> str | None:
    try:
        default = default_sink()
        out = run("pactl", "list", "sinks")
    except subprocess.CalledProcessError:
        return None
    in_sink = False
    for line in out.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            in_sink = stripped.split(":", 1)[1].strip() == default
        elif in_sink and stripped.startswith("Active Port:"):
            return stripped.split(":", 1)[1].strip()
    return None


def headphones_active() -> bool:
    port = default_sink_active_port()
    return bool(port and HEADPHONE_RE.search(port))


def session_usable() -> bool:
    sid = os.environ.get("XDG_SESSION_ID")
    if not sid:
        try:
            uid = str(os.getuid())
            for line in run("loginctl", "list-sessions", "--no-legend").splitlines():
                parts = line.split()
                if len(parts) >= 3 and parts[2] == uid and "tty" not in parts[1]:
                    sid = parts[0]
                    break
        except subprocess.CalledProcessError:
            return True
    if not sid:
        return True
    try:
        props = run("loginctl", "show-session", sid, "-p", "Locked", "-p", "State")
    except subprocess.CalledProcessError:
        return True
    if "Locked=yes" in props or "State=closing" in props:
        return False
    return True


def capture_monitor(seconds: float, rate: int = 16000) -> bytes:
    sink = default_sink()
    source_idx = None
    try:
        for line in run("pactl", "list", "sources", "short").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == f"{sink}.monitor":
                source_idx = parts[0]
                break
    except subprocess.CalledProcessError:
        pass

    attempts: list[list[str]] = []
    if source_idx is not None:
        attempts.append(
            [
                "pacat",
                "-r",
                f"--device={source_idx}",
                "--format=s16le",
                "--rate",
                str(rate),
                "--channels=1",
            ]
        )
    attempts.extend(
        [
            [
                "parec",
                f"--device={sink}.monitor",
                "--format=s16le",
                "--rate",
                str(rate),
                "--channels=2",
            ],
            [
                "parec",
                "--device=@DEFAULT_MONITOR@",
                "--format=s16le",
                "--rate",
                str(rate),
                "--channels=2",
            ],
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-f",
                "pulse",
                "-i",
                f"{sink}.monitor",
                "-t",
                str(seconds),
                "-f",
                "s16le",
                "-ac",
                "1",
                "-ar",
                str(rate),
                "-",
            ],
        ]
    )

    for cmd in attempts:
        try:
            if cmd[0] in ("pacat", "parec"):
                proc = subprocess.run(
                    ["timeout", str(seconds + 0.15), *cmd],
                    capture_output=True,
                    timeout=seconds + 0.5,
                )
            else:
                proc = subprocess.run(
                    cmd, capture_output=True, timeout=seconds + 0.8
                )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            continue
        if len(proc.stdout) >= 200:
            return proc.stdout
    return b""


def monitor_peak() -> int:
    data = capture_monitor(0.08, rate=16000)
    if len(data) < 200:
        return 0
    return max(
        abs(int.from_bytes(data[i : i + 2], "little", signed=True))
        for i in range(0, len(data) - 1, 2)
    )


def _pgrep_pids(pattern: str) -> list[int]:
    proc = subprocess.run(
        ["pgrep", "-f", pattern],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [int(pid) for pid in proc.stdout.split() if pid.strip().isdigit()]


def _parse_sink_inputs(text: str) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("Sink Input #"):
            if current:
                blocks.append(current)
            current = {"id": line.split("#", 1)[1].strip()}
        elif " = " in line:
            key, val = line.strip().split(" = ", 1)
            current[key] = val.strip('"')
        elif line.strip() == "" and current:
            blocks.append(current)
            current = {}
    if current:
        blocks.append(current)
    return blocks


def _our_audio_pids() -> set[int]:
    pids = {os.getpid()}
    for pattern in (f"paplay.*{APP_NAME}", "ffmpeg.*anoisesrc"):
        pids.update(_pgrep_pids(pattern))
    return pids


def _list_other_sink_inputs() -> list[dict[str, str]]:
    try:
        out = run("pactl", "list", "sink-inputs")
    except subprocess.CalledProcessError:
        return []
    our_pids = _our_audio_pids()
    others: list[dict[str, str]] = []
    for block in _parse_sink_inputs(out):
        if block.get("corked") == "yes":
            continue
        if block.get("application.name") == APP_NAME:
            continue
        pid_s = block.get("application.process.id", "")
        if pid_s.isdigit() and int(pid_s) in our_pids:
            continue
        others.append(block)
    return others


def mpris_any_playing() -> bool | None:
    """True if a media player is Playing; False if all paused/stopped; None if unknown."""
    playing = False
    found = False
    try:
        if subprocess.call(["which", "playerctl"], stdout=subprocess.DEVNULL) == 0:
            proc = subprocess.run(
                ["playerctl", "-a", "status"],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                found = True
                for line in proc.stdout.splitlines():
                    if line.strip().lower() == "playing":
                        return True
                return False

        proc = subprocess.run(
            ["busctl", "--user", "list"],
            capture_output=True,
            text=True,
            timeout=0.5,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            dest = line.split()[0] if line.split() else ""
            if not dest.startswith("org.mpris.MediaPlayer2."):
                continue
            found = True
            status = subprocess.run(
                [
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
                ],
                capture_output=True,
                text=True,
                timeout=0.5,
            )
            if status.returncode != 0:
                continue
            if '"Playing"' in status.stdout:
                playing = True
            elif '"Paused"' not in status.stdout and '"Stopped"' not in status.stdout:
                continue
        if not found:
            return None
        return playing
    except (OSError, subprocess.TimeoutExpired):
        return None


def other_audio_playing(
    threshold: int,
    *,
    mask_running: bool = False,
    mask_baseline: int = 0,
) -> bool:
    """Block mask when other media is actually playing."""
    others = _list_other_sink_inputs()
    if not others:
        return False

    has_non_browser = any(
        block.get("application.process.binary", "").lower() not in BROWSER_BINARIES
        for block in others
    )
    if has_non_browser:
        return True

    mpris = mpris_any_playing()
    if mpris is True:
        return True
    if mpris is False:
        return False

    peak = monitor_peak()
    if mask_running and mask_baseline > 0:
        return peak > max(threshold, int(mask_baseline * 1.4))
    if peak > max(500, threshold // 4):
        return True
    return True


def find_hiss_peaks(samples: bytes, rate: int, top_n: int = 5) -> list[tuple[float, float]]:
    try:
        import numpy as np
    except ImportError:
        print("Install python3-numpy for analyze, or edit config by hand.", file=sys.stderr)
        return []

    arr = np.frombuffer(samples, dtype=np.int16).astype(np.float64)
    if len(arr) < rate:
        return []
    window = np.hanning(len(arr))
    spec = np.abs(np.fft.rfft(arr * window))
    freqs = np.fft.rfftfreq(len(arr), 1.0 / rate)
    peaks: list[tuple[float, float]] = []
    for i in range(2, len(spec) - 1):
        if spec[i] > spec[i - 1] and spec[i] > spec[i + 1] and freqs[i] > 80:
            peaks.append((float(freqs[i]), float(spec[i])))
    peaks.sort(key=lambda x: x[1], reverse=True)
    return peaks[:top_n]


def wake_default_sink() -> None:
    try:
        sink = default_sink()
        subprocess.run(
            ["pactl", "suspend-sink", sink, "0"],
            check=False,
            capture_output=True,
            timeout=2,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        pass


def clamp_volume(vol: float) -> float:
    return min(MAX_VOLUME, max(MIN_VOLUME, float(vol)))


def build_noise_ffmpeg_cmd(noise: dict, *, invert: bool = False) -> list[str]:
    color = noise["color"]
    vol = clamp_volume(noise["volume"])
    hp = int(noise["highpass_hz"])
    lp = int(noise["lowpass_hz"])
    af = f"aformat=channel_layouts=stereo,highpass=f={hp},lowpass=f={lp}"
    if invert:
        af += ",volume=-1"
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"anoisesrc=color={color}:amplitude={vol:.10f}:sample_rate=48000",
        "-af",
        af,
        "-f",
        "s16le",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-",
    ]


def build_tone_ffmpeg_cmd(
    tones: list[dict],
    *,
    volume_multiplier: float = 1.0,
    playback_volume: float | None = None,
    noise_mix: float = 0.0,
) -> list[str]:
    if not tones:
        raise ValueError("No tones configured")

    parts: list[str] = []
    for tone in tones:
        freq = tone["freq"]
        phase = tone["phase"]
        if playback_volume is not None:
            vol = min(1.0, playback_volume)
        else:
            vol = min(1.0, tone["volume"] * volume_multiplier)
        parts.append(f"{vol}*sin(2*PI*{freq}*t+{phase})")

    expr = "+".join(parts)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"aevalsrc={expr}:sample_rate=48000:channel_layout=stereo",
    ]
    if noise_mix > 0:
        cmd.extend(
            [
                "-f",
                "lavfi",
                "-i",
                f"anoisesrc=color=pink:amplitude={noise_mix}:sample_rate=48000",
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:duration=longest",
            ]
        )
    cmd.extend(["-f", "s16le", "-ac", "2", "-ar", "48000", "-"])
    return cmd


class MaskPlayer:
    def __init__(
        self,
        cfg: dict,
        *,
        preview_level: float | None = None,
        invert: bool = False,
    ) -> None:
        self._cfg = cfg
        self._preview_level = preview_level
        self._volume_multiplier = 1.0
        self._noise_mix = 0.0
        self._invert = invert
        self._ffmpeg: subprocess.Popen[bytes] | None = None
        self._paplay: subprocess.Popen[bytes] | None = None

    def _noise_for_playback(self) -> dict:
        noise = dict(self._cfg["noise"])
        if self._preview_level is not None:
            noise["volume"] = self._preview_level
        return noise

    def reload(self) -> None:
        if self.running():
            self.stop()
        self.start()

    def running(self) -> bool:
        return self._paplay is not None and self._paplay.poll() is None

    def start(self) -> None:
        if self.running():
            return
        mode = self._cfg.get("mode", "noise")
        if mode == "noise":
            noise = self._noise_for_playback()
            if noise["volume"] < MIN_VOLUME:
                return
            ffmpeg_cmd = build_noise_ffmpeg_cmd(noise, invert=self._invert)
        else:
            if not self._cfg.get("tones"):
                return
            ffmpeg_cmd = build_tone_ffmpeg_cmd(
                self._cfg["tones"],
                volume_multiplier=self._volume_multiplier,
                playback_volume=self._preview_level,
                noise_mix=self._noise_mix,
            )
        wake_default_sink()
        sink = default_sink()
        paplay_cmd = [
            "paplay",
            f"--device={sink}",
            "--raw",
            "--format=s16le",
            "--rate=48000",
            "--channels=2",
            f"--property=application.name={APP_NAME}",
            f"--property=media.name={APP_NAME}",
        ]
        self._ffmpeg = subprocess.Popen(
            ffmpeg_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.15)
        if self._ffmpeg.poll() is not None:
            print("ffmpeg failed to start", file=sys.stderr)
            self._ffmpeg = None
            return
        assert self._ffmpeg.stdout is not None
        self._paplay = subprocess.Popen(
            paplay_cmd,
            stdin=self._ffmpeg.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._ffmpeg.stdout.close()
        time.sleep(0.1)
        if self._paplay.poll() is not None:
            print("paplay failed to start", file=sys.stderr)
            self.stop()

    def stop(self) -> None:
        for proc in (self._paplay, self._ffmpeg):
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (self._paplay, self._ffmpeg):
            if proc:
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self._ffmpeg = None
        self._paplay = None


def check_deps() -> int:
    for bin_name in ("pactl", "ffmpeg", "paplay", "parec"):
        if subprocess.call(["which", bin_name], stdout=subprocess.DEVNULL) != 0:
            print(f"Missing dependency: {bin_name}", file=sys.stderr)
            return 1
    return 0


def cmd_analyze() -> int:
    if check_deps():
        return 1
    if not headphones_active():
        print("Plug in headphones (active port) first.", file=sys.stderr)
        return 1
    print("Keep other audio silent. Capturing 2s of idle hiss from the monitor...")
    samples = capture_monitor(2.0, rate=44100)
    peaks = find_hiss_peaks(samples, 44100)
    cfg = load_config()
    cfg["mode"] = "noise"
    if not peaks or peaks[0][1] < 1e6:
        print("Idle hiss looks broadband (wind/white noise) — using noise mask mode.")
        save_config(cfg)
        print("Run: headphone-hum-mask.py tune")
        return 0

    print("\nSome tonal peaks found, but wind-like hiss is usually broadband.")
    print("Using noise mask mode (not beeps). Strongest peaks for reference:")
    for freq, strength in peaks[:5]:
        rel = strength / peaks[0][1]
        print(f"  {freq:7.1f} Hz  (relative {rel:.2f})")
    save_config(cfg)
    print("\nRun: headphone-hum-mask.py tune")
    return 0


def _kill_pids(pids: list[int], sig: int = signal.SIGTERM) -> None:
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def stop_preview_playback() -> None:
    """Stop tune preview + ffmpeg/paplay only (never the calling CLI)."""
    me = os.getpid()
    targets: list[int] = []
    for pattern in (
        "headphone-hum-mask.py _play-preview",
        "ffmpeg.*aevalsrc",
        "ffmpeg.*anoisesrc",
        f"paplay.*{APP_NAME}",
    ):
        targets.extend(_pgrep_pids(pattern))
    _kill_pids([pid for pid in set(targets) if pid != me])


def stop_everything() -> None:
    """Stop daemon, preview, and audio workers (for `stop` command)."""
    me = os.getpid()
    targets = _pgrep_pids("headphone-hum-mask")
    _kill_pids([pid for pid in set(targets) if pid != me])
    stop_preview_playback()


def cmd_stop() -> int:
    stop_everything()
    print("Stopped headphone-hum-mask audio.", flush=True)
    return 0


def cmd_tune_noise(cfg: dict) -> int:
    noise = cfg["noise"]
    if noise["volume"] > 0.001:
        noise["volume"] = 0.0005
    listen_boost = 1.0
    player = MaskPlayer(cfg, preview_level=None)
    player.start()
    if not player.running():
        print("Audio failed to start. Try: headphone-hum-mask.py test", file=sys.stderr)
        return 1

    def log(msg: str = "") -> None:
        print(msg, flush=True)

    def heard_volume() -> float:
        return clamp_volume(noise["volume"] * listen_boost)

    def apply_heard() -> None:
        cfg["noise"] = noise
        player._cfg = cfg
        player._preview_level = heard_volume()
        player.reload()

    log(
        "Noise mask tune — you hear saved_vol × listen_boost.\n"
        "v/v- = saved volume ÷1.25   v+ = ×1.25  (what the daemon uses)\n"
        "g/g- = listen boost ÷1.25   g+ = ×1.25  (temporary, not saved)\n"
        "h/h+ high-pass   l/l+ low-pass   c = color   s save   q quit"
    )

    def show() -> None:
        log(
            f"\ncolor={noise['color']}  saved_vol={noise['volume']:.8f}  "
            f"listen×{listen_boost:.2f}  heard={heard_volume():.8f}  "
            f"highpass={noise['highpass_hz']} Hz  lowpass={noise['lowpass_hz']} Hz"
        )
        log("v/v- v+  g/g- g+  h/h- h+  l/l- l+  c  s  q")

    show()
    try:
        while True:
            try:
                line = read_tty_line("> ")
            except (EOFError, KeyboardInterrupt):
                log("\nExiting.")
                break
            if not line:
                continue
            line = normalize_tune_cmd(line)
            changed = False
            if line == "q":
                break
            if line == "s":
                cfg["mode"] = "noise"
                cfg.pop("tune_preview_level", None)
                cfg["noise"] = noise
                save_config(cfg)
                break
            if line in ("g", "g-"):
                listen_boost = max(0.01, listen_boost / 1.25)
                changed = True
            elif line == "g+":
                listen_boost = min(100.0, listen_boost * 1.25)
                changed = True
            elif line in ("v", "v-"):
                noise["volume"] = clamp_volume(noise["volume"] / 1.25)
                changed = True
            elif line == "v+":
                noise["volume"] = clamp_volume(noise["volume"] * 1.25)
                changed = True
            elif line in ("h", "h-"):
                noise["highpass_hz"] = max(20, noise["highpass_hz"] - 50)
                changed = True
            elif line == "h+":
                noise["highpass_hz"] = min(8000, noise["highpass_hz"] + 50)
                changed = True
            elif line in ("l", "l-"):
                noise["lowpass_hz"] = max(
                    noise["highpass_hz"] + 200, noise["lowpass_hz"] - 500
                )
                changed = True
            elif line == "l+":
                noise["lowpass_hz"] = min(16000, noise["lowpass_hz"] + 500)
                changed = True
            elif line == "c":
                i = NOISE_COLORS.index(noise["color"]) if noise["color"] in NOISE_COLORS else 0
                noise["color"] = NOISE_COLORS[(i + 1) % len(NOISE_COLORS)]
                changed = True
            else:
                log("Unknown command")
                continue
            if changed:
                apply_heard()
                if not player.running():
                    log("Warning: audio stopped (volume too low?) — try v+ or g+")
                else:
                    log(f"Applied: {line}")
            show()
    finally:
        player.stop()
    return 0


def cmd_tune_tone(cfg: dict) -> int:
    tones = cfg["tones"]
    if not tones:
        tones = [{"freq": 400, "volume": 0.04, "phase": 0.0}]
    idx = 0
    preview_level = float(
        os.environ.get(
            "TUNE_PREVIEW_VOLUME",
            str(cfg.get("tune_preview_level", DEFAULT_PREVIEW_LEVEL)),
        )
    )
    cfg["mode"] = "tone"
    player = MaskPlayer(cfg, preview_level=preview_level)
    player._noise_mix = 0.02
    player.start()
    if not player.running():
        print("Audio failed to start.", file=sys.stderr)
        return 1

    def log(msg: str = "") -> None:
        print(msg, flush=True)

    log("Tone mode (beeps) — f/f+ freq, p/p+ phase, v/v+ saved level, s save, q quit")

    def show() -> None:
        t = tones[idx]
        log(
            f"\nTone {idx + 1}/{len(tones)}: freq={t['freq']:.0f} Hz  "
            f"saved_vol={t['volume']:.4f}  preview={preview_level:.2f}  "
            f"phase={t['phase']:.3f} rad"
        )
        log("f/f- f+  ff/ff- ff+  v/v- v+  g/g- g+  p/p- p+  pi  n  n+  d  s  q")

    show()
    try:
        while True:
            try:
                line = read_tty_line("> ")
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            line = normalize_tune_cmd(line)
            t = tones[idx]
            changed = False
            if line == "q":
                break
            if line == "s":
                cfg.pop("tune_preview_level", None)
                cfg["tones"] = tones
                save_config(cfg)
                break
            if line in ("g", "g-"):
                preview_level = max(0.01, preview_level - 0.02)
                changed = True
            elif line == "g+":
                preview_level = min(1.0, preview_level + 0.02)
                changed = True
            elif line == "f":
                t["freq"] = max(20.0, t["freq"] - 50.0)
                changed = True
            elif line == "f+":
                t["freq"] = min(20000.0, t["freq"] + 50.0)
                changed = True
            elif line == "ff":
                t["freq"] = max(20.0, t["freq"] - 500.0)
                changed = True
            elif line == "ff+":
                t["freq"] = min(20000.0, t["freq"] + 500.0)
                changed = True
            elif line in ("v", "v-"):
                t["volume"] = max(0.0, t["volume"] - 0.002)
                changed = True
            elif line == "v+":
                t["volume"] = min(0.5, t["volume"] + 0.002)
                changed = True
            elif line in ("p", "p-"):
                t["phase"] = (t["phase"] - 0.1) % (2 * math.pi)
                changed = True
            elif line == "p+":
                t["phase"] = (t["phase"] + 0.1) % (2 * math.pi)
                changed = True
            elif line == "pi":
                t["phase"] = (t["phase"] + math.pi) % (2 * math.pi)
                changed = True
            elif line == "n":
                idx = (idx + 1) % len(tones)
                show()
                continue
            elif line == "n+":
                tones.append({"freq": 400, "volume": 0.03, "phase": 0.0})
                idx = len(tones) - 1
                changed = True
            elif line == "d" and len(tones) > 1:
                del tones[idx]
                idx = min(idx, len(tones) - 1)
                changed = True
            else:
                log("Unknown command")
                continue
            if changed:
                cfg["tones"] = tones
                player._cfg = cfg
                player._preview_level = preview_level
                player.reload()
                log(f"Applied: {line}")
            show()
    finally:
        player.stop()
    return 0


def cmd_tune() -> int:
    if check_deps():
        return 1
    if not headphones_active():
        print("Plug in headphones first.", file=sys.stderr)
        return 1
    stop_preview_playback()
    cfg = load_config()
    if cfg.get("mode", "noise") == "tone":
        return cmd_tune_tone(cfg)
    cfg["mode"] = "noise"
    return cmd_tune_noise(cfg)


def cmd_test() -> int:
    if check_deps():
        return 1
    wake_default_sink()
    sink = default_sink()
    cfg = load_config()
    cfg["mode"] = "noise"
    print(f"Playing noise mask sample for 3s on {sink}...")
    player = MaskPlayer(cfg, preview_level=0.05)
    player.start()
    if not player.running():
        print("Playback did not start — run: ffmpeg -version", file=sys.stderr)
        return 1
    time.sleep(3)
    player.stop()
    print("Done. If you heard nothing, check system volume and headphone port.")
    return 0


def cmd_invert() -> int:
    """Play saved noise config phase-inverted (180°). Experiment only."""
    if check_deps():
        return 1
    if not headphones_active():
        print("Plug in headphones first.", file=sys.stderr)
        return 1

    cfg = load_config()
    noise = cfg["noise"]
    sink = default_sink()
    print(
        f"Inverted playback on {sink}\n"
        f"  color={noise['color']}  vol={noise['volume']:.8f}  "
        f"highpass={noise['highpass_hz']} Hz  lowpass={noise['lowpass_hz']} Hz\n"
        "Phase inverted (×−1). Uncorrelated hiss will not truly cancel — experiment only.\n"
        "Ctrl+C to stop."
    )

    player = MaskPlayer(cfg, invert=True)
    stopping = False

    def on_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    player.start()
    if not player.running():
        print("Playback failed to start.", file=sys.stderr)
        return 1

    while not stopping:
        time.sleep(0.3)

    player.stop()
    return 0


def cmd_daemon() -> int:
    if check_deps():
        return 1

    cfg = load_config()
    threshold = cfg["peak_threshold"]
    player = MaskPlayer(cfg)
    mask_baseline = 0
    stopping = False

    def on_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, on_stop)
    signal.signal(signal.SIGINT, on_stop)

    player.start()
    time.sleep(0.1)
    mask_baseline = monitor_peak()
    if mask_baseline < 500:
        mask_baseline = 3000

    while not stopping:
        try:
            if player.running() and not _list_other_sink_inputs():
                sample = monitor_peak()
                if sample > 0:
                    mask_baseline = int(mask_baseline * 0.8 + sample * 0.2)

            want = (
                headphones_active()
                and session_usable()
                and not other_audio_playing(
                    threshold,
                    mask_running=player.running(),
                    mask_baseline=mask_baseline,
                )
            )
        except Exception:
            want = False
        if want:
            player.start()
        else:
            player.stop()
        time.sleep(POLL_SEC)

    player.stop()
    return 0


def main() -> int:
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "analyze":
            return cmd_analyze()
        if cmd == "tune":
            return cmd_tune()
        if cmd == "stop":
            return cmd_stop()
        if cmd == "test":
            return cmd_test()
        if cmd == "invert":
            return cmd_invert()
        if cmd in ("-h", "--help", "help"):
            print(__doc__)
            return 0
        print(f"Unknown command: {sys.argv[1]}", file=sys.stderr)
        return 1
    return cmd_daemon()


if __name__ == "__main__":
    sys.exit(main())
