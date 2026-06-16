-- 2026-05-17: 보안 강화 — 로그인 시도 기록 (rate limit)
-- IP 단위로 잠금/시도 횟수 관리. 잠금은 fail이 N회 이상이면 LOCKOUT_UNTIL 적용.
CREATE TABLE IF NOT EXISTS login_attempts (
  ip TEXT PRIMARY KEY,
  fail_count INTEGER NOT NULL DEFAULT 0,
  first_fail_at INTEGER NOT NULL,
  last_fail_at INTEGER NOT NULL,
  locked_until INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_locked ON login_attempts(locked_until);
