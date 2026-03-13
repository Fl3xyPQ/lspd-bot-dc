import logging
import os
from dataclasses import dataclass
from datetime import timedelta
from importlib.util import find_spec

import discord
from discord import app_commands
from discord.ext import commands


logging.basicConfig(
	level=logging.INFO,
	format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("lspd-bot")


if find_spec("dotenv") is not None:
	dotenv_module = __import__("dotenv")
	dotenv_module.load_dotenv()


def _parse_int(value: str | None, default: int = 0) -> int:
	if not value:
		return default
	try:
		return int(value)
	except ValueError:
		return default


def _parse_int_set(value: str | None) -> set[int]:
	if not value:
		return set()
	result: set[int] = set()
	for item in value.split(","):
		item = item.strip()
		if item.isdigit():
			result.add(int(item))
	return result


@dataclass(slots=True)
class RewriteRequest:
	channel_id: int
	fake_name: str
	avatar_url: str | None


class LSPDBot(commands.Bot):
	def __init__(self) -> None:
		intents = discord.Intents.default()
		intents.guilds = True
		intents.members = True
		intents.messages = True
		intents.message_content = True

		super().__init__(
			command_prefix="!",
			intents=intents,
			help_command=None,
		)

		self.log_channel_id = _parse_int(os.getenv("LOG_CHANNEL_ID"))
		self.auto_eye_channels = _parse_int_set(os.getenv("AUTO_EYE_CHANNEL_IDS"))
		self.guild_id = _parse_int(os.getenv("GUILD_ID"))
		self.pending_rewrites: dict[int, RewriteRequest] = {}
		self.webhook_cache: dict[int, discord.Webhook] = {}
		self.synced = False

	async def setup_hook(self) -> None:
		if self.guild_id:
			guild_obj = discord.Object(id=self.guild_id)
			self.tree.copy_global_to(guild=guild_obj)
			await self.tree.sync(guild=guild_obj)
			logger.info("Slash commands synced for guild %s", self.guild_id)
		else:
			await self.tree.sync()
			logger.info("Slash commands synced globally")

	async def on_ready(self) -> None:
		logger.info("Přihlášen jako %s (%s)", self.user, self.user.id if self.user else "-")

	async def log_event(self, title: str, description: str, color: discord.Color = discord.Color.blue()) -> None:
		if not self.log_channel_id:
			return

		channel = self.get_channel(self.log_channel_id)
		if channel is None:
			try:
				channel = await self.fetch_channel(self.log_channel_id)
			except discord.DiscordException as exc:
				logger.warning("Nelze načíst log channel %s: %s", self.log_channel_id, exc)
				return

		if not isinstance(channel, discord.TextChannel):
			return

		embed = discord.Embed(title=title, description=description, color=color)
		embed.timestamp = discord.utils.utcnow()
		try:
			await channel.send(embed=embed)
		except discord.DiscordException as exc:
			logger.warning("Log send failed: %s", exc)

	async def get_or_create_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
		cached = self.webhook_cache.get(channel.id)
		if cached:
			return cached

		hooks = await channel.webhooks()
		for hook in hooks:
			if hook.user and self.user and hook.user.id == self.user.id and hook.name == "LSPD Rewrite":
				self.webhook_cache[channel.id] = hook
				return hook

		webhook = await channel.create_webhook(name="LSPD Rewrite")
		self.webhook_cache[channel.id] = webhook
		return webhook

	async def on_message(self, message: discord.Message) -> None:
		if message.author.bot:
			return

		if message.channel.id in self.auto_eye_channels:
			try:
				await message.add_reaction("👁️")
			except discord.DiscordException:
				pass

		pending = self.pending_rewrites.get(message.author.id)
		if pending and pending.channel_id == message.channel.id and isinstance(message.channel, discord.TextChannel):
			self.pending_rewrites.pop(message.author.id, None)
			content = message.content
			files = [await attachment.to_file() for attachment in message.attachments]

			try:
				await message.delete()
			except discord.DiscordException:
				pass

			webhook = await self.get_or_create_webhook(message.channel)
			await webhook.send(
				content=content if content else None,
				username=pending.fake_name,
				avatar_url=pending.avatar_url,
				files=files,
				allowed_mentions=discord.AllowedMentions.none(),
			)
			await self.log_event(
				"🕵️ Přepis zprávy",
				f"Uživatel {message.author.mention} přepsal zprávu v {message.channel.mention} jako **{pending.fake_name}**.",
				discord.Color.orange(),
			)


bot = LSPDBot()


def _check_manage_messages(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.manage_messages
	return False


@bot.tree.command(name="vymazat", description="Smaže poslední zprávy v aktuálním kanálu")
@app_commands.describe(pocet="Kolik zpráv smazat (1-100)")
async def vymazat(interaction: discord.Interaction, pocet: app_commands.Range[int, 1, 100]) -> None:
	if not _check_manage_messages(interaction):
		await interaction.response.send_message("Nemáš oprávnění `manage_messages`.", ephemeral=True)
		return

	channel = interaction.channel
	if not isinstance(channel, discord.TextChannel):
		await interaction.response.send_message("Tento příkaz lze použít jen v textovém kanálu.", ephemeral=True)
		return

	await interaction.response.defer(ephemeral=True)
	deleted = await channel.purge(limit=pocet, reason=f"/vymazat by {interaction.user}")
	await interaction.followup.send(f"Smazáno zpráv: **{len(deleted)}**", ephemeral=True)
	await bot.log_event(
		"🧹 Vymazání zpráv",
		f"{interaction.user.mention} smazal **{len(deleted)}** zpráv v {channel.mention}.",
		discord.Color.red(),
	)


@bot.tree.command(name="kick", description="Vyhodí člena ze serveru")
@app_commands.default_permissions(kick_members=True)
@app_commands.describe(uzivatel="Uživatel k vyhození", duvod="Důvod")
async def kick(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	duvod: str = "Bez důvodu",
) -> None:
	if not interaction.user.guild_permissions.kick_members:
		await interaction.response.send_message("Nemáš oprávnění `kick_members`.", ephemeral=True)
		return

	await uzivatel.kick(reason=f"{duvod} | by {interaction.user}")
	await interaction.response.send_message(f"Uživatel {uzivatel.mention} byl vyhozen.")
	await bot.log_event(
		"👢 Kick",
		f"{interaction.user.mention} vyhodil {uzivatel.mention}. Důvod: **{duvod}**",
		discord.Color.red(),
	)


@bot.tree.command(name="ban", description="Zabanuje člena serveru")
@app_commands.default_permissions(ban_members=True)
@app_commands.describe(uzivatel="Uživatel k banu", duvod="Důvod")
async def ban(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	duvod: str = "Bez důvodu",
) -> None:
	if not interaction.user.guild_permissions.ban_members:
		await interaction.response.send_message("Nemáš oprávnění `ban_members`.", ephemeral=True)
		return

	await uzivatel.ban(reason=f"{duvod} | by {interaction.user}")
	await interaction.response.send_message(f"Uživatel {uzivatel} byl zabanován.")
	await bot.log_event(
		"🔨 Ban",
		f"{interaction.user.mention} zabanoval {uzivatel}. Důvod: **{duvod}**",
		discord.Color.dark_red(),
	)


@bot.tree.command(name="timeout", description="Udělí timeout uživateli (v minutách)")
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(uzivatel="Uživatel", minuty="Délka timeoutu", duvod="Důvod")
async def timeout(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	minuty: app_commands.Range[int, 1, 40320],
	duvod: str = "Bez důvodu",
) -> None:
	if not interaction.user.guild_permissions.moderate_members:
		await interaction.response.send_message("Nemáš oprávnění `moderate_members`.", ephemeral=True)
		return

	until = discord.utils.utcnow() + timedelta(minutes=minuty)
	await uzivatel.edit(timed_out_until=until, reason=f"{duvod} | by {interaction.user}")
	await interaction.response.send_message(f"Uživatel {uzivatel.mention} má timeout na {minuty} min.")
	await bot.log_event(
		"⏳ Timeout",
		f"{interaction.user.mention} dal timeout {uzivatel.mention} na **{minuty}** min. Důvod: **{duvod}**",
		discord.Color.gold(),
	)


@bot.tree.command(name="odtimeout", description="Zruší timeout uživateli")
@app_commands.default_permissions(moderate_members=True)
@app_commands.describe(uzivatel="Uživatel")
async def odtimeout(interaction: discord.Interaction, uzivatel: discord.Member) -> None:
	if not interaction.user.guild_permissions.moderate_members:
		await interaction.response.send_message("Nemáš oprávnění `moderate_members`.", ephemeral=True)
		return

	await uzivatel.edit(timed_out_until=None, reason=f"odtimeout by {interaction.user}")
	await interaction.response.send_message(f"Timeout uživatele {uzivatel.mention} byl zrušen.")
	await bot.log_event(
		"✅ Timeout zrušen",
		f"{interaction.user.mention} zrušil timeout uživateli {uzivatel.mention}.",
		discord.Color.green(),
	)


@bot.tree.command(name="prepis", description="Další tvoji zprávu bot přepošle pod zadaným jménem")
@app_commands.describe(
	jmeno="Jméno bota pro přepis", avatar_url="URL avataru (volitelné)")
async def prepis(
	interaction: discord.Interaction,
	jmeno: app_commands.Range[str, 2, 32],
	avatar_url: str | None = None,
) -> None:
	if not _check_manage_messages(interaction):
		await interaction.response.send_message("Nemáš oprávnění `manage_messages`.", ephemeral=True)
		return

	if not isinstance(interaction.channel, discord.TextChannel):
		await interaction.response.send_message("Příkaz lze použít jen v textovém kanálu.", ephemeral=True)
		return

	bot.pending_rewrites[interaction.user.id] = RewriteRequest(
		channel_id=interaction.channel.id,
		fake_name=jmeno,
		avatar_url=avatar_url,
	)
	await interaction.response.send_message(
		"Pošli teď další zprávu. Smažu ji a přepošlu jako bot.",
		ephemeral=True,
	)


@bot.event
async def on_message_delete(message: discord.Message) -> None:
	if message.author.bot or not isinstance(message.channel, discord.TextChannel):
		return
	if message.guild is None:
		return
	if not message.content:
		return
	await bot.log_event(
		"🗑️ Smazaná zpráva",
		f"Autor: {message.author.mention}\nKanál: {message.channel.mention}\nObsah: {message.content[:1400]}",
		discord.Color.red(),
	)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
	if before.author.bot or not isinstance(before.channel, discord.TextChannel):
		return
	if before.guild is None:
		return
	if before.content == after.content:
		return
	await bot.log_event(
		"✏️ Upravená zpráva",
		(
			f"Autor: {before.author.mention}\nKanál: {before.channel.mention}"
			f"\nPřed: {before.content[:700]}\nPo: {after.content[:700]}"
		),
		discord.Color.orange(),
	)


@bot.event
async def on_member_join(member: discord.Member) -> None:
	await bot.log_event(
		"📥 Member join",
		f"{member.mention} se připojil na server.",
		discord.Color.green(),
	)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
	await bot.log_event(
		"📤 Member leave",
		f"{member} odešel ze serveru.",
		discord.Color.dark_grey(),
	)


def main() -> None:
	token = os.getenv("DISCORD_TOKEN")
	if not token:
		raise RuntimeError("Chybí DISCORD_TOKEN v proměnných prostředí.")
	bot.run(token, log_handler=None)


if __name__ == "__main__":
	main()
