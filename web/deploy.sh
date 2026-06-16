#!/bin/bash
# 2026-05-17: Cloudflare Pages 자동 배포 스크립트 (웹 플레이리스트 관리)
# 환경변수 두 개만 export 후 실행:
#   export CLOUDFLARE_API_TOKEN="..."   # Pages:Edit + D1:Edit + Account Settings:Read
#   export CLOUDFLARE_ACCOUNT_ID="..."  # dash.cloudflare.com 우측 사이드바
# (선택)
#   export INITIAL_PASSWORD="..."        # 최초 비번 (미지정 시 자동 랜덤)

set -e

cd "$(dirname "$0")"

PROJECT_NAME="ayaya-playlist"

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ]; then
  echo "❌ CLOUDFLARE_API_TOKEN 환경변수가 비어있어요."; exit 1
fi
if [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
  echo "❌ CLOUDFLARE_ACCOUNT_ID 환경변수가 비어있어요."; exit 1
fi

echo "▶ 1/8 D1 데이터베이스 생성/조회..."
DB_OUT=$(npx wrangler d1 create ayaya_playlist 2>&1 || true)
DB_ID=$(echo "$DB_OUT" | grep -Eo '[a-f0-9-]{36}' | head -1 || true)
if [ -z "$DB_ID" ]; then
  echo "기존 DB 찾는 중..."
  LIST_OUT=$(npx wrangler d1 list 2>&1)
  DB_ID=$(echo "$LIST_OUT" | grep -E "ayaya_playlist" | grep -Eo '[a-f0-9-]{36}' | head -1 || true)
fi
if [ -z "$DB_ID" ]; then echo "❌ D1 database ID 추출 실패"; exit 1; fi
echo "✓ D1 database_id: $DB_ID"

echo "▶ 2/8 wrangler.toml database_id 갱신..."
sed -i.bak -E "s/database_id = \"[^\"]+\"/database_id = \"$DB_ID\"/" wrangler.toml && rm -f wrangler.toml.bak

echo "▶ 3/8 D1 마이그레이션 적용 (스키마/시드/보안/제목)..."
npx wrangler d1 execute ayaya_playlist --remote --file=./migrations/0001_init.sql
npx wrangler d1 execute ayaya_playlist --remote --file=./migrations/0002_seed.sql || true
npx wrangler d1 execute ayaya_playlist --remote --file=./migrations/0003_security.sql || true
npx wrangler d1 execute ayaya_playlist --remote --file=./migrations/0004_song_title.sql || true
npx wrangler d1 execute ayaya_playlist --remote --file=./migrations/0005_trash.sql || true

echo "▶ 4/8 Pages 프로젝트 생성/확인..."
npx wrangler pages project create "$PROJECT_NAME" --production-branch=main 2>&1 | tail -3 || true

echo "▶ 5/8 봇 전용 토큰 생성 및 secret 등록..."
BOT_TOKEN=$(openssl rand -hex 32)
echo "$BOT_TOKEN" | npx wrangler pages secret put BOT_TOKEN --project-name="$PROJECT_NAME"
if [ -n "${INITIAL_PASSWORD:-}" ]; then
  echo "$INITIAL_PASSWORD" | npx wrangler pages secret put INITIAL_PASSWORD --project-name="$PROJECT_NAME"
fi

echo "▶ 6/8 빌드 (esbuild + 정적 자산 복사)..."
npm install --silent
npm run build

echo "▶ 7/8 Pages 배포..."
DEPLOY_OUT=$(npx wrangler pages deploy dist --project-name="$PROJECT_NAME" --branch=main 2>&1)
echo "$DEPLOY_OUT" | tail -10
PROD_URL="https://${PROJECT_NAME}.pages.dev"

echo "▶ 8/8 wrangler.toml database_id 다시 placeholder로 복원 (공개 안전)..."
sed -i.bak -E "s/database_id = \"[^\"]+\"/database_id = \"REPLACE_WITH_DB_ID_AFTER_CREATE\"/" wrangler.toml && rm -f wrangler.toml.bak

echo ""
echo "========================================"
echo "✅ 배포 완료"
echo "========================================"
echo "1) /Users/medi/Music/config.py 갱신:"
echo "   WORKER_URL = \"$PROD_URL\""
echo "   BOT_TOKEN_FOR_WORKER = \"$BOT_TOKEN\""
echo ""
echo "2) 봇 재시작:"
echo "   kill \$(pgrep -f localmusicbot.py)  # run.sh가 자동 재시작"
echo ""
echo "3) 웹 접속: $PROD_URL"
echo "========================================"
