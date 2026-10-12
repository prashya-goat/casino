"""Game engine (event driven) + per-chat session manager.

* One GameSession per group chat (keyed by chat_id) -> groups never share state.
* Players send the game emoji themselves; every dice is counted the moment it arrives.
* Works with the bot as a normal member too ("limited mode"): Telegram only shows such a bot
  replies to its own messages, so players reply to the bot's message with the emoji or press
  the "Throw for me" button.
* The full match state is saved to MongoDB after every roll, so a redeploy/restart
  resumes the match exactly where it stopped (see SessionManager.restore_all).
* A started match cannot be stopped.  Stakes are virtual (stats only) - no real money.
"""
import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
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
LOBBY_TIMEOUT = 300    # seconds before an unstarted lobby expires
SETUP_TIMEOUT = 300    # seconds before an abandoned setup wizard frees the chat
TURN_TIMEOUT = 60      # seconds without a throw before the bot throws for the player
WARN_AT = 30           # "you have 30 sec to roll" appears when this many seconds are left
FINAL_AT = 10          # ...and the same message is edited when this many seconds are left
DICE_ANIM = 3.5        # let the dice animation finish before announcing a bot/button throw
BOT_ROLL_GAP = 1.5     # pause between the bot's own throws (flood-control friendly)
WARN_COOLDOWN = 10     # max one "wrong turn" warning per player per N seconds
# Abandoned matches: nobody throws while the bot keeps auto-throwing -> the match is dropped (no stats).
AUTO_TURN_LIMIT = int(os.getenv("AUTO_TURN_LIMIT", "5"))
# After a restart, only matches with a real player throw this recent are resumed; older ones are deleted.
RESUME_WINDOW = int(os.getenv("RESUME_WINDOW_MINUTES", "30")) * 60
VS = "️"

GAMES = {
    "dice": ("🎲", "Dice"),
    "basketball": ("🏀", "Basketball"),
    "football": ("⚽", "Football"),
    "bowling": ("🎳", "Bowling"),
    "darts": ("🎯", "Darts"),
}
MODES = {
    "normal": ("🟢", "Normal", "highest total wins"),
    "crazy": ("🔴", "Crazy", "lowest total wins"),
}
MEDALS = ["🥇", "🥈", "🥉"]
# Virtual stake per player in cents (0 = free play). Nothing real is ever charged.
STAKES = [0, 25, 50, 100, 200, 500, 1000, 2500, 5000, 10000]

_bg: set = set()   # keeps fire-and-forget tasks alive


def esc(s: str) -> str:
    return html.escape(s)


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def money(cents: int) -> str:
    c = abs(int(cents))
    return f"${c // 100}.{c % 100:02d}"


def money_signed(cents: int) -> str:
    return ("+" if cents > 0 else "−" if cents < 0 else "") + money(cents)


def btn(text: str, data: str, style: Optional[str] = None) -> InlineKeyboardButton:
    """Inline button. style: 'primary' (blue) | 'success' (green) | 'danger' (red)."""
    extra = {"style": style} if style else {}
    return InlineKeyboardButton(text=text, callback_data=data, **extra)


def lobby_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        btn("▶️ Start match", "lobby:start", "success"),
        btn("✖️ Cancel", "lobby:cancel", "danger"),
    ]])


class InviteCB(CallbackData, prefix="inv"):
    action: str   # yes | no
    chat_id: int
    user_id: int  # the ONLY user this invite is for


@dataclass(eq=False)
class Player:
    id: int
    name: str
    series_wins: int = 0
    total_points: int = 0
    round_rolls: list = field(default_factory=list)


