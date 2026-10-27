"""python3 -I test_keybloom.py: the helper's logic against a fake keyboard (no hardware, no real state)."""
import importlib.util
import os
import json
import tempfile

spec = importlib.util.spec_from_file_location("keybloom", os.path.join(os.path.dirname(os.path.abspath(__file__)), "keybloom.py"))
kg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kg)


class FakeKeyboard(kg.Keyboard):
    def __init__(self):
        super().__init__()
        self.ram = {"effect": 6, "speed": 127, "brightness": 255, "hue": 25, "sat": 241}
        self.saves = 0
        self.protocol = 0x000B
        self.count = 23   # Q1 v1 ISO: 22 effects + off
        self.keeps = True
        self.rgb = True

    def connect(self):
        self.fd, self.dev, self.pid = -1, "/dev/fake", "0102"
        return True

    def read(self):
        if not self.rgb:
            raise kg.Unsupported("no rgb matrix")
        return dict(self.ram)

    def count_effects(self, current):
        return self.count, current if self.keeps else current + 1

    def set(self, look, have):
        self.ram = dict(look)
        return dict(look)

    def save(self):
        self.saves += 1


def helper(tmp, entry=None, **fake):
    """A helper on a fake keyboard, started with Keybloom's shell.json entry."""
    h = kg.Helper()
    h.kb = FakeKeyboard()
    for k, v in fake.items():
        setattr(h.kb, k, v)
    h.handle({"init": entry or {}})
    return h


with tempfile.TemporaryDirectory() as tmp:
    h = helper(tmp)
    assert h.state["effect"] == 6 and h.state["colorMode"] == "own", "first run adopts the keyboard's look"

    h.handle({"step": 1}); assert h.kb.ram["effect"] == 7
    h.handle({"set": {"effect": 22}}); h.handle({"step": 1}); assert h.state["effect"] == 1, "wraps, skips off"
    h.handle({"step": -1}); assert h.state["effect"] == 22

    h.handle({"set": {"brightness": 300, "speed": -1, "hue": True, "effect": 99}})
    assert h.state["brightness"] == 255 and h.state["speed"] == 127 and h.state["effect"] == 22, "bad values ignored/clamped"

    h.handle({"idle": True}); assert h.kb.ram["brightness"] == 0 and h.state["brightness"] == 255
    h.handle({"idle": False}); assert h.kb.ram["brightness"] == 255

    h.handle({"store": True}); h.handle({"store": True}); assert h.kb.saves == 1, "store twice = one flash write"
    for _ in range(50):
        h.handle({"step": 1})
    assert h.kb.saves == 1, "live changes never write flash"
    assert os.listdir(tmp) == [] and h.status()["settings"]["effect"] == h.state["effect"], "no files: settings go to shell.json"

    h.handle({"set": {"colorMode": "rainbow", "speed": 255}})
    a, b = h.look(0.0)["hue"], h.look(1.5)["hue"]
    assert a != b and h.state["hue"] == 25, "rainbow moves the hue, keeps your color"
    h.handle({"set": {"brightness": 200}}); h.handle({"store": True})
    assert h.kb.saves == 2 and h.kb.ram["hue"] == h.state["hue"], "store saves the still look, not a rainbow frame"
    h.handle({"set": {"hue": 100}}); assert h.state["colorMode"] == "own", "picking a color leaves rainbow"
    h.handle({"set": {"colorMode": "bogus"}}); assert h.state["colorMode"] == "own"

    h.music.proc, h.music.level, h.music.sound_at = object(), 0.0, 0.0  # fake capture: nothing played yet
    assert h.look(100)["brightness"] == 200, "music on, nothing playing: your brightness exactly"
    h.music.sound_at = 99.5
    assert h.look(100)["brightness"] == 60, "music: quiet part = 30% of your brightness, not darker"
    h.music.level = 1.0
    assert h.look(100)["brightness"] == 200, "music: loud = your brightness"
    assert h.look(102)["brightness"] == 200, "2 s of silence: back to your brightness"
    h.idle, h.music.sound_at = True, 98
    assert h.look(100)["brightness"] == 200, "music playing keeps the lights on while idle"
    assert h.look(110)["brightness"] == 0, "silence + idle = off"
    h.idle = False
    h.state["idleSec"], h.idle = 0, True
    assert h.look(110)["brightness"] == 200, "idleSec 0: never off"
    h.state["idleSec"] = 60
    assert h.look(110)["brightness"] == 0
    h.idle = False
    h.state["effect"] = 18  # Reactive: a typing effect
    h.music.sound_at = 99.5
    h.music.level = 0.0; quiet = h.look(100)
    h.music.level = 1.0; loud = h.look(100)
    assert quiet["brightness"] == loud["brightness"] == 200 and quiet["hue"] != loud["hue"], "typing effects: music shifts color, never dims"
    h.state["musicTyping"] = False
    assert h.look(100) == h.lights(), "switch off: music leaves typing effects alone"
    h.music.proc = None

    h.handle({"set": {"colorMode": "rainbow", "effect": 4}})  # Cycle all ignores the color
    assert not h.animated(), "rainbow pauses on effects that bring their own colors"
    h.handle({"set": {"effect": 1}}); assert h.animated()
    h.handle({"set": {"colorMode": "own"}})

    before = dict(h.saved)
    h.handle({"set": {"speed": 7}, "preview": True})
    assert h.kb.ram["speed"] == 7 and h.saved == before, "preview: applied, not saved"
    h.handle({"set": {"speed": 7}}); assert h.status()["settings"]["speed"] == 7

    h.handle({"set": {"colorMode": "theme"}})
    assert "hue" not in h.saved and "sat" not in h.saved, "theme mode: the color is the theme's, not saved"
    before = dict(h.saved)
    h.state["hue"] = 99; h.handle({"theme": True})
    assert h.saved == before, "a theme switch does not rewrite shell.json"
    h.handle({"set": {"colorMode": "own"}})

    h.handle({"set": {"idleSec": 0}}); assert h.state["idleSec"] == 0
    h.handle({"set": {"idleSec": 3601}}); h.handle({"set": {"idleSec": True}}); assert h.state["idleSec"] == 0, "bad idleSec ignored"
    h.handle({"set": {"idleSec": 300}}); assert h.status()["settings"]["idleSec"] == 300

    entry = json.loads(json.dumps(h.status()["settings"]))  # as the popup writes it to shell.json and reads it back
    h2 = helper(tmp, {"settings": entry})
    assert h2.state["stored"] is not None and h2.kb.saves == 0 and not h2.adopt, "settings survive a restart, re-applied to RAM only"
    assert h2.lights() == h.lights() and h2.saved == h.saved

