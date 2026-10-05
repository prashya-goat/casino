"""Telegram multiplayer PvP bot (aiogram v3).  Run:  python bot.py"""
import asyncio
import html
import logging
import os
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, MessageEntityType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import database
from game import (GAMES, MAX_PLAYERS, MIN_PLAYERS, MODES, GameSession, InviteCB, lobby_markup,
                  manager, mention)

log = logging.getLogger("pvp.bot")
router = Router()
GROUP = F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP})
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").replace(" ", "").split(",") if x.isdigit()}


# ============================================================ FSM + callback data
class PvP(StatesGroup):
    choosing_game = State()
    choosing_mode = State()
    choosing_rolls = State()
    choosing_wins = State()
    inviting = State()
    in_match = State()


class SetupCB(CallbackData, prefix="su"):
    step: str       # game | mode | rolls | wins | back | cancel
    value: str = ""


STATE_OF = {"game": PvP.choosing_game, "mode": PvP.choosing_mode,
            "rolls": PvP.choosing_rolls, "wins": PvP.choosing_wins}
STEP_OF_STATE = {st.state: step for step, st in STATE_OF.items()}
SETUP_STATES = tuple(STATE_OF.values())
ORDER = ["game", "mode", "rolls", "wins"]
QUESTIONS = {
    "game": "Which game?",
    "mode": "Which mode?\nNormal — highest total wins\nCrazy — lowest total wins",
    "rolls": "How many rolls per round?",
    "wins": "First to how many wins?",
}


# ============================================================ wizard views (casino style vertical layout with emojis)
def setup_header(d: dict) -> str:
    lines = [f"⚔️ {mention(d['creator_id'], d['creator_name'])} is setting up a PvP match"]
    if "game" in d:
        lines.append("{} {}".format(*GAMES[d["game"]]))
    parts = []
    if "mode" in d:
        e, n, desc = MODES[d["mode"]]
        parts.append(f"{e} {n} — {desc}")
    if "rolls" in d:
        parts.append(f"{d['rolls']} roll(s)")
    if "wins" in d:
        parts.append(f"first to {d['wins']}")
    if parts:
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def step_view(step: str, d: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = setup_header(d) + f"\n\n<blockquote>{QUESTIONS[step]}</blockquote>"
    b = InlineKeyboardBuilder()
    
    # Game selection (Grid style as requested previously)
    if step == "game":      
        for key, (emoji, name) in GAMES.items():
            b.button(text=f"{emoji} {name}", callback_data=SetupCB(step="game", value=key))
        b.button(text="❌ Cancel", callback_data=SetupCB(step="cancel"))
        b.adjust(2, 2, 1, 1)
        
    # Mode selection (Vertical list)
    elif step == "mode":    
        b.button(text="🟢 Normal Mode", callback_data=SetupCB(step="mode", value="normal"))
        b.button(text="🔴 Crazy Mode", callback_data=SetupCB(step="mode", value="crazy"))
        b.button(text="« Back", callback_data=SetupCB(step="back"))
        b.adjust(1, 1, 1)
        
    # Rolls selection (Vertical stack with game emojis like 🏀 1 Roll)
    elif step == "rolls":   
        emoji = GAMES.get(d.get("game", "dice"), ("🎲",))[0]
        for n in range(1, 11):
            label = f"{emoji} {n} Roll{'s' if n > 1 else ''}"
            b.button(text=label, callback_data=SetupCB(step="rolls", value=str(n)))
        b.button(text="« Back", callback_data=SetupCB(step="back"))
        b.adjust(*(1 for _ in range(12))) # Har row mein ek button (vertical stack)
        
    # Wins selection (Vertical stack with clean numbering)
    elif step == "wins":    
        for n in range(1, 21):
            label = f"🏆 {n} Win{'s' if n > 1 else ''}"
            b.button(text=label, callback_data=SetupCB(step="wins", value=str(n)))
        b.button(text="« Back", callback_data=SetupCB(step="back"))
        b.adjust(*(1 for _ in range(22))) # Har row mein ek button (vertical stack)
        
    return text, b.as_markup()


def validate(step: str, value: str) -> Any:
    if step == "game":
        return value if value in GAMES else None
    if step == "mode":
        return value if value in MODES else None
    hi = 10 if step == "rolls" else 20
    return int(value) if value.isdigit() and 1 <= int(value) <= hi else None


# ============================================================ RAM-only username memory
class SeenUsers(BaseMiddleware):
    MAX = 5000

    def __init__(self) -> None:
        self.data: OrderedDict = OrderedDict()

    def lookup(self, chat_id: int, username: str) -> Optional[tuple]:
        return self.data.get((chat_id, username.lower()))

    async def __call__(self, handler: Callable[[TelegramObject, dict], Awaitable[Any]],
                       event: TelegramObject, data: dict) -> Any:
        user, chat = data.get("event_from_user"), data.get("event_chat")
        if user and chat and user.username and not user.is_bot:
            key = (chat.id, user.username.lower())
            self.data[key] = (user.id, user.full_name)
            self.data.move_to_end(key)
            while len(self.data) > self.MAX:
                self.data.popitem(last=False)
        return await handler(event, data)


seen = SeenUsers()


async def is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        m = await bot.get_chat_member(chat_id, user_id)
    except TelegramBadRequest:
        return False
    return m.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)


