# Music credits

The jazz that plays in the Agent Hub's Casino (the hall) and in The Velvet
Tail (the club behind its front wall). Every track is by Kevin MacLeod and is
used under Creative Commons Attribution 4.0. The app credits each track on
screen when it starts. These files were transcoded from the originals to
48 kbps Opus, and nothing else was changed.

| File | Track | Plays in |
|---|---|---|
| `lobby-time.ogg` | "Lobby Time" | the hall |
| `bossa-antigua.ogg` | "Bossa Antigua" | the hall |
| `backbay-lounge.ogg` | "Backbay Lounge" | the hall |
| `smooth-lovin.ogg` | "Smooth Lovin" | the club |
| `bass-walker.ogg` | "Bass Walker" | the club |

Attribution, as the composer asks for it:

> "Lobby Time", "Bossa Antigua", "Backbay Lounge", "Smooth Lovin", "Bass Walker"
> Kevin MacLeod (incompetech.com)
> Licensed under Creative Commons: By Attribution 4.0
> http://creativecommons.org/licenses/by/4.0/

Source: https://incompetech.com/music/royalty-free/

The sound effects (the revolver, the lighter, the bottles, the cards, the
wheel, the slots, the doors, the applause and the voices) are not recordings.
`src/hub/engine/audio.ts` synthesises them in the browser.

# Guide video voice

The voice in the guide videos (`src/assets/guide/`) is Kokoro-82M, Apache License 2.0, weights by hexgrad,
ONNX export by onnx-community. It was generated offline with Krypt Studio's analyzer. See `tools/guide-videos/README.md`.
