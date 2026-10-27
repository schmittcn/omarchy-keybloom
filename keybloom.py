#!/usr/bin/python3 -I
"""Keybloom helper: drives a Keychron (QMK/VIA) keyboard's RGB matrix for the Omarchy bar widget.

`keybloom.py serve` runs for as long as the widget is loaded. It reads one JSON command per line on
stdin and answers every command with one JSON status line on stdout. It touches the keyboard only
after the first command, init:

  {"init": {"settings": {...}, "boards": {...}}}
                               your settings and keyboards from Keybloom's shell.json entry (again whenever
                               that entry changes); an empty entry: take over what the keyboard shows
  {"set": {"effect": 4, "speed": 128, "brightness": 255, "hue": 25, "sat": 240,
           "colorMode": "own"|"theme"|"rainbow", "music": false, "musicTyping": true, "idleSec": 120}}
                               any subset; picking a hue switches colorMode to "own"
  {"step": 1} / {"step": -1}   next / previous effect (skips "off")
  {"theme": true}              the Omarchy theme changed: re-read keyboard.rgb if colorMode is "theme"
  {"idle": true|false}         dim to 0 while idle (idleSec > 0; the shell measures it), restore on resume
  {"read": true}               adopt what the keyboard shows now (the Fn lighting keys change it too)
  {"store": true}              save to the keyboard's onboard memory; no-op when nothing changed
  "preview": true              on any command: apply, but do not rewrite state.json (slider drags)

Flash wear: every change is sent with VIA's custom_set_value, which QMK applies with the
*_noeeprom functions (RAM only). The one command that writes flash, custom_save, is sent only on
{"store": true}, i.e. only when you press "Store on keyboard". After a replug the helper re-applies
your settings to RAM, so storing is never needed for normal use. Rainbow and music animate by
sending set commands ~30 times a second: RAM only as well.

Music: `pw-record` captures what your speakers play (8 kHz mono, in memory only, never stored) and
the loudness drives the brightness.

Your settings live inline on Keybloom's entry in ~/.config/omarchy/shell.json, as Omarchy's storage rules
ask. The popup writes them there (status "settings"); this helper writes no files.
"""
import colorsys
import glob
import json
import os
import pwd
import re
import select
import stat
import subprocess
import sys
import time
from array import array

# Effect names per keyboard: effects.json (tools/gen_effects.py) lists, by USB product id, the animations
# each Keychron's firmware enables, in the order the firmware numbers them (0 is "off"). Used only when the
# keyboard reports the same number of effects; otherwise they show as "Effect N".
EFFECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "effects.json")
# effects that light up on keypresses: the "while typing" ones
TYPING_KEYS = {"typing_heatmap", "solid_reactive_simple", "solid_reactive", "solid_reactive_wide",
               "solid_reactive_multiwide", "solid_reactive_cross", "solid_reactive_multicross",
               "solid_reactive_nexus", "solid_reactive_multinexus", "splash", "multisplash", "solid_splash",
               "solid_multisplash"}
# effects drawn in the chosen color; the others bring their own (rainbows, random, heatmap)
COLOR_KEYS = {"solid_color", "alphas_mods", "breathing", "band_sat", "band_val", "band_pinwheel_sat",
              "band_pinwheel_val", "band_spiral_sat", "band_spiral_val", "hue_breathing", "starlight",
              "starlight_smooth", "starlight_dual_sat", "solid_reactive_simple", "solid_reactive",
              "solid_reactive_wide", "solid_reactive_multiwide", "solid_reactive_cross",
              "solid_reactive_multicross", "solid_reactive_nexus", "solid_reactive_multinexus", "solid_splash",
              "solid_multisplash"}
COLOR_MODES = ("own", "theme", "rainbow")
FRAME = 1 / 30  # rainbow/music update interval, seconds

CMD_PROTOCOL = 0x01
MIN_PROTOCOL = 0x000B  # VIA protocol 11 (QMK 0.19, late 2022): the channel commands below; 10 used 0x07 differently
CMD_UNHANDLED = 0xFF   # the firmware's answer to a command it does not have

# what the popup says when a keyboard cannot be driven
NOT_FOUND = "No Keychron keyboard found. Plug it in with the USB cable."
NO_ACCESS = ("A Keychron is connected, but Keybloom may not open it: add the udev rule from the README, then "
             "replug the keyboard.")
