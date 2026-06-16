// 2026-05-17: Cloudflare Worker (D1 백엔드 + 정적 HTML 서빙) — 웹 플레이리스트 관리
// 인증: 일일 공유 비밀번호 (사용자) + 봇 전용 secret 토큰 (Discord 봇)
// 2026-05-17: 보안 강화 — rate limit, CSRF (Origin 검증), 세션 회전,
//             constant-time 비교, 보안 헤더, 입력 길이 제한, URL 스킴 화이트리스트

export interface Env {
  DB: D1Database;
  ASSETS: Fetcher;
  BOT_TOKEN: string;
  INITIAL_PASSWORD?: string;
  SESSION_TTL_HOURS: string;
}

// ──────────────────────────────────────────────
// 보안 상수
// ──────────────────────────────────────────────
const MAX_NAME_LEN = 100;
const MAX_NOTE_LEN = 500;
const MAX_URL_LEN = 500;
const MAX_SONGS_PER_PLAYLIST = 100;  // 2026-05-17: 플레이리스트당 곡 상한
const TRASH_RETENTION_SEC = 3 * 24 * 3600;  // 2026-05-17: 휴지통 보관 기간 (3일)
const MAX_LOGIN_FAILS = 5;       // 5회 실패 시 잠금
const LOGIN_FAIL_WINDOW = 300;   // 5분 슬라이딩 윈도우
const LOGIN_LOCKOUT_SEC = 300;   // 잠금 5분
const MIN_LOGIN_RESPONSE_MS = 200; // 타이밍 일정화 최소 응답 시간

// 같은 origin으로 인정할 호스트 (state-changing 요청 시 Origin 헤더 비교)
function isSameOrigin(req: Request): boolean {
  const origin = req.headers.get("Origin");
  const referer = req.headers.get("Referer");
  const host = req.headers.get("Host");
  if (!host) return false;
  // Origin이 있으면 우선 비교 (CORS 표준)
  if (origin) {
    try {
      return new URL(origin).host === host;
    } catch {
      return false;
    }
  }
  // Origin 없는 경우 Referer 폴백
  if (referer) {
    try {
      return new URL(referer).host === host;
    } catch {
      return false;
    }
  }
  // 둘 다 없으면 state-changing 요청 거부 (브라우저는 보통 둘 중 하나는 보냄)
  return false;
}

// 타이밍 안전 문자열 비교
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    // 길이 다르면 무조건 false이지만, 가짜 비교 한 번 돌려 타이밍 정보 누설 방지
    let dummy = 0;
    for (let i = 0; i < a.length; i++) dummy |= a.charCodeAt(i);
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

// ──────────────────────────────────────────────
// 유틸: 랜덤 비번 / 세션 토큰
// ──────────────────────────────────────────────
function randomPassword(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
}

function randomSessionToken(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function now(): number {
  return Math.floor(Date.now() / 1000);
}

function nextRotationUnix(fromUnix: number): number {
  const date = new Date(fromUnix * 1000);
  const utcHour = date.getUTCHours();
  const next = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), 15, 0, 0));
  if (utcHour >= 15) next.setUTCDate(next.getUTCDate() + 1);
  return Math.floor(next.getTime() / 1000);
}

// 클라이언트 IP — CF가 자동 주입
function clientIP(req: Request): string {
  return req.headers.get("CF-Connecting-IP") || req.headers.get("X-Forwarded-For")?.split(",")[0].trim() || "unknown";
}

// ──────────────────────────────────────────────
// 비번 가져오기 / 회전
// ──────────────────────────────────────────────
async function getOrCreatePassword(env: Env): Promise<{ password: string; valid_until: number }> {
  const row = await env.DB.prepare(
    "SELECT password, valid_from, valid_until FROM auth WHERE key = 'daily_password'"
  ).first<{ password: string; valid_from: number; valid_until: number }>();

  const t = now();
  if (row && row.valid_until > t) {
    return { password: row.password, valid_until: row.valid_until };
  }
  const newPassword = env.INITIAL_PASSWORD && !row ? env.INITIAL_PASSWORD : randomPassword();
  const validUntil = nextRotationUnix(t);
  await env.DB.prepare(
    `INSERT INTO auth (key, password, valid_from, valid_until, rotated_at)
     VALUES ('daily_password', ?, ?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET password=excluded.password, valid_from=excluded.valid_from, valid_until=excluded.valid_until, rotated_at=excluded.rotated_at`
  )
    .bind(newPassword, t, validUntil, t)
    .run();
  await env.DB.prepare("DELETE FROM sessions").run();
  return { password: newPassword, valid_until: validUntil };
}