# nothing reaches the keyboard before init
h = kg.Helper()
h.kb = FakeKeyboard()
h.handle({"set": {"effect": 3}}); h.handle({"step": 1}); h.handle({"store": True})
assert h.kb.fd is None and h.kb.ram["effect"] == 6 and h.kb.saves == 0, "no init: hands off"

# malformed entries: ignored, never fatal
with tempfile.TemporaryDirectory() as tmp:
    for bad in ({"settings": [1, 2], "boards": [3]}, {"settings": "x", "boards": {"0102": "x"}}, [1], "x"):
        h = helper(tmp, bad)
        assert h.adopt is False and h.state["effect"] == 6 and h.user_boards == {}, bad

# keyboards that cannot (fully) be driven: say so, and send blocked ones nothing
with tempfile.TemporaryDirectory() as tmp:
    h = helper(tmp, protocol=0x000A)
    before = dict(h.kb.ram)
    h.handle({"set": {"effect": 3}}); h.handle({"step": 1}); h.handle({"store": True})
    st = h.status()
    assert st["blocked"] and "protocol 10" in st["warning"] and h.kb.ram == before and h.kb.saves == 0, "old firmware: hands off"
    h = helper(tmp, rgb=False)
    assert h.status()["blocked"] and "no RGB" in h.status()["warning"], "no rgb matrix channel"
    h = helper(tmp, keeps=False)
    assert not h.blocked and "did not keep" in h.status()["warning"], "change not kept: warn, keep going"
    h = helper(tmp, count=300)
    assert not h.blocked and "looks wrong" in h.status()["warning"] and len(h.effects) >= h.kb.ram["effect"] + 1
    h = helper(tmp)
    assert h.status()["warning"] == "" and not h.blocked

# why connect() found nothing: no access (no udev rule), or no answer (dongle/Bluetooth)
kb = kg.Keyboard()
real_candidates = kg.via_hidraw_candidates
with tempfile.TemporaryDirectory() as tmp:
    locked = os.path.join(tmp, "hidraw-locked")
    open(locked, "w").close(); os.chmod(locked, 0)
    kg.via_hidraw_candidates = lambda: iter([locked])
    if os.geteuid() != 0:  # root opens anything
        assert not kb.connect() and kb.why == kg.NO_ACCESS
    kg.via_hidraw_candidates = lambda: iter(["/dev/null"])  # a char device that never answers
    assert not kb.connect() and kb.why == kg.NO_ANSWER
    kg.via_hidraw_candidates = lambda: iter([])
    assert not kb.connect() and kb.why == kg.NOT_FOUND
kg.via_hidraw_candidates = real_candidates

# the real Keyboard class against a fake device: a socket pair passes whole 32/33-byte packets like hidraw
import socket
host, dev = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
kb = kg.Keyboard()
kb.fd = host.fileno()
dev.send(bytes([0x08, 3, 2, 99]) + bytes(28))          # a stale reply nobody asked for
dev.send(bytes([0x42]) + bytes(31))                    # and a stray report
import threading
def answer():
    req = dev.recv(64)
    dev.send(bytes([0x42]) + bytes(31))                # stray report first, then the real reply
    dev.send(req[1:4] + bytes([6]) + bytes(28))
threading.Thread(target=answer).start()
assert kb.get(kg.VAL_EFFECT)[0] == 6, "stale and stray packets are skipped"
def unhandled():
    req = dev.recv(64)
    dev.send(bytes([kg.CMD_UNHANDLED]) + req[2:33])         # QMK: first byte 0xFF, the rest of the request kept
