import os
import re
import sqlite3
from datetime import datetime
import asyncio
import logging
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "karateli_roles.sqlite3")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

approval_locks = {}

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            guild_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY(guild_id,key)
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            real_name TEXT NOT NULL, member_status TEXT NOT NULL,
            reason TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
            ticket_id INTEGER, reviewer_id INTEGER, reviewer_name TEXT,
            created_at TEXT NOT NULL, decided_at TEXT
        );
        """)

def get_setting(guild_id, key, default=""):
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE guild_id=? AND key=?",
                          (guild_id, key)).fetchone()
    return row["value"] if row else default

def set_setting(guild_id, key, value):
    with db() as con:
        con.execute("""INSERT INTO settings(guild_id,key,value) VALUES(?,?,?)
        ON CONFLICT(guild_id,key) DO UPDATE SET value=excluded.value""",
                    (guild_id, key, str(value)))

def now():
    return datetime.now().astimezone().strftime("%d.%m.%Y %H:%M")

def is_reviewer(member: discord.Member):
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    role_id = get_setting(member.guild.id, "review_role_id")
    role = member.guild.get_role(int(role_id)) if role_id.isdigit() else None
    return role is not None and role in member.roles

def get_role_ids(guild_id):
    raw = get_setting(guild_id, "target_role_ids") or get_setting(guild_id, "target_role_id")
    if not raw:
        return []
    values = [v.strip() for v in re.split(r"[,;\s]+", raw) if v.strip()]
    return list(dict.fromkeys(int(v) for v in values if v.isdigit()))

def make_embed(title, description="", color=0xA30D20):
    e = discord.Embed(title=title, description=description, color=color)
    e.set_footer(text="☠ КАРАТЕЛИ • ЗАПРОС РОЛИ")
    return e

def safe_channel_name(value):
    value = re.sub(r"[^a-zA-Zа-яА-ЯёЁ0-9_-]", "-", value.strip().lower())
    return (value[:45] or "request").strip("-")

class RoleRequestModal(discord.ui.Modal, title="Заявление на получение роли"):
    real_name = discord.ui.TextInput(label="Имя и фамилия", placeholder="Например: Иван Иванов", max_length=80)
    member_status = discord.ui.TextInput(
        label="Кто вы? Напишите: новичок / участник / возвращаюсь",
        placeholder="Например: Новичок", max_length=60
    )
    reason = discord.ui.TextInput(
        label="Дополнительная информация", style=discord.TextStyle.paragraph,
        placeholder="Расскажите немного о себе или укажите нужные сведения",
        required=False, max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        category_id = get_setting(guild.id, "ticket_category_id")
        category = guild.get_channel(int(category_id)) if category_id.isdigit() else None
        review_role_id = get_setting(guild.id, "review_role_id")
        review_role = guild.get_role(int(review_role_id)) if review_role_id.isdigit() else None

        with db() as con:
            cur = con.execute("""INSERT INTO requests
                (guild_id,user_id,real_name,member_status,reason,created_at)
                VALUES(?,?,?,?,?,?)""",
                (guild.id, interaction.user.id, self.real_name.value.strip(),
                 self.member_status.value.strip(), self.reason.value.strip(), now()))
            request_id = cur.lastrowid

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                          read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                  read_message_history=True, manage_channels=True)
        }
        if review_role:
            overwrites[review_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        try:
            ticket = await guild.create_text_channel(
                name=f"запрос-роли-{request_id}-{safe_channel_name(interaction.user.display_name)}",
                category=category if isinstance(category, discord.CategoryChannel) else None,
                overwrites=overwrites, reason=f"Role request #{request_id}"
            )
        except discord.Forbidden:
            return await interaction.followup.send(
                "Не удалось создать приватный канал. Проверьте право бота «Управление каналами».",
                ephemeral=True
            )

        with db() as con:
            con.execute("UPDATE requests SET ticket_id=? WHERE id=?", (ticket.id, request_id))

        e = make_embed("🎭 ЗАПРОС РОЛИ", color=0xD49B26)
        e.add_field(name="Заявка", value=f"№{request_id}", inline=True)
        e.add_field(name="Заявитель", value=f"{interaction.user.mention}\n{self.real_name.value}", inline=True)
        e.add_field(name="Кто заявитель", value=self.member_status.value, inline=True)
        e.add_field(name="Дополнительная информация", value=self.reason.value or "Не указана", inline=False)
        e.add_field(name="Подано", value=now(), inline=True)
        await ticket.send(content=review_role.mention if review_role else None,
                          embed=e, view=ReviewView(request_id))
        await interaction.followup.send(
            f"✅ Заявка №{request_id} создана. Приватный канал: {ticket.mention}",
            ephemeral=True
        )

class RolePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Запросить роль", emoji="🎭",
                       style=discord.ButtonStyle.danger, custom_id="karateli:role:open")
    async def open_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoleRequestModal())

class ReviewView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id
        for child in self.children:
            child.custom_id = f"karateli:role:review:{request_id}:{child.label.lower()}"

    async def decide(self, interaction, decision):
        if not is_reviewer(interaction.user):
            return await interaction.response.send_message(
                "⛔ Рассматривать заявки может только старший состав.", ephemeral=True)

        # Discord requires a response within 3 seconds. Acknowledge first,
        # then make Discord API calls in the background of this interaction.
        await interaction.response.defer(ephemeral=True, thinking=True)
        lock = approval_locks.setdefault(self.request_id, asyncio.Lock())
        async with lock:
            with db() as con:
                req = con.execute(
                    "SELECT * FROM requests WHERE id=? AND guild_id=?",
                    (self.request_id, interaction.guild_id)).fetchone()
            if not req or req["state"] != "pending":
                return await interaction.followup.send(
                    "Эта заявка уже обработана или не найдена.", ephemeral=True)

            member = interaction.guild.get_member(req["user_id"])
            result_text = "Роли не изменялись."
            color = 0xD49B26
            state_label = "НА ДОРАБОТКЕ"

            if decision == "approved":
                if member is None:
                    try:
                        member = await interaction.guild.fetch_member(req["user_id"])
                    except discord.HTTPException:
                        return await interaction.followup.send(
                            "Участник отсутствует на сервере. Одобрение невозможно.", ephemeral=True)
                ids = get_role_ids(interaction.guild_id)
                if not ids:
                    return await interaction.followup.send(
                        "Не указаны роли для выдачи. Настройте их через `/role_setup`.", ephemeral=True)
                roles = [interaction.guild.get_role(rid) for rid in ids]
                if any(role is None for role in roles):
                    return await interaction.followup.send(
                        "Некоторые ID ролей не найдены на сервере. Проверьте настройки.", ephemeral=True)
                if any(not role.is_assignable() for role in roles):
                    return await interaction.followup.send(
                        "Бот не может выдать одну из ролей. Проверьте иерархию и права бота.", ephemeral=True)
                try:
                    # One Discord request rather than one request per role.
                    new_roles = list({r.id: r for r in [*member.roles, *roles]}.values())
                    await member.edit(
                        roles=new_roles, reason=f"Заявка на роль #{self.request_id}")
                except discord.HTTPException as exc:
                    return await interaction.followup.send(
                        f"Не удалось выдать роли: {exc}", ephemeral=True)

                tag = get_setting(interaction.guild_id, "nickname_tag", "Учеб Отдел").strip()
                new_nick = f"[{tag}] {req['real_name']}"[:32] if tag else req["real_name"][:32]
                nick_note = ""
                try:
                    await member.edit(nick=new_nick, reason=f"Заявка на роль #{self.request_id}")
                    nick_note = f"\n**Никнейм:** `{new_nick}`"
                except discord.HTTPException:
                    nick_note = "\n⚠️ Никнейм изменить не удалось (права или иерархия ролей)."
                result_text = "Выданы роли: " + ", ".join(r.mention for r in roles) + nick_note
                color = 0x239B56
                state_label = "ОДОБРЕНО"
            elif decision == "rejected":
                result_text = "Заявка отклонена. Роли не изменялись."
                color = 0xB42332
                state_label = "ОТКЛОНЕНО"

            with db() as con:
                con.execute(
                    """UPDATE requests SET state=?,reviewer_id=?,reviewer_name=?,decided_at=?
                       WHERE id=? AND state='pending'""",
                    (decision, interaction.user.id, interaction.user.display_name,
                     now(), self.request_id))

            e = make_embed(
                f"{'✅' if decision == 'approved' else '❌' if decision == 'rejected' else '📝'} "
                f"ЗАЯВКА №{self.request_id} — {state_label}", color=color)
            e.add_field(name="Участник",
                        value=f"<@{req['user_id']}> • {req['real_name']}", inline=False)
            e.add_field(name="Кто заявитель", value=req["member_status"], inline=True)
            e.add_field(name="Решение принял", value=interaction.user.display_name, inline=True)
            e.add_field(name="Результат", value=result_text[:1024], inline=False)
            e.add_field(name="Дата", value=now(), inline=True)

            try:
                await interaction.message.edit(embed=e, view=None)
            except discord.HTTPException:
                pass
            await interaction.followup.send(
                f"✅ Заявка №{self.request_id}: {state_label}.", ephemeral=True)

            audit_id = get_setting(interaction.guild_id, "audit_channel_id")
            audit_channel = interaction.guild.get_channel(int(audit_id)) if audit_id.isdigit() else None
            if audit_channel:
                try:
                    await audit_channel.send(embed=e)
                except discord.HTTPException:
                    pass

            if member:
                try:
                    await member.send(embed=make_embed(
                        f"{'✅' if decision == 'approved' else '❌' if decision == 'rejected' else '📝'} "
                        "РЕШЕНИЕ ПО ЗАПРОСУ РОЛИ",
                        f"**Заявка:** №{self.request_id}\n"
                        f"**Кто заявитель:** {req['member_status']}\n"
                        f"**Решение:** {state_label}\n"
                        f"**Рассмотрел:** {interaction.user.display_name}\n"
                        f"**Дата:** {now()}\n\n{result_text}",
                        color=color))
                except discord.HTTPException:
                    pass

    @discord.ui.button(label="Одобрить", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await self.decide(interaction, "approved")

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction, button):
        await self.decide(interaction, "rejected")

    @discord.ui.button(label="На доработку", emoji="📝", style=discord.ButtonStyle.secondary)
    async def revision(self, interaction, button):
        await self.decide(interaction, "revision")

class SetupModal(discord.ui.Modal, title="Настройки бота запросов ролей"):
    panel_channel = discord.ui.TextInput(label="ID канала панели запроса роли")
    ticket_category = discord.ui.TextInput(label="ID категории приватных заявок")
    reviewer_role = discord.ui.TextInput(label="ID роли старшего состава")
    target_role = discord.ui.TextInput(
        label="ID ролей для выдачи (через запятую)",
        placeholder="123456789012345678,234567890123456789",
        style=discord.TextStyle.paragraph, max_length=1000)
    nickname_tag = discord.ui.TextInput(label="Подпись в никнейме", placeholder="Например: Учеб Отдел")

    def __init__(self, guild_id):
        super().__init__()
        self.guild_id = guild_id
        for field, key in [
            (self.panel_channel, "panel_channel_id"),
            (self.ticket_category, "ticket_category_id"),
            (self.reviewer_role, "review_role_id"),
            (self.target_role, "target_role_ids"),
            (self.nickname_tag, "nickname_tag")
        ]:
            field.default = get_setting(guild_id, key) or (
                get_setting(guild_id, "target_role_id") if key == "target_role_ids" else "")

    async def on_submit(self, interaction):
        fields = [
            (self.panel_channel, "panel_channel_id"),
            (self.ticket_category, "ticket_category_id"),
            (self.reviewer_role, "review_role_id"),
            (self.target_role, "target_role_ids"),
            (self.nickname_tag, "nickname_tag")
        ]
        for field, key in fields:
            value = field.value.strip()
            if key == "target_role_ids":
                entries = [part.strip() for part in re.split(r"[,;\s]+", value) if part.strip()]
                if not entries or len(entries) > 20 or any(not part.isdigit() for part in entries):
                    return await interaction.response.send_message(
                        "Укажите от 1 до 20 числовых ID ролей через запятую.", ephemeral=True)
                value = ",".join(dict.fromkeys(entries))
            elif key != "nickname_tag" and value and not value.isdigit():
                return await interaction.response.send_message(
                    f"Поле «{field.label}» должно содержать числовой ID.", ephemeral=True)
            set_setting(interaction.guild_id, key, value)
        await interaction.response.send_message("✅ Настройки сохранены.", ephemeral=True)


class AuditModal(discord.ui.Modal, title="Настройка канала аудита"):
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logging.exception("AuditModal error: %s", error, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send("❌ Ошибка сохранения аудита. Проверьте логи.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Ошибка сохранения аудита. Проверьте логи.", ephemeral=True)
        except discord.HTTPException:
            pass

    audit_channel = discord.ui.TextInput(label="ID канала аудита", placeholder="Вставьте ID текстового канала")

    async def on_submit(self, interaction):
        value = self.audit_channel.value.strip()
        if not value.isdigit():
            return await interaction.response.send_message(
                "ID канала должен состоять только из цифр.", ephemeral=True)
        channel = interaction.guild.get_channel(int(value))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message(
                "Текстовый канал с таким ID не найден на этом сервере.", ephemeral=True)
        set_setting(interaction.guild_id, "audit_channel_id", value)
        await interaction.response.send_message(
            f"✅ Канал аудита установлен: {channel.mention}", ephemeral=True)

class SetupView(discord.ui.View):
    def __init__(self):
        # Ephemeral views stay interactive for up to ~15 minutes.
        super().__init__(timeout=840)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item):
        logging.exception("SetupView button error: %s", error, exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "❌ Ошибка при открытии настроек. Повторите `/role_setup` и посмотрите логи Bothost.",
                    ephemeral=True)
            else:
                await interaction.response.send_message(
                    "❌ Не удалось открыть настройки. Повторите `/role_setup` и проверьте логи Bothost.",
                    ephemeral=True)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Настроить бота", emoji="⚙️", style=discord.ButtonStyle.primary)
    async def setup(self, interaction, button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Нужно право «Управление сервером».", ephemeral=True)
        await interaction.response.send_modal(SetupModal(interaction.guild_id))

    @discord.ui.button(label="Настроить аудит", emoji="📜", style=discord.ButtonStyle.secondary)
    async def audit(self, interaction, button):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message("Нужно право «Управление сервером».", ephemeral=True)
        try:
            await interaction.response.send_modal(AuditModal())
        except discord.HTTPException:
            logging.exception("Failed to open AuditModal")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Не удалось открыть форму аудита. Повторите `/role_setup`.", ephemeral=True)

class KarateliRoleBot(commands.Bot):
    async def setup_hook(self):
        init_db()
        self.add_view(RolePanel())
        with db() as con:
            pending = con.execute("SELECT id FROM requests WHERE state='pending'").fetchall()
        for request in pending:
            self.add_view(ReviewView(request["id"]))
        await self.tree.sync()

bot = KarateliRoleBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Бот запросов ролей запущен: {bot.user}")

@bot.tree.command(name="role_setup", description="Настроить бота запросов ролей")
@app_commands.guild_only()
async def role_setup(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Нужно право «Управление сервером».", ephemeral=True)
    await interaction.response.send_message(
        embed=make_embed("☠ КАРАТЕЛИ | НАСТРОЙКА ЗАПРОСА РОЛИ",
                         "Настройте канал панели, категорию приватных заявок, проверяющих и несколько ролей для выдачи."),
        view=SetupView(), ephemeral=True)

@bot.tree.command(name="role_publish", description="Опубликовать отдельную панель запроса роли")
@app_commands.guild_only()
async def role_publish(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Нужно право «Управление сервером».", ephemeral=True)
    channel_id = get_setting(interaction.guild_id, "panel_channel_id")
    channel = interaction.guild.get_channel(int(channel_id)) if channel_id.isdigit() else None
    if channel is None:
        return await interaction.response.send_message("Сначала настройте ID канала через `/role_setup`.", ephemeral=True)
    await interaction.response.defer(ephemeral=True, thinking=True)
    await channel.send(
        embed=make_embed("🎭 КАРАТЕЛИ | ЗАПРОС РОЛИ",
                         "Нажмите кнопку и заполните заявление.\n\n"
                         "Укажите имя и фамилию, кто вы — новичок, действующий участник или возвращаетесь, "
                         "и при необходимости добавьте информацию. Решение принимает старший состав."),
        view=RolePanel())
    await interaction.followup.send(f"✅ Отдельная панель опубликована в {channel.mention}.", ephemeral=True)

if not TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN. Скопируйте .env.example в .env и вставьте токен.")
bot.run(TOKEN)
