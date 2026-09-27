import asyncio
import json
import os
import re
import threading
import time
from datetime import timedelta
from flask import Flask
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp as ytdl

# -------------------------------------------------------------
# 1. 24/7 Keep-Alive Web Server Setup (Flask)
# -------------------------------------------------------------
web_app = Flask('')

@web_app.route('/')
def home():
    return "Bot is online and running 24/7!"

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web_server)
    t.daemon = True
    t.start()

# -------------------------------------------------------------
# 2. Configuration & Channel Names
# -------------------------------------------------------------
WELCOME_CHANNEL_NAME = "welcome"
LEVEL_CHANNEL_NAME = "level-up"
MUSIC_CHANNEL_NAME = "music"
TICKET_CHANNEL_NAME = "tickets"
MODERATOR_ROLE_NAME = "Moderator"

# Specific level roles to give and remove lower ones
LEVEL_TIER_CONFIG = {
    1: {"base_role": "New Member", "level_role": "Level 1"},
    10: {"base_role": "Member", "level_role": "Level 10"},
    50: {"base_role": "Special Member", "level_role": "Level 50"},
    100: {"base_role": "Special Member", "level_role": "Level 100"}
}

# -------------------------------------------------------------
# 3. Bot Setup & Persistence
# -------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

playlists = {}
invites_cache = {}
user_xp_cooldowns = {}
XP_COOLDOWN_SECONDS = 300  # 5 minutes

XP_FILE = "xp_data.json"

def load_xp():
    if os.path.exists(XP_FILE):
        with open(XP_FILE, "r") as f:
            return json.load(f)
    return {}

def save_xp(data):
    with open(XP_FILE, "w") as f:
        json.dump(data, f, indent=4)

xp_data = load_xp()

# -------------------------------------------------------------
# 4. Audio Setup (yt-dlp / FFmpeg)
# -------------------------------------------------------------
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl_client = ytdl.YoutubeDL(YTDL_OPTIONS)

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('webpage_url')

    @classmethod
    async def from_search(cls, query, *, loop=None):
        loop = loop or asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, lambda: ytdl_client.extract_info(query, download=False)
        )
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url']
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)

# -------------------------------------------------------------
# 5. Role & XP Logic
# -------------------------------------------------------------
async def update_user_level_roles(member: discord.Member, new_level: int):
    """Assigns current tier roles and removes old tier roles."""
    guild = member.guild
    milestones = sorted(LEVEL_TIER_CONFIG.keys())

    target_tier = None
    for m in milestones:
        if new_level >= m:
            target_tier = m

    if not target_tier:
        return

    config = LEVEL_TIER_CONFIG[target_tier]
    
    # Identify roles to remove from lower tiers
    roles_to_remove = []
    for m in milestones:
        if m != target_tier:
            old_base = discord.utils.get(guild.roles, name=LEVEL_TIER_CONFIG[m]["base_role"])
            old_lvl = discord.utils.get(guild.roles, name=LEVEL_TIER_CONFIG[m]["level_role"])
            if old_base and old_base in member.roles and LEVEL_TIER_CONFIG[m]["base_role"] != config["base_role"]:
                roles_to_remove.append(old_base)
            if old_lvl and old_lvl in member.roles:
                roles_to_remove.append(old_lvl)

    if roles_to_remove:
        try:
            await member.remove_roles(*roles_to_remove)
        except Exception:
            pass

    # Add new tier roles
    new_base_role = discord.utils.get(guild.roles, name=config["base_role"])
    new_level_role = discord.utils.get(guild.roles, name=config["level_role"])

    roles_to_add = [r for r in [new_base_role, new_level_role] if r and r not in member.roles]
    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add)
        except Exception:
            pass