async function rotatePassword(env: Env): Promise<void> {
  const newPassword = randomPassword();
  const t = now();
  const validUntil = nextRotationUnix(t);
  await env.DB.prepare(
    `INSERT INTO auth (key, password, valid_from, valid_until, rotated_at)
     VALUES ('daily_password', ?, ?, ?, ?)
     ON CONFLICT(key) DO UPDATE SET password=excluded.password, valid_from=excluded.valid_from, valid_until=excluded.valid_until, rotated_at=excluded.rotated_at`
  )
    .bind(newPassword, t, validUntil, t)
    .run();
  await env.DB.prepare("DELETE FROM sessions").run();
}

// ──────────────────────────────────────────────
// Rate limit (로그인)
// ──────────────────────────────────────────────
async function checkLoginAllowed(env: Env, ip: string): Promise<{ allowed: boolean; retryAfter?: number }> {
  const row = await env.DB.prepare(
    "SELECT fail_count, first_fail_at, locked_until FROM login_attempts WHERE ip = ?"
  )
    .bind(ip)
    .first<{ fail_count: number; first_fail_at: number; locked_until: number }>();
  const t = now();
  if (!row) return { allowed: true };
  if (row.locked_until > t) return { allowed: false, retryAfter: row.locked_until - t };
  return { allowed: true };
}

async function recordLoginFail(env: Env, ip: string): Promise<void> {
  const t = now();
  // 2% 확률로 30일 이상 묵은 로그인 시도 기록 정리 (lazy GC, cron 대체)
  if (Math.random() < 0.02) {
    await env.DB.prepare("DELETE FROM login_attempts WHERE last_fail_at < ?")
      .bind(t - 30 * 86400)
      .run();
  }
  const row = await env.DB.prepare(
    "SELECT fail_count, first_fail_at, locked_until FROM login_attempts WHERE ip = ?"
  )
    .bind(ip)
    .first<{ fail_count: number; first_fail_at: number; locked_until: number }>();

  if (!row) {
    await env.DB.prepare(
      "INSERT INTO login_attempts (ip, fail_count, first_fail_at, last_fail_at, locked_until) VALUES (?, 1, ?, ?, 0)"
    )
      .bind(ip, t, t)
      .run();
    return;
  }

  // 2026-05-17: 잠금 중엔 추가 시도를 카운트하지 않음 (잠금 영구 연장 방지).
  // last_fail_at만 갱신 (GC 회피용).
  if (row.locked_until > t) {
    await env.DB.prepare("UPDATE login_attempts SET last_fail_at = ? WHERE ip = ?").bind(t, ip).run();
    return;
  }

  // 슬라이딩 윈도우: first_fail_at이 윈도우 밖이면 카운트 리셋
  let failCount = row.fail_count;
  let firstFailAt = row.first_fail_at;
  if (t - row.first_fail_at > LOGIN_FAIL_WINDOW) {
    failCount = 0;
    firstFailAt = t;
  }
  failCount += 1;
  const lockedUntil = failCount >= MAX_LOGIN_FAILS ? t + LOGIN_LOCKOUT_SEC : 0;

  await env.DB.prepare(
    "UPDATE login_attempts SET fail_count = ?, first_fail_at = ?, last_fail_at = ?, locked_until = ? WHERE ip = ?"
  )
    .bind(failCount, firstFailAt, t, lockedUntil, ip)
    .run();
}

async function recordLoginSuccess(env: Env, ip: string): Promise<void> {
  await env.DB.prepare("DELETE FROM login_attempts WHERE ip = ?").bind(ip).run();
}

// ──────────────────────────────────────────────
// 인증 검증
// ──────────────────────────────────────────────
function verifyBotAuth(req: Request, env: Env): boolean {
  const auth = req.headers.get("Authorization");
  if (!auth || !auth.startsWith("Bearer ")) return false;
  const token = auth.slice("Bearer ".length).trim();
  if (token.length === 0) return false;
  return timingSafeEqual(token, env.BOT_TOKEN);
}

async function verifyUserSession(req: Request, env: Env): Promise<boolean> {
  const cookie = req.headers.get("Cookie") || "";
  const match = cookie.match(/session_id=([a-f0-9]+)/);
  if (!match) return false;
  const token = match[1];
  const row = await env.DB.prepare(
    "SELECT expires_at FROM sessions WHERE token = ?"
  )
    .bind(token)
    .first<{ expires_at: number }>();
  if (!row) return false;
  if (row.expires_at < now()) {
    await env.DB.prepare("DELETE FROM sessions WHERE token = ?").bind(token).run();
    return false;
  }
  return true;
}