NO_ANSWER = ("A Keychron is connected but does not answer. Lighting control needs the USB cable: the 2.4 GHz "
             "Link dongle and Bluetooth do not pass it on.")
OLD_FIRMWARE = ("This keyboard's firmware is too old (VIA protocol {}; Keybloom needs 11 or newer, QMK from late "
                "2022 on). Update it with Keychron Launcher. Until then Keybloom sends it nothing.")
NO_RGB = ("This keyboard's firmware has no RGB lighting control over VIA (white-only backlight, or RGB not "
          "built in), so Keybloom cannot change its lighting.")
NOT_KEPT = ("The keyboard did not keep a test change (effect {} read back as {}). Its firmware may not fully "
            "support lighting control over VIA; changes may not show.")
ODD_COUNT = "The keyboard reported {} effects, which looks wrong: effects are numbered, some may be missing."


class Unsupported(Exception):
    """The firmware answered "unhandled": it has no such command. Not a lost keyboard, so not an OSError."""
CMD_SET, CMD_GET, CMD_SAVE = 0x07, 0x08, 0x09
CHANNEL_RGB_MATRIX = 0x03
VAL_BRIGHTNESS, VAL_EFFECT, VAL_SPEED, VAL_COLOR = 0x01, 0x02, 0x03, 0x04
LIGHT_KEYS = ("effect", "speed", "brightness", "hue", "sat")
DEFAULT_STATE = {"effect": 1, "speed": 128, "brightness": 255, "hue": 0, "sat": 255,
                 "colorMode": "theme", "music": False, "musicTyping": True, "idleSec": 120, "stored": None}


def parse_settings(data, base=None):
    """(state, found): settings from Keybloom's shell.json entry laid over `base`
    (defaults), every value checked: one that is not usable keeps base's. `found` is False when nothing
    usable was there (first run)."""
    state, found = dict(base or DEFAULT_STATE), False
    if not isinstance(data, dict):
        return state, False
    # `omarchy bar set <id> speed 200` without --json stores "200": take it as the number it means
    data = {k: (int(v) if isinstance(v, str) and re.fullmatch(r"[0-9]{1,4}", v)
                else v == "true" if v in ("true", "false") else v) for k, v in data.items()}
    for k in LIGHT_KEYS:
        if byte(data.get(k)) is not None:
            state[k], found = data[k], True
    for k in ("music", "musicTyping"):
        if isinstance(data.get(k), bool):
            state[k], found = data[k], True
    if idle_seconds(data.get("idleSec")) is not None:
        state["idleSec"], found = data["idleSec"], True
    if data.get("colorMode") in COLOR_MODES:
        state["colorMode"], found = data["colorMode"], True
    stored = data.get("storedOnKeyboard")
    if isinstance(stored, dict) and all(byte(stored.get(k)) is not None for k in LIGHT_KEYS):
        state["stored"] = {k: stored[k] for k in LIGHT_KEYS}
    return state, found


def read_json(path):
    """A small JSON file (size-limited); raises OSError/ValueError."""
    with open(path, "rb") as f:
        raw = f.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("bigger than 4 MB")
    try:
        return json.loads(raw)
    except RecursionError:
        raise ValueError("nested too deeply")


def idle_seconds(v):
    """Seconds until the lights go out when idle (0 = never), 0..3600, from untrusted JSON; or None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3600 else None


def byte(v):
    """A 0..255 int from untrusted JSON, or None."""
    return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 else None


# --- Portions from omakeychron (bin/omakeychron-apply.py at f9a1204), https://github.com/paulsp94/omakeychron
# Copyright (c) 2026 paulsp94, MIT License: see LICENSE, which carries this copyright line and the notice.
#
# Taken: read_theme_color, vivid, via_hidraw_candidates, _read_sysfs (lightly adapted: no exits,
# returns instead of raising where the long-running helper needs to keep going).

VENDOR_ID = "3434"  # Keychron
MAX_RGB_BYTES = 64


def read_theme_color():
    """Bounded, descriptor-validated read of the theme's keyboard.rgb: "rrggbb", or None if absent."""
    home = pwd.getpwuid(os.geteuid()).pw_dir
    path = os.path.join(home, ".local/state/omarchy/current/theme/keyboard.rgb")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_size > MAX_RGB_BYTES:
            raise PermissionError("refusing keyboard.rgb (not a small regular file owned by us)")
        os.set_blocking(fd, True)
        data = os.read(fd, MAX_RGB_BYTES + 1)
        if len(data) > MAX_RGB_BYTES:
            raise PermissionError("keyboard.rgb grew past the limit")
    finally:
        os.close(fd)
    text = data.decode("ascii", "strict").strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", text):
        raise ValueError("keyboard.rgb does not hold one RRGGBB color")
    return text.lower()


