# SFX placement map

Machine-readable registry: `style/sfx.json`. The validator rejects any `[SFX:<name>]`
whose name is absent from that file.

| Interval | Dimension | Cue names |
|---|---|---|
| Every 3–5s | Visual cut texture | `vhs-burst`, `static-crackle` |
| Every 10–15s | Audio accent | `glitch-sting`, `metal-scrape` |
| Every 30–60s | Impact | `sub-drop`, `bass-thud` |
| Every 45–60s | Tension | `heartbeat` |

## Silence drops

`[SILENCE:<n>s]` mutes the bed completely for 0.5–2.0 seconds before a reveal. This is
true digital silence, not room tone. Narration chunk boundaries are forced at every
silence marker, so the gap always falls between two rendered TTS requests.
