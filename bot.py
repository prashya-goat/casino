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
from game import (GAMES, MAX_PLAYERS, MIN_PLAYERS, MODES, GameSession, InviteCB, lobby_markup,
                  manager, mention)

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
    step: str       # game | mode | rolls | wins | quick | back | cancel
    value: str = ""


ORDER = ["game", "mode", "rolls", "wins"]
STATE_OF = {"game": PvP.choosing_game, "mode": PvP.choosing_mode,
            "rolls": PvP.choosing_rolls, "wins": PvP.choosing_wins}
STEP_OF_STATE = {st.state: step for step, st in STATE_OF.items()}
SETUP_STATES = tuple(STATE_OF.values())
DEFAULTS = {"mode": "normal", "rolls": 3, "wins": 3}
GAME_WORDS = {"dice": "dice", "basketball": "basketball", "basket": "basketball", "football": "football",
              "bowling": "bowling", "darts": "darts", "dart": "darts"}
QUESTIONS = {
    "game": "Choose the game",
    "mode": "Which mode?\nNormal — highest total wins\nCrazy — lowest total wins",
    "rolls": "How many rolls per round? (1–10)",
    "wins": "First to how many wins? (1–20)",
}


# ============================================================ wizard helpers
def next_step(d: dict) -> Optional[str]:
    return next((s for s in ORDER if s not in d), None)


def back_candidates(step: str, d: dict) -> list[str]:
    return [s for s in ORDER[:ORDER.index(step)] if s not in d.get("preset", [])]


def validate(step: str, value: str) -> Any:
    if step == "game":
        return value if value in GAMES else None
    if step == "mode":
        return value if value in MODES else None
    if step in ("rolls", "wins"):
        hi = 10 if step == "rolls" else 20
        return int(value) if value.isdigit() and 1 <= int(value) <= hi else None
    return None


def setup_header(d: dict) -> str:
    lines = [f"⚔️ {mention(d['creator_id'], d['creator_name'])} is setting up a PvP match"]
    a, b = [], []
    if "game" in d:
        a.append("{} {}".format(*GAMES[d["game"]]))
    if "mode" in d:
        e, n, desc = MODES[d["mode"]]
        a.append(f"{e} {n} — {desc}")
    if "rolls" in d:
        b.append(f"🔁 {d['rolls']} roll(s)")
    if "wins" in d:
        b.append(f"🏆 first to {d['wins']}")
    if a:
        lines.append(" · ".join(a))
    if b:
        lines.append(" · ".join(b))
    return "\n".join(lines)