def extract_targets(message: Message) -> tuple[dict[int, str], list[str]]:
    me = message.from_user.id if message.from_user else 0
    targets: dict[int, str] = {}
    unknown: list[str] = []
    rep = message.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot and rep.from_user.id != me:
        targets[rep.from_user.id] = rep.from_user.full_name
    for ent in message.entities or []:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot and ent.user.id != me:
            targets[ent.user.id] = ent.user.full_name
        elif ent.type == MessageEntityType.MENTION:
            uname = ent.extract_from(message.text)[1:]
            hit = seen.lookup(message.chat.id, uname)
            if hit is None:
                unknown.append(uname)
            elif hit[0] != me:
                targets[hit[0]] = hit[1]
    return targets, unknown


# ============================================================ help
HELP = (
    "🎮 <b>PvP Games Bot</b>\n\n"
    "/pvp — set up a match in a group (2–7 players)\n"
    "/invite @username — invite a player (or reply to them with /invite)\n"
    "/stats · /top · /history — your stats, leaderboard, last matches\n\n"
    "On your turn, send the game emoji yourself (🎲 🏀 ⚽ 🎳 🎯). Don't throw within 60s and I throw for you.\n"
    "⚠️ Once a match has started it can't be stopped."
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


# ============================================================ /pvp wizard (FSM)
@router.message(Command("pvp"), ~GROUP)
async def cmd_pvp_private(message: Message) -> None:
    await message.answer("Add me to a group and use /pvp there 🎮")


@router.message(Command("pvp"), GROUP)
async def cmd_pvp(message: Message, state: FSMContext) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return await message.reply("⚠️ Please turn off anonymous-admin mode to start a match.")
    chat_id = message.chat.id
    if manager.get(chat_id):
        return await message.reply("⚠️ A match/lobby is already active in this group.")
    if not manager.reserve_setup(chat_id, user.id):
        return await message.reply("⚠️ Someone is already setting up a match here. Try again in a moment.")

    d = {"creator_id": user.id, "creator_name": user.full_name, "title": message.chat.title or "",
         "thread_id": message.message_thread_id if message.is_topic_message else None}
    await state.clear()
    await state.set_data(d)
    await state.set_state(PvP.choosing_game)
    text, kb = step_view("game", d)
    await message.reply(text, reply_markup=kb)


async def goto(msg: Message, state: FSMContext, step: str) -> None:
    d = await state.get_data()
    await state.set_state(STATE_OF[step])
    text, kb = step_view(step, d)
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def open_lobby(msg: Message, state: FSMContext, d: dict) -> None:
    chat_id = msg.chat.id
    if manager.get(chat_id):
        await state.clear()
        return await msg.edit_text("⚠️ A match already exists in this group.")
    s = GameSession(msg.bot, chat_id, d.get("thread_id"), d.get("title", ""), d["creator_id"],
                    d["creator_name"], d["game"], d["mode"], d["rolls"], d["wins"])

    async def _on_close() -> None:
        await state.clear()

    s.on_close = _on_close
    s.lobby_msg_id = msg.message_id
    manager.add(s)
    await state.set_state(PvP.inviting)
    await msg.edit_text(s.lobby_text(), reply_markup=lobby_markup())
    s.start_lobby_timer()


@router.callback_query(SetupCB.filter(F.step == "cancel"), StateFilter(*SETUP_STATES))
async def setup_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    manager.release_setup(cb.message.chat.id)
    await cb.message.edit_text("❌ Match setup cancelled.")
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "back"), StateFilter(*SETUP_STATES))
async def setup_back(cb: CallbackQuery, state: FSMContext) -> None:
    cur = STEP_OF_STATE.get(await state.get_state())
    idx = ORDER.index(cur)
    if idx == 0:
        return await cb.answer()
    d = await state.get_data()
    d.pop(ORDER[idx - 1], None)
    await state.set_data(d)
    await cb.answer()
    await goto(cb.message, state, ORDER[idx - 1])


