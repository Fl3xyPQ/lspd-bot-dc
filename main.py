import logging
import os
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path

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


def _parse_name_set(value: str | None) -> set[str]:
	if not value:
		return set()
	return {item.strip().casefold() for item in value.split(",") if item.strip()}


def _coerce_int(value: object, default: int = 0) -> int:
	if isinstance(value, bool):
		return int(value)
	if isinstance(value, int):
		return value
	if isinstance(value, str):
		try:
			return int(value)
		except ValueError:
			return default
	return default


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
		self.admin_role_ids = _parse_int_set(os.getenv("ADMIN_ROLE_IDS"))
		self.mod_role_ids = _parse_int_set(os.getenv("MOD_ROLE_IDS"))
		self.prepis_role_ids = _parse_int_set(os.getenv("PREPIS_ROLE_IDS"))
		self.admin_role_names = _parse_name_set(os.getenv("ADMIN_ROLE_NAMES"))
		self.mod_role_names = _parse_name_set(os.getenv("MOD_ROLE_NAMES"))
		self.prepis_role_names = _parse_name_set(os.getenv("PREPIS_ROLE_NAMES"))
		self.pending_rewrites: dict[int, RewriteRequest] = {}
		self.webhook_cache: dict[int, discord.Webhook] = {}
		self.synced = False

	async def setup_hook(self) -> None:
		load_duty_records()
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


def _member_has_role(member: discord.Member, role_ids: set[int], role_names: set[str]) -> bool:
	for role in member.roles:
		if role.id in role_ids or role.name.casefold() in role_names:
			return True
	return False


def _has_bot_access(member: discord.Member, access_level: str) -> bool:
	if member.guild_permissions.administrator:
		return True

	if _member_has_role(member, bot.admin_role_ids, bot.admin_role_names):
		return True

	if access_level == "admin":
		return False

	if _member_has_role(member, bot.mod_role_ids, bot.mod_role_names):
		return True

	if access_level == "mod":
		return False

	if access_level == "prepis":
		return _member_has_role(member, bot.prepis_role_ids, bot.prepis_role_names)

	return False


