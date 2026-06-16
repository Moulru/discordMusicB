# Discord Music Bot + Web Playlist Manager

YouTube 음원을 Discord 음성 채널에서 재생하는 봇과, **Cloudflare Pages + D1** 기반의 **웹 플레이리스트 관리 페이지**를 함께 제공합니다.

- 🎵 Discord 봇: discord.py + yt-dlp
- 🌐 웹 UI: Cloudflare Pages, 라이트/다크 모드 + 반응형
- 🔐 일일 자동 갱신 공유 비밀번호 인증 (소수 그룹용)
- 🔄 25초 폴링 + 낙관적 잠금으로 다인 동시 편집 안전
- 🛡 보안 헤더(CSP·X-Frame·HSTS 등) + rate limit + CSRF 차단

---

## 주요 기능

### Discord 봇

| 명령어 | 설명 |
|---|---|
| `!play <검색어/URL>` | 음악 재생 / 대기열 추가 |
| `!play1 <검색어/URL>` | 다음 곡으로 우선예약 |
| `!playlist <id>` | 플레이리스트 순서대로 재생 (대소문자 무관) |
| `!playlist <id> shuffle` | 셔플 재생 |
| `!playlists` | 등록된 플레이리스트 목록 |
| `!list` | 현재 대기열 표시 |
| `!skip` | 다음 곡으로 스킵 |
| `!stop` | 정지 + 음성 채널 퇴장 |
| `!autoplay` | 대기열 비면 관련곡 자동 추가 토글 |

**Now Playing 카드** — 곡이 바뀔 때 같은 메시지를 edit, 6개 버튼 부착:
- `⏮ 이전곡` / `⏸ 일시정지` / `⏭ 다음곡` / `⏹ 정지` (빨간 강조)
- `✏️ 플레이리스트 편집` (클릭 시 일일 공유 비밀번호 + 웹 URL을 카드 안에 표시, 다음 3곡 동안)
- `🔀 플레이리스트 섞기` (현재 큐만 셔플, 재생 중 곡 영향 없음)

**자동 동작**:
- **멀티 서버 지원** — 서버별 독립된 큐/상태
- **NP 메시지를 항상 채널 최하단에 유지** — 다른 채팅 올라오면 1초 debounce 후 재배치
- **빈 채널 자동 종료** — 사람이 모두 나가면 봇도 자동 퇴장
- **봇 강제 disconnect 감지** — 관리자에 의해 kick 시 state 정리
- **곡 종료 시 자동 채널 퇴장** (서버는 유지)
- **사용자 다른 음성채널 이동 시 자동 따라감**
- **첫 곡 로딩 실패 시 채널 유지** — 다음 명령 대기 (자동 퇴장 X)
- **YouTube 영상 재생 실패 시 자동 검색 폴백** — oEmbed로 제목 추출 후 같은 제목으로 일반 YouTube 검색해 재생 (Music Premium 전용 등 차단된 영상 우회)

### 웹 페이지

- 플레이리스트 / 곡 CRUD (이름 변경 = 명령어 식별자 변경)
- 플레이리스트당 곡 상한 **100개**
- 곡 추가 시 YouTube oEmbed로 영상 제목 자동 캐시 (제목 30자 + …)
- ☐ 랜덤 재생 체크 + 📋 재생 명령 복사 → Discord 채팅에 붙여넣기로 즉시 재생
- 25초 폴링 + 낙관적 잠금(version)으로 동시 편집 충돌 안전 처리
- 매일 한국시간 00:00 비밀번호 자동 갱신 (lazy rotation)
- **🌗 라이트/다크 모드** (GitHub Dark 톤, localStorage 캐시, 기본 라이트)
- **🗑 휴지통 (soft delete)** — 플레이리스트 삭제 시 3일 보관 후 영구 삭제. 그 전엔 복원 가능
- 라이트/다크 자유 전환, 모바일 반응형

---

## 아키텍처

```
[브라우저] ──HTTPS──> [Cloudflare Pages: 정적 HTML + Worker API] ──> [Cloudflare D1]
                                                                       ↑
                                           [Discord 봇이 봇 토큰으로 GET]
```

- **Discord 봇**: Python ([localmusicbot.py](localmusicbot.py))
- **Worker API**: TypeScript ([web/src/index.ts](web/src/index.ts)) — esbuild로 번들
- **DB**: Cloudflare D1 (SQLite)
- **프론트**: 단일 HTML ([web/static/index.html](web/static/index.html)) + Tailwind CDN + Vanilla JS

---

## 설치 가이드

### 사전 요구사항