def step_view(step: str, d: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = setup_header(d) + f"\n\n<blockquote>{QUESTIONS[step]}</blockquote>"
    b = InlineKeyboardBuilder()
    sizes: list[int] = []
    if step == "game":
        for key, (emoji, name) in GAMES.items():
            b.button(text=f"{emoji} {name}", callback_data=SetupCB(step="game", value=key))
        sizes = [2, 2, 1]
    elif step == "mode":
        for key, (emoji, name, _) in MODES.items():
            b.button(text=f"{emoji} {name} Mode", callback_data=SetupCB(step="mode", value=key))
        q = {**DEFAULTS, **{k: d[k] for k in ("mode", "rolls", "wins") if k in d}}
        b.button(text=f"⚡ Quick play — {GAMES[d['game']][0]} {MODES[q['mode']][1]} · {q['rolls']} rolls · first to {q['wins']}",
                 callback_data=SetupCB(step="quick"))
        sizes = [2, 1]
    elif step == "rolls":
        for n in range(1, 11):
            b.button(text=str(n), callback_data=SetupCB(step="rolls", value=str(n)))
        sizes = [5, 5]
    elif step == "wins":
        for n in range(1, 21):
            b.button(text=str(n), callback_data=SetupCB(step="wins", value=str(n)))
        sizes = [5, 5, 5, 5]
    if back_candidates(step, d):
        b.button(text="« Back", callback_data=SetupCB(step="back"))
        sizes.append(2)
    else:
        sizes.append(1)
    b.button(text="✖️ Cancel", callback_data=SetupCB(step="cancel"))
    b.adjust(*sizes)
    return text, b.as_markup()


def parse_args(args: Optional[str]) -> dict:
    """`/dice 3 5 crazy` -> rolls=3, wins=5, mode=crazy (anything missing is asked in the wizard)."""
    out, nums = {}, []
    for tok in (args or "").lower().split():
        if tok.isdigit():
            nums.append(int(tok))
        elif tok in ("normal", "n"):
            out["mode"] = "normal"
        elif tok in ("crazy", "c"):
            out["mode"] = "crazy"
        elif tok in GAME_WORDS:
            out["game"] = GAME_WORDS[tok]
    if nums and 1 <= nums[0] <= 10:
        out["rolls"] = nums[0]
    if len(nums) > 1 and 1 <= nums[1] <= 20:
        out["wins"] = nums[1]
    return out


# ============================================================ RAM-only username memory
class SeenUsers(BaseMiddleware):
    """RAM-only memory of recent (chat, @username) -> user id, so `/invite @user` can send a
    private invite. Never written to disk or the database; cleared on every restart."""

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


def extract_targets(message: Message) -> dict[str, tuple]:
    """Players mentioned by reply / text_mention / @username -> {target: (HTML label, user id or None)}."""
    me = message.from_user.id if message.from_user else 0
    targets: dict[str, tuple] = {}
    rep = message.reply_to_message
    if rep and rep.from_user and not rep.from_user.is_bot and rep.from_user.id != me:
        u = rep.from_user
        targets[str(u.id)] = (mention(u.id, u.full_name), u.id)
    for ent in message.entities or []:
        if ent.type == MessageEntityType.TEXT_MENTION and ent.user and not ent.user.is_bot and ent.user.id != me:
            targets[str(ent.user.id)] = (mention(ent.user.id, ent.user.full_name), ent.user.id)
        elif ent.type == MessageEntityType.MENTION:
            uname = ent.extract_from(message.text)[1:]
            hit = seen.lookup(message.chat.id, uname)
            if hit:
                if hit[0] != me:
                    targets[str(hit[0])] = (mention(hit[0], hit[1]), hit[0])
            else:
                targets[uname.lower()] = ("@" + html.escape(uname), None)
    return targets


async def process_invites(s: GameSession, targets: dict) -> list[str]:
    problems: list[str] = []
    private: list[str] = []
    public_no_id: list[str] = []
    for target, (label, uid) in targets.items():
        if uid is not None and uid in s.players:
            problems.append(f"{label}: already in the match")
            continue
        if target not in s.invited:  # re-sending to an already invited player is allowed
            err = s.can_invite(target, uid)
            if err:
                problems.append(f"{label}: {err}")
                continue
        s.invited[target] = label
        mode = await s.send_invite(target, uid)
        if mode == "private":
            private.append(label)
        elif uid is None:
            public_no_id.append(label)
    await s.refresh_lobby()

    notes = []
    if private:
        notes.append("📩 Private invite sent to " + ", ".join(private) + " — only they can see it. "
                     "If they don't see it (offline?), use /invite again to resend.")
    if public_no_id:
        notes.append("ℹ️ " + ", ".join(public_no_id) + ": I haven't seen them chat here, so I can't message "
                     "them privately. The invite was posted in the group — only they can use its buttons.")
    if problems:
        notes.append("⚠️ Couldn't invite:\n• " + "\n• ".join(problems))
    return notes


async def create_session(bot: Bot, chat_id: int, thread_id: Optional[int], title: str, creator_id: int,
                         creator_name: str, cfg: dict, state: FSMContext) -> GameSession:
    session = GameSession(
        bot=bot, chat_id=chat_id, thread_id=thread_id, chat_title=title or "",
        creator_id=creator_id, creator_name=creator_name, game=cfg["game"], mode=cfg["mode"],
        rolls=cfg["rolls"], target_wins=cfg["wins"])

    async def _on_close() -> None:
        await state.clear()

    session.on_close = _on_close
    manager.add(session)
    await state.set_state(PvP.inviting)
    session.start_lobby_timer()
    return session


# ============================================================ commands
HELP = (
    "🎮 <b>PvP Games Bot</b>\n\n"
    "<b>Start a match (groups)</b>\n"
    "/pvp — step-by-step setup\n"
    "/dice · /basketball · /football · /bowling · /darts — start with that game\n"
    "Shortcuts: <code>/dice 3 5 crazy</code> = 3 rolls, first to 5, Crazy mode\n"
    "Reply to someone with /dice to challenge them privately.\n\n"
    "/invite — invite players (reply to them or /invite @user)\n"
    "/stop — cancel the running match (creator/admin)\n"
    "/stats · /top · /history — your stats, leaderboard, last matches\n\n"
    "🎲 Dice · 🏀 Basketball · ⚽ Football · 🎳 Bowling · 🎯 Darts"
)


@router.message(Command("start", "help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP)


# ============================================================ setup wizard (FSM)
@router.message(Command("pvp", "dice", "basketball", "basket", "football", "bowling", "darts"), GROUP)
async def cmd_match(message: Message, command: CommandObject, state: FSMContext) -> None:
    user = message.from_user
    if user is None or user.is_bot:  # anonymous admin posts can't be tracked
        return await message.reply("⚠️ Please disable anonymous-admin mode to start a match.")
    chat_id = message.chat.id
    if manager.get(chat_id):
        return await message.reply("⚠️ A match/lobby is already active here. Use /stop to cancel it.")
    if not manager.reserve_setup(chat_id, user.id):
        return await message.reply("⚠️ Someone is already setting up a match here. Try again in a moment.")

    preset = parse_args(command.args)
    cmd_game = GAME_WORDS.get((command.command or "").lower())
    if cmd_game:
        preset["game"] = cmd_game

    d = {
        "creator_id": user.id, "creator_name": user.full_name, "title": message.chat.title or "",
        "thread_id": message.message_thread_id if message.is_topic_message else None,
        "challenge": extract_targets(message), "preset": list(preset), **preset,
    }
    await state.clear()
    await state.set_data(d)
    step = next_step(d)
    if step is None:  # everything was given in the command -> straight to the lobby
        m = await message.reply("⏳ Creating lobby…")
        return await finish_setup(m, state, d)
    await state.set_state(STATE_OF[step])
    text, kb = step_view(step, d)
    await message.reply(text, reply_markup=kb)  # reply => the command is quoted above the wizard


async def goto(msg: Message, state: FSMContext, step: Optional[str]) -> None:
    d = await state.get_data()
    if step is None:
        return await finish_setup(msg, state, d)
    await state.set_state(STATE_OF[step])
    text, kb = step_view(step, d)
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


async def finish_setup(msg: Message, state: FSMContext, d: dict) -> None:
    chat_id = msg.chat.id
    if manager.get(chat_id):
        await state.clear()
        return await msg.edit_text("⚠️ A match already exists in this group.")
    session = await create_session(msg.bot, chat_id, d.get("thread_id"), d.get("title", ""),
                                   d["creator_id"], d["creator_name"], d, state)
    session.lobby_msg_id = msg.message_id
    await msg.edit_text(session.lobby_text(), reply_markup=lobby_markup())
    if d.get("challenge"):  # `/dice` sent as a reply / with @mention => challenge them right away
        notes = await process_invites(session, d["challenge"])
        if notes:
            await session.send("\n\n".join(notes))


@router.callback_query(SetupCB.filter(F.step == "cancel"), StateFilter(*SETUP_STATES))
async def setup_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    manager.release_setup(cb.message.chat.id)
    await cb.message.edit_text("❌ Match setup cancelled.")
    await cb.answer()


@router.callback_query(SetupCB.filter(F.step == "back"), StateFilter(*SETUP_STATES))
async def setup_back(cb: CallbackQuery, state: FSMContext) -> None:
    cur = STEP_OF_STATE.get(await state.get_state())
    d = await state.get_data()
    cands = back_candidates(cur, d)
    if not cands:
        return await cb.answer("Nothing to go back to.")
    d.pop(cands[-1], None)
    await state.set_data(d)
    await cb.answer()
    await goto(cb.message, state, cands[-1])


@router.callback_query(SetupCB.filter(F.step == "quick"), StateFilter(*SETUP_STATES))
async def setup_quick(cb: CallbackQuery, state: FSMContext) -> None:
    d = await state.get_data()
    if "game" not in d:
        return await cb.answer("Pick a game first.", show_alert=True)
    for k, v in DEFAULTS.items():
        d.setdefault(k, v)
    await state.set_data(d)
    await cb.answer("⚡ Quick play!")
    await goto(cb.message, state, None)


@router.callback_query(SetupCB.filter(F.step.in_({"game", "mode", "rolls", "wins"})), StateFilter(*SETUP_STATES))
async def setup_pick(cb: CallbackQuery, callback_data: SetupCB, state: FSMContext) -> None:
    cur = STEP_OF_STATE.get(await state.get_state())
    if callback_data.step != cur:
        return await cb.answer("That button is outdated.")
    value = validate(cur, callback_data.value)
    if value is None:
        return await cb.answer()
    manager.reserve_setup(cb.message.chat.id, cb.from_user.id)  # keep the wizard alive
    await state.update_data(**{cur: value})
    d = await state.get_data()
    await cb.answer()
    await goto(cb.message, state, next_step(d))


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
    targets = extract_targets(message)
    if not targets:
        return await message.reply("Reply to a player's message with /invite, or use <code>/invite @username</code>.")
    notes = await process_invites(s, targets)
    if notes:
        await message.reply("\n\n".join(notes))


@router.callback_query(InviteCB.filter())
async def on_invite(cb: CallbackQuery, callback_data: InviteCB) -> None:
    me, target = cb.from_user, callback_data.target
    # Privacy: valid only for the targeted user (matched by id OR by @username)
    if not (target == str(me.id) or (me.username and target == me.username.lower())):
        return await cb.answer("This invite is not for you! ❌", show_alert=True)

    s = manager.get(callback_data.chat_id)
    if not s or s.status != "lobby" or (target not in s.invited and me.id not in s.players):
        return await cb.answer("This invite has expired.", show_alert=True)

    if callback_data.action == "no":
        s.invited.pop(target, None)
        await s.set_invite_result(target, f"❌ {mention(me.id, me.full_name)} declined the invite.")
        await s.refresh_lobby()
        return await cb.answer("Declined")

    err = s.accept(me.id, me.full_name, target)
    if err:
        return await cb.answer(err, show_alert=True)
    await s.set_invite_result(target, f"✅ You joined the match! ({len(s.players)}/{MAX_PLAYERS})")
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


# ============================================================ in-match
@router.callback_query(F.data == "throw")
async def on_throw(cb: CallbackQuery) -> None:
    s = manager.get(cb.message.chat.id)
    if not s or s.status != "running":
        return await cb.answer("This match is over.", show_alert=True)
    if s.current_turn is None or cb.message.message_id != s.turn_msg_id:
        return await cb.answer("That turn is already finished.")
    if cb.from_user.id != s.current_turn:
        return await cb.answer("It's not your turn! ⏳", show_alert=True)
    s.request_bot_throw()
    await cb.answer("🤖 Throwing for you…")


@router.message(F.dice, GROUP)
async def on_user_dice(message: Message) -> None:
    """A player threw the emoji themselves during their turn."""
    s = manager.get(message.chat.id)
    if not s or s.status != "running" or message.from_user is None:
        return
    if message.forward_origin is not None or message.via_bot is not None:
        return  # forwarded / inline-bot dice don't count
    s.submit_roll(message.from_user.id, message.dice.emoji, message.dice.value, message.date)


@router.callback_query(F.data == "rematch")
async def on_rematch(cb: CallbackQuery, state: FSMContext) -> None:
    chat_id, me = cb.message.chat.id, cb.from_user
    last = manager.last_match.get(chat_id)
    if not last:
        return await cb.answer("Rematch expired — use /pvp.", show_alert=True)
    if me.id not in {uid for uid, _ in last["players"]}:
        return await cb.answer("Only players of that match can start a rematch ❌", show_alert=True)
    if manager.get(chat_id) or manager.setup_owner(chat_id):
        return await cb.answer("A match/lobby is already open here.", show_alert=True)

    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # one rematch per result
    except TelegramBadRequest:
        pass
    await state.clear()
    cfg = {"game": last["game"], "mode": last["mode"], "rolls": last["rolls"], "wins": last["wins"]}
    s = await create_session(cb.bot, chat_id, last["thread_id"], last["title"], me.id, me.full_name, cfg, state)
    msg = await s.send(s.lobby_text(), reply_markup=lobby_markup())
    s.lobby_msg_id = msg.message_id
    others = {str(uid): (mention(uid, name), uid) for uid, name in last["players"] if uid != me.id}
    notes = await process_invites(s, others)
    await cb.answer("🔁 Rematch lobby created!")
    if notes:
        await s.send("\n\n".join(notes))


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
        lines.append(f"{emoji} {MODES.get(m.mode, ('', m.mode))[1]} · {m.player_count} players · "
                     f"you {r.series_wins}/{m.target_wins} · {result} · {m.finished_at:%d %b %Y}\n"
                     f"<code>{m.game_id or m.id}</code>")
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
    dp.message.outer_middleware(seen)
    dp.callback_query.outer_middleware(seen)
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="pvp", description="Start a PvP match (step by step)"),
        BotCommand(command="dice", description="Start a Dice match"),
        BotCommand(command="basketball", description="Start a Basketball match"),
        BotCommand(command="football", description="Start a Football match"),
        BotCommand(command="bowling", description="Start a Bowling match"),
        BotCommand(command="darts", description="Start a Darts match"),
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