// ──────────────────────────────────────────────
// 응답 헬퍼 (보안 헤더 적용)
// ──────────────────────────────────────────────
const SECURITY_HEADERS: HeadersInit = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
  // CSP — Tailwind CDN(cdn.tailwindcss.com), YouTube 링크는 a href만 (script/img 외부 로딩 없음)
  "Content-Security-Policy": [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com",
    "style-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com https://fonts.googleapis.com",
    "img-src 'self' data:",
    "font-src 'self' data: https://fonts.gstatic.com",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; "),
};

// ASSETS binding 응답은 immutable headers를 갖고 있어 stream 그대로 넘기면 헤더 적용 안 됨.
// arrayBuffer로 받아 새 Response를 만들어야 헤더가 확실히 적용됨.
async function applySecurityHeaders(res: Response): Promise<Response> {
  const headers = new Headers(res.headers);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) headers.set(k, v as string);
  // 2026-05-17: HTML은 새 배포 즉시 반영되도록 캐시 비활성 (no-cache + must-revalidate)
  const ct = headers.get("Content-Type") || "";
  if (ct.startsWith("text/html") && !headers.has("Cache-Control")) {
    headers.set("Cache-Control", "no-cache, must-revalidate");
  }
  // 304 등 body 없는 응답은 그대로
  if (res.status === 204 || res.status === 304) {
    return new Response(null, { status: res.status, statusText: res.statusText, headers });
  }
  const body = await res.arrayBuffer();
  return new Response(body, {
    status: res.status,
    statusText: res.statusText,
    headers,
  });
}

function json(data: unknown, init?: ResponseInit): Response {
  return new Response(JSON.stringify(data), {
    ...init,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Cache-Control": "no-store",
      ...(init?.headers || {}),
    },
  });
}

function err(status: number, message: string): Response {
  return json({ error: message }, { status });
}

// ──────────────────────────────────────────────
// 입력 검증 헬퍼
// ──────────────────────────────────────────────
function validatePlaylistId(id: unknown): string | null {
  if (typeof id !== "string") return null;
  const trimmed = id.trim();
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(trimmed)) return null;
  return trimmed;
}

function validateName(name: unknown): string | null {
  if (typeof name !== "string") return null;
  const trimmed = name.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_NAME_LEN) return null;
  return trimmed;
}

function validateNote(note: unknown): string | null | undefined {
  if (note === null || note === undefined) return note as null | undefined;
  if (typeof note !== "string") return undefined;
  const trimmed = note.trim();
  if (trimmed.length === 0) return null;
  if (trimmed.length > MAX_NOTE_LEN) return undefined;
  return trimmed;
}