async def grant_xp_if_eligible(guild: discord.Guild, member: discord.Member):
    """Grants 10 XP if user hasn't messaged in the last 5 minutes (500 XP = level up)."""
    if member.bot:
        return

    key = f"{guild.id}_{member.id}"
    now = time.time()
    last_xp_time = user_xp_cooldowns.get(key, 0)

    if now - last_xp_time < XP_COOLDOWN_SECONDS:
        return

    user_xp_cooldowns[key] = now

    guild_id = str(guild.id)
    user_id = str(member.id)

    if guild_id not in xp_data:
        xp_data[guild_id] = {}
    if user_id not in xp_data[guild_id]:
        xp_data[guild_id][user_id] = {"xp": 0, "level": 1}

    xp_data[guild_id][user_id]["xp"] += 10
    current_xp = xp_data[guild_id][user_id]["xp"]
    current_level = xp_data[guild_id][user_id]["level"]

    needed_xp = 500  # Fixed 500 XP per level requirement

    if current_xp >= needed_xp:
        xp_data[guild_id][user_id]["level"] += 1
        xp_data[guild_id][user_id]["xp"] = current_xp - needed_xp
        new_level = xp_data[guild_id][user_id]["level"]
        save_xp(xp_data)

        # Send Level Up notification in level channel
        lvl_channel = discord.utils.get(guild.text_channels, name=LEVEL_CHANNEL_NAME)
        if lvl_channel:
            embed = discord.Embed(
                title="🎉 Level Up!",
                description=f"Congratulations {member.mention}, you reached **Level {new_level}**!",
                color=discord.Color.gold()
            )
            embed.set_field_at(0, name="Level", value=str(new_level), inline=True) if len(embed.fields) > 0 else embed.add_field(name="Level", value=str(new_level), inline=True)
            if guild.icon:
                embed.set_thumbnail(url=guild.icon.url)
            if guild.banner:
                embed.set_image(url=guild.banner.url)
            await lvl_channel.send(embed=embed)

        await update_user_level_roles(member, new_level)
    else:
        save_xp(xp_data)

# -------------------------------------------------------------
# 6. Ticket UI Components
# -------------------------------------------------------------
class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close Ticket", style=discord.ButtonStyle.red, custom_id="close_ticket_btn", emoji="🔒")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("🔒 Closing this ticket in 5 seconds...")
        await asyncio.sleep(5)
        await interaction.channel.delete()

class TicketLauncherView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create a Ticket", style=discord.ButtonStyle.primary, custom_id="create_ticket_btn", emoji="🎫")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        # Fetch or create Moderator role
        mod_role = discord.utils.get(guild.roles, name=MODERATOR_ROLE_NAME)

        # Define private permission overrides
        overrides = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }

        if mod_role:
            overrides[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        category = interaction.channel.category
        ticket_channel = await guild.create_text_channel(
            name=f"ticket-{member.name}",
            category=category,
            overrides=overrides
        )

        embed = discord.Embed(
            title="🎟️ Support Ticket Created",
            description=f"Welcome {member.mention}! A moderator will be with you shortly.\nClick the button below when you are ready to close this ticket.",
            color=discord.Color.blue()
        )

        await ticket_channel.send(content=f"{member.mention}", embed=embed, view=CloseTicketView())
        await interaction.response.send_message(f"✅ Ticket created: {ticket_channel.mention}", ephemeral=True)

# -------------------------------------------------------------
# 7. Event Handlers
# -------------------------------------------------------------
@bot.event
async def on_ready():
    # Register persistent view for ticket buttons
    bot.add_view(TicketLauncherView())
    bot.add_view(CloseTicketView())
    await bot.tree.sync()
    print(f"Bot connected as {bot.user}")

    for guild in bot.guilds:
        try:
            guild_invites = await guild.invites()
            invites_cache[guild.id] = {inv.code: inv.uses for inv in guild_invites}
        except Exception:
            invites_cache[guild.id] = {}

@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild

    # Track who invited the new user
    inviter_mention = None
    old_invites = invites_cache.get(guild.id, {})
    try:
        new_invites = await guild.invites()
        for inv in new_invites:
            if inv.code in old_invites and inv.uses > old_invites[inv.code]:
                inviter_mention = inv.inviter.mention
                break
            elif inv.uses > 0 and inv.code not in old_invites:
                inviter_mention = inv.inviter.mention
                break
        invites_cache[guild.id] = {inv.code: inv.uses for inv in new_invites}
    except Exception:
        pass

    # Default joining user to Level 1 and assign Level 1 roles
    guild_id = str(guild.id)
    user_id = str(member.id)
    if guild_id not in xp_data:
        xp_data[guild_id] = {}
    xp_data[guild_id][user_id] = {"xp": 0, "level": 1}
    save_xp(xp_data)

    await update_user_level_roles(member, 1)

    # Format Welcome Embed
    welcome_channel = discord.utils.get(guild.text_channels, name=WELCOME_CHANNEL_NAME)
    if welcome_channel:
        embed = discord.Embed(
            title="🎉 **WELCOME TO THE SERVER!** 🎉",
            color=discord.Color.blue()
        )
        
        description_text = f"**Member joined:** {member.mention}\n"
        if inviter_mention:
            description_text += f"**Invited by:** {inviter_mention}\n"
        description_text += f"**Level:** Level 1"

        embed.description = description_text

        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)
        if guild.banner:
            embed.set_image(url=guild.banner.url)

        await welcome_channel.send(content=f"{member.mention}", embed=embed)

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    await grant_xp_if_eligible(message.guild, message.author)
    await bot.process_commands(message)

