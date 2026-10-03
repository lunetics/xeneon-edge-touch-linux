#!/usr/bin/python3
"""Touch proxy for several identical Corsair Xeneon Edge screens on GNOME/Wayland.

All Edges report USB id 27c0:0859, so Mutter cannot tell their touch controllers
apart. `run` grabs each controller and re-emits its events on a uinput device with
its own product id (0x0859 + n); `setup` pairs controllers with monitors (tap
assistant) and writes the config plus the per-device dconf keys.

    xeneon-edge-touch setup [--config P] [--dry-run]
    xeneon-edge-touch run [--config P] [--verbose]
"""

import argparse
import contextlib
import errno
import glob
import logging
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# python-evdev has neither stubs nor py.typed; the ignore covers this line only.
from evdev import InputDevice, UInput, ecodes  # type: ignore[import-untyped]

log = logging.getLogger("xeneon-edge-touch")
BY_ID = "/dev/input/by-id/usb-wch.cn_TouchScreen_{}-event-if00"
DCONF_KEY = "/org/gnome/desktop/peripherals/touchscreens/27c0:{:04x}/output"
Edge = tuple[int, int, tuple[str, str, str, str]]  # (x, y, (connector, vendor, product, serial))


def default_config() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "xeneon-edge-touch.conf"


def read_config(path: Path) -> list[str]:
    """Entries in file order: a touch serial, or (leading '/') a source path."""
    lines = (ln.split("#")[0].strip() for ln in path.read_text().splitlines())
    return [ln for ln in lines if ln]


def source_of(entry: str) -> str:
    return entry if entry.startswith("/") else BY_ID.format(entry)


def entry_of(source: str) -> str:
    m = re.search(r"TouchScreen_(.+)-event-if00$", source)
    return m[1] if m else source


# ---- run -------------------------------------------------------------------------


def serve(source: str, product: int, name: str, verbose: bool) -> None:
    dev = InputDevice(source)
    try:
        dev.grab()
        with UInput.from_device(
            dev,
            name=name,
            vendor=0x27C0,
            product=product,
            version=dev.info.version,
            bustype=ecodes.BUS_USB,
            input_props=dev.input_props(),
        ) as ui:
            log.info("%s: grabbed %s -> %s vid:pid 27c0:%04x", name, source, ui.device.path, product)
            for ev in dev.read_loop():
                ui.write_event(ev)
                if verbose and ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH:
                    log.debug("%s: BTN_TOUCH %d", name, ev.value)
    finally:
        with contextlib.suppress(OSError):  # source may already be gone
            dev.ungrab()
        dev.close()


def proxy_loop(source: str, product: int, name: str, verbose: bool) -> None:
    try:
        while True:
            if not os.path.exists(source):
                log.info("%s: waiting for %s", name, source)
                while not os.path.exists(source):
                    time.sleep(1)
            try:
                serve(source, product, name, verbose)
            except OSError as exc:
                if exc.errno not in (errno.ENOENT, errno.ENODEV):
                    raise
                log.info("%s: source gone: %s", name, exc)
                time.sleep(1)  # no busy loop if the source fails again at once
    except Exception:
        log.exception("%s: fatal, exiting for restart", name)
        os._exit(1)  # one dead proxy = exit != 0, systemd restarts all; kernel frees grabs


def run(config: Path, verbose: bool) -> int:
    entries = read_config(config) if config.is_file() else []
    if not entries:
        print(f"no config entries in {config}; run `xeneon-edge-touch setup` first", file=sys.stderr)
        return 2
    # SystemExit from the handler runs the finally below; the kernel releases grabs and
    # uinput devices on process end anyway. ponytail: daemon threads, no orderly thread join.
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: sys.exit(0))
    try:
        for n, entry in enumerate(entries, 1):
            args = (source_of(entry), 0x0859 + n, f"Xeneon Edge {n} touch", verbose)
            threading.Thread(target=proxy_loop, args=args, daemon=True).start()
        while True:
            signal.pause()
    finally:
        log.info("stopped")


# ---- setup -----------------------------------------------------------------------


