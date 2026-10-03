"""Tests for xeneon-edge-touch.py against uinput fakes (run: `pytest -q`).

Never touches the real device, dconf, ~/.config or Mutter. Proxy nodes are found by
sysfs name only and grabbed exclusively BEFORE the first fake event is written,
otherwise fake taps would click on the real desktop."""

import importlib.util
import os
import select
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from evdev import AbsInfo, InputDevice, UInput
from evdev import ecodes as e

SCRIPT = Path(__file__).resolve().parent / "xeneon-edge-touch.py"
PROXY1, PROXY2 = "Xeneon Edge 1 touch", "Xeneon Edge 2 touch"

CAPS = {
    e.EV_KEY: [e.BTN_TOUCH],
    e.EV_ABS: [
        (e.ABS_X, AbsInfo(0, 0, 16383, 0, 0, 76)),
        (e.ABS_Y, AbsInfo(0, 0, 9599, 0, 0, 107)),
        (e.ABS_MT_SLOT, AbsInfo(0, 0, 14, 0, 0, 0)),
        (e.ABS_MT_POSITION_X, AbsInfo(0, 0, 16383, 0, 0, 76)),
        (e.ABS_MT_POSITION_Y, AbsInfo(0, 0, 9599, 0, 0, 107)),
        (e.ABS_MT_TRACKING_ID, AbsInfo(0, 0, 65535, 0, 0, 0)),
    ],
    e.EV_MSC: [e.MSC_TIMESTAMP],
}

# MT tap with non-trivial values (slot 1, id 5), the input core drops duplicates otherwise.
TAP = [
    (e.EV_ABS, e.ABS_MT_SLOT, 1),
    (e.EV_ABS, e.ABS_MT_TRACKING_ID, 5),
    (e.EV_ABS, e.ABS_MT_POSITION_X, 8000),
    (e.EV_ABS, e.ABS_MT_POSITION_Y, 4800),
    (e.EV_KEY, e.BTN_TOUCH, 1),
    (e.EV_MSC, e.MSC_TIMESTAMP, 1000),
    (e.EV_SYN, e.SYN_REPORT, 0),
    (e.EV_ABS, e.ABS_MT_TRACKING_ID, -1),
    (e.EV_KEY, e.BTN_TOUCH, 0),
    (e.EV_MSC, e.MSC_TIMESTAMP, 2000),
    (e.EV_SYN, e.SYN_REPORT, 0),
]


def find_node(name: str) -> str | None:
    """/dev/input/eventN by sysfs name, opens nothing."""
    for entry in Path("/sys/class/input").glob("event*"):
        try:
            if (entry / "device" / "name").read_text().strip() == name:
                return f"/dev/input/{entry.name}"
        except OSError:
            pass  # device vanished between glob and read
    return None


def wait_for(cond: Callable[[], Any], timeout: float = 6.0) -> Any:
    deadline = time.monotonic() + timeout
    while not (result := cond()):
        assert time.monotonic() < deadline, "timeout"
        time.sleep(0.05)
    return result


def wait_node(name: str) -> str:
    """Wait for the node; udev sets permissions late, so poll for readability."""
    node: str = wait_for(lambda: find_node(name))
    wait_for(lambda: os.access(node, os.R_OK | os.W_OK))
    return node


def read_events(dev: InputDevice, syns: int) -> list[tuple[int, int, int]]:
    out: list[tuple[int, int, int]] = []
    seen = 0
    while seen < syns and select.select([dev.fd], [], [], 3)[0]:
        for ev in dev.read():
            out.append((ev.type, ev.code, ev.value))
            seen += ev.type == e.EV_SYN
    return out


@pytest.fixture(autouse=True)
def no_real_proxy() -> None:
    if find_node(PROXY1) or find_node(PROXY2):
        pytest.skip("real proxy running")


@contextmanager
def fake_source(name: str) -> Iterator[Any]:
    if not os.access("/dev/uinput", os.W_OK):
        pytest.skip("/dev/uinput not writable (group input required)")
    ui = UInput(CAPS, name=name, vendor=0x27C0, product=0x0859, input_props=[e.INPUT_PROP_DIRECT])
    try:
        wait_node(name)
        yield ui
    finally:
        ui.close()  # idempotent, also after a test-side close


@pytest.fixture
def fake() -> Iterator[Any]:
    with fake_source("Fake Edge source") as ui:
        yield ui


@contextmanager
def proxies(sources: list[str], tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], list[InputDevice]]]:
    """Run `run` on a config with fake-path entries; yield process + exclusively grabbed proxy nodes."""
    conf = tmp_path / "xeneon-edge-touch.conf"
    conf.write_text("# fake sources\n" + "\n".join(sources) + "\n")
    with (tmp_path / "log").open("wb") as logfile:
        proc = subprocess.Popen([SCRIPT, "run", "--config", conf, "--verbose"], stderr=logfile)
        devs: list[InputDevice] = []
        try:
            for n in range(1, len(sources) + 1):
                dev = InputDevice(wait_node(f"Xeneon Edge {n} touch"))
                devs.append(dev)
                dev.grab()
            yield proc, devs
        finally:
            for dev in devs:
                dev.close()
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


def logtext(tmp_path: Path) -> str:
    return (tmp_path / "log").read_text()


def load() -> Any:
    s = importlib.util.spec_from_file_location("xet", SCRIPT)
    assert s and s.loader
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


def test_identity(fake: Any, tmp_path: Path) -> None:
    src = InputDevice(fake.device.path)
    with proxies([fake.device.path], tmp_path) as (_, (dev,)):
        assert (dev.info.vendor, dev.info.product, dev.name) == (0x27C0, 0x085A, PROXY1)
        assert e.INPUT_PROP_DIRECT in dev.input_props()
        assert dev.input_props() == src.input_props()
        assert dev.capabilities(absinfo=True)[e.EV_ABS] == src.capabilities(absinfo=True)[e.EV_ABS]
    src.close()


