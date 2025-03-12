import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

queue = []  # 대기열을 저장할 리스트
current_song = None  # 현재 재생 중인 노래

# 노래가 끝나면 다음 노래를 재생하는 함수
async def check_queue(ctx):
    global current_song
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and len(queue) > 0:
        current_song = queue.pop(0)  # 대기열에서 첫 번째 노래를 꺼냄

        ydl_opts = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "quiet": True,
            "cookies": "./myCookies.txt",
            "headers": {
                "User-Agent": "Mozilla/5.0"
            },
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch:{current_song}", download=False)
            if not info["entries"]:
                await ctx.send("검색 결과가 없습니다.")
                return
            url = info["entries"][0]["url"]
            current_song = info["entries"][0]["title"]

        ffmpeg_options = {
            "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
            "options": "-vn -tune zerolatency"
        }

        def after_playing(error):
            asyncio.run_coroutine_threadsafe(check_queue(ctx), bot.loop)

        voice_client.play(discord.FFmpegPCMAudio(url, **ffmpeg_options), after=after_playing)
        await ctx.send(f"🎵 {current_song} 재생 중!")

@bot.command()
async def play(ctx, *, search: str = None):
    global current_song
    if not search:
        await ctx.send("검색어를 입력해주세요!")
        return

    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("음성 채널에 먼저 들어가 주세요!")
        return

    voice_channel = ctx.author.voice.channel
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)

    if not voice_client:
        voice_client = await voice_channel.connect()

    if voice_client.is_playing():
        queue.append(search)
        await ctx.send(f"현재 재생 중인 노래가 있습니다. 다음 노래 '{search}'을 예약합니다.")
        return

    queue.insert(0, search)
    await check_queue(ctx)

@bot.command()
async def skip(ctx):
    voice_client = discord.utils.get(bot.voice_clients, guild=ctx.guild)
    if voice_client and voice_client.is_playing():
        voice_client.stop()
        await ctx.send("현재 노래를 스킵하고 다음 노래를 재생합니다.")
        await check_queue(ctx)
    else:
        await ctx.send("현재 재생 중인 노래가 없습니다.")

@bot.command()
async def list(ctx):
    if not queue and not current_song:
        await ctx.send("현재 재생 중인 노래와 대기열이 없습니다.")
        return

    queue_list = f"현재 재생 중: {current_song}\n"
    queue_list += "\n".join([f"{index + 1}. {song}" for index, song in enumerate(queue)])
    await ctx.send(f"대기열:\n{queue_list}")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not TOKEN:
    raise ValueError("DISCORD_BOT_TOKEN 환경 변수를 설정해주세요!")

bot.run(TOKEN)
