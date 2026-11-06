# Setup video

The narrated setup video in onboarding (`src/assets/setup-guide.mp4`, shown by
`src/components/GuideVideo.tsx`). Jessica (ElevenLabs) reads `script.json`.
Bluey bounces along to her voice. The captions and highlights are timed from
ElevenLabs' per-character alignment, so editing a line re-times everything.

## Regenerate

```bash
# 1. Voice. The key is read from $XI only and never written to disk.
XI=sk_... python tts.py cgSgspJ2msm6clMCkdW9 jessica   # writes jessica.mp3 + jessica.align.json
#    Or re-voice only some lines and keep every other line's audio exactly as it was:
XI=sk_... python tts.py cgSgspJ2msm6clMCkdW9 part --scenes connect,golive,trade
python splice.py jessica part jessica2                 # then render jessica2

# 2. Screens. npm run build, then launch Electron ISOLATED (APPDATA, LOCALAPPDATA and
#    --user-data-dir all pointed at a temp dir, see .claude/skills/run-app) with
#    --remote-debugging-port=9344. capture-steps.json starts from the first-run
#    onboarding modal.
mkdir -p shots && node cap.mjs onb0.json   # [{"wait":9000},{"shot":"shots/onb1.png"}]
node cap.mjs capture-steps.json

# 3. Render (Python with numpy + Pillow, ffmpeg on PATH). --stills writes 2 frames a scene to check first.
python render.py jessica --stills
python render.py jessica                  # out/jessica.mp4, 1920x1080

# 4. App copy + poster
ffmpeg -y -i out/jessica.mp4 -vf "scale=1280:720:flags=lanczos" -c:v libx264 -profile:v high -preset slow -crf 25 \
  -pix_fmt yuv420p -movflags +faststart -c:a aac -b:a 96k -ac 1 ../../src/assets/setup-guide.mp4
ffmpeg -y -ss 2.2 -i out/jessica.mp4 -frames:v 1 -vf scale=640:360:flags=lanczos -q:v 4 ../../src/assets/setup-guide-poster.jpg
```

Then update `GUIDE_LENGTH` in `GuideVideo.tsx` if the duration changed.

## Things to know

- The highlight boxes in `render.py` `shots_for()` are screenshot pixels: CSS px × 1.2
  at the 1600×900 capture viewport. `rects.js` measures them from the live DOM; when
  the UI moves, re-measure. Keep each highlighted button above y≈880 of the cropped
  frame, or the caption bar covers it.
- Steps 5-7 are the MCP agent path: Claude Desktop → AI Agents → Go live with approvals
  switched off (fully automatic, inside the starter caps) → ask Claude to trade. The bundled 15-minute strategies are not in the video:
  they measured net negative on their own.
- Some shots are patched in the DOM, because the real state needs a funded wallet:
  - the Go live wallet row shows the app's own "Connected, not tested yet" text and a
    Test wallet button (saving a real key would make the app contact Polymarket);
  - the Go live confirm screen is reached by setting the panel's `stage` hook
    (`__hook` in rects.js), since the checklist can't pass without a tested wallet;
- Polymarket's Settings screen (step 3) and Claude Desktop (steps 5 and 7) are drawn
  cards, not footage.
- The last line, the VPN / legal-region warning, is required. Keep it in.
- The 1080p master and the mp3 are kept in `kryptware/krypt-agent-guide/`.