# -------------------------------------------------------------
# 8. Slash Commands
# -------------------------------------------------------------
@bot.tree.command(name="level", description="Check your current level and XP")
async def level(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    guild_id = str(interaction.guild_id)
    user_id = str(target.id)

    user_data = xp_data.get(guild_id, {}).get(user_id, {"xp": 0, "level": 1})
    lvl = user_data["level"]
    xp = user_data["xp"]

    embed = discord.Embed(title=f"📊 Stats - {target.display_name}", color=discord.Color.green())
    embed.add_field(name="Level", value=f"Level {lvl}", inline=True)
    embed.add_field(name="XP Progress", value=f"{xp} / 500 XP", inline=True)

    if interaction.guild.icon:
        embed.set_thumbnail(url=interaction.guild.icon.url)
    if interaction.guild.banner:
        embed.set_image(url=interaction.guild.banner.url)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="setup_tickets", description="Deploy the persistent ticket system panel")
@app_commands.checks.has_permissions(administrator=True)
async def setup_tickets(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎫 **Support Center**",
        description="Need help or have questions? Click the button below to open a private support ticket with our moderation team.",
        color=discord.Color.blue()
    )
    await interaction.channel.send(embed=embed, view=TicketLauncherView())
    await interaction.response.send_message("✅ Ticket system launcher posted!", ephemeral=True)

# --- Music Commands ---
async def check_music_channel(interaction: discord.Interaction) -> bool:
    if interaction.channel.name.lower() != MUSIC_CHANNEL_NAME.lower():
        await interaction.response.send_message(
            f"❌ Music commands are only allowed in **#{MUSIC_CHANNEL_NAME}**!",
            ephemeral=True
        )
        return False
    return True

@bot.tree.command(name="play", description="Play audio in your voice channel")
async def play(interaction: discord.Interaction, query: str):
    if not await check_music_channel(interaction):
        return
    if not interaction.user.voice or not interaction.user.voice.channel:
        return await interaction.response.send_message("❌ You must join a voice channel first!", ephemeral=True)

    await interaction.response.defer()
    voice_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_client.channel != voice_channel:
        await voice_client.move_to(voice_channel)

    try:
        player = await YTDLSource.from_search(query, loop=bot.loop)
        if voice_client.is_playing() or voice_client.is_paused():
            voice_client.stop()
        voice_client.play(player)
        voice_client.current_track = {"title": player.title, "url": player.url}
        await interaction.followup.send(f"🎶 **Now Playing:** `{player.title}`")
    except Exception as e:
        await interaction.followup.send(f"⚠️ Playback error: {e}")

@bot.tree.command(name="pause", description="Pause active audio")
async def pause(interaction: discord.Interaction):
    if not await check_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ Paused.")
    else:
        await interaction.response.send_message("❌ No active track playing.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop music and disconnect")
async def stop(interaction: discord.Interaction):
    if not await check_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
        await vc.disconnect()
        await interaction.response.send_message("⏹️ Disconnected.")
    else:
        await interaction.response.send_message("❌ Bot is not connected.", ephemeral=True)

@bot.tree.command(name="save_in_playlist", description="Save current song to server playlist")
async def save_in_playlist(interaction: discord.Interaction):
    if not await check_music_channel(interaction):
        return
    vc = interaction.guild.voice_client
    if not vc or not getattr(vc, "current_track", None):
        return await interaction.response.send_message("❌ No track currently active!", ephemeral=True)

    guild_id = interaction.guild_id
    if guild_id not in playlists:
        playlists[guild_id] = []

    track = vc.current_track
    if track in playlists[guild_id]:
        return await interaction.response.send_message(f"ℹ️ `{track['title']}` is already in playlist.")

    playlists[guild_id].append(track)
    await interaction.response.send_message(f"⭐ Saved `{track['title']}` to playlist.")

# -------------------------------------------------------------
# 9. Main Execution
# -------------------------------------------------------------
if __name__ == "__main__":
    # Start web server for 24/7 pings
    keep_alive()

    # REPLACE WITH YOUR DISCORD BOT TOKEN
    bot.run("MTU1Mzc4MTY1NDA1NTgxNzIzOA.GAkW5M.Z9L9RgG2RUPrRpWHEdImrloKdQqB5WAjibWnvY")