def test_pass_through_and_grab(fake: Any, tmp_path: Path) -> None:
    early = InputDevice(fake.device.path)  # opened before the proxy
    with proxies([fake.device.path], tmp_path) as (_, (dev,)):
        for t, c, v in TAP:
            fake.write(t, c, v)
        assert read_events(dev, 2) == TAP
        assert not select.select([early.fd], [], [], 1)[0]  # grab: earlier reader stays silent
    early.close()
    lines = [ln for ln in logtext(tmp_path).splitlines() if "DEBUG" in ln and "BTN_TOUCH" in ln]
    assert [ln.split()[-1] for ln in lines] == ["1", "0"]  # one per BTN_TOUCH change


def test_unplug(fake: Any, tmp_path: Path) -> None:
    with proxies([fake.device.path], tmp_path) as (proc, (dev,)):
        dev.close()
        fake.close()
        wait_for(lambda: find_node(PROXY1) is None, 3)
        wait_for(lambda: "source gone" in logtext(tmp_path), 3)
        assert proc.poll() is None


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_signal_exits_cleanly(sig: signal.Signals, fake: Any, tmp_path: Path) -> None:
    with proxies([fake.device.path], tmp_path) as (proc, (dev,)):
        dev.close()
        proc.send_signal(sig)
        assert proc.wait(timeout=3) == 0
        wait_for(lambda: find_node(PROXY1) is None, 3)


def test_two_fakes_two_proxies(tmp_path: Path) -> None:
    with (fake_source("Fake Edge source A") as a, fake_source("Fake Edge source B") as b,
          proxies([a.device.path, b.device.path], tmp_path) as (_, (d1, d2))):
        assert [(d.name, d.info.vendor, d.info.product) for d in (d1, d2)] == [
            (PROXY1, 0x27C0, 0x085A), (PROXY2, 0x27C0, 0x085B)]
        for t, c, v in TAP:  # tap on B arrives on proxy 2 only
            b.write(t, c, v)
        assert read_events(d2, 2) == TAP
        assert not select.select([d1.fd], [], [], 0.5)[0]


def test_assign_by_tap_order(tmp_path: Path) -> None:
    m = load()
    edges = [(0, 0, ("HDMI-1", "CRX", "XENEON EDGE", "FAKE1")),
             (2560, 0, ("DP-1", "CRX", "XENEON EDGE", "FAKE2"))]
    prompts: list[str] = []
    with fake_source("Fake Edge source A") as a, fake_source("Fake Edge source B") as b:
        pa, pb = a.device.path, b.device.path
        result: list[str] = []
        t = threading.Thread(target=lambda: result.extend(
            m.assign(edges, [pa, pb], prompts.append, 5)), daemon=True)
        t.start()
        wait_for(lambda: len(prompts) >= 1)  # prompt comes only after the function grabbed both
        for path in (pa, pb):  # prove the grab before writing anything
            probe = InputDevice(path)
            with pytest.raises(OSError, match="busy"):
                probe.grab()
            probe.close()
        for t_, c, v in TAP:  # tap on B first ...
            b.write(t_, c, v)
        wait_for(lambda: len(prompts) >= 2)
        for t_, c, v in TAP:  # ... then on A
            a.write(t_, c, v)
        t.join(5)
    assert result == [pb, pa]
    assert prompts[0] == "Tap Edge 1 of 2 at x=0, y=0 (HDMI-1)"


def test_find_edges_and_dconf_commands() -> None:
    m = load()
    edge = ("CRX", "XENEON EDGE")
    lms = [(2560, 0, 1.0, 0, False, [("DP-1", *edge, "FAKE2 ")], {}),
           (0, 0, 1.0, 0, True, [("eDP-1", "AUO", "panel", "0x1")], {}),
           (0, 1440, 1.0, 0, False, [("HDMI-1", *edge, "FAKE1 ")], {})]
    edges = m.find_edges(lms)
    assert [x[2][0] for x in edges] == ["HDMI-1", "DP-1"]  # sorted by (x, y), panel dropped
    assert m.dconf_commands(edges) == [
        ["dconf", "write", "/org/gnome/desktop/peripherals/touchscreens/27c0:085a/output",
         "['CRX', 'XENEON EDGE', 'FAKE1 ']"],
        ["dconf", "write", "/org/gnome/desktop/peripherals/touchscreens/27c0:085b/output",
         "['CRX', 'XENEON EDGE', 'FAKE2 ']"],
    ]


def test_config_parsing(tmp_path: Path) -> None:
    m = load()
    conf = tmp_path / "c.conf"
    conf.write_text("# HDMI-1 FAKE\nFAKESERIAL1\n\n  /dev/input/event99  \nFAKESERIAL2 # inline\n")
    entries = m.read_config(conf)
    assert entries == ["FAKESERIAL1", "/dev/input/event99", "FAKESERIAL2"]
    assert m.source_of(entries[0]) == "/dev/input/by-id/usb-wch.cn_TouchScreen_FAKESERIAL1-event-if00"
    assert m.source_of(entries[1]) == "/dev/input/event99"
    assert m.entry_of(m.source_of(entries[0])) == "FAKESERIAL1"


def test_run_without_config_exits_2(tmp_path: Path) -> None:
    r = subprocess.run([SCRIPT, "run", "--config", tmp_path / "missing.conf"],
                       capture_output=True, text=True, timeout=10, check=False)
    assert r.returncode == 2 and "setup" in r.stderr
