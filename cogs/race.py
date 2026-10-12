# cogs/race.py — 레이스 미니게임: 🐌 달팽이 · 🚣 조정 · 🏎️ F1
# 달팽이는 경마와 같은 틀 — 출전자 정보는 분위기용, 순위는 완전 랜덤, 상금표 합 0 (기대값 0).
# 조정은 내가 직접 노를 젓는다 — 구간마다 [노 젓기] 연타 수만큼 빨라지고, 물살(운)이 나머지 반.
#   끝까지 열심히 저으면 상대와 똑같은 조건(기대값 0), 덜 저을수록 불리하다. 목표 이상 연타는 더 빨라지지 않는다(매크로 방지).
# F1 은 2026 실제 드라이버 · 팀 · 서킷. 머신 · 드라이버 전력, 예선 그리드, 서킷 성격(추월 난이도 · 세이프티카 · 비)이
#   결과에 반영되고, 배당은 드라이버마다 시뮬레이션으로 재서 기대값이 0 밑이 되게 정한다.
#   중계는 실제 서킷 모양 위를 차들이 도는 그림 + 옆에 순위표 (서킷 좌표: assets/f1_circuits.json, MIT).
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
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Rectangle
import matplotlib.patheffects as pe

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
ROW_SEC = 3.0             # 구간 하나 길이 (초)
ROW_TARGET = 6            # 구간당 이만큼 저으면 전속력 (그 이상은 같음)
ROW_NOISE = 0.5           # 구간마다 물살 운 (±)
ROW_LAZY = 0.75           # 하나도 안 저었을 때 속도 (전속력 = 1)


def row_step(dist: list[float], me: int, taps: int, rng=random) -> float:
    """한 구간 진행. 상대는 1 ± 물살, 나는 (0.75 + 0.25 × 젓기 비율) ± 물살 — 전속력이면 상대와 똑같은 분포.
    덜 저을수록 불리하지만 물살 운으로 뒤집을 여지는 남는다 (반은 운). 젓기 비율을 돌려준다."""
    e = min(1.0, taps / ROW_TARGET)
    for i in range(len(dist)):
        dist[i] += (ROW_LAZY + (1 - ROW_LAZY) * e if i == me else 1.0) + rng.uniform(-ROW_NOISE, ROW_NOISE)
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
TRACKS = json.loads((Path(__file__).resolve().parent.parent / "assets" / "f1_circuits.json")
                    .read_text(encoding="utf-8"))["circuits"]
STRENGTH = [0.65 * TEAMS[d[3]][3] + 0.35 * d[4] for d in DRIVERS]
F1_TEMP = 2.4        # 전력 차이가 결과에 미치는 정도 (작을수록 강팀 독주)
GRID_W = 0.8         # 그리드 한 칸의 값 × 서킷 그리드 영향
POD_SHARE = 0.4      # 포디움(2~3위) 배수 = 우승 배수의 40%
F1_EDGE = 0.95       # 배당을 손익분기의 95%로
F1_CAP = 100.0
F1_SIMS = 12000
F1_STAGES = (0.02, 0.3, 0.55, 0.75, 0.98, 1.0)
F1_RULE = "우승 = 표시 배수 · 포디움(2~3위) = 그 40% · 4~10위 본전 · 11위 이하 1배 손실 · 💥 리타이어 2배 손실"
F1_LOGO = "https://upload.wikimedia.org/wikipedia/commons/thumb/3/33/F1.svg/330px-F1.svg.png"   # 공식 로고 (상표)
F1_SECTION = "🎙️ F1 중계"
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


