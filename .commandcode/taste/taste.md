# Taste (Continuously Learned by [CommandCode][cmd])

[cmd]: https://commandcode.ai/

# git
- Split commits into logical, scoped conventional-commit messages (fix(...), feat(...)). Confidence: 0.60
- Skip commit verification when committing. Confidence: 0.70

# debugging
- Use systematic debugging: gather log evidence and root-cause before applying fixes, present findings and hypotheses, and propose a minimal test to confirm. Confidence: 0.65

# workflow
- Use multiple sub-agents when doing research. Confidence: 0.65

# communication
- Respond in German. Confidence: 0.85

# bisly-addon
- Camera slugs for the RTSP server must be ASCII-normalized (transliterate/strip non-ASCII chars like 'ü'); mediamtx rejects path names with non-alphanumeric characters. Confidence: 0.70
- Pin aiortc==1.15.0 alongside av==17.0.1 in requirements.txt to match the verified-working Home Assistant camera stack; this combination installs/imports cleanly with all aiortc internals present. Confidence: 0.80
- In the add-on config.yaml, set `protected: false` and `apparmor: false` to allow raw sockets and UDP/ICE/TURN network paths. Confidence: 0.75
- The Protection Mode toggle in the HA add-on UI (Info tab) overrides the manifest/config `protected: false`; it must also be switched OFF in the UI, otherwise UDP sockets are blocked and ICE/STUN checks hang in IN_PROGRESS. Confidence: 0.70