@router.callback_query(SetupCB.filter(F.step.in_({"game", "mode", "rolls", "wins"})), StateFilter(*SETUP_STATES))
async def setup_pick(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    cur = STEP_OF_STATE.get(await state.get_state())
    if callback_data.step != cur:
        return await cb.answer("That button is outdated.")
    value = validate(cur, callback_data.value)
    if value is None:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)
    await state.update_data(**{cur: value})
    await cb.answer()
    if cur == "wins":
        return await open_lobby(cb.message, state, await state.get_data())
    await goto(cb.message, state, ORDER[ORDER.index(cur) + 1])


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

    targets, unknown = extract_targets(message)
    try:
        await message.delete()
    except Exception:
        pass

    problems: list[str] = []
    for uid, name in targets.items():
        if uid not in s.invited:
            err = s.can_invite(uid)
            if err:
                problems.append(f"{html.escape(name)}: {err}")
                continue
        s.invited[uid] = name
        fail = await s.send_invite(uid, name)
        if fail:
            s.invited.pop(uid, None)
            problems.append(fail)
    for uname in unknown:
        problems.append(f"@{html.escape(uname)}: I can't find them yet — reply to one of their messages with /invite")

    await s.refresh_lobby()
    if not targets and not unknown:
        await s.tell_creator("Use <code>/invite @username</code>, or reply to a player's message with /invite.")
    elif problems:
        await s.tell_creator("⚠️ Couldn't invite:\n• " + "\n• ".join(problems))


@router.callback_query(InviteCB.filter())
async def on_invite(cb: CallbackQuery, callback_data: InviteCB) -> None:
    me = cb.from_user
    if me.id != callback_data.user_id:
        return await cb.answer("This invite is not for you! ❌", show_alert=True)

    s = manager.get(callback_data.chat_id)
    if not s or s.status != "lobby" or (me.id not in s.invited and me.id not in s.players):
        return await cb.answer("This invite has expired.", show_alert=True)

    if callback_data.action == "no":
        await s.resolve_invite(me.id, "❌ You declined the invite.", f"❌ {html.escape(me.full_name)} declined your invite.")
        await s.refresh_lobby()
        return await cb.answer("Declined")

    err = s.accept(me.id, me.full_name)
    if err:
        return await cb.answer(err, show_alert=True)
    n = len(s.players)
    await s.resolve_invite(me.id, f"✅ You joined the match! ({n}/{MAX_PLAYERS})",
                           f"✅ {html.escape(me.full_name)} joined ({n}/{MAX_PLAYERS})")
    await s.refresh_lobby()
    await cb.answer("You're in! 🎉")


@router.callback_query(F.data.in_({"lobby:start", "lobby:cancel"}))
async def on_lobby(cb: CallbackQuery, state: FSMContext) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "lobby":
        return await cb.answer("This lobby is no longer active.", show_alert=True)
    if cb.from_user.id != s.creator_id:
        return await cb.answer("Only the match creator can do this! ❌", show_alert=True)

    if cb.data == "lobby:cancel":
        await s.edit(s.lobby_msg_id, "❌ Lobby cancelled by the creator.")
        await s.close_invites("❌ The lobby was cancelled.")
        await manager.close(s)
        return await cb.answer("Cancelled")

    if len(s.players) < MIN_PLAYERS:
        return await cb.answer(f"Need at least {MIN_PLAYERS} players to start!", show_alert=True)
    await cb.answer("Match starting! 🚀")
    await s.start()
    await state.set_state(PvP.in_match)


@router.message(F.dice, GROUP)
async def on_dice(message: Message) -> None:
    s = manager.get(message.chat.id)
    if not s or s.status != "running" or message.from_user is None:
        return
    if message.forward_origin is not None or message.via_bot is not None:
        return
    await s.handle_dice(message.from_user.id, message.dice.emoji, message.dice.value, message.date)


@router.message(Command("stop", "cancel"), GROUP)
async def cmd_stop(message: Message, bot: Bot) -> None:
    uid, chat_id = (message.from_user.id if message.from_user else 0), message.chat.id
    s = manager.get(chat_id)
    if s and s.status == "running":
        return await message.reply("🔒 The match has already started — it can't be stopped.")
    if not s:
        owner = manager.setup_owner(chat_id)
        if owner and (uid == owner or await is_admin(bot, chat_id, uid)):
            manager.release_setup(chat_id)
            return await message.reply("🛑 Setup cancelled.")
        return await message.reply("Nothing to stop.")
    if uid != s.creator_id and not await is_admin(bot, chat_id, uid):
        return await message.reply("Only the lobby creator or a group admin can cancel the lobby ❌")
    await s.edit(s.lobby_msg_id, "❌ Lobby cancelled.")
    await s.close_invites("❌ The lobby was cancelled.")
    await manager.close(s)


@router.message(Command("forcestop"), GROUP)
async def cmd_forcestop(message: Message) -> None:
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return
    s = manager.get(message.chat.id)
    if s:
        await manager.close(s)
    await database.delete_active(message.chat.id)
    await message.reply("🛑 Match removed by the bot owner (no stats recorded).")


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
        f"🥇 Rounds won: {u.rounds_won}\n"
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
    dp.message.outer_middleware(seen)
    dp.callback_query.outer_middleware(seen)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="pvp", description="Start a PvP match"),
        BotCommand(command="invite", description="Invite a player to the lobby"),
        BotCommand(command="stats", description="Your stats"),
        BotCommand(command="top", description="Leaderboard"),
        BotCommand(command="history", description="Your recent matches"),
        BotCommand(command="help", description="Help"),
    ])
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await manager.restore_all(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await manager.shutdown(bot)
        await database.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
