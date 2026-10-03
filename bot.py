"""Telegram multiplayer PvP bot (aiogram v3).  Run:  python bot.py"""
import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import database
from game import GAMES, MAX_PLAYERS, MIN_PLAYERS, MODES, GameSession, lobby_markup, manager, mention

log = logging.getLogger("pvp.bot")
router = Router()
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})


# ============================================================ FSM + callback data
class PvP(StatesGroup):
    choosing_game = State()
    choosing_mode = State()
    choosing_rolls = State()
    choosing_wins = State()
    inviting = State()
    in_match = State()


class SetupCB(CallbackData, prefix="su"):
    step: str
    value: str = ""


class InviteCB(CallbackData, prefix="inv"):
    action: str  # yes | no
    target: str  # user id (digits) or lowercase @username


# ============================================================ keyboards
def _cancel(b: InlineKeyboardBuilder) -> None:
    b.button(text="✖️ Cancel", callback_data=SetupCB(step="cancel"))


def game_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, (emoji, name) in GAMES.items():
        b.button(text=f"{emoji} {name}", callback_data=SetupCB(step="game", value=key))
    _cancel(b)
    b.adjust(2, 2, 1, 1)
    return b.as_markup()


def mode_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key, (emoji, name, desc) in MODES.items():
        b.button(text=f"{emoji} {name} — {desc}", callback_data=SetupCB(step="mode", value=key))
    _cancel(b)
    b.adjust(1)
    return b.as_markup()