- macOS / Linux
- Python 3.11+ / pip
- Node.js 18+ / npm
- ffmpeg (`brew install ffmpeg`)
- Cloudflare 계정 (무료 티어로 충분)
- Discord 봇 토큰 ([Discord Developer Portal](https://discord.com/developers/applications))

### 1. 클론 & Python 환경 준비

```bash
git clone https://github.com/Moulru/discordMusicB.git
cd discordMusicB

python3 -m venv venv
source venv/bin/activate
pip install discord.py yt-dlp PyNaCl
```

### 2. Discord 봇 설정

1. [Discord Developer Portal](https://discord.com/developers/applications)에서 봇 생성
2. **Bot → Privileged Gateway Intents → Message Content Intent** 활성화
3. 봇 초대 URL 생성 (OAuth2 → URL Generator → `bot` scope + 음성 채널 권한)

### 3. Cloudflare Pages 배포

```bash
cd web

# Cloudflare 인증 (한 번만)
export CLOUDFLARE_API_TOKEN="..."   # 필요 권한: Workers Scripts:Edit, Cloudflare Pages:Edit, D1:Edit, Account Settings:Read
export CLOUDFLARE_ACCOUNT_ID="..."  # dash.cloudflare.com 우측 사이드바
# (선택) 최초 비번 지정 (안 주면 랜덤 생성)
# export INITIAL_PASSWORD="원하는비번"

./deploy.sh
```

스크립트가 자동으로 다음 단계 진행:
1. D1 데이터베이스 생성 + ID 자동 주입
2. 스키마/시드/보안/제목/휴지통 마이그레이션 적용
3. Pages 프로젝트 생성
4. 봇 전용 토큰 64자 hex 자동 생성 + Pages secret으로 등록
5. esbuild로 Worker 번들 + 정적 자산 복사
6. `wrangler pages deploy`로 배포
7. **`config.py`에 넣을 URL/토큰을 출력**

> Pages 프로젝트 이름을 변경하려면 `web/wrangler.toml`의 `name`과 `web/deploy.sh`의 `PROJECT_NAME`을 함께 수정하세요.

### 4. config.py 작성

```bash
cd ..
cp config.example.py config.py
# config.py 편집 — Discord 토큰 + Pages URL + 봇 토큰 (deploy.sh 출력값) 입력
```

### 5. 봇 실행

```bash
./run.sh
# 또는 tmux 세션으로 백그라운드 실행:
# ./start_bot.sh
```

`run.sh`는 무한 재시작 루프를 돌려 봇이 비정상 종료되면 5초 후 자동 재시작합니다.

---

## 사용 흐름

1. Discord 음성 채널 입장 후 `!play <검색어>` 또는 `!playlist <id>`
2. 봇이 **Now Playing 카드** 표시 (6개 버튼 부착)
3. ✏️ **플레이리스트 편집** 버튼 클릭 → 카드에 비밀번호 + 웹 URL 표시 (이후 3곡 동안 유지)
4. 웹 페이지에서 비번 입력 → 플레이리스트 / 곡 CRUD
5. 곡 추가 후 웹에서 ☐ 랜덤 재생 체크 + 📋 복사 → Discord 채팅에 붙여넣기 → 대기열에 추가됨

---

## 디렉토리 구조

```
.
├── localmusicbot.py        # Discord 봇 본체
├── config.example.py       # config.py 템플릿
├── run.sh                  # 봇 실행 스크립트 (자동 재시작 루프)
├── start_bot.sh            # tmux 세션 시작 헬퍼
├── update_and_restart.sh   # 매일 새벽 의존성 업데이트 + 봇 재시작
├── web/                    # Cloudflare Pages (Worker + 정적 HTML)
│   ├── src/index.ts        # Worker 본체 (API + 인증 + 보안 + 라우팅)
│   ├── static/index.html   # 프론트엔드 (Tailwind CDN + Vanilla JS)
│   ├── migrations/         # D1 스키마 + 샘플 시드
│   │   ├── 0001_init.sql
│   │   ├── 0002_seed.sql   # 샘플 (Lofi/NCS)
│   │   ├── 0003_security.sql
│   │   ├── 0004_song_title.sql
│   │   └── 0005_trash.sql
│   ├── deploy.sh           # 자동 배포 스크립트
│   ├── wrangler.toml       # Wrangler 설정
│   ├── package.json        # esbuild + wrangler
│   └── tsconfig.json
└── README.md
```

---

## 보안 정리

- 사용자 인증: 일일 공유 비밀번호 (KST 00:00 자동 회전, lazy 회전 — Pages는 Cron 미지원)
- 봇 인증: 별도 64자 hex 토큰 (timing-safe 비교)
- Rate limit: 로그인 IP당 5회 실패 시 5분 잠금
- CSRF: state-changing 요청에 Origin/Referer 헤더 검증
- XSS: 모든 사용자 입력에 escapeHtml + URL 스킴 화이트리스트(`http(s)://` + YouTube 도메인만)
- 입력 길이 제한: id ≤32자, name ≤100자, note ≤500자, URL ≤500자
- 플레이리스트당 곡 상한 100개
- 보안 헤더: CSP, X-Frame-Options=DENY, X-Content-Type-Options=nosniff, Referrer-Policy, Permissions-Policy
- 봇 → Worker: 명시적 User-Agent (Cloudflare Bot Fight Mode 우회)

---

## 기술 결정 메모

- **D1 vs KV**: 즉시 일관성 + 관계형 모델 + 무료 티어 모두 충족 → D1
- **인증**: Cloudflare Access 대신 단순 공유 비밀번호 — 소수(<10명) 그룹 가정
- **폴링 vs WebSocket**: Durable Objects 비용 + 복잡도 ↑ → 25초 폴링이 가성비 최적
- **낙관적 잠금**: 플레이리스트 단위 `version` 컬럼으로 충돌 감지 (HTTP 409)
- **NP 메시지 동시성**: `asyncio.Lock`으로 update / reattach / delete 직렬화
- **재귀 → 루프**: `check_queue`를 while 루프로 — 연속 실패 시 stack overflow 방지
- **휴지통 lazy GC**: Pages Cron 미지원 → listPlaylists 호출 시 10% 확률 GC

---

## 라이선스

MIT License. 자세한 내용은 [LICENSE](./LICENSE) 참조.

---

## 크레딧

- [discord.py](https://github.com/Rapptz/discord.py)
- [yt-dlp](https://github.com/yt-dlp/yt-dlp)
- [Cloudflare Workers / Pages / D1](https://workers.cloudflare.com/)
- [Tailwind CSS](https://tailwindcss.com/)