def vivid(hexcolor):
    """Boost saturation and value so theme palette colors read true on LEDs.

    Near-grays pass through untouched. LEDs render the green-cyan band bluer than screens, so hues
    between 120 and 180 degrees are pulled most of the way back to green.
    """
    r, g, b = (int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s >= 0.15:
        s = max(s, 0.9)
        v = 1.0
        deg = h * 360
        if 120 < deg < 180:
            h = (120 + (deg - 120) * 0.2) / 360
    return "%02x%02x%02x" % tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h, s, v))


def via_hidraw_candidates():
    """Yield /dev/hidraw nodes of Keychron devices whose descriptor declares the VIA usage page."""
    for uevent in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            info = _read_sysfs(uevent)
        except OSError:
            continue
        if f"HID_ID=0003:0000{VENDOR_ID}:".encode() not in info:
            continue
        desc_path = os.path.join(os.path.dirname(uevent), "report_descriptor")
        try:
            desc = _read_sysfs(desc_path)
        except OSError:
            continue
        if desc.startswith(b"\x06\x60\xff\x09\x61"):  # usage page 0xFF60, usage 0x61
            yield "/dev/" + uevent.split("/")[4]


def _read_sysfs(path, cap=4096):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
            raise OSError("not a root-owned sysfs file")
        return os.read(fd, cap)
    finally:
        os.close(fd)

# --- end of omakeychron portions


def text(v):
    return isinstance(v, str) and 0 < len(v) <= 64 and v.isprintable()


def parse_boards(data):
    """{pid: {"model", "effects"}} from untrusted data; malformed entries are dropped, never fatal."""
    boards = {}
    for pid, b in (data.items() if isinstance(data, dict) else ()):
        effects = b.get("effects") if isinstance(b, dict) else None
        if (isinstance(pid, str) and re.fullmatch(r"[0-9A-Fa-f]{4}", pid) and isinstance(effects, list)
                and 0 < len(effects) < 255 and all(text(e) for e in effects)):
            boards[pid.lower()] = {"model": b["model"] if text(b.get("model")) else "", "effects": effects}
    return boards


def shipped_table():
    """({name key: label}, boards) from effects.json next to this file."""
    try:
        data = read_json(EFFECTS_FILE)
    except (OSError, ValueError):
        return {}, {}
    names = data.get("names") if isinstance(data, dict) else None
    names = {k: v for k, v in names.items() if text(k) and text(v)} if isinstance(names, dict) else {}
    return names, parse_boards(data.get("boards") if isinstance(data, dict) else None)


def effect_list(pid, count, user_boards=None):
    """(names, typing ids, color ids, model, note) for a keyboard with effects 0..count-1.

    Your own keyboards (the "boards" setting) win over the shipped table. An entry is used only when it
    lists as many effects as the keyboard reports; `note` says why not otherwise."""
    note = ""
    names, boards = shipped_table()
    source = "The shipped table"
    if pid in (user_boards or {}):
        boards[pid], source = user_boards[pid], "Your boards setting"
    board = boards.get(pid)
    if board and len(board["effects"]) + 1 == count:
        keys = [None] + board["effects"]
        # QMK effect names get a label and count as typing/color effects; anything else shows as written
        return (["Off"] + [names.get(k, k) for k in board["effects"]],
                [i for i, k in enumerate(keys) if k in TYPING_KEYS],
                [i for i, k in enumerate(keys) if k in COLOR_KEYS], board["model"], note)
    if board:
        note = (f"{source} lists {len(board['effects'])} effects for {pid}, "
                f"the keyboard has {count - 1}")
    # unknown model or other firmware: numbers only; nothing counts as "typing", nothing gets the
    # "brings its own colors" hint
    return ["Off"] + [f"Effect {i}" for i in range(1, count)], [], list(range(1, count)), "", note


