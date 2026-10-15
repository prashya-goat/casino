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
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

import database
import jackpot
from jackpot import JackpotInviteCB, parse_amount
from game import (GAMES, MAX_PLAYERS, MEDALS, MIN_PLAYERS, MODES, STAKES, GameSession, InviteCB, btn,
                  lobby_markup, manager, mention, money, money_signed)

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
    choosing_stake = State()
    inviting = State()
    in_match = State()


class SetupCB(CallbackData, prefix="su"):
    step: str       # game | mode | rolls | wins | stake | back | cancel
    value: str = ""


STATE_OF = {"game": PvP.choosing_game, "mode": PvP.choosing_mode,
            "rolls": PvP.choosing_rolls, "wins": PvP.choosing_wins, "stake": PvP.choosing_stake}
STEP_OF_STATE = {st.state: step for step, st in STATE_OF.items()}
SETUP_STATES = tuple(STATE_OF.values())
ORDER = ["game", "mode", "rolls", "wins", "stake"]
QUESTIONS = {
    "game": "Which game?",
    "mode": "Which mode?\nNormal — highest total wins\nCrazy — lowest total wins",
    "rolls": "How many rolls per round?",
    "wins": "First to how many wins?",
    "stake": "How much is each player playing for?\n(virtual money — just for fun & stats, nothing real)",
}


# ============================================================ wizard views (exact layouts)
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
    if "stake" in d:
        parts.append(f"💰 {money(d['stake'])}" if d["stake"] else "🆓 Free play")
    if parts:
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def step_view(step: str, d: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = setup_header(d) + f"\n\n<blockquote>{QUESTIONS[step]}</blockquote>"
    b = InlineKeyboardBuilder()
    if step == "game":      # [🎲][🏀] / [⚽][🎳] / [🎯] / [❌ Cancel]
        for key, (emoji, name) in GAMES.items():
            b.add(btn(f"{emoji} {name}", SetupCB(step="game", value=key).pack(), "primary"))
        b.add(btn("❌ Cancel", SetupCB(step="cancel").pack(), "danger"))
        b.adjust(2, 2, 1, 1)
    elif step == "mode":    # one button per row, full width
        b.add(btn("🟢 Normal Mode", SetupCB(step="mode", value="normal").pack(), "success"))
        b.add(btn("🔴 Crazy Mode", SetupCB(step="mode", value="crazy").pack(), "danger"))
        b.add(btn("« Back", SetupCB(step="back").pack()))
        b.adjust(1, repeat=True)
    elif step == "rolls":   # 1 Roll / 2 Rolls ... (full width, uses the chosen game's emoji)
        emoji = GAMES[d["game"]][0]
        for n in range(1, 11):
            b.add(btn(f"{emoji} {n} Roll{'s' if n > 1 else ''}", SetupCB(step="rolls", value=str(n)).pack(), "primary"))
        b.add(btn("« Back", SetupCB(step="back").pack()))
        b.adjust(1, repeat=True)
    elif step == "wins":    # 🏆 1 Win / 🏆 2 Wins ... (full width)
        for n in range(1, 21):
            b.add(btn(f"🏆 {n} Win{'s' if n > 1 else ''}", SetupCB(step="wins", value=str(n)).pack(), "primary"))
        b.add(btn("« Back", SetupCB(step="back").pack()))
        b.adjust(1, repeat=True)
    elif step == "stake":   # [Free] / 2 per row / [Back]
        for c in STAKES:
            b.add(btn("🆓 Free play" if c == 0 else f"💰 {money(c)}", SetupCB(step="stake", value=str(c)).pack(),
                      "success" if c == 0 else "primary"))
        b.add(btn("« Back", SetupCB(step="back").pack()))
        b.adjust(1, 2, 2, 2, 2, 1, 1)
    return text, b.as_markup()


def validate(step: str, value: str) -> Any:
    if step == "game":
        return value if value in GAMES else None
    if step == "mode":
        return value if value in MODES else None
    if step == "stake":
        return int(value) if value.isdigit() and int(value) in STAKES else None
    hi = 10 if step == "rolls" else 20
    return int(value) if value.isdigit() and 1 <= int(value) <= hi else None


# ============================================================ RAM-only username memory
class SeenUsers(BaseMiddleware):
    """RAM-only memory of recent (chat, @username) -> user id, so `/invite @user` can find the id.
    Never written to disk or the database; cleared on every restart."""

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


def extract_targets(message: Message, bot_username: str = "") -> tuple[dict[int, str], list[str]]:
    """Reply / text_mention / @username -> ({user_id: name}, [usernames we couldn't resolve])."""
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
            if uname.lower() == bot_username.lower():
                continue
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
    "/invite @username — invite a player (or just type @username as the lobby creator, or reply with /invite)\n"
    "/jackpot [amount] — start/join a 🎰 Jackpot: everyone bets, a wheel picks ONE winner who takes the whole pot\n"
    "/stats · /top · /history — your stats, leaderboard, last matches\n\n"
    "On your turn, send the game emoji yourself (🎲 🏀 ⚽ 🎳 🎯). If I'm not an admin, reply to my message with it "
    "or tap the button. No throw within 60s (I warn you at 30s) and I throw for you.\n"
    "/stop — the match creator can stop a running match (no stats are recorded)"
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
    if user is None or user.is_bot:  # anonymous admin posts can't be tracked
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
    await message.reply(text, reply_markup=kb)  # reply => the /pvp command is quoted above the wizard


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
                    d["creator_name"], d["game"], d["mode"], d["rolls"], d["wins"], d.get("stake", 0))

    async def _on_close() -> None:
        await state.clear()

    s.on_close = _on_close
    await s.refresh_limited()
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


