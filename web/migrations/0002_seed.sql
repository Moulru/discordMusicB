-- 샘플 시드: 처음 사용 시 참고용 (자유 음원 위주)
-- 본인의 곡으로 자유롭게 교체하세요. 웹 UI에서 추가/삭제 가능합니다.
INSERT OR IGNORE INTO playlists (id, name, position) VALUES
  ('1', 'Lofi',  1),
  ('2', 'NCS',   2);

-- Lofi (1) — Lofi Girl 공식 영상 등 자유 청취용
INSERT INTO songs (playlist_id, url, position, note) VALUES
  ('1', 'https://www.youtube.com/watch?v=jfKfPfyJRdk', 1, 'lofi hip hop radio - beats to relax/study to'),
  ('1', 'https://www.youtube.com/watch?v=4xDzrJKXOOY', 2, 'synthwave radio - beats to chill/game to');

-- NCS (2) — NoCopyrightSounds 트랙
INSERT INTO songs (playlist_id, url, position, note) VALUES
  ('2', 'https://www.youtube.com/watch?v=K4DyBUG242c', 1, 'Cartoon - On & On'),
  ('2', 'https://www.youtube.com/watch?v=bM7SZ5SBzyY', 2, 'Alan Walker - Fade'),
  ('2', 'https://www.youtube.com/watch?v=8X2kIfS6fb8', 3, 'NIVIRO - The Floor Is Lava');
