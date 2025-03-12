import discord
from discord.ext import commands
import wavelink
import asyncio

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

queue = []  # 대기열을 저장할 리스트
current_song = None  # 현재 재생 중인 노래

# Lavalink 연결
class LavalinkBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.add_cog(MusicCog(self))

    async def on_ready(self):
        await bot.wait_until_ready()
        await self.connect_to_lavalink()

    async def connect_to_lavalink(self):
        await wavelink.NodePool.create_node(
            bot=self,
            host='your-lavalink-server-url',  # Lavalink 서버 주소
            port=2333,  # Lavalink 서버 포트
            password='1234'  # Lavalink 서버 비밀번호
        )

bot = LavalinkBot(command_prefix="!", intents=intents)

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def play(self, ctx, *, search: str = None):
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

        # Lavalink 음성 연결을 설정
        player = await voice_channel.connect_to_lavalink()

        if player.is_playing():
            queue.append(search)
            await ctx.send(f"현재 재생 중인 노래가 있습니다. '{search}'을 예약합니다.")
            return

        queue.insert(0, search)
        await self.check_queue(ctx)

    async def check_queue(self, ctx):
        global current_song
        if len(queue) > 0:
            current_song = queue.pop(0)
            player = await wavelink.Player.get(ctx.guild.id)

            # 노래 검색
            track = await wavelink.YouTubeTrack.search(current_song)

            if not track:
                await ctx.send("검색 결과가 없습니다.")
                return

            current_song = track[0].title
            await ctx.send(f"🎵 {current_song} 재생 중!")

            # 음악 재생
            await player.play(track[0])

            # 음악이 끝났을 때 다음 곡 재생
            player.add_listener(self.after_playing)

    async def after_playing(self, player):
        await self.check_queue(ctx)

    @commands.command()
    async def skip(self, ctx):
        player = await wavelink.Player.get(ctx.guild.id)
        if player.is_playing():
            await player.stop()
            await ctx.send("현재 노래를 스킵하고 다음 노래를 재생합니다.")
            await self.check_queue(ctx)
        else:
            await ctx.send("현재 재생 중인 노래가 없습니다.")

    @commands.command()
    async def list(self, ctx):
        if not queue and not current_song:
            await ctx.send("현재 재생 중인 노래와 대기열이 없습니다.")
            return

        queue_list = f"현재 재생 중: {current_song}\n"
        queue_list += "\n".join([f"{index + 1}. {song}" for index, song in enumerate(queue)])
        await ctx.send(f"대기열:\n{queue_list}")

# 봇 실행
TOKEN = "YOUR_DISCORD_BOT_TOKEN"
bot.run(TOKEN)