@router.callback_query(SetupCB.filter(F.step.in_({"game", "mode", "rolls", "wins", "stake"})), StateFilter(*SETUP_STATES))
async def setup_pick(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    cur = STEP_OF_STATE.get(await state.get_state())
    if callback_data.step != cur:
        return await cb.answer("That button is outdated.")
    value = validate(cur, callback_data.value)
    if value is None:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)  # keep the wizard alive
    await state.update_data(**{cur: value})
    await cb.answer()
    if cur == "stake":
        return await open_lobby(cb.message, state, await state.get_data())
    await goto(cb.message, state, ORDER[ORDER.index(cur) + 1])


# Anyone else touching a wizard menu (or a stale one):
@router.callback_query(F.data.startswith("su:"))
async def setup_not_yours(cb: CallbackQuery) -> None:
    await cb.answer("This menu isn't yours (or it expired) ❌", show_alert=True)


# ============================================================ invites (visible only to the invited player + creator)
async def collect_targets(message: Message) -> tuple[dict[int, str], list[str]]:
    """Everybody mentioned in the creator's message -> ({id: name}, usernames we couldn't resolve).
    Also removes the creator's message to keep the group clean (needs 'delete messages')."""
    me = await message.bot.me()
    targets, unknown = extract_targets(message, me.username or "")
    still_unknown: list[str] = []
    for uname in unknown:  # not seen yet -> ask Telegram (works for people the bot already knows)
        try:
            chat = await message.bot.get_chat(f"@{uname}")
            if chat.type == ChatType.PRIVATE and chat.id != message.from_user.id:
                targets[chat.id] = " ".join(x for x in (chat.first_name, chat.last_name) if x) or uname
                continue
        except Exception:
            pass
        still_unknown.append(uname)
    try:
        await message.delete()
    except Exception:
        pass
    return targets, still_unknown


async def run_invites(message: Message, s: GameSession) -> None:
    """Invite everybody mentioned in `message` (creator only, checked by the callers)."""
    targets, still_unknown = await collect_targets(message)

    problems: list[str] = []
    for uid, name in targets.items():
        if uid not in s.invited:  # re-sending to an already invited player is allowed
            err = s.can_invite(uid)
            if err:
                problems.append(f"{html.escape(name)}: {err}")
                continue
        s.invited[uid] = name
        fail = await s.send_invite(uid, name)
        if fail:
            s.invited.pop(uid, None)
            problems.append(fail)
    for uname in still_unknown:
        problems.append(f"@{html.escape(uname)}: I can't find them yet — reply to one of their messages with /invite")

    await s.refresh_lobby()
    if not targets and not still_unknown:
        await s.tell_creator("Type <code>@username</code> of the player (or reply to their message with /invite).")
    elif problems:
        await s.tell_creator("⚠️ Couldn't invite:\n• " + "\n• ".join(problems))


async def run_jp_invites(message: Message, j: "jackpot.JackpotSession") -> None:
    targets, still_unknown = await collect_targets(message)
    problems: list[str] = []
    for uid, name in targets.items():
        if uid not in j.invited:
            err = j.can_invite(uid)
            if err:
                problems.append(f"{html.escape(name)}: {err}")
                continue
        j.invited[uid] = name
        fail = await j.send_invite(uid, name)
        if fail:
            j.invited.pop(uid, None)
            problems.append(fail)
    for uname in still_unknown:
        problems.append(f"@{html.escape(uname)}: I can't find them yet — reply to one of their messages with /invite")
    if not targets and not still_unknown:
        await j.tell_creator("Type <code>@username</code> of the player (or reply to their message with /invite).")
    elif problems:
        await j.tell_creator("⚠️ Couldn't invite:\n• " + "\n• ".join(problems))


