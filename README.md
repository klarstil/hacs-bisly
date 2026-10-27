# Bisly Home Assistant Websockets Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)

[![hacs][hacsbadge]][hacs]
![Project Maintenance][maintenance-shield]

A Home Assistant custom integration that connects to the **Bisly smart home cloud** (`wss://cloud.bisly.ee:8223`) via its NATS WebSocket protocol and exposes your Bisly home for monitoring and control. State changes are pushed in real time; the integration additionally syncs periodically so changes made from the Bisly app are picked up.

> [!NOTE]
> This is a community project and is not affiliated with or endorsed by Bisly.

## Features

- **Lighting** – on/off, dimming, and RGB control for Bisly lights
- **Climate** – room thermostats with target temperature and heat mode, plus separate floor-heating toggles
- **Sensors** – room temperature from climate zones
- **Doors** – lock/unlock for door/video-door intercoms
- **Cameras** – snapshots and live streams via WebRTC
- **Intercom** – doorbell and call detection, hang up, open door, with visitor audio bridged into the camera stream
- **Push-based updates** – real-time state via NATS broadcasts, no polling of devices
- **Reauthentication & reconfiguration** – update credentials without removing the integration

## Supported Platforms

| Platform        | Description                                                        |
| --------------- | ------------------------------------------------------------------ |
| `climate`       | Room thermostats (target temperature, heat on/off)                 |
| `switch`        | Floor heating toggles                                              |
| `sensor`        | Room temperature                                                   |
| `light`         | Lights with on/off, dimming, and RGB modes                         |
| `lock`          | Door locks                                                         |
| `camera`        | Camera snapshots and live streams (WebRTC)                         |
| `binary_sensor` | API connectivity (diagnostic), intercom ringing, doorbell          |
| `button`        | Hang up intercom call, open door during a call                     |

## Installation

### Via HACS

1. Make sure [HACS](https://hacs.xyz/) is installed.
2. In HACS, go to **Integrations** → **⋮** → **Custom repositories**.
3. Add `https://github.com/klarstil/hacs-bisly` with category **Integration**.
4. Find **Bisly Home Assistant Websockets Integration** and click **Download**.
5. **Restart Home Assistant**.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=klarstil&repository=hacs-bisly&category=integration)

### Manual Installation

1. Copy the `custom_components/hacs_bisly/` folder into your Home Assistant `custom_components/` directory.
2. Restart Home Assistant.

## Setup

1. Go to **Settings** → **Devices & Services** → **+ Add Integration**.
2. Search for **Bisly Home Assistant Websockets Integration**.
3. Enter your **Bisly account username and password**. The credentials are validated against the Bisly cloud during setup.

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=hacs_bisly)

## Options

After setup, click **Configure** on the integration to adjust:

| Option            | Default | Description                              |
| ----------------- | ------- | ---------------------------------------- |
| Update interval   | 6 h     | How often to re-sync state from the cloud |
| Enable debugging  | Off     | Extra debug logging                      |
| Custom icon       | —       | Icon shown for the integration           |

## Camera Live Streaming

Bisly cameras only stream through a proprietary WebRTC protocol. The integration supports two paths for live view:

- **In-process (`aiortc`)**: If `aiortc` can be installed in your Home Assistant environment, the camera streams natively via WebRTC. Nothing to configure.
- **Bisly Stream Bridge add-on**: On Home Assistant versions that pin a PyAV version `aiortc` cannot accept, install the **Bisly Stream Bridge** HAOS add-on (in [`addons/bisly-stream-bridge/`](addons/bisly-stream-bridge/)). It terminates the Bisly WebRTC leg and republishes it as RTSP. The integration discovers the add-on automatically through Supervisor and streams via Home Assistant's go2rtc/HLS pipeline — add the `go2rtc` integration for low-latency WebRTC.

Without either, cameras still provide snapshots but no live stream.

## Intercom

When someone rings your door:

- `binary_sensor` entities report **ringing** and **doorbell** presses.
- A **hang up** button ends the call; **open door** buttons appear for doors linked to the active call.
- During an active call, visitor audio is bridged into the camera stream via WebRTC.

## Troubleshooting

### Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.hacs_bisly: debug
```

### Connection Status

The **API Connectivity** binary sensor shows whether the connection to the Bisly cloud is healthy. If credentials expire, Home Assistant will prompt you to reauthenticate — you can also use **⋮ → Reconfigure** at any time.

## Contributing

Contributions are welcome. The repository ships a ready-to-use devcontainer: open the folder in VS Code with the Dev Containers extension and run `script/develop` to start a local Home Assistant instance. See [`AGENTS.md`](AGENTS.md) and the [`docs/`](docs/) directory for architecture and development notes.

## License

MIT — see [LICENSE](LICENSE).

---

**Made with ❤️ by [@klarstil][user_profile]**

[commits-shield]: https://img.shields.io/github/commit-activity/y/klarstil/hacs-bisly.svg?style=for-the-badge
[commits]: https://github.com/klarstil/hacs-bisly/commits/main
[hacs]: https://hacs.xyz
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/klarstil/hacs-bisly.svg?style=for-the-badge
[maintenance-shield]: https://img.shields.io/badge/maintainer-%40klarstil-blue.svg?style=for-the-badge
[releases-shield]: https://img.shields.io/github/release/klarstil/hacs-bisly.svg?style=for-the-badge
[releases]: https://github.com/klarstil/hacs-bisly/releases
[user_profile]: https://github.com/klarstil