def f1_race(grid: list[int], gp: tuple, rain: bool, sc: bool, rng=random) -> tuple[list[int], dict[int, float]]:
    """결승 → (완주 순위, {리타이어한 드라이버: 리타이어 시점 0~1})."""
    grip, street = gp[3], gp[6]
    noise = 1.0 + (0.5 if rain else 0.0) + (0.25 if sc else 0.0)
    p_out = 0.025 + (0.02 if street else 0.0) + (0.02 if rain else 0.0)
    n, out, scored = len(grid), {}, []
    for p, d in enumerate(grid):
        if rng.random() < p_out:
            out[d] = rng.uniform(0.05, 0.95)
        else:
            scored.append((STRENGTH[d] / F1_TEMP + grip * GRID_W * (n - 1 - p) + noise * _gumbel(rng), d))
    scored.sort(reverse=True)
    return [d for _, d in scored], out


def f1_odds(grid: list[int], gp: tuple, rain: bool, sims: int = F1_SIMS, rng=None) -> list[tuple[float, float]]:
    """드라이버별 (우승 배수, 포디움 배수). 결승을 sims 번 돌려 확률을 재고, 기대값이 0 밑이 되게 정한다.
    기대값 = P(우승)·우승배수 + P(2~3위)·포디움배수 − P(11위 이하) − 2·P(리타이어) ≤ 0
    시뮬레이션 오차로 배당이 부풀지 않게 이득 쪽은 넉넉히(+2σ), 손실 쪽은 박하게(−2σ) 잡는다 — 롱샷일수록 중요."""
    rng = rng or random.Random()
    c = [[0, 0, 0, 0] for _ in DRIVERS]          # 우승 · 포디움 · 11위 이하 · 리타이어
    for _ in range(sims):
        fin, out = f1_race(grid, gp, rain, rng.random() < gp[4] / 100, rng)
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


def f1_frames(grid: list[int], finish: list[int], out: dict[int, float], comeback: int | None, rng=random) -> list[list[int]]:
    """장면마다 달리는 순서. 그리드에서 출발해 갈수록 완주 순위로 모이고, 리타이어한 차는 그 시점부터 빠진다.
    comeback = 막판 두 장면에서 선두로 보이다 마지막에 추월당하는 차."""
    gpos = {d: p for p, d in enumerate(grid)}
    fpos = {d: p for p, d in enumerate(finish)}
    res = []
    for k, f in enumerate(F1_STAGES):
        if f >= 1:
            res.append(list(finish))
            continue
        alive = [d for d in grid if out.get(d, 2.0) > f]
        key = {d: (1 - f) * gpos[d] + f * fpos.get(d, gpos[d]) + rng.gauss(0, 1.5) * (1 - f) for d in alive}
        order = sorted(alive, key=key.get)
        if comeback is not None and k >= len(F1_STAGES) - 3:
            order.remove(comeback)
            order.insert(0, comeback)
        res.append(order)
    return res


def f1_gaps(n: int, sc: bool = False, rng=random) -> list[float]:
    """선두와의 간격(초). 세이프티카 장면은 다닥다닥 붙는다."""
    gaps = [0.0]
    for _ in range(n - 1):
        gaps.append(gaps[-1] + (rng.uniform(0.15, 0.5) if sc else rng.uniform(0.4, 2.4)))
    return gaps


def _drv(d: int, me: int) -> str:
    code, name, flag, team, _ = DRIVERS[d]
    s = f"{TEAMS[team][1]} {flag} {code} {name}"
    return f"**{s}** 👈" if d == me else s


