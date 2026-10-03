# Changelog

All notable changes to this project are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-10-03

### Added

- `run`: one uinput proxy per configured Edge touch controller, each with its own product ID (`0x0859 + n`), so Mutter maps every Edge separately.
- `setup`: finds the touch controllers and Edge monitors, pairs them by asking for a tap on each Edge and writes the config file and the dconf keys. `--dry-run` prints them instead.
- Re-grab after a controller disconnects, for example when the Edge loses its picture.
- systemd user unit `xeneon-edge-touch.service`.

[0.1.0]: https://github.com/lunetics/xeneon-edge-touch-linux/releases/tag/v0.1.0
