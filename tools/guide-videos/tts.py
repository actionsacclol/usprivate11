"""Voice for the guide videos: Kokoro, run by Krypt Studio's own analyzer. Offline and free.

  python tts.py <model-dir> [video-id ...]

<model-dir> holds model.onnx and af_heart.bin, the files Krypt Studio's Model Manager installs for
Kokoro (shared/analyzer/models.ts in KryptStudio lists their URLs and hashes). A line that hasn't
changed keeps its audio. Writes out/voice/<video>/<scene>.wav and .json (duration and word times).
"""
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ANALYZER = os.environ.get("KRYPT_ANALYZER", os.path.expandvars(
    r"%LOCALAPPDATA%\Programs\Krypt Studio\resources\resources\bin\krypt-studio-analyzer.exe"))
VOICE = os.environ.get("GUIDE_VOICE", "af_heart")
SPEED = float(os.environ.get("GUIDE_SPEED", "1.0"))

# Spellings the voice gets wrong. Captions keep the real word. ALL CAPS words (MCP, API, AI) are spelled out on their own.
SAY_AS = {"Krypt": "Crypt", "krypt": "crypt", "PolyBot": "Poly Bot", "polybot": "poly bot", "Polymarket": "Poly Market", "ChatGPT": "Chat GPT",
          "Backtest": "Back test", "backtest": "back test", "Autopilot": "Auto pilot", "In-app": "In app", "krypt-polybot-us": "crypt poly bot US"}
SAY_RE = re.compile(r"(?<![\w-])(" + "|".join(sorted(map(re.escape, SAY_AS), key=len, reverse=True)) + r")(?![\w-])")


def say_as(text: str) -> str:
    return SAY_RE.sub(lambda m: SAY_AS[m.group(1)], text)


def main() -> None:
    model_dir = os.path.abspath(sys.argv[1])
    only = set(sys.argv[2:])
    videos = json.load(open(os.path.join(HERE, "videos.json"), encoding="utf-8"))
    proc = subprocess.Popen([ANALYZER], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace")
    next_id = [0]

    def call(cmd, args=None):
        next_id[0] += 1
        job = next_id[0]
        msg = {"id": job, "cmd": cmd}
        if args is not None:
            msg["args"] = args
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if reply.get("id") == job and ("ok" in reply or "error" in reply or "result" in reply):
                return reply
        raise RuntimeError("the analyzer stopped")

    print("analyzer:", json.dumps(call("hello"))[:160])
    total = spoken = 0
    for video in videos:
        if only and video["id"] not in only:
            continue
        folder = os.path.join(HERE, "out", "voice", video["id"])
        os.makedirs(folder, exist_ok=True)
        for scene in video["scenes"]:
            total += 1
            # "speak" is the wording the voice reads where the caption's spelling reads badly.
            text = say_as(scene.get("speak") or scene["say"])
            key = hashlib.sha256(f"{VOICE}|{SPEED}|{text}".encode()).hexdigest()[:16]
            wav = os.path.join(folder, f"{scene['id']}.wav")
            meta = os.path.join(folder, f"{scene['id']}.json")
            if os.path.exists(wav) and os.path.exists(meta) and json.load(open(meta, encoding="utf-8")).get("key") == key:
                continue
            reply = call("tts", {"text": text, "voice": VOICE, "speed": SPEED, "out": wav, "modelDir": model_dir})
            data = reply.get("data") or reply.get("result")
            if not data:
                raise RuntimeError(f"{video['id']}/{scene['id']}: {json.dumps(reply)[:300]}")
            json.dump({"key": key, "text": text, "durationSec": data["durationSec"], "words": data.get("words") or []},
                      open(meta, "w", encoding="utf-8"))
            spoken += 1
            print(f"  {video['id']}/{scene['id']}: {data['durationSec']:.1f}s", flush=True)
    proc.terminate()
    print(f"{spoken} spoken, {total - spoken} kept")


main()
