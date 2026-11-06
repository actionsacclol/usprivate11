"""Render the PolyBot setup guide: real app footage, Bluey on the voiceover, karaoke captions.

  python render.py <voice-name> [--preview t0 t1] [--stills]
  reads <voice>.mp3 + <voice>.align.json + script.json + shots/*.png, writes out/<voice>.mp4
"""
import json, math, os, subprocess, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
P = lambda *a: os.path.join(HERE, *a)
VOICE = sys.argv[1]
W, H = 1920, 1080
FPS = 30
PREVIEW = None
if "--preview" in sys.argv:
    i = sys.argv.index("--preview"); PREVIEW = (float(sys.argv[i + 1]), float(sys.argv[i + 2]))
STILLS = "--stills" in sys.argv
SPRITE = os.environ.get("BLUEY", os.path.join(HERE, "..", "..", "src", "assets", "spritebluey.png"))

ACCENT = (192, 132, 252)
BLUE = (96, 165, 250)
GREEN = (74, 222, 128)
RED = (248, 113, 113)
FONT = {
    "b": P("fonts", "chakra-petch-700.ttf"), "s": P("fonts", "chakra-petch-600.ttf"),
    "m": P("fonts", "chakra-petch-500.ttf"), "px": P("fonts", "press-start-2p-400.ttf"),
    "mono": P("fonts", "jetbrains-mono-400.ttf"),
}
_fc = {}
def font(k, size):
    if (k, size) not in _fc: _fc[(k, size)] = ImageFont.truetype(FONT[k], size)
    return _fc[(k, size)]

# ── timing from the alignment ────────────────────────────────────────────────
script = json.load(open(P("script.json"), encoding="utf-8"))
al = json.load(open(P(f"{VOICE}.align.json"), encoding="utf-8"))
text, A = al["text"], al["alignment"]
chars, cs, ce = A["characters"], A["character_start_times_seconds"], A["character_end_times_seconds"]
assert "".join(chars) == text, "alignment does not match the script text"

words = []  # (word, t0, t1, char_index)
i = 0
while i < len(text):
    if text[i].isspace(): i += 1; continue
    j = i
    while j < len(text) and not text[j].isspace(): j += 1
    words.append((text[i:j], cs[i], ce[j - 1], i))
    i = j

offs, o = [], 0
for s in script:
    offs.append(o); o += len(s["text"]) + 2
AUDIO_END = ce[-1]
TOTAL = AUDIO_END + 3.5  # hold the safety card a moment after the last word
scene_t = {}
for k, s in enumerate(script):
    t0 = 0.0 if k == 0 else cs[offs[k]] - 0.25
    t1 = TOTAL if k == len(script) - 1 else cs[offs[k + 1]] - 0.25
    scene_t[s["id"]] = (t0, t1, offs[k], offs[k] + len(s["text"]))

def norm(w): return "".join(c for c in w.lower() if c.isalnum() or c == "'").replace("’", "'")
def cue(scene, word, n=0, end=False):
    """time the n-th occurrence of `word` is spoken inside `scene`"""
    a, b = scene_t[scene][2], scene_t[scene][3]
    hits = [w for w in words if a <= w[3] < b and norm(w[0]) == norm(word)]
    if len(hits) <= n: raise KeyError(f"{scene}: no '{word}' #{n}")
    return hits[n][2] if end else hits[n][1]

# ── audio envelope (Bluey bounces with the voice) ────────────────────────────
SR = 22050
pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", P(f"{VOICE}.mp3"), "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                     capture_output=True, check=True).stdout
pcm = np.frombuffer(pcm, dtype=np.float32)
hop = SR // FPS
nfr = int(math.ceil(TOTAL * FPS)) + 2
env = np.zeros(nfr)
for f in range(nfr):
    seg = pcm[f * hop:(f + 1) * hop]
    env[f] = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
env /= max(np.percentile(env[env > 0], 95), 1e-6)
sm = np.zeros(nfr)
for f in range(1, nfr):
    a = 0.75 if env[f] > sm[f - 1] else 0.35
    sm[f] = sm[f - 1] + a * (env[f] - sm[f - 1])
