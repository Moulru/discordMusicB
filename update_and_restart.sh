#!/bin/bash
# 2026-05-16: 매일 새벽 05:00 (KST) 실행되는 업데이트 + 재시작 스크립트
# launchd com.medi.musicbot-update.plist에서 호출됨

cd "$(dirname "$0")"

LOG="update.log"
TMUX=/opt/homebrew/bin/tmux
PIP=./venv/bin/pip
SESSION_NAME="musicbot"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

log "===================== 일일 업데이트 시작 ====================="

# 1) 봇 종료
if "$TMUX" has-session -t "$SESSION_NAME" 2>/dev/null; then
    log "tmux 세션 '$SESSION_NAME' 종료 중..."
    "$TMUX" kill-session -t "$SESSION_NAME"
    sleep 2
else
    log "tmux 세션 '$SESSION_NAME' 미실행 상태."
fi

# 2) 업데이트 대상 패키지 (봇이 의존하는 핵심만)
TARGETS=("discord.py" "yt-dlp" "PyNaCl")

# 3) 업데이트 가능 목록 확인
log "pip --outdated 확인 중..."
OUTDATED=$($PIP list --outdated --format=columns 2>>"$LOG")
echo "$OUTDATED" | tee -a "$LOG"

# 4) 대상 중 업데이트 가능한 것만 골라서 설치
NEEDS_UPDATE=()
for pkg in "${TARGETS[@]}"; do
    # case-insensitive 매칭 (discord.py 등 케이스 다양)
    if echo "$OUTDATED" | awk '{print tolower($1)}' | grep -qx "$(echo "$pkg" | tr 'A-Z' 'a-z')"; then
        NEEDS_UPDATE+=("$pkg")
    fi
done

if [ ${#NEEDS_UPDATE[@]} -eq 0 ]; then
    log "업데이트할 패키지 없음. 현재 버전 유지."
else
    log "업데이트 대상: ${NEEDS_UPDATE[*]}"
    $PIP install -U "${NEEDS_UPDATE[@]}" 2>&1 | tee -a "$LOG"
    log "업데이트 완료."
fi

# 5) 봇 재시작
log "봇 재시작 중..."
./start_bot.sh 2>&1 | tee -a "$LOG"

log "===================== 일일 업데이트 종료 ====================="
echo "" | tee -a "$LOG"
