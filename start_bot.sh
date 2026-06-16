#!/bin/bash
# 2026-05-16: tmux 세션 시작 헬퍼 (idempotent - 이미 떠있으면 스킵)
# launchd에서 로그인 시 자동 호출됨

cd "$(dirname "$0")"

SESSION_NAME="musicbot"

# 이미 같은 이름 세션이 있으면 종료
if /opt/homebrew/bin/tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] tmux 세션 '$SESSION_NAME' 이미 실행 중. 스킵."
    exit 0
fi

# 새 detached 세션으로 봇 실행
/opt/homebrew/bin/tmux new-session -d -s "$SESSION_NAME" -c "$(pwd)" './run.sh'
echo "[$(date '+%Y-%m-%d %H:%M:%S')] tmux 세션 '$SESSION_NAME' 시작 완료."
