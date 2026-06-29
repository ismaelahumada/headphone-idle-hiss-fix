# Headphone idle-hiss fix (Linux)

Kill the faint **hiss/whine you hear through headphones for several seconds after
audio stops** on many Linux laptops. That hiss is the analog codec/DAC staying
powered while the system thinks audio might resume. This repo shortens that
window from ~10 s down to ~2 s — **without playing any masking noise**.

It does two independent things:

1. **Makes the sound card suspend the instant it goes idle** (PulseAudio +
   kernel codec power-down tuning). This already fixes short system sounds.
2. **Defeats Chromium's audio keep-alive** with a tiny daemon. Brave/Chrome keep
   their audio stream open (playing digital silence) for ~10 s after you pause a
   video, which forces the card to stay powered. The daemon detects pause/play
   over MPRIS and **parks the browser's stream on a silent virtual sink while
   paused**, letting the real card power down, then moves it back on play.

> No audio is generated — this is the recommended, real fix. A masking daemon
> (`headphone-hum-mask.py`) is also included for completeness, but **it is not a
> good alternative**: it does not remove the hiss, it merely covers it with a
> constant low-level noise. See
> [Optional (not recommended): noise mask](#optional-not-recommended-noise-mask).

---

## Why the hiss happens

| After audio stops | What keeps the codec powered |
|---|---|
| A **system sound** ends | Its stream disconnects immediately → card suspends fast. ✅ |
| You **pause a Brave/Chrome video** | The browser keeps its stream `Corked: no` (playing silence) for ~10 s. PulseAudio is *required* to keep the device open while a stream is connected, so the codec stays in `D0` and hisses the whole time. ❌ |

There is **no browser flag** to disable Chromium's keep-alive, and force-suspending
the card drops audio. Moving the *stream* to a dummy sink is the reliable fix.

---

## System requirements

This solution targets the following setup. The PulseAudio + park parts are fairly
general; the `power_save` part is specific to Intel HD Audio / Realtek codecs.

**Required**

- **Linux with a systemd user session** (`systemctl --user`).
- **PulseAudio** as the active sound server — the daemon uses `pactl move-sink-input`.
  (Likely works under PipeWire's `pipewire-pulse` since `pactl` is provided, but it
  is only tested on real PulseAudio. The `config/pulse/default.pa` override applies
  to PulseAudio only.)
- **Python 3.8+** (standard library only).
- **`pactl`** (package `pulseaudio-utils`).
- **MPRIS source**: `busctl` (ships with systemd) — or `playerctl` if you prefer.
- A **Chromium-based browser** (Brave, Chrome, Chromium). The keep-alive this works
  around is a Chromium behavior; Firefox is also handled but behaves differently.

**Recommended (for the fastest, most complete power-down)**

- An **Intel HD Audio driver** (`snd_hda_intel`) — i.e. most Intel/AMD laptops with
  a Realtek/Conexant codec. Enables the `power_save` codec power-down (root step).

**Only needed for the optional masking daemon**

- **`ffmpeg`**, plus `paplay`/`parec` (also from `pulseaudio-utils`).

**Tested on**

- Lenovo XiaoXin Air 15ARE 2021 (AMD), Realtek **ALC257** codec
- Ubuntu 22.04, PulseAudio, Brave 148 (Chromium 148)

---

## What's in the repo

```
bin/
  headphone-hiss-park.py        # the fix: park browser streams while paused (no audio)
  headphone-hum-mask.py         # optional alternative: quiet noise mask
systemd/
  headphone-hiss-park.service   # user service for the park daemon
  headphone-hum-mask.service    # user service for the mask daemon (optional)
config/
  pulse/default.pa              # PulseAudio: suspend the sink the moment it's idle
  modprobe/audio_powersave.conf # kernel: power down the HDA codec after 1s (root)
install.sh                      # installs per-user files + enables the park daemon
uninstall.sh                    # reverts the per-user install
```

---

## Installation

### Quick install (per-user)

```bash
git clone https://github.com/ismaelahumada/headphone-idle-hiss-fix.git
cd headphone-idle-hiss-fix
./install.sh
```

`install.sh` will:

1. Copy the scripts to `~/.local/bin/`.
2. Install the systemd **user** units to `~/.config/systemd/user/`.
3. If PulseAudio is the active server, install `~/.config/pulse/default.pa`
   (backing up any existing file) and restart PulseAudio.
4. `enable --now` the **park daemon**.

### One manual root step (recommended)

This powers the HDA codec down quickly once the device is idle. It needs root,
so the installer only prints it:

```bash
sudo install -m 0644 config/modprobe/audio_powersave.conf /etc/modprobe.d/audio_powersave.conf
sudo update-initramfs -u   # or just reboot
```

Verify after reboot:

```bash
cat /sys/module/snd_hda_intel/parameters/power_save   # -> 1
```

### Manual install (if you prefer not to run the script)

```bash
# scripts
install -m 0755 bin/headphone-hiss-park.py ~/.local/bin/
# service
install -m 0644 systemd/headphone-hiss-park.service ~/.config/systemd/user/
# PulseAudio (PulseAudio servers only)
mkdir -p ~/.config/pulse && cp config/pulse/default.pa ~/.config/pulse/default.pa
pulseaudio -k
# enable
systemctl --user daemon-reload
systemctl --user enable --now headphone-hiss-park.service
```

---

## Usage / control

```bash
headphone-hiss-park.py status                       # show parked/live browser streams
systemctl --user status  headphone-hiss-park         # service health
journalctl --user -u headphone-hiss-park -f          # watch park/unpark live
systemctl --user stop    headphone-hiss-park         # turn off this session
systemctl --user disable --now headphone-hiss-park   # turn off permanently
```

## Verifying it works

```bash
# Watch the hardware power state (reading sysfs does NOT wake the codec):
watch -n0.5 'cat /sys/class/sound/card*/device/power/runtime_status'
```

Play a Brave video, then pause it. Within ~2 s you should see the relevant card
flip to `suspended`, and `journalctl --user -u headphone-hiss-park -f` will show a
`park` line. Press play and it logs `unpark`.

---

## Trade-offs & troubleshooting

- **Calls are protected**: while a browser is capturing your microphone (Google
  Meet, Zoom, Discord, …), parking is suspended so call audio is never silenced.
  Plain media playback (YouTube) doesn't capture the mic, so it's still parked
  normally. (If you join a call with no microphone access at all, parking can't
  detect the call — grant mic access or `systemctl --user stop headphone-hiss-park`.)
- **Resume clip**: resuming a paused browser may clip the first ~0.1–0.2 s while the
  stream moves back. Lower `POLL_SEC` in `headphone-hiss-park.py` for faster response.
- **If browser audio ever breaks**: `systemctl --user stop headphone-hiss-park`
  reverts to normal instantly. (Moving a stream can, in rare cases, make Chromium
  fall back to a silent stream; stopping the daemon fixes it.)
- **No `park` events appear**: confirm MPRIS works — `busctl --user list | grep mpris`
  while a video plays. Install `playerctl` if needed.
- **Not PulseAudio**: the `config/pulse/default.pa` override is skipped. On
  PipeWire/WirePlumber, set `session.suspend-timeout-seconds` in
  `~/.config/pipewire/...` instead; the park daemon still works via `pactl`.

---

## Optional (not recommended): noise mask

`bin/headphone-hum-mask.py` takes the opposite, inferior approach: instead of
removing the hiss it **plays a constant, barely-audible noise (brown/pink) to
cover it up**. It is included only for completeness.

**Why this is not a good alternative:**

- It **does not solve the problem** — the codec stays powered and the hiss is
  still there; you've just added another quiet sound on top of it.
- It puts a **constant noise in your ears**, which many people find more tiring
  than the intermittent hiss it hides.
- It **keeps the sound card awake on purpose**, so it works against power saving
  rather than with it.
- Tuning it is fiddly and the result is system/headphone specific.

Prefer the **park daemon** (the default in this repo), which actually lets the
hardware power down. Only consider the mask as a last resort — for example on a
non-Chromium setup where stream parking doesn't apply — and never run both at
once.

```bash
headphone-hum-mask.py tune                          # tune color/volume/filters by ear
systemctl --user enable --now headphone-hum-mask    # run at login (not recommended)
```

---

## How it was diagnosed

The root cause was confirmed by sampling, every 0.5 s, the PulseAudio sink state,
each browser stream's `Corked` flag, and the PCI controller's `runtime_status`
(sysfs, which does not wake the codec). The hiss tracked exactly the window where
Brave's stream was `Corked: no`; the moment it corked, the sink suspended and the
hardware powered down ~1.8 s later.

## Author & license

Created by **Ismael Ahumada**.

Licensed under the **MIT License** — see [LICENSE](LICENSE). MIT lets others use,
modify and redistribute the code freely, **provided they keep the copyright and
license notice** (i.e. my authorship line) in copies and substantial portions.
That is the attribution MIT guarantees; it does not force credit in a UI or
docs. If you reuse this work, please keep the author/copyright notice intact.
