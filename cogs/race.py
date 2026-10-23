# cogs/race.py — 레이스 미니게임: 🐌 달팽이 · 🚣 조정 · 🏎️ F1
# 달팽이는 경마와 같은 틀 — 출전자 정보는 분위기용, 순위는 완전 랜덤, 상금표 합 0 (기대값 0).
# 조정은 내가 직접 노를 젓는다 — 구간마다 [노 젓기] 연타 수만큼 빨라지고, 물살(운)이 나머지 반.
#   끝까지 열심히 저으면 상대와 똑같은 조건(기대값 0), 덜 저을수록 불리하다. 목표 이상 연타는 더 빨라지지 않는다(매크로 방지).
# F1 은 2026 실제 드라이버 · 팀 · 서킷. 머신 · 드라이버 전력, 예선 그리드, 서킷 성격(추월 난이도 · 세이프티카 · 비)이
#   결과에 반영되고, 배당은 드라이버마다 시뮬레이션으로 재서 기대값이 0 밑이 되게 정한다.
#   중계는 실제 서킷 모양 위를 차들이 도는 그림 + 옆에 순위표 (서킷 좌표: assets/f1_circuits.json, MIT · 팀 로고: assets/teams, 커먼스).
# 베팅금은 출전표를 띄울 때 먼저 빠진다 — 60초 안에 안 고르면(안 출발하면) 잃는다 (경마와 같음).
import asyncio
import io
import json
import math
import random
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import matplotlib.patheffects as pe
from PIL import Image

from services.economy_db import EconomyDB
from services import ui

MIN_BET = 1_000
TRACK = 12
STAGES = (0.25, 0.5, 0.75, 0.88, 1.0)
COMEBACK_PROB = 0.45     # 막판까지 앞서던 쪽이 결승선 앞에서 뒤집히는 연출 비율 (결과 확률과는 무관)
MOODS = ("😆 최상", "🙂 좋음", "😐 보통", "😫 나쁨")
PLACES = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣")


def label(mult) -> str:
    """0.38 → '0.38배 수익', 0 → '본전', -2 → '2배 손실'."""
    if mult > 0:
        return f"{mult:g}배 수익"
    return "본전" if mult == 0 else f"{-mult:g}배 손실"


