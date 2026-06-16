-- 2026-05-17: Playlist 초기 스키마
-- 플레이리스트: id는 사용자가 봇 명령어로 쓰는 짧은 식별자 ("JP", "KR" 등)
-- version: 낙관적 잠금용
CREATE TABLE IF NOT EXISTS playlists (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
  deleted_at INTEGER  -- 휴지통: NULL이면 활성, 값이면 휴지통 이동 시각. 3일 후 영구 삭제.
);
CREATE INDEX IF NOT EXISTS idx_playlists_deleted_at ON playlists(deleted_at);

CREATE TABLE IF NOT EXISTS songs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  playlist_id TEXT NOT NULL,
  url TEXT NOT NULL,
  position INTEGER NOT NULL DEFAULT 0,
  note TEXT,
  title TEXT,  -- YouTube oEmbed로 자동 fetch한 영상 제목
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_songs_playlist ON songs(playlist_id, position);

-- 인증: 일일 공유 비밀번호
CREATE TABLE IF NOT EXISTS auth (
  key TEXT PRIMARY KEY,
  password TEXT NOT NULL,
  valid_from INTEGER NOT NULL,
  valid_until INTEGER NOT NULL,
  rotated_at INTEGER NOT NULL DEFAULT (unixepoch())
);

-- 웹 세션 (쿠키 토큰)
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL DEFAULT (unixepoch()),
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
