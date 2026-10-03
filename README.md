# xeneon-edge-touch-linux

Give each of several Corsair Xeneon Edge touchscreens its own monitor on GNOME.

## The problem

GNOME's compositor, Mutter, maps a touchscreen to a monitor by USB vendor and product ID. Every Xeneon Edge reports `27c0:0859`, so two Edges share one mapping and both send their touches to the same screen. Mutter tracks this in [#1019](https://gitlab.gnome.org/GNOME/mutter/-/issues/1019) and [#4886](https://gitlab.gnome.org/GNOME/mutter/-/issues/4886).

## The fix

xeneon-edge-touch grabs each Edge's touch controller and passes its events unchanged to a virtual uinput device with its own product ID: `27c0:085a` for the first Edge, `27c0:085b` for the second, and so on. Mutter sees one touchscreen per Edge, and `setup` points each one at its monitor.

A single Edge works without this tool, because GNOME can map one Edge on its own.

## How it works

For every configured Edge, `run` opens the touch controller, grabs it exclusively and creates a virtual uinput device that copies the controller's axes and properties. Every event goes through unchanged, multitouch slots and timestamps included. Mutter stores one setting per device under `/org/gnome/desktop/peripherals/touchscreens/27c0:<id>/output` and does the coordinate mapping itself.

If a controller disappears, the proxy waits for it and grabs it again. If the process dies, the kernel releases the grab and removes the virtual device, so a crash never leaves a touchscreen stuck.

## Requirements

GNOME on Wayland, Python 3.10 or newer, `python3-evdev` and `python3-gi`. Your user needs read access to `/dev/input/event*` and write access to `/dev/uinput`. Tested on Ubuntu 24.04 with GNOME 46, Python 3.12 and python3-evdev 1.7.0.

```sh
sudo apt install python3-evdev python3-gi
sudo usermod -aG input "$USER"
echo 'KERNEL=="uinput", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' | sudo tee /etc/udev/rules.d/80-uinput.rules
```

Reboot so the group and the udev rule take effect. `ls -l /dev/uinput` then shows group `input` with `rw`.

## Install

```sh
git clone https://github.com/lunetics/xeneon-edge-touch-linux.git
cd xeneon-edge-touch-linux
mkdir -p ~/.local/bin ~/.config/systemd/user
ln -s "$PWD/xeneon-edge-touch.py" ~/.local/bin/xeneon-edge-touch
cp xeneon-edge-touch.service ~/.config/systemd/user/
```

## Usage

Connect every Edge (picture and USB), then pair them:

```sh
xeneon-edge-touch setup --dry-run   # print what would be written
xeneon-edge-touch setup             # tap each Edge when asked
systemctl --user daemon-reload
systemctl --user enable --now xeneon-edge-touch
```

`setup` finds the touch controllers and the Edge monitors Mutter knows about. It asks you to tap each Edge in turn, ordered left to right and then top to bottom. While it waits, it holds the controllers, so your taps don't click anything. Then it writes the config file and one dconf key per Edge, for example `/org/gnome/desktop/peripherals/touchscreens/27c0:085a/output`. With exactly one controller and one Edge it skips the tapping. Stop the service before you run `setup` again, otherwise the controllers are busy.

`xeneon-edge-touch run --verbose` runs the proxy in the foreground and logs every touch down and up. Under systemd, the log is in `journalctl --user -u xeneon-edge-touch`.

## Config

`~/.config/xeneon-edge-touch.conf` (or `$XDG_CONFIG_HOME/xeneon-edge-touch.conf`) holds one touch controller serial per line. Line n gets product ID `0x0859 + n`. `#` starts a comment, and an entry starting with `/` is used as a device path.

```
# HDMI-1 <monitor serial>
<SERIAL>
# DP-1 <monitor serial>
<SERIAL>
```

The serial is the `<SERIAL>` part of `/dev/input/by-id/usb-wch.cn_TouchScreen_<SERIAL>-event-if00`. Prefer serials over `/dev/input/eventN` paths, because event numbers change between boots.

## Uninstall

```sh
systemctl --user disable --now xeneon-edge-touch
rm ~/.config/systemd/user/xeneon-edge-touch.service ~/.local/bin/xeneon-edge-touch ~/.config/xeneon-edge-touch.conf
dconf reset /org/gnome/desktop/peripherals/touchscreens/27c0:085a/output   # repeat for 085b, 085c, ...
```

## Troubleshooting

Ask Mutter where a touchscreen is mapped:

```sh
gdbus call --session --dest org.gnome.Mutter.InputMapping \
  --object-path /org/gnome/Mutter/InputMapping \
  --method org.gnome.Mutter.InputMapping.GetDeviceMapping /dev/input/eventN
```

A rectangle such as `((0, 568, 2560, 720),)` is the Edge's area on the desktop. "Device is not mapped to any output" means the dconf key is missing or doesn't match a monitor, and touches then cover the whole desktop.

Check the key with `dconf read`, not `gsettings get`. In some environments `gsettings` uses a different settings backend than gnome-shell, so it can show a value the shell never sees. `setup` writes through `dconf` for that reason.

Mutter reports the Edge's monitor serial with a trailing space. `setup` copies it exactly. If you write a key by hand, keep the space.

## Limitations

- Mutter only. Other compositors store touch mappings differently, and the tool is untested there. Reports are welcome.
- Pairing two real Edges is tested with virtual devices only. One real Edge is tested end to end.
- An Edge without a picture disconnects its touch controller, for example when the screen locks and the monitors switch off. The proxy waits and grabs the controller again when it returns. It checks once per second, so a tap in the first second after the screen wakes can bypass the proxy.
- `setup` only sees Edges that are switched on and part of the desktop layout.

## Contributing

Issues and pull requests are welcome, especially reports from setups with two or more Edges. Run the tests before you open a pull request:

```sh
sudo apt install python3-pytest
pytest -q -p no:cacheprovider
```

The tests create virtual input devices through uinput. They never open a real Edge, call dconf or write to `~/.config`.

## Prior art

[MacXeneonEdgeTouchDriver](https://github.com/ajvwhite/MacXeneonEdgeTouchDriver) and [xeneon-edge-touch-macos](https://github.com/MorganZ/xeneon-edge-touch-macos) handle Edge touch input on macOS.

[evsieve](https://github.com/KarsMulder/evsieve) re-emits input events through uinput as well, but it can't set the vendor and product ID, so every copy would share one Mutter key again. A udev `LIBINPUT_CALIBRATION_MATRIX` doesn't help either, because Mutter sets its own matrix when it maps a device.

## License

[MIT](LICENSE)