// 2026-05-17: YouTube oEmbed로 영상 제목 fetch (CF cache 24h)
async function fetchYouTubeTitle(url: string): Promise<string | null> {
  try {
    const oembedUrl = `https://www.youtube.com/oembed?url=${encodeURIComponent(url)}&format=json`;
    const res = await fetch(oembedUrl, {
      headers: { "Accept": "application/json" },
      cf: { cacheTtl: 86400, cacheEverything: true } as any,
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { title?: string };
    return (data.title || "").trim() || null;
  } catch {
    return null;
  }
}

// YouTube URL만 허용 (javascript:, data:, file:, 임의 도메인 차단)
function validateYouTubeUrl(url: unknown): string | null {
  if (typeof url !== "string") return null;
  const trimmed = url.trim();
  if (trimmed.length === 0 || trimmed.length > MAX_URL_LEN) return null;
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return null;
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return null;
  const host = parsed.hostname.toLowerCase();
  const allowed = ["youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"];
  if (!allowed.includes(host)) return null;
  return trimmed;
}

// ──────────────────────────────────────────────
// 플레이리스트 조회
// ──────────────────────────────────────────────
// 2026-05-17: 휴지통 3일 지난 항목 영구 삭제 (lazy GC). list 호출 시 10% 확률 실행.
async function gcTrash(env: Env): Promise<void> {
  const cutoff = now() - TRASH_RETENTION_SEC;
  // CASCADE FK가 비활성 환경 대비 명시적 songs 정리
  const expired = await env.DB.prepare(
    "SELECT id FROM playlists WHERE deleted_at IS NOT NULL AND deleted_at < ?"
  )
    .bind(cutoff)
    .all<{ id: string }>();
  if (expired.results.length === 0) return;
  const stmts = [];
  for (const r of expired.results) {
    stmts.push(env.DB.prepare("DELETE FROM songs WHERE playlist_id = ?").bind(r.id));
    stmts.push(env.DB.prepare("DELETE FROM playlists WHERE id = ?").bind(r.id));
  }
  await env.DB.batch(stmts);
}

async function listPlaylists(env: Env) {
  if (Math.random() < 0.1) {
    try { await gcTrash(env); } catch (e) { console.error("gcTrash:", e); }
  }
  // 활성(휴지통 아님)만
  const playlistsRes = await env.DB.prepare(
    "SELECT id, name, position, version, updated_at FROM playlists WHERE deleted_at IS NULL ORDER BY position ASC, id ASC"
  ).all<{ id: string; name: string; position: number; version: number; updated_at: number }>();
  // 활성 PL의 곡들만 (휴지통 PL의 곡은 따로 listTrash에서)
  const songsRes = await env.DB.prepare(
    `SELECT s.id, s.playlist_id, s.url, s.position, s.note, s.title
       FROM songs s INNER JOIN playlists p ON p.id = s.playlist_id
      WHERE p.deleted_at IS NULL
      ORDER BY s.playlist_id, s.position ASC`
  ).all<{ id: number; playlist_id: string; url: string; position: number; note: string | null; title: string | null }>();

  const byPlaylist = new Map<string, Array<{ id: number; url: string; position: number; note: string | null; title: string | null }>>();
  for (const s of songsRes.results) {
    const arr = byPlaylist.get(s.playlist_id) || [];
    arr.push({ id: s.id, url: s.url, position: s.position, note: s.note, title: s.title });
    byPlaylist.set(s.playlist_id, arr);
  }
  const maxUpdated = playlistsRes.results.reduce((m, p) => Math.max(m, p.updated_at), 0);
  return {
    global_version: maxUpdated,
    playlists: playlistsRes.results.map((p) => ({
      ...p,
      songs: byPlaylist.get(p.id) || [],
    })),
  };
}

// 휴지통 목록 (삭제된 PL + 각 PL 곡 수 + 남은 시간)
async function listTrash(env: Env) {
  const trashRes = await env.DB.prepare(
    "SELECT id, name, deleted_at FROM playlists WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
  ).all<{ id: string; name: string; deleted_at: number }>();
  if (trashRes.results.length === 0) return { trash: [] };
  const counts = await env.DB.prepare(
    `SELECT s.playlist_id, COUNT(*) AS cnt
       FROM songs s INNER JOIN playlists p ON p.id = s.playlist_id
      WHERE p.deleted_at IS NOT NULL
      GROUP BY s.playlist_id`
  ).all<{ playlist_id: string; cnt: number }>();
  const cntMap = new Map<string, number>();
  for (const c of counts.results) cntMap.set(c.playlist_id, c.cnt);
  return {
    trash: trashRes.results.map((p) => ({
      id: p.id,
      name: p.name,
      deleted_at: p.deleted_at,
      expires_at: p.deleted_at + TRASH_RETENTION_SEC,
      song_count: cntMap.get(p.id) ?? 0,
    })),
  };
}

// ──────────────────────────────────────────────
// 라우터
// ──────────────────────────────────────────────
async function handleRequest(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(req.url);
  const path = url.pathname;
  const method = req.method;

  // ── 봇 전용 엔드포인트 (CSRF 면제: 쿠키 아님 + Bearer 토큰) ──
  if (path.startsWith("/api/bot/")) {
    if (!verifyBotAuth(req, env)) return err(401, "Invalid bot token");
    if (path === "/api/bot/password" && method === "GET") {
      const p = await getOrCreatePassword(env);
      return json({ password: p.password, valid_until: p.valid_until });
    }
    if (path === "/api/bot/playlists" && method === "GET") {
      const data = await listPlaylists(env);
      return json(data);
    }
    return err(404, "Not found");
  }

  // ── 사용자 API ──
  // CSRF 방어: state-changing 요청에 Origin 헤더 검증
  if (path.startsWith("/api/") && method !== "GET" && method !== "HEAD") {
    if (!isSameOrigin(req)) return err(403, "Cross-origin request blocked");
  }

  if (path === "/api/auth/login" && method === "POST") {
    const ip = clientIP(req);
    const startedAt = Date.now();

    // Rate limit 체크
    const gate = await checkLoginAllowed(env, ip);
    if (!gate.allowed) {
      return new Response(JSON.stringify({ error: "Too many failed attempts. Try again later." }), {
        status: 429,
        headers: { "Content-Type": "application/json; charset=utf-8", "Retry-After": String(gate.retryAfter || 60) },
      });
    }

    let body: { password?: string };
    try {
      body = await req.json();
    } catch {
      return err(400, "Invalid JSON");
    }
    if (typeof body.password !== "string" || body.password.length === 0 || body.password.length > 100) {
      // 타이밍 일정화
      await new Promise((r) => setTimeout(r, Math.max(0, MIN_LOGIN_RESPONSE_MS - (Date.now() - startedAt))));
      await recordLoginFail(env, ip);
      return err(401, "Authentication failed");
    }

    const p = await getOrCreatePassword(env);
    const ok = timingSafeEqual(body.password, p.password);

    // 응답 시간 일정화 — 최소 200ms 보장 (성공/실패 무관)
    const elapsed = Date.now() - startedAt;
    if (elapsed < MIN_LOGIN_RESPONSE_MS) await new Promise((r) => setTimeout(r, MIN_LOGIN_RESPONSE_MS - elapsed));

    if (!ok) {
      await recordLoginFail(env, ip);
      return err(401, "Authentication failed");
    }

    // 성공 — 세션 회전(매번 새 토큰) + 시도 기록 리셋
    await recordLoginSuccess(env, ip);
    const token = randomSessionToken();
    const ttlHours = parseInt(env.SESSION_TTL_HOURS || "24", 10);
    const expiresAt = now() + ttlHours * 3600;
    const sessionExpires = Math.min(expiresAt, p.valid_until);
    await env.DB.prepare("INSERT INTO sessions (token, expires_at) VALUES (?, ?)")
      .bind(token, sessionExpires)
      .run();
    // 2026-05-17: cookieMaxAge가 음수면 브라우저가 즉시 만료 → 로그인 직후 인증 실패. 0 미만 방어.
    const cookieMaxAge = Math.max(0, sessionExpires - now());
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Set-Cookie": `session_id=${token}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=${cookieMaxAge}`,
      },
    });
  }

  if (path === "/api/auth/logout" && method === "POST") {
    const cookie = req.headers.get("Cookie") || "";
    const match = cookie.match(/session_id=([a-f0-9]+)/);
    if (match) {
      await env.DB.prepare("DELETE FROM sessions WHERE token = ?").bind(match[1]).run();
    }
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Set-Cookie": "session_id=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0",
      },
    });
  }

  if (path === "/api/auth/status" && method === "GET") {
    const ok = await verifyUserSession(req, env);
    return json({ authenticated: ok });
  }

  // 이하 모든 /api/* 는 인증 필요
  if (path.startsWith("/api/")) {
    if (!(await verifyUserSession(req, env))) return err(401, "Not logged in");

    if (path === "/api/playlists" && method === "GET") {
      const sinceParam = url.searchParams.get("since");
      const data = await listPlaylists(env);
      if (sinceParam !== null) {
        const since = parseInt(sinceParam, 10);
        if (!isNaN(since) && data.global_version <= since) {
          return new Response(null, { status: 304 });
        }
      }
      return json(data);
    }

    if (path === "/api/playlists" && method === "POST") {
      let body: { id?: string; name?: string };
      try {
        body = await req.json();
      } catch {
        return err(400, "Invalid JSON");
      }
      const id = validatePlaylistId(body.id);
      const name = validateName(body.name);
      if (!id) return err(400, "Invalid id (alphanumeric/_/-, 1-32 chars)");
      if (!name) return err(400, `Invalid name (1-${MAX_NAME_LEN} chars)`);
      // 2026-05-17: 휴지통에 같은 id가 있으면 안내 (PK 점유 중)
      const existing = await env.DB.prepare(
        "SELECT deleted_at FROM playlists WHERE id = ?"
      )
        .bind(id)
        .first<{ deleted_at: number | null }>();
      if (existing) {
        if (existing.deleted_at !== null) {
          return err(409, `'${id}'는 휴지통에 있어요. 복원하거나 다른 이름을 사용해주세요.`);
        }
        return err(409, "id already exists");
      }
      const posRow = await env.DB.prepare(
        "SELECT COALESCE(MAX(position), 0) AS max FROM playlists"
      ).first<{ max: number }>();
      const nextPos = (posRow?.max ?? 0) + 1;
      await env.DB.prepare(
        "INSERT INTO playlists (id, name, position, updated_at) VALUES (?, ?, ?, unixepoch())"
      )
        .bind(id, name, nextPos)
        .run();
      return json({ ok: true, id });
    }

    // 2026-05-17: 휴지통 목록 조회
    if (path === "/api/trash" && method === "GET") {
      const data = await listTrash(env);
      return json(data);
    }

    // 2026-05-17: 휴지통에서 복원
    // 활성 PL과 ID 충돌 시 자동으로 _restore (또는 _restore1, _restore2 …) 접미사 부여
    const matchRestore = path.match(/^\/api\/playlists\/([^/]+)\/restore$/);
    if (matchRestore && method === "POST") {
      const id = validatePlaylistId(decodeURIComponent(matchRestore[1]));
      if (!id) return err(400, "Invalid id");
      const trashed = await env.DB.prepare(
        "SELECT id FROM playlists WHERE id = ? AND deleted_at IS NOT NULL"
      )
        .bind(id)
        .first();
      if (!trashed) return err(404, "Not in trash");

      const active = await env.DB.prepare(
        "SELECT id FROM playlists WHERE id = ? AND deleted_at IS NULL"
      )
        .bind(id)
        .first();

      let finalId = id;
      if (active) {
        // 충돌 — 비어 있는 _restore[N] 찾기
        let candidate = id + "_restore";
        let suffix = 1;
        while (true) {
          const taken = await env.DB.prepare("SELECT id FROM playlists WHERE id = ?").bind(candidate).first();
          if (!taken) break;
          candidate = id + "_restore" + suffix;
          suffix++;
          if (suffix > 50) return err(500, "Too many restore candidates");
        }
        finalId = candidate;
        // ID 변경 + songs FK cascade + 복원
        await env.DB.batch([
          env.DB.prepare("UPDATE songs SET playlist_id = ? WHERE playlist_id = ?").bind(finalId, id),
          env.DB.prepare(
            "UPDATE playlists SET id = ?, name = ?, deleted_at = NULL, version = version + 1, updated_at = unixepoch() WHERE id = ?"
          ).bind(finalId, finalId, id),
        ]);
      } else {
        // 충돌 없음 — 단순 복원
        await env.DB.prepare(
          "UPDATE playlists SET deleted_at = NULL, version = version + 1, updated_at = unixepoch() WHERE id = ?"
        )
          .bind(id)
          .run();
      }
      return json({ ok: true, id: finalId, renamed: finalId !== id });
    }

    const matchPlaylist = path.match(/^\/api\/playlists\/([^/]+)$/);
    if (matchPlaylist && method === "PATCH") {
      const id = validatePlaylistId(decodeURIComponent(matchPlaylist[1]));
      if (!id) return err(400, "Invalid id");
      let body: { new_id?: string; name?: string; version?: number };
      try {
        body = await req.json();
      } catch {
        return err(400, "Invalid JSON");
      }
      if (typeof body.version !== "number") return err(400, "version required");

      // 2026-05-17: ID 변경 = 봇 명령어 식별자 변경 (!playlist <id>)
      // 동시에 songs.playlist_id도 cascade 갱신 (FK는 ON DELETE만 설정되어 있어 명시적 UPDATE)
      if (body.new_id !== undefined) {
        const newId = validatePlaylistId(body.new_id);
        if (!newId) return err(400, "Invalid new_id (alphanumeric/_/-, 1-32 chars)");
        if (newId === id) return json({ ok: true }); // no-op

        // 새 id 중복 체크
        const dup = await env.DB.prepare("SELECT id FROM playlists WHERE id = ?").bind(newId).first();
        if (dup) return err(409, "new_id already exists");

        // 버전 체크 + 원자적으로 id 변경 + songs FK 갱신
        const verCheck = await env.DB.prepare(
          "SELECT version FROM playlists WHERE id = ?"
        )
          .bind(id)
          .first<{ version: number }>();
        if (!verCheck) return err(404, "Playlist not found");
        if (verCheck.version !== body.version) return err(409, "Version conflict");

        await env.DB.batch([
          env.DB.prepare("UPDATE songs SET playlist_id = ? WHERE playlist_id = ?").bind(newId, id),
          env.DB.prepare(
            "UPDATE playlists SET id = ?, name = ?, version = version + 1, updated_at = unixepoch() WHERE id = ?"
          ).bind(newId, newId, id),
        ]);
        return json({ ok: true, id: newId });
      }

      // 기존 name만 변경하는 흐름은 호환성 위해 유지
      const name = validateName(body.name);
      if (!name) return err(400, "name or new_id required");
      const res = await env.DB.prepare(
        "UPDATE playlists SET name = ?, version = version + 1, updated_at = unixepoch() WHERE id = ? AND version = ?"
      )
        .bind(name, id, body.version)
        .run();
      if ((res.meta.changes ?? 0) === 0) return err(409, "Version conflict");
      return json({ ok: true });
    }

    if (matchPlaylist && method === "DELETE") {
      const id = validatePlaylistId(decodeURIComponent(matchPlaylist[1]));
      if (!id) return err(400, "Invalid id");
      // 2026-05-17: Soft delete — 3일 후 lazy GC가 영구 삭제. 곡은 그대로 보관.
      const res = await env.DB.prepare(
        "UPDATE playlists SET deleted_at = unixepoch(), version = version + 1, updated_at = unixepoch() WHERE id = ? AND deleted_at IS NULL"
      )
        .bind(id)
        .run();
      if ((res.meta.changes ?? 0) === 0) return err(404, "Playlist not found");
      return json({ ok: true });
    }

    const matchSongs = path.match(/^\/api\/playlists\/([^/]+)\/songs$/);
    if (matchSongs && method === "POST") {
      const playlistId = validatePlaylistId(decodeURIComponent(matchSongs[1]));
      if (!playlistId) return err(400, "Invalid playlist id");
      let body: { url?: string; version?: number; note?: string };
      try {
        body = await req.json();
      } catch {
        return err(400, "Invalid JSON");
      }
      const songUrl = validateYouTubeUrl(body.url);
      if (!songUrl) return err(400, "Invalid YouTube URL");
      if (typeof body.version !== "number") return err(400, "version required");
      const note = validateNote(body.note);
      if (note === undefined && body.note !== undefined) return err(400, `Invalid note (≤${MAX_NOTE_LEN} chars)`);
      const plRow = await env.DB.prepare(
        "SELECT version FROM playlists WHERE id = ?"
      )
        .bind(playlistId)
        .first<{ version: number }>();
      if (!plRow) return err(404, "Playlist not found");
      if (plRow.version !== body.version) return err(409, "Version conflict");
      // 2026-05-17: 곡 수 상한 체크 + max position을 한 쿼리로 (round-trip 절약)
      const stat = await env.DB.prepare(
        "SELECT COUNT(*) AS cnt, COALESCE(MAX(position), 0) AS max FROM songs WHERE playlist_id = ?"
      )
        .bind(playlistId)
        .first<{ cnt: number; max: number }>();
      if ((stat?.cnt ?? 0) >= MAX_SONGS_PER_PLAYLIST) {
        return err(409, `플레이리스트가 가득 찼어요 (${MAX_SONGS_PER_PLAYLIST}곡 상한). 곡을 삭제한 후 다시 추가해주세요.`);
      }
      const nextPos = (stat?.max ?? 0) + 1;
      // 2026-05-17: YouTube oEmbed로 영상 제목 자동 fetch (실패 시 null)
      const title = await fetchYouTubeTitle(songUrl);
      await env.DB.prepare(
        "INSERT INTO songs (playlist_id, url, position, note, title) VALUES (?, ?, ?, ?, ?)"
      )
        .bind(playlistId, songUrl, nextPos, note ?? null, title)
        .run();
      await env.DB.prepare(
        "UPDATE playlists SET version = version + 1, updated_at = unixepoch() WHERE id = ?"
      )
        .bind(playlistId)
        .run();
      return json({ ok: true, title });
    }

    // 2026-05-17: 기존 곡 일괄 title 백필 (title이 null인 곡만 oEmbed 호출)
    if (path === "/api/admin/backfill-titles" && method === "POST") {
      const rows = await env.DB.prepare(
        "SELECT id, url FROM songs WHERE title IS NULL OR title = '' LIMIT 200"
      ).all<{ id: number; url: string }>();
      let updated = 0;
      let failed = 0;
      const touchedPlaylists = new Set<string>();
      for (const row of rows.results) {
        const t = await fetchYouTubeTitle(row.url);
        if (t) {
          await env.DB.prepare("UPDATE songs SET title = ? WHERE id = ?").bind(t, row.id).run();
          // 어느 플레이리스트인지 추적 (version bump용)
          const pl = await env.DB.prepare("SELECT playlist_id FROM songs WHERE id = ?")
            .bind(row.id)
            .first<{ playlist_id: string }>();
          if (pl) touchedPlaylists.add(pl.playlist_id);
          updated++;
        } else {
          failed++;
        }
      }
      // 영향받은 플레이리스트 version 증가 (클라이언트 폴링이 변경 감지)
      for (const plId of touchedPlaylists) {
        await env.DB.prepare(
          "UPDATE playlists SET version = version + 1, updated_at = unixepoch() WHERE id = ?"
        )
          .bind(plId)
          .run();
      }
      return json({ ok: true, updated, failed, remaining: Math.max(0, rows.results.length - updated) });
    }

    const matchSong = path.match(/^\/api\/playlists\/([^/]+)\/songs\/(\d+)$/);
    if (matchSong && method === "DELETE") {
      const playlistId = validatePlaylistId(decodeURIComponent(matchSong[1]));
      if (!playlistId) return err(400, "Invalid playlist id");
      const songId = parseInt(matchSong[2], 10);
      const versionStr = url.searchParams.get("version");
      if (versionStr === null) return err(400, "version required");
      const version = parseInt(versionStr, 10);
      const plRow = await env.DB.prepare(
        "SELECT version FROM playlists WHERE id = ?"
      )
        .bind(playlistId)
        .first<{ version: number }>();
      if (!plRow) return err(404, "Playlist not found");
      if (plRow.version !== version) return err(409, "Version conflict");
      const res = await env.DB.prepare(
        "DELETE FROM songs WHERE id = ? AND playlist_id = ?"
      )
        .bind(songId, playlistId)
        .run();
      if ((res.meta.changes ?? 0) === 0) return err(404, "Song not found");
      await env.DB.prepare(
        "UPDATE playlists SET version = version + 1, updated_at = unixepoch() WHERE id = ?"
      )
        .bind(playlistId)
        .run();
      return json({ ok: true });
    }

    if (matchSong && method === "PATCH") {
      const playlistId = validatePlaylistId(decodeURIComponent(matchSong[1]));
      if (!playlistId) return err(400, "Invalid playlist id");
      const songId = parseInt(matchSong[2], 10);
      let body: { position?: number; note?: string | null; version?: number };
      try {
        body = await req.json();
      } catch {
        return err(400, "Invalid JSON");
      }
      if (typeof body.version !== "number") return err(400, "version required");
      const plRow = await env.DB.prepare(
        "SELECT version FROM playlists WHERE id = ?"
      )
        .bind(playlistId)
        .first<{ version: number }>();
      if (!plRow) return err(404, "Playlist not found");
      if (plRow.version !== body.version) return err(409, "Version conflict");
      const sets: string[] = [];
      const binds: any[] = [];
      if (typeof body.position === "number") {
        if (!Number.isInteger(body.position) || body.position < 0 || body.position > 1_000_000) {
          return err(400, "Invalid position");
        }
        sets.push("position = ?");
        binds.push(body.position);
      }
      if (body.note !== undefined) {
        const note = validateNote(body.note);
        if (note === undefined) return err(400, `Invalid note (≤${MAX_NOTE_LEN} chars)`);
        sets.push("note = ?");
        binds.push(note);
      }
      if (sets.length === 0) return err(400, "Nothing to update");
      binds.push(songId, playlistId);
      const res = await env.DB.prepare(
        `UPDATE songs SET ${sets.join(", ")} WHERE id = ? AND playlist_id = ?`
      )
        .bind(...binds)
        .run();
      if ((res.meta.changes ?? 0) === 0) return err(404, "Song not found");
      await env.DB.prepare(
        "UPDATE playlists SET version = version + 1, updated_at = unixepoch() WHERE id = ?"
      )
        .bind(playlistId)
        .run();
      return json({ ok: true });
    }

    return err(404, "Not found");
  }

  // ── 정적 자산 (HTML/CSS/JS) ──
  return env.ASSETS.fetch(req);
}

export default {
  async fetch(req: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    try {
      const res = await handleRequest(req, env, ctx);
      return await applySecurityHeaders(res);
    } catch (e: any) {
      console.error("Worker error:", e);
      return await applySecurityHeaders(err(500, "Internal error"));
    }
  },
  // 2026-05-17: Pages 이전 — Cron Trigger 지원 X.
  // 비번 회전은 getOrCreatePassword가 만료 시 자동 갱신 (lazy rotation).
  // login_attempts 정리는 recordLoginFail에서 가끔 (확률적으로) 실행.
};
