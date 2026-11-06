# Guide videos

The videos on the Guide page. Real app screens, a Kokoro voice, burned-in captions, Bluey in the corner.
`videos.json` holds every line, click and highlight, so a change to the app or a script is a rerun, not an edit.

## Rerun

```
npm run build
node tools/guide-videos/capture.mjs [video-id ...]     # screens, on a throwaway profile on paper
python -X utf8 tools/guide-videos/tts.py <model-dir> [video-id ...]   # voice; unchanged lines are kept
python -X utf8 tools/guide-videos/render.py --stills [video-id ...]   # one frame per scene: look at these
python -X utf8 tools/guide-videos/render.py [video-id ...]            # out/<id>.mp4 + .jpg
python -X utf8 tools/guide-videos/render.py --install   # copies them to src/assets/guide with the manifest
```

`node tools/guide-videos/verify-play.mjs` then plays every video in the built app and prints what the player saw. Set `PLAY_EXE`
to the packaged exe to check the packaged app, whose stricter content policy is the one that matters.

Needs Node, Python with Pillow and numpy, and ffmpeg on the PATH.

## Voice

Kokoro-82M (Apache-2.0), run offline by Krypt Studio's `krypt-studio-analyzer.exe`. `<model-dir>` holds `model.onnx` and
`af_heart.bin`, the files Studio's Model Manager installs; `shared/analyzer/models.ts` in KryptStudio lists their URLs and
hashes. `GUIDE_VOICE` and `GUIDE_SPEED` change the voice. A word said wrong goes in `SAY_AS` in `tts.py`; the caption keeps
the real spelling. ALL CAPS words such as MCP and API are spelled out by the voice on their own.

## videos.json

A scene is one spoken line (`say`) on one screen:

- `do`: steps before the shot. `{"id": testid}`, `{"qa": data-qa}`, `{"nav": "Sidebar label"}`, `{"t": "Button text"}` click;
  `fill`, `scrollTo`, `hook`, `esc`, `wait` and `js` do what they say.
- `hl`: `[{"find": ..., "at": "word"}]` draws a box and arrow around `find` when the voice reaches `word`. The camera zooms to fit.
  `find` also takes `{"has": "text", "up": 1}`, `{"sel": "css"}` and `{"size": true}`.
- `card` / `prompt` with `"blur": true`: for Polymarket's site, the phone app and other programs, which aren't ours to film.
- `seed`: places three paper trades through the agent server first, so the AI Agents screens aren't empty.

## Rules for the scripts

- One action per sentence, in plain words. Say exactly what's on the button.
- No profit claims. Paper first, always, and say what real money risks.
- Point new users at agents and paper, not at the crypto presets.
- `quickstart` ends on the VPN and legal-region line. Keep it.

`capture.mjs` uses its own folders and agent port (47699), so a real app running at the same time isn't touched. It stops
if the agent server's relay would reach any other profile. `hook` steps open Autopilot screens that otherwise need a real
AI key to reach; nothing on those screens is changed.
