# bike-controller

Bridge a **NordicTrack G/GX LE recumbent bike** (model NTEX99025) into Xbox
input, so a game controller only works while you are pedalling.

The bike's own Bluetooth is the data source — no cadence sensor, no magnets, no
hardware modification. The console reports **cadence, power and resistance**,
which is strictly more than a bolt-on sensor would give.

```
NordicTrack G LE ──BLE──┐
                        ├──► Raspberry Pi 5 ──► virtual gamepad ──► Chromium
8BitDo controller ─USB──┘      (bridge.py)       (/dev/uinput)     xbox.com/remoteplay
                                                                         │
                                                                         ▼
                                                                   Xbox console
                                                                 (watched on the TV
                                                                   over HDMI)
```

Remote Play is used purely as an **input transport** — the game is watched on the
TV over HDMI, so video quality on the Pi is irrelevant and can be set to minimum.

Status: **working end to end.** Verified against a real console and a real Xbox,
and field-tested playing Helldivers 2 — the gate catches reliably even at fairly
low cadence, so the 40/25 rpm thresholds do not force you to sprint to move.

### Known-working hardware

| | |
|---|---|
| Bike | NordicTrack G/GX LE, model NTEX99025 |
| Console | `54801-VV`, firmware `22017.0908`, advertises as `I_EB` |
| Host | Raspberry Pi 5, Debian 12, kernel 6.12 |
| Controller | 8BitDo Ultimate 2 Wireless, USB, X-input mode |

Other Icon consoles (NordicTrack, ProForm, FreeMotion) that advertise as `I_EB`
or `I_SB` are likely to work — `tools/probe_bike.py` tries three known poll
variants and prints a byte-movement table so you can confirm the field offsets
on yours. Any evdev gamepad works; nothing assumes 8BitDo.

---

## Install

On the Pi:

```bash
git clone https://github.com/iamnirav/bike-controller.git
cd bike-controller && ./install.sh
```

It derives almost everything: paths from wherever you cloned it, the desktop user
from who owns the checkout, the controller's USB IDs from whatever is plugged in.
It will ask for your Xbox console ID — find it at
`https://www.xbox.com/play/consoles`, pick your console, and copy the ID out of
the URL. Everything else lives in `config.env` (created from
`config.env.example`) and every value there is optional.

```bash
sudo systemctl enable --now bike-bridge
```

### Updating

```bash
git pull && ./install.sh
```

`install.sh` is idempotent and skips what has not changed, so this is always
correct and usually takes seconds. Most updates need nothing beyond a restart —
but rather than make you remember which ones need more, re-running it is the one
answer that is never wrong. It restarts the service itself if it was running.

### Removing it

```bash
./uninstall.sh            # keeps config.env and your ride logs
./uninstall.sh --purge    # removes those too, after confirming
```

Reverses everything `install.sh` put on the machine. It does not touch the repo
or apt packages, and says so.

## Day-to-day use

**No laptop needed.** The bridge starts on boot, so the routine is:

1. The Pi stays powered on; the bridge starts at boot and waits for the bike
2. Get on the bike
3. Enter the **Konami code** on the controller: ↑ ↑ ↓ ↓ ← → ← → B A
4. Short buzz = registered. Long buzz = Remote Play is streaming. Ride.

Three short pulses means the launch failed; see
`/run/user/1000/bike-remoteplay.log`.

The bridge runs as a systemd service on the Pi:

```bash
ssh <your-pi>
sudo systemctl start bike-bridge          # start
sudo journalctl -u bike-bridge -f         # watch
sudo systemctl stop bike-bridge           # stop
sudo systemctl enable bike-bridge         # optional: start on boot
```

Then open `xbox.com/remoteplay` in Chromium on the Pi. Get on the bike and ride.

**Default mapping:** the left stick's **deflection scales with your power output**
— work harder, move faster. Sprint (left stick click) engages above 100 W. Every
other input passes through unconditionally, so menus and actions still work while
stopped; you just cannot *travel*.

Because speed follows watts rather than rpm, **resistance sets the character of
the workout**: crank it up and pedal slow and hard, drop it and spin fast and
light. Either reaches full speed.

---

## Hardware setup

