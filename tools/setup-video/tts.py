"""ElevenLabs TTS with character timestamps. Key comes from $XI only (never written to disk).

  python tts.py <voice-id> <name>                    the whole script.json -> <name>.mp3 + <name>.align.json
  python tts.py <voice-id> <name> --scenes a,b,c     only those (consecutive) scenes, voiced with the
                                                     lines around them as context; splice.py puts them in
"""
import base64, json, os, sys, urllib.request, urllib.error

voice, name = sys.argv[1], sys.argv[2]
here = os.path.dirname(os.path.abspath(__file__))
scenes = json.load(open(os.path.join(here, "script.json"), encoding="utf-8"))
ids = [s["id"] for s in scenes]
pick = sys.argv[sys.argv.index("--scenes") + 1].split(",") if "--scenes" in sys.argv else ids
k0, k1 = ids.index(pick[0]), ids.index(pick[-1])
assert ids[k0:k1 + 1] == pick, "--scenes must be consecutive, in script order"
text = "\n\n".join(s["text"] for s in scenes[k0:k1 + 1])

body = {
    "text": text,
    "model_id": "eleven_multilingual_v2",
    "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25, "use_speaker_boost": True},
}
# The neighbouring lines keep the new part's pace and pitch continuous with the audio it is spliced into.
if k0 > 0: body["previous_text"] = scenes[k0 - 1]["text"]
if k1 < len(scenes) - 1: body["next_text"] = scenes[k1 + 1]["text"]
req = urllib.request.Request(
    f"https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps?output_format=mp3_44100_128",
    data=json.dumps(body).encode(), method="POST",
    headers={"xi-api-key": os.environ["XI"], "Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.load(r)
except urllib.error.HTTPError as e:
    print("HTTP", e.code, e.read()[:400].decode(errors="replace")); sys.exit(1)

open(os.path.join(here, f"{name}.mp3"), "wb").write(base64.b64decode(d["audio_base64"]))
json.dump({"text": text, "scenes": pick, "alignment": d.get("alignment") or d.get("normalized_alignment")},
          open(os.path.join(here, f"{name}.align.json"), "w", encoding="utf-8"))
al = d.get("alignment") or {}
print("ok", name, len(text), "chars, ends at", (al.get("character_end_times_seconds") or [0])[-1], "s")
