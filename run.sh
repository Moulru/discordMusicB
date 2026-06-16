#!/bin/bash
# 2026-05-16: Discord 음악봇 실행 스크립트
# 비정상 종료 시 5초 대기 후 자동 재시작 (네트워크 일시 끊김, YouTube 오류 등 대응)

cd "$(dirname "$0")"

# 로그 파일 (재시작 시 누적)
LOG_FILE="bot.log"

echo "==================== 봇 시작: $(date '+%Y-%m-%d %H:%M:%S') ====================" | tee -a "$LOG_FILE"

while true; do
    # caffeinate -i: 봇이 도는 동안 시스템 idle sleep 방지
    caffeinate -i ./venv/bin/python -u localmusicbot.py 2>&1 | tee -a "$LOG_FILE"
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 봇 종료 (code: $EXIT_CODE). 5초 후 재시작..." | tee -a "$LOG_FILE"
    sleep 5
done