| Piece | Notes |
|---|---|
| Raspberry Pi 5 | Debian 12, kernel 6.12. Must be **within BLE range of the bike** (~10 m, less through walls). |
| Controller | Any evdev gamepad. An 8BitDo Ultimate 2 in **X-input mode** passes through exactly, no rescaling. |
| Connection | USB preferred: one less latency hop, avoids the ERTM pairing bugs Xbox pads have on the Pi's radio, and leaves the radio free for the bike. |

The controller's brand is invisible to the Xbox — the bridge grabs it and
re-presents everything as a virtual Xbox 360 pad.

---

## The bike: Icon/iFit BLE protocol

The G/GX LE has an **LCD console, not a touchscreen**. That distinction decides
everything: touchscreen iFit machines run Android and are only readable over ADB,
while LCD-console machines expose telemetry over plain BLE.

Ours advertises as **`I_EB`** and speaks Icon Health & Fitness's proprietary
service (no FTMS). Reverse-engineering credit: the
[qdomyos-zwift](https://github.com/cagnulein/qdomyos-zwift) project, specifically
`src/devices/proformbike/proformbike.cpp`.

| UUID | Role |
|---|---|
| `00001533-1412-efde-1523-785feabcd123` | service |
| `00001534-...` | write — handshake, poll requests, resistance commands |
| `00001535-...` | notify — telemetry |

Bike name prefixes: `I_EB` (exercise bike), `I_SB` (studio bike), or a name
containing `_IFIT_BIKE`.

### It is request/response, not streaming

This is the thing that wastes an afternoon if you miss it. Three stages, and
skipping any one leaves the console completely silent:

1. **Subscribe** to notifications on `1535`.
2. **Handshake**: 13 packets on `1534`, 400 ms apart. This characteristic is
   **write-with-response only** — it has no `write-without-response` property,
   and CoreBluetooth *silently discards* unacknowledged writes to it rather than
   raising. The replies carry model, firmware and serial as ASCII.
3. **Poll forever**: write a short "noOp" request every 200 ms, cycling a fixed
   sequence. Each request draws exactly one reply, and telemetry rides in those
   replies. Stop polling and the console stops talking.

The poll sequence differs per console generation. `tools/probe_bike.py` knows
three variants (`gx27`, `generic`, `gx45pro`) and tries each until telemetry
appears. **Ours answers `gx27`** — the variant covering the VR21, Icon's other
recumbent.

### Confirmed telemetry layout

Console **`54801-VV`**, firmware **`22017.0908`**. A telemetry frame is 20 bytes
starting `00 12 01 04` with **byte 5 = `0x31`**. (Frames with byte 5 = `0x17` are
all-`0xff` filler and must be ignored.)

| Offset | Field | Observed |
|---|---|---|
| 18 | **cadence, RPM** | 0–81 |
| 12–13 | **power, watts** (uint16 LE) | 0–120 |
| 11 | **resistance level** | 0–9 (console goes to 26) |
| 14–15 | distance accumulator (uint16 LE) | resets on stop |

**How cadence was identified**, since this is expensive to re-derive: while
resistance was 0, byte 18 tracked byte 12 by the exact rule `b18 = b12 // 2 + 25`
across all 20 distinct samples — consistent with the console *estimating* watts
from cadence at fixed resistance. The decisive sample came when resistance
changed to 3: that rule predicts `b18 = 85`, but byte 18 **fell to 73** while
byte 12 **rose to 120**. More watts at lower cadence under higher resistance is
the physically correct behaviour, and is only consistent with byte 18 being
cadence and byte 12 being power.

### Update rate

**~2.56 Hz at the deployed `--poll-interval 0.05`** — see the measured table
below. It was 0.87 Hz at the original 0.2 s interval, where the sleep was ~87%
of cycle time rather than the BLE round trip
(~30 ms), so `--interval 0.05` should get roughly 2.5 Hz. Verify with the Hz
readout in `tools/live.py` before assuming.

---

## The Xbox side: Chromium and virtual gamepads

Chromium on Linux is picky about gamepads, and the widespread claim that it
"ignores virtual gamepads on Linux" turns out to be **stale**. From
`device/gamepad/udev_gamepad_linux.cc` and `gamepad_device_linux.cc` it requires
all of the following — and a uinput device satisfies them if built correctly:

| Requirement | How we satisfy it |
|---|---|
| a `/dev/input/jsN` node | load `joydev` — **not loaded by default on Pi OS** |
| `ID_INPUT_JOYSTICK` set by udev | automatic, given gamepad buttons + abs axes |
| a parent in the `input` subsystem | `/devices/virtual/input/inputN/jsN` |
| vendor/product from that parent's sysfs | declare `045e:028e` |

`045e:028e` ("Microsoft X-Box 360 pad") is in Chromium's `GamepadIdList`, so it
applies the **standard** mapping and buttons land where games expect them.

Without `joydev` the pad exists but is invisible to the browser. This is the
single most likely thing to break on a fresh Pi.

### The phantom pad

`EVIOCGRAB` stops the kernel delivering the physical controller's events to any
other handler — confirmed: with the bridge running, pressing buttons moves only
the virtual pad. But the grab does **not remove** the physical pad's
`/dev/input/jsN` node, so Chromium still lists it as a phantom gamepad that never
reports input. A game binding to that slot would get nothing.

Fixed by `udev/99-bike-controller.rules`, which **removes** `ID_INPUT_JOYSTICK`
from the physical pad. It must be removed outright, not set to `0` — Chromium
only tests whether the property is *present*.

```bash
sudo cp udev/99-bike-controller.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger -s input
```

Side effect: the controller is then invisible to browsers whenever the bridge
is not running. Undo instructions are in the rule file's header.

---

## Movement scaling

The left stick is multiplied by a 0.0–1.0 scale derived from effort. A uniform
scalar on both components scales magnitude and preserves direction exactly, so
diagonals are unaffected.

### Why the lower bound is 0

`--movement-min` defaults to **0**, which looks like it would let a feeble pedal
produce a feeble crawl. It doesn't, because **every game already applies its own
stick deadzone** and ignores anything below it:

| Source | Deadzone |
|---|---|
| XInput recommended (`XINPUT_GAMEPAD_LEFT_THUMB_DEADZONE` = 7849) | ~24% |
| Unity Input System default | 12.5% |
| Typical user-facing slider range | 5–35% |

So the game's deadzone *is* the threshold you must clear before you move, and it
self-calibrates to whatever you are playing. One number solves two problems.
With `--movement-max 100`, movement starts somewhere around 12–24 W depending on
the game.

If a game's deadzone is unusually low and gentle pedalling moves you more than
you want, raise `--movement-min`. If movement starts too abruptly, add
`--movement-floor` to jump straight to a usable scale.

### Poll rate

Telemetry rate is set by `--poll-interval`. Measured on this console:

| `--poll-interval` | Telemetry rate |
|---|---|
| 0.2 | 0.77 Hz |
| 0.1 | 1.95 Hz |
| **0.05** (deployed) | **2.56 Hz** |
| 0.02 | 2.74 Hz |

It plateaus near 2.7 Hz — the console's own update ceiling, not our polling — so
**0.05 is the knee**. Below it you quadruple BLE traffic for about 7%.

Worth knowing: cadence is inherently measured over crank revolutions, so at
60 rpm the console cannot produce a genuinely new value more than about once a
second. Faster polling does not invent data; it just means you learn about each
new value sooner, which is what fixes the "controller has to catch up" feel.

### Haptics

Short rumble cues on the physical controller mark the transitions you cannot see
while riding:

| Cue | Meaning |
|---|---|
| strong 100 ms | hit full speed |
| strong 200 ms, both motors | sprint engaged |

Plus two outside the ride loop, for the launch flow: a **120 ms** ack when the
Konami code registers, a **700 ms** double-motor buzz when Remote Play is
confirmed streaming, and three short pulses if it failed.

There are deliberately **no "off" cues**. Buzzing on the way down doubles the
haptic traffic while telling you nothing your legs have not already told you.

Rising-edge only, and both underlying flags are **hysteretic** — sprint releases at
92% of its threshold, full speed at 95% — so hovering at a boundary does not
buzz continuously. Requires `FF_RUMBLE` on the pad (the 8BitDo Ultimate 2 has it,
with 16 effect slots). Effects are uploaded once and replayed by id; uploading
per pulse would exhaust those slots. Disable with `--no-rumble`.

### Game rumble passthrough

Until this existed, **every rumble the game sent was dropped** — the virtual pad
advertised no haptics at all, so Helldivers' feedback never reached your hands.

python-evdev cannot fix that: its `UInput` exposes no `ff_effects_max` (required
at device creation) and no `UI_BEGIN_FF_UPLOAD` / `UI_END_FF_UPLOAD` wrappers,
both of which the kernel demands before a uinput device may accept effects. So
`bike_controller/uinput_ff.py` builds the device over ctypes instead.

The protocol, once the device exists: the browser uploads an effect and the
kernel hands us an `EV_UINPUT` request; we `UI_BEGIN_FF_UPLOAD` to read it, set
`retval = 0` to accept, and `UI_END_FF_UPLOAD`. **Skipping that leaves the
browser blocked in its ioctl**, not merely silent — which is why the output loop
services FF every frame. When the game later plays the effect, an `EV_FF` event
arrives with its id and we forward the magnitudes to the physical pad, reusing a
single effect slot so gameplay cannot exhaust the pad's 16.

If any of it fails, `VirtualGamepad` falls back to a plain evdev pad and says so.
Rumble is a luxury; a working gamepad is not.

Note this shares one physical device with the bike's own cues, so a game rumble
and a "full speed" buzz can interrupt each other. In practice they are short
enough not to matter.

### Deliberately unsmoothed

Movement scale is the **raw** value, passed straight through with no filter.
This is a decision, not an oversight: it is not yet known whether smoothing is
needed, and at ~2.56 Hz telemetry any filter also adds lag, which on movement
speed feels like input delay. Judge the real feel first.

One exception, which is safety rather than smoothing: **a stale feed forces the
scale to 0**. Otherwise a dropped BLE link would freeze the stick at its last
deflection and walk your character into a wall indefinitely.

"Stale" is **derived from `--poll-interval`, not a fixed constant** — about four
missed frames, so 1.6 s at the deployed 0.05 and 4.6 s at 0.2. That coupling
matters: a hard-coded 1.5 s window left barely one frame of margin at the slower
poll rate, and a single dropped BLE frame would zero movement mid-ride.
`tests/test_mapping.py` asserts both the fail-safe and that dropped frames do
not trip it, at every poll rate.

### Where the thresholds came from

Measured on this bike: 55–81 rpm at resistance 0–3 produced 61–120 W.

Deployed settings are **75 W** for full speed and **100 W** for sprint (see the
systemd unit). These came down with every real ride: 130/150 derived from a
single capture, then 100/125, then 75/100. **Ride data beat arithmetic every
time** — treat any number derived from a capture as a starting point.

`--movement-min` stays at 0; the game's deadzone does that job.

Sprint **holds** `BTN_THUMBL` (left stick click) for as long as you are above the
threshold — it is not a toggle or a click, so configure games for
**hold-to-sprint** rather than press-to-toggle.

## Configuration

All tuning is command-line; edit `ExecStart` in the systemd unit to persist.

| Flag | Default | Meaning |
|---|---|---|
| `--address` | — | bike MAC. Omit to autodiscover by name. |
| `--gate-inputs` | `left_stick` | comma-separated groups the gate suppresses: `left_stick`, `right_stick`, `triggers`, `dpad`, `face_buttons`, `shoulders`, `all` |
| `--gate-open` / `--gate-close` | 40 / 25 | hysteresis thresholds, rpm |
| `--gate-grace` | 1.5 | seconds below the close threshold before the gate shuts |
| `--no-gate` | off | disable gating entirely |
| `--axis` | `none` | axis driven by cadence: `right_trigger`, `left_trigger`, `right_stick_y`, `left_stick_y` |
| `--axis-min` / `--axis-max` | 30 / 90 | cadence range mapped onto 0.0–1.0 |
| `--movement` | `none` | scale the left stick by effort: `power`, `cadence`, or `none` |
| `--movement-min` | 0 | effort at which movement starts; 0 lets the game's deadzone decide |
| `--movement-max` | 100 | effort giving full deflection (watts or rpm) |
| `--movement-floor` | 0.0 | minimum scale once above min; 0 means pure scaling |
| `--sprint-at` | — | hold the sprint button at/above this effort |
| `--sprint-button` | `BTN_THUMBL` | button held when sprinting (left stick click) |
| `--button RPM:BTN` | — | hold a button above an rpm threshold; repeatable, e.g. `--button 80:BTN_TR` |
| `--poll-interval` | 0.05 | seconds between BLE poll writes; lower = fresher telemetry |
| `--no-rumble` | off | disable haptic cues on the physical controller |
| `--rumble-passthrough` | off | forward the game's own rumble to your controller |
| `--launch-on-input` | — | command to run on the launch trigger (see below) |
| `--launch-trigger` | `konami` | `konami` or `any` button press |
| `--simulate-bike` | off | sweep cadence 0–95 rpm instead of connecting |
| `--no-controller` | off | bike-driven input only |
| `--no-grab` | off | share the controller instead of taking it exclusively (debugging) |
| `--status` | off | print state once a second |
| `--ride-log DIR` | — | append ride telemetry to a CSV in DIR |

Enabling `--movement` automatically drops `left_stick` from the gate set —
gating *and* scaling the same stick is just the gate with extra steps.

`--status` prints `age=` — seconds since the last telemetry frame. Trust that
over `bike=connected`: a healthy link sits near 1 s, and a climbing age means
telemetry has stopped even if the connection is nominally up.

**Two thresholds, not one.** Hysteresis stops the gate chattering when you hover
at the boundary; the grace period stops one slow pedal stroke killing your input
mid-fight.

**Smoothing** lives in `bike_controller/mapping.py` (`smoothing_per_second`,
default 3.0). Higher is snappier and jitterier. Even at 2.56 Hz, raw cadence is
too steppy to drive an axis directly. Note this applies to the *cadence* axis
and the gate only — movement scale is deliberately unsmoothed, see above.

---

## Tools

| Tool | Purpose |
|---|---|
| `tools/scan.py` | find the bike; flags anything advertising FTMS, CSC or the iFit service |
| `tools/probe_bike.py` | handshake + poll + decode; prints a **per-byte movement table** so you can see which byte tracks your legs |
| `tools/live.py` | live cadence/power/resistance readout with an Hz counter |
| `tools/inspect_controller.py` | list controllers, check for standard codes and axis ranges; `--watch` live-dumps events |
| `tools/test_gamepad.py` | create a virtual pad and verify the kernel/udev requirements |
| `tools/bridge.py` | the bridge |

The probe's movement table is the important one. Published field offsets are a
hypothesis; that table is how you confirm them against *your* console.

---

## Troubleshooting

**Cannot reach the Pi over SSH.** Its DHCP lease changes when it is unplugged
and moved, and the old address may get handed to another device (which gives a
confusing "connection refused" rather than a timeout). Use the mDNS name
`<your-pi>.local` rather than a hard-coded IP.

**Bike not found.** In order of likelihood: (1) the iFit app on a phone or tablet
is holding the connection — these consoles accept exactly one at a time, force-quit
it; (2) the console is asleep, pedal to wake it; (3) the Pi is out of range.

**Bike found but no telemetry.** You are probably not polling. See
"request/response" above.

**Address differs between machines.** macOS reports a CoreBluetooth-assigned
UUID, Linux reports a real MAC. They are not interchangeable — rescan on the
machine you are running from.

**Nothing appears in the browser.** Check `joydev` is loaded and `/dev/input/jsN`
exists. `tools/test_gamepad.py` diagnoses this.

**Two pads in the browser.** Install the udev rule.

**A tool appears to hang with no output.** Python block-buffers stdout when piped
(e.g. through `tee`), so output can sit invisible in an 8 KB buffer. Run with
`python3 -u`. All tools here set line buffering themselves, but anything new
should too.

**The service dies when SSH disconnects.** Do not background it with
`nohup`/`setsid`/`&` over SSH — it does not reliably survive the channel closing,
and overlapping instances fight over the controller grab. Use systemd.

---

## Layout

```
bike_controller/
  bike.py      BLE reader: handshake, poll loop, telemetry decode
  gamepad.py   virtual pad output (uinput) + physical controller input (evdev)
  mapping.py   cadence -> gate/axis/buttons. No BLE or evdev dependency.
  sequence.py  Konami-code matcher (KMP). No BLE or evdev imports either.
tools/         scan, probe, live readout, controller inspector, gamepad test,
               bridge, remoteplay (drives Chromium via DevTools Protocol)
tests/         33 tests: mapping incl. the fail-safe, sequence, haptic cues
systemd/       service unit
udev/          rule hiding the physical pad from browsers
```

Package imports are lazy: the bike half needs `bleak`, the gamepad half needs
`evdev` (Linux only), and neither is a hard requirement of the other. This lets
the mapping logic be tested anywhere and the probe tools run on macOS.

`mapping.py` is deliberately pure logic over numbers, so it is unit-testable
without hardware and survives unchanged if the output layer ever moves from
uinput to USB HID gadget mode.

### Fail-safe

If the BLE link dies mid-ride, the cadence tracker decays to zero and the gate
**closes**. A stuck-open gate handing a game full control from a stationary bike
is the dangerous failure; closing is the safe one.

`tests/test_mapping.py` asserts this directly, along with hysteresis, the grace
period, cold start, and that fail-safe timing does **not** vary with the output
loop's frame rate.

```bash
python3 tests/test_mapping.py        # or: pytest tests/
```

No hardware needed — `mapping.py` has no BLE or evdev dependency and time is
injected, so the suite is deterministic and instant.

### Self-healing

Both feeds supervise themselves, because this runs unattended:

- **Bike**: reconnects forever, with backoff from 3 s to 30 s. Essential — at
  boot the console is asleep and unreachable, and it also sleeps mid-session.
  Just start pedalling and the bridge catches up within a few seconds.

  Three independent detectors, because each misses a case the others catch: a
  BLE `disconnected_callback`; a poll-write failure; and a 10 s timeout on
  waiting for the next frame, which is the only one that catches a console that
  stays *connected* but stops replying. A failure in any of them pushes a
  sentinel that makes `stream()` raise, so the reconnect loop actually runs.
- **Controller**: re-acquired if absent or unplugged, so it can be plugged in
  after boot. While no controller is held, the pad emits nothing.

Neither failure takes the virtual pad down, so the browser never sees the
gamepad disappear and reconnect.

---

## Starting Remote Play from the saddle

The Pi is headless next to the bike, so there is no way to click "Click to start
playing" without VNCing in from another device — and the controller cannot help,
because the bridge grabs it exclusively.

**Enter the Konami code on the controller** (↑ ↑ ↓ ↓ ← → ← → B A) and the bridge
runs `tools/start-remoteplay.sh`, which launches Chromium at the console's Remote
Play URL and drives it via the DevTools Protocol.

Deliberately *not* a boot autostart: the Pi being powered on does not mean anyone
is riding. It is also self-re-arming — it declines to launch while a browser is
already running, so the code does nothing mid-session but brings Remote Play back
if it has died.

Details that took a while to get right:

- **The click is a dispatched input event**, not `element.click()`. A synthetic
  DOM click is not a trusted user gesture and some browser APIs ignore it.
- **The button is hit-tested before clicking.** The dialog exists in the DOM
  *behind* the loading screens, so `display`/`visibility` checks are not enough —
  it is also hidden by opacity and by overlays. `document.elementFromPoint` at
  the click coordinates is the decisive test. Without it, the launcher clicked a
  dormant element, reported success, and left the real dialog untouched.
- **Success means streaming, not clicking.** It polls for a `<video>` genuinely
  playing frames, and re-clicks if none appears. Reporting success at the click
  is what made a stalled page look like a win.
- **It matches on the button's visible text**, so a Microsoft reword breaks it.
  It fails loudly with the page title and body text; `--dry-run` shows what it
  can currently see without clicking.
- **The bridge runs as root, so the script drops to the desktop user** via
  `runuser`. As root, `XDG_RUNTIME_DIR` is `/run/user/0`, which does not exist,
  and the script dies before doing anything.

Log: `/run/user/1000/bike-remoteplay.log`.

## Ride logs

Every telemetry sample is appended to a CSV, one file per bridge run, **only
while you are actually pedalling** — the bridge runs whenever the Pi is on, and
logging the idle hours would be ~10 MB a day of zeros. An unridden day leaves no
file at all.

```
wall_time, t, cadence_rpm, power_w, resistance, movement_scale, sprint, gate_open
```

Logs live at `~/bike-rides/` on the Pi — **outside the repo on purpose**, since
`tools/deploy.sh` rsyncs with `--delete` and would erase them otherwise.

```bash
python3 tools/ride_report.py ~/bike-rides/
```

That prints your power and cadence distribution while riding, how much of the
ride you spent at full speed and sprinting, and suggests `--movement-max` and
`--sprint-at` from the data: full speed at the median of your riding power (so
about half a ride is at full deflection), sprint at the 90th percentile (so it
stays a genuine push). Those thresholds were tuned by feel three times
(130 → 100 → 75); this is how to stop guessing.

The logger deliberately **never calls `Mapper.evaluate()`** — `value()` is a
mutating getter that advances the filter's clock, so a second caller would steal
`dt` from the output loop. Derived fields come from `Status` instead. Logging
failures are swallowed: a full disk must not end a ride.

## Working on this

**The Pi installs and updates from git, exactly as anyone else would.** Work on a
branch, push it, and the Pi pulls that branch:

```bash
git switch -c my-change
# ... edit, commit ...
tools/deploy.sh               # gate, push, and update the Pi to this branch
```

`tools/deploy.sh` is a convenience, not a requirement — it is `git push` plus
`git pull && ./install.sh` over SSH. What it adds is a gate: the suite and
mutation testing run **before** anything leaves your machine, and `install.sh`
runs the self-test on arrival. It refuses to run with a dirty tree, because the
Pi pulls from git and uncommitted work would silently not be deployed.

It reads `PI_HOST` from `config.env`. Use the Pi's **mDNS** name rather than an
IP: its DHCP lease moves whenever it is unplugged, and the old address gets
handed to another device, which presents as a baffling "connection refused".

To do it without the script:

```bash
ssh <your-pi> 'cd bike-controller && git pull && ./install.sh'
```

### Tests

```bash
python3 tests/test_mapping.py     # gate, movement scaling, the fail-safe
python3 tests/test_sequence.py    # Konami matcher
python3 tests/test_cues.py        # haptic cue names line up
python3 tests/test_ridelog.py     # ride logging and rotation
python3 tests/test_watchdog.py    # sd_notify, against a real socket
python3 tests/test_uinput_abi.py  # kernel struct sizes and ioctl numbers
```

On the Pi, `tools/selftest.sh` goes further than the unit tests: it runs the real
bridge for 8 seconds and requires it to survive, bring up force feedback, and
write a ride log. The unit tests never import `tools/bridge.py`, so they cannot
catch a wiring error — that has already shipped a bridge that could not start.

All run anywhere — no hardware, no evdev, no BLE, time injected — so they are
deterministic and instant.

`bike_controller/mapping.py` and `sequence.py` are deliberately free of BLE and
evdev imports so they stay testable; **keep it that way**. `gamepad.py` and
`tools/bridge.py` import `evdev`, which is Linux-only and cannot even be
imported on macOS, so `test_cues.py` inspects them with `ast` instead. That test
exists because `Rumbler.play()` ignores unknown cue names by design (haptics must
never crash the bridge), which means a renamed cue fails **silently** — no error,
no log, no buzz.

### Configuration

Tuning lives in **`config.env`** — edit and `systemctl restart bike-bridge`. No
`daemon-reload`, and no editing a systemd unit to change how hard you have to
pedal. `tools/run-bridge.sh` turns those values into the flags below; anything
not in `config.env` can still be passed directly for one-off experiments.

### Traps already hit

- **The bridge runs as root** (it needs `/dev/uinput` and `EVIOCGRAB`), so
  anything it spawns inherits root — where `XDG_RUNTIME_DIR` is `/run/user/0`,
  which does not exist. `tools/start-remoteplay.sh` drops to the desktop user
  via `runuser`; Chromium must run as the session owner.
- **`pkill -f <pattern>` over SSH kills your own session** when the pattern
  appears in the command you just sent. Use `pkill -f "[r]emoteplay.py"`.
- **Do not background long processes over SSH** — they do not reliably survive
  the channel closing, and two bridges fight over the controller grab. Use
  `systemd-run --unit=... --collect`.

## Ideas, not yet built

- **Decide whether movement scaling needs smoothing.** Currently raw by design.
  The poll rate has since been raised to the measured knee, so if stepping is
  still noticeable a filter is the remaining option — and it costs lag.
- **Tune the thresholds against real rides.** `--movement-max` and `--sprint-at`
  are calibrated from one capture at low resistance.
- **A cheap screen or a phone tap** if the Konami launcher ever proves flaky;
  today it is the only way to start Remote Play without VNC.

## What install.sh does

Everything below is automated; it is here so you know what was changed on your
machine, and so you can undo it.

- `apt install python3-evdev python3-venv`; creates `.venv --system-site-packages`
- Loads `uinput` and `joydev`, persisted via `/etc/modules-load.d/`. **`joydev`
  is the one not loaded by default on Pi OS**, and without it the virtual pad
  exists but stays invisible to the browser.
- Generates `udev/99-bike-controller.rules` from the template with your
  controller's USB IDs, and installs it
- Generates `systemd/bike-bridge.service` from the template with your checkout
  path, and installs it
- Creates `config.env` and prompts for the console ID

The generated unit and udev rule are gitignored: they belong to your machine, not
to the repo.

