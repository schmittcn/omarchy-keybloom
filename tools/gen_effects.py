#!/usr/bin/python3 -I
"""Rebuild effects.json: the RGB effect list of every Keychron keyboard, by USB product id.

A keyboard numbers its effects 1.. in QMK's effect order, counting only the animations its firmware enables
(0 is "off", 1 is always solid color). This walks QMK checkouts and writes that list per product id. With
several checkouts, the first one that has a product id wins.

The shipped effects.json comes from upstream QMK plus Keychron's fork (K Pro, K Max, Q Max, ... live only
there), each a sparse checkout of just the keyboards and the effect list:

  for src in "qmk https://github.com/qmk/qmk_firmware.git master" \
             "kc-2025q3 https://github.com/Keychron/qmk_firmware.git 2025q3" \
             "kc-wireless https://github.com/Keychron/qmk_firmware.git wireless_playground"; do
    set -- $src
    git clone -q --depth 1 --branch $3 --filter=blob:none --sparse $2 $1
    git -C $1 sparse-checkout set keyboards/keychron quantum/rgb_matrix/animations
  done
  python3 -I tools/gen_effects.py qmk kc-2025q3 kc-wireless > effects.json

For a keyboard of your own, run it on the QMK tree its firmware was built from and put that board's entry into
Keybloom's "boards" setting (README "Adding your keyboard").
"""
import json
import os
import re
import sys

# names shown in the popup; anything not listed is derived from the QMK name
NAMES = {
    "solid_color": "Solid color", "alphas_mods": "Alphas/mods", "band_spiral_val": "Band spiral",
    "rainbow_moving_chevron": "Rainbow chevron", "cycle_left_right": "Cycle left/right",
    "cycle_up_down": "Cycle up/down", "cycle_out_in": "Cycle out/in", "cycle_out_in_dual": "Cycle out/in dual",
    "solid_reactive_simple": "Reactive", "solid_reactive": "Reactive hue", "solid_reactive_wide": "Reactive wide",
    "solid_reactive_multiwide": "Reactive multiwide", "solid_reactive_cross": "Reactive cross",
    "solid_reactive_multicross": "Reactive multicross", "solid_reactive_nexus": "Reactive nexus",
    "solid_reactive_multinexus": "Reactive multinexus",
}


def effect_order(qmk):
    """QMK's effect enum order, e.g. ["solid_color", "alphas_mods", ...]."""
    anim = os.path.join(qmk, "quantum/rgb_matrix/animations")
    order = []
    with open(os.path.join(anim, "rgb_matrix_effects.inc")) as f:
        for header in re.findall(r'#include "(\w+\.h)"', f.read()):
            with open(os.path.join(anim, header)) as h:
                order += [n.lower() for n in re.findall(r"RGB_MATRIX_EFFECT\((\w+)\)", h.read())]
    return order


def merged(path, root):
    """info.json/keyboard.json from the board folder down to `path`, later ones winning (as QMK merges them)."""
    data = {}
    rel = os.path.relpath(path, root).split(os.sep)
    for i in range(1, len(rel) + 1):
        folder = os.path.join(root, *rel[:i])
        for name in ("info.json", "keyboard.json"):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                with open(p) as f:
                    try:
                        # QMK's own loader takes trailing commas, so a few board files have them
                        deep_update(data, json.loads(re.sub(r",(\s*[}\]])", r"\1", f.read())))
                    except ValueError as e:  # a broken upstream file: that board is left out, not guessed
                        print(f"warning: skipping {p}: {e}", file=sys.stderr)
                        return {}
    return data


def deep_update(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            deep_update(a[k], v)
        else:
            a[k] = v


def main(checkouts):
    boards, names = {}, {}
    for qmk in checkouts:
        found = scan(qmk, names)
        for pid, board in found.items():
            boards.setdefault(pid, board)
    json.dump({"names": names, "boards": dict(sorted(boards.items()))}, sys.stdout, indent=1)
    print()


def scan(qmk, names):
    """{pid: {"model", "effects"}} for one checkout; adds its effect names to `names`."""
    order = effect_order(qmk)
    for k in order:
        names.setdefault(k, NAMES.get(k, k.replace("_", " ").capitalize()))
    root = os.path.join(qmk, "keyboards/keychron")
    boards = {}
    for folder, _, files in os.walk(root):
        if "keyboard.json" not in files and "info.json" not in files:  # upstream uses the first, Keychron's fork the second
            continue
        data = merged(folder, root)
        pid = data.get("usb", {}).get("pid")
        anims = data.get("rgb_matrix", {}).get("animations", {})
        if not pid or not data.get("features", {}).get("rgb_matrix"):
            continue  # white-backlight (LED matrix) boards: not what Keybloom drives
        keys = ["solid_color"] + [k for k in order if k != "solid_color" and anims.get(k)]
        pid = "%04x" % int(pid, 16)
        model = os.path.relpath(folder, root).replace(os.sep, " ").upper()
        if pid in boards and boards[pid]["effects"] != keys:
            print(f"warning: {pid} differs between {boards[pid]['model']} and {model}; keeping the first",
                  file=sys.stderr)
            continue
        boards.setdefault(pid, {"model": model, "effects": keys})
    return boards


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: gen_effects.py <qmk checkout> [<more checkouts> ...]")
    main(sys.argv[1:])
