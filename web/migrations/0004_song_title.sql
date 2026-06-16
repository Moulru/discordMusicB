-- 2026-05-17: 곡 제목 자동 캐시 (YouTube oEmbed로 fetch 후 저장)
-- 0001_init.sql에 이미 포함됨. 기존 환경 호환용 (idempotent — ALTER 실패는 deploy.sh가 무시).
ALTER TABLE songs ADD COLUMN title TEXT;