env = np.clip(sm, 0, 1.4)

# ── sources ──────────────────────────────────────────────────────────────────
_img = {}
def img(name):
    if name not in _img:
        im = Image.open(P("shots", name.replace("~blur", ""))).convert("RGB")
        _img[name] = im.filter(ImageFilter.GaussianBlur(14)) if name.endswith("~blur") else im
    return _img[name]

def ease(x): x = min(max(x, 0.0), 1.0); return x * x * (3 - 2 * x)

def view(src, r0, r1, u):
    """crop rect (x, y, w) in source pixels, 16:9, eased between r0 and r1"""
    e = ease(u)
    x, y, w = [a + (b - a) * e for a, b in zip(r0, r1)]
    h = w * 9 / 16
    x, y = min(max(x, 0), 1920 - w), min(max(y, 0), 1080 - h)  # never crop past the screenshot
    return img(src).resize((W, H), Image.BICUBIC, box=(x, y, x + w, y + h)), (x, y, w, h)

# ── shot list (boxes are screenshot pixels, measured from the live DOM) ───────
FULL = (0, 0, 1920)
def shots_for(sid):
    t0, t1 = scene_t[sid][:2]
    if sid == "intro":
        S = [(t0, "home.png", FULL, (100, 40, 1700), [])]
    elif sid == "signup":
        S = [(t0, "onb1.png", (380, 0, 1160), (520, 230, 860), [(cue(sid, "Sign"), (611, 427, 948, 473))])]
    elif sid == "onboard":
        S = [(t0, "onb-disc.png", (440, 150, 1040), (440, 400, 1040),
              [(cue(sid, "boxes"), (590, 404, 1330, 544)), (cue(sid, "Continue"), (1200, 854, 1330, 900))]),
             (cue(sid, "Skip") - 0.3, "onb-agent.png", (480, 400, 960), (480, 440, 960),
              [(cue(sid, "Skip"), (1126, 820, 1194, 866))]),
             (cue(sid, "Start") - 0.3, "onb-start.png", (500, 330, 900), (560, 400, 820),
              [(cue(sid, "Start"), (1157, 683, 1329, 728))])]
    elif sid == "export":
        S = [(t0, "wallet.png~blur", FULL, (60, 34, 1800), [])]
    elif sid == "wallet":
        S = [(t0, "home.png", (0, 120, 1200), (0, 300, 900), [(cue(sid, "Wallet"), (10, 641, 277, 684))]),
             (cue(sid, "Paste") - 0.3, "wallet.png", (280, 260, 1440), (300, 300, 1360),
              [(cue(sid, "key"), (342, 499, 1854, 545)), (cue(sid, "address"), (358, 652, 1838, 697)),
               (cue(sid, "Save"), (342, 794, 525, 840)), (cue(sid, "Test"), (534, 794, 628, 840))])]
    elif sid == "connect":
        S = [(t0, "agents.png~blur", FULL, (60, 34, 1800), []),
             (cue(sid, "Back") - 0.3, "agents.png", (0, 0, 1500), (0, 30, 1100),
              [(cue(sid, "AI", 1), (10, 150, 277, 193))]),
             (cue(sid, "Pick") - 0.3, "agents-cd.png", (300, 200, 1100), (280, 230, 960),
              [(cue(sid, "Pick"), (549, 319, 757, 360)), (cue(sid, "Add"), (934, 440, 1175, 486))]),
             (cue(sid, "Then") - 0.2, "agents-cd.png~blur", (280, 230, 960), (240, 200, 1100), [])]
    elif sid == "golive":
        S = [(t0, "agents.png", (700, 60, 1220), (1000, 110, 920), [(cue(sid, "Go"), (1779, 200, 1891, 246))]),
             (cue(sid, "Test") - 0.4, "gl1.png", (560, 150, 840), (510, 180, 900),
              [(cue(sid, "Test"), (625, 249, 756, 290))]),
             (cue(sid, "tick") - 0.1, "gl2.png", (510, 180, 900), (510, 180, 900),
              [(cue(sid, "tick"), (625, 491, 1307, 560))]),
             (cue(sid, "agent") - 0.4, "gl3.png", (480, 60, 960), (440, 80, 960),
              [(cue(sid, "agent"), (625, 169, 1307, 254)), (cue(sid, "Use"), (625, 461, 770, 502)),
               (cue(sid, "five"), (625, 380, 1307, 455))]),
             (cue(sid, "Switch") - 0.3, "gl3.png", (440, 250, 960), (440, 300, 960), [(cue(sid, "Switch"), (625, 653, 1307, 708))]),
             (cue(sid, "Turn") - 0.2, "noapp.png", (440, 300, 960), (500, 330, 880), [(cue(sid, "Turn"), (1032, 608, 1205, 651))]),
             (cue(sid, "so") - 0.1, "gl3b.png", (440, 300, 960), (440, 300, 960), [(cue(sid, "so"), (581, 532, 1322, 724))]),
             (cue(sid, "Review") - 0.4, "gl4.png", (560, 480, 960), (640, 630, 800),
              [(cue(sid, "Review"), (1182, 942, 1322, 983))]),
             (cue(sid, "then") - 0.2, "gl5.png", (560, 520, 800), (560, 560, 800),
              [(cue(sid, "Go", 2), (1258, 872, 1334, 913))])]
    elif sid == "trade":
        S = [(t0, "agents.png~blur", (60, 34, 1800), FULL, []),
             (cue(sid, "keep") - 0.3, "agents.png", (0, 0, 1920), (300, 200, 1100),
              [(cue(sid, "Set"), (987, 441, 1174, 485))])]
    elif sid == "outro":
        S = [(t0, "home.png~blur", (100, 40, 1700), FULL, [])]
    out = []
    for k, (s, src, r0, r1, hl) in enumerate(S):
        e = S[k + 1][0] if k + 1 < len(S) else t1
        out.append(dict(t0=s, t1=e, src=src, r0=r0, r1=r1, hl=hl, scene=sid))
    return out

