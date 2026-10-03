"""Game engine: lobby, turn loop, live scoreboard and per-chat session manager.

Every group chat gets its own GameSession (keyed by chat_id) running in its own
asyncio task, so matches in different groups never share state.
"""
import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import database

log = logging.getLogger("pvp.game")

MIN_PLAYERS = 2
MAX_PLAYERS = 7
LOBBY_TIMEOUT = 300   # seconds before an unstarted lobby expires
SETUP_TIMEOUT = 300   # seconds before an abandoned setup wizard frees the chat
TURN_TIMEOUT = 45     # seconds before a player is auto-thrown for
ANIM_DELAY = 4.0      # Telegram dice animation length (also keeps us under flood limits)

GAMES = {
    "dice": ("🎲", "Dice"),
    "basketball": ("🏀", "Basketball"),
    "football": ("⚽", "Football"),
    "bowling": ("🎳", "Bowling"),
    "darts": ("🎯", "Darts"),
}
MODES = {
    "normal": ("📈", "Normal Mode", "highest total wins"),
    "crazy": ("🤪", "Crazy Mode", "lowest total wins"),
}
MEDALS = ["🥇", "🥈", "🥉"]


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'


def lobby_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="▶️ Start match", callback_data="lobby:start"),
        InlineKeyboardButton(text="✖️ Cancel", callback_data="lobby:cancel"),
    ]])


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

        self.players: dict[int, Player] = {creator_id: Player(creator_id, creator_name)}
        # target = str(user_id) or lowercase username  ->  HTML label shown in the lobby
        self.invited: dict[str, str] = {}
        self.invite_msgs: dict[str, int] = {}

        self.status = "lobby"  # lobby -> running -> closed
        self.lobby_msg_id: Optional[int] = None
        self.board_msg_id: Optional[int] = None
        self.turn_msg_id: Optional[int] = None
        self.board_header = ""
        self.last_note = ""
        self.round_no = 0
        self.participants: list[Player] = []
        self.current_turn: Optional[int] = None
        self.throw_event = asyncio.Event()
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
                f"first to {self.target_wins} series win(s)")

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
            "• reply to someone's message with /invite",
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
        self.invite_msgs.pop(target, None)
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
        names = ", ".join(mention(p.id, p.name) for p in self.players.values())
        await self.edit(self.lobby_msg_id, f"🚀 <b>Match started!</b>\n{self.emoji} {self.game_name} · {self.config_line()}\n👥 {names}")
        for uid, mid in list(self.invite_msgs.items()):
            await self.edit(mid, "⌛ Invite closed — the match has started.")

        while True:
            self.round_no += 1
            self.participants = list(self.players.values())
            tie = 0
            while True:
                for p in self.players.values():
                    p.round_rolls, p.done = [], False
                self.board_header = f"Round {self.round_no}" + (f" · Tie-break #{tie}" if tie else "")
                await self._update_board(repost=True)

                for p in self.participants:
                    await self._take_turn(p)

                winners = self._round_winners()
                if len(winners) == 1:
                    break
                tie += 1
                self.participants = winners
                self.last_note = ("🤝 Tie between " + ", ".join(mention(w.id, w.name) for w in winners)
                                  + " — tie-break throw!")
                await asyncio.sleep(3)

            winner = winners[0]
            winner.series_wins += 1
            if winner.series_wins >= self.target_wins:
                self.last_note = f"🏆 {mention(winner.id, winner.name)} wins round {self.round_no} and the match!"
                await self._update_board()
                await self._finish(winner)
                return
            self.last_note = f"✅ Round {self.round_no} → {mention(winner.id, winner.name)} (+1 series win)"
            await self._update_board()
            await asyncio.sleep(5)

    def _round_winners(self) -> list[Player]:
        totals = {p.id: sum(p.round_rolls) for p in self.participants}
        best = (min if self.mode == "crazy" else max)(totals.values())
        return [p for p in self.participants if totals[p.id] == best]

    async def _take_turn(self, p: Player) -> None:
        self.current_turn = p.id
        self.throw_event.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{self.emoji} Throw!", callback_data="throw")]])
        msg = await self.send(
            f"{self.emoji} {mention(p.id, p.name)}, it's your turn! ({self.board_header})\n"
            f"Tap <b>Throw</b> to roll {self.rolls}× — auto-throw in {TURN_TIMEOUT}s.",
            reply_markup=kb)
        self.turn_msg_id = msg.message_id

        auto = False
        try:
            await asyncio.wait_for(self.throw_event.wait(), TURN_TIMEOUT)
        except asyncio.TimeoutError:
            auto = True
        self.current_turn = None

        head = f"{self.emoji} {mention(p.id, p.name)}" + (" ⏰ (auto-throw)" if auto else "")
        await self.edit(msg.message_id, head + " is throwing…")

        kw = {"message_thread_id": self.thread_id} if self.thread_id else {}
        for _ in range(self.rolls):
            dm = await self._call(self.bot.send_dice, self.chat_id, emoji=self.emoji, **kw)
            value = dm.dice.value
            p.round_rolls.append(value)
            p.total_points += value
            await asyncio.sleep(ANIM_DELAY)
            await self.edit(msg.message_id,
                            f"{head}\nRolls: {' '.join(map(str, p.round_rolls))} → <b>{sum(p.round_rolls)}</b>")
        p.done = True
        await self._update_board()

    # ------------------------------------------------------------- scoreboard
    def _score_str(self, p: Player) -> str:
        if p not in self.participants:
            return "— (out)"
        if not p.done:
            return "⏳"
        return f"<b>{sum(p.round_rolls)}</b> ({'+'.join(map(str, p.round_rolls))})"

    def board_text(self) -> str:
        lines = [
            f"{self.emoji} <b>{self.game_name}</b> · {self.mode_label} · {self.rolls} roll(s)",
            f"🎯 First to <b>{self.target_wins}</b> series win(s)",
            "",
            f"📋 <b>{self.board_header}</b>",
        ]
        for p in self.players.values():
            lines.append(f"• {mention(p.id, p.name)}: {self._score_str(p)}  |  🏅 {p.series_wins}/{self.target_wins}")
        if self.last_note:
            lines += ["", self.last_note]
        return "\n".join(lines)

    async def _update_board(self, repost: bool = False) -> None:
        text = self.board_text()
        if repost or self.board_msg_id is None:
            old = self.board_msg_id
            msg = await self.send(text)
            self.board_msg_id = msg.message_id
            await self.delete(old)
        else:
            await self.edit(self.board_msg_id, text)

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
                winner_id=winner.id, winner_name=winner.name,
                players=[database.PlayerResult(p.id, p.name, p.series_wins, p.total_points) for p in ranking],
            )
        except Exception:
            saved = False
            log.exception("Failed to save match")

        lines = [f"🏆 <b>MATCH OVER!</b> 🏆", f"Winner: {mention(winner.id, winner.name)} 🎉", "", "<b>Final standings:</b>"]
        for i, p in enumerate(ranking):
            lines.append(f"{MEDALS[i] if i < 3 else '▫️'} {mention(p.id, p.name)} — "
                         f"{p.series_wins} series win(s) · {p.total_points} pts")
        lines += ["", "💾 Stats saved. Check /stats, /top, /history." if saved else "⚠️ Could not save stats (see server logs)."]
        await self.send("\n".join(lines))


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