@router.message(Command("invite"), GROUP)
async def cmd_invite(message: Message) -> None:
    uid = message.from_user.id if message.from_user else 0
    s = manager.get(message.chat.id)
    if s and s.status == "lobby":
        if uid != s.creator_id:
            return await message.reply("Only the match creator can invite players ❌")
        return await run_invites(message, s)
    j = jackpot.manager.get(message.chat.id)
    if j and j.status in ("lobby", "countdown"):
        if uid != j.creator_id:
            return await message.reply("Only the jackpot creator can invite players ❌")
        return await run_jp_invites(message, j)
    await message.reply("No open lobby here. Start one with /pvp or /jackpot.")


def _mention_only(message: Message) -> bool:
    """True for a lobby creator's message that contains nothing but @mentions (e.g. '@parshya @rahul')."""
    if not message.text or not message.entities or not message.from_user:
        return False
    s = manager.get(message.chat.id)
    if not s or s.status != "lobby" or message.from_user.id != s.creator_id:
        return False
    return _only_mentions(message)


def _only_mentions(message: Message) -> bool:
    raw = message.text.encode("utf-16-le")
    keep, last, found = [], 0, False
    for ent in sorted(message.entities, key=lambda e: e.offset):
        if ent.type in (MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION):
            keep.append(raw[last * 2:ent.offset * 2])
            last, found = ent.offset + ent.length, True
    keep.append(raw[last * 2:])
    rest = b"".join(keep).decode("utf-16-le")
    return found and rest.strip(" ,;&+\n\t") == ""


@router.message(GROUP, F.text, _mention_only)
async def on_mention_invite(message: Message) -> None:
    s = manager.get(message.chat.id)
    if s and s.status == "lobby":
        await run_invites(message, s)


def _jp_mention_only(message: Message) -> bool:
    if not message.text or not message.entities or not message.from_user:
        return False
    j = jackpot.manager.get(message.chat.id)
    if not j or j.status not in ("lobby", "countdown") or message.from_user.id != j.creator_id:
        return False
    return _only_mentions(message)


@router.message(GROUP, F.text, _jp_mention_only)
async def on_jp_mention_invite(message: Message) -> None:
    j = jackpot.manager.get(message.chat.id)
    if j and j.status in ("lobby", "countdown"):
        await run_jp_invites(message, j)


# ============================================================ jackpot
@router.message(Command("jackpot"), ~GROUP)
async def cmd_jackpot_private(message: Message) -> None:
    await message.answer("Add me to a group and use /jackpot there 🎰")


@router.message(Command("jackpot"), GROUP)
async def cmd_jackpot(message: Message, command: CommandObject) -> None:
    user = message.from_user
    if user is None or user.is_bot:
        return await message.reply("⚠️ Please turn off anonymous-admin mode to play.")
    cents = None
    if command.args:
        cents = parse_amount(command.args.split()[0])
        if cents is None:
            return await message.reply("Use it like <code>/jackpot 10</code> (amount in $).")
    j = jackpot.manager.get(message.chat.id)
    created = j is None
    if created:
        j = jackpot.JackpotSession(message.bot, message.chat.id,
                                   message.message_thread_id if message.is_topic_message else None,
                                   user.id, user.full_name, await database.get_jackpot_meta())
        jackpot.manager.sessions[message.chat.id] = j
        await j.open()
    elif j.status == "spinning":
        return await message.reply("🎡 The wheel is spinning — wait for the result!")
    if cents is None:
        if created:
            return  # just opened: the jackpot message explains everything
        return await message.reply("Pick an amount: press a button on the jackpot message, or <code>/jackpot 10</code>.")
    err, info = await j.place_bet(user.id, user.full_name, cents)
    await message.reply(err or info)


@router.callback_query(F.data.startswith("jp:"))
async def on_jackpot_button(cb: CallbackQuery) -> None:
    j = jackpot.manager.get(cb.message.chat.id)
    if not j or cb.message.message_id != j.msg_id or j.status not in ("lobby", "countdown"):
        return await cb.answer("This jackpot is closed.", show_alert=True)
    parts = cb.data.split(":")
    if parts[1] == "cancel":
        if cb.from_user.id != j.creator_id:
            return await cb.answer("Only the jackpot creator can cancel! ❌", show_alert=True)
        if j.status != "lobby":
            return await cb.answer("Too late — the countdown has started.", show_alert=True)
        await j._edit_caption("❌ Jackpot cancelled by the creator.", markup=None)
        await j.close_invites("❌ The jackpot was cancelled.")
        await jackpot.manager.close(j)
        return await cb.answer("Cancelled")
    cents = int(parts[2])
    err, info = await j.place_bet(cb.from_user.id, cb.from_user.full_name, cents)
    await cb.answer(err or info, show_alert=bool(err))