threading.Thread(target=unhandled).start()
try:
    kb.get(kg.VAL_EFFECT)
    raise AssertionError("an unhandled command must raise Unsupported")
except kg.Unsupported:
    pass
dev.close()
try:
    kb.drain()
    raise AssertionError("an unplugged keyboard must raise")
except OSError:
    pass
host.close()

names, typing, color, model, note = kg.effect_list("0102", 23)
assert names[18] == "Reactive" and typing == [16, 18, 19, 20, 21, 22] and color == [1, 2, 3, 18, 19, 20, 22], "Q1 table"
names, typing, color, model, note = kg.effect_list("0102", 30)
assert names[18] == "Effect 18" and typing == [] and model == "" and "has 29" in note, "other firmware: numbers only"
assert kg.effect_list("ffff", 5)[0] == ["Off", "Effect 1", "Effect 2", "Effect 3", "Effect 4"], "unknown model"

# your own keyboards (the "boards" setting): QMK names get labels, anything else shows as written
mine = kg.parse_boards({"FFFF": {"model": "My board", "effects": ["solid_color", "solid_splash", "Fireworks"]},
                        "0001": {"effects": ["x\u0007"]}, "nope": {"effects": ["solid_color"]}, "0002": [1]})
assert mine.keys() == {"ffff"}, "bad entries dropped"
names, typing, color, model, note = kg.effect_list("ffff", 4, mine)
assert names == ["Off", "Solid color", "Solid splash", "Fireworks"] and typing == [2] and color == [1, 2], names
assert model == "My board" and note == ""
assert "Your boards setting lists 3" in kg.effect_list("ffff", 9, mine)[4]
assert kg.parse_boards([1]) == {} and kg.parse_boards("x") == {} and kg.effect_list("0102", 23, {})[3] == "Q1V1 ISO"

# auto cycle: in order or at random, typing effects left out, steps not saved, your pick restarts the wait
with tempfile.TemporaryDirectory() as tmp:
    h = helper(tmp, {"settings": {"effect": 1, "cycle": "order", "cycleSec": 10}})
    assert h.state["cycle"] == "order" and h.saved["cycle"] == "order" and h.saved["cycleSec"] == 10
    assert not h.cycle_tick(100.0) and h.next_cycle == 110.0, "first tick only starts the wait"
    assert not h.cycle_tick(109.9) and h.wait(109.0) == 1.0 and h.wait(100.0) == 2.0, "sleeps until the step is due (max 2 s)"
    seen = []
    for t in range(110, 400, 10):
        assert h.cycle_tick(float(t)); seen.append(h.kb.ram["effect"])
    typing = set(h.typing_effects)
    assert seen[:3] == [2, 3, 4] and not typing & set(seen) and set(seen) == set(range(1, 23)) - typing, seen
    assert h.saved["effect"] == 1 and h.state["effect"] == seen[-1], "steps show on the keyboard, shell.json keeps your pick"
    h.handle({"set": {"effect": 5}})
    assert h.saved["effect"] == 5 and h.next_cycle is None, "your pick is saved and restarts the wait"
    h.handle({"set": {"cycle": "random"}})
    h.cycle_tick(1000.0)
    picks = []
    for t in range(1, 60):
        before = h.state["effect"]
        assert h.cycle_tick(1000.0 + 10 * t) and h.state["effect"] != before and h.state["effect"] not in typing
        picks.append(h.state["effect"])
    assert len(set(picks)) > 5, "random really varies"
    h.idle = True
    assert not h.cycle_tick(5000.0) and h.next_cycle is None, "no cycling while the lights are off for idle"
    h.idle = False
    h.handle({"set": {"cycle": "sideways", "cycleSec": 2}}); h.handle({"set": {"cycleSec": 4000}})
    assert h.state["cycle"] == "random" and h.state["cycleSec"] == 10, "bad cycle values ignored"
    h.handle({"set": {"cycle": "off"}})
    assert not h.cycle_tick(9000.0) and not h.cycle_tick(9999.0) and h.wait(9999.0) == 2.0

# edited by hand: `omarchy bar set ... speed 200` stores "200"; a value that makes no sense keeps the current one
with tempfile.TemporaryDirectory() as tmp:
    h = helper(tmp, {"settings": {"effect": 5, "speed": 113, "music": False}})
    h.handle({"init": {"settings": dict(h.saved, speed="200", music="true")}})
    assert h.state["speed"] == 200 and h.state["music"] is True and h.saved["speed"] == 200, "text numbers/booleans taken"
    h.handle({"init": {"settings": dict(h.saved, speed="fast", effect=999, idleSec=-1)}})
    assert h.state["speed"] == 200 and h.state["effect"] == 5 and h.state["idleSec"] == 120, "unusable values keep the current ones"

# re-init while connected (boards edited in shell.json): names follow at once
with tempfile.TemporaryDirectory() as tmp:
    h = helper(tmp)
    h.handle({"init": {"settings": h.saved, "boards": {"0102": {"model": "Renamed", "effects": ["x"] * 22}}}})
    assert h.status()["model"] == "Renamed" and h.effects[1] == "x"

print("ok")
