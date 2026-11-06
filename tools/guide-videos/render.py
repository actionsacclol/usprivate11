"""Render the guide videos: real app screens, Bluey on the voiceover, karaoke captions.

  python render.py [video-id ...]            full renders into out/<id>.mp4 + .jpg
  python render.py --stills [video-id ...]   one frame per scene into out/stills/<id>/, to look at first
  python render.py --install                 copies out/*.mp4 + .jpg + a manifest into src/assets/guide/

Reads videos.json, shots/<video>/<scene>.png + .json (capture.mjs) and out/voice/<video>/ (tts.py).
"""
import json
import math
import multiprocessing as mp
import os
import shutil
import struct
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
P = lambda *a: os.path.join(HERE, *a)
W, H = 1280, 720
K = W / 1920  # layout numbers below are for 1080p
FPS = 24
SRC_W, SRC_H = 1920, 1080
GAP = 0.35  # silence between scenes
LEAD = 2.2  # title card, before the voice
TAIL = 1.1  # last frame, after the voice
XF = 0.35  # crossfade
SR = 24000

BLUE = (96, 165, 250)
GREEN = (74, 222, 128)
RED = (248, 113, 113)
YELLOW = (250, 204, 21)
FONT = {k: P("fonts", f) for k, f in {"b": "chakra-petch-700.ttf", "s": "chakra-petch-600.ttf", "m": "chakra-petch-500.ttf",
                                      "px": "press-start-2p-400.ttf"}.items()}
_fc = {}


def font(k, size):
    size = max(8, int(round(size * K)))
    if (k, size) not in _fc:
        _fc[(k, size)] = ImageFont.truetype(FONT[k], size)
    return _fc[(k, size)]


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def norm(w):
    return "".join(c for c in w.lower() if c.isalnum())


def caption_words(scene, meta):
    """The caption's words with the times the voice said them. The voice may read other spellings
    (tts.py's pronunciation map), so words are matched by how far through the line they fall."""
    shown = scene["say"].split()
    said = [w for w in meta["words"] if norm(w["text"])]
    if not said:
        d = meta["durationSec"]
        said = [{"text": w, "startSec": d * i / len(shown), "endSec": d * (i + 1) / len(shown)} for i, w in enumerate(shown)]
    if len(said) == len(shown):
        return [(w, s["startSec"], s["endSec"]) for w, s in zip(shown, said)]
    a = np.cumsum([0] + [len(norm(s["text"])) or 1 for s in said], dtype=float)
    a /= a[-1]
    t0s = [s["startSec"] for s in said]
    t1s = [s["endSec"] for s in said]
    b = np.cumsum([0] + [len(norm(w)) or 1 for w in shown], dtype=float)
    b /= b[-1]

    def at(frac):
        i = min(int(np.searchsorted(a, frac, side="right")) - 1, len(said) - 1)
        span = (a[i + 1] - a[i]) or 1
        return t0s[i] + (t1s[i] - t0s[i]) * (frac - a[i]) / span
    return [(w, at(b[i]), at(b[i + 1])) for i, w in enumerate(shown)]