class GameSession:
    def __init__(self, bot: Bot, chat_id: int, thread_id: Optional[int], chat_title: str,
                 creator_id: int, creator_name: str, game: str, mode: str, rolls: int, target_wins: int,
                 stake_cents: int = 0):
        self.bot, self.chat_id, self.thread_id, self.chat_title = bot, chat_id, thread_id, chat_title
        self.creator_id, self.creator_name = creator_id, creator_name
        self.game, self.mode, self.rolls, self.target_wins = game, mode, rolls, target_wins
        self.stake_cents = stake_cents

        self.players: dict[int, Player] = {creator_id: Player(creator_id, creator_name)}  # join order
        self.invited: dict[int, str] = {}      # pending invites: user id -> name
        self.invites: dict[int, dict] = {}     # user id -> ephemeral message ids

        self.status = "lobby"                  # lobby -> running -> finished/closed
        self.limited = False                   # True = bot is not admin -> only sees replies/commands
        self.lobby_msg_id: Optional[int] = None
        self.started_at: Optional[datetime] = None

        # match state (all of it is persisted)
        self.round_no = 0
        self.turn_idx = 0
        self.tiebreaks = 0
        self.participants: list[Player] = []

        self.turn_started: Optional[datetime] = None
        self.last_activity = datetime.now(timezone.utc)   # last throw made by a REAL player
        self.auto_streak = 0                              # consecutive turns thrown by the bot
        self.lock = asyncio.Lock()
        self.warned: dict[int, float] = {}
        self.warn_msg: Optional[int] = None               # the "30 sec left" message
        self.timer: Optional[asyncio.Task] = None
        self.timer_token = 0
        self._saver: Optional[asyncio.Task] = None
        self._dirty = False
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

    @property
    def stake_label(self) -> str:
        return f"💰 {money(self.stake_cents)} each" if self.stake_cents else "🆓 Free play"

    def config_line(self) -> str:
        return (f"{self.mode_label} ({MODES[self.mode][2]}) · {self.rolls} roll(s) each · "
                f"first to {self.target_wins} · {self.stake_label}")

    def current_player(self) -> Player:
        return self.participants[self.turn_idx]

    def scores_block(self, title: str = "Scores:") -> str:
        lines = [f"<b>{title}</b>"] + [f"👤 {esc(p.name)} • {p.series_wins}" for p in self.players.values()]
        return "\n".join(lines)

    def round_block(self) -> str:
        title = "Tie-break:" if self.tiebreaks else "This round:"
        lines = [f"<b>{title}</b>"]
        for p in self.participants:
            tot = sum(p.round_rolls)
            detail = f"{' · '.join(map(str, p.round_rolls))} = {tot}" if len(p.round_rolls) > 1 else f"{tot}"
            lines.append(f"👤 {esc(p.name)} • {detail}")
        return "\n".join(lines)

    def prompt(self) -> str:
        p = self.current_player()
        return f"{mention(p.id, p.name)} to roll — send {self.emoji} ×{self.rolls - len(p.round_rolls)}"

    def turn_markup(self) -> Optional[InlineKeyboardMarkup]:
        if not self.limited:
            return None
        return InlineKeyboardMarkup(inline_keyboard=[[btn(f"{self.emoji} Throw for me", "throw", "primary")]])

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

    async def _safe_delete(self, msg_id: Optional[int]) -> None:
        if not msg_id:
            return
        try:
            await self.bot.delete_message(self.chat_id, msg_id)
        except Exception as e:
            log.debug("delete failed: %s", e)

    def _clear_warn(self) -> None:
        """Remove the '30 sec left' message (the player threw / the turn moved on)."""
        mid, self.warn_msg = self.warn_msg, None
        if mid:
            t = asyncio.create_task(self._safe_delete(mid))
            _bg.add(t)
            t.add_done_callback(_bg.discard)

    async def refresh_limited(self) -> None:
        """Not an admin => Telegram (privacy mode) only shows the bot commands and replies to its messages."""
        try:
            me = await self.bot.me()
            m = await self.bot.get_chat_member(self.chat_id, me.id)
            self.limited = m.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
        except Exception as e:
            log.debug("admin check failed: %s", e)
            self.limited = True

    # ---- ephemeral ("only visible to you") messages
    async def _eph_send(self, uid: int, text: str, kb=None) -> Optional[int]:
        if EphemeralMessageParameters is None:
            raise RuntimeError("aiogram >= 3.31 is required for private invites")
        m = await self.send(text, reply_markup=kb,
                            ephemeral_message_parameters=EphemeralMessageParameters(receiver_user_id=uid))
        return getattr(m, "ephemeral_message_id", None)

    async def _eph_edit(self, uid: int, eid: Optional[int], text: str) -> None:
        if eid is None:
            return
        try:
            await self.bot.edit_ephemeral_message_text(
                chat_id=self.chat_id, receiver_user_id=uid, ephemeral_message_id=eid,
                text=text, parse_mode="HTML")
        except Exception as e:
            log.debug("ephemeral edit failed: %s", e)

    async def _eph_delete(self, uid: int, eid: Optional[int]) -> None:
        if eid is None:
            return
        try:
            await self.bot.delete_ephemeral_message(
                chat_id=self.chat_id, receiver_user_id=uid, ephemeral_message_id=eid)
        except Exception as e:
            log.debug("ephemeral delete failed: %s", e)

    async def tell_creator(self, text: str) -> None:
        """Notice visible only to the creator (falls back to a plain reply if ephemeral is unavailable)."""
        try:
            await self._eph_send(self.creator_id, text)
        except Exception as e:
            log.warning("tell_creator via ephemeral failed (%s)", e)
            try:
                await self.send(text)
            except Exception:
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
            lines.append(f"⏳ {len(self.invited)} invite(s) pending")
        lines.append("")
        if self.limited:
            lines += [
                "➕ <b>Invite</b> (creator only): <code>/invite @username</code>, or reply to a player with /invite.",
                "ℹ️ I'm not an admin here, so I only see commands and replies to my messages. "
                "Players will reply to my message with the emoji (or press a button). "
                "Make me admin for the normal, faster mode.",
            ]
        else:
            lines += [
                "➕ <b>Invite</b> (creator only): just type <code>@username</code> in this chat — "
                "or reply to a player with /invite.",
            ]
        lines += [
            "Only the invited player (and you) can see the invite.",
            f"Need {MIN_PLAYERS}–{MAX_PLAYERS} players, then press ▶️ Start. "
            "<b>A started match cannot be stopped.</b>",
        ]
        return "\n".join(lines)

    async def refresh_lobby(self) -> None:
        if self.status == "lobby":
            await self.edit(self.lobby_msg_id, self.lobby_text(), lobby_markup())

    def can_invite(self, uid: int) -> Optional[str]:
        if self.status != "lobby":
            return "the lobby is closed"
        if uid in self.players:
            return "already in the match"
        if len(self.players) + len(self.invited) >= MAX_PLAYERS:
            return f"all {MAX_PLAYERS} slots are taken/reserved"
        return None

    def accept(self, uid: int, name: str) -> Optional[str]:
        if uid in self.players:
            return "You're already in! ✅"
        if uid not in self.invited:
            return "You have no pending invite."
        if len(self.players) >= MAX_PLAYERS:
            return f"The match is full ({MAX_PLAYERS}/{MAX_PLAYERS})."
        self.invited.pop(uid)
        self.players[uid] = Player(uid, name)
        return None

    async def send_invite(self, uid: int, name: str) -> Optional[str]:
        """Private invite (only `uid` sees it) + a small private status note for the creator.
        Returns None on success, or a short error text."""
        await self.drop_invite(uid)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            btn("✅ Join", InviteCB(action="yes", chat_id=self.chat_id, user_id=uid).pack(), "success"),
            btn("❌ Decline", InviteCB(action="no", chat_id=self.chat_id, user_id=uid).pack(), "danger"),
        ]])
        text = (f"👋 {mention(uid, name)}, {mention(self.creator_id, self.creator_name)} invited you to a "
                f"{self.emoji} <b>{self.game_name}</b> PvP match!\n⚙️ {self.config_line()}")
        try:
            target_eid = await self._eph_send(uid, text, kb)
        except Exception as e:
            log.warning("private invite to %s failed: %s", uid, e)
            return f"{esc(name)}: couldn't deliver the private invite"
        creator_eid = None
        try:
            creator_eid = await self._eph_send(self.creator_id, f"📩 Invite sent to {esc(name)} — waiting for a reply…")
        except Exception:
            pass
        self.invites[uid] = {"name": name, "target": target_eid, "creator": creator_eid}
        return None

    async def drop_invite(self, uid: int) -> None:
        ref = self.invites.pop(uid, None)
        if ref:
            await self._eph_delete(uid, ref.get("target"))
            await self._eph_delete(self.creator_id, ref.get("creator"))

    async def resolve_invite(self, uid: int, target_text: str, creator_text: str) -> None:
        self.invited.pop(uid, None)
        ref = self.invites.pop(uid, None)
        if ref:
            await self._eph_edit(uid, ref.get("target"), target_text)
            await self._eph_edit(self.creator_id, ref.get("creator"), creator_text)

    async def close_invites(self, text: str) -> None:
        for uid in list(self.invites):
            name = self.invites[uid]["name"]
            await self.resolve_invite(uid, text, f"⌛ Invite to {esc(name)} closed.")
        self.invited.clear()

    def start_lobby_timer(self) -> None:
        self.lobby_task = asyncio.create_task(self._lobby_expiry())

    async def _lobby_expiry(self) -> None:
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            if self.status == "lobby":
                await self.edit(self.lobby_msg_id, "⌛ Lobby expired — nobody started the match. Use /pvp to try again.")
                await self.close_invites("⌛ This lobby expired.")
                await manager.close(self)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("lobby expiry failed")

    # ------------------------------------------------------------- starting
    async def start(self) -> None:
        if self.status != "lobby":  # (sync check first: guards against double clicks)
            return
        self.status = "running"
        if self.lobby_task and not self.lobby_task.done():
            self.lobby_task.cancel()
        self.started_at = self.last_activity = datetime.now(timezone.utc)
        self.round_no, self.tiebreaks, self.turn_idx = 1, 0, 0
        self.participants = list(self.players.values())
        for p in self.players.values():
            p.round_rolls = []
        await self.refresh_limited()
        names = ", ".join(esc(p.name) for p in self.players.values())
        await self.close_invites("⌛ Invite closed — the match has started.")
        await self.edit(self.lobby_msg_id,
                        f"🚀 <b>Match started!</b>\n{self.emoji} {self.game_name} · {self.config_line()}\n👥 {names}")
        async with self.lock:
            await self._announce()

    # ------------------------------------------------------------- dice handling (the fast path)
    async def handle_dice(self, uid: int, emoji: str, value: int, msg_date) -> None:
        if self.status != "running" or uid not in self.players:
            return
        async with self.lock:
            if self.status != "running":
                return
            cur = self.current_player()
            if uid != cur.id:
                return await self._warn(uid, f"⛔️ {esc(self.players[uid].name)}, it's {esc(cur.name)}'s turn.")
            if emoji.replace(VS, "") != self.emoji.replace(VS, ""):
                return await self._warn(uid, f"⚠️ {esc(cur.name)}, send {self.emoji} (not {emoji}).")
            if self.turn_started is not None and msg_date < self.turn_started:
                return  # thrown before this turn began
            if len(cur.round_rolls) >= self.rolls:
                return
            self.last_activity, self.auto_streak = datetime.now(timezone.utc), 0   # a real player is here
            await self._apply_roll(cur, value)

    async def throw_button(self, uid: int) -> Optional[str]:
        """'Throw for me' button: the bot throws the dice on behalf of the player whose turn it is.
        Returns an error text (shown as a popup) or None."""
        if self.status != "running":
            return "No match is running."
        if uid not in self.players:
            return "You're not in this match ❌"
        async with self.lock:
            if self.status != "running":
                return "No match is running."
            cur = self.current_player()
            if uid != cur.id:
                return f"It's {cur.name}'s turn ❌"
            if len(cur.round_rolls) >= self.rolls:
                return None
            self.last_activity, self.auto_streak = datetime.now(timezone.utc), 0
            kw = {"message_thread_id": self.thread_id} if self.thread_id else {}
            dm = await self._call(self.bot.send_dice, self.chat_id, emoji=self.emoji, **kw)
            await asyncio.sleep(DICE_ANIM)
            if self.status == "running" and self.current_player() is cur:
                await self._apply_roll(cur, dm.dice.value)
        return None

    async def _warn(self, uid: int, text: str) -> None:
        now = time.monotonic()
        if now - self.warned.get(uid, 0) < WARN_COOLDOWN:
            return
        self.warned[uid] = now
        try:
            await self.send(text)
        except Exception:
            pass

    async def _apply_roll(self, p: Player, value: int, auto: bool = False) -> None:
        p.round_rolls.append(value)
        p.total_points += value
        self.persist()
        if len(p.round_rolls) >= self.rolls:
            await self._complete_turn(p, auto)
        else:
            self._arm_timer()

    async def _complete_turn(self, p: Player, auto: bool) -> None:
        line = f"{self.emoji} {esc(p.name)} rolled <b>{sum(p.round_rolls)}</b>"
        if self.rolls > 1:
            line += " (" + " + ".join(map(str, p.round_rolls)) + ")"
        if auto:
            line += " ⏰"

        self.turn_idx += 1
        if self.turn_idx < len(self.participants):
            return await self._announce(line)

        # ---- everyone has thrown: decide the round
        block = self.round_block()
        winners = self._round_winners()
        if len(winners) > 1:  # tie -> tie-break between the tied players only
            names = ", ".join(esc(w.name) for w in winners)
            self.tiebreaks += 1
            self.participants, self.turn_idx = winners, 0
            for w in winners:
                w.round_rolls = []
            head = f"🤝 <b>Round {self.round_no} is tied!</b>\n\n{block}\n\nTied: {names}"
            return await self._announce(head, label="➡️ Tie-break: ", blank=True)

        w = winners[0]
        w.series_wins += 1
        if w.series_wins >= self.target_wins:
            return await self._finish(w, block)
        head = f"🏆 <b>{esc(w.name)} wins this round!</b>\n\n{block}\n\n{self.scores_block()}"
        self.round_no += 1
        self.tiebreaks, self.turn_idx = 0, 0
        self.participants = list(self.players.values())  # same order every round
        for q in self.participants:
            q.round_rolls = []
        await self._announce(head, label="➡️ Next round: ", blank=True)

    def _round_winners(self) -> list[Player]:
        totals = {p.id: sum(p.round_rolls) for p in self.participants}
        best = (min if self.mode == "crazy" else max)(totals.values())
        return [p for p in self.participants if totals[p.id] == best]

    async def _announce(self, head: str = "", label: str = "", blank: bool = False) -> None:
        body = label + self.prompt()
        if self.limited:
            body += f"\n<i>↩️ Reply to this message with {self.emoji} — or tap the button</i>"
        text = body if not head else head + ("\n\n" if blank else "\n") + body
        self.persist()
        try:
            msg = await self.send(text, reply_markup=self.turn_markup())
            self.turn_started = msg.date
        finally:
            self._arm_timer()

    # ------------------------------------------------------------- turn timer: warn at 30s, 10s, then bot throws
    def _arm_timer(self) -> None:
        self.timer_token += 1
        self._clear_warn()
        old = self.timer
        if old and not old.done() and old is not asyncio.current_task():
            old.cancel()
        if self.status == "running":
            self.timer = asyncio.create_task(self._watchdog(self.timer_token))

    def _stop_timer(self) -> None:
        self.timer_token += 1
        self._clear_warn()
        if self.timer and not self.timer.done() and self.timer is not asyncio.current_task():
            self.timer.cancel()

    async def _watchdog(self, token: int) -> None:
        def stale() -> bool:
            return token != self.timer_token or self.status != "running"

        try:
            await asyncio.sleep(TURN_TIMEOUT - WARN_AT)
            if stale():
                return
            p = self.current_player()
            who = mention(p.id, p.name)
            msg = await self.send(f"⏳ {who} — you have <b>{WARN_AT} sec</b> to roll!")
            if stale():
                await self._safe_delete(msg.message_id)
                return
            self.warn_msg = msg.message_id

            await asyncio.sleep(WARN_AT - FINAL_AT)
            if stale():
                return
            await self.edit(self.warn_msg, f"⚠️ {who} — only <b>{FINAL_AT} sec</b> left to roll!")

            await asyncio.sleep(FINAL_AT)
            async with self.lock:
                if stale():
                    return
                wm, self.warn_msg = self.warn_msg, None
                await self._bot_throw_current(wm)
        except asyncio.CancelledError:
            raise
        except TelegramForbiddenError:
            log.warning("Bot lost access to chat %s", self.chat_id)
        except Exception:
            log.exception("watchdog failed in chat %s", self.chat_id)
            self._arm_timer()  # try again later instead of freezing the match

    async def _bot_throw_current(self, warn_id: Optional[int] = None) -> None:
        self.auto_streak += 1
        if self.auto_streak > AUTO_TURN_LIMIT:   # nobody is playing any more
            await self._safe_delete(warn_id)
            return await self._abandon()
        p = self.current_player()
        note = f"⏰ Time's up — I'll throw for {esc(p.name)}."
        if warn_id:
            await self.edit(warn_id, note)
        else:
            await self.send(note)
        kw = {"message_thread_id": self.thread_id} if self.thread_id else {}
        while self.status == "running" and self.current_player() is p and len(p.round_rolls) < self.rolls:
            dm = await self._call(self.bot.send_dice, self.chat_id, emoji=self.emoji, **kw)
            await asyncio.sleep(DICE_ANIM)
            if self.status != "running" or self.current_player() is not p:
                break
            await self._apply_roll(p, dm.dice.value, auto=True)
            if self.status == "running" and self.current_player() is p:
                await asyncio.sleep(BOT_ROLL_GAP)

    # ------------------------------------------------------------- persistence (resume after redeploy)
    def snapshot(self) -> dict:
        return {
            "_id": self.chat_id, "thread_id": self.thread_id, "chat_title": self.chat_title,
            "creator_id": self.creator_id, "creator_name": self.creator_name,
            "game": self.game, "mode": self.mode, "rolls": self.rolls, "target_wins": self.target_wins,
            "stake_cents": self.stake_cents,
            "round_no": self.round_no, "turn_idx": self.turn_idx, "tiebreaks": self.tiebreaks,
            "participants": [p.id for p in self.participants],
            "players": [{"id": p.id, "name": p.name, "series_wins": p.series_wins,
                         "total_points": p.total_points, "round_rolls": list(p.round_rolls)}
                        for p in self.players.values()],
            "started_at": self.started_at, "last_activity": self.last_activity,
            "updated_at": datetime.now(timezone.utc),
        }

    @classmethod
    def from_snapshot(cls, bot: Bot, s: dict) -> "GameSession":
        obj = cls(bot, s["_id"], s.get("thread_id"), s.get("chat_title", ""), s["creator_id"],
                  s["creator_name"], s["game"], s["mode"], s["rolls"], s["target_wins"],
                  s.get("stake_cents", 0))
        obj.players = {pl["id"]: Player(pl["id"], pl["name"], pl["series_wins"], pl["total_points"],
                                        list(pl["round_rolls"])) for pl in s["players"]}
        obj.participants = [obj.players[i] for i in s["participants"]]
        obj.round_no, obj.turn_idx, obj.tiebreaks = s["round_no"], s["turn_idx"], s.get("tiebreaks", 0)
        obj.started_at = s.get("started_at")
        la = s.get("last_activity")
        if la is not None:
            obj.last_activity = la if la.tzinfo else la.replace(tzinfo=timezone.utc)
        obj.status = "running"
        return obj

    def persist(self) -> None:
        """Save the state in the background (coalesced) - never slows the dice path down."""
        if self.status != "running":
            return
        self._dirty = True
        if self._saver is None or self._saver.done():
            self._saver = asyncio.create_task(self._save_loop())

    async def _save_loop(self) -> None:
        while self._dirty and self.status == "running":
            self._dirty = False
            try:
                await database.save_active(self.snapshot())
            except Exception:
                log.exception("saving match state failed")
                await asyncio.sleep(2)
                self._dirty = True

    async def resume(self) -> None:
        await self.refresh_limited()
        async with self.lock:
            await self._announce(f"🔄 <b>Bot is back — match resumed</b> · Round {self.round_no}\n\n{self.scores_block()}",
                                 blank=True)

    async def _abandon(self) -> None:
        """Nobody has played for a while: drop the match (no stats) instead of auto-playing it forever."""
        self.status = "abandoned"
        self._stop_timer()
        if self._saver and not self._saver.done():
            try:
                await self._saver
            except Exception:
                pass
        try:
            await database.delete_active(self.chat_id)
        except Exception:
            log.exception("delete_active failed")
        try:
            await self.send("⌛ Nobody played for a while, so this match was abandoned (no stats recorded). "
                            "Use /pvp to start a new one.")
        except Exception:
            pass
        finally:
            await manager.close(self)

    # ------------------------------------------------------------- finish
    async def _finish(self, winner: Player, block: str) -> None:
        self.status = "finished"
        self._stop_timer()
        crazy = self.mode == "crazy"
        ranking = sorted(self.players.values(),
                         key=lambda p: (-p.series_wins, p.total_points if crazy else -p.total_points))
        n, stake = len(ranking), self.stake_cents
        profit = stake * (n - 1)
        nets = {p.id: (profit if p.id == winner.id else -stake) for p in ranking}

        saved = False
        for _ in range(3):
            try:
                await database.record_match(
                    chat_id=self.chat_id, chat_title=self.chat_title, game=self.game, mode=self.mode,
                    rolls=self.rolls, target_wins=self.target_wins, stake_cents=stake,
                    started_at=self.started_at, winner_id=winner.id, winner_name=winner.name,
                    players=[database.PlayerResult(p.id, p.name, p.series_wins, p.total_points, nets[p.id])
                             for p in ranking])
                saved = True
                break
            except Exception:
                log.exception("record_match failed")
                await asyncio.sleep(1)
        if self._saver and not self._saver.done():
            try:
                await self._saver
            except Exception:
                pass
        try:
            await database.delete_active(self.chat_id)
        except Exception:
            log.exception("delete_active failed")

        lines = [f"🏆 <b>{esc(winner.name)} wins this round!</b>", "", block, "", "<b>Final scores:</b>"]
        lines += [f"{MEDALS[i] if i < 3 else '▫️'} {esc(p.name)} • {p.series_wins}" for i, p in enumerate(ranking)]
        losers = [esc(p.name) for p in ranking if p.id != winner.id]
        lines.append("")
        if stake:
            lines.append(f"🎉 <b>{esc(winner.name)}</b> takes the game — wins <b>{money(profit)}</b>!")
            lines.append(f"💸 {', '.join(losers)} {'loses' if len(losers) == 1 else 'lose'} "
                         f"{money(stake)}{'' if len(losers) == 1 else ' each'}. Better luck next time!")
        else:
            lines.append(f"🎉 <b>{esc(winner.name)}</b> takes the game — GG! Better luck next time, "
                         f"{', '.join(losers)}!")
        if not saved:
            lines += ["", "⚠️ Could not save stats (see server logs)."]
        try:
            await self.send("\n".join(lines))
        finally:
            await manager.close(self)