def prizes(n: int, rng=random) -> list[int]:
    """순위별 순이익 배수 (경마와 같은 규칙). 1위 > 2위 > 0, 꼴찌 쪽 손실, 합 = 0.
    4명: [+, +, -, -] · 6명: [+, +, 0, -, -, -]."""
    s = rng.choices(range(3, 9), weights=(25, 25, 18, 14, 10, 8))[0]
    top = rng.randint(s // 2 + 1, s - 1)
    k = n // 2                                         # 손실 칸 수
    cuts = sorted(rng.sample(range(1, s), k - 1))
    loss = sorted(b - a for a, b in zip([0] + cuts, cuts + [s]))
    return [top, s - top] + [0] * (n - 2 - k) + [-x for x in loss]


def prize_lines(amount: int, table: list[int]) -> str:
    return "\n".join(f"{PLACES[i]} {i + 1}위 **{ui.won(amount * m)}** · {label(m)}" for i, m in enumerate(table))


def frames(finish: list[int], rng=random, comeback: int | None = None) -> list[list[int]]:
    """장면마다 위치(출전 번호순). 초반엔 뒤섞이고 갈수록 최종 순위대로 벌어진다 — 경마와 같은 연출.
    comeback = 막판까지 선두로 보이다 뒤집히는 쪽. 마지막 장면은 결승선: 1위가 🏁, 나머지는 한 칸씩 뒤."""
    n = len(finish)
    rank = {h: r for r, h in enumerate(finish)}
    mid, spread = (n - 1) / 2, 4.2 / max(n - 1, 1)    # 4명일 때 경마와 같은 벌어짐
    out = []
    for f in STAGES[:-1]:
        row = []
        for i in range(n):
            bias = (mid - rank[i]) * f * spread
            if comeback is not None and i == comeback:
                bias = 3.0 * f / 0.88                  # 가짜 선두: 3 · 4번째 장면까지 확실히 앞선다
            elif comeback is not None and i == finish[0]:
                bias = -0.6 * f                        # 진짜 1위: 뒤에서 힘을 아낀다
            row.append(max(0, min(TRACK - 1, round(f * (TRACK - 3) + bias + rng.uniform(-1.5, 1.5) * (1 - f)))))
        out.append(row)
    out.append([TRACK - rank[i] for i in range(n)])
    return out


def lanes(runners, pos: list[int], pick: int) -> str:
    return "\n".join(f"`{i + 1}` {r[0]} {ui.bar(p, TRACK, TRACK)}🏁 " + (f"**{r[1]}** 👈" if i == pick else r[1])
                     for i, (r, p) in enumerate(zip(runners, pos)))


# ───────────── 🐌 달팽이 (경마 틀) ─────────────

class Snail:
    """출전표 → 버튼으로 하나 고르기 → 5장면 중계 → 결과. 순위는 완전 랜덤."""
    icon, noun, section, n = "🐌", "달팽이", "🎙️ 달팽이 레이스 중계", 4
    ready = "출발선에 섰습니다… 아마도요."
    POOL = (   # (이모지, 이름, 점액량 %, 소개) — 표시용
        ("🥬", "상추도둑", 88, "밭 하나를 통째로 먹어 치운 전과가 있어요"),
        ("🚀", "터보달팽이", 95, "이름만 터보입니다"),
        ("🏠", "집순이", 61, "껍질 밖으로 잘 안 나와요"),
        ("💧", "새벽이슬", 79, "아침 경주에 유독 강함"),
        ("🧂", "소금기피", 70, "관중석에 소금이 보이면 멈춥니다"),
        ("👑", "왕달팽이", 91, "지난 대회 우승… 이라고 본인이 주장"),
        ("🍼", "아기달팽이", 54, "생후 3일, 첫 출전"),
        ("🌌", "우주달팽이", 83, "가끔 하늘을 보느라 멈춰요"),
        ("🎨", "껍질장인", 66, "껍질 무늬가 예술입니다"),
        ("🌧️", "비오는날", 97, "비만 오면 날아다닙니다"),
        ("😴", "낮잠왕", 58, "경주 중 수면 기록 보유"),
        ("🍓", "딸기우유", 74, "간식을 주면 빨라진대요"),
        ("🎢", "미끄럼틀", 99, "점액 양만큼은 리그 최고"),
        ("🐢", "거북이친구", 63, "거북이랑 같이 훈련했습니다"),
        ("🔥", "열정달팽", 86, "의지만큼은 치타급"),
        ("🍄", "버섯모자", 69, "버섯을 모자처럼 쓰고 다녀요"),
    )
    calls = (   # 장면별 (소제목, 멘트) — {0}{1} = 그 장면 1 · 2등
        ("출발…?", "출발했습니다! …아마도요. {0}, 1mm 앞서갑니다!"),
        ("10분 경과", "10분 경과! {0} 선두, {1} 0.5cm 차이로 추격!"),
        ("중간 지점", "중간 지점! {0}, {1}! 손에 땀을 쥐는… 아주 느린 접전!"),
        ("마지막 상추 구간", "마지막 상추 구간!! {0}, 이대로 들어가나요?!"),
        ("결승선", "{0}, 결승선 통과!! (1시간 12분 걸렸습니다)"),
    )
    comeback_call = "아아— {0}!!! 상추 냄새를 맡았나요?! {1}를 제치고 대역전!!!"
    results = (   # 순위별 (이모지, 헤드라인, 멘트)
        ("🏆", "우승!!", "끈적한 승리! 탁월한 안목이에요!"),
        ("🥈", "2위!", "아깝게 2위! 그래도 상금은 챙겨 갑니다."),
        ("🥉", "3위…", "3위… 상추 한 장 차이였어요."),
        ("🐌", "꼴찌…", "…아직도 출발선 근처에 있습니다."),
    )

    def runners(self, rng):
        """이번 경주 출전자 [(이모지, 이름, 정보 한 줄, 소개)]."""
        return [(e, name, f"점액 {slime}% · {rng.choice(MOODS + ('💤 졸림',))}", intro)
                for e, name, slime, intro in rng.sample(self.POOL, self.n)]

    def twist(self, fr, finish, comeback, names, rng) -> dict[int, str]:
        """연출용 돌발 상황 — 장면 위치를 살짝 바꾸고 그 장면의 멘트를 돌려준다. 결승선 장면(순위)은 건드리지 않는다."""
        others = [i for i in range(len(finish)) if i not in (finish[0], comeback)]
        r = rng.random()
        if r < 0.3:      # 낮잠: 두 장면 동안 제자리 — 1위가 자면 깨어나서 우승
            s = finish[0] if comeback is None and rng.random() < 0.5 else rng.choice(others)
            fr[1][s] = fr[2][s] = fr[0][s]
            calls = {2: f"어… {names[s]}, 움직이지 않습니다. 💤 잠들었어요!!"}
            if s == finish[0]:
                calls[4] = f"{names[s]}!!! 낮잠에서 깨어나 그대로 결승선 통과!!!"
            return calls
        if r < 0.55:     # 역주행
            s = rng.choice(others)
            fr[3][s] = max(0, fr[2][s] - 2)
            return {3: f"{names[s]}, 갑자기 뒤로 돌아갑니다?! 역주행!! 관중석이 술렁입니다!"}
        return {}


SNAIL = Snail()


# ───────────── 🚣 조정 (직접 노 젓기) ─────────────
ROWERS = (   # (국기, 이름, 체중 kg, 소개) — 표시용
    ("🇬🇧", "올리버 템스", 88, "템스강에서 자랐습니다"),
    ("🇩🇪", "한스 라이너", 92, "라인강의 기관차"),
    ("🇳🇿", "케이트 와이카토", 74, "올림픽 금메달 꿈나무"),
    ("🇳🇱", "다안 흐라흐트", 90, "운하 출퇴근 15년"),
    ("🇮🇹", "마르코 베네치아", 85, "곤돌라 집안 출신"),
    ("🇦🇺", "잭 머레이", 89, "스타트가 리그 최고"),
    ("🇺🇸", "브래드 찰스", 94, "하버드 조정부 주장"),
    ("🇨🇦", "노아 로렌스", 87, "추운 날 유독 강함"),
    ("🇳🇴", "올라 피오르", 91, "피오르에서 매일 훈련"),
    ("🇫🇷", "루이 센느", 83, "막판 스퍼트의 귀재"),
    ("🇰🇷", "한강철", 86, "한강 새벽 훈련 10년차"),
    ("🇯🇵", "스미다 타로", 80, "리듬감 하나는 완벽"),
    ("🇨🇳", "리 창장", 88, "지구력 괴물"),
    ("🇭🇷", "이반 아드리아", 93, "형제 조정 선수 중 동생"),
    ("🇧🇷", "카이오 아마존", 84, "물살 읽는 눈이 좋아요"),
    ("🇪🇸", "파블로 에브로", 82, "오늘 컨디션은 본인도 몰라요"),
)
ROW_N = 6                 # 레인 수 (나 + 상대 5)
ROW_SEGS = ("500m", "1000m", "1500m", "라스트 스퍼트", "결승선 앞")
ROW_SEC = 5.0             # 구간 하나 길이 (초) — 2.8: 3 → 4 · 2.82: 4 → 5
ROW_TARGET = 3            # 구간당 이만큼 저으면 전속력 (그 이상은 같음) — 2.8: 6 → 4 · 2.82: 4 → 3
ROW_NOISE = 0.5           # 구간마다 물살 운 (±)
ROW_LAZY = 0.9            # 하나도 안 저었을 때 속도 (전속력 = 1) — 2.8: 0.75 → 0.85 · 2.82: 0.85 → 0.9


def row_rules(guild_id) -> tuple[float, int, float]:
    """(구간 초, 전속력 횟수, 최저 속도)."""
    return ROW_SEC, ROW_TARGET, ROW_LAZY


def row_step(dist: list[float], me: int, taps: int, rng=random, target: int = ROW_TARGET, lazy: float = ROW_LAZY) -> float:
    """한 구간 진행. 상대는 1 ± 물살, 나는 (최저 + (1 - 최저) × 젓기 비율) ± 물살 — 전속력이면 상대와 똑같은 분포.
    덜 저을수록 불리하지만 물살 운으로 뒤집을 여지는 남는다 (반은 운). 젓기 비율을 돌려준다."""
    e = min(1.0, taps / target)
    for i in range(len(dist)):
        dist[i] += (lazy + (1 - lazy) * e if i == me else 1.0) + rng.uniform(-ROW_NOISE, ROW_NOISE)
    return e


def row_cells(dist: list[float]) -> list[int]:
    """진행 거리 → 게이지 칸 (전체 구간 기준)."""
    full = len(ROW_SEGS) + 0.6
    return [max(0, min(TRACK - 1, round(d / full * (TRACK - 1)))) for d in dist]


def row_call(me_name: str, e: float, leader: str, me_lead: bool) -> str:
    if e >= 0.99:
        s = f"{me_name}, 스트로크가 폭발합니다!! 쭉쭉 나갑니다!"
    elif e >= 0.5:
        s = f"{me_name}, 리듬을 타고 있어요! 조금만 더!"
    elif e > 0:
        s = f"{me_name}, 힘이 빠지나요? 속도가 떨어집니다!"
    else:
        s = f"{me_name}… 노를 놓았나요?! 보트가 멈춰 섭니다!"
    return s + (" 지금 선두입니다!!" if me_lead else f" 선두는 {leader}!")


# ───────────── 🏎️ F1 (2026 실제 그리드) ─────────────
# 전력 수치는 게임용 추정치 — 시즌 흐름에 맞춰 여기만 고치면 배당까지 따라 바뀐다.
TEAMS = {   # 키: (이름, 이모지 색, 그림 색, 머신 전력)
    "MER": ("메르세데스", "⬜", "#27F4D2", 95),
    "MCL": ("맥라렌", "🟧", "#FF8000", 94),
    "FER": ("페라리", "🟥", "#E8002D", 92),
    "RBR": ("레드불", "🟦", "#3671C6", 90),
    "WIL": ("윌리엄스", "🔷", "#64C4FF", 84),
    "ALP": ("알핀", "🟪", "#FF87BC", 83),
    "HAA": ("하스", "⚪", "#E6E6E6", 83),
    "RB": ("레이싱 불스", "🔵", "#6692FF", 82),
    "AUD": ("아우디", "⬛", "#9AA0A6", 81),
    "AMR": ("애스턴 마틴", "🟩", "#229971", 80),
    "CAD": ("캐딜락", "⚫", "#D4AF37", 76),
}
DRIVERS = (   # (코드, 이름, 국기, 팀, 실력)
    ("RUS", "러셀", "🇬🇧", "MER", 93), ("ANT", "안토넬리", "🇮🇹", "MER", 88),
    ("NOR", "노리스", "🇬🇧", "MCL", 95), ("PIA", "피아스트리", "🇦🇺", "MCL", 93),
    ("LEC", "르클레르", "🇲🇨", "FER", 94), ("HAM", "해밀턴", "🇬🇧", "FER", 90),
    ("VER", "베르스타펜", "🇳🇱", "RBR", 98), ("HAD", "하자르", "🇫🇷", "RBR", 85),
    ("SAI", "사인츠", "🇪🇸", "WIL", 89), ("ALB", "알본", "🇹🇭", "WIL", 87),
    ("GAS", "가슬리", "🇫🇷", "ALP", 86), ("COL", "콜라핀토", "🇦🇷", "ALP", 79),
    ("OCO", "오콘", "🇫🇷", "HAA", 84), ("BEA", "베어먼", "🇬🇧", "HAA", 84),
    ("LAW", "로슨", "🇳🇿", "RB", 82), ("LIN", "린드블라드", "🇬🇧", "RB", 79),
    ("HUL", "휠켄베르크", "🇩🇪", "AUD", 84), ("BOR", "보르톨레토", "🇧🇷", "AUD", 82),
    ("ALO", "알론소", "🇪🇸", "AMR", 90), ("STR", "스트롤", "🇨🇦", "AMR", 78),
    ("PER", "페레스", "🇲🇽", "CAD", 83), ("BOT", "보타스", "🇫🇮", "CAD", 83),
)
CIRCUITS = (   # (국기, 이름, 랩, 그리드 영향 0~1, 세이프티카 %, 비 %, 시가지, 한 줄) — 이름 = f1_circuits.json 키
    ("🇦🇺", "호주 GP · 앨버트 파크", 58, 0.6, 50, 15, True, "시즌 개막전, 세이프티카 단골"),
    ("🇨🇳", "중국 GP · 상하이", 56, 0.5, 30, 20, False, "긴 백스트레치, 타이어 관리 싸움"),
    ("🇯🇵", "일본 GP · 스즈카", 53, 0.75, 25, 25, False, "8자 레이아웃의 고속 S자, 추월이 어렵다"),
    ("🇧🇭", "바레인 GP · 사키르", 57, 0.45, 25, 0, False, "사막의 야간 경주, 타이어 마모"),
    ("🇸🇦", "사우디 GP · 제다", 50, 0.55, 55, 0, True, "가장 빠른 시가지 서킷, 벽이 가깝다"),
    ("🇺🇸", "마이애미 GP", 57, 0.55, 40, 20, True, "하드록 스타디움을 도는 시가지"),
    ("🇨🇦", "캐나다 GP · 질 빌뇌브", 70, 0.5, 55, 25, False, "챔피언의 벽, 이변의 서킷"),
    ("🇲🇨", "모나코 GP", 78, 1.0, 50, 10, True, "추월 거의 불가, 예선이 전부"),
    ("🇪🇸", "스페인 GP · 바르셀로나", 66, 0.65, 20, 10, False, "머신 성능이 그대로 드러나는 서킷"),
    ("🇦🇹", "오스트리아 GP · 레드불 링", 71, 0.45, 30, 25, False, "짧은 랩, 오르막 직선"),
    ("🇬🇧", "영국 GP · 실버스톤", 52, 0.5, 35, 35, False, "F1의 고향, 변덕스러운 날씨"),
    ("🇧🇪", "벨기에 GP · 스파", 44, 0.4, 40, 45, False, "오 루즈, 한쪽에만 비가 오는 서킷"),
    ("🇭🇺", "헝가리 GP · 헝가로링", 70, 0.8, 25, 15, False, "벽 없는 모나코, 추월이 어렵다"),
    ("🇳🇱", "네덜란드 GP · 잔드보르트", 72, 0.8, 35, 30, False, "뱅크 코너, 오렌지 군단의 함성"),
    ("🇮🇹", "이탈리아 GP · 몬차", 53, 0.35, 30, 15, False, "속도의 신전, 슬립스트림 싸움"),
    ("🇪🇸", "마드리드 GP · 마드링", 57, 0.6, 45, 10, True, "2026 새 서킷, 절반은 시가지"),
    ("🇦🇿", "아제르바이잔 GP · 바쿠", 51, 0.45, 60, 5, True, "구시가지 성벽, 2km 직선"),
    ("🇸🇬", "싱가포르 GP · 마리나 베이", 62, 0.75, 75, 25, True, "야간 시가지, 세이프티카 확률 최고"),
    ("🇺🇸", "미국 GP · COTA", 56, 0.5, 30, 10, False, "1코너 오르막, 텍사스"),
    ("🇲🇽", "멕시코시티 GP", 71, 0.55, 35, 10, False, "해발 2,200m, 공기가 희박하다"),
    ("🇧🇷", "브라질 GP · 인테를라고스", 71, 0.4, 50, 40, False, "이변의 성지, 갑자기 쏟아지는 비"),
    ("🇺🇸", "라스베이거스 GP", 50, 0.45, 45, 0, True, "스트립 위 야간 레이스, 차가운 노면"),
    ("🇶🇦", "카타르 GP · 루사일", 57, 0.55, 30, 0, False, "야간 고속 서킷, 체력 싸움"),
    ("🇦🇪", "아부다비 GP · 야스 마리나", 58, 0.6, 25, 0, False, "황혼에 끝나는 시즌 피날레"),
)
ASSETS = Path(__file__).resolve().parent.parent / "assets"
TRACKS = json.loads((ASSETS / "f1_circuits.json").read_text(encoding="utf-8"))["circuits"]
LOGO_DIR, EMOJI_DIR = ASSETS / "teams" / "dark", ASSETS / "teams" / "emoji"   # 다크 화면용 로고 · 이모지용 정사각 (원본: assets/teams)
def _load_logo(path: Path, height: int = 64):
    """그릴 크기 근처로 미리 줄여 둔다 — 큰 그림을 매번 줄이면 느리다."""
    im = Image.open(path).convert("RGBA")
    im = im.crop(im.getbbox())
    im = im.resize((max(1, round(im.width * height / im.height)), height), Image.LANCZOS)
    return np.asarray(im)


TEAM_LOGO = {k: _load_logo(LOGO_DIR / f"{k}.png") for k in TEAMS if (LOGO_DIR / f"{k}.png").exists()}
TEAM_ICON = {k: _load_logo(EMOJI_DIR / f"{k}.png") for k in TEAM_LOGO}   # 엠블럼만 — 순위표 작은 칸용
TEAM_TAG: dict[str, str] = {}   # 로고 파일이 없는 팀은 여기 이름(없으면 팀 이름)을 팀 색 글자로 — 지금은 11팀 모두 로고
STRENGTH = [0.65 * TEAMS[d[3]][3] + 0.35 * d[4] for d in DRIVERS]
S_MAX = max(STRENGTH)
F1_TEMP = 2.4        # 예선: 전력 차이가 그리드에 미치는 정도 (작을수록 강팀 독식)
# 결승은 구간마다 랩 수만큼 시간을 쌓아 가며 실제로 굴린다 (초, 작을수록 앞) — 중계 장면의 순위 · 간격이 그대로 결과가 된다.
F1_CHECKS = (0.016, 0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0)   # 중계 장면 = 레이스 진행률
PACE_A = 0.05        # 전력 1 차이 = 랩당 0.05초 (비 오면 0.7배 — 머신 차이가 줄어든다)
LAP_SIGMA = 0.4      # 랩당 운 (초) — 비 오면 1.8배
GRID_SEC = 0.35      # 그리드 한 칸 = (0.35 + 서킷 추월 난이도) 초
STUCK = (0.25, 0.65)  # 바로 앞차를 근소하게 따라잡으면 막힐 확률 = 0.25 + 0.65 × 추월 난이도 (모나코 0.9 · 몬차 0.48)
PIT_LOSS = 21.0      # 피트스톱 한 번 (초)
TYRE_PACE = {"S": -0.35, "M": 0.0, "H": 0.25, "I": 0.0}     # 랩당 (초)
TYRE_COLOR = {"S": "#E8002D", "M": "#FFD12E", "H": "#F0F0F0", "I": "#39B54A"}
LAP1_CRASH = 0.2     # 1랩 사고 (+시가지 0.15, +비 0.1)
SLOW_PIT = 0.07      # 피트스톱 실수
CRASH_P = 0.0025     # 구간마다 차 한 대가 사고 낼 확률 (+비 0.004) — 터지면 60%는 세이프티카
PENALTY_P = 0.08     # 구간마다 누군가 페널티
VSC_P, RED_P = 0.15, 0.03
POD_SHARE = 0.4      # 포디움(2~3위) 배수 = 우승 배수의 40%
F1_EDGE = 0.95       # 배당을 손익분기의 95%로
F1_CAP = 100.0
F1_SIMS = 6000
F1_RULE = "우승 = 표시 배수 · 포디움(2~3위) = 그 40% · 4~10위 본전 · 11위 이하 1배 손실 · 💥 리타이어 2배 손실"
F1_LOGO = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/330px-F1.svg.png"   # 공식 로고 (상표)
F1_SECTION = "🎙️ F1 중계"
F1_READY = 2          # 미리 만들어 두는 F1 경주 수 (명령어 치면 바로 뜨게)
F1_SCENE_SEC = (2.8, 3.5)   # 중계 장면 하나 보여 주는 시간 (보통 · 막판 두 장면) — 그림 보고 순위표 읽을 시간
LAP_SEC = 88.0       # 그림에서 간격(초) → 트랙 위 거리로 바꿀 때 쓰는 한 랩 시간
FONT = ["NanumGothic", "Malgun Gothic", "DejaVu Sans"]
BG, PANEL = "#15151E", "#1F1F2B"


def _gumbel(rng) -> float:
    return -math.log(-math.log(rng.random() or 1e-12))


def f1_qualify(rain: bool, rng=random) -> list[int]:
    """예선 → 출발 그리드 (드라이버 번호, 폴부터). 비가 오면 더 흔들린다."""
    noise = 1.4 if rain else 0.8
    return [d for _, d in sorted(((STRENGTH[d] / F1_TEMP + noise * _gumbel(rng), d) for d in range(len(DRIVERS))),
                                 reverse=True)]


def _strategy(rain: bool, rng, one_stop: bool = False) -> list[tuple[str, float]]:
    """타이어 전략 [(컴파운드, 이 스틴트가 끝나는 진행률)]. 1스톱(M→H · H→M) 과 2스톱(S→M→S) 은 평균 비슷, 운은 다르다.
    추월이 어려운 서킷(one_stop)은 다들 1스톱 — 자리를 지키는 게 먼저다."""
    if rain:
        return [("I", rng.uniform(0.4, 0.7)), ("I", 1.0)]
    r = rng.random()
    if r < 0.45 or (one_stop and r < 0.7):
        return [("M", rng.uniform(0.35, 0.6)), ("H", 1.0)]
    if r < 0.65 or one_stop:
        return [("H", rng.uniform(0.45, 0.7)), ("M", 1.0)]
    return [("S", rng.uniform(0.2, 0.35)), ("M", rng.uniform(0.6, 0.75)), ("S", 1.0)]


def f1_sim(grid: list[int], gp: tuple, rain: bool, rng=random, record: bool = False):
    """결승을 장면(F1_CHECKS)마다 굴린다 → (완주 순위, {리타이어: 시점 0~1}, 장면 기록).
    변수: 머신 · 드라이버 전력, 그리드(서킷 추월 난이도), 랩마다 운, 타이어 전략 · 피트스톱(실수), 1랩 사고,
    머신 고장, 크래시, 세이프티카(간격 압축 + 싼 피트인), VSC, 레드 플래그, 경기 중 비, 페널티."""
    laps, grip, sc_pct, rain_pct, street = gp[2], gp[3], gp[4], gp[5], gp[6]
    last = len(F1_CHECKS) - 1
    wet = rain
    pace, sigma = PACE_A * (0.7 if wet else 1.0), LAP_SIGMA * (1.8 if wet else 1.0)
    T = {d: p * (GRID_SEC + grip) for p, d in enumerate(grid)}
    strat = {d: _strategy(rain, rng, one_stop=grip >= 0.75) for d in grid}
    stint = dict.fromkeys(grid, 0)
    tyre = {d: strat[d][0][0] for d in grid}
    out: dict[int, float] = {}
    p_dnf = 0.02 + (0.012 if street else 0) + (0.012 if rain else 0)
    mech = {d: rng.randrange(1, last + 1) for d in grid if rng.random() < p_dnf}
    sc_at = rng.randrange(1, last) if rng.random() < sc_pct / 100 else None
    vsc_at = rng.randrange(1, last) if rng.random() < VSC_P else None
    red_at = rng.randrange(2, last - 1) if rng.random() < RED_P else None
    shower = rng.randrange(2, last - 1) if not rain and rng.random() < rain_pct / 200 else None
    snaps, prev, order_prev = [], 0.0, list(grid)
    for j, c in enumerate(F1_CHECKS):
        L = (c - prev) * laps
        ev = []
        if j == 0 and rng.random() < LAP1_CRASH + (0.15 if street else 0) + (0.1 if rain else 0):
            mid = sorted(grid, key=T.get)[4:18]
            hit = rng.sample(mid, rng.randint(1, 3))
            for d in hit:
                if rng.random() < 0.45:
                    out[d] = c
                else:
                    T[d] += rng.uniform(12, 35)          # 부서진 앞날개 갈러 피트로
            ev.append(("crash1", hit))
        if j == shower:                                   # 경기 중 비: 인터미디어로 갈아타는 타이밍이 갈린다
            wet, pace, sigma = True, PACE_A * 0.7, LAP_SIGMA * 1.8
            for d in grid:
                if d not in out:
                    T[d] += PIT_LOSS + rng.uniform(0, 14)
                    strat[d], stint[d], tyre[d] = [("I", 1.0)], 0, "I"
            ev.append(("shower", []))
        pitted = []
        for d in grid:
            if d in out:
                continue
            if mech.get(d) == j:
                out[d] = rng.uniform(prev, c)
                ev.append(("dnf", [d]))
                continue
            T[d] += L * (pace * (S_MAX - STRENGTH[d]) + TYRE_PACE[tyre[d]]) + rng.gauss(0, sigma * math.sqrt(max(L, 0.5)))
            st = strat[d]
            while stint[d] < len(st) - 1 and st[stint[d]][1] <= c:
                T[d] += PIT_LOSS
                if rng.random() < SLOW_PIT:
                    slow = rng.uniform(2.5, 9.0)
                    T[d] += slow
                    ev.append(("slowpit", [d], slow))
                stint[d] += 1
                tyre[d] = st[stint[d]][0]
                pitted.append(d)
            if j and rng.random() < CRASH_P + (0.004 if wet else 0):
                out[d] = rng.uniform(prev, c)
                ev.append(("crash", [d]))
                if sc_at is None and j < last and rng.random() < 0.6:
                    sc_at = j
        if pitted:
            ev.append(("pit", pitted))
        run = [d for d in grid if d not in out]
        before = [d for d in order_prev if d not in out]
        for a, b in zip(before, before[1:]):              # 추월이 어려운 서킷일수록 바로 앞차에 막힌다 (더러운 공기)
            if 0 < T[a] - T[b] < 1.0 + 6 * grip and rng.random() < STUCK[0] + STUCK[1] * grip:
                T[b] = T[a] + rng.uniform(0.2, 0.8)
        if run and rng.random() < PENALTY_P:
            d, sec = rng.choice(run), (10 if rng.random() < 0.25 else 5)
            T[d] += sec
            ev.append(("pen", [d], sec))
        flag = None
        if j < last and j in (sc_at, red_at):            # 세이프티카 · 레드 플래그: 간격이 사라지고, 남은 피트스톱을 싸게
            flag = "RED" if j == red_at else "SC"
            order = sorted(run, key=T.get)
            for r, d in enumerate(order):
                T[d] = T[order[0]] + 0.6 * r
            cheap = []
            for d in order:
                if stint[d] < len(strat[d]) - 1 and (flag == "RED" or rng.random() < 0.5):
                    stint[d] += 1
                    tyre[d] = strat[d][stint[d]][0]
                    T[d] += 0 if flag == "RED" else PIT_LOSS * 0.45
                    cheap.append(d)
            ev.append(("red" if flag == "RED" else "sc", cheap))
        elif j < last and j == vsc_at:
            flag = "VSC"
            lead = min(T[d] for d in run)
            for d in run:
                T[d] = lead + (T[d] - lead) * 0.6
            ev.append(("vsc", []))
        order_prev = sorted(run, key=T.get)
        if record:
            snaps.append({"frac": c, "order": order_prev, "gap": {d: T[d] - T[order_prev[0]] for d in order_prev},
                          "tyre": dict(tyre), "out": sorted(out, key=out.get), "ev": ev, "flag": flag, "wet": wet})
        prev = c
    return sorted((d for d in grid if d not in out), key=T.get), out, snaps


def f1_odds(grid: list[int], gp: tuple, rain: bool, sims: int = F1_SIMS, rng=None) -> list[tuple[float, float]]:
    """드라이버별 (우승 배수, 포디움 배수). 결승을 sims 번 돌려 확률을 재고, 기대값이 0 밑이 되게 정한다.
    기대값 = P(우승)·우승배수 + P(2~3위)·포디움배수 − P(11위 이하) − 2·P(리타이어) ≤ 0
    시뮬레이션 오차로 배당이 부풀지 않게 이득 쪽은 넉넉히(+2σ), 손실 쪽은 박하게(−2σ) 잡는다 — 롱샷일수록 중요."""
    rng = rng or random.Random()
    c = [[0, 0, 0, 0] for _ in DRIVERS]          # 우승 · 포디움 · 11위 이하 · 리타이어
    for _ in range(sims):
        fin, out, _ = f1_sim(grid, gp, rain, rng)
        for r, d in enumerate(fin):
            if r == 0:
                c[d][0] += 1
            elif r < 3:
                c[d][1] += 1
            elif r >= 10:
                c[d][2] += 1
        for d in out:
            c[d][3] += 1
    res = []
    for w, p, o, x in c:
        up = w + POD_SHARE * p + 2 * math.sqrt(w + POD_SHARE ** 2 * p + 1)
        loss = max(0.0, o + 2 * x - 2 * math.sqrt(o + 4 * x + 1))
        m = min(F1_CAP, F1_EDGE * loss / up)
        m = max(0.01, math.floor(m * 100) / 100)
        res.append((m, math.floor(m * POD_SHARE * 100) / 100))
    return res


def f1_mult(odds: tuple[float, float], rank: int | None) -> float:
    """rank = 완주 순위(0부터), None = 리타이어."""
    if rank is None:
        return -2
    if rank == 0:
        return odds[0]
    if rank < 3:
        return odds[1]
    return 0 if rank < 10 else -1


def _nm(d: int) -> str:
    return DRIVERS[d][1]


def f1_call(snap: dict, prev: list[int], me: int, laps: int) -> tuple[str, str]:
    """장면 소제목 + 캐스터 멘트 (내 드라이버 일 → 깃발 · 사고 → 선두 변화 → 크게 오른 차 순으로 세 마디까지)."""
    lap = max(1, round(snap["frac"] * laps))
    order, ev = snap["order"], snap["ev"]
    lines = []
    for kind, ds, *x in ev:
        names = ", ".join(_nm(d) for d in ds)
        line = {
            "crash1": f"💥 1랩 사고! {names} 충돌!",
            "dnf": f"💥 {names}, 머신 고장으로 멈춰 섭니다!",
            "crash": f"💥 {names} 크래시! 벽에 부딪혔습니다!",
            "slowpit": f"🔧 {names} 피트스톱 실수! {x[0]:.1f}초 손해!" if x else "",
            "pen": f"⚠️ {names} {x[0]}초 페널티!" if x else "",
            "sc": "🟡 세이프티카 출동! 간격이 전부 사라집니다" + (f" — {len(ds)}대가 그 틈에 피트인!" if ds else "!"),
            "vsc": "🟡 버추얼 세이프티카! 간격이 줄어듭니다!",
            "red": "🔴 레드 플래그! 레이스 중단 — 전원 줄 서서 재출발!",
            "shower": "🌧️ 비가 쏟아집니다! 전원 인터미디어 타이어로 — 타이밍 싸움!",
            "pit": f"🔧 피트스톱! {len(ds)}대가 타이어 교체" if len(ds) >= 3 else "",
        }[kind]
        if line:
            lines.append((0 if me in ds else 1, line))
    if order and prev and order[0] != prev[0]:
        lines.append((2, f"🔄 {_nm(order[0])} 선두로!"))
    gain = max(((prev.index(d) - p, d) for p, d in enumerate(order) if d in prev), default=(0, None))
    if gain[0] >= 3:
        lines.append((3, f"↗️ {_nm(gain[1])} {gain[0]}계단 점프!"))
    if not lines and order:
        lines.append((4, f"{_nm(order[0])} 선두, {_nm(order[1])} 추격!" if len(order) > 1 else f"{_nm(order[0])} 선두!"))
    text = " ".join(s for _, s in sorted(lines, key=lambda t: t[0])[:3])
    if snap["frac"] >= 1:
        stage = "🏁 체커기"
    elif snap["flag"] == "RED":
        stage = f"🔴 레드 플래그 · 랩 {lap}/{laps}"
    elif snap["flag"]:
        stage = f"🟡 {'세이프티카' if snap['flag'] == 'SC' else 'VSC'} · 랩 {lap}/{laps}"
    elif snap["frac"] < 0.05:
        stage = f"🟢 오프닝 랩 · 랩 {lap}/{laps}"
    else:
        stage = f"랩 {lap}/{laps}"
    return stage, text


def _arrow(prev: list[int], order: list[int], d: int) -> int:
    return prev.index(d) - order.index(d) if prev and d in prev and d in order else 0


def f1_status(snap: dict, prev: list[int], me: int) -> str:
    code = f"{DRIVERS[me][0]} {DRIVERS[me][1]}"
    if me not in snap["order"]:
        return f"💥 **{code}** 리타이어"
    p = snap["order"].index(me)
    a = _arrow(prev, snap["order"], me)
    move = f" (▲{a})" if a > 0 else (f" (▼{-a})" if a < 0 else "")
    gap = "선두!!" if p == 0 else f"+{snap['gap'][me]:.1f}초"
    return f"👈 **{code}** 지금 **P{p + 1}**{move} · {gap} · 타이어 {snap['tyre'][me]}"


def _track_xy(pts: list, frac: float) -> tuple[float, float]:
    t = (frac % 1.0) * len(pts)
    i, u = int(t) % len(pts), t - int(t)
    (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
    return x0 + u * (x1 - x0), y0 + u * (y1 - y0)


def _team_badge(ax, team: str, cx: float, cy: float, w: float, h: float, px: tuple[float, float], size: int = 10,
                icon: bool = False):
    """팀 로고 (흰 바탕 없이 — 다크 화면용으로 어두운 부분을 밝힌 것, 비율 유지). 쓸 수 있는 로고가 없는 팀은 팀 색 글자.
    ax 는 y 축이 아래로 커지는 축, px = (x 1칸, y 1칸) 이 몇 픽셀인지."""
    logo = (TEAM_ICON if icon else TEAM_LOGO).get(team)
    if logo is None:
        ax.text(cx, cy, TEAM_TAG.get(team, TEAMS[team][0]), color=TEAMS[team][2], fontsize=size, fontweight="bold", ha="center", va="center",
                linespacing=1.0, zorder=4, fontfamily=FONT)
        return
    ih, iw = logo.shape[:2]
    s = min(w * px[0] / iw, h * px[1] / ih)
    dx, dy = iw * s / px[0], ih * s / px[1]
    ax.imshow(logo, extent=(cx - dx / 2, cx + dx / 2, cy + dy / 2, cy - dy / 2), aspect="auto", zorder=4,
              interpolation="antialiased")


def _ink(hex_color: str) -> str:
    """팀 색 위에 올릴 글자색 (밝은 색이면 검정)."""
    r, g, b = (int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return "black" if 0.299 * r + 0.587 * g + 0.114 * b > 0.6 else "white"


def f1_render(gp: tuple, head: str, snap: dict, me: int, prev: list[int] | None = None, odds=None, final: bool = False) -> bytes:
    """왼쪽: 실제 서킷 위를 도는 차들(동그라미 안 숫자 = 지금 순위) · 오른쪽: 22명 전원 순위표(달리는 차 → 리타이어).
    글씨를 크게 — 디스코드 임베드는 그림을 작게 줄여 보여 준다."""
    fig = Figure(figsize=(12, 10), dpi=100, facecolor=BG)
    FigureCanvasAgg(fig)
    pts = TRACKS[gp[1]]["pts"]
    order, gap = snap["order"], snap["gap"]
    flag = snap.get("flag")
    stroke = [pe.withStroke(linewidth=4, foreground=BG)]

    ax = fig.add_axes((0.005, 0.1, 0.585, 0.78))
    ax.set_xlim(-0.05, 1.05)
    ax.set_ylim(-0.05, 1.05)
    ax.set_aspect("equal")
    ax.axis("off")
    xs, ys = [p[0] for p in pts] + [pts[0][0]], [p[1] for p in pts] + [pts[0][1]]
    track = {"SC": "#E0B000", "VSC": "#E0B000", "RED": "#C0392B"}.get(flag, "#5A5A70")
    ax.plot(xs, ys, color="#0A0A0F", lw=26, solid_capstyle="round", solid_joinstyle="round", zorder=1)
    ax.plot(xs, ys, color=track, lw=16, solid_capstyle="round", solid_joinstyle="round", zorder=2)
    (x0, y0), (x1, y1) = pts[0], pts[1]
    nx, ny = y0 - y1, x1 - x0
    norm = math.hypot(nx, ny) or 1
    nx, ny = nx / norm * 0.045, ny / norm * 0.045
    ax.plot([x0 - nx, x0 + nx], [y0 - ny, y0 + ny], color="white", lw=6, zorder=3)          # 출발 · 결승선
    ax.annotate("", xy=(x0 + (x1 - x0) * 6, y0 + (y1 - y0) * 6), xytext=(x0, y0), zorder=3,     # 달리는 방향
                arrowprops={"arrowstyle": "-|>", "color": "white", "lw": 2.5, "mutation_scale": 22})
    lead = 0.0 if final else (snap["frac"] * gp[2]) % 1.0
    for p in reversed(range(len(order))):                                                     # 선두가 맨 위에 그려지게
        d = order[p]
        x, y = _track_xy(pts, lead - gap.get(d, 0.0) / LAP_SEC)
        color, mine = TEAMS[DRIVERS[d][3]][2], d == me
        ax.scatter([x], [y], s=1100 if mine else 560, color=color, zorder=9 if mine else 5 + (p == 0),
                   edgecolors="white" if mine else ("#FFD43B" if p == 0 else BG), linewidths=4 if mine else 2)
        ax.text(x, y, str(p + 1), color=_ink(color), fontsize=15 if mine else 12, fontweight="bold", ha="center",
                va="center", zorder=10 if mine else 6 + (p == 0), fontfamily=FONT)
        if mine:
            ax.annotate(f"▼ {DRIVERS[d][0]}", (x, y), xytext=(0, 24), textcoords="offset points", ha="center",
                        color="#FFD43B", fontsize=19, fontweight="bold", fontfamily=FONT, zorder=11, path_effects=stroke)

    if me >= 0:                                                                                # 왼쪽 아래: 내 드라이버 팀 로고
        badge = fig.add_axes((0.015, 0.02, 0.17, 0.07))
        badge.set_xlim(0, 1)
        badge.set_ylim(1, 0)
        badge.set_autoscale_on(False)
        badge.axis("off")
        _team_badge(badge, DRIVERS[me][3], 0.5, 0.5, 1.0, 1.0, (fig.bbox.width * 0.17, fig.bbox.height * 0.07), 14)
        fig.text(0.195, 0.055, f"내 드라이버\n{DRIVERS[me][0]} {DRIVERS[me][1]}", color="#FFD43B", fontsize=17,
                 fontweight="bold", va="center", fontfamily=FONT, linespacing=1.3)

    fig.text(0.015, 0.955, gp[1], color="white", fontsize=27, fontweight="bold", fontfamily=FONT)
    tags = [(head, "#E10600", "white")]
    if flag:
        tags.append(({"SC": "SAFETY CAR", "VSC": "VIRTUAL SC", "RED": "RED FLAG"}[flag],
                     "#C0392B" if flag == "RED" else "#FFD43B", "white" if flag == "RED" else "black"))
    if snap.get("wet"):
        tags.append(("RAIN", "#3B82F6", "white"))
    x, renderer = 0.015, fig.canvas.get_renderer()
    for t, bg, fg in tags:
        txt = fig.text(x, 0.905, t, color=fg, fontsize=17, fontweight="bold", fontfamily=FONT,
                       bbox={"boxstyle": "round,pad=0.35", "facecolor": bg, "edgecolor": "none"})
        x += txt.get_window_extent(renderer).width / fig.bbox.width + 0.02

    rows = [(d, False) for d in order] + [(d, True) for d in snap.get("out", [])]          # 22명 전원
    span = len(rows) + 1.4
    tw = fig.add_axes((0.6, 0.008, 0.392, 0.984))
    tw.set_xlim(0, 1)
    tw.set_ylim(span, 0)
    tw.set_autoscale_on(False)
    tw.axis("off")
    px = (fig.bbox.width * 0.392, fig.bbox.height * 0.984 / span)    # 순위표 칸 1 = 몇 픽셀 (로고 비율 맞추기)
    tw.add_patch(Rectangle((0, 0), 1, span, facecolor=PANEL, edgecolor="none"))
    tw.text(0.5, 0.6, "출발 그리드 · 우승 배당" if odds else ("최종 순위" if final else "실시간 순위"),
            color="#B8B8CC", fontsize=16, fontweight="bold", ha="center", va="center", fontfamily=FONT)
    medal = {0: "#FFD43B", 1: "#D9D9D9", 2: "#E3A869"}
    for i, (d, out) in enumerate(rows):
        y = i + 1.55
        p = None if out else i
        if d == me:
            tw.add_patch(Rectangle((0.01, y - 0.48), 0.98, 0.96, facecolor="#5A1A1A" if out else "#B3161B", edgecolor="none"))
        elif i % 2:
            tw.add_patch(Rectangle((0.01, y - 0.48), 0.98, 0.96, facecolor="#262634", edgecolor="none"))   # 줄 구분
        tw.text(0.06, y, "OUT" if out else str(p + 1), fontsize=12 if out else 17, fontweight="bold", ha="center",
                va="center", fontfamily=FONT, color="#FF6B6B" if out else (medal.get(p, "white") if final else "white"))
        _team_badge(tw, DRIVERS[d][3], 0.2, y, 0.17, 0.8, px, icon=True)
        tw.text(0.31, y, DRIVERS[d][0], color="white", fontsize=18, fontweight="bold", va="center", fontfamily=FONT)
        if not out and not odds and d in snap.get("tyre", {}):
            t = snap["tyre"][d]
            tw.scatter([0.52], [y], s=330, facecolor=BG, edgecolors=TYRE_COLOR[t], linewidths=2.5, zorder=3)
            tw.text(0.52, y, t, color=TYRE_COLOR[t], fontsize=10, fontweight="bold", ha="center", va="center", fontfamily=FONT)
        a = 0 if out or odds else _arrow(prev or [], order, d)
        if a:
            tw.text(0.65, y, f"▲{a}" if a > 0 else f"▼{-a}", color="#2ECC71" if a > 0 else "#FF5A5A", fontsize=14,
                    fontweight="bold", ha="center", va="center", fontfamily=FONT)
        if out:
            right = "리타이어"
        elif odds:
            right = f"+{odds[d][0]:g}배"
        else:
            g = gap.get(d, 0.0)
            right = "선두" if p == 0 else (f"+{int(g // LAP_SEC)}랩" if g >= LAP_SEC else f"+{g:.1f}")
        tw.text(0.965, y, right, color="#FF6B6B" if out else "white", fontsize=16, fontweight="bold" if d == me else "normal",
                ha="right", va="center", fontfamily=FONT)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return buf.getvalue()


def f1_grid_snap(grid: list[int]) -> dict:
    """출발 그리드 그림용 장면 — 줄 서 있는 차들."""
    return {"frac": 0.0, "order": list(grid), "gap": {d: 0.35 * p for p, d in enumerate(grid)}, "tyre": {}, "out": [],
            "ev": [], "flag": None, "wet": False}


def f1_file(png: bytes) -> discord.File:
    return discord.File(io.BytesIO(png), filename="f1.png")


# ───────────── 버튼 ─────────────

class PickView(discord.ui.View):
    """출전표의 선택 — 경주를 연 사람만, 한 번만. 달팽이는 버튼, F1 은 드라이버 목록."""

    def __init__(self, cog: "Race", game, user, amount: int, race: dict):
        super().__init__(timeout=60)
        self.cog, self.game, self.user, self.amount, self.race = cog, game, user, amount, race
        self.message = None
        self.done = False
        if game == "f1":
            opts = [discord.SelectOption(label=f"P{p + 1} {DRIVERS[d][0]} {DRIVERS[d][1]}", value=str(d),
                                         emoji=cog.temoji(DRIVERS[d][3]),
                                         description=f"{TEAMS[DRIVERS[d][3]][0]} · 우승 +{race['odds'][d][0]:g}배 · "
                                                     f"포디움 +{race['odds'][d][1]:g}배")
                    for p, d in enumerate(race["grid"])]
            sel = discord.ui.Select(placeholder="🏎️ 드라이버를 고르세요", options=opts)

            async def chosen(interaction: discord.Interaction):
                await self._go(interaction, int(sel.values[0]))
            sel.callback = chosen
            self.add_item(sel)
        else:
            for i, r in enumerate(race["runners"]):
                b = discord.ui.Button(label=f"{i + 1}번 {r[1]}", emoji=r[0], style=discord.ButtonStyle.primary)
                b.callback = self._picker(i)
                self.add_item(b)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 경주를 연 사람만 고를 수 있어요.", ephemeral=True)
            return False
        return True

    def _picker(self, idx: int):
        async def pick(interaction: discord.Interaction):
            await self._go(interaction, idx)
        return pick

    async def _go(self, interaction: discord.Interaction, idx: int):
        if self.done:   # 연타 방지
            return await interaction.response.defer()
        self.done = True
        self.stop()
        if self.game == "f1":
            await self.cog._run_f1(interaction, self, idx)
        else:
            await self.cog._run_simple(interaction, self, idx)

    async def on_timeout(self):
        if self.done or self.message is None:
            return
        await _disqualify(self.message, self.user, self.amount, F1_SECTION if self.game == "f1" else self.game.section)


class RowView(discord.ui.View):
    """조정: [출발!] → 경주 동안 [노 젓기!] 연타. 경주를 연 사람만."""

    def __init__(self, cog: "Race", user, amount: int, race: dict):
        super().__init__(timeout=60)
        self.cog, self.user, self.amount, self.race = cog, user, amount, race
        self.message = None
        self.started = False
        self.taps = 0
        go = discord.ui.Button(label="출발!", emoji="🚣", style=discord.ButtonStyle.success)
        go.callback = self._start
        self.add_item(go)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("🙅 경주를 연 사람만 노를 저을 수 있어요.", ephemeral=True)
            return False
        return True

    async def _start(self, interaction: discord.Interaction):
        if self.started:
            return await interaction.response.defer()
        self.started = True
        self.timeout = None
        self.clear_items()
        tap = discord.ui.Button(label="노 젓기!", emoji="🚣", style=discord.ButtonStyle.success)
        tap.callback = self._tap
        self.add_item(tap)
        await self.cog._run_row(interaction, self)

    async def _tap(self, interaction: discord.Interaction):
        self.taps += 1
        await interaction.response.defer()

    async def on_timeout(self):
        if self.started or self.message is None:
            return
        await _disqualify(self.message, self.user, self.amount, "🎙️ 조정 중계")


async def _disqualify(message, user, amount: int, section: str):
    """60초 안에 안 고르면 실격 — 베팅금은 출전표를 띄울 때 이미 빠졌다."""
    e = ui.card("⌛ 실격", '> 🎙️ *"끝내 아무도 나타나지 않았습니다! 실격 처리됩니다."*'
                f"\n\n`정산` **{ui.won(-amount)}** · 1배 손실", ui.LOSE, user, section)
    try:
        await message.edit(embed=e, view=None, attachments=[])
    except discord.HTTPException:
        pass


class Race(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = EconomyDB()
        self.team_emoji: dict = {}
        self.f1_ready: list[dict] = []    # 미리 만들어 둔 F1 경주 (명령어 치면 바로 뜨게)
        self._filling = None

    async def cog_load(self):
        self._refill()
        self._emoji_task = asyncio.create_task(self._sync_emoji())

    async def _bet(self, interaction: discord.Interaction, amount: int, section: str, build):
        """잔액 확인 → 출전표 만들기 → 베팅금 차감 → 출전표 + 버튼. 못 띄우면 환불.
        build() → (view 를 만드는 함수, 출전표 임베드, 첨부 그림 또는 None)."""
        user = interaction.user
        await interaction.response.defer()
        bal = await self.db.get_balance(user.id)
        if bal < amount:
            return await interaction.followup.send(embed=ui.card(
                "🙅 참가할 수 없어요", f'> 🎙️ *"잔액이 부족합니다!"*\n\n`베팅` **{amount:,}원**\n`잔액` **{bal:,}원**',
                ui.LOSE, user, section))
        make_view, card, png = await build()
        await self.db.add_balance(user.id, -amount)
        view = make_view()
        try:
            kw = {"file": f1_file(png)} if png else {}
            view.message = await interaction.followup.send(embed=card, view=view, wait=True, **kw)
        except discord.HTTPException:
            await self.db.add_balance(user.id, amount)
            raise

    async def _show(self, interaction: discord.Interaction, embed: discord.Embed, png: bytes | None = None, **kw):
        """중계 장면 바꾸기. 그림이 있으면 같이 바꾼다."""
        if png:
            embed.set_image(url="attachment://f1.png")
            kw["attachments"] = [f1_file(png)]
        await interaction.edit_original_response(embed=embed, **kw)

    # ───────────── 🐌 달팽이 ─────────────

    @staticmethod
    def _snail_card(user, amount: int, race: dict) -> discord.Embed:
        lines = [f"`{i + 1}` {e} **{name}** · {info}\n　 *{intro}*" for i, (e, name, info, intro) in enumerate(race["runners"])]
        e = ui.card(f"🐌 제{race['no']}경주 출전표",
                    '> 🎙️ *"오늘의 출전 달팽이 4마리입니다! 어디에 거시겠어요?"*\n\n'
                    + "\n".join(lines) + f"\n\n**🏆 상금표** · `베팅` **{amount:,}원**\n" + prize_lines(amount, race["prizes"]),
                    ui.INFO, user, SNAIL.section)
        e.set_thumbnail(url=ui.emoji_url("🐌"))
        e.set_footer(text="60초 안에 고르지 않으면 실격 — 베팅금은 돌려받지 못합니다")
        return e

    @app_commands.command(name="달팽이", description="달팽이 4마리 중 한 마리에 베팅! 1·2위는 수익, 3·4위는 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def snail(self, interaction: discord.Interaction, amount: app_commands.Range[int, MIN_BET]):
        user, amount = interaction.user, int(amount)

        async def build():
            race = {"no": random.randint(1, 12), "runners": SNAIL.runners(random), "prizes": prizes(SNAIL.n)}
            return (lambda: PickView(self, SNAIL, user, amount, race)), self._snail_card(user, amount, race), None
        await self._bet(interaction, amount, SNAIL.section, build)

    async def _run_simple(self, interaction: discord.Interaction, view: PickView, pick: int):
        game, race, amount, user = view.game, view.race, view.amount, view.user
        runners, sec, title = race["runners"], game.section, f"{game.icon} 제{race['no']}경주"
        names = [r[1] for r in runners]
        n = len(runners)
        finish = random.sample(range(n), n)   # 순위는 완전 랜덤
        place = finish.index(pick)
        mult = race["prizes"][place]
        delta = amount * mult

        await interaction.response.edit_message(
            embed=ui.card(f"{title} — 출발 대기", f'> 🎙️ *"{names[pick]}에 {amount:,}원! {game.ready}"*', ui.DARK, user, sec),
            view=None)
        # 결과를 먼저 저장하고 연출한다 (연출이 실패해도 돈은 정확하다). 베팅금은 이미 빠져 있다 → 전체 변동 = delta.
        try:
            new_bal = await self.db.add_balance(user.id, amount + delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        comeback = random.choice(finish[1:]) if random.random() < COMEBACK_PROB else None
        fr = frames(finish, comeback=comeback)
        special = game.twist(fr, finish, comeback, names, random)
        try:
            for k, ((stage, call), pos) in enumerate(zip(game.calls, fr)):
                last = k == len(fr) - 1
                order = sorted(range(n), key=lambda i: (-pos[i], finish.index(i)))
                if last and comeback is not None:
                    stage, text = "🔥 대역전!!", game.comeback_call.format(names[finish[0]], names[comeback])
                else:
                    text = special.get(k) or call.format(names[order[0]], names[order[1]])
                await asyncio.sleep(1.8 if k >= len(fr) - 2 else 1.3)   # 막판은 한 박자 늦게
                await interaction.edit_original_response(embed=ui.card(
                    f"{title} — {stage}", f'> 🎙️ *"{text}"*\n\n' + lanes(runners, pos, pick),
                    ui.GOLD if (last and comeback is not None) else ui.DARK, user, sec))
            await asyncio.sleep(1.5)
        except discord.HTTPException:
            pass

        emoji, headline, caster = game.results[place]
        if comeback is not None and pick == finish[0]:
            headline, caster = "역전 우승!!!", "끝까지 기다렸다가 결승선 앞에서 뒤집었습니다!!"
        elif comeback is not None and pick == comeback:
            caster = "다 잡은 우승을 결승선 앞에서 놓쳤습니다… 너무 아쉬워요!"
        board = "\n".join(f"{PLACES[r]} {runners[i][0]} " + (f"**{names[i]}** 👈" if i == pick else names[i])
                          for r, i in enumerate(finish))
        await self._result(interaction, f"{emoji} {names[pick]} {headline}", caster, board, place, mult, delta,
                           new_bal, user, sec, emoji)

    async def _result(self, interaction, title, caster, board, place, mult, delta, new_bal, user, sec, emoji):
        color = ui.GOLD if place == 0 else (ui.DOOM if mult <= -5 else ui.tone(delta))
        e = ui.card(title, f'> 🎙️ *"{caster}"*\n\n{board}\n\n`정산` **{ui.won(delta)}** · {label(mult)}\n`잔액` **{new_bal:,}원**',
                    color, user, sec)
        e.set_thumbnail(url=ui.result_thumb(emoji, e.color.value, user))   # 2.10.3 키코 그림 (공개 전엔 이모지)
        try:
            await interaction.edit_original_response(embed=e, view=None)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)

    # ───────────── 🚣 조정 ─────────────

    @app_commands.command(name="조정", description="노를 직접 저어라! [노 젓기] 연타 + 물살 운으로 6척 경주 · 1·2위 수익, 3위 본전, 4~6위 손실 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def row(self, interaction: discord.Interaction, amount: app_commands.Range[int, MIN_BET]):
        user, amount = interaction.user, int(amount)

        async def build():
            me = random.randrange(ROW_N)
            rivals = iter(random.sample(ROWERS, ROW_N - 1))
            boats = [None if i == me else next(rivals) for i in range(ROW_N)]
            race = {"no": random.randint(1, 12), "me": me, "boats": boats, "prizes": prizes(ROW_N),
                    "rules": row_rules(interaction.guild_id)}
            lines = [f"`{i + 1}` 🚣 **{user.display_name}** 👈 · 내 보트" if b is None else
                     f"`{i + 1}` {b[0]} **{b[1]}** · {b[2]}kg · {random.choice(MOODS)}\n　 *{b[3]}*" for i, b in enumerate(boats)]
            e = ui.card(f"🚣 제{race['no']}경주 · 싱글 스컬 2000m",
                        '> 🎙️ *"출발하면 [🚣 노 젓기!]를 연타하세요! 많이 저을수록 빨라지지만, 물살은 반쯤 운이에요."*\n\n'
                        + "\n".join(lines) + f"\n\n**🏆 상금표** · `베팅` **{amount:,}원**\n" + prize_lines(amount, race["prizes"]),
                        ui.INFO, user, "🎙️ 조정 중계")
            e.set_thumbnail(url=ui.emoji_url("🚣"))
            e.set_footer(text=f"구간마다 {race['rules'][1]}번 이상 저으면 전속력 · 60초 안에 출발하지 않으면 실격")
            return (lambda: RowView(self, user, amount, race)), e, None
        await self._bet(interaction, amount, "🎙️ 조정 중계", build)

    async def _run_row(self, interaction: discord.Interaction, view: RowView):
        # ponytail: 경주 중(약 15초) 봇이 재시작되면 정산 전이라 베팅금이 사라진다 — 문제 되면 진행 중 경주를 DB에 남겨 재시작 때 환불
        race, amount, user, sec = view.race, view.amount, view.user, "🎙️ 조정 중계"
        me, boats = race["me"], race["boats"]
        seg_sec, target, lazy = race["rules"]
        names = [user.display_name if b is None else b[1] for b in boats]
        runners = [("🚣", n) if b is None else (b[0], n) for b, n in zip(boats, names)]
        title = f"🚣 제{race['no']}경주"
        dist = [0.0] * ROW_N
        await interaction.response.edit_message(embed=ui.card(
            f"{title} — 출발!", '> 🎙️ *"출발!! 지금 [🚣 노 젓기!]를 연타하세요!!"*\n\n' + lanes(runners, [0] * ROW_N, me),
            ui.DARK, user, sec), view=view)
        for k, seg in enumerate(ROW_SEGS):
            view.taps = 0
            await asyncio.sleep(seg_sec)
            taps = view.taps
            e = row_step(dist, me, taps, random, target, lazy)
            lead = max(range(ROW_N), key=lambda i: dist[i])
            rank = sorted(range(ROW_N), key=lambda i: -dist[i]).index(me) + 1
            last = k == len(ROW_SEGS) - 1
            try:   # 화면 한 장이 실패해도 경주는 끝까지 간다
                await interaction.edit_original_response(embed=ui.card(
                    f"{title} — {seg}",
                    f'> 🎙️ *"{row_call(names[me], e, names[lead], lead == me)}"*\n\n' + lanes(runners, row_cells(dist), me)
                    + f"\n\n🚣 이번 구간 **{taps}번** 저음 {ui.bar(e, 1, 10)} · 지금 **{rank}위**",
                    ui.DARK, user, sec), view=None if last else view)
            except discord.HTTPException:
                pass
        view.stop()

        finish = sorted(range(ROW_N), key=lambda i: -dist[i])
        place = finish.index(me)
        mult = race["prizes"][place]
        delta = amount * mult
        try:
            new_bal = await self.db.add_balance(user.id, amount + delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")
        await asyncio.sleep(1.2)
        heads = ("🏆", "우승!!", "물살을 가르며 1위! 엄청난 스트로크였어요!"), ("🥈", "2위!", "아깝게 2위! 그래도 상금은 챙겨 갑니다."), \
                ("🥉", "3위", "3위 — 본전은 지켰습니다."), ("😥", "4위…", "4위… 한 번만 더 저었다면!"), \
                ("😫", "5위…", "5위… 팔이 너무 무거웠나요?"), ("🌊", "꼴찌…", "마지막으로 들어옵니다… 물만 잔뜩 먹었어요.")
        emoji, headline, caster = heads[place]
        board = "\n".join(f"{PLACES[r]} {runners[i][0]} " + (f"**{names[i]}** 👈" if i == me else names[i])
                          for r, i in enumerate(finish))
        await self._result(interaction, f"{emoji} {headline}", caster, board, place, mult, delta, new_bal, user, sec, emoji)

    # ───────────── 🏎️ F1 ─────────────

    def temoji(self, team: str):
        """팀 로고 이모지 (봇 앱 이모지). 아직 없거나 로고 없는 팀이면 팀 색 이모지."""
        return getattr(self, "team_emoji", {}).get(team) or TEAMS[team][1]

    async def _sync_emoji(self):
        """assets/teams/emoji/*.png 를 봇 앱 이모지(f1_mer …)로 올려 둔다 — 한 번 올리면 다음부턴 있는 걸 쓴다."""
        await self.bot.wait_until_ready()
        try:
            have = {e.name: e for e in await self.bot.fetch_application_emojis()}
            for f in sorted(EMOJI_DIR.glob("*.png")):
                name = f"f1_{f.stem.lower()}"
                self.team_emoji[f.stem] = have.get(name) or await self.bot.create_application_emoji(name=name, image=f.read_bytes())
            print(f"🏎️ F1 팀 로고 이모지 {len(self.team_emoji)}개 준비")
        except Exception as ex:   # 실패해도 팀 색 이모지로 계속
            print(f"[F1] 팀 로고 이모지 준비 실패: {type(ex).__name__}: {ex}")

    @staticmethod
    async def _prepare_f1() -> dict:
        """경주 하나 준비: 서킷 · 날씨 · 예선 · 배당(시뮬레이션) · 출발 그리드 그림 — 무거워서 미리 만들어 둔다."""
        gp = random.choice(CIRCUITS)
        rain = random.random() < gp[5] / 100
        grid = f1_qualify(rain)

        def work():
            odds = f1_odds(grid, gp, rain)
            return odds, f1_render(gp, "출발 그리드", dict(f1_grid_snap(grid), wet=rain), -1, None, odds)
        odds, png = await asyncio.to_thread(work)
        return {"gp": gp, "rain": rain, "grid": grid, "odds": odds, "png": png}

    def _refill(self):
        """준비된 경주가 F1_READY 개가 되게 뒤에서 채운다 (이미 채우는 중이면 그대로)."""
        if len(self.f1_ready) >= F1_READY or (self._filling and not self._filling.done()):
            return

        async def fill():
            while len(self.f1_ready) < F1_READY:
                self.f1_ready.append(await self._prepare_f1())
        self._filling = asyncio.create_task(fill())

    def _f1_card(self, user, amount: int, race: dict) -> discord.Embed:
        flag, name, laps, grip, sc_pct, rain_pct, street, note = race["gp"]
        weather = "🌧️ 비" if race["rain"] else (f"☁️ 경기 중 비 {rain_pct // 2}%" if rain_pct else "☀️ 맑음")
        overtake = "어려움" if grip >= 0.7 else ("보통" if grip >= 0.5 else "쉬움")
        e = ui.card(f"🏎️ {flag} {name} — 출발 그리드",
                    f'> 🎙️ *"{note}! {laps}랩 레이스가 곧 시작됩니다. 누구에게 거시겠어요?"*\n'
                    f"{weather} · 🟡 세이프티카 {sc_pct}% · {'🏙️ 시가지' if street else '🏁 상설 서킷'} · 추월 {overtake}"
                    f"\n\n`베팅` **{amount:,}원**\n-# {F1_RULE}", ui.INFO, user, F1_SECTION)
        grid, odds = race["grid"], race["odds"]
        for lo, hi in ((0, 11), (11, 22)):
            e.add_field(name=f"우승 배당 · P{lo + 1}~P{hi}", inline=True, value="\n".join(
                f"`P{p + 1:<2}` {self.temoji(DRIVERS[d][3])} {DRIVERS[d][0]} **+{odds[d][0]:g}**" for p, d in enumerate(grid[lo:hi], lo)))
        e.set_thumbnail(url=F1_LOGO)
        e.set_image(url="attachment://f1.png")
        e.set_footer(text="아래 목록에서 드라이버를 고르세요 · 60초 안에 안 고르면 실격")
        return e

    @app_commands.command(name="f1", description="2026 F1 그리드에서 드라이버를 골라 베팅! 실제 서킷 · 전력 · 타이어 전략 · 사고 반영 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def f1(self, interaction: discord.Interaction, amount: app_commands.Range[int, MIN_BET]):
        user, amount = interaction.user, int(amount)

        async def build():
            race = self.f1_ready.pop(0) if self.f1_ready else await self._prepare_f1()   # 보통은 미리 만들어 둔 경주
            self._refill()
            return (lambda: PickView(self, "f1", user, amount, race)), self._f1_card(user, amount, race), race["png"]
        await self._bet(interaction, amount, F1_SECTION, build)

    async def _run_f1(self, interaction: discord.Interaction, view: PickView, me: int):
        race, amount, user, sec = view.race, view.amount, view.user, F1_SECTION
        gp, grid = race["gp"], race["grid"]
        flag, gp_name, laps = gp[:3]
        finish, out, snaps = f1_sim(grid, gp, race["rain"], random, record=True)
        rank = finish.index(me) if me in finish else None
        mult = f1_mult(race["odds"][me], rank)
        delta = math.floor(amount * mult)
        name = f"{DRIVERS[me][0]} {DRIVERS[me][1]}"

        def card(title, text, color=ui.DARK):
            e = ui.card(f"🏎️ {flag} {gp_name} — {title}", text, color, user, sec)
            e.set_thumbnail(url=F1_LOGO)
            return e

        formation = card("포메이션 랩", f'> 🎙️ *"{name}에 {amount:,}원! 그리드 P{grid.index(me) + 1}에서 출발합니다…"*')
        formation.set_image(url="attachment://f1.png")
        await interaction.response.edit_message(embed=formation, view=None)
        # 결과를 먼저 저장하고 연출한다 (연출이 실패해도 돈은 정확하다).
        try:
            new_bal = await self.db.add_balance(user.id, amount + delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        scenes, prev = [], list(grid)
        for snap in snaps:
            stage, text = f1_call(snap, prev, me, laps)
            final = snap["frac"] >= 1
            head = "체커기" if final else f"LAP {max(1, round(snap['frac'] * laps))}/{laps}"
            color = (ui.GOLD if rank == 0 else ui.tone(delta)) if final else ui.DARK
            base = grid if final else prev   # 체커기 장면의 ▲▼ 는 출발 그리드 대비
            scenes.append((stage, f'> 🎙️ *"{text}"*\n\n{f1_status(snap, base, me)}', (gp, head, snap, me, base, None, final), color))
            prev = snap["order"]
        def draw(s):   # 장면을 보여 주는 동안 다음 장면 그림을 그린다
            return asyncio.create_task(asyncio.to_thread(f1_render, *s[2]))
        nxt = draw(scenes[0])
        for k, (stage, text, _, color) in enumerate(scenes):
            png = await nxt
            if k + 1 < len(scenes):
                nxt = draw(scenes[k + 1])
            await asyncio.sleep(F1_SCENE_SEC[1] if k >= len(scenes) - 2 else F1_SCENE_SEC[0])   # 막판은 한 박자 늦게
            try:
                await self._show(interaction, card(stage, text, color), png)
            except discord.HTTPException:
                pass
        await asyncio.sleep(F1_SCENE_SEC[1])

        g = grid.index(me) + 1
        if rank is None:
            emoji, head, caster = "💥", "리타이어…", "머신이 멈춰 섰습니다… 리타이어. 2배 손실이에요."
        elif rank == 0:
            emoji, head, caster = "🏆", "우승!!", "체커기를 가장 먼저 받습니다! 탁월한 안목이에요!"
            if g >= 4:
                head, caster = "역전 우승!!!", f"P{g}에서 출발해 우승까지!! 이게 F1이죠!!"
        elif rank < 3:
            emoji, head, caster = "🍾", f"P{rank + 1} 포디움!", "포디움! 샴페인 파티입니다!"
        elif rank < 10:
            emoji, head, caster = "🏁", f"P{rank + 1} 완주", "포인트권 완주 — 본전입니다."
        else:
            emoji, head, caster = "😶", f"P{rank + 1} 완주", "포인트권 밖… 1배 손실이에요."
        move = "" if rank is None else f" (그리드 P{g} → P{rank + 1})"
        podium = " · ".join(f"{PLACES[i]} {DRIVERS[d][0]}" for i, d in enumerate(finish[:3]))
        star = max(finish, key=lambda d: grid.index(d) - finish.index(d))
        jump = grid.index(star) - finish.index(star)
        lines = [podium]
        if jump >= 3:
            lines.append(f"🚀 오늘의 드라이버: {DRIVERS[star][0]} {_nm(star)} (P{grid.index(star) + 1} → P{finish.index(star) + 1})")
        if out:
            lines.append("💥 리타이어: " + ", ".join(DRIVERS[d][0] for d in sorted(out, key=out.get)))
        e = ui.card(f"{emoji} {name} {head}",
                    f'> 🎙️ *"{caster}"*{move}\n\n' + "\n".join(lines)
                    + f"\n\n`정산` **{ui.won(delta)}** · {label(mult)}\n`잔액` **{new_bal:,}원**",
                    ui.GOLD if rank == 0 else ui.tone(delta), user, sec)
        e.set_thumbnail(url=F1_LOGO)
        e.set_image(url="attachment://f1.png")   # 마지막 장면(체커기 순위표)을 그대로 둔다
        try:
            await interaction.edit_original_response(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)

async def setup(bot: commands.Bot):
    await bot.add_cog(Race(bot))