@router.callback_query(JackpotInviteCB.filter())
async def on_jackpot_invite(cb: CallbackQuery, callback_data: JackpotInviteCB) -> None:
    me = cb.from_user
    if me.id != callback_data.user_id:
        return await cb.answer("This invite is not for you! ❌", show_alert=True)
    j = jackpot.manager.get(callback_data.chat_id)
    if not j or j.status not in ("lobby", "countdown") or (me.id not in j.invited and me.id not in j.bets):
        return await cb.answer("This invite has expired.", show_alert=True)
    if callback_data.action == "no":
        await j.resolve_invite(me.id, "❌ You declined the jackpot invite.",
                               f"❌ {html.escape(me.full_name)} declined your jackpot invite.")
        return await cb.answer("Declined")
    err, info = await j.place_bet(me.id, me.full_name, callback_data.cents)
    if err:
        return await cb.answer(err, show_alert=True)
    await j.resolve_invite(me.id, info, f"✅ {html.escape(me.full_name)} joined with {money(callback_data.cents)}")
    await cb.answer("You're in! 🎰")


@router.callback_query(InviteCB.filter())
async def on_invite(cb: CallbackQuery, callback_data: InviteCB) -> None:
    me = cb.from_user
    if me.id != callback_data.user_id:  # security: the invite works only for the targeted user
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
        await s.close_invites("❌ The lobby was cancelled.")
        await manager.close(s)
        return await cb.answer("Cancelled")

    if len(s.players) < MIN_PLAYERS:
        return await cb.answer(f"Need at least {MIN_PLAYERS} players to start!", show_alert=True)
    await cb.answer("Match starting! 🚀")
    await s.start()
    await state.set_state(PvP.in_match)


# ============================================================ in-match: dice
@router.message(F.dice, GROUP)
async def on_dice(message: Message) -> None:
    s = manager.get(message.chat.id)
    if not s or s.status != "running" or message.from_user is None:
        return
    if message.forward_origin is not None or message.via_bot is not None:
        return  # forwarded / inline-bot dice never count
    rep = message.reply_to_message
    if s.limited and not (rep and rep.from_user and rep.from_user.id == message.bot.id):
        s.limited = False  # we can see plain messages -> the bot can read everything (privacy mode off / admin)
    await s.handle_dice(message.from_user.id, message.dice.emoji, message.dice.value, message.date)


@router.callback_query(F.data == "throw")
async def on_throw(cb: CallbackQuery) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "running":
        return await cb.answer("No match is running.", show_alert=True)
    cur = s.current_player()
    if cb.from_user.id != cur.id:
        return await cb.answer(f"It's {cur.name}'s turn ❌", show_alert=True)
    await cb.answer("Throwing! 🎲")
    err = await s.throw_button(cb.from_user.id)
    if err:
        try:
            await cb.answer(err, show_alert=True)
        except Exception:
            pass


@router.message(Command("stop", "cancel"), GROUP)
async def cmd_stop(message: Message, bot: Bot) -> None:
    uid, chat_id = (message.from_user.id if message.from_user else 0), message.chat.id
    s = manager.get(chat_id)
    if s and s.status == "running":
        if uid != s.creator_id:
            return await message.reply("Only the match creator can stop the match ❌")
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn("🛑 Yes, stop", "stop:yes", "danger"),
                                                    btn("↩️ Keep playing", "stop:no", "success")]])
        return await message.reply("🛑 <b>Stop the running match?</b>\nNo stats will be recorded.", reply_markup=kb)
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


@router.callback_query(F.data.in_({"stop:yes", "stop:no"}))
async def on_stop_confirm(cb: CallbackQuery) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "running":
        try:
            await cb.message.edit_text("This match is already over.")
        except Exception:
            pass
        return await cb.answer()
    if cb.from_user.id != s.creator_id:
        return await cb.answer("Only the match creator can do this! ❌", show_alert=True)
    if cb.data == "stop:no":
        await cb.message.edit_text("👍 Okay, the match continues.")
        return await cb.answer()
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer("Match stopped")
    await s.stop_match(cb.from_user.full_name)


