-- 2026-05-17: 휴지통 (soft delete) — 플레이리스트 삭제 시 3일 보관 후 영구 삭제
-- 0001_init.sql에 이미 포함됨. 기존 환경 호환용 (idempotent — ALTER 실패는 deploy.sh가 무시).
ALTER TABLE playlists ADD COLUMN deleted_at INTEGER;
CREATE INDEX IF NOT EXISTS idx_playlists_deleted_at ON playlists(deleted_at);