SHOTS = [sh for s in script for sh in shots_for(s["id"])]
XF = 0.4  # crossfade

def render_shot(sh, t):
    span = max(sh["t1"] - sh["t0"], 0.01)
    return view(sh["src"], sh["r0"], sh["r1"], (t - sh["t0"]) / (span + XF))

def bg_at(t):
    k = max(i for i, s in enumerate(SHOTS) if s["t0"] <= t or i == 0)
    sh = SHOTS[k]
    frame, rect = render_shot(sh, t)
    if k > 0 and t < sh["t0"] + XF:
        prev, _ = render_shot(SHOTS[k - 1], t)
        frame = Image.blend(prev, frame, ease((t - sh["t0"]) / XF))
    return frame, rect, sh

# ── overlays ─────────────────────────────────────────────────────────────────
def rrect(d, box, r, **kw): d.rounded_rectangle(box, radius=r, **kw)

def glass(size, r=22, fill=(16, 10, 30, 214), outline=(255, 255, 255, 40)):
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    rrect(ImageDraw.Draw(im), (0, 0, size[0] - 1, size[1] - 1), r, fill=fill, outline=outline, width=2)
    return im

def put(frame, layer, xy, opacity=1.0):
    if opacity <= 0: return
    if opacity < 1:
        a = layer.split()[3].point(lambda v: int(v * opacity))
        layer = layer.copy(); layer.putalpha(a)
    frame.paste(layer, (int(xy[0]), int(xy[1])), layer)

VIG = Image.new("L", (1, H))  # bottom vignette so captions read on any frame
for y in range(H):
    v = max(0.0, (y - H * 0.62) / (H * 0.38))
    VIG.putpixel((0, y), int(185 * v ** 1.6))
VIG = VIG.resize((W, H))
BLACK = Image.new("RGB", (W, H), (6, 3, 14))

