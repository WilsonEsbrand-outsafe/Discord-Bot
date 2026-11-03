"""3.7.3 원석 그림 (assets/gems/*.png) 만들기 — python tools/make_gems.py"""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets" / "gems"
S = 512          # 결과 크기
K = 2            # 계단 줄이려고 크게 그려 줄인다
W = S * K

# key → (밝은 색, 기본 색, 어두운 색)
COLORS = {
    "emerald":  ((140, 255, 180), (20, 170, 90), (5, 70, 40)),
    "sapphire": ((150, 200, 255), (30, 90, 220), (10, 30, 110)),
    "ruby":     ((255, 150, 170), (215, 20, 60), (100, 5, 25)),
    "diamond":  ((255, 255, 255), (190, 230, 255), (110, 160, 210)),
}


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def glow(color, r):
    g = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    c = W // 2
    d.ellipse((c - r, c - r, c + r, c + r), fill=color + (110,))
    return g.filter(ImageFilter.GaussianBlur(W // 14))


def cut_gem(key):
    hi, mid, lo = COLORS[key]
    img = glow(mid, int(W * 0.36))
    d = ImageDraw.Draw(img)
    cx, cy = W / 2, W * 0.47
    R = W * 0.36               # 거들(가장 넓은 곳) 반지름
    top_y = cy - W * 0.17      # 테이블 높이
    tab = R * 0.55             # 테이블 반폭
    tip = (cx, cy + W * 0.36)  # 아래 꼭짓점
    n = 8
    # 거들 점 (위에서 본 타원을 옆으로 납작하게)
    girdle = [(cx - R + 2 * R * i / n, cy) for i in range(n + 1)]
    table = [(cx - tab + 2 * tab * i / (n // 2), top_y) for i in range(n // 2 + 1)]
    # 크라운 (위쪽 면)
    for i in range(n):
        a, b = girdle[i], girdle[i + 1]
        t = table[min(i // 2, n // 2 - 1) + (1 if i % 2 else 0)] if False else None
        j = i // 2
        ta, tb = table[j], table[j + 1]
        if i % 2 == 0:
            poly = [a, b, ta]
        else:
            poly = [a, b, tb]
        shade = 0.25 + 0.5 * abs(i - n / 2 + 0.5) / (n / 2)
        d.polygon(poly, fill=mix(hi, mid, shade))
    for j in range(n // 2):
        ta, tb = table[j], table[j + 1]
        gm = girdle[2 * j + 1]
        d.polygon([ta, tb, gm], fill=mix(hi, mid, 0.15 + 0.15 * (j % 2)))
    # 테이블 윗면 (얇은 띠)
    d.polygon([table[0], table[-1], (cx + tab * 0.9, top_y - W * 0.012), (cx - tab * 0.9, top_y - W * 0.012)], fill=hi)
    # 파빌리온 (아래쪽 면)
    for i in range(n):
        a, b = girdle[i], girdle[i + 1]
        t = abs((i + 0.5) - n / 2) / (n / 2)
        d.polygon([a, b, tip], fill=mix(mid, lo, 0.25 + 0.6 * t if i < n / 2 else 0.1 + 0.5 * t))
    # 모서리 선
    edge = mix(hi, (255, 255, 255), 0.5) + (150,)
    for i in range(n + 1):
        d.line([girdle[i], tip], fill=edge, width=K * 2)
    d.line([girdle[0], girdle[-1]], fill=edge, width=K * 3)
    for p in table:
        pass
    d.line([girdle[0], table[0], table[-1], girdle[-1]], fill=edge, width=K * 3)
    d.line([girdle[0], tip, girdle[-1]], fill=edge, width=K * 3)
    sparkle(img, [(cx - tab * 0.4, top_y + W * 0.05), (cx + R * 0.55, cy + W * 0.08)], W * 0.06)
    return img


def sparkle(img, points, size):
    d = ImageDraw.Draw(img)
    for x, y in points:
        s = size
        d.polygon([(x, y - s), (x + s * 0.18, y - s * 0.18), (x + s, y), (x + s * 0.18, y + s * 0.18),
                   (x, y + s), (x - s * 0.18, y + s * 0.18), (x - s, y), (x - s * 0.18, y - s * 0.18)],
                  fill=(255, 255, 255, 235))


def rough():
    rng = random.Random(7)
    img = glow((150, 140, 130), int(W * 0.34))
    d = ImageDraw.Draw(img)
    cx, cy = W / 2, W / 2
    pts = []
    for i in range(11):
        a = 2 * math.pi * i / 11 + rng.uniform(-0.12, 0.12)
        r = W * rng.uniform(0.30, 0.38)
        pts.append((cx + r * math.cos(a), cy + r * math.sin(a) * 0.85))
    d.polygon(pts, fill=(110, 100, 92))
    # 면 나누기
    c = (cx - W * 0.04, cy - W * 0.05)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        ang = math.atan2((a[1] + b[1]) / 2 - cy, (a[0] + b[0]) / 2 - cx)
        light = 0.5 + 0.5 * math.cos(ang + 2.3)
        col = mix((70, 62, 56), (175, 165, 150), light)
        d.polygon([a, b, c], fill=col)
        d.line([a, c], fill=(60, 54, 48), width=K * 2)
    d.line(pts + [pts[0]], fill=(55, 48, 42), width=K * 3)
    # 안에 비치는 결정
    for (x, y, s) in [(cx + W * 0.06, cy + W * 0.02, W * 0.07), (cx - W * 0.12, cy + W * 0.1, W * 0.045)]:
        d.polygon([(x, y - s), (x + s * 0.7, y), (x, y + s), (x - s * 0.7, y)], fill=(170, 225, 235))
        d.polygon([(x, y - s), (x + s * 0.7, y), (x, y)], fill=(225, 250, 255))
    sparkle(img, [(cx + W * 0.06, cy - W * 0.03)], W * 0.04)
    return img


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for key in ("rough", "emerald", "sapphire", "ruby", "diamond"):
        img = rough() if key == "rough" else cut_gem(key)
        img.resize((S, S), Image.LANCZOS).save(OUT / f"{key}.png", optimize=True)
        print(OUT / f"{key}.png")


if __name__ == "__main__":
    main()