def theme_hue_sat():
    """The theme's keyboard color as VIA hue/sat (0..255 each), or None."""
    color = read_theme_color()
    if color is None:
        return None
    r, g, b = (int(vivid(color)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return round(h * 255) % 256, round(s * 255)


class Keyboard:
    """One open VIA interface. Every method raises OSError once the keyboard is gone."""

    def __init__(self):
        self.fd = None
        self.dev = ""
        self.pid = ""   # USB product id, e.g. "0102"
        self.protocol = 0
        self.why = ""   # why connect() found nothing: one of the messages above

    def connect(self):
        self.why = NOT_FOUND
        for dev in via_hidraw_candidates():
            try:
                fd = os.open(dev, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
            except PermissionError:
                self.why = NO_ACCESS  # no udev rule
                continue
            except OSError:
                continue
            self.fd, self.dev = fd, dev
            try:
                uevent = _read_sysfs(f"/sys/class/hidraw/{os.path.basename(dev)}/device/uevent")
                m = re.search(rb"HID_ID=0003:0000" + VENDOR_ID.encode() + rb":0000([0-9A-Fa-f]{4})", uevent)
                self.pid = m.group(1).decode().lower() if m else ""
            except OSError:
                self.pid = ""
            try:
                if stat.S_ISCHR(os.fstat(fd).st_mode):
                    reply = self.xfer([CMD_PROTOCOL])
                    if len(reply) < 3:
                        raise OSError("short reply")
                    self.protocol = reply[1] << 8 | reply[2]
                    return True
            except (OSError, Unsupported):
                if self.why == NOT_FOUND:
                    self.why = NO_ANSWER  # the Link dongle's dead interface never answers
            self.close()
        return False

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
        self.fd, self.dev = None, ""

    def xfer(self, payload):
        self.drain()  # a stale reply (e.g. to VIA open at the same time) must not pass for ours
        os.write(self.fd, b"\x00" + bytes(payload) + bytes(32 - len(payload)))
        deadline = time.monotonic() + 1.0
        while True:
            ready, _, _ = select.select([self.fd], [], [], max(0, deadline - time.monotonic()))
            if not ready:
                raise OSError("keyboard did not answer")
            reply = os.read(self.fd, 32)
            if not reply:
                raise OSError("keyboard gone")
            if reply[:1] == bytes(payload[:1]):  # VIA echoes the command id
                return reply
            if reply[0] == CMD_UNHANDLED and reply[1:3] == bytes(payload[1:3]):  # QMK keeps the rest of the request
                raise Unsupported(f"command {payload[0]:#04x} not supported")

    def drain(self):
        """Read whatever is waiting. Raises OSError when the keyboard is gone (unplugged, suspended)."""
        while select.select([self.fd], [], [], 0)[0]:
            if not os.read(self.fd, 32):
                raise OSError("keyboard gone")

    def get(self, value_id):
        return self.xfer([CMD_GET, CHANNEL_RGB_MATRIX, value_id])[3:5]

    def read(self):
        b, e, s, c = (self.get(v) for v in (VAL_BRIGHTNESS, VAL_EFFECT, VAL_SPEED, VAL_COLOR))
        return {"brightness": b[0], "effect": e[0], "speed": s[0], "hue": c[0], "sat": c[1]}

    def set(self, look, have):
        """Send the parts of `look` that differ from `have` (what the keyboard shows). RAM only."""
        if look["effect"] != have.get("effect"):
            self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_EFFECT, look["effect"]])
        if look["speed"] != have.get("speed"):
            self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_SPEED, look["speed"]])
        if (look["hue"], look["sat"]) != (have.get("hue"), have.get("sat")):
            self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_COLOR, look["hue"], look["sat"]])
        if look["brightness"] != have.get("brightness"):
            self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_BRIGHTNESS, look["brightness"]])
        return dict(look)

    def count_effects(self, current):
        """(count, kept): how many effects the firmware has (incl. 0 = off), and whether it took the change.
        Asks for effect 255, which QMK clamps to its last effect, reads that back, puts `current` back and
        checks that it is back. RAM only, like every set."""
        self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_EFFECT, 255])
        last = self.get(VAL_EFFECT)[0]
        self.xfer([CMD_SET, CHANNEL_RGB_MATRIX, VAL_EFFECT, current])
        back = self.get(VAL_EFFECT)[0]
        return last + 1, back

    def save(self):
        """The only flash write: VIA custom_save -> eeconfig_update_rgb_matrix()."""
        self.xfer([CMD_SAVE, CHANNEL_RGB_MATRIX])