def _track_xy(pts: list, frac: float) -> tuple[float, float]:
    t = (frac % 1.0) * len(pts)
    i, u = int(t) % len(pts), t - int(t)
    (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
    return x0 + u * (x1 - x0), y0 + u * (y1 - y0)


def f1_render(gp: tuple, head: str, order: list[int], gaps: list[float], gone: list[int], me: int, lead: float,
              sc: bool = False, rain: bool = False, odds=None, final: bool = False) -> bytes:
    """왼쪽: 실제 서킷 위를 도는 차들 · 오른쪽: 순위표(간격, 출발 그리드면 우승 배당). lead = 선두의 랩 위치(0~1)."""
    fig = Figure(figsize=(12, 6.6), dpi=90, facecolor=BG)
    FigureCanvasAgg(fig)
    pts = TRACKS[gp[1]]["pts"]
    stroke = [pe.withStroke(linewidth=3, foreground=BG)]

    ax = fig.add_axes((0.02, 0.03, 0.57, 0.80))
    ax.set_xlim(-0.06, 1.06)
    ax.set_ylim(-0.06, 1.06)
    ax.set_aspect("equal")
    ax.axis("off")
    xs, ys = [p[0] for p in pts] + [pts[0][0]], [p[1] for p in pts] + [pts[0][1]]
    ax.plot(xs, ys, color="#0B0B10", lw=17, solid_capstyle="round", solid_joinstyle="round", zorder=1)
    ax.plot(xs, ys, color="#FFD43B" if sc else "#4A4A5C", lw=10, solid_capstyle="round", solid_joinstyle="round", zorder=2)
    (x0, y0), (x1, y1) = pts[0], pts[1]
    nx, ny = y0 - y1, x1 - x0
    norm = math.hypot(nx, ny) or 1
    nx, ny = nx / norm * 0.035, ny / norm * 0.035
    ax.plot([x0 - nx, x0 + nx], [y0 - ny, y0 + ny], color="white", lw=4, zorder=3)     # 출발 · 결승선
    for p, d in reversed(list(enumerate(order))):                                        # 선두가 맨 위에 그려지게
        x, y = _track_xy(pts, lead - gaps[p] / LAP_SEC)
        mine = d == me
        ax.scatter([x], [y], s=330 if mine else 150, color=TEAMS[DRIVERS[d][3]][2], zorder=7 if mine else 5,
                   edgecolors="white" if mine else BG, linewidths=3 if mine else 1.2)
        if mine or p == 0:   # 이름표는 선두와 내 차만 (붙어 있으면 겹친다)
            ax.annotate(f"P{p + 1} {DRIVERS[d][0]}", (x, y), xytext=(0, 13), textcoords="offset points", ha="center",
                        color="#FFD43B" if mine else "white", fontsize=12 if mine else 10, fontweight="bold",
                        fontfamily=FONT, zorder=8, path_effects=stroke)

    fig.text(0.02, 0.935, f"{gp[1]}", color="white", fontsize=21, fontweight="bold", fontfamily=FONT)
    tags = [head] + (["SAFETY CAR"] if sc else []) + (["RAIN"] if rain else [])
    x, renderer = 0.02, fig.canvas.get_renderer()
    for t in tags:
        color = {"SAFETY CAR": ("#FFD43B", "black"), "RAIN": ("#3B82F6", "white")}.get(t, ("#E10600", "white"))
        txt = fig.text(x, 0.875, t, color=color[1], fontsize=12, fontweight="bold", fontfamily=FONT,
                       bbox={"boxstyle": "round,pad=0.35", "facecolor": color[0], "edgecolor": "none"})
        x += txt.get_window_extent(renderer).width / fig.bbox.width + 0.02

    rows = list(order) + list(gone)
    tw = fig.add_axes((0.615, 0.02, 0.37, 0.96))
    tw.set_xlim(0, 1)
    tw.set_ylim(len(rows) + 1.2, 0)
    tw.axis("off")
    tw.add_patch(Rectangle((0, 0), 1, len(rows) + 1.2, facecolor=PANEL, edgecolor="none"))
    tw.text(0.04, 0.75, "POS", color="#9A9AB0", fontsize=10, fontweight="bold", va="center", fontfamily=FONT)
    tw.text(0.2, 0.75, "DRIVER", color="#9A9AB0", fontsize=10, fontweight="bold", va="center", fontfamily=FONT)
    tw.text(0.96, 0.75, "우승 배당" if odds else "GAP", color="#9A9AB0", fontsize=10, fontweight="bold", va="center",
            ha="right", fontfamily=FONT)
    medal = {0: "#FFD43B", 1: "#D9D9D9", 2: "#E3A869"}
    for i, d in enumerate(rows):
        y = i + 1.75
        out = i >= len(order)
        if d == me:
            tw.add_patch(Rectangle((0.005, y - 0.5), 0.99, 1, facecolor="#E10600" if not out else "#5A1A1A", alpha=0.55,
                                   edgecolor="none"))
        tw.text(0.06, y, "OUT" if out else str(i + 1), color="#FF6B6B" if out else (medal.get(i, "white") if final else "white"),
                fontsize=11, fontweight="bold", va="center", ha="center", fontfamily=FONT)
        tw.add_patch(Rectangle((0.13, y - 0.36), 0.018, 0.72, facecolor=TEAMS[DRIVERS[d][3]][2], edgecolor="none"))
        tw.text(0.17, y, DRIVERS[d][0], color="white", fontsize=12, fontweight="bold", va="center", fontfamily=FONT)
        tw.text(0.33, y, DRIVERS[d][1], color="#C8C8D8", fontsize=10, va="center", fontfamily=FONT)
        if out:
            right = "리타이어"
        elif odds:
            right = f"+{odds[d][0]:g}배"
        else:
            right = "선두" if i == 0 else f"+{gaps[i]:.3f}"
        tw.text(0.96, y, right, color="#FF6B6B" if out else "white", fontsize=11, va="center", ha="right", fontfamily=FONT)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    return buf.getvalue()


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
                                         emoji=TEAMS[DRIVERS[d][3]][1],
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
        e.set_thumbnail(url=ui.emoji_url(emoji))
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
            race = {"no": random.randint(1, 12), "me": me, "boats": boats, "prizes": prizes(ROW_N)}
            lines = [f"`{i + 1}` 🚣 **{user.display_name}** 👈 · 내 보트" if b is None else
                     f"`{i + 1}` {b[0]} **{b[1]}** · {b[2]}kg · {random.choice(MOODS)}\n　 *{b[3]}*" for i, b in enumerate(boats)]
            e = ui.card(f"🚣 제{race['no']}경주 · 싱글 스컬 2000m",
                        '> 🎙️ *"출발하면 [🚣 노 젓기!]를 연타하세요! 많이 저을수록 빨라지지만, 물살은 반쯤 운이에요."*\n\n'
                        + "\n".join(lines) + f"\n\n**🏆 상금표** · `베팅` **{amount:,}원**\n" + prize_lines(amount, race["prizes"]),
                        ui.INFO, user, "🎙️ 조정 중계")
            e.set_thumbnail(url=ui.emoji_url("🚣"))
            e.set_footer(text=f"구간마다 {ROW_TARGET}번 이상 저으면 전속력 · 60초 안에 출발하지 않으면 실격")
            return (lambda: RowView(self, user, amount, race)), e, None
        await self._bet(interaction, amount, "🎙️ 조정 중계", build)

    async def _run_row(self, interaction: discord.Interaction, view: RowView):
        # ponytail: 경주 중(약 15초) 봇이 재시작되면 정산 전이라 베팅금이 사라진다 — 문제 되면 진행 중 경주를 DB에 남겨 재시작 때 환불
        race, amount, user, sec = view.race, view.amount, view.user, "🎙️ 조정 중계"
        me, boats = race["me"], race["boats"]
        names = [user.display_name if b is None else b[1] for b in boats]
        runners = [("🚣", n) if b is None else (b[0], n) for b, n in zip(boats, names)]
        title = f"🚣 제{race['no']}경주"
        dist = [0.0] * ROW_N
        await interaction.response.edit_message(embed=ui.card(
            f"{title} — 출발!", '> 🎙️ *"출발!! 지금 [🚣 노 젓기!]를 연타하세요!!"*\n\n' + lanes(runners, [0] * ROW_N, me),
            ui.DARK, user, sec), view=view)
        for k, seg in enumerate(ROW_SEGS):
            view.taps = 0
            await asyncio.sleep(ROW_SEC)
            taps = view.taps
            e = row_step(dist, me, taps)
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

    @app_commands.command(name="f1", description="2026 F1 그리드에서 드라이버를 골라 베팅! 실제 서킷 · 전력 반영, 배당은 드라이버마다 (최소 1,000원)")
    @app_commands.rename(amount="베팅액")
    @app_commands.describe(amount="베팅 금액 (최소 1,000원)")
    async def f1(self, interaction: discord.Interaction, amount: app_commands.Range[int, MIN_BET]):
        user, amount = interaction.user, int(amount)

        async def build():
            gp = random.choice(CIRCUITS)
            rain = random.random() < gp[5] / 100
            grid = f1_qualify(rain)
            odds = await asyncio.to_thread(f1_odds, grid, gp, rain)
            png = await asyncio.to_thread(f1_render, gp, "출발 그리드", grid, f1_gaps(len(grid), True), [], -1, 0.0,
                                          False, rain, odds)
            race = {"gp": gp, "rain": rain, "grid": grid, "odds": odds}
            flag, name, laps, _, _, _, _, note = gp
            weather = "🌧️ 빗속" if rain else "☀️ 맑은 날씨"
            e = ui.card(f"🏎️ {flag} {name} — 출발 그리드",
                        f'> 🎙️ *"{note}! {weather}에 {laps}랩 레이스가 곧 시작됩니다. 누구에게 거시겠어요?"*\n\n'
                        f"`베팅` **{amount:,}원**\n-# {F1_RULE}", ui.INFO, user, F1_SECTION)
            e.set_thumbnail(url=F1_LOGO)
            e.set_image(url="attachment://f1.png")
            e.set_footer(text="아래 목록에서 드라이버를 고르세요 · 60초 안에 안 고르면 실격")
            return (lambda: PickView(self, "f1", user, amount, race)), e, png
        await self._bet(interaction, amount, F1_SECTION, build)

    async def _run_f1(self, interaction: discord.Interaction, view: PickView, me: int):
        race, amount, user, sec = view.race, view.amount, view.user, F1_SECTION
        gp, grid = race["gp"], race["grid"]
        flag, gp_name, laps = gp[:3]
        sc = random.random() < gp[4] / 100
        finish, out = f1_race(grid, gp, race["rain"], sc)
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
        try:
            new_bal = await self.db.add_balance(user.id, amount + delta)
        except Exception as ex:
            return await interaction.followup.send(f"❌ DB 오류: {type(ex).__name__}")

        comeback = random.choice(finish[1:3]) if len(finish) >= 3 and random.random() < COMEBACK_PROB else None
        fr = f1_frames(grid, finish, out, comeback)
        nm = lambda d: DRIVERS[d][1]   # noqa: E731
        scenes, prev = [], 0.0
        for k, (f, order) in enumerate(zip(F1_STAGES, fr)):
            lap = max(1, round(f * laps))
            sc_now = k == 3 and sc
            gone = [d for d, t in sorted(out.items(), key=lambda x: x[1]) if t <= f]
            new_out = [d for d, t in out.items() if prev < t <= f]
            prev = f
            a, b = nm(order[0]), nm(order[1])
            if k == 0:
                stage, text = "🟢 소등!", f"소등! {a}, 깔끔한 스타트! {b}가 뒤를 바짝!"
            elif k == 1:
                stage, text = f"랩 {lap}/{laps}", f"{a} 선두를 지킵니다! {b}, DRS 거리 안!"
            elif k == 2:
                stage, text = f"랩 {lap}/{laps} · 피트스톱", f"피트스톱 러시! {a}, {random.uniform(1.9, 3.2):.1f}초 만에 타이어 교체!"
            elif sc_now:
                stage, text = f"🟡 세이프티카 · 랩 {lap}/{laps}", "세이프티카 출동! 간격이 전부 사라집니다… 재출발이 승부처!"
            elif k == 3:
                stage, text = f"랩 {lap}/{laps}", f"{a} 선두, {b} 바짝 추격!"
            elif k == 4:
                stage, text = f"🏁 파이널 랩 · 랩 {laps}/{laps}", f"파이널 랩!! {a}, 이대로 체커기를 받나요?!"
            elif comeback is not None:
                stage, text = "🔥 마지막 코너 대역전!!", f"마지막 코너!!! {a}, {nm(comeback)}를 제치고 역전 우승!!!"
            else:
                stage, text = "🏁 체커기", f"{a}, 체커기!! 우승!!"
            for d in new_out:
                text += f" 💥 {'내 드라이버 ' if d == me else ''}{nm(d)} 리타이어!"
            gaps = f1_gaps(len(order), sc_now)
            if me in order:
                p = order.index(me)
                status = f"👈 **{name}** 지금 **P{p + 1}**" + (" · 선두!!" if p == 0 else f" · +{gaps[p]:.3f}")
            else:
                status = f"💥 **{name}** 리타이어"
            head = "체커기" if f >= 1 else ("포메이션" if k == 0 else f"LAP {lap}/{laps}")
            lead = 0.0 if f >= 1 else (f * laps) % 1.0
            scenes.append((stage, f'> 🎙️ *"{text}"*\n\n{status}', (gp, head, order, gaps, gone, me, lead, sc_now, race["rain"],
                                                                  None, f >= 1),
                           ui.GOLD if (f >= 1 and comeback is not None) else ui.DARK))
        pngs = await asyncio.to_thread(lambda: [f1_render(*s[2]) for s in scenes])   # 그림은 미리 다 그려 둔다
        try:
            for k, ((stage, text, _, color), png) in enumerate(zip(scenes, pngs)):
                await asyncio.sleep(1.8 if k >= len(scenes) - 2 else 1.3)
                await self._show(interaction, card(stage, text, color), png)
            await asyncio.sleep(1.5)
        except discord.HTTPException:
            pass

        if rank is None:
            emoji, head, caster = "💥", "리타이어…", "머신이 멈춰 섰습니다… 리타이어. 2배 손실이에요."
        elif rank == 0:
            emoji, head, caster = "🏆", "우승!!", "체커기를 가장 먼저 받습니다! 탁월한 안목이에요!"
            if comeback is not None:
                head, caster = "역전 우승!!!", "마지막 코너에서 뒤집었습니다!! 이게 F1이죠!!"
        elif rank < 3:
            emoji, head, caster = "🍾", f"P{rank + 1} 포디움!", "포디움! 샴페인 파티입니다!"
            if me == comeback:
                caster = "다 잡은 우승을 마지막 코너에서 놓쳤습니다… 그래도 포디움!"
        elif rank < 10:
            emoji, head, caster = "🏁", f"P{rank + 1} 완주", "포인트권 완주 — 본전입니다."
        else:
            emoji, head, caster = "😶", f"P{rank + 1} 완주", "포인트권 밖… 1배 손실이에요."
        fl = random.choice(finish[:6])
        podium = " · ".join(f"{PLACES[i]} {DRIVERS[d][0]}" for i, d in enumerate(finish[:3]))
        e = ui.card(f"{emoji} {name} {head}",
                    f'> 🎙️ *"{caster}"*\n\n{podium}\n⚡ 패스티스트 랩: {DRIVERS[fl][0]} {nm(fl)}\n\n'
                    f"`정산` **{ui.won(delta)}** · {label(mult)}\n`잔액` **{new_bal:,}원**",
                    ui.GOLD if rank == 0 else ui.tone(delta), user, sec)
        e.set_thumbnail(url=F1_LOGO)
        e.set_image(url="attachment://f1.png")   # 마지막 장면(체커기 순위표)을 그대로 둔다
        try:
            await interaction.edit_original_response(embed=e)
        except discord.HTTPException:
            await interaction.followup.send(embed=e)


async def setup(bot: commands.Bot):
    await bot.add_cog(Race(bot))
