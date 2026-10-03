"""Game engine: lobby, turn loop, live match message and per-chat session manager.

Every group chat gets its own GameSession (keyed by chat_id) running in its own
asyncio task, so matches in different groups never share state.
"""
import asyncio
import html
import logging
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

try:  # Bot API 10.3 ephemeral ("only visible to you") messages - needs aiogram >= 3.31
    from aiogram.types import EphemeralMessageParameters
except ImportError:  # pragma: no cover
    EphemeralMessageParameters = None

import database

log = logging.getLogger("pvp.game")

MIN_PLAYERS = 2
MAX_PLAYERS = 7
LOBBY_TIMEOUT = 300   # seconds before an unstarted lobby expires
SETUP_TIMEOUT = 300   # seconds before an abandoned setup wizard frees the chat
TURN_TIMEOUT = 60     # seconds of inactivity before the bot throws for the player
ANIM_DELAY = 4.0      # Telegram dice animation length (also keeps us under flood limits)

GAMES = {
    "dice": ("🎲", "Dice"),
    "basketball": ("🏀", "Basketball"),
    "football": ("⚽", "Football"),
    "bowling": ("🎳", "Bowling"),
    "darts": ("🎯", "Darts"),
}
GAME_CODES = {"dice": "DI", "basketball": "BA", "football": "FO", "bowling": "BO", "darts": "DA"}
MODES = {
    "normal": ("📈", "Normal", "highest total wins"),
    "crazy": ("🤪", "Crazy", "lowest total wins"),
}
MEDALS = ["🥇", "🥈", "🥉"]
WIN_QUOTES = [
    "The dice made this look easy.",
    "Pure skill. Okay, mostly luck.",
    "Fortune favours the bold.",
    "The table has a new boss.",
    "Smooth roll, smoother victory.",
    "Dice never lie — they just have favourites.",
    "That wasn't luck. Okay, it was a little luck.",
    "Rolled like a legend.",
    "Calm hands, loud result.",
    "The dice filed your rivals' game plan under fiction.",
    "Somebody call the dice — they want an autograph.",
    "Gravity did its job. So did you.",
]


def esc(s: str) -> str:
    return html.escape(s)


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def lobby_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Start match", callback_data="lobby:start"),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="lobby:cancel"),
    ]])


def throw_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Bot, throw for me", callback_data="throw")]])


def rematch_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔁 Rematch", callback_data="rematch")]])


class InviteCB(CallbackData, prefix="inv"):
    action: str   # yes | no
    chat_id: int
    target: str   # user id (digits) or lowercase username


@dataclass(eq=False)
class Player:
    id: int
    name: str
    series_wins: int = 0
    total_points: int = 0
    round_rolls: list = field(default_factory=list)
    done: bool = False