class Music:
    """Loudness of what the speakers play, 0..1, from pw-record. Audio stays in memory, a few ms at a time."""

    def __init__(self):
        self.proc = None
        self.rest = b""
        self.level = 0.0
        self.peak = 500.0    # auto gain: follows the loudest recent part, so quiet and loud music both pulse
        self.sound_at = 0.0  # last time something was playing
        self.error = ""

    @property
    def fd(self):
        return self.proc.stdout.fileno() if self.proc else None

    def start(self):
        if self.proc:
            return
        try:
            self.proc = subprocess.Popen(
                ["/usr/bin/pw-record", "-P", "{ stream.capture.sink=true }", "--rate", "8000", "--channels", "1",
                 "--format", "s16", "-"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            os.set_blocking(self.proc.stdout.fileno(), False)
            self.error = ""
        except OSError as e:
            self.proc, self.error = None, f"Music needs pw-record (PipeWire): {e}"

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(1)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc.stdout.close()
        self.proc, self.rest, self.level = None, b"", 0.0

    def feed(self):
        try:
            chunk = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        if not chunk:
            self.stop()
            self.error = "pw-record stopped"
            return
        data = self.rest + chunk
        cut = len(data) - len(data) % 2
        self.rest = data[cut:]
        samples = array("h", data[:cut])
        if not samples:
            return
        rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
        if rms > 200:
            self.sound_at = time.monotonic()
        # ponytail: plain RMS with fast attack / slow release; add beat detection if a pulse feels too smooth
        self.peak = max(rms, self.peak * 0.995, 500.0)
        level = min(1.0, rms / self.peak)
        self.level = level if level > self.level else self.level * 0.85


class Helper:
    def __init__(self):
        self.ready = False   # init has arrived: before it, the keyboard is left alone
        self.adopt = False   # first run: take over what the keyboard shows instead of overwriting it
        self.state = dict(DEFAULT_STATE)
        self.user_boards = {}
        self.saved = {}      # the settings as they should stand in shell.json
        self.count = 0       # effects the connected keyboard has (0: not known yet)
        self.kb = Keyboard()
        self.music = Music()
        self.have = {}   # what the keyboard shows now (RAM), as far as we know
        self.idle = False
        self.error = ""
        self.effects, self.typing_effects, self.color_effects, self.model, self.effects_note = ["Off"], [], [], "", ""
        self.warning = ""     # something about this keyboard you should know (firmware, support)
        self.blocked = False  # ... and it means Keybloom sends it nothing

    def lights(self):
        return {k: self.state[k] for k in LIGHT_KEYS}

    def persisted(self):
        """What goes into the shell.json entry. In Theme mode the color comes from the theme: not stored, so a
        theme switch does not rewrite shell.json."""
        s = {k: self.state[k] for k in ("effect", "speed", "brightness", "colorMode", "music", "musicTyping", "idleSec")}
        if self.state["colorMode"] != "theme":
            s["hue"], s["sat"] = self.state["hue"], self.state["sat"]
        if self.state["stored"]:
            s["storedOnKeyboard"] = self.state["stored"]
        if self.user_boards:
            s["boards"] = self.user_boards
        return s

    def init(self, data):
        """Settings and keyboards from the shell.json entry. The first time, an empty entry means: take over
        what the keyboard shows."""
        data = data if isinstance(data, dict) else {}
        first = not self.ready
        state, found = parse_settings(data.get("settings"), None if first else self.state)
        if found:
            self.state = state
        elif first:
            self.adopt = True
        self.user_boards = parse_boards(data.get("boards"))
        if self.count:  # already connected: the effect names may have changed with your boards
            self.effects, self.typing_effects, self.color_effects, self.model, self.effects_note = \
                effect_list(self.kb.pid, self.count, self.user_boards)
        self.ready = True
        self.apply_theme()
        self.saved = self.persisted()

    def animated(self):
        # rainbow only animates effects drawn in the chosen color; the others would just get useless updates
        rainbow = self.state["colorMode"] == "rainbow" and self.state["effect"] in self.color_effects
        return self.kb.fd is not None and not self.blocked and (rainbow or self.music.proc is not None)

    def look(self, now=None):
        """What the keyboard should show right now: your settings, animated, dark while idle."""
        now = time.monotonic() if now is None else now
        look = self.lights()
        if self.state["colorMode"] == "rainbow":
            period = 30 - self.state["speed"] / 255 * 27  # Speed slider: one round in 30 s .. 3 s
            look["hue"] = int(now / period % 1 * 256) % 256
        # music shapes the light only while something plays: after 2 s of silence your brightness is exactly as set
        beat = self.music.proc is not None and now - self.music.sound_at < 2
        if beat and look["effect"] in self.typing_effects:
            # dimming would dim the key flashes too: shift the color with the beat instead (up to a third of the
            # wheel), or leave typing effects alone when musicTyping is off
            if self.state["musicTyping"]:
                look["hue"] = (look["hue"] + int(self.music.level * 85)) % 256
        elif beat:
            # the Brightness slider is the peak; quiet parts dip to 30% of it, never darker
            look["brightness"] = int(look["brightness"] * (0.3 + 0.7 * self.music.level))
        playing = self.music.proc is not None and now - self.music.sound_at < 5
        if self.idle and self.state["idleSec"] > 0 and not playing:  # music keeps the lights on while it plays
            look["brightness"] = 0
        return look

    def apply_theme(self):
        if self.state["colorMode"] != "theme":
            return
        try:
            hs = theme_hue_sat()
        except (OSError, ValueError, UnicodeDecodeError) as e:
            self.error = f"keyboard.rgb refused: {e}"
            return
        if hs:
            self.state["hue"], self.state["sat"] = hs

    def sync(self):
        """Make the keyboard match look(); (re)connects when needed, starts/stops the music capture."""
        try:
            if self.kb.fd is None:
                if not self.kb.connect():
                    self.music.stop()
                    self.error, self.warning, self.blocked = self.kb.why, "", False
                    return
                self.setup()
            if self.blocked:
                self.music.stop()
                self.error = ""
                return
            if self.state["music"]:
                self.music.start()
            else:
                self.music.stop()
            self.have = self.kb.set(self.look(), self.have)
            self.error = ""
        except OSError as e:
            self.kb.close()
            self.music.stop()
            self.have = {}
            self.error = f"Keyboard lost: {e}"
        except Unsupported:
            self.block(NO_RGB)

    def block(self, warning):
        """Keep the keyboard open (so an unplug is still noticed) but send it nothing more."""
        self.warning, self.blocked = warning, True
        self.music.stop()

    def setup(self):
        """First contact after connecting: check the firmware, learn its effects, take over or re-apply."""
        self.warning, self.blocked = "", False
        if self.kb.protocol < MIN_PROTOCOL:
            return self.block(OLD_FIRMWARE.format(self.kb.protocol))
        try:
            self.have = self.kb.read()
            count, back = self.kb.count_effects(self.have["effect"])
        except Unsupported:
            return self.block(NO_RGB)
        if back != self.have["effect"]:
            self.warning = NOT_KEPT.format(self.have["effect"], back)
        if not 2 <= count <= 128:
            self.warning = ODD_COUNT.format(count - 1)
            count = max(self.have["effect"], self.state["effect"]) + 1  # at least what is in use
        self.count = count
        self.effects, self.typing_effects, self.color_effects, self.model, self.effects_note = \
            effect_list(self.kb.pid, count, self.user_boards)
        self.state["effect"] = min(self.state["effect"], len(self.effects) - 1)  # another model: fewer effects
        if self.adopt:
            self.state.update(self.have)
            self.state["colorMode"] = "own"
            self.adopt = False
        self.apply_theme()
        self.saved = self.persisted()

    def handle(self, cmd):
        changed = False
        if "init" in cmd:
            self.init(cmd["init"])
        if not self.ready:
            return  # nothing touches the keyboard before init
        if isinstance(cmd.get("set"), dict):
            new = cmd["set"]
            for k in LIGHT_KEYS:
                v = byte(new.get(k))
                if v is not None:
                    if k == "effect":
                        v = min(v, len(self.effects) - 1)
                    self.state[k] = v
                    changed = True
            if "hue" in new:
                self.state["colorMode"] = "own"  # you picked a color: neither theme nor rainbow decides
            if new.get("colorMode") in COLOR_MODES:
                self.state["colorMode"] = new["colorMode"]
                changed = True
                self.apply_theme()
            for k in ("music", "musicTyping"):
                if isinstance(new.get(k), bool):
                    self.state[k] = new[k]
                    changed = True
            if idle_seconds(new.get("idleSec")) is not None:
                self.state["idleSec"] = new["idleSec"]
                changed = True
        if cmd.get("step") in (1, -1) and len(self.effects) > 1:
            n = len(self.effects) - 1  # effects 1..n, 0 (off) is skipped
            self.state["effect"] = (self.state["effect"] - 1 + cmd["step"]) % n + 1
            changed = True
        if cmd.get("theme") is True:
            self.apply_theme()
            changed = True
        if isinstance(cmd.get("idle"), bool):
            self.idle = cmd["idle"]
        if cmd.get("read") is True and self.kb.fd is not None and not self.blocked and not self.idle and not self.animated():
            try:
                self.have = self.kb.read()
                if any(self.have[k] != self.state[k] for k in LIGHT_KEYS):
                    self.state.update(self.have)  # changed with the Fn keys: take it over
                    changed = True
            except OSError:
                self.kb.close()
            except Unsupported:
                self.block(NO_RGB)
        self.sync()
        if cmd.get("store") is True and self.kb.fd is not None and not self.blocked:
            if self.lights() != self.state["stored"] and not self.idle:
                try:
                    # the still look, not a rainbow/music frame: save writes whatever RAM holds
                    self.have = self.kb.set(self.lights(), self.have)
                    self.kb.save()
                    self.state["stored"] = self.lights()
                    changed = True
                except (OSError, Unsupported) as e:
                    self.error = f"Store failed: {e}"
        if changed and cmd.get("preview") is not True:  # slider drags preview without rewriting shell.json
            self.saved = self.persisted()

    def status(self):
        return {"connected": self.kb.fd is not None, "device": self.kb.dev, "error": self.error or self.music.error,
                "idle": self.idle, "state": {k: v for k, v in self.state.items() if k != "stored"},
                "storedMatches": self.lights() == self.state["stored"],
                "effects": self.effects, "typingEffects": self.typing_effects, "colorEffects": self.color_effects,
                "model": self.model, "pid": self.kb.pid, "effectsNote": self.effects_note,
                "warning": self.warning, "blocked": self.blocked,
                "settings": self.saved}


def serve():
    helper = Helper()
    buf = b""
    try:
        while True:
            # the keyboard's fd is watched too: an unplug wakes us at once, so a replugged keyboard gets your
            # settings back even when nothing animates
            fds = [sys.stdin] + [fd for fd in (helper.music.fd, helper.kb.fd) if fd is not None]
            ready, _, _ = select.select(fds, [], [], FRAME if helper.animated() else 2.0)
            if helper.music.proc and helper.music.fd in ready:
                helper.music.feed()
            was = helper.kb.fd is not None
            if helper.kb.fd is not None and helper.kb.fd in ready:
                try:
                    helper.kb.drain()  # stray reports are dropped
                except OSError:
                    helper.kb.close()
                    helper.have = {}
            if sys.stdin not in ready:
                if not helper.ready:
                    continue
                if helper.kb.fd is None or helper.animated():
                    helper.sync()  # unplugged: look for it every 2 s; animated: next frame
                if (helper.kb.fd is not None) != was:
                    print(json.dumps(helper.status()), flush=True)
                continue
            chunk = os.read(sys.stdin.fileno(), 4096)
            if not chunk:
                return  # the shell closed us
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    cmd = json.loads(line)
                except ValueError:
                    continue
                if isinstance(cmd, dict):
                    helper.handle(cmd)
                    print(json.dumps(helper.status()), flush=True)
            buf = buf[-65536:]  # a runaway line without newline is dropped, not buffered forever
    finally:
        helper.music.stop()


if __name__ == "__main__":
    if sys.argv[1:] != ["serve"]:
        sys.exit("usage: keybloom.py serve")
    serve()
