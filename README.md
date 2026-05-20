# Discord 축구 봇

디스코드 서버를 위한 축구 전용 봇입니다. 이적시장 정보, 경기 일정, 선수 카드 거래 등 다양한 기능을 제공합니다.

## 주요 기능

- **이적시장 임베드** — Here We Go / 오피셜 / 속보 스타일 이적 정보 전송
- **경기 일정** — 최신 경기 일정 조회
- **선수 카드 시장** — 선수 카드 구매·판매·팩 개봉·시세 그래프
- **토토** — 경기 결과 예측 베팅
- **클럽 관리** — 서버 내 클럽 생성 및 관리
- **겨울 이적시장 결산** — 페이지 넘김 방식 결산 뷰어

## 설치 및 실행

### 필수 조건

- Python 3.11+
- Discord 봇 토큰 및 서버 권한 (`applications.commands`, `bot`)

### 설정

```bash
pip install -r requirements.txt
```

`.env` 파일 생성:

```
DISCORD_TOKEN=your_bot_token_here
DISCORD_OWNER_ID=your_discord_user_id
FOOTBALL_API_KEY=your_api_key_here   # 경기 일정 기능 사용 시
```

### 실행

```bash
python bot.py
```

## 프로젝트 구조

```
bot.py                  # 메인 봇 진입점, 공통 슬래시 명령어
auth.py                 # 오너 전용 권한 체크
cogs/
  fixtures.py           # 경기 일정
  economy.py            # 재화 시스템
  toto.py               # 토토 베팅
  players_market.py     # 선수 카드 시장
  club.py               # 클럽 관리
  tutorial.py           # 튜토리얼
services/
  football_api.py       # 외부 축구 API 클라이언트
  economy_db.py         # 재화 DB
  player_market_db.py   # 선수 시장 DB
  club_db.py            # 클럽 DB
```

## 슬래시 명령어 동기화

봇을 처음 실행하면 등록된 서버에 자동으로 명령어가 동기화됩니다.  
수동으로 재동기화가 필요하면 `/동기화` 명령어를 사용하세요 (오너 전용).