@router.message(Command("forcestop"), GROUP)
async def cmd_forcestop(message: Message) -> None:
    """Emergency switch for the BOT OWNER only (OWNER_IDS env). Players cannot use it."""
    if not message.from_user or message.from_user.id not in OWNER_IDS:
        return
    s = manager.get(message.chat.id)
    if s:
        await manager.close(s)
    await database.delete_active(message.chat.id)
    await message.reply("🛑 Match removed by the bot owner (no stats recorded).")


# ============================================================ stats / top / history
@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    rep = message.reply_to_message
    target = rep.from_user if rep and rep.from_user and not rep.from_user.is_bot else message.from_user
    u = await database.get_user(target.id)
    if not u or u.matches_played == 0:
        return await message.reply(f"{html.escape(target.full_name)} hasn't finished any matches yet.")
    rate = u.wins / u.matches_played * 100
    lines = [
        f"📊 <b>Stats — {html.escape(u.full_name)}</b>",
        "",
        f"🎮 Matches played:  <b>{u.matches_played}</b>",
        f"🏆 Wins:  <b>{u.wins}</b>   ·   💀 Losses:  <b>{u.losses}</b>",
        f"📈 Win rate:  <b>{rate:.0f}%</b>",
        "",
        f"🥇 Rounds won:  <b>{u.rounds_won}</b>",
        f"🎯 Total points rolled:  <b>{u.total_points}</b>",
    ]
    if u.wagered_cents:
        lines += ["", f"💰 Virtual balance:  <b>{money_signed(u.net_cents)}</b>",
                  f"🎟 Total staked:  {money(u.wagered_cents)}"]
    if u.jp_played:
        lines += ["", f"🎰 Jackpots:  played <b>{u.jp_played}</b> · won <b>{u.jp_wins}</b> · "
                      f"net <b>{money_signed(u.jp_net_cents)}</b>"]
    await message.reply("\n".join(lines))


@router.message(Command("top"))
async def cmd_top(message: Message) -> None:
    rows = await database.top_players(10)
    if not rows:
        return await message.reply("No finished matches yet — be the first! /pvp")
    blocks = ["🏅 <b>Leaderboard</b>"]
    for i, u in enumerate(rows):
        rate = u.wins / u.matches_played * 100
        extra = f" · 💰 {money_signed(u.net_cents)}" if u.wagered_cents else ""
        blocks.append(f"{MEDALS[i] if i < 3 else f'<b>{i + 1}.</b>'} <b>{html.escape(u.full_name)}</b>\n"
                      f"└ {u.wins}W · {u.losses}L · {rate:.0f}%{extra}")
    await message.reply("\n\n".join(blocks))


@router.message(Command("history"))
async def cmd_history(message: Message) -> None:
    rows = await database.recent_matches(message.from_user.id, 5)
    if not rows:
        return await message.reply("No match history yet.")
    me_id = message.from_user.id
    blocks = ["🕘 <b>Your last matches</b>"]
    for m in rows:
        emoji = GAMES.get(m.game, ("🎮",))[0]
        mode_e, mode_n = MODES.get(m.mode, ("", m.mode))[:2]
        stake = f"💰 {money(m.stake_cents)}" if m.stake_cents else "🆓 Free"
        head = (f"<b>#{m.id}</b> · {emoji} {mode_e} {mode_n} · {m.player_count} players\n"
                f"<i>{m.rolls} roll(s) · first to {m.target_wins} · {stake} · {m.finished_at:%d %b %Y}</i>")
        standing = []
        for i, p in enumerate(m.players):
            name = html.escape(p["name"])
            if p["user_id"] == me_id:
                name = f"<b>{name} (you)</b>"
            standing.append(f"{MEDALS[i] if i < 3 else '▫️'} {name} — {p['series_wins']}")
        money_txt = f" {money_signed(m.net_cents)}" if m.stake_cents else ""
        result = f"✅ <b>Won</b>{money_txt}" if m.is_winner else f"❌ <b>Lost</b>{money_txt}"
        blocks.append(f"{head}\n<blockquote>{chr(10).join(standing)}</blockquote>\n{result}")
    await message.reply("\n\n".join(blocks))


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
    dp.message.outer_middleware(seen)
    dp.callback_query.outer_middleware(seen)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="pvp", description="Start a PvP match"),
        BotCommand(command="jackpot", description="Start / join a Jackpot"),
        BotCommand(command="invite", description="Invite a player to the lobby"),
        BotCommand(command="stats", description="Your stats"),
        BotCommand(command="top", description="Leaderboard"),
        BotCommand(command="history", description="Your recent matches"),
        BotCommand(command="help", description="Help"),
    ])
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await manager.restore_all(bot)  # continue matches that were running before the restart
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await manager.shutdown(bot)     # running matches are saved, not cancelled
        await jackpot.manager.shutdown()
        await database.close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