class Video:
    def __init__(self, video):
        self.v = video
        self.vid = video["id"]
        self.scenes = []
        t = LEAD
        pcm = [np.zeros(int(LEAD * SR), dtype=np.float32)]
        for sc in video["scenes"]:
            meta = json.load(open(P("out", "voice", self.vid, f"{sc['id']}.json"), encoding="utf-8"))
            with open(P("out", "voice", self.vid, f"{sc['id']}.wav"), "rb") as f:
                data = read_wav(f.read())
            words = [(w, t + a, t + b) for w, a, b in caption_words(sc, meta)]
            dur = len(data) / SR
            shot = json.load(open(P("shots", self.vid, f"{sc['id']}.json"), encoding="utf-8"))
            hl = []
            for h, r in zip(sc.get("hl", []), shot["rects"]):
                if not r:
                    continue
                r = [max(0, r[0]), max(0, r[1]), min(SRC_W, r[2]), min(SRC_H, r[3])]
                if r[2] - r[0] < 4 or r[3] - r[1] < 4:
                    print(f"  {self.vid}/{sc['id']}: highlight off screen: {h['find']}")
                    continue
                hl.append({"box": r, "t": self.cue(words, h.get("at"), t, sc, h.get("n", 0))})
            end = t + dur + GAP
            self.scenes.append({"sc": sc, "t0": t, "t1": end, "words": words, "hl": hl, "src": P("shots", self.vid, f"{sc['id']}.png"),
                                "blur": bool(sc.get("blur"))})
            pcm += [data, np.zeros(int(GAP * SR), dtype=np.float32)]
            t = end
        self.voice_end = t - GAP
        self.total = t + TAIL
        pcm.append(np.zeros(int(TAIL * SR), dtype=np.float32))
        self.pcm = np.concatenate(pcm)
        self.nframes = int(math.ceil(self.total * FPS))
        self.env = self.envelope()
        self.caps = self.caption_lines()
        for i, s in enumerate(self.scenes):
            self.plan_camera(s, self.scenes[i - 1] if i else None)
        self._img = {}
        strip = Image.open(P("..", "..", "src", "assets", "spritebluey.png")).convert("RGBA")
        size = int(150 * K)
        self.bluey = [strip.crop((i * 48, 0, i * 48 + 48, 48)).resize((size, size), Image.NEAREST) for i in range(8)]
        vig = Image.new("L", (1, H))
        for y in range(H):
            vig.putpixel((0, y), int(170 * max(0.0, (y - H * 0.7) / (H * 0.3)) ** 1.6))
        self.vig = vig.resize((W, H))
        self.black = Image.new("RGB", (W, H), (6, 8, 18))

    def cue(self, words, at, t0, sc, n=0):
        if not at:
            return t0 + 0.2
        hits = [w for w in words if norm(w[0]).startswith(norm(at))]
        if len(hits) <= n:
            print(f"  {self.vid}/{sc['id']}: no cue word '{at}' in the line")
            return t0 + 0.3
        return hits[n][1]

    def envelope(self):
        hop = SR // FPS
        env = np.array([float(np.sqrt(np.mean(self.pcm[f * hop:(f + 1) * hop] ** 2))) if len(self.pcm[f * hop:(f + 1) * hop]) else 0.0
                        for f in range(self.nframes + 2)])
        env /= max(np.percentile(env[env > 0], 95), 1e-6)
        out = np.zeros(len(env))
        for f in range(1, len(env)):
            a = 0.75 if env[f] > out[f - 1] else 0.35
            out[f] = out[f - 1] + a * (env[f] - out[f - 1])
        return np.clip(out, 0, 1.4)

    def caption_lines(self):
        lines = []
        MAXC = 46
        for s in self.scenes:
            sents, cur = [], []
            for w in s["words"]:
                cur.append(w)
                if w[0][-1] in ".!?":
                    sents.append(cur)
                    cur = []
            if cur:
                sents.append(cur)
            for sent in sents:
                n = len(sent)
                best = [(0.0, -1)] + [(1e18, -1)] * n
                for j in range(1, n + 1):
                    for i in range(j):
                        L = len(" ".join(x[0] for x in sent[i:j]))
                        if L > MAXC and j - i > 1:
                            continue
                        pen = 0 if j == n or sent[j - 1][0][-1] in ",:;" else 260
                        cost = best[i][0] + (MAXC - L) ** 2 * 0.15 + pen + 120
                        if j - i <= 2 and n > 2:
                            cost += 400
                        if cost < best[j][0]:
                            best[j] = (cost, i)
                cuts, j = [], n
                while j > 0:
                    i = best[j][1]
                    cuts.append((i, j))
                    j = i
                lines += [(s, sent[i:j]) for i, j in reversed(cuts)]
        out = []
        for k, (s, ln) in enumerate(lines):
            start = ln[0][1] - 0.1
            nxt = lines[k + 1][1][0][1] - 0.1 if k + 1 < len(lines) else ln[-1][2] + 0.8
            out.append((start, min(nxt, ln[-1][2] + 0.8), ln, s))
        return out

    # ── camera ────────────────────────────────────────────────────────────────
    def plan_camera(self, s, prev):
        """Zoom until the highlighted things are easy to read, and keep them clear of the captions."""
        full = (0.0, 0.0, float(SRC_W))
        if s["blur"] or s["sc"].get("card") or s["sc"].get("prompt"):
            s["r0"], s["r1"] = full, full
            s["cam_to"] = s["t1"]
        elif not s["hl"]:
            # Nothing to point at: a slow push so the screen isn't a dead still.
            s["r0"], s["r1"] = full, (SRC_W * 0.03, SRC_H * 0.015, SRC_W * 0.94)
            s["cam_to"] = s["t1"] + 0.5
        else:
            x0 = min(h["box"][0] for h in s["hl"])
            y0 = min(h["box"][1] for h in s["hl"])
            x1 = max(h["box"][2] for h in s["hl"])
            y1 = max(h["box"][3] for h in s["hl"])
            uw, uh = x1 - x0, y1 - y0
            w = min(SRC_W, max(1080, uw * 1.45, uh * 1.75 * 16 / 9))
            h = w * 9 / 16
            x = (x0 + x1) / 2 - w / 2
            y = (y0 + y1) / 2 - h * 0.44  # a little high, because captions sit low
            x, y = min(max(x, 0), SRC_W - w), min(max(y, 0), SRC_H - h)
            s["r0"] = tuple(a + (b - a) * 0.55 for a, b in zip((x, y, w), full))
            s["r1"] = (x, y, w)
            s["cam_to"] = max(s["t0"] + 0.9, min(h_["t"] for h_ in s["hl"]) + 0.15)
            if prev is not None and not prev["blur"] and not prev["sc"].get("card") and not prev["sc"].get("prompt") and prev["hl"]:
                s["r0"] = prev["r1"]
        # Captions move to the top when the thing to look at sits where they would be.
        x, y, w = s["r1"]
        sy = H / (w * 9 / 16)
        s["cap_top"] = any((h["box"][3] - y) * sy > H - 130 * K * 1.35 for h in s["hl"])
        s["bluey_right"] = any((h["box"][0] - x) * (W / w) < 260 * K and (h["box"][3] - y) * sy > H - 300 * K for h in s["hl"])

    def view(self, s, t):
        u = ease((t - s["t0"]) / max(s["cam_to"] - s["t0"], 0.01))
        x, y, w = [a + (b - a) * u for a, b in zip(s["r0"], s["r1"])]
        h = w * 9 / 16
        x, y = min(max(x, 0), SRC_W - w), min(max(y, 0), SRC_H - h)
        name = s["src"] + ("~" if s["blur"] else "")
        if name not in self._img:
            if len(self._img) > 6:
                self._img.clear()
            im = Image.open(s["src"]).convert("RGB")
            if im.size != (SRC_W, SRC_H):
                im = im.resize((SRC_W, SRC_H), Image.LANCZOS)
            if s["blur"]:
                im = im.filter(ImageFilter.GaussianBlur(10))
            self._img[name] = im
        return self._img[name].resize((W, H), Image.BICUBIC, box=(x, y, x + w, y + h)), (x, y, w, h)

    # ── overlays ──────────────────────────────────────────────────────────────
    def highlight(self, frame, rect, hl, t):
        tc = hl["t"]
        if t < tc:
            return
        x, y, w, _ = rect
        sx = W / w
        sy = H / (w * 9 / 16)
        pad = 8 * K
        b = [(hl["box"][0] - x) * sx - pad, (hl["box"][1] - y) * sy - pad, (hl["box"][2] - x) * sx + pad, (hl["box"][3] - y) * sy + pad]
        k = ease((t - tc) / 0.3)
        grow = (1 - k) * 22 * K
        b = [b[0] - grow, b[1] - grow, b[2] + grow, b[3] + grow]
        lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        pulse = 0.65 + 0.35 * math.sin((t - tc) * 5)
        r = int(14 * K)
        for wdt, al in ((12, int(50 * pulse)), (7, int(90 * pulse)), (3, 255)):
            d.rounded_rectangle(b, radius=r, outline=YELLOW + (int(al * k),), width=max(1, int(wdt * K * 1.5)))
        # A bouncing arrow, so nobody has to hunt for the box.
        bob = (math.sin((t - tc) * 6) + 1) * 8 * K
        if not hl["arrow"]:
            frame.paste(lay, (0, 0), lay)
            return
        size = 20 * K
        cx = (b[0] + b[2]) / 2
        if b[3] + size * 2.6 < H - 110 * K:
            tip = b[3] + 6 * K + bob
            pts = [(cx, tip), (cx - size, tip + size * 1.3), (cx + size, tip + size * 1.3)]
        elif b[1] - size * 2.4 > 0:
            tip = b[1] - 6 * K - bob
            pts = [(cx, tip), (cx - size, tip - size * 1.3), (cx + size, tip - size * 1.3)]
        else:
            tip = b[0] - 6 * K - bob
            cy = (b[1] + b[3]) / 2
            pts = [(tip, cy), (tip - size * 1.3, cy - size), (tip - size * 1.3, cy + size)]
        d.polygon(pts, fill=YELLOW + (int(255 * k),), outline=(30, 20, 0, int(220 * k)))
        frame.paste(lay, (0, 0), lay)

    def caption(self, frame, t):
        for start, end, ln, s in self.caps:
            if start <= t < end:
                break
        else:
            return
        fnt = font("s", 50)
        d0 = ImageDraw.Draw(frame)
        space = d0.textlength(" ", font=fnt)
        widths = [d0.textlength(w[0], font=fnt) for w in ln]
        total = sum(widths) + space * (len(ln) - 1)
        pad, hgt = int(32 * K), int(82 * K)
        lay = Image.new("RGBA", (int(total + pad * 2), hgt), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        d.rounded_rectangle((0, 0, lay.width - 1, hgt - 1), radius=int(18 * K), fill=(8, 10, 24, 215))
        x = pad
        for (word, w0, w1), wid in zip(ln, widths):
            col = (255, 255, 255, 255) if t >= w1 else BLUE + (255,) if t >= w0 else (205, 210, 230, 130)
            d.text((x, hgt / 2), word, font=fnt, fill=col, anchor="lm")
            x += wid + space
        a = min(1, (t - start) / 0.12, (end - t) / 0.12)
        y = 36 * K if s["cap_top"] else H - 118 * K
        self.put(frame, lay, ((W - lay.width) / 2, y), a)

    @staticmethod
    def put(frame, layer, xy, opacity=1.0):
        if opacity <= 0:
            return
        if opacity < 1:
            layer = layer.copy()
            layer.putalpha(layer.split()[3].point(lambda v: int(v * opacity)))
        frame.paste(layer, (int(xy[0]), int(xy[1])), layer)

    def draw_bluey(self, frame, t, f, right):
        e = float(self.env[min(f, len(self.env) - 1)])
        talking = LEAD <= t < self.voice_end + 0.1
        badge = int(150 * K)
        spr = self.bluey[int(t * 9 + float(np.sum(self.env[:f])) / FPS * 10) % 8]
        m = int(24 * K)
        lay = Image.new("RGBA", (badge + m * 2, badge + m * 2 + int(20 * K)), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        cx, cy = m + badge // 2, m + badge // 2
        glow = int(70 + 120 * min(e, 1.0)) if talking else 60
        halo = Image.new("RGBA", lay.size, (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse((cx - badge // 2 - 6, cy - badge // 2 - 6, cx + badge // 2 + 6, cy + badge // 2 + 6), fill=(59, 130, 246, glow))
        lay.alpha_composite(halo.filter(ImageFilter.GaussianBlur(int(12 * K))))
        d.ellipse((cx - badge // 2, cy - badge // 2, cx + badge // 2, cy + badge // 2), fill=(14, 18, 40, 245), outline=BLUE + (255,), width=max(2, int(3 * K)))
        bob = math.sin(t * 2.4) * 3 * K - (e * 7 * K if talking else 0)
        sl = Image.new("RGBA", lay.size, (0, 0, 0, 0))
        sl.alpha_composite(spr, (cx - spr.width // 2, max(0, int(cy - spr.height // 2 + 4 * K + bob))))
        clip = Image.new("L", lay.size, 0)
        ImageDraw.Draw(clip).ellipse((cx - badge // 2 + 3, cy - badge // 2 + 3, cx + badge // 2 - 3, cy + badge // 2 - 3), fill=255)
        sl.putalpha(Image.composite(sl.split()[3], Image.new("L", lay.size, 0), clip))
        lay.alpha_composite(sl)
        x = W - lay.width - 10 * K if right else 10 * K
        self.put(frame, lay, (x, H - lay.height - 8 * K))

    def card(self, frame, s, t):
        c = s["sc"]["card"]
        words = s["words"]
        cw = int(1180 * K)
        rows = c["rows"]
        row_h = int(116 * K)
        red = c.get("tone") == "red"
        hd = int(120 * K)
        ch = hd + row_h * len(rows) + int(36 * K)
        lay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        edge = RED if red else BLUE
        d.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=int(28 * K), fill=(10, 14, 30, 244), outline=edge + (200,), width=max(2, int(3 * K)))
        d.text((int(50 * K), int(62 * K)), c["title"], font=font("px", 28), fill=(255, 255, 255, 255), anchor="lm")
        y = hd
        for i, r in enumerate(rows):
            tc = self.cue(words, r.get("at"), s["t0"], s["sc"])
            e = ease((t - tc + 0.1) / 0.25)
            rtone = RED if (r.get("tone") == "red" or red) else GREEN
            d.rounded_rectangle((int(36 * K), y, cw - int(36 * K), y + row_h - int(14 * K)), radius=int(18 * K),
                                fill=rtone + (int(34 * e),), outline=rtone + (int(40 + 170 * e),), width=max(1, int(2 * K)))
            bx = int(84 * K)
            by = y + (row_h - int(14 * K)) // 2
            rad = int(30 * K)
            d.ellipse((bx - rad, by - rad, bx + rad, by + rad), fill=rtone + (int(70 + 185 * e),))
            d.text((bx, by), "!" if (r.get("tone") == "red" or red) else str(i + 1), font=font("b", 36), fill=(8, 10, 24, 255), anchor="mm")
            d.text((int(138 * K), by), r["text"], font=font("s", 44), fill=(255, 255, 255, int(110 + 145 * e)), anchor="lm")
            y += row_h
        a = min(1, (t - s["t0"] + XF) / XF, (s["t1"] + XF - t) / XF)
        self.put(frame, lay, ((W - cw) / 2, (H - 100 * K - ch) / 2 + (1 - ease(a)) * 24 * K), a)

    def prompt(self, frame, s, t):
        p = s["sc"]["prompt"]
        cw, ch = int(1240 * K), int(430 * K)
        lay = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        d.rounded_rectangle((0, 0, cw - 1, ch - 1), radius=int(28 * K), fill=(10, 14, 30, 244), outline=BLUE + (200,), width=max(2, int(3 * K)))
        d.text((int(50 * K), int(62 * K)), p["title"], font=font("px", 26), fill=(255, 255, 255, 255), anchor="lm")
        d.rounded_rectangle((int(36 * K), int(116 * K), cw - int(36 * K), ch - int(36 * K)), radius=int(20 * K), fill=(28, 34, 58, 255), outline=(255, 255, 255, 60), width=max(1, int(2 * K)))
        ts = self.cue(s["words"], p.get("from"), s["t0"], s["sc"])
        te = self.cue(s["words"], p.get("to"), s["t0"], s["sc"])
        n = int(len(p["text"]) * min(1, max(0, (t - ts) / max(te - ts, 0.1))))
        fnt = font("m", 46)
        y, line = int(150 * K), ""
        for wd in p["text"][:n].split(" "):
            if line and d.textlength(line + wd, font=fnt) > cw - int(150 * K):
                d.text((int(72 * K), y), line, font=fnt, fill=(255, 255, 255, 255))
                y += int(62 * K)
                line = ""
            line += wd + " "
        d.text((int(72 * K), y), line.rstrip() + ("|" if int(t * 2.5) % 2 else ""), font=fnt, fill=(255, 255, 255, 255))
        a = min(1, (t - s["t0"] + XF) / XF, (s["t1"] + XF - t) / XF)
        self.put(frame, lay, ((W - cw) / 2, (H - 100 * K - ch) / 2), a)

    def title(self, frame, t):
        """The opening card over the first screen."""
        a = min(1, t / 0.3, (LEAD + 0.25 - t) / 0.35)
        if a <= 0:
            return frame
        frame = Image.blend(frame, self.black, 0.9 * ease(a))
        lay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(lay)
        d.text((W / 2, H * 0.34), "KRYPT POLYBOT US", font=font("px", 30), fill=BLUE + (255,), anchor="mm")
        title = self.v["title"]
        size = 84
        while d.textlength(title, font=font("b", size)) > W * 0.88 and size > 40:
            size -= 4
        d.text((W / 2, H * 0.49), title, font=font("b", size), fill=(255, 255, 255, 255), anchor="mm")
        d.text((W / 2, H * 0.62), self.v["blurb"], font=font("s", 42), fill=(205, 215, 240, 255), anchor="mm")
        self.put(frame, lay, (0, 0), a)
        return frame

    # ── a frame ───────────────────────────────────────────────────────────────
    def scene_at(self, t):
        k = 0
        for i, s in enumerate(self.scenes):
            if s["t0"] <= t:
                k = i
        return k

    def frame(self, f):
        t = f / FPS
        k = self.scene_at(t)
        s = self.scenes[k]
        img, rect = self.view(s, t)
        if k > 0 and t < s["t0"] + XF:
            prev_img, _ = self.view(self.scenes[k - 1], t)
            img = Image.blend(prev_img, img, ease((t - s["t0"]) / XF))
        if t < LEAD + 0.3:
            img = self.title(img, t)
        else:
            img = Image.composite(self.black, img, self.vig)
        newest = max((h["t"] for h in s["hl"] if h["t"] <= t), default=None)
        for h in s["hl"]:
            h["arrow"] = h["t"] == newest  # only the thing just mentioned gets the arrow
            self.highlight(img, rect, h, t)
        if s["sc"].get("card"):
            self.card(img, s, t)
        if s["sc"].get("prompt"):
            self.prompt(img, s, t)
        if t >= LEAD:
            self.draw_bluey(img, t, f, s["bluey_right"])
            self.caption(img, t)
        fade = min(1, (self.total - t) / 0.5)
        if fade < 1:
            img = Image.blend(self.black, img, max(0.0, fade))
        return img


def read_wav(raw):
    """Studio's analyzer writes 32-bit float mono WAV; the wave module won't read float."""
    pos = 12
    fmt = None
    while pos + 8 <= len(raw):
        cid = raw[pos:pos + 4]
        size = struct.unpack("<I", raw[pos + 4:pos + 8])[0]
        body = raw[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            if fmt[0] == 3:
                return np.frombuffer(body, dtype=np.float32).copy()
            return np.frombuffer(body, dtype=np.int16).astype(np.float32) / 32768
        pos += 8 + size + (size & 1)
    raise ValueError("no audio in that wav")


def render(video):
    v = Video(video)
    os.makedirs(P("out"), exist_ok=True)
    tmp = P("out", f"{v.vid}.voice.f32")
    v.pcm.astype(np.float32).tofile(tmp)
    out = P("out", f"{v.vid}.mp4")
    ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                           "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", tmp,
                           "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "44100",
                           "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "25", "-pix_fmt", "yuv420p", "-threads", "2",
                           "-movflags", "+faststart", "-c:a", "aac", "-b:a", "96k", "-ac", "1", "-shortest", out],
                          stdin=subprocess.PIPE)
    for f in range(v.nframes):
        ff.stdin.write(v.frame(f).tobytes())
    ff.stdin.close()
    if ff.wait() != 0:
        raise RuntimeError(f"{v.vid}: ffmpeg failed")
    os.remove(tmp)
    v.frame(int(LEAD * 0.55 * FPS)).resize((640, 360), Image.LANCZOS).save(P("out", f"{v.vid}.jpg"), quality=82)
    size = os.path.getsize(out) / 1e6
    print(f"{v.vid}: {v.total:.0f}s, {size:.1f} MB", flush=True)
    return v.vid, v.total


def stills(video):
    v = Video(video)
    folder = P("out", "stills", v.vid)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    v.frame(int(LEAD * 0.5 * FPS)).save(os.path.join(folder, "00-title.jpg"), quality=85)
    for i, s in enumerate(v.scenes):
        cues = [h["t"] for h in s["hl"]]
        t = max(cues) + 0.4 if cues else s["t1"] - GAP - 0.2
        t = min(t, s["t1"] - 0.05)
        v.frame(int(t * FPS)).save(os.path.join(folder, f"{i + 1:02d}-{s['sc']['id']}.jpg"), quality=85)
    return v.vid


def install():
    videos = json.load(open(P("videos.json"), encoding="utf-8"))
    dest = P("..", "..", "src", "assets", "guide")
    os.makedirs(dest, exist_ok=True)
    rows = []
    for video in videos:
        src = P("out", f"{video['id']}.mp4")
        if not os.path.exists(src):
            print("not rendered:", video["id"])
            continue
        shutil.copy(src, os.path.join(dest, f"{video['id']}.mp4"))
        shutil.copy(P("out", f"{video['id']}.jpg"), os.path.join(dest, f"{video['id']}.jpg"))
        secs = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", src],
                                    capture_output=True, text=True, check=True).stdout)
        rows.append({"id": video["id"], "title": video["title"], "blurb": video["blurb"], "seconds": round(secs)})
    json.dump(rows, open(os.path.join(dest, "videos.json"), "w", encoding="utf-8"), indent=2)
    print(len(rows), "videos installed")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--install" in sys.argv:
        install()
        sys.exit(0)
    all_videos = json.load(open(P("videos.json"), encoding="utf-8"))
    todo = [v for v in all_videos if not args or v["id"] in args]
    job = stills if "--stills" in sys.argv else render
    with mp.Pool(min(len(todo), max(1, (os.cpu_count() or 2) // 2))) as pool:
        for r in pool.imap_unordered(job, todo):
            pass