class GameSession:
    def __init__(self, bot: Bot, chat_id: int, thread_id: Optional[int], chat_title: str,
                 creator_id: int, creator_name: str, game: str, mode: str, rolls: int, target_wins: int):
        self.bot, self.chat_id, self.thread_id, self.chat_title = bot, chat_id, thread_id, chat_title
        self.creator_id, self.creator_name = creator_id, creator_name
        self.game, self.mode, self.rolls, self.target_wins = game, mode, rolls, target_wins
        self.game_id = f"PV-{GAME_CODES[game]}-{secrets.token_hex(3).upper()}"

        self.players: dict[int, Player] = {creator_id: Player(creator_id, creator_name)}
        # target = str(user_id) or lowercase username  ->  HTML label shown in the lobby
        self.invited: dict[str, str] = {}
        # target -> ("eph", ephemeral_id, user_id) | ("pub", message_id)
        self.invite_msgs: dict[str, tuple] = {}

        self.status = "lobby"  # lobby -> running -> closed
        self.lobby_msg_id: Optional[int] = None
        self.board_msg_id: Optional[int] = None
        self.board_date: Optional[datetime] = None
        self.turn_msg_id: Optional[int] = None
        self.turn_date: Optional[datetime] = None
        self.board_header = ""
        self.last_note = ""
        self.round_no = 0
        self.participants: list[Player] = []
        self.current_turn: Optional[int] = None
        self.roll_event = asyncio.Event()
        self.bot_throw = False
        self.started_at: Optional[datetime] = None

        self.task: Optional[asyncio.Task] = None
        self.lobby_task: Optional[asyncio.Task] = None
        self.on_close: Optional[Callable[[], Awaitable[None]]] = None

    # ------------------------------------------------------------- properties
    @property
    def emoji(self) -> str:
        return GAMES[self.game][0]

    @property
    def game_name(self) -> str:
        return GAMES[self.game][1]

    @property
    def mode_label(self) -> str:
        return f"{MODES[self.mode][0]} {MODES[self.mode][1]}"

    def config_line(self) -> str:
        return (f"{self.mode_label} ({MODES[self.mode][2]}) · {self.rolls} roll(s) each · "
                f"first to {self.target_wins}")

    # ------------------------------------------------------------- telegram helpers
    async def _call(self, method, *args, **kwargs):
        for _ in range(5):
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        raise RuntimeError("Flood control: too many retries")

    async def send(self, text: str, **kw):
        if self.thread_id:
            kw.setdefault("message_thread_id", self.thread_id)
        return await self._call(self.bot.send_message, self.chat_id, text, **kw)

    async def edit(self, msg_id: Optional[int], text: str, reply_markup=None) -> None:
        if not msg_id:
            return
        try:
            await self._call(self.bot.edit_message_text, text, chat_id=self.chat_id,
                             message_id=msg_id, reply_markup=reply_markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.debug("edit failed: %s", e)

    async def delete(self, msg_id: Optional[int]) -> None:
        if not msg_id:
            return
        try:
            await self.bot.delete_message(self.chat_id, msg_id)
        except (TelegramBadRequest, TelegramForbiddenError):
            pass

    # ------------------------------------------------------------- lobby
    def lobby_text(self) -> str:
        lines = [
            f"{self.emoji} <b>{self.game_name} PvP — Lobby</b>",
            f"⚙️ {self.config_line()}",
            "",
            f"👥 <b>Players ({len(self.players)}/{MAX_PLAYERS}):</b>",
        ]
        lines += [f"• {mention(p.id, p.name)}" for p in self.players.values()]
        if self.invited:
            lines.append("")
            lines.append("⏳ <b>Waiting for:</b> " + ", ".join(self.invited.values()))
        lines += [
            "",
            "➕ <b>Invite players</b> (creator only):",
            "• reply to someone's message with /invite (they get a private message only they can see)",
            "• or <code>/invite @username</code>",
            f"Need at least {MIN_PLAYERS} players, max {MAX_PLAYERS}. Then press ▶️ Start.",
        ]
        return "\n".join(lines)

    async def refresh_lobby(self) -> None:
        if self.status == "lobby":
            await self.edit(self.lobby_msg_id, self.lobby_text(), lobby_markup())

    def can_invite(self, target: str, uid: Optional[int] = None) -> Optional[str]:
        if self.status != "lobby":
            return "lobby is closed"
        if uid is not None and uid in self.players:
            return "already in the match"
        if target in self.invited:
            return "already invited"
        if len(self.players) + len(self.invited) >= MAX_PLAYERS:
            return f"all {MAX_PLAYERS} slots are taken/reserved"
        return None

    def accept(self, uid: int, name: str, target: str) -> Optional[str]:
        if uid in self.players:
            return "You're already in! ✅"
        if target not in self.invited:
            return "You have no pending invite."
        if len(self.players) >= MAX_PLAYERS:
            return f"The match is full ({MAX_PLAYERS}/{MAX_PLAYERS})."
        self.invited.pop(target)
        self.players[uid] = Player(uid, name)
        return None

    def start_lobby_timer(self) -> None:
        self.lobby_task = asyncio.create_task(self._lobby_expiry())

    async def _lobby_expiry(self) -> None:
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            if self.status == "lobby":
                await self.edit(self.lobby_msg_id, "⌛ Lobby expired — nobody started the match. Use /pvp to try again.")
                await manager.close(self)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lobby expiry failed")

    def start(self) -> None:
        self.status = "running"
        if self.lobby_task and not self.lobby_task.done():
            self.lobby_task.cancel()
        self.task = asyncio.create_task(self._run())

    # ------------------------------------------------------------- invites (private when possible)
    async def send_invite(self, target: str, uid: Optional[int]) -> str:
        """Send invite. Returns 'private' (ephemeral, only the target sees it) or 'public'."""
        await self.set_invite_result(target, None)  # drop an older invite for a resend
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Join", callback_data=InviteCB(action="yes", chat_id=self.chat_id, target=target).pack()),
            InlineKeyboardButton(text="❌ Decline", callback_data=InviteCB(action="no", chat_id=self.chat_id, target=target).pack()),
        ]])
        text = (f"👋 {self.invited[target]}, {mention(self.creator_id, self.creator_name)} invited you to a "
                f"{self.emoji} <b>{self.game_name}</b> PvP match!\n⚙️ {self.config_line()}")
        if uid is not None and EphemeralMessageParameters is not None:
            try:
                m = await self.send(text, reply_markup=kb,
                                    ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=uid))
                self.invite_msgs[target] = ("eph", getattr(m, "ephemeral_message_id", None), uid)
                return "private"
            except Exception as e:
                log.warning("Ephemeral invite failed (%s) - falling back to a public invite", e)
        m = await self.send(text, reply_markup=kb)
        self.invite_msgs[target] = ("pub", m.message_id)
        return "public"

    async def set_invite_result(self, target: str, text: Optional[str]) -> None:
        """Replace the invite message with `text` (or just delete it when text is None)."""
        ref = self.invite_msgs.pop(target, None)
        if not ref:
            return
        try:
            if ref[0] == "eph":
                if ref[1] is None:
                    return
                if text is None:
                    await self.bot.delete_ephemeral_message(
                        chat_id=self.chat_id, receiver_user_id=ref[2], ephemeral_message_id=ref[1])
                else:
                    await self.bot.edit_ephemeral_message_text(
                        chat_id=self.chat_id, receiver_user_id=ref[2], ephemeral_message_id=ref[1],
                        text=text, parse_mode="HTML")
            elif text is None:
                await self.delete(ref[1])
            else:
                await self.edit(ref[1], text)
        except Exception as e:
            log.debug("set_invite_result failed: %s", e)

    # ------------------------------------------------------------- match loop
    async def _run(self) -> None:
        try:
            await self._play()
        except asyncio.CancelledError:
            raise
        except TelegramForbiddenError:
            log.warning("Bot lost access to chat %s", self.chat_id)
        except Exception:
            log.exception("Match crashed in chat %s", self.chat_id)
            try:
                await self.send("⚠️ Something went wrong — match aborted. No stats were recorded.")
            except Exception:
                pass
        finally:
            await manager.close(self)

    async def _play(self) -> None:
        self.started_at = datetime.now(timezone.utc)
        names = ", ".join(esc(p.name) for p in self.players.values())
        await self.edit(self.lobby_msg_id,
                        f"🚀 <b>Match started!</b>\n{self.emoji} {self.game_name} · {self.config_line()}\n👥 {names}\n"
                        f"🆔 <code>{self.game_id}</code>")
        for target in list(self.invite_msgs):
            await self.set_invite_result(target, "⌛ Invite closed — the match has started.")

        while True:
            self.round_no += 1
            self.participants = list(self.players.values())
            tie = 0
            while True:
                for p in self.players.values():
                    p.round_rolls, p.done = [], False
                self.board_header = f"Round {self.round_no}" + (f" · Tie-break #{tie}" if tie else "")
                for p in list(self.participants):
                    await self._take_turn(p)

                winners = self._round_winners()
                if len(winners) == 1:
                    break
                tie += 1
                self.participants = winners
                self.last_note = ("🤝 Tie between " + ", ".join(esc(w.name) for w in winners)
                                  + " — tie-break throw!")
                await self._update_board()
                await asyncio.sleep(3)

            winner = winners[0]
            winner.series_wins += 1
            if winner.series_wins >= self.target_wins:
                self.last_note = f"🏆 {esc(winner.name)} takes round {self.round_no} and the match!"
                await self._update_board()
                await self._finish(winner)
                return
            self.last_note = f"✅ Round {self.round_no} → <b>{esc(winner.name)}</b>"
            await self._update_board()
            await asyncio.sleep(5)

    def _round_winners(self) -> list[Player]:
        totals = {p.id: sum(p.round_rolls) for p in self.participants}
        best = (min if self.mode == "crazy" else max)(totals.values())
        return [p for p in self.participants if totals[p.id] == best]

    def submit_roll(self, uid: int, emoji: str, value: int, msg_date) -> bool:
        """Called when a player sends their own dice emoji in the group."""
        if self.status != "running" or self.current_turn != uid:
            return False
        if emoji.replace("\ufe0f", "") != self.emoji.replace("\ufe0f", ""):
            return False
        if self.turn_date is not None and msg_date < self.turn_date:
            return False  # sent before the turn started
        p = self.players.get(uid)
        if p is None or len(p.round_rolls) >= self.rolls:
            return False
        p.round_rolls.append(value)
        p.total_points += value
        self.roll_event.set()
        return True

    def request_bot_throw(self) -> None:
        self.bot_throw = True
        self.roll_event.set()

    async def _take_turn(self, p: Player) -> None:
        self.current_turn = p.id
        self.bot_throw = False
        self.roll_event.clear()
        who = mention(p.id, p.name)
        kb = throw_markup()
        # The live match message doubles as the turn prompt (re-posted at the bottom each turn).
        await self._update_board(
            f"▶️ {who} to roll — send {self.emoji} ×{self.rolls}\n"
            f"⏰ {TURN_TIMEOUT}s, or I'll throw for you", kb, repost=True)
        self.turn_msg_id = self.board_msg_id
        self.turn_date = self.board_date

        auto = False
        while len(p.round_rolls) < self.rolls and not self.bot_throw:
            self.roll_event.clear()
            try:
                await asyncio.wait_for(self.roll_event.wait(), TURN_TIMEOUT)
            except asyncio.TimeoutError:
                auto = True
                break
            if not self.bot_throw and p.round_rolls:
                await asyncio.sleep(3.5)  # let the player's dice animation finish (no spoilers)
                left = self.rolls - len(p.round_rolls)
                await self._update_board(
                    f"▶️ {esc(p.name)} — {len(p.round_rolls)}/{self.rolls} thrown"
                    + (f", send {self.emoji} ×{left} more" if left > 0 else ""), kb)
        self.current_turn = None

        remaining = self.rolls - len(p.round_rolls)
        if remaining > 0:
            await self._update_board(f"🤖 Throwing {remaining}× for {esc(p.name)}" + (" ⏰" if auto else "") + "…")
            kw = {"message_thread_id": self.thread_id} if self.thread_id else {}
            for _ in range(remaining):
                dm = await self._call(self.bot.send_dice, self.chat_id, emoji=self.emoji, **kw)
                p.round_rolls.append(dm.dice.value)
                p.total_points += dm.dice.value
                await asyncio.sleep(ANIM_DELAY)
                await self._update_board(f"🤖 Throwing for {esc(p.name)}…")
        else:
            await asyncio.sleep(ANIM_DELAY)
        p.done = True
        await self._update_board()

    # ------------------------------------------------------------- live match message
    def _score_str(self, p: Player) -> str:
        if all(p is not q for q in self.participants):
            return "— (out)"
        if not p.round_rolls:
            return "⏳"
        return f"<b>{sum(p.round_rolls)}</b> ({'+'.join(map(str, p.round_rolls))})" + ("" if p.done else " …")

    def board_text(self, prompt: str = "") -> str:
        standings = "\n".join(f"{esc(p.name)} - {p.series_wins}"
                              for p in sorted(self.players.values(), key=lambda q: -q.series_wins))
        lines = [
            f"{self.emoji} <b>{self.game_name}</b> · {self.mode_label} · {self.rolls} roll(s) · first to <b>{self.target_wins}</b>",
            "",
            f"📋 <b>{self.board_header}</b>",
        ]
        lines += [f"• {esc(p.name)}: {self._score_str(p)}" for p in self.players.values()]
        lines += ["", f"<blockquote>{standings}</blockquote>"]
        if self.last_note:
            lines += ["", self.last_note]
        if prompt:
            lines += ["", prompt]
        return "\n".join(lines)

    async def _update_board(self, prompt: str = "", kb=None, repost: bool = False) -> None:
        text = self.board_text(prompt)
        if repost or self.board_msg_id is None:
            old = self.board_msg_id
            msg = await self.send(text, reply_markup=kb)
            self.board_msg_id, self.board_date = msg.message_id, msg.date
            await self.delete(old)
        else:
            await self.edit(self.board_msg_id, text, kb)

    # ------------------------------------------------------------- finish
    async def _finish(self, winner: Player) -> None:
        crazy = self.mode == "crazy"
        ranking = sorted(self.players.values(),
                         key=lambda p: (-p.series_wins, p.total_points if crazy else -p.total_points))
        saved = True
        try:
            await database.record_match(
                chat_id=self.chat_id, chat_title=self.chat_title, game=self.game, mode=self.mode,
                rolls=self.rolls, target_wins=self.target_wins, started_at=self.started_at,
                winner_id=winner.id, winner_name=winner.name, game_id=self.game_id,
                players=[database.PlayerResult(p.id, p.name, p.series_wins, p.total_points) for p in ranking],
            )
        except Exception:
            saved = False
            log.exception("Failed to save match")

        board = "\n".join(f"{MEDALS[i] if i < 3 else '▫️'} {esc(p.name)} - {p.series_wins}  ({p.total_points} pts)"
                          for i, p in enumerate(ranking))
        text = (f"🏆 <b>{mention(winner.id, winner.name)} won!</b>\n"
                f"<blockquote>{board}</blockquote>\n"
                f"{self.emoji} {self.game_name} · {self.mode_label} · first to {self.target_wins}\n"
                f"🆔 <code>{self.game_id}</code>\n\n"
                f"<i>“{random.choice(WIN_QUOTES)}”</i>")
        if not saved:
            text += "\n\n⚠️ Could not save stats (see server logs)."
        manager.remember(self)
        await self.send(text, reply_markup=rematch_markup())


