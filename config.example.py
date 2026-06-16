# Discord 봇 토큰 및 Cloudflare Worker 연동 설정
# 이 파일을 `config.py`로 복사한 뒤 실제 값을 채워주세요.
# config.py 는 .gitignore 에 포함되어 절대 깃에 올라가지 않습니다.

# Discord Developer Portal → Bot → Reset Token 으로 발급
DISCORD_TOKEN = "YOUR_DISCORD_BOT_TOKEN"

# Cloudflare Pages 배포 후 정해지는 URL + 봇 전용 secret 토큰
# (web/deploy.sh 출력에서 자동 안내됨)
WORKER_URL = "https://YOUR-PROJECT.pages.dev"
BOT_TOKEN_FOR_WORKER = "GENERATED_BY_DEPLOY_SCRIPT"  # 64자 hex 권장