# Bluey: the app's own 8-frame idle strip, cycling faster and bouncing while she talks
STRIP = Image.open(SPRITE).convert("RGBA")
BFRAMES = [STRIP.crop((i * 48, 0, i * 48 + 48, 48)).resize((230, 230), Image.NEAREST) for i in range(8)]
BADGE = 236
def draw_bluey(frame, t, f, show):
    if show <= 0: return
    e = float(env[min(f, nfr - 1)])
    talking = t < AUDIO_END + 0.1
    phase = t * (8 / 0.9) + float(np.sum(env[:min(f, nfr - 1)])) / FPS * (8 / 0.9) * 1.2
    spr = BFRAMES[int(phase) % 8]
    lay = Image.new("RGBA", (BADGE + 80, BADGE + 110), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    cx, cy = 40 + BADGE // 2, 40 + BADGE // 2
    glow = int(70 + 120 * min(e, 1.0)) if talking else 60
    halo = Image.new("RGBA", lay.size, (0, 0, 0, 0))
    ImageDraw.Draw(halo).ellipse((cx - BADGE // 2 - 10, cy - BADGE // 2 - 10, cx + BADGE // 2 + 10, cy + BADGE // 2 + 10),
                                 fill=(59, 130, 246, glow))
    lay.alpha_composite(halo.filter(ImageFilter.GaussianBlur(16)))
    d.ellipse((cx - BADGE // 2, cy - BADGE // 2, cx + BADGE // 2, cy + BADGE // 2), fill=(14, 18, 40, 245),
              outline=BLUE + (255,), width=4)
    bob = math.sin(t * 2.4) * 4 - (e * 9 if talking else 0)
    clip = Image.new("L", lay.size, 0)
    ImageDraw.Draw(clip).ellipse((cx - BADGE // 2 + 4, cy - BADGE // 2 + 4, cx + BADGE // 2 - 4, cy + BADGE // 2 - 4), fill=255)
    sl = Image.new("RGBA", lay.size, (0, 0, 0, 0))
    sl.alpha_composite(spr, (cx - 115, int(cy - 115 + 6 + bob)))
    sl.putalpha(Image.composite(sl.split()[3], Image.new("L", lay.size, 0), clip))
    lay.alpha_composite(sl)
    tag, fnt = "BLUEY", font("px", 18)
    tw = d.textlength(tag, font=fnt)
    bx0, by0 = cx - tw / 2 - 16, cy + BADGE // 2 - 6
    rrect(d, (bx0, by0, bx0 + tw + 32, by0 + 40), 12, fill=(37, 99, 235, 255), outline=(191, 219, 254, 255), width=2)
    d.text((cx - tw / 2, by0 + 11), tag, font=fnt, fill=(255, 255, 255, 255))
    put(frame, lay, (34 - 40, H - BADGE - 150 + (1 - ease(show)) * 60), opacity=min(1, show))

# captions
def caption_lines():
    sents, cur = [], []
    for w in words:
        cur.append(w)
        if w[0][-1] in ".!?":
            sents.append(cur); cur = []
    if cur: sents.append(cur)
    lines = []
    MAXC = 44
    for s in sents:
        n = len(s)
        best = [(0.0, -1)] + [(1e18, -1)] * n
        for j in range(1, n + 1):
            for i in range(j):
                L = len(" ".join(x[0] for x in s[i:j]))
                if L > MAXC and j - i > 1: continue
                pen = 0 if j == n or s[j - 1][0][-1] in ",:;" else 260
                cost = best[i][0] + (MAXC - L) ** 2 * 0.15 + pen + 120
                if j - i <= 2 and n > 2: cost += 400
                if cost < best[j][0]: best[j] = (cost, i)
        cuts, j = [], n
        while j > 0:
            i = best[j][1]; cuts.append((i, j)); j = i
        for i, j in reversed(cuts): lines.append(s[i:j])
    out = []
    for k, ln in enumerate(lines):
        s = ln[0][1] - 0.12
        e = (lines[k + 1][0][1] - 0.12) if k + 1 < len(lines) else ln[-1][2] + 0.9
        out.append((s, min(e, ln[-1][2] + 0.9), ln))
    return out
CAPS = caption_lines()

def draw_caption(frame, t):
    for s, e, ln in CAPS:
        if s <= t < e: break
    else:
        return
    fnt = font("s", 50)
    d0 = ImageDraw.Draw(frame)
    space = d0.textlength(" ", font=fnt)
    widths = [d0.textlength(w[0], font=fnt) for w in ln]
    total = sum(widths) + space * (len(ln) - 1)
    pad_x, hgt = 34, 82
    lay = Image.new("RGBA", (int(total + pad_x * 2), hgt), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    rrect(d, (0, 0, lay.width - 1, hgt - 1), 20, fill=(10, 6, 22, 190))
    x = pad_x
    for (wd, w0, w1, _), wid in zip(ln, widths):
        col = (255, 255, 255, 255) if t >= w1 else BLUE + (255,) if t >= w0 else (200, 190, 225, 120)
        d.text((x, 10), wd, font=fnt, fill=col)
        x += wid + space
    a = min(1, (t - s) / 0.15, (e - t) / 0.15)
    put(frame, lay, (300 + (W - 360) / 2 - lay.width / 2, H - 128), opacity=a)

STEPS = {"signup": "STEP 1  SIGN UP", "onboard": "STEP 2  START ON PAPER", "export": "STEP 3  COPY YOUR KEY",
         "wallet": "STEP 4  CONNECT", "connect": "STEP 5  ADD CLAUDE", "golive": "STEP 6  GO LIVE",
         "trade": "STEP 7  LET IT TRADE"}
def scene_chip(frame, t):
    for sid, lab in STEPS.items():
        t0, t1 = scene_t[sid][:2]
        if t0 <= t < t1:
            fnt = font("px", 26)
            tw = int(ImageDraw.Draw(frame).textlength(lab, font=fnt))
            lay = glass((tw + 64, 74), r=18, fill=(16, 10, 30, 230), outline=ACCENT + (170,))
            ImageDraw.Draw(lay).text((32, 24), lab, font=fnt, fill=(255, 255, 255, 255))
            a = min(1, (t - t0) / 0.35, (t1 - t) / 0.3)
            put(frame, lay, (44 + (1 - ease(min(1, (t - t0) / 0.45))) * -40, 40), opacity=a)

def highlight(frame, rect, box, t, tc):
    if t < tc: return
    x, y, w, h = rect
    sx, sy = W / w, H / (w * 9 / 16)
    b = ((box[0] - x) * sx - 10, (box[1] - y) * sy - 10, (box[2] - x) * sx + 10, (box[3] - y) * sy + 10)
    k = ease(min(1, (t - tc) / 0.35))
    grow = (1 - k) * 26
    b = (b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow)
    lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(lay)
    pulse = 0.65 + 0.35 * math.sin((t - tc) * 5)
    for wdt, al_ in ((14, int(50 * pulse)), (8, int(90 * pulse)), (4, 255)):
        rrect(d, b, 16, outline=(250, 204, 21, int(al_ * k)), width=wdt)
    frame.paste(lay, (0, 0), lay)

def dim(frame, amt):
    return Image.blend(frame, BLACK, amt) if amt > 0 else frame

def big_text(d, xy, s, fnt, fill, anchor="mm"):
    d.text((xy[0] + 4, xy[1] + 5), s, font=fnt, fill=(0, 0, 0, 160), anchor=anchor)
    d.text(xy, s, font=fnt, fill=fill, anchor=anchor)

def intro_overlay(frame, t):
    t0, t1 = scene_t["intro"][:2]
    if not (t0 <= t < t1 + 0.5): return frame
    a = min(1, (t - 0.2) / 0.5, (t1 + 0.5 - t) / 0.5)
    frame = dim(frame, 0.5 * a)
    lay = Image.new("RGBA", (W, 420), (0, 0, 0, 0)); d = ImageDraw.Draw(lay)
    big_text(d, (W / 2, 150), "KRYPT POLYBOT", font("px", 84), (255, 255, 255, 255))
    big_text(d, (W / 2, 262), "Your AI trading Polymarket in 7 steps", font("s", 50), (191, 219, 254, 255))
    put(frame, lay, (0, H / 2 - 260 + (1 - ease(a)) * 30), opacity=a)
    return frame

def export_overlay(frame, t):
    """Polymarket's own screens aren't ours to film: the steps as a card instead."""
    sid = "export"; t0, t1 = scene_t[sid][:2]
    return steps_card(frame, t, t0, t1, "ON POLYMARKET.COM", [
        ("1", "Settings  >  Export private key", cue(sid, "Export"), ACCENT),
        ("2", "Turn on Auto-redeem", cue(sid, "redeem"), ACCENT),
        ("3", "Copy your deposit address  0x...", cue(sid, "deposit"), ACCENT),
        ("!", "Never share your key. Ever.", cue(sid, "Never"), RED)])

def claude_overlay(frame, t):
    """Claude Desktop is someone else's app too: getting it, and restarting it, as cards."""
    sid = "connect"; t0, t1 = scene_t[sid][:2]
    frame = steps_card(frame, t, t0, cue(sid, "Back") - 0.3, "GET CLAUDE DESKTOP", [
        ("1", "Download it from claude.ai/download", cue(sid, "Claude"), ACCENT),
        ("2", "Open it and sign in", cue(sid, "sign"), ACCENT)])
    return steps_card(frame, t, cue(sid, "Then") - 0.2, t1, "THEN RESTART CLAUDE", [
        ("1", "Tray icon  >  right-click  >  Quit", cue(sid, "quit"), ACCENT),
        ("2", "Open Claude Desktop again", cue(sid, "open"), ACCENT)])

PROMPT = "Use krypt-polybot. Look through all the markets and buy where you have an edge."
def prompt_overlay(frame, t):
    """What to type into Claude, typed out as she says it, then its permission prompt."""
    sid = "trade"; t0 = scene_t[sid][0]; t1 = cue(sid, "keep") - 0.3
    if not (t0 <= t < t1 + 0.4): return frame
    a = min(1, (t - t0) / 0.4, (t1 + 0.4 - t) / 0.4)
    frame = dim(frame, 0.35 * a)
    cw, ch = 1240, 720
    card = glass((cw, ch), r=30, fill=(18, 11, 34, 242), outline=ACCENT + (170,))
    d = ImageDraw.Draw(card)
    d.text((52, 46), "IN CLAUDE DESKTOP", font=font("px", 26), fill=(233, 213, 255, 255))
    ts, te = cue(sid, "use"), cue(sid, "edge", end=True)
    n = int(len(PROMPT) * min(1, max(0, (t - ts) / max(te - ts, 0.1))))
    rrect(d, (40, 112, cw - 40, 330), 22, fill=(32, 24, 52, 255), outline=(255, 255, 255, 60), width=2)
    fnt, y, line = font("m", 44), 140, ""
    for wd in PROMPT[:n].split(" "):  # wrap by hand
        if line and d.textlength(line + wd, font=fnt) > cw - 150:
            d.text((72, y), line, font=fnt, fill=(255, 255, 255, 255)); y += 58; line = ""
        line += wd + " "
    d.text((72, y), line.rstrip() + ("|" if int(t * 2.5) % 2 else ""), font=fnt, fill=(255, 255, 255, 255))
    ta, tc = cue(sid, "asks"), cue(sid, "Allow")
    if t >= ta - 0.1:
        e = ease((t - ta + 0.1) / 0.3)
        rrect(d, (40, 370, cw - 40, 590), 22, fill=(16, 10, 30, int(255 * e)), outline=ACCENT + (int(200 * e),), width=3)
        d.text((72, 420), "Claude wants to use krypt-polybot", font=font("b", 42), fill=(255, 255, 255, int(255 * e)), anchor="lm")
        bx, hot = (72, 470, 330, 556), t >= tc - 0.1
        rrect(d, bx, 18, fill=(250, 204, 21, int(255 * e)) if hot else (59, 130, 246, int(255 * e)))
        d.text(((bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2), "Allow", font=font("b", 40),
               fill=(12, 8, 22, int(255 * e)) if hot else (255, 255, 255, int(255 * e)), anchor="mm")
    tw_ = cue(sid, "works")
    if t >= tw_ - 0.1:
        e = ease((t - tw_ + 0.1) / 0.3)
        d.text((cw / 2, 650), "It trades while it works on your message", font=font("s", 40),
               fill=(254, 240, 138, int(255 * e)), anchor="mm")
    put(frame, card, ((W - cw) / 2, 110 + (1 - ease(a)) * 40), opacity=a)
    return frame

def steps_card(frame, t, t0, t1, title, rows, cw=1120):
    """Numbered rows that light up as each is said: for screens that aren't ours to film."""
    if not (t0 <= t < t1 + 0.4): return frame
    a = min(1, (t - t0) / 0.4, (t1 + 0.4 - t) / 0.4)
    frame = dim(frame, 0.35 * a)
    card = glass((cw, 150 + 118 * len(rows)), r=30, fill=(18, 11, 34, 242), outline=ACCENT + (170,))
    d = ImageDraw.Draw(card)
    d.text((52, 46), title, font=font("px", 26), fill=(233, 213, 255, 255))
    y = 120
    for num, s, tc, col in rows:
        if t >= tc - 0.1:
            e = ease((t - tc + 0.1) / 0.3)
            rrect(d, (40, y, cw - 40, y + 96), 22, fill=col + (int(60 * e),), outline=col + (int(230 * e),), width=3)
            d.ellipse((64, y + 22, 116, y + 74), fill=col + (int(255 * e),))
            d.text((90, y + 48), num, font=font("b", 34), fill=(12, 8, 22, int(255 * e)), anchor="mm")
            d.text((140, y + 48), s, font=font("b", 44), fill=(255, 255, 255, int(255 * e)), anchor="lm")
        y += 118
    put(frame, card, ((W - cw) / 2, 120 + (1 - ease(a)) * 40), opacity=a)
    return frame

def running_overlay(frame, t):
    sid = "trade"; t1 = scene_t[sid][1]
    tc = cue(sid, "trading", 1)  # #0 is "keep trading around the clock"
    if not (tc - 0.2 <= t < t1 + 0.3): return frame
    a = min(1, (t - tc + 0.2) / 0.35, (t1 + 0.3 - t) / 0.3)
    frame = dim(frame, 0.45 * a)
    fnt = font("b", 72)
    s = "Your AI is trading!"
    tw = int(ImageDraw.Draw(frame).textlength(s, font=fnt))
    lay = glass((tw + 200, 150), r=36, fill=(20, 83, 45, 240), outline=GREEN + (255,))
    d = ImageDraw.Draw(lay)
    d.ellipse((44, 43, 108, 107), fill=GREEN + (255,))
    d.line((60, 76, 72, 90, 94, 60), fill=(12, 40, 20, 255), width=9, joint="curve")
    d.text((140, 75), s, font=fnt, fill=(255, 255, 255, 255), anchor="lm")
    put(frame, lay, ((W - lay.width) / 2, 300 - (1 - ease(a)) * 30), opacity=a)
    if t >= tc + 0.3:
        e = ease((t - tc - 0.3) / 0.3) * a
        s2, f2 = "$5 a trade max  ·  stops for the day at -$10", font("s", 44)
        tw2 = int(ImageDraw.Draw(frame).textlength(s2, font=f2))
        p = glass((tw2 + 80, 86), r=24, fill=(16, 10, 30, 235), outline=(250, 204, 21, 220))
        ImageDraw.Draw(p).text(((tw2 + 80) / 2, 43), s2, font=f2, fill=(254, 240, 138, 255), anchor="mm")
        put(frame, p, ((W - p.width) / 2, 480), opacity=e)
    return frame

def outro_overlay(frame, t):
    sid = "outro"; t0 = scene_t[sid][0]
    if t < t0: return frame
    a = min(1, (t - t0) / 0.5)
    frame = dim(frame, 0.55 * a)
    cw, ch = 1240, 560
    card = glass((cw, ch), r=34, fill=(40, 10, 16, 245), outline=RED + (255,))
    d = ImageDraw.Draw(card)
    cx = cw / 2
    d.polygon([(cx, 40), (cx - 62, 146), (cx + 62, 146)], fill=(250, 204, 21, 255))
    d.text((cx, 112), "!", font=font("b", 70), fill=(40, 10, 16, 255), anchor="mm")
    d.text((cx, 210), "BOT NOT WORKING?", font=font("px", 40), fill=(255, 255, 255, 255), anchor="mm")
    rows = [("Turn OFF your VPN", cue(sid, "disabling"), 52),
            ("You must be where polymarket.com is legal", cue(sid, "region"), 42)]
    y = 300
    for s, tc, size in rows:
        if t >= tc - 0.1:
            e = ease((t - tc + 0.1) / 0.3)
            rrect(d, (60, y, cw - 60, y + 100), 24, fill=(127, 29, 29, int(230 * e)), outline=RED + (int(255 * e),), width=3)
            d.text((cx, y + 50), s, font=font("b", size), fill=(255, 255, 255, int(255 * e)), anchor="mm")
        y += 124
    put(frame, card, ((W - cw) / 2, 80 + (1 - ease(a)) * 40), opacity=a)
    return frame

# ── frame loop ───────────────────────────────────────────────────────────────
def render(t, f):
    frame, rect, sh = bg_at(t)
    for tc, box in sh["hl"]:
        highlight(frame, rect, box, t, tc)
    frame = intro_overlay(frame, t)
    frame = export_overlay(frame, t)
    frame = claude_overlay(frame, t)
    frame = prompt_overlay(frame, t)
    frame = running_overlay(frame, t)
    frame = outro_overlay(frame, t)
    frame.paste(BLACK, (0, 0), VIG)
    scene_chip(frame, t)
    draw_bluey(frame, t, f, min(1, (t - 0.4) / 0.6))
    draw_caption(frame, t)
    fade = min(1, t / 0.5, (TOTAL - t) / 0.8)
    if fade < 1: frame = Image.blend(BLACK, frame, max(0, fade))
    return frame

if __name__ == "__main__":
    os.makedirs(P("out"), exist_ok=True)
    print("scenes:", {k: (round(v[0], 2), round(v[1], 2)) for k, v in scene_t.items()}, "total", round(TOTAL, 2))
    if STILLS:
        for k, s in enumerate(script):
            t0, t1 = scene_t[s["id"]][:2]
            for q in (0.35, 0.85):
                t = t0 + (t1 - t0) * q
                render(t, int(t * FPS)).save(P("out", f"still-{k:02d}-{s['id']}-{int(q*100)}.jpg"), quality=88)
        sys.exit()
    f0, f1 = 0, int(TOTAL * FPS)
    if PREVIEW: f0, f1 = int(PREVIEW[0] * FPS), int(PREVIEW[1] * FPS)
    out = P("out", f"{VOICE}{'-preview' if PREVIEW else ''}.mp4")
    ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
                           "-i", "-", "-ss", str(f0 / FPS), "-i", P(f"{VOICE}.mp3"),
                           "-filter_complex", f"[1:a]apad=whole_dur={(f1 - f0) / FPS}[a]", "-map", "0:v", "-map", "[a]",
                           "-c:v", "libx264", "-preset", "medium", "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                           "-c:a", "aac", "-b:a", "160k", "-shortest", out], stdin=subprocess.PIPE)
    for f in range(f0, f1):
        ff.stdin.write(render(f / FPS, f).tobytes())
        if f % 300 == 0: print(f"{f / FPS:6.1f}s", flush=True)
    ff.stdin.close(); ff.wait()
    print("wrote", out)