class SessionManager:
    """Holds one session per chat. The dict key is the chat id => no cross-group clashes."""

    def __init__(self) -> None:
        self.sessions: dict[int, GameSession] = {}
        self.last_match: dict[int, dict] = {}   # for the Rematch button (RAM only)
        self._setups: dict[int, tuple[int, float]] = {}

    def get(self, chat_id: int) -> Optional[GameSession]:
        return self.sessions.get(chat_id)

    def remember(self, s: GameSession) -> None:
        self.last_match[s.chat_id] = {
            "game": s.game, "mode": s.mode, "rolls": s.rolls, "wins": s.target_wins,
            "players": [(p.id, p.name) for p in s.players.values()],
            "thread_id": s.thread_id, "title": s.chat_title,
        }
        while len(self.last_match) > 500:
            self.last_match.pop(next(iter(self.last_match)))

    # setup-wizard reservation (one wizard per chat at a time)
    def reserve_setup(self, chat_id: int, user_id: int) -> bool:
        cur, now = self._setups.get(chat_id), time.monotonic()
        if cur and cur[0] != user_id and now - cur[1] < SETUP_TIMEOUT:
            return False
        self._setups[chat_id] = (user_id, now)
        return True

    def setup_owner(self, chat_id: int) -> Optional[int]:
        cur = self._setups.get(chat_id)
        return cur[0] if cur and time.monotonic() - cur[1] < SETUP_TIMEOUT else None

    def release_setup(self, chat_id: int) -> None:
        self._setups.pop(chat_id, None)

    def add(self, session: GameSession) -> None:
        self.sessions[session.chat_id] = session
        self._setups.pop(session.chat_id, None)

    async def close(self, session: GameSession) -> None:
        if self.sessions.get(session.chat_id) is not session:
            return
        del self.sessions[session.chat_id]
        session.status = "closed"
        me = asyncio.current_task()
        for t in (session.lobby_task, session.task):
            if t and t is not me and not t.done():
                t.cancel()
        if session.on_close:
            try:
                await session.on_close()
            except Exception:
                log.exception("on_close failed")

    async def shutdown(self, bot: Bot) -> None:
        for s in list(self.sessions.values()):
            try:
                await s.send("🔄 Bot is restarting — the current match was cancelled. "
                             "Your saved stats are safe. Use /pvp to start again.")
            except Exception:
                pass
            await self.close(s)


manager = SessionManager()
