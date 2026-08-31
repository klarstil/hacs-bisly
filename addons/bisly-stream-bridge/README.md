# Bisly Stream Bridge (HAOS Add-on)

Bridges Bisly WebRTC cameras to **RTSP** so they can be used by the Home Assistant **Generic Camera** integration, the **HomeKit Bridge** (Apple Home), or **Scrypted**.

## Why

Bisly cameras only stream via a proprietary WebRTC protocol (NATS WebSocket signaling to `wss://cloud.bisly.ee:8223`). There is no RTSP/ONVIF/HLS endpoint, which is why the camera does not work in Apple Home via the HomeKit Bridge.

This add-on:

1. Speaks the Bisly protocol (handshake, camera discovery, WebRTC offer/answer) — the same code the `hacs_bisly` integration uses.
2. Terminates the WebRTC stream with aiortc.
3. Pipes decoded frames into ffmpeg, which pushes H.264 to a local **mediamtx** RTSP server.

```
Bisly Cloud ──(NATS/WebRTC)──> Bridge add-on ──(RTSP)──> Generic Camera / HomeKit / Scrypted
```

## Installation

1. Copy the `addons/bisly-stream-bridge` folder to the HAOS `addons` share (e.g. `\\homeassistant\addons\bisly-stream-bridge` via SMB).
2. In Home Assistant: **Settings → Add-ons → Add-on Store → ⋮ → Repositories** → add the local repository URL (e.g. `http://homeassistant.local:8123` style local repo, or use the Samba share path).
3. Install the **Bisly Stream Bridge** add-on from the local store.
4. Under **Configuration**, enter your Bisly account username and password.
5. Start the add-on and check the log for `Authenticated` and the list of registered cameras.

## Usage

After startup the add-on registers every discovered camera:

```
Registered camera 'Garden Gate' → rtsp://<ha-host>:8554/garden_gate
```

A WebRTC connection is only opened when an RTSP client connects (on-demand via mediamtx `runOnDemand`), so there is no permanent Bisly cloud connection.

### Generic Camera

Add the **Generic Camera** integration with:

- Stream Source URL: `rtsp://<ha-host>:8554/garden_gate`

(Leave the still image URL empty, or use the HA snapshot URL of the `hacs_bisly` camera entity if you also run the integration.)

### Apple Home via HomeKit Bridge

1. Make sure the camera exists as a Home Assistant camera entity (Generic Camera, see above).
2. In the HomeKit Bridge integration, select the camera.
3. In Apple Home, add the bridge — the camera appears and streams live.

### Scrypted (alternative)

Install Scrypted, add the **RTSP** plugin, and use `rtsp://<ha-host>:8554/garden_gate` as the camera source. Then pair the Scrypted HomeKit bridge.

## Configuration

| Option               | Default    | Description                                      |
| -------------------- | ---------- | ------------------------------------------------ |
| `bisly_username`     | —          | Bisly account username (required)                |
| `bisly_password`     | —          | Bisly account password (required)                |
| `rtsp_port`          | `8554`     | RTSP port exposed on the HAOS host               |
| `video_width`        | `1280`     | Output width (streams are transcoded via ffmpeg) |
| `video_fps`          | `15`       | Output frame rate                                |
| `ffmpeg_cpu_preset`  | `veryfast` | x264 preset (lower = less CPU, more latency)     |

## How it works

- **mediamtx** runs the RTSP server and calls `/opt/start_camera.sh <path>` when a reader requests a stream (`runOnDemand`).
- The start script calls the bridge's HTTP control API (`http://127.0.0.1:4599/api/start/<slug>`), which opens the Bisly WebRTC session for that camera.
- Frames are decoded by aiortc, piped as raw BGR into an ffmpeg process that publishes H.264 to `rtsp://localhost:<port>/<slug>`.
- When the last reader disconnects, mediamtx calls `/opt/stop_camera.sh <path>`, which closes the WebRTC session and the ffmpeg process.

## Limitations

- The stream still flows through the Bisly cloud (TURN servers); the add-on only makes it locally consumable. Expect a few seconds of latency.
- Transcoding costs CPU. On Raspberry Pi class hardware, lower `video_width`/`video_fps` or use `ultrafast`.
- The aiortc ICE-candidate extraction relies on aiortc internals; `aiortc==1.9.0` is pinned to match the integration.
