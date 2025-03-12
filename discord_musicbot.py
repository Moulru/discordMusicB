import discord
from discord.ext import commands
import wavelink

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

queue = []  # 대기열을 저장할 리스트
current_song = None  # 현재 재생 중인 노래


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    # Lavalink 서버와 연결하기
    await wavelink.NodePool.create_node(
        bot=bot,  # 봇 객체
        host='127.0.0.1',  # 수정된 Lavalink 서버 주소
        port=2333,  # Lavalink 포트
        password='youshallnotpass'  # yml 파일에 설정한 비밀번호
    )
    print("Lavalink 서버에 연결됨")


@bot.command()
async def play(ctx, *, search: str = None):
    """노래 검색 및 재생"""
    global current_song

    if not search:
        await ctx.send("검색어를 입력해줘!")
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("음성 채널에 먼저 들어가 줘!")
        return

    voice_channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    if not voice_client:
        voice_client = await voice_channel.connect(cls=wavelink.Player)

    # 🔍 노래 검색 (유튜브)
    track = await wavelink.YouTubeTrack.search(query=search, return_first=True)

    if not track:
        await ctx.send("검색 결과가 없어!")
        return

    queue.append(track)

    if not voice_client.is_playing():
        await play_next(ctx)


async def play_next(ctx):
    """대기열에서 다음 노래 재생"""
    global current_song
    voice_client = ctx.voice_client

    if not queue:
        current_song = None
        return

    current_song = queue.pop(0)
    await voice_client.play(current_song)
    await ctx.send(f"🎵 {current_song.title} 재생 중!")


@bot.command()
async def skip(ctx):
    """현재 노래 스킵"""
    voice_client = ctx.voice_client
    if voice_client and voice_client.is_playing():
        await voice_client.stop()
        await ctx.send("⏭️ 현재 노래를 스킵하고 다음 노래를 재생할게!")
        await play_next(ctx)
    else:
        await ctx.send("현재 재생 중인 노래가 없어!")


@bot.command()
async def list(ctx):
    """대기열 확인"""
    if not queue and not current_song:
        await ctx.send("현재 재생 중인 노래와 대기열이 없어!")
        return

    queue_list = f"🎶 **현재 재생 중:** {current_song.title}\n"
    queue_list += "\n".join([f"{index + 1}. {track.title}" for index, track in enumerate(queue)])
    await ctx.send(f"**대기열:**\n{queue_list}")

bot.run("토큰")