def _check_manage_messages(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.manage_messages or _has_bot_access(member, "mod")
	return False


def _check_prepis_access(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.manage_messages or _has_bot_access(member, "prepis")
	return False


def _check_kick_access(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.kick_members or _has_bot_access(member, "mod")
	return False


def _check_ban_access(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.ban_members or _has_bot_access(member, "admin")
	return False


def _check_timeout_access(interaction: discord.Interaction) -> bool:
	member = interaction.user
	if isinstance(member, discord.Member):
		return member.guild_permissions.moderate_members or _has_bot_access(member, "mod")
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
@app_commands.describe(uzivatel="Uživatel k vyhození", duvod="Důvod")
async def kick(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	duvod: str = "Bez důvodu",
) -> None:
	if not _check_kick_access(interaction):
		await interaction.response.send_message("Nemáš oprávnění pro `/kick`.", ephemeral=True)
		return

	await uzivatel.kick(reason=f"{duvod} | by {interaction.user}")
	await interaction.response.send_message(f"Uživatel {uzivatel.mention} byl vyhozen.")
	await bot.log_event(
		"👢 Kick",
		f"{interaction.user.mention} vyhodil {uzivatel.mention}. Důvod: **{duvod}**",
		discord.Color.red(),
	)


@bot.tree.command(name="ban", description="Zabanuje člena serveru")
@app_commands.describe(uzivatel="Uživatel k banu", duvod="Důvod")
async def ban(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	duvod: str = "Bez důvodu",
) -> None:
	if not _check_ban_access(interaction):
		await interaction.response.send_message("Nemáš oprávnění pro `/ban`.", ephemeral=True)
		return

	await uzivatel.ban(reason=f"{duvod} | by {interaction.user}")
	await interaction.response.send_message(f"Uživatel {uzivatel} byl zabanován.")
	await bot.log_event(
		"🔨 Ban",
		f"{interaction.user.mention} zabanoval {uzivatel}. Důvod: **{duvod}**",
		discord.Color.dark_red(),
	)


@bot.tree.command(name="timeout", description="Udělí timeout uživateli (v minutách)")
@app_commands.describe(uzivatel="Uživatel", minuty="Délka timeoutu", duvod="Důvod")
async def timeout(
	interaction: discord.Interaction,
	uzivatel: discord.Member,
	minuty: app_commands.Range[int, 1, 40320],
	duvod: str = "Bez důvodu",
) -> None:
	if not _check_timeout_access(interaction):
		await interaction.response.send_message("Nemáš oprávnění pro `/timeout`.", ephemeral=True)
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
@app_commands.describe(uzivatel="Uživatel")
async def odtimeout(interaction: discord.Interaction, uzivatel: discord.Member) -> None:
	if not _check_timeout_access(interaction):
		await interaction.response.send_message("Nemáš oprávnění pro `/odtimeout`.", ephemeral=True)
		return

	await uzivatel.edit(timed_out_until=None, reason=f"odtimeout by {interaction.user}")
	await interaction.response.send_message(f"Timeout uživatele {uzivatel.mention} byl zrušen.")
	await bot.log_event(
		"✅ Timeout zrušen",
		f"{interaction.user.mention} zrušil timeout uživateli {uzivatel.mention}.",
		discord.Color.green(),
	)


DEFAULT_PREPIS_NAME = "Los Santos Police Department"


class PrepisModal(discord.ui.Modal, title="Přepis zprávy"):
	jmeno: discord.ui.TextInput = discord.ui.TextInput(
		label="Jméno bota",
		min_length=2,
		max_length=32,
	)
	avatar_url: discord.ui.TextInput = discord.ui.TextInput(
		label="URL avataru (volitelné)",
		placeholder="https://example.com/avatar.png",
		required=False,
		max_length=512,
	)

	def __init__(self, default_name: str, default_avatar: str = "") -> None:
		super().__init__()
		self.jmeno.default = default_name
		self.avatar_url.default = default_avatar

	async def on_submit(self, interaction: discord.Interaction) -> None:
		if not isinstance(interaction.channel, discord.TextChannel):
			await interaction.response.send_message("Příkaz lze použít jen v textovém kanálu.", ephemeral=True)
			return

		bot.pending_rewrites[interaction.user.id] = RewriteRequest(
			channel_id=interaction.channel.id,
			fake_name=self.jmeno.value,
			avatar_url=self.avatar_url.value or None,
		)
		await interaction.response.send_message(
			f"Pošli teď další zprávu. Smažu ji a přepošlu jako **{self.jmeno.value}**.",
			ephemeral=True,
		)


@bot.tree.command(name="prepis", description="Přepošle tvoji další zprávu jako bot. Bez parametrů použije výchozí jméno.")
@app_commands.describe(
	jmeno="Vlastní jméno bota (otevře widget pro úpravu)",
	avatar_url="URL vlastního avataru (otevře widget pro úpravu)",
)
async def prepis(
	interaction: discord.Interaction,
	jmeno: str | None = None,
	avatar_url: str | None = None,
) -> None:
	if not _check_prepis_access(interaction):
		await interaction.response.send_message("Nemáš oprávnění pro `/prepis`.", ephemeral=True)
		return

	if not isinstance(interaction.channel, discord.TextChannel):
		await interaction.response.send_message("Příkaz lze použít jen v textovém kanálu.", ephemeral=True)
		return

	# Žádný parametr → výchozí jméno, okamžitě bez widgetu
	if jmeno is None and avatar_url is None:
		bot.pending_rewrites[interaction.user.id] = RewriteRequest(
			channel_id=interaction.channel.id,
			fake_name=DEFAULT_PREPIS_NAME,
			avatar_url=None,
		)
		await interaction.response.send_message(
			f"Pošli teď další zprávu. Smažu ji a přepošlu jako **{DEFAULT_PREPIS_NAME}**.",
			ephemeral=True,
		)
		return

	# Alespoň jeden parametr → otevři modal s předvyplněnými hodnotami
	modal = PrepisModal(
		default_name=jmeno or DEFAULT_PREPIS_NAME,
		default_avatar=avatar_url or "",
	)
	await interaction.response.send_modal(modal)


# ---------------------------------------------------------------------------
# /příkazy – interaktivní přehled příkazů bota
# ---------------------------------------------------------------------------

COMMAND_PAGES: dict[str, tuple[str, discord.Color, list[tuple[str, str]]]] = {
	"moderace": (
		"🛡️ Moderační příkazy",
		discord.Color.blue(),
		[
			("/kick", "Vyhodí člena ze serveru. Povoleno pro Discord `Kick Members` nebo **administrátory**."),
			("/ban", "Zabanuje člena serveru. Povoleno pro Discord `Ban Members` nebo **administrátory**."),
			("/timeout", "Udělí timeout uživateli. Povoleno pro Discord `Moderate Members` nebo **administrátory**."),
			("/odtimeout", "Zruší timeout. Povoleno pro Discord `Moderate Members` nebo **administrátory**."),
		],
	),
	"zprávy": (
		"✉️ Příkazy pro zprávy",
		discord.Color.green(),
		[
			("/vymazat", "Smaže 1–100 posledních zpráv. Povoleno pro Discord `Manage Messages` nebo **administrátory**."),
			(
				"/prepis",
				"Tvoji další zprávu bot přepošle jako webhook.\n"
				"• Bez parametrů → odesláno jako **Los Santos Police Department**.\n"
				"• S parametry `jméno`/`avatar_url` → otevře se widget pro úpravu.\n"
				"• Povoleno pro Discord `Manage Messages` nebo **administrátory**.",
			),
		],
	),
	"automatizace": (
		"⚙️ Automatizace & logy",
		discord.Color.gold(),
		[
			("👁️ Auto-reakce", "V určených kanálech bot automaticky reaguje na každou zprávu emoji 👁️."),
			("📥 Připojení/odchod člena", "Každý příchod a odchod člena je zaznamenán v log kanálu."),
			("🗑️ Smazané zprávy", "Obsah smazané zprávy je automaticky uložen do logu."),
			("✏️ Upravené zprávy", "Při editaci zprávy se do logu uloží verze před i po úpravě."),
			("🧹 Purge log", "Každé použití `/vymazat` se zaznamená s počtem smazaných zpráv."),
			("🕵️ Přepis log", "Každé použití `/prepis` se zaznamená včetně použitého jména."),
		],
	),
	"sluzba": (
		"📂 Služební složka",
		discord.Color.green(),
		[
			(
				"/sluzba",
				"Otevře tvoji osobní služební složku s tlačítky pro nástup/ukončení služby.\n"
				"• Při prvním použití vyplníš formulář (jméno + číslo odznaku).\n"
				"• Bot automaticky počítá délku aktuální i celkovou dobu ve službě.\n"
				"• `/sluzba @člen` – zobrazí složku jiného člena (vyžaduje **mod/admin**).",
			),
			(
				"/kontrola_duty",
				"Zobrazí tabulkový přehled všech registrovaných složek včetně aktuální duty a celkového času.\n"
				"• Výpis je seřazený tak, aby byli členové ve službě nahoře.\n"
				"• Přístup mají pouze **mod/admin**.",
			),
		],
	),
}


class KategoriePrikazuSelect(discord.ui.Select):
	def __init__(self) -> None:
		options = [
			discord.SelectOption(label="Moderace", value="moderace", emoji="🛡️", description="kick, ban, timeout, odtimeout"),
			discord.SelectOption(label="Zprávy", value="zprávy", emoji="✉️", description="vymazat, přepis"),
			discord.SelectOption(label="Automatizace & logy", value="automatizace", emoji="⚙️", description="auto-reakce, logy"),
			discord.SelectOption(label="Služební složka", value="sluzba", emoji="📂", description="/sluzba – nástup/ukončení, hodiny"),
		]
		super().__init__(placeholder="Vyber kategorii příkazů…", options=options)

	async def callback(self, interaction: discord.Interaction) -> None:
		category = self.values[0]
		title, color, fields = COMMAND_PAGES[category]
		embed = discord.Embed(title=title, color=color)
		embed.set_footer(text="LSPD Bot • Los Santos Police Department")
		for name, desc in fields:
			embed.add_field(name=name, value=desc, inline=False)
		await interaction.response.edit_message(embed=embed, view=self.view)


class PrikazyView(discord.ui.View):
	def __init__(self) -> None:
		super().__init__(timeout=120)
		self.add_item(KategoriePrikazuSelect())

	async def on_timeout(self) -> None:
		for item in self.children:
			if isinstance(item, discord.ui.Select):
				item.disabled = True


def _uvodni_embed() -> discord.Embed:
	embed = discord.Embed(
		title="📋 LSPD Bot – přehled příkazů",
		description=(
			"Vítej v interaktivním přehledu funkcí bota.\n"
			"Pomocí menu níže si prohlédni dostupné příkazy podle kategorie."
		),
		color=discord.Color.dark_blue(),
	)
	embed.add_field(name="Kategorie", value="🛡️ Moderace  •  ✉️ Zprávy  •  ⚙️ Automatizace & logy  •  📂 Služební složka", inline=False)
	embed.set_footer(text="LSPD Bot • Los Santos Police Department")
	return embed


@bot.tree.command(name="příkazy", description="Zobrazí interaktivní přehled všech funkcí a příkazů bota")
async def prikazy(interaction: discord.Interaction) -> None:
	await interaction.response.send_message(
		embed=_uvodni_embed(),
		view=PrikazyView(),
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
		"📥 Připojení člena",
		f"{member.mention} se připojil na server.",
		discord.Color.green(),
	)


@bot.event
async def on_member_remove(member: discord.Member) -> None:
	await bot.log_event(
		"📤 Odchod člena",
		f"{member} odešel ze serveru.",
		discord.Color.dark_grey(),
	)


# ---------------------------------------------------------------------------
# SYSTÉM SLUŽOBNÉ SLOŽKY
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ServiceRecord:
	user_id: int
	name: str
	badge: str
	is_on_duty: bool = False
	duty_start: datetime | None = None
	total_minutes: int = 0
	last_service_end: datetime | None = None
	last_service_minutes: int = 0


duty_records: dict[int, ServiceRecord] = {}
SERVICE_RECORDS_PATH = Path(__file__).with_name("duty_records.json")


def _serialize_datetime(value: datetime | None) -> str | None:
	if value is None:
		return None
	return value.astimezone(timezone.utc).isoformat()


def _deserialize_datetime(value: object) -> datetime | None:
	if not isinstance(value, str) or not value:
		return None
	try:
		parsed = datetime.fromisoformat(value)
	except ValueError:
		return None
	if parsed.tzinfo is None:
		return parsed.replace(tzinfo=timezone.utc)
	return parsed.astimezone(timezone.utc)


def _record_to_dict(record: ServiceRecord) -> dict[str, object]:
	return {
		"user_id": record.user_id,
		"name": record.name,
		"badge": record.badge,
		"is_on_duty": record.is_on_duty,
		"duty_start": _serialize_datetime(record.duty_start),
		"total_minutes": record.total_minutes,
		"last_service_end": _serialize_datetime(record.last_service_end),
		"last_service_minutes": record.last_service_minutes,
	}


def _record_from_dict(data: object) -> ServiceRecord | None:
	if not isinstance(data, dict):
		return None
	user_id = data.get("user_id")
	name = data.get("name")
	badge = data.get("badge")
	if not isinstance(user_id, int) or not isinstance(name, str) or not isinstance(badge, str):
		return None

	return ServiceRecord(
		user_id=user_id,
		name=name,
		badge=badge,
		is_on_duty=bool(data.get("is_on_duty", False)),
		duty_start=_deserialize_datetime(data.get("duty_start")),
		total_minutes=_coerce_int(data.get("total_minutes", 0)),
		last_service_end=_deserialize_datetime(data.get("last_service_end")),
		last_service_minutes=_coerce_int(data.get("last_service_minutes", 0)),
	)


def load_duty_records() -> None:
	if not SERVICE_RECORDS_PATH.exists():
		return

	try:
		payload = json.loads(SERVICE_RECORDS_PATH.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError) as exc:
		logger.warning("Nepodařilo se načíst duty data z %s: %s", SERVICE_RECORDS_PATH, exc)
		return

	if not isinstance(payload, list):
		logger.warning("Duty data v %s mají neplatný formát.", SERVICE_RECORDS_PATH)
		return

	duty_records.clear()
	for item in payload:
		record = _record_from_dict(item)
		if record is None:
			continue
		if record.duty_start is None:
			record.is_on_duty = False
		duty_records[record.user_id] = record

	logger.info("Načteno %s služebních složek z %s", len(duty_records), SERVICE_RECORDS_PATH)


def save_duty_records() -> None:
	serialized = [_record_to_dict(record) for record in duty_records.values()]
	tmp_path = SERVICE_RECORDS_PATH.with_suffix(".json.tmp")
	try:
		tmp_path.write_text(json.dumps(serialized, ensure_ascii=False, indent=2), encoding="utf-8")
		tmp_path.replace(SERVICE_RECORDS_PATH)
	except OSError as exc:
		logger.warning("Nepodařilo se uložit duty data do %s: %s", SERVICE_RECORDS_PATH, exc)


def _fmt_duration(minutes: int) -> str:
	h = minutes // 60
	m = minutes % 60
	return f"{h}h {m}m"


def _fmt_duration_compact(minutes: int | None) -> str:
	if minutes is None:
		return "   --"
	hours = minutes // 60
	remaining_minutes = minutes % 60
	return f"{hours:>4}:{remaining_minutes:02}"


def _truncate_table_value(value: str, limit: int) -> str:
	if len(value) <= limit:
		return value
	if limit <= 3:
		return value[:limit]
	return f"{value[:limit - 3]}..."


def _current_session_minutes(record: ServiceRecord) -> int | None:
	if not record.is_on_duty or record.duty_start is None:
		return None
	now = datetime.now(timezone.utc)
	return int((now - record.duty_start).total_seconds() / 60)


def _build_duty_table(records: list[ServiceRecord], max_rows: int = 20) -> tuple[str, int]:
	sorted_records = sorted(
		records,
		key=lambda record: (
			not record.is_on_duty,
			record.name.casefold(),
			record.badge.casefold(),
		),
	)
	visible_records = sorted_records[:max_rows]
	lines = [
		"#  ODZNAK JMENO              STAV   AKTIVNI  CELKEM",
		"-- ------ ------------------ ------ -------- --------",
	]

	for index, record in enumerate(visible_records, start=1):
		status = "ONLINE" if record.is_on_duty else "MIMO"
		current_minutes = _current_session_minutes(record)
		lines.append(
			f"{index:>2} "
			f"{_truncate_table_value(record.badge, 6):<6} "
			f"{_truncate_table_value(record.name, 18):<18} "
			f"{status:<6} "
			f"{_fmt_duration_compact(current_minutes):>8} "
			f"{_fmt_duration_compact(record.total_minutes):>8}"
		)

	table = "```text\n" + "\n".join(lines) + "\n```"
	remaining = max(0, len(sorted_records) - len(visible_records))
	return table, remaining


def _build_service_embed(record: ServiceRecord) -> discord.Embed:
	session_minutes = _current_session_minutes(record)
	if session_minutes is not None and record.duty_start:
		session_str = _fmt_duration(session_minutes)
		start_ts = int(record.duty_start.timestamp())
		doba_value = f"od <t:{start_ts}:R> ({session_str})"
	else:
		doba_value = "—"

	last_service_value = (
		f"<t:{int(record.last_service_end.timestamp())}:f> ({_fmt_duration(record.last_service_minutes)})"
		if record.last_service_end
		else "—"
	)

	status_emoji = "🟢" if record.is_on_duty else "🔴"
	status_text = "VE SLUŽBĚ" if record.is_on_duty else "MIMO SLUŽBU"
	color = discord.Color.green() if record.is_on_duty else discord.Color.dark_grey()

	embed = discord.Embed(title="📂 Služební složka", color=color)
	embed.description = f"Příslušník: <@{record.user_id}> **[{record.badge}]**"
	embed.add_field(name=f"{status_emoji} Stav", value=status_text, inline=True)
	embed.add_field(name="⏱️ Doba ve službě", value=doba_value, inline=True)
	embed.add_field(name="📊 Celkem hodin", value=_fmt_duration(record.total_minutes), inline=True)
	embed.add_field(name="🕐 Poslední služba", value=last_service_value, inline=False)
	embed.set_footer(text=f"Jméno: {record.name} • Aktualizuje se při interakci")
	return embed


class ServiceView(discord.ui.View):
	def __init__(self, user_id: int) -> None:
		super().__init__(timeout=None)
		self.user_id = user_id
		self._refresh_buttons()

	def _refresh_buttons(self) -> None:
		self.clear_items()
		record = duty_records.get(self.user_id)
		if record and record.is_on_duty:
			btn = discord.ui.Button(
				label="Ukončit službu",
				style=discord.ButtonStyle.danger,
				emoji="🔴",
			)
			btn.callback = self._off_duty
		else:
			btn = discord.ui.Button(
				label="Nastoupit do služby",
				style=discord.ButtonStyle.success,
				emoji="🟢",
			)
			btn.callback = self._on_duty
		self.add_item(btn)

	async def _check_owner(self, interaction: discord.Interaction) -> bool:
		if interaction.user.id == self.user_id:
			return True
		if isinstance(interaction.user, discord.Member) and _has_bot_access(interaction.user, "mod"):
			return True
		await interaction.response.send_message("Tato složka není tvoje.", ephemeral=True)
		return False

	async def _on_duty(self, interaction: discord.Interaction) -> None:
		if not await self._check_owner(interaction):
			return
		record = duty_records.get(self.user_id)
		if record:
			record.is_on_duty = True
			record.duty_start = datetime.now(timezone.utc)
			save_duty_records()
		self._refresh_buttons()
		await interaction.response.edit_message(embed=_build_service_embed(record), view=self)
		if record:
			await bot.log_event(
				"🟢 Nástup do služby",
				f"{interaction.user.mention} nastoupil do služby. (složka: <@{self.user_id}> [{record.badge}])",
				discord.Color.green(),
			)

	async def _off_duty(self, interaction: discord.Interaction) -> None:
		if not await self._check_owner(interaction):
			return
		record = duty_records.get(self.user_id)
		session_min = 0
		if record and record.duty_start:
			now = datetime.now(timezone.utc)
			session_min = int((now - record.duty_start).total_seconds() / 60)
			record.total_minutes += session_min
			record.last_service_end = now
			record.last_service_minutes = session_min
			record.is_on_duty = False
			record.duty_start = None
			save_duty_records()
		self._refresh_buttons()
		await interaction.response.edit_message(embed=_build_service_embed(record), view=self)
		if record:
			await bot.log_event(
				"🔴 Ukončení služby",
				f"{interaction.user.mention} ukončil službu. Délka: **{_fmt_duration(session_min if record else 0)}** (složka: <@{self.user_id}> [{record.badge}])",
				discord.Color.red(),
			)


class RegisterModal(discord.ui.Modal, title="Registrace složky"):
	jmeno: discord.ui.TextInput = discord.ui.TextInput(
		label="Celé jméno",
		placeholder="John Doe",
		min_length=2,
		max_length=50,
	)
	odznak: discord.ui.TextInput = discord.ui.TextInput(
		label="Číslo odznaku",
		placeholder="5056",
		min_length=1,
		max_length=10,
	)

	def __init__(self, target_user: discord.Member, original_message: discord.Message) -> None:
		super().__init__()
		self.target_user = target_user
		self.original_message = original_message

	async def on_submit(self, interaction: discord.Interaction) -> None:
		await interaction.response.defer()
		record = ServiceRecord(
			user_id=self.target_user.id,
			name=self.jmeno.value,
			badge=self.odznak.value,
		)
		duty_records[self.target_user.id] = record
		save_duty_records()
		view = ServiceView(self.target_user.id)
		await self.original_message.edit(embed=_build_service_embed(record), view=view)
		await bot.log_event(
			"📂 Nová složka",
			f"<@{self.target_user.id}> si vytvořil složku. Jméno: **{self.jmeno.value}**, odznak: **{self.odznak.value}**.",
			discord.Color.blurple(),
		)


class RegisterView(discord.ui.View):
	def __init__(self, target_user: discord.Member) -> None:
		super().__init__(timeout=300)
		self.target_user = target_user

	@discord.ui.button(label="📋 Zaregistrovat složku", style=discord.ButtonStyle.primary)
	async def register(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
		is_owner = interaction.user.id == self.target_user.id
		is_admin = isinstance(interaction.user, discord.Member) and _has_bot_access(interaction.user, "mod")
		if not (is_owner or is_admin):
			await interaction.response.send_message(
				"Tuto složku může zaregistrovat pouze její majitel nebo admin.",
				ephemeral=True,
			)
			return
		if interaction.message is None:
			await interaction.response.send_message("Nepodařilo se načíst zprávu.", ephemeral=True)
			return
		await interaction.response.send_modal(RegisterModal(self.target_user, interaction.message))

	async def on_timeout(self) -> None:
		for item in self.children:
			if isinstance(item, discord.ui.Button):
				item.disabled = True


@bot.tree.command(name="sluzba", description="Otevře tvoji služební složku (nebo složku jiného člena – pouze admin)")
@app_commands.describe(uzivatel="Člen, jehož složku chceš zobrazit (pouze mod/admin)")
async def sluzba(
	interaction: discord.Interaction,
	uzivatel: discord.Member | None = None,
) -> None:
	if uzivatel is not None:
		if not (isinstance(interaction.user, discord.Member) and _has_bot_access(interaction.user, "mod")):
			await interaction.response.send_message(
				"Nemáš oprávnění zobrazit složku jiného člena.", ephemeral=True
			)
			return
		target = uzivatel
	else:
		if not isinstance(interaction.user, discord.Member):
			await interaction.response.send_message("Příkaz lze použít jen na serveru.", ephemeral=True)
			return
		target = interaction.user

	record = duty_records.get(target.id)
	if record is None:
		embed = discord.Embed(
			title="📂 Služební složka",
			description=(
				f"Složka pro {target.mention} ještě nebyla vytvořena.\n"
				"Klikni na tlačítko níže a vyplň registrační formulář."
			),
			color=discord.Color.greyple(),
		)
		view = RegisterView(target)
	else:
		view = ServiceView(target.id)
		embed = _build_service_embed(record)

	channel = interaction.channel
	if isinstance(channel, discord.TextChannel):
		await interaction.response.send_message("Složka byla odeslána do kanálu.", ephemeral=True)
		await channel.send(embed=embed, view=view)
		return

	await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name="kontrola_duty", description="Zobrazí přehled duty v tabulce")
async def kontrola_duty(interaction: discord.Interaction) -> None:
	if not (isinstance(interaction.user, discord.Member) and _has_bot_access(interaction.user, "mod")):
		await interaction.response.send_message("Nemáš oprávnění zobrazit přehled duty.", ephemeral=True)
		return

	if not duty_records:
		await interaction.response.send_message("Zatím neexistuje žádná registrovaná služební složka.", ephemeral=True)
		return

	records = list(duty_records.values())
	on_duty_count = sum(1 for record in records if record.is_on_duty)
	off_duty_count = len(records) - on_duty_count
	table, remaining = _build_duty_table(records)

	embed = discord.Embed(
		title="📋 Přehled duty",
		description=table,
		color=discord.Color.blurple(),
	)
	embed.add_field(name="🟢 Ve službě", value=str(on_duty_count), inline=True)
	embed.add_field(name="🔴 Mimo službu", value=str(off_duty_count), inline=True)
	embed.add_field(name="👥 Celkem složek", value=str(len(records)), inline=True)
	if remaining:
		embed.add_field(
			name="Poznámka",
			value=f"Tabulka zobrazuje prvních 20 záznamů. Dalších skrytých: **{remaining}**.",
			inline=False,
		)
	embed.set_footer(text="Aktuální stav služby a celkový odsloužený čas")
	await interaction.response.send_message(embed=embed, ephemeral=True)


def main() -> None:
	token = os.getenv("DISCORD_TOKEN")
	if not token:
		raise RuntimeError("Chybí DISCORD_TOKEN v proměnných prostředí.")
	bot.run(token, log_handler=None)


if __name__ == "__main__":
	main()