def find_edges(logical_monitors: Sequence[Any]) -> list[Edge]:
    """Edges (vendor CRX, product XENEON EDGE) from Mutter's logical monitors, by (x, y)."""
    found = [(lm[0], lm[1], tuple(spec)) for lm in logical_monitors for spec in lm[5]
             if spec[1] == "CRX" and spec[2] == "XENEON EDGE"]
    return sorted(found)


def dconf_commands(edges: Sequence[Edge]) -> list[list[str]]:
    """One `dconf write` argv per Edge (n-th Edge -> product 0x0859+n). Pure, runs nothing."""
    # str(list) is a valid GVariant 'as' literal: ['CRX', 'XENEON EDGE', '<serial>']
    return [["dconf", "write", DCONF_KEY.format(0x0859 + n), str(list(spec[1:]))]
            for n, (_, _, spec) in enumerate(edges, 1)]


def wait_tap(devs: dict[str, Any], timeout: float) -> str:
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        ready = select.select([d.fd for d in devs.values()], [], [], left)[0]
        for src, d in devs.items():
            if d.fd in ready:
                for ev in d.read():
                    if ev.type == ecodes.EV_KEY and ev.code == ecodes.BTN_TOUCH and ev.value == 1:
                        return src
    sys.exit("timed out waiting for a tap")


def assign(edges: Sequence[Edge], sources: Sequence[str], say: Callable[[str], None] = print,
           timeout: float = 60.0) -> list[str]:
    """Grab all sources (taps click nothing), return the source tapped for each Edge, in order."""
    devs: dict[str, Any] = {}
    try:
        for src in sources:
            devs[src] = InputDevice(src)
            try:
                devs[src].grab()
            except OSError as exc:
                if exc.errno != errno.EBUSY:
                    raise
                sys.exit(f"{src} is busy; stop the service: systemctl --user stop xeneon-edge-touch")
        order: list[str] = []
        for k, (x, y, spec) in enumerate(edges, 1):
            say(f"Tap Edge {k} of {len(edges)} at x={x}, y={y} ({spec[0]})")
            order.append(wait_tap({s: d for s, d in devs.items() if s not in order}, timeout))
        return order
    finally:
        for d in devs.values():
            d.close()


def mutter_logical_monitors() -> list[Any]:
    # PyGObject has no stubs; imported lazily because only setup needs it.
    from gi.repository import Gio  # type: ignore[import-untyped]

    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    state = bus.call_sync("org.gnome.Mutter.DisplayConfig", "/org/gnome/Mutter/DisplayConfig",
                          "org.gnome.Mutter.DisplayConfig", "GetCurrentState", None, None,
                          Gio.DBusCallFlags.NONE, -1, None).unpack()
    return list(state[2])


def setup(config: Path, dry_run: bool) -> int:
    sources = sorted(glob.glob(BY_ID.format("*")))
    edges = find_edges(mutter_logical_monitors())
    if not sources or not edges:
        print(f"found {len(sources)} touch controller(s) and {len(edges)} Edge monitor(s)", file=sys.stderr)
        return 1
    if len(sources) < len(edges):
        print("fewer touch controllers than Edge monitors", file=sys.stderr)
        return 1
    order = sources if len(sources) == len(edges) == 1 else assign(edges, sources)
    text = "".join(f"# {spec[0]} {spec[3]}\n{entry_of(src)}\n" for src, (_, _, spec) in zip(order, edges))
    cmds = dconf_commands(edges)
    print(f"{config}:\n{text}" + "\n".join(f'{" ".join(c[:3])} "{c[3]}"' for c in cmds))
    if not dry_run:
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text)
        for c in cmds:
            subprocess.run(c, check=True)
        print("done; now run: systemctl --user enable --now xeneon-edge-touch")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="xeneon-edge-touch")
    sub = p.add_subparsers(dest="cmd", required=True)
    r, s = sub.add_parser("run"), sub.add_parser("setup")
    for sp in (r, s):
        sp.add_argument("--config", type=Path, default=default_config())
    r.add_argument("--verbose", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(a, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return run(a.config, a.verbose) if a.cmd == "run" else setup(a.config, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