class SessionManager:
    """Holds one session per chat. The dict key is the chat id => no cross-group clashes."""

    def __init__(self) -> None:
        self.sessions: dict[int, GameSession] = {}
        self._setups: dict[int, tuple[int, float]] = {}

    def get(self, chat_id: int) -> Optional[GameSession]:
        return self.sessions.get(chat_id)

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
        if session.status in ("lobby", "running"):
            session.status = "closed"
        session._stop_timer()
        if session.lobby_task and not session.lobby_task.done() and session.lobby_task is not asyncio.current_task():
            session.lobby_task.cancel()
        if session.on_close:
            try:
                await session.on_close()
            except Exception:
                log.exception("on_close failed")

    async def restore_all(self, bot: Bot) -> None:
        """On start-up: continue every match that was running when the bot stopped."""
        try:
            docs = await database.load_active()
        except Exception:
            log.exception("could not load saved matches")
            return
        for snap in docs:
            chat_id = snap.get("_id")
            la = snap.get("last_activity")   # saved matches without it are old-format => treated as stale
            if la is not None and la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            if la is None or (datetime.now(timezone.utc) - la).total_seconds() > RESUME_WINDOW:
                log.info("Dropping stale saved match in chat %s (no player activity recently)", chat_id)
                try:
                    await database.delete_active(chat_id)
                except Exception:
                    log.exception("could not delete stale match")
                continue
            try:
                s = GameSession.from_snapshot(bot, snap)
                self.sessions[s.chat_id] = s
                await s.resume()
                log.info("Resumed match in chat %s", chat_id)
            except (TelegramForbiddenError, TelegramBadRequest) as e:
                log.warning("Dropping saved match for chat %s: %s", chat_id, e)
                self.sessions.pop(chat_id, None)
                await database.delete_active(chat_id)
            except Exception:
                log.exception("Could not resume match in chat %s", chat_id)
                self.sessions.pop(chat_id, None)

    async def shutdown(self, bot: Bot) -> None:
        """Graceful stop (redeploy): running matches are SAVED, not cancelled."""
        for s in list(self.sessions.values()):
            if s.status == "running":
                s._stop_timer()
                try:
                    await database.save_active(s.snapshot())
                except Exception:
                    log.exception("final save failed")
                try:
                    await s.send("🔄 Bot is restarting — your match is saved and will continue automatically.")
                except Exception:
                    pass
            elif s.status == "lobby":
                await s.edit(s.lobby_msg_id, "⚠️ Bot restarted — this lobby was closed. Use /pvp to start again.")
                await s.close_invites("⌛ This lobby was closed.")
        self.sessions.clear()


manager = SessionManager()
