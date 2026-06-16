import discord
from discord.ext import commands
from discord import ui
import yt_dlp
import asyncio
import random
import re
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import urllib.request
import json as _json
# 2026-05-16: 토큰을 config.py로 분리 (보안)
# 2026-05-17: Cloudflare Worker(웹 플레이리스트 관리) URL/토큰 추가
from config import DISCORD_TOKEN, WORKER_URL, BOT_TOKEN_FOR_WORKER

intents = discord.Intents.default()
intents.message_content = True
# 2026-05-17: 음성 채널 인원 감지(자동 종료)용
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
executor = ThreadPoolExecutor(max_workers=4)

# 2026-05-17: 멀티 서버 지원 — 재생 상태를 guild_id 단위로 분리
# 2026-05-17: title_cache는 LRU(OrderedDict)로 무제한 증가 방지
TITLE_CACHE_MAX = 1000
title_cache: "OrderedDict[str, str]" = OrderedDict()
guild_states = {}  # guild_id -> state dict
MAX_SEARCH_LEN = 500       # !play 검색어/URL 길이 제한 (메모리 보호)
AUTOPLAY_HISTORY_MAX = 50  # autoplay 중복 방지 set 상한


def _title_cache_set(search: str, title: str) -> None:
    """LRU 갱신 + 상한 초과 시 가장 오래된 항목 제거"""
    if search in title_cache:
        title_cache.move_to_end(search)
    title_cache[search] = title
    while len(title_cache) > TITLE_CACHE_MAX:
        title_cache.popitem(last=False)


def _title_cache_get(search: str) -> "str | None":
    if search in title_cache:
        title_cache.move_to_end(search)
        return title_cache[search]
    return None


def get_state(guild_id: int) -> dict:
    """서버별 재생 상태 (없으면 새로 생성)"""
    if guild_id not in guild_states:
        guild_states[guild_id] = {
            "queue": [],
            "current_song": None,         # 제목
            "current_search": None,       # 검색어/URL (history용)
            "current_page_url": None,     # YouTube 페이지 URL
            "prefetched": None,
            "autoplay": True,
            "autoplay_history": set(),
            "history": [],                # 직전 재생 곡 검색어 (가장 최근이 끝)
            "paused": False,
            # 2026-05-17: Now Playing 카드
            "np_message": None,           # discord.Message
            "np_channel_id": None,
            "np_view": None,              # NowPlayingView
            "password_show_remaining": 0, # 비번 표시 잔여 곡 수 (NP embed field로 표시)
            # 2026-05-17: NP 메시지를 채널 최하단에 유지 (debounce reattach)
            "np_reattach_scheduled": False,
            # 2026-05-17: update/reattach 동시 실행 race condition 방지
            "np_lock": None,  # asyncio.Lock(), event loop 안에서 lazy 생성
        }
    return guild_states[guild_id]


ydl_opts = {
    "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "headers": {"User-Agent": "Mozilla/5.0"},
    # 2026-05-16: yt-dlp EJS 보조 스크립트 (deno + GitHub 원격 컴포넌트)
    "remote_components": ["ejs:github"],
}

ffmpeg_options = {
    # 2026-05-17: 음질/안정성 개선
    # - reconnect: 일시 연결 끊김 자동 재연결
    # - 48000Hz/2ch: Discord 음성과 동일 → 불필요 변환 회피
    # - loglevel warning: Broken pipe 등 정상 종료 시 노이즈 제거
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -timeout 15000000 -loglevel warning",
    "options": "-vn -ar 48000 -ac 2",
}


# ──────────────────────────────────────────────
# 2026-05-17: Worker API 클라이언트 (플레이리스트/비번)
# ──────────────────────────────────────────────
_playlists_cache = {"data": None, "fetched_at": 0}
_PLAYLISTS_TTL_SEC = 10  # 2026-05-17: 5 → 10초 (호출 빈도 ↓ + fail 시 더 오래 캐시 유효)
_password_cache = {"password": None, "valid_until": 0}