def number_kb(step: str, upto: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for n in range(1, upto + 1):
        b.button(text=str(n), callback_data=SetupCB(step=step, value=str(n)))
    _cancel(b)
    b.adjust(*([5] * (upto // 5)), 1)
    return b.as_markup()


def summary(d: dict) -> str:
    parts = []
    if "game" in d:
        parts.append("{} {}".format(*GAMES[d["game"]]))
    if "mode" in d:
        parts.append(f"{MODES[d['mode']][0]} {MODES[d['mode']][1]}")
    if "rolls" in d:
        parts.append(f"{d['rolls']} roll(s)")
    return "⚙️ " + " · ".join(parts) + "\n\n" if parts else ""


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return False
    return m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


# ============================================================ basic commands
HELP = (
    "🎮 <b>PvP Games Bot</b>\n\n"
    "Add me to a group and use:\n"
    "/pvp — set up & start a match (2–7 players)\n"
    "/invite — invite players (reply to them or /invite @user)\n"
    "/stop — cancel the running match (creator/admin)\n"
    "/stats — your stats (reply to someone for theirs)\n"
    "/top — leaderboard\n"
    "/history — your last matches\n\n"
    "🎲 Dice · 🏀 Basketball · ⚽ Football · 🎳 Bowling · 🎯 Darts"
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


# ============================================================ /pvp wizard (FSM)
@router.message(Command("pvp"), GROUP)
async def cmd_pvp(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or user.is_bot:  # anonymous admin posts can't be tracked
        return await message.reply("⚠️ Please disable anonymous-admin mode to start a match.")
    chat_id = message.chat.id
    if manager.get(chat_id):
        return await message.reply("⚠️ A match/lobby is already active here. Use /stop to cancel it.")
    if not manager.reserve_setup(chat_id, user.id):
        return await message.reply("⚠️ Someone is already setting up a match here. Try again in a moment.")

    await state.clear()
    await state.set_state(PvP.choosing_game)
    await state.update_data(
        creator_id=user.id, creator_name=user.full_name, title=message.chat.title or "",
        thread_id=message.message_thread_id if message.is_topic_message else None)
    await message.answer(f"{mention(user.id, user.full_name)} is creating a match!\n\n"
                         "<b>Step 1/4 — Choose the game:</b>", reply_markup=game_kb())


@router.callback_query(SetupCB.filter(F.step == "cancel"),
                       StateFilter(PvP.choosing_game, PvP.choosing_mode, PvP.choosing_rolls, PvP.choosing_wins))
async def setup_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    manager.release_setup(cb.message.chat.id)
    await cb.message.edit_text("❌ Match setup cancelled.")
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "game"), PvP.choosing_game)
async def setup_game(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    if callback_data.value not in GAMES:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)
    await state.update_data(game=callback_data.value)
    await state.set_state(PvP.choosing_mode)
    await cb.message.edit_text(summary(await state.get_data()) + "<b>Step 2/4 — Choose the mode:</b>",
                               reply_markup=mode_kb())
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "mode"), PvP.choosing_mode)
async def setup_mode(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    if callback_data.value not in MODES:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)
    await state.update_data(mode=callback_data.value)
    await state.set_state(PvP.choosing_rolls)
    await cb.message.edit_text(summary(await state.get_data()) + "<b>Step 3/4 — Rolls per player (1–10):</b>",
                               reply_markup=number_kb("rolls", 10))
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "rolls"), PvP.choosing_rolls)
async def setup_rolls(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    if not callback_data.value.isdigit() or not 1 <= int(callback_data.value) <= 10:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)
    await state.update_data(rolls=int(callback_data.value))
    await state.set_state(PvP.choosing_wins)
    await cb.message.edit_text(summary(await state.get_data()) + "<b>Step 4/4 — Series wins needed (1–20):</b>",
                               reply_markup=number_kb("wins", 20))
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "wins"), PvP.choosing_wins)
async def setup_wins(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    if not callback_data.value.isdigit() or not 1 <= int(callback_data.value) <= 20:
        return await cb.answer()
    d = await state.get_data()
    chat_id = cb.message.chat.id
    if manager.get(chat_id):
        return await cb.answer("A match already exists in this group.", show_alert=True)

    session = GameSession(
        bot=cb.bot, chat_id=chat_id, thread_id=d.get("thread_id"), chat_title=d.get("title", ""),
        creator_id=d["creator_id"], creator_name=d["creator_name"], game=d["game"], mode=d["mode"],
        rolls=d["rolls"], target_wins=int(callback_data.value))
    session.lobby_msg_id = cb.message.message_id

    async def _on_close() -> None:
        await state.clear()

    session.on_close = _on_close
    manager.add(session)
    await state.set_state(PvP.inviting)
    await cb.message.edit_text(session.lobby_text(), reply_markup=lobby_markup())
    session.start_lobby_timer()
    await cb.answer("Lobby created!")


# Anyone else touching a wizard menu (or a stale one):
@router.callback_query(F.data.startswith("su:"))
async def setup_not_yours(cb: CallbackQuery) -> None:
    await cb.answer("This menu isn't yours (or it expired) ❌", show_alert=True)


# ============================================================ invites
@router.message(Command("invite"), GROUP)
async def cmd_invite(message: Message) -> None:
    s = manager.get(message.chat.id)
    if not s or s.status != "lobby":
        return await message.reply("No open lobby here. Start one with /pvp.")
    if not message.from_user or message.from_user.id != s.creator_id:
        return await message.reply("Only the match creator can invite players ❌")

    targets: dict[str, str] = {}  # target -> HTML label
    rep = message.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot:
        targets[str(rep.from_user.id)] = mention(rep.from_user.id, rep.from_user.full_name)

    for ent in message.entities or []:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot:
            targets[str(ent.user.id)] = mention(ent.user.id, ent.user.full_name)
        elif ent.type == MessageEntityType.MENTION:
            uname = ent.extract_from(message.text)[1:]
            targets[uname.lower()] = "@" + html.escape(uname)

    if not targets:
        return await message.reply("Reply to a player's message with /invite, or use <code>/invite @username</code>.")

    problems: list[str] = []
    for target, label in targets.items():
        uid = int(target) if target.isdigit() else None
        err = s.can_invite(target, uid)
        if err:
            problems.append(f"{label}: {err}")
            continue
        s.invited[target] = label
        b = InlineKeyboardBuilder()
        b.button(text="✅ Join", callback_data=InviteCB(action="yes", target=target))
        b.button(text="❌ Decline", callback_data=InviteCB(action="no", target=target))
        sent = await message.answer(
            f"👋 {label}, {mention(s.creator_id, s.creator_name)} invited you to a "
            f"{s.emoji} <b>{s.game_name}</b> PvP match!\n⚙️ {s.config_line()}",
            reply_markup=b.as_markup())
        s.invite_msgs[target] = sent.message_id

    await s.refresh_lobby()
    if problems:
        await message.reply("⚠️ Couldn't invite:\n• " + "\n• ".join(problems))


@router.callback_query(InviteCB.filter())
async def on_invite(cb: CallbackQuery, callback_data: InviteCB) -> None:
    me, target = cb.from_user, callback_data.target
    # Privacy: valid only for the targeted user (matched by id OR by @username)
    if not (target == str(me.id) or (me.username and target == me.username.lower())):
        return await cb.answer("This invite is not for you! ❌", show_alert=True)

    s = manager.get(cb.message.chat.id)
    if not s or s.status != "lobby" or (target not in s.invited and me.id not in s.players):
        await cb.answer("This invite has expired.", show_alert=True)
        try:
            await cb.message.edit_text("⌛ This invite has expired.")
        except TelegramBadRequest:
            pass
        return

    if callback_data.action == "no":
        s.invited.pop(target, None)
        s.invite_msgs.pop(target, None)
        await cb.message.edit_text(f"❌ {mention(me.id, me.full_name)} declined the invite.")
        await s.refresh_lobby()
        return await cb.answer("Declined")

    err = s.accept(me.id, me.full_name, target)
    if err:
        return await cb.answer(err, show_alert=True)
    await cb.message.edit_text(f"✅ {mention(me.id, me.full_name)} joined the match! ({len(s.players)}/{MAX_PLAYERS})")
    await s.refresh_lobby()
    await cb.answer("You're in! 🎉")


# ============================================================ lobby buttons
@router.callback_query(F.data.in_({"lobby:start", "lobby:cancel"}))
async def on_lobby(cb: CallbackQuery, state: FSMContext) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "lobby":
        return await cb.answer("This lobby is no longer active.", show_alert=True)
    if cb.from_user.id != s.creator_id:
        return await cb.answer("Only the match creator can do this! ❌", show_alert=True)

    if cb.data == "lobby:cancel":
        await s.edit(s.lobby_msg_id, "❌ Lobby cancelled by the creator.")
        await manager.close(s)
        return await cb.answer("Cancelled")

    if len(s.players) < MIN_PLAYERS:
        return await cb.answer(f"Need at least {MIN_PLAYERS} players to start!", show_alert=True)
    await state.set_state(PvP.in_match)
    s.start()
    await cb.answer("Match starting! 🚀")


# ============================================================ in-match: throw button
@router.callback_query(F.data == "throw")
async def on_throw(cb: CallbackQuery) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "running":
        return await cb.answer("This match is over.", show_alert=True)
    if s.current_turn is None or cb.message.message_id != s.turn_msg_id:
        return await cb.answer("That turn is already finished.")
    if cb.from_user.id != s.current_turn:
        return await cb.answer("It's not your turn! ⏳", show_alert=True)
    s.throw_event.set()
    await cb.answer("Throwing…")


@router.message(Command("stop", "cancel"), GROUP)
async def cmd_stop(message: Message, bot: Bot) -> None:
    uid, chat_id = (message.from_user.id if message.from_user else 0), message.chat.id
    s = manager.get(chat_id)
    if not s:
        owner = manager.setup_owner(chat_id)
        if owner and (uid == owner or await is_admin(bot, chat_id, uid)):
            manager.release_setup(chat_id)
            return await message.reply("🛑 Setup cancelled.")
        return await message.reply("Nothing to stop.")
    if uid != s.creator_id and not await is_admin(bot, chat_id, uid):
        return await message.reply("Only the match creator or a group admin can stop the match ❌")
    await manager.close(s)
    await message.reply("🛑 Match cancelled (no stats recorded for unfinished matches).")


# ============================================================ stats
@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    rep = message.reply_to_message
    target = rep.from_user if rep and rep.from_user and not rep.from_user.is_bot else message.from_user
    u = await database.get_user(target.id)
    if not u or u.matches_played == 0:
        return await message.reply(f"{html.escape(target.full_name)} hasn't finished any matches yet.")
    rate = u.wins / u.matches_played * 100
    await message.reply(
        f"📊 <b>Stats — {html.escape(u.full_name)}</b>\n"
        f"🎮 Matches: {u.matches_played}\n"
        f"🏆 Wins: {u.wins}   💀 Losses: {u.losses}\n"
        f"📈 Win rate: {rate:.0f}%\n"
        f"🥇 Series rounds won: {u.rounds_won}\n"
        f"🎯 Total points rolled: {u.total_points}")


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    rows = await database.top_players(10)
    if not rows:
        return await message.reply("No finished matches yet — be the first! /pvp")
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏅 <b>Leaderboard</b>"]
    for i, u in enumerate(rows):
        lines.append(f"{medals[i] if i < 3 else f'{i + 1}.'} {html.escape(u.full_name)} — "
                     f"{u.wins}W / {u.losses}L ({u.wins / u.matches_played * 100:.0f}%)")
    await message.reply("\n".join(lines))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    rows = await database.recent_matches(message.from_user.id, 5)
    if not rows:
        return await message.reply("No match history yet.")
    lines = ["🕘 <b>Your last matches</b>"]
    for r in rows:
        m = r.Match
        emoji = GAMES.get(m.game, ("🎮",))[0]
        result = "✅ Won" if r.is_winner else f"❌ Lost (winner: {html.escape(m.winner_name)})"
        lines.append(f"#{m.id} {emoji} {MODES.get(m.mode, ('', m.mode))[1]} · {m.player_count} players · "
                     f"you {r.series_wins}/{m.target_wins} · {result} · {m.finished_at:%d %b %Y}")
    await message.reply("\n".join(lines))


# ============================================================ main
async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("BOT_TOKEN is not set (see .env.example)")

    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI is not set (see .env.example)")
    await database.init_db(uri, os.getenv("MONGODB_DB", "pvp_bot"))

    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="pvp", description="Start a PvP match"),
        BotCommand(command="invite", description="Invite a player to the lobby"),
        BotCommand(command="stop", description="Cancel the current match"),
        BotCommand(command="stats", description="Your stats"),
        BotCommand(command="top", description="Leaderboard"),
        BotCommand(command="history", description="Your recent matches"),
        BotCommand(command="help", description="Help"),
    ])
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await manager.shutdown(bot)
        await database.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
