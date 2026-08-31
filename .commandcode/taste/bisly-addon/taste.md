# bisly-addon
- The Bisly videoserver replies with plain-text status messages (e.g. "Camera stream is not ready yet, please retry") when a stream is not ready; guard with base64.b64decode(..., validate=True) and treat ValueError as a server error rather than decoding it as an offer. Confidence: 0.85
- Camera UUIDs (needed by the CDN image endpoint and videoserver) are only returned by a controller_list type-14 param-"1" query (as the Bisly app does), NOT from the handshake config; match the UUID to the numeric-id camera list by sip id. Confidence: 0.85
- Camera slugs for the RTSP server must be ASCII-normalized (transliterate/strip non-ASCII chars like 'ü'); mediamtx rejects path names with non-alphanumeric characters. Confidence: 0.70
- Pin aiortc==1.15.0 alongside av==17.0.1 in requirements.txt to match the verified-working Home Assistant camera stack; this combination installs/imports cleanly with all aiortc internals present. Confidence: 0.80
- In the add-on config.yaml, set `protected: false` and `apparmor: false` to allow raw sockets and UDP/ICE/TURN network paths. Confidence: 0.75
- The Protection Mode toggle in the HA add-on UI (Info tab) overrides the manifest/config `protected: false`; it must also be switched OFF in the UI, otherwise UDP sockets are blocked and ICE/STUN checks hang in IN_PROGRESS. Confidence: 0.70
