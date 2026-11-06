"""Splice re-voiced scenes into an existing narration, keeping every other line's audio as it was.

  python splice.py <base> <part> <out>
  <base>.mp3/.align.json: a full narration. <part>.mp3/.align.json: scenes from tts.py --scenes.
  Writes <out>.mp3 + <out>.align.json in script.json order, ready for render.py <out>.
  A base scene is found by its exact text, so only the lines tts.py re-voiced may differ from it.
"""
import json, os, subprocess, sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
P = lambda *a: os.path.join(HERE, *a)
SR = 44100
PAD = 0.4  # extra silence at each join with the new part (the lines keep ~0.8 s between them)

def load(name):
    al = json.load(open(P(f"{name}.align.json"), encoding="utf-8"))
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", P(f"{name}.mp3"), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    a = al["alignment"]
    return al["text"], a["characters"], a["character_start_times_seconds"], a["character_end_times_seconds"], np.frombuffer(pcm, np.float32)

def spans(text, cs, ce):
    """{scene text: (char offset, start s, end s)} for each \\n\\n-separated line"""
    out, o = {}, 0
    for line in text.split("\n\n"):
        out[line] = (o, cs[o], ce[o + len(line) - 1])
        o += len(line) + 2
    return out

base, part, outname = sys.argv[1:4]
script = json.load(open(P("script.json"), encoding="utf-8"))
src = {"base": load(base), "part": load(part)}
sp = {k: spans(v[0], v[2], v[3]) for k, v in src.items()}
lines = {k: list(sp[k]) for k in sp}

pieces, chars, starts, ends = [], [], [], []
t = 0.0
for k, s in enumerate(script):
    who = "part" if s["text"] in sp["part"] else "base"
    if s["text"] not in sp[who]: sys.exit(f"scene '{s['id']}' is in neither {base} nor {part}: voice it with tts.py --scenes")
    text, ch, cs, ce, pcm = src[who]
    o, a, b = sp[who][s["text"]]
    # Cut halfway into the silence on each side of the line (the file's own edge where there is none).
    i = lines[who].index(s["text"])
    lo = 0.0 if i == 0 else (sp[who][lines[who][i - 1]][2] + a) / 2
    hi = len(pcm) / SR if i == len(lines[who]) - 1 else (b + sp[who][lines[who][i + 1]][1]) / 2
    prev = None if k == 0 else ("part" if script[k - 1]["text"] in sp["part"] else "base")
    if prev and prev != who:
        pieces.append(np.zeros(int(PAD * SR), np.float32)); t += PAD
    if k:  # the "\n\n" between lines, timed across the gap
        gap0 = ends[-1]
        for _ in range(2):
            chars.append("\n"); starts.append(gap0); ends.append(gap0)
    seg = pcm[int(lo * SR):int(hi * SR)]
    shift = t - lo
    for j in range(len(s["text"])):
        chars.append(ch[o + j]); starts.append(cs[o + j] + shift); ends.append(ce[o + j] + shift)
    pieces.append(seg); t += len(seg) / SR

pcm = np.concatenate(pieces)
subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                "-c:a", "libmp3lame", "-b:a", "128k", P(f"{outname}.mp3")], input=pcm.tobytes(), check=True)
text = "\n\n".join(s["text"] for s in script)
assert "".join(chars) == text
json.dump({"text": text, "alignment": {"characters": chars, "character_start_times_seconds": starts,
                                       "character_end_times_seconds": ends}},
          open(P(f"{outname}.align.json"), "w", encoding="utf-8"))
print("ok", outname, round(len(pcm) / SR, 2), "s")