def _worker_get_sync(path: str) -> dict:
    """봇 토큰으로 Worker API GET (동기)
    2026-05-17: Python-urllib 기본 UA는 Cloudflare Bot Fight Mode(error 1010)에 차단됨.
    명시적 UA로 우회.
    """
    req = urllib.request.Request(
        WORKER_URL.rstrip("/") + path,
        headers={
            "Authorization": f"Bearer {BOT_TOKEN_FOR_WORKER}",
            "Accept": "application/json",
            "User-Agent": "DiscordMusicBot/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return _json.loads(resp.read().decode("utf-8"))


async def fetch_playlists() -> dict:
    """Worker에서 플레이리스트 조회 (10초 캐시 + 실패 시 마지막 성공 캐시 fallback)
    2026-05-17: Worker 다운/네트워크 실패 시에도 옛 데이터로 !playlist 계속 동작
    """
    now = time.time()
    if _playlists_cache["data"] and now - _playlists_cache["fetched_at"] < _PLAYLISTS_TTL_SEC:
        return _playlists_cache["data"]
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(executor, _worker_get_sync, "/api/bot/playlists")
        _playlists_cache["data"] = data
        _playlists_cache["fetched_at"] = now
        return data
    except Exception as e:
        # fetch 실패 + 옛 캐시 있으면 stale 데이터로 fallback (Worker 다운 시 가용성 ↑)
        if _playlists_cache["data"]:
            print(f"[fetch_playlists] failed, using stale cache: {e}")
            return _playlists_cache["data"]
        raise


async def fetch_password() -> str | None:
    """Worker에서 현재 비번 조회 (만료 시점 캐시)"""
    now = time.time()
    if _password_cache["password"] and now < _password_cache["valid_until"] - 5:
        return _password_cache["password"]
    try:
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(executor, _worker_get_sync, "/api/bot/password")
        _password_cache["password"] = data.get("password")
        _password_cache["valid_until"] = data.get("valid_until", 0)
        return _password_cache["password"]
    except Exception as e:
        print(f"[fetch_password] error: {e}")
        return None


# ──────────────────────────────────────────────
# YouTube 추출 / 썸네일
# ──────────────────────────────────────────────
def get_youtube_thumbnail(url: str):
    if not url:
        return None
    match = re.search(r'(?:v=|youtu\.be/)([^&?/\s]+)', url)
    if match:
        return f"https://img.youtube.com/vi/{match.group(1)}/mqdefault.jpg"
    return None


def _extract_sync(search):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        if search.startswith("http"):
            info = ydl.extract_info(search, download=False)
        else:
            info = ydl.extract_info(f"ytsearch:{search}", download=False)
            if "entries" in info:
                info = info["entries"][0]
        title = info.get("title", search)
        stream_url = info.get("url")
        page_url = info.get("webpage_url", search)
        return title, stream_url, page_url


async def extract_info(search):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _extract_sync, search)


def _fetch_oembed_title_sync(url: str) -> "str | None":
    """YouTube oEmbed로 영상 제목 fetch.
    2026-05-17: Premium/지역제한 영상도 metadata는 공개 가능 → 검색 폴백용 제목 추출."""
    import urllib.parse as _up
    oembed_url = f"https://www.youtube.com/oembed?url={_up.quote(url, safe='')}&format=json"
    req = urllib.request.Request(oembed_url, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
            t = (data.get("title") or "").strip()
            return t or None
    except Exception:
        return None


async def _fetch_oembed_title(url: str) -> "str | None":
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, _fetch_oembed_title_sync, url)


async def fetch_title_only(search: str):
    if _title_cache_get(search) is not None:
        return
    try:
        title, _, _ = await extract_info(search)
        _title_cache_set(search, title)
    except Exception:
        pass


async def prefetch_next(guild_id: int):
    state = get_state(guild_id)
    if not state["queue"]:
        return
    next_search = state["queue"][0]
    try:
        title, stream_url, page_url = await extract_info(next_search)
        _title_cache_set(next_search, title)
        state["prefetched"] = (next_search, title, stream_url, page_url)
    except Exception as e:
        # 2026-05-17: 디버깅용 로그 (silent 실패 → 추적 불가 문제 해결)
        print(f"[prefetch_next] {next_search!r}: {e}")
        state["prefetched"] = None


def _fetch_related_sync(page_url, count=5):
    match = re.search(r'(?:v=|youtu\.be/)([^&?/\s]+)', page_url)
    if not match:
        return []
    video_id = match.group(1)
    mix_url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
    opts = {
        "quiet": True,
        "noplaylist": False,
        "extract_flat": True,
        "playliststart": 2,
        "playlistend": count + 5,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(mix_url, download=False)
            if not info or "entries" not in info:
                return []
            urls = []
            for entry in info["entries"]:
                if entry and entry.get("id"):
                    urls.append(f"https://www.youtube.com/watch?v={entry['id']}")
            return urls
    except Exception:
        return []


# ──────────────────────────────────────────────
# Now Playing 카드 (메시지 + 4 버튼)
# ──────────────────────────────────────────────
class NowPlayingView(ui.View):
    """Now Playing 메시지에 부착되는 버튼
    2026-05-17: row 0 = 이전/정지/다음 (재생 컨트롤), row 1 = 플레이리스트 편집 (최하단)
    """

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id

    # 2026-05-17: 표준 미디어 컨트롤 emoji 복원 (단순하고 익숙함)
    # secondary 버튼은 파란 글리프, danger(정지)는 흰 글리프로 디스코드가 자동 렌더 — 의도된 강조
    @ui.button(label="이전곡", style=discord.ButtonStyle.secondary, emoji="⏮", row=0)
    async def prev_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_prev(interaction)

    @ui.button(label="일시정지", style=discord.ButtonStyle.secondary, emoji="⏸", row=0, custom_id="np_pause")
    async def pause_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_pause_toggle(interaction)

    @ui.button(label="다음곡", style=discord.ButtonStyle.secondary, emoji="⏭", row=0)
    async def next_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_next(interaction)

    @ui.button(label="정지", style=discord.ButtonStyle.danger, emoji="⏹", row=0)
    async def stop_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_full_stop(interaction)

    @ui.button(label="플레이리스트 편집", style=discord.ButtonStyle.secondary, emoji="✏️", row=1)
    async def edit_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_show_password(interaction)

    @ui.button(label="플레이리스트 섞기", style=discord.ButtonStyle.secondary, emoji="🔀", row=1)
    async def shuffle_button(self, interaction: discord.Interaction, _btn: ui.Button):
        await interaction.response.defer()
        await handle_shuffle_queue(interaction)


def build_now_playing_embed(state: dict, password: str | None = None) -> discord.Embed:
    """현재 상태로 Now Playing 임베드 생성
    2026-05-17: 비번/링크는 footer 아래 field로 표시 (reattach 시 자동 동행)
    """
    if state["paused"]:
        title_prefix = "⏸ 일시정지됨"
        color = discord.Color.greyple()
    else:
        title_prefix = "🎵 지금 재생 중"
        color = discord.Color.from_rgb(236, 72, 153)  # soft pink

    desc_lines = []
    if state["current_song"]:
        if state["current_page_url"]:
            desc_lines.append(f"**[{state['current_song']}]({state['current_page_url']})**")
        else:
            desc_lines.append(f"**{state['current_song']}**")
    embed = discord.Embed(
        title=title_prefix,
        description="\n".join(desc_lines) or "(없음)",
        color=color,
    )
    thumb = get_youtube_thumbnail(state["current_page_url"])
    if thumb:
        embed.set_thumbnail(url=thumb)

    # 대기열/오토플레이 정보를 inline field로 (footer보다 잘 보임)
    queue_info = f"{len(state['queue'])}곡 · 오토플레이 {'ON' if state['autoplay'] else 'OFF'}"
    embed.add_field(name="대기열", value=queue_info, inline=False)

    # 비번이 있고 표시 잔여 곡이 남아있으면 그 아래 field로
    if password and state["password_show_remaining"] > 0:
        embed.add_field(
            name="🔒 플레이리스트 편집",
            value=f"비밀번호 : `{password}`\n링크 : {WORKER_URL}",
            inline=False,
        )
    return embed


async def get_np_channel(state: dict) -> discord.TextChannel | None:
    if state["np_channel_id"] is None:
        return None
    ch = bot.get_channel(state["np_channel_id"])
    return ch if isinstance(ch, discord.TextChannel) else None


def _get_np_lock(state: dict) -> asyncio.Lock:
    """np_lock을 lazy 생성 (event loop 안에서 호출되어야 함)"""
    if state["np_lock"] is None:
        state["np_lock"] = asyncio.Lock()
    return state["np_lock"]


async def update_now_playing(guild_id: int, *, channel: discord.TextChannel | None = None):
    """Now Playing 메시지를 갱신(없으면 생성). channel이 주어지면 거기에 생성.
    2026-05-17: lock으로 reattach와 직렬화 — NP 메시지 동시 생성 race condition 방지
    """
    state = get_state(guild_id)
    async with _get_np_lock(state):
        # 비번 표시 중이면 가져오기
        password = None
        if state["password_show_remaining"] > 0:
            password = await fetch_password()

        # 일시정지 버튼 라벨/emoji 토글
        if state["np_view"] is not None:
            for child in state["np_view"].children:
                if isinstance(child, ui.Button) and child.custom_id == "np_pause":
                    if state["paused"]:
                        child.label = "재생"
                        child.emoji = "▶"
                    else:
                        child.label = "일시정지"
                        child.emoji = "⏸"

        embed = build_now_playing_embed(state, password=password)

        if state["np_message"] is not None:
            try:
                await state["np_message"].edit(embed=embed, view=state["np_view"])
                return
            except (discord.NotFound, discord.HTTPException):
                state["np_message"] = None  # 메시지 사라졌으면 새로 생성
        # 새로 생성
        target_channel = channel or await get_np_channel(state)
        if target_channel is None:
            return
        state["np_view"] = NowPlayingView(guild_id)
        state["np_channel_id"] = target_channel.id
        state["np_message"] = await target_channel.send(embed=embed, view=state["np_view"])


async def delete_now_playing(guild_id: int):
    state = get_state(guild_id)
    async with _get_np_lock(state):
        if state["np_message"] is not None:
            try:
                await state["np_message"].delete()
            except (discord.NotFound, discord.HTTPException):
                pass
        if state["np_view"] is not None:
            try:
                state["np_view"].stop()
            except Exception:
                pass
        state["np_message"] = None
        state["np_view"] = None
        state["np_channel_id"] = None


async def reattach_now_playing(guild_id: int):
    """NP 메시지를 삭제하고 채널 최하단에 다시 send.
    2026-05-17: 채팅이 NP를 위로 밀어내면 호출되어 NP가 항상 채널 하단에 보이게 유지.
    2026-05-17: lock으로 update와 직렬화 + embed를 옛것 copy 대신 최신 state로 새로 빌드 (stale 방지)
    """
    state = get_state(guild_id)
    async with _get_np_lock(state):
        if state["np_message"] is None:
            return
        channel = state["np_message"].channel
        old_view = state["np_view"]
        # 옛 embed copy 대신 항상 최신 state로 새로 빌드 (stale 방지)
        password = None
        if state["password_show_remaining"] > 0:
            password = await fetch_password()
        embed = build_now_playing_embed(state, password=password)
        try:
            await state["np_message"].delete()
        except (discord.NotFound, discord.HTTPException):
            pass
        state["np_message"] = None
        if channel is None:
            state["np_view"] = None
            return
        # 새로 send. View는 그대로 재사용 (timeout=None이라 안전)
        try:
            state["np_message"] = await channel.send(embed=embed, view=old_view)
            state["np_channel_id"] = channel.id
        except discord.HTTPException:
            state["np_view"] = None


# ──────────────────────────────────────────────
# 버튼 핸들러
# ──────────────────────────────────────────────
async def handle_prev(interaction: discord.Interaction):
    """이전 곡: 직전 1곡을 큐 맨 앞에 넣고, 현재 곡도 그 다음에 넣어 다시 재생"""
    state = get_state(interaction.guild_id)
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client:
        return
    if not state["history"]:
        try:
            await interaction.followup.send("⏮ 이전 곡이 없어!", ephemeral=True)
        except Exception:
            pass
        return
    prev_search = state["history"].pop()
    # 현재 곡을 prev 다음에 다시 넣음 (돌아왔던 곡 재생 후 복귀)
    if state["current_search"]:
        state["queue"].insert(0, state["current_search"])
    state["queue"].insert(0, prev_search)
    # prefetch 무효
    state["prefetched"] = None
    if voice_client.is_playing() or voice_client.is_paused():
        voice_client.stop()  # after 콜백이 check_queue 호출


async def handle_pause_toggle(interaction: discord.Interaction):
    state = get_state(interaction.guild_id)
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if not voice_client:
        return
    if voice_client.is_playing():
        voice_client.pause()
        state["paused"] = True
        await update_now_playing(interaction.guild_id)
    elif voice_client.is_paused():
        voice_client.resume()
        state["paused"] = False
        await update_now_playing(interaction.guild_id)


async def handle_next(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()  # after 콜백이 check_queue 호출


# 2026-05-17: 정지 버튼 — 재생 멈추고 음성 채널 퇴장 (= !stop)
async def handle_full_stop(interaction: discord.Interaction):
    voice_client = discord.utils.get(bot.voice_clients, guild=interaction.guild)
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await disconnect_voice(interaction.guild)


async def handle_shuffle_queue(interaction: discord.Interaction):
    """2026-05-17: 현재 재생 대기 중인 큐만 셔플. 현재 재생 곡은 영향 없음."""
    state = get_state(interaction.guild_id)
    if not state["queue"]:
        try:
            await interaction.followup.send("🔀 섞을 곡이 없어!", ephemeral=True)
        except Exception:
            pass
        return
    random.shuffle(state["queue"])
    state["prefetched"] = None  # 큐 첫 번째가 바뀌었을 수 있으니 prefetch 무효
    try:
        await interaction.followup.send(f"🔀 대기열 {len(state['queue'])}곡을 섞었어!", ephemeral=True)
    except Exception:
        pass


async def handle_show_password(interaction: discord.Interaction):
    """2026-05-17: NP embed 안에 비번/링크 field 추가. 다음 3곡 동안 유지.
    별도 메시지를 보내지 않으므로 on_message reattach 트리거 없음 = NP 중복 방지.
    """
    state = get_state(interaction.guild_id)
    pw = await fetch_password()
    if not pw:
        try:
            await interaction.followup.send("비밀번호를 가져오지 못했어. 잠시 후 다시 시도해줘.", ephemeral=True)
        except Exception:
            pass
        return
    state["password_show_remaining"] = 3
    await update_now_playing(interaction.guild_id)


# ──────────────────────────────────────────────
# 음성 종료 / 채널 자동 퇴장
# ──────────────────────────────────────────────
async def disconnect_voice(guild):
    """음성 채널에서 봇이 나가고 해당 서버 상태 초기화"""
    voice_client = discord.utils.get(bot.voice_clients, guild=guild)
    await delete_now_playing(guild.id)
    state = get_state(guild.id)
    state["queue"].clear()
    state["current_song"] = None
    state["current_search"] = None
    state["current_page_url"] = None
    state["prefetched"] = None
    state["autoplay_history"].clear()
    state["history"].clear()
    state["paused"] = False
    state["password_show_remaining"] = 0
    if voice_client:
        try:
            await voice_client.disconnect()
        except Exception:
            pass


async def fetch_and_add_related(ctx_or_guild, page_url):
    """관련 곡을 가져와 대기열에 추가하고 재생 시작"""
    guild = ctx_or_guild.guild if hasattr(ctx_or_guild, "guild") else ctx_or_guild
    state = get_state(guild.id)
    loop = asyncio.get_event_loop()
    related_urls = await loop.run_in_executor(executor, _fetch_related_sync, page_url)

    added = []
    history = state["autoplay_history"]
    for url in related_urls:
        vid_match = re.search(r'v=([^&?/\s]+)', url)
        vid_id = vid_match.group(1) if vid_match else url
        if vid_id not in history:
            history.add(vid_id)
            added.append(url)
        if len(added) >= 3:
            break
    # 2026-05-17: history set 무제한 증가 방지 — 상한 초과 시 새 set으로 (간단)
    if len(history) > AUTOPLAY_HISTORY_MAX:
        # 가장 최근 N개만 유지 (set은 순서 없으므로 단순 reset이 가장 단순)
        state["autoplay_history"] = set()

    if not added:
        await disconnect_voice(guild)
        return

    state["queue"].extend(added)
    await check_queue(guild)


async def check_queue(guild_or_ctx, channel: discord.TextChannel | None = None):
    """다음 곡 재생. guild_or_ctx는 Guild 또는 Context.
    2026-05-17: 재귀 → while 루프 (연속 실패 시 stack overflow 방지)
    """
    if hasattr(guild_or_ctx, "guild"):
        guild = guild_or_ctx.guild
        if channel is None:
            channel = getattr(guild_or_ctx, "channel", None)
    else:
        guild = guild_or_ctx

    state = get_state(guild.id)

    while True:
        voice_client = discord.utils.get(bot.voice_clients, guild=guild)

        if not voice_client:
            state["current_song"] = None
            state["current_search"] = None
            state["current_page_url"] = None
            await delete_now_playing(guild.id)
            return

        if not state["queue"]:
            if state["autoplay"] and state["current_page_url"]:
                # 정상 재생 후 큐가 비면 autoplay로 관련곡 추가
                last_url = state["current_page_url"]
                if state["current_search"]:
                    state["history"].append(state["current_search"])
                    state["history"] = state["history"][-10:]
                state["current_song"] = None
                state["current_search"] = None
                state["current_page_url"] = None
                await fetch_and_add_related(guild, last_url)
            elif state["current_page_url"] or state["current_song"]:
                # autoplay OFF 또는 관련곡 없음 — 정상 종료로 보고 채널 퇴장
                await disconnect_voice(guild)
            # 한 번도 재생한 적 없는 상태(첫 곡 실패): 채널 유지, 사용자 다음 명령 대기
            return

        # 직전 곡을 history에 push (이전곡 버튼용)
        if state["current_search"]:
            state["history"].append(state["current_search"])
            state["history"] = state["history"][-10:]

        next_search = state["queue"].pop(0)

        if state["prefetched"] and state["prefetched"][0] == next_search:
            _, title, stream_url, page_url = state["prefetched"]
            state["prefetched"] = None
        else:
            state["prefetched"] = None
            try:
                title, stream_url, page_url = await extract_info(next_search)
            except Exception as e:
                # 2026-05-17: 폴백 — URL이면 oEmbed로 제목 추출 후 검색 모드로 재시도
                # (Premium/지역제한 영상의 경우 다른 영상 결과로 대체 재생)
                fallback_used = False
                if next_search.startswith("http"):
                    fb_title = await _fetch_oembed_title(next_search)
                    if fb_title:
                        try:
                            title, stream_url, page_url = await extract_info(fb_title)
                            fallback_used = True
                            if channel:
                                try:
                                    await channel.send(embed=discord.Embed(
                                        description=f"🔍 원본 영상 재생 불가 → '**{fb_title}**' 검색 결과로 대체 재생",
                                        color=discord.Color.orange()))
                                except Exception:
                                    pass
                        except Exception as e2:
                            print(f"[check_queue] fallback search failed for {fb_title!r}: {e2}")
                if not fallback_used:
                    has_more = bool(state["queue"])
                    msg = "곡을 불러오지 못했어. 다음 곡으로 넘어갈게!" if has_more else "❌ 곡을 불러오지 못했어. 다른 검색어로 다시 시도해줘."
                    if channel:
                        try:
                            await channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
                        except Exception:
                            pass
                    print(f"[check_queue] extract failed for {next_search!r}: {e}")
                    continue  # 루프 재진입

        if not stream_url:
            has_more = bool(state["queue"])
            msg = "재생할 수 없는 곡이야. 다음 곡으로 넘어갈게!" if has_more else "❌ 재생할 수 없는 곡이야. 다른 검색어로 다시 시도해줘."
            if channel:
                try:
                    await channel.send(embed=discord.Embed(description=msg, color=discord.Color.red()))
                except Exception:
                    pass
            continue

        # 재생 성공 흐름 — 루프 빠져나가 한 곡 재생 후 종료
        _title_cache_set(next_search, title)
        state["current_song"] = title
        state["current_search"] = next_search
        state["current_page_url"] = page_url
        state["paused"] = False

        if state["password_show_remaining"] > 0:
            state["password_show_remaining"] -= 1

        asyncio.create_task(prefetch_next(guild.id))

        def after_playing(error):
            asyncio.run_coroutine_threadsafe(check_queue(guild, channel), bot.loop)

        voice_client.play(
            discord.FFmpegPCMAudio(stream_url, **ffmpeg_options),
            after=after_playing,
        )
        # 채널 max bitrate에 맞춰 동적 설정 (Boost 서버에서 음질 최대화)
        try:
            ch_bitrate_kbps = max(64, voice_client.channel.bitrate // 1000)
            voice_client.encoder.set_bitrate(ch_bitrate_kbps)
        except Exception:
            voice_client.encoder.set_bitrate(128)

        if channel is None:
            channel = await get_np_channel(state)
        await update_now_playing(guild.id, channel=channel)
        return


def format_queue_display(search: str) -> str:
    cached = _title_cache_get(search)
    if cached is not None:
        return cached
    if search.startswith("http"):
        match = re.search(r'(?:v=|youtu\.be/)([^&?/\s]+)', search)
        if match:
            return f"youtu.be/{match.group(1)}"
        return search[:50] + "..." if len(search) > 50 else search
    return search


# ──────────────────────────────────────────────
# 이벤트
# ──────────────────────────────────────────────
NP_REATTACH_DEBOUNCE_SEC = 1.0  # 새 메시지 후 NP reattach까지 대기 (rate limit 안전)


async def _schedule_np_reattach(guild_id: int):
    """1초 debounce 후 NP 메시지 reattach. 동시 호출은 무시 (scheduled flag)."""
    state = get_state(guild_id)
    if state["np_reattach_scheduled"]:
        return
    state["np_reattach_scheduled"] = True
    try:
        await asyncio.sleep(NP_REATTACH_DEBOUNCE_SEC)
        await reattach_now_playing(guild_id)
    finally:
        state["np_reattach_scheduled"] = False


@bot.event
async def on_message(message: discord.Message):
    """채널에 새 메시지가 올라오면 NP 카드를 채널 최하단으로 reattach.
    2026-05-17: 봇 자신의 NP 메시지는 제외 (무한 루프 방지).
    """
    if message.guild is not None:
        state = get_state(message.guild.id)
        if (
            state["np_message"] is not None
            and message.channel.id == state["np_channel_id"]
            and not (message.author == bot.user and state["np_message"].id == message.id)
        ):
            asyncio.create_task(_schedule_np_reattach(message.guild.id))
    # on_message를 오버라이드했으므로 명령어 처리는 직접 호출해야 함
    await bot.process_commands(message)


@bot.event
async def on_voice_state_update(member, before, after):
    """- 봇이 강제로 채널에서 끊겼을 때 state 정리 (NP 좀비 방지)
    - 봇이 있는 음성 채널에서 사람이 모두 나가면 봇도 자동 퇴장
    """
    # 2026-05-17: 봇 자신이 강제 disconnect (관리자 kick / Discord 측 종료 등)
    if member == bot.user:
        if before.channel is not None and after.channel is None:
            await disconnect_voice(member.guild)
        return
    if before.channel is None or before.channel == after.channel:
        return
    voice_client = discord.utils.get(bot.voice_clients, guild=member.guild)
    if not voice_client or voice_client.channel != before.channel:
        return
    human_members = [m for m in before.channel.members if not m.bot]
    if not human_members:
        await disconnect_voice(member.guild)


# ──────────────────────────────────────────────
# 명령어
# ──────────────────────────────────────────────
# 2026-05-17: 명령어 공통 입력 검증 — 검색어 유효성 + 사용자 음성 채널 확인 + 봇 연결/이동
async def _ensure_voice_for_command(ctx, search: "str | None") -> "tuple[bool, str | None]":
    """검증 + 봇 음성 연결 보장. (ok, sanitized_search) 반환. ok=False면 이미 안내 메시지 전송됨."""
    if not search or not search.strip():
        await ctx.send(embed=discord.Embed(description="검색어를 입력해줘!", color=discord.Color.red()))
        return False, None
    sanitized = search.strip()
    if len(sanitized) > MAX_SEARCH_LEN:
        await ctx.send(embed=discord.Embed(
            description=f"검색어가 너무 길어! ({len(sanitized)}자, 최대 {MAX_SEARCH_LEN}자)",
            color=discord.Color.red()))
        return False, None
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send(embed=discord.Embed(description="음성 채널에 먼저 들어가줘!", color=discord.Color.red()))
        return False, None
    voice_channel = ctx.author.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not voice_client:
        await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        # 봇이 다른 채널에 있으면 사용자 채널로 이동
        try:
            await voice_client.move_to(voice_channel)
        except Exception:
            pass
    return True, sanitized


@bot.command()
async def play(ctx, *, search: str = None):
    ok, search = await _ensure_voice_for_command(ctx, search)
    if not ok:
        return
    state = get_state(ctx.guild.id)
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    state["np_channel_id"] = ctx.channel.id

    if voice_client.is_playing() or voice_client.is_paused():
        state["queue"].append(search)
        embed = discord.Embed(
            description=f"대기열에 추가됐어! **{len(state['queue'])}번째**",
            color=discord.Color.from_rgb(244, 63, 116))
        embed.add_field(name="곡", value=format_queue_display(search), inline=False)
        await ctx.send(embed=embed)
        return

    state["queue"].insert(0, search)
    await check_queue(ctx, channel=ctx.channel)


@bot.command(name="play1")
async def play1(ctx, *, search: str = None):
    ok, search = await _ensure_voice_for_command(ctx, search)
    if not ok:
        return
    state = get_state(ctx.guild.id)
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    state["np_channel_id"] = ctx.channel.id

    if voice_client.is_playing() or voice_client.is_paused():
        state["queue"].insert(0, search)
        embed = discord.Embed(
            description="다음 곡으로 바로 예약했어!",
            color=discord.Color.from_rgb(244, 63, 116))
        embed.add_field(name="곡", value=format_queue_display(search), inline=False)
        await ctx.send(embed=embed)
        return

    state["queue"].insert(0, search)
    await check_queue(ctx, channel=ctx.channel)


@bot.command()
async def skip(ctx):
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await ctx.send(embed=discord.Embed(description="⏭️ 스킵!", color=discord.Color.orange()))
    else:
        await ctx.send(embed=discord.Embed(description="지금 재생 중인 노래가 없어!", color=discord.Color.red()))


@bot.command()
async def stop(ctx):
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
    await disconnect_voice(ctx.guild)
    await ctx.send(embed=discord.Embed(description="⏹️ 모든 재생을 멈추고 음성 채널에서 나갈게.", color=discord.Color.dark_gray()))


@bot.command(name="autoplay")
async def autoplay_cmd(ctx):
    state = get_state(ctx.guild.id)
    state["autoplay"] = not state["autoplay"]
    if state["autoplay"]:
        state["autoplay_history"].clear()
        embed = discord.Embed(
            description="🔀 오토플레이 **ON** — 대기열이 비면 관련 곡을 자동으로 추가할게!",
            color=discord.Color.purple())
    else:
        embed = discord.Embed(description="🔀 오토플레이 **OFF**", color=discord.Color.dark_gray())
    await ctx.send(embed=embed)
    await update_now_playing(ctx.guild.id)


@bot.command(name="list")
async def queue_list(ctx):
    state = get_state(ctx.guild.id)
    queue = state["queue"]
    if not queue and not state["current_song"]:
        await ctx.send(embed=discord.Embed(description="지금 재생 중인 노래도 없고 대기열도 비어있어!", color=discord.Color.red()))
        return
    embed = discord.Embed(title="📋 재생 대기열", color=discord.Color.from_rgb(244, 63, 116))
    if state["current_song"]:
        thumb = get_youtube_thumbnail(state["current_page_url"])
        now_value = f"[{state['current_song']}]({state['current_page_url']})" if state["current_page_url"] else state["current_song"]
        embed.add_field(name="🎵 지금 재생 중", value=now_value, inline=False)
        if thumb:
            embed.set_thumbnail(url=thumb)
    if queue:
        front_n, back_n = 5, 2
        if len(queue) <= front_n + back_n:
            display_items = list(enumerate(queue))
            show_sep = False
        else:
            display_items = (
                [(i, queue[i]) for i in range(front_n)] +
                [(len(queue) - back_n + i, queue[len(queue) - back_n + i]) for i in range(back_n)]
            )
            show_sep = True
        await asyncio.gather(*[fetch_title_only(s) for _, s in display_items])
        lines = []
        prev_idx = -1
        for idx, search in display_items:
            if show_sep and prev_idx != -1 and idx != prev_idx + 1:
                lines.append(f"_... {idx - prev_idx - 1}곡 생략 ..._")
            lines.append(f"`{idx + 1}.` {_title_cache_get(search) or format_queue_display(search)}")
            prev_idx = idx
        embed.add_field(name=f"대기열 ({len(queue)}곡)", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="playlist")
async def playlist_cmd(ctx, number: str, mode: str = None):
    """2026-05-17: 기본 순서대로, `shuffle` 인자 주면 셔플. Worker에서 fetch."""
    state = get_state(ctx.guild.id)
    is_shuffle = (mode and mode.lower() in ("shuffle", "random", "셔플", "랜덤"))

    # Worker에서 플레이리스트 fetch
    try:
        data = await fetch_playlists()
    except Exception as e:
        await ctx.send(embed=discord.Embed(
            description=f"플레이리스트 서버 연결 실패: `{e}`",
            color=discord.Color.red()))
        return

    playlists = {p["id"]: p for p in data.get("playlists", [])}

    # 2026-05-17: 대소문자 무관 매칭 (정확 매칭 우선, 없으면 lower 비교)
    if number.lower() == "all":
        selected = []
        for pid in sorted(playlists.keys(), key=lambda x: playlists[x].get("position", 0)):
            for s in playlists[pid]["songs"]:
                selected.append(s["url"])
        playlist_label = f"전체 ({len(playlists)}개 플레이리스트)"
    else:
        target = playlists.get(number) or next(
            (p for k, p in playlists.items() if k.lower() == number.lower()), None
        )
        if target is None:
            await ctx.send(embed=discord.Embed(
                description=f"플레이리스트 `{number}`는 존재하지 않아! (사용 가능: {', '.join(sorted(playlists.keys()))}, all)",
                color=discord.Color.red()))
            return
        selected = [s["url"] for s in target["songs"]]
        playlist_label = f"{target['id']}번 ({target['name']})"

    if not selected:
        await ctx.send(embed=discord.Embed(description="플레이리스트가 비어있어!", color=discord.Color.red()))
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send(embed=discord.Embed(description="음성 채널에 먼저 들어가줘!", color=discord.Color.red()))
        return

    voice_channel = ctx.author.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if not voice_client:
        voice_client = await voice_channel.connect()

    state["np_channel_id"] = ctx.channel.id

    if is_shuffle:
        random.shuffle(selected)

    mode_label = "셔플" if is_shuffle else "순서대로"

    if voice_client.is_playing() or voice_client.is_paused() or state["current_song"]:
        state["queue"].extend(selected)
        embed = discord.Embed(
            title="📂 플레이리스트 추가",
            description=f"{playlist_label} **{len(selected)}곡**을 대기열 뒤에 추가했어! ({mode_label})",
            color=discord.Color.from_rgb(244, 63, 116))
        await ctx.send(embed=embed)
    else:
        first = selected.pop(0)
        state["queue"].insert(0, first)
        state["queue"].extend(selected)
        embed = discord.Embed(
            title="📂 플레이리스트 시작",
            description=f"{playlist_label} **{len(selected) + 1}곡** 재생 시작! ({mode_label})",
            color=discord.Color.from_rgb(244, 63, 116))
        await ctx.send(embed=embed)
        await check_queue(ctx, channel=ctx.channel)


@bot.command(name="playlists")
async def playlists_show(ctx):
    """현재 워커에 등록된 플레이리스트 목록"""
    try:
        data = await fetch_playlists()
    except Exception as e:
        await ctx.send(embed=discord.Embed(
            description=f"서버 연결 실패: `{e}`", color=discord.Color.red()))
        return
    items = sorted(data.get("playlists", []), key=lambda p: p.get("position", 0))
    if not items:
        await ctx.send(embed=discord.Embed(description="플레이리스트가 없어!", color=discord.Color.red()))
        return
    lines = [f"`{p['id']}` · {p['name']} ({len(p['songs'])}곡)" for p in items]
    embed = discord.Embed(
        title="📂 플레이리스트 목록",
        description="\n".join(lines),
        color=discord.Color.from_rgb(244, 63, 116))
    embed.set_footer(text=f"편집은 🔑 편집 버튼으로 비밀번호 확인 후 {WORKER_URL}")
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")


# 2026-05-16: 하드코딩 토큰 → config.py에서 import
bot.run(DISCORD_TOKEN)
