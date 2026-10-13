"""Jackpot game: everybody bets, a wheel (slice size = share of the pot) spins, winner takes the WHOLE pot.

* No house cut. Max 10 players. 20 s countdown starts when the 2nd player is in.
* Players join with the buttons / `/jackpot <amount>`, or by a private invite from the creator
  ("only visible to you" message, same system as the dice game).
* Provably fair: the hash of a secret seed is shown when the jackpot opens, the seed is revealed after
  the spin; winner ticket = HMAC-SHA256(seed, "jackpot|total|players") mod total (in cents).
* Amounts are plain numbers for now (no wallet / balance yet).
"""
import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import time
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters.callback_data import CallbackData
from aiogram.types import BufferedInputFile, InlineKeyboardMarkup

import database
import wheel
from game import EphemeralMessageParameters, btn, esc, mention, money

log = logging.getLogger("pvp.jackpot")

MAX_PLAYERS = 10
MIN_BET = 10               # cents  ($0.10)
MAX_BET = 1_000_000        # cents  ($10,000) per player
COUNTDOWN = 20             # seconds, starts when the 2nd player joins
LOBBY_TIMEOUT = 300        # an empty/one-player jackpot expires after this
BET_PRESETS = [100, 500, 1000, 2500]   # $1 / $5 / $10 / $25
_bg: set = set()


class JackpotInviteCB(CallbackData, prefix="jpi"):
    action: str        # bet | no
    chat_id: int
    user_id: int       # the ONLY user this invite is for
    cents: int = 0


def parse_amount(text: str) -> Optional[int]:
    """'30' / '0.5' / '$12.50' -> cents (None if not a valid amount)."""
    t = text.strip().lstrip("$").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    if not math.isfinite(v) or v <= 0:
        return None
    return int(round(v * 100))


class JackpotSession:
    def __init__(self, bot: Bot, chat_id: int, thread_id: Optional[int], creator_id: int, creator_name: str,
                 meta: Optional[dict] = None):
        self.bot, self.chat_id, self.thread_id = bot, chat_id, thread_id
        self.creator_id, self.creator_name = creator_id, creator_name
        self.meta = meta or {}
        self.bets: dict[int, dict] = {}       # uid -> {"name", "cents", "color"} (join order = slice order)
        self.invited: dict[int, str] = {}
        self.invites: dict[int, dict] = {}    # uid -> ephemeral message ids
        self.status = "lobby"                 # lobby -> countdown -> spinning -> done/closed
        self.msg_id: Optional[int] = None
        self.seed = secrets.token_hex(16)
        self.commit = hashlib.sha256(bytes.fromhex(self.seed)).hexdigest()
        self.deadline = 0.0
        self.lock = asyncio.Lock()
        self._dirty = False
        self._updater: Optional[asyncio.Task] = None
        self.countdown_task: Optional[asyncio.Task] = None
        self.lobby_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------- helpers
    def total(self) -> int:
        return sum(b["cents"] for b in self.bets.values())

    def pct(self, uid: int) -> float:
        t = self.total()
        return 100.0 * self.bets[uid]["cents"] / t if t else 0.0

    def _meta_line(self) -> str:
        m = self.meta
        if not m.get("record_cents"):
            return ""
        line = f"🏅 Record {money(m['record_cents'])} ({esc(m.get('record_name', '?'))})"
        if m.get("last_name"):
            line += f" · Last: {esc(m['last_name'])} +{money(m.get('last_cents', 0))}"
        return line

    def players_text(self) -> str:
        if not self.bets:
            return "<i>nobody yet — be the first!</i>"
        return "\n".join(
            f"{wheel.COLORS[b['color']][1]} {esc(b['name'][:20])} — {money(b['cents'])} · {self.pct(uid):.1f}%"
            for uid, b in self.bets.items())

    def caption(self) -> str:
        lines = ["🎰 <b>JACKPOT — winner takes the pot</b>", "",
                 f"💰 Pot: <b>{money(self.total())}</b>", "",
                 f"👥 <b>Players ({len(self.bets)}/{MAX_PLAYERS})</b>", self.players_text(), ""]
        if self.status == "countdown":
            lines.append(f"⏳ Spinning in <b>{max(0, math.ceil(self.deadline - time.monotonic()))}s</b>")
        elif self.status == "lobby":
            lines.append("⏳ Waiting for a 2nd player…")
        lines.append(f"🔐 Fair hash: <code>{self.commit[:16]}…</code>")
        lines.append("➕ Tap an amount, or <code>/jackpot 10</code> · creator can invite with @username")
        ml = self._meta_line()
        if ml:
            lines.append(ml)
        return "\n".join(lines)

    def markup(self) -> Optional[InlineKeyboardMarkup]:
        if self.status not in ("lobby", "countdown"):
            return None
        rows = [[btn(f"💰 {money(c)}", f"jp:bet:{c}", "primary") for c in BET_PRESETS]]
        if self.status == "lobby":
            rows.append([btn("✖️ Cancel (creator)", "jp:cancel", "danger")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    # ------------------------------------------------------------- telegram helpers
    async def _call(self, method, *args, **kwargs):
        for _ in range(5):
            try:
                return await method(*args, **kwargs)
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
        raise RuntimeError("Flood control: too many retries")

    def _kw(self) -> dict:
        return {"message_thread_id": self.thread_id} if self.thread_id else {}

    async def send(self, text: str, **kw):
        return await self._call(self.bot.send_message, self.chat_id, text, **self._kw(), **kw)

    async def _edit_caption(self, text: Optional[str] = None, markup="auto") -> None:
        if not self.msg_id:
            return
        try:
            await self._call(self.bot.edit_message_caption, chat_id=self.chat_id, message_id=self.msg_id,
                             caption=text if text is not None else self.caption(),
                             reply_markup=self.markup() if markup == "auto" else markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.debug("caption edit failed: %s", e)

    def request_refresh(self) -> None:
        """Coalesced live update of the jackpot message (max ~1 edit per second)."""
        self._dirty = True
        if self._updater is None or self._updater.done():
            self._updater = asyncio.create_task(self._update_loop())

    async def _update_loop(self) -> None:
        while self._dirty and self.status in ("lobby", "countdown"):
            self._dirty = False
            await self._edit_caption()
            await asyncio.sleep(1.1)

    async def open(self) -> None:
        m = await self._call(self.bot.send_photo, self.chat_id,
                             BufferedInputFile(wheel.render_banner(), "jackpot.png"),
                             caption=self.caption(), reply_markup=self.markup(), **self._kw())
        self.msg_id = m.message_id
        self.lobby_task = asyncio.create_task(self._lobby_expiry())

    async def _lobby_expiry(self) -> None:
        try:
            await asyncio.sleep(LOBBY_TIMEOUT)
            if self.status == "lobby":
                await self._edit_caption("⌛ Jackpot expired — nobody joined in time. Use /jackpot to start a new one.",
                                         markup=None)
                await self.close_invites("⌛ This jackpot expired.")
                await manager.close(self)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("jackpot lobby expiry failed")

    # ------------------------------------------------------------- betting
    async def place_bet(self, uid: int, name: str, cents: int) -> tuple[Optional[str], str]:
        """Join / raise a bet. Returns (error or None, confirmation text)."""
        async with self.lock:
            if self.status not in ("lobby", "countdown"):
                return "This jackpot is already closed.", ""
            if self.status == "countdown" and self.deadline - time.monotonic() < 1.0:
                return "Too late — the wheel is about to spin! 🎡", ""
            if cents < MIN_BET:
                return f"Minimum bet is {money(MIN_BET)}.", ""
            cur = self.bets.get(uid)
            new = (cur["cents"] if cur else 0) + cents
            if new > MAX_BET:
                return f"Maximum bet is {money(MAX_BET)}.", ""
            if not cur and len(self.bets) >= MAX_PLAYERS:
                return f"The jackpot is full ({MAX_PLAYERS}/{MAX_PLAYERS}).", ""
            if cur:
                cur["cents"], cur["name"] = new, name
            else:
                self.bets[uid] = {"name": name, "cents": new, "color": len(self.bets)}
            self.invited.pop(uid, None)
            if self.status == "lobby" and len(self.bets) >= 2:
                self._start_countdown()
            self.request_refresh()
            total = self.total()
            return None, (f"✅ You're in for {money(new)} — {self.pct(uid):.1f}% chance to win {money(total)}")

    def _start_countdown(self) -> None:
        self.status = "countdown"
        self.deadline = time.monotonic() + COUNTDOWN
        if self.lobby_task and not self.lobby_task.done():
            self.lobby_task.cancel()
        self.countdown_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while True:
                rem = self.deadline - time.monotonic()
                if rem <= 0:
                    break
                await asyncio.sleep(min(5.0, rem))
                self.request_refresh()
            await self._spin()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("jackpot run failed in chat %s", self.chat_id)
            try:
                await self.send("⚠️ The jackpot hit an error and was cancelled.")
            except Exception:
                pass
            await manager.close(self)

    # ------------------------------------------------------------- the spin
    async def _avatar(self, uid: int) -> Optional[bytes]:
        try:
            photos = await self.bot.get_user_profile_photos(uid, limit=1)
            if not photos.total_count:
                return None
            sizes = photos.photos[0]
            buf = await self.bot.download(sizes[1] if len(sizes) > 1 else sizes[0])
            return buf.read()
        except Exception as e:
            log.debug("no avatar for %s: %s", uid, e)
            return None

    def _ticket(self, total: int, n: int) -> int:
        digest = hmac.new(bytes.fromhex(self.seed), f"jackpot|{total}|{n}".encode(), hashlib.sha256).digest()
        return int.from_bytes(digest, "big") % total

    async def _spin(self) -> None:
        async with self.lock:
            self.status = "spinning"                 # bets are locked from here on
            players = [{"id": uid, "name": b["name"], "cents": b["cents"], "color": b["color"]}
                       for uid, b in self.bets.items()]
        total = sum(p["cents"] for p in players)
        ticket = self._ticket(total, len(players))
        acc, widx = 0, 0
        for i, p in enumerate(players):
            acc += p["cents"]
            if ticket < acc:
                widx = i
                break
        winner = players[widx]
        await self.close_invites("⌛ Bets are closed — the wheel is spinning.")
        await self._edit_caption("🎰 <b>Bets are closed!</b>\n🎡 Building the wheel…", markup=None)

        avatars = await asyncio.gather(*(self._avatar(p["id"]) for p in players))
        wheel_players = [{"name": p["name"], "cents": p["cents"], "color": wheel.COLORS[p["color"]][0], "avatar": a}
                         for p, a in zip(players, avatars)]
        gif = None
        try:
            gif, dur = await asyncio.to_thread(wheel.render_spin, wheel_players, widx, total,
                                               secrets.randbits(32))
        except Exception:
            log.exception("wheel render failed")
            dur = 0.0

        lines = "\n".join(f"{wheel.COLORS[p['color']][1]} {esc(p['name'][:20])} — {money(p['cents'])} · "
                          f"{100.0 * p['cents'] / total:.1f}%" for p in players)
        spin_caption = f"🎰 <b>Jackpot — spinning!</b>\n💰 Pot: <b>{money(total)}</b>\n\n{lines}"
        if gif:
            try:
                await self._call(self.bot.send_animation, self.chat_id, BufferedInputFile(gif, "jackpot.gif"),
                                 caption=spin_caption, width=wheel.OUT, height=wheel.OUT,
                                 duration=int(dur) + 4, **self._kw())
            except Exception:
                log.exception("sending the wheel failed")
                gif = None
        if not gif:
            await self.send(spin_caption)
        await self._edit_caption("🔒 <b>Bets closed</b> — see the wheel below ⬇️", markup=None)

        pct = 100.0 * winner["cents"] / total
        info = {}
        try:
            info = await database.record_jackpot(
                chat_id=self.chat_id, pot_cents=total, winner_id=winner["id"], winner_name=winner["name"],
                players=[(p["id"], p["name"], p["cents"]) for p in players], commit=self.commit, seed=self.seed)
        except Exception:
            log.exception("record_jackpot failed")
        if gif:
            await asyncio.sleep(dur + 1.0)

        res = [f"🏆 <b>{esc(winner['name'])} wins the jackpot!</b>",
               f"💰 Pot <b>{money(total)}</b> with a <b>{pct:.1f}%</b> chance"]
        if info.get("is_record"):
            res.append(f"🔥 <b>NEW RECORD!</b> {money(total)}")
        elif info.get("record_cents"):
            res.append(f"🏅 Record {money(info['record_cents'])} ({esc(info.get('record_name', '?'))})")
        res += ["", f"🔐 Seed <code>{self.seed}</code>",
                f"SHA-256 = the hash shown at the start · ticket <b>{ticket}</b> of {total} (cents)"]
        self.status = "done"
        try:
            await self.send("\n".join(res))
        finally:
            await manager.close(self)

    # ------------------------------------------------------------- private invites (ephemeral)
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
            await self.bot.edit_ephemeral_message_text(chat_id=self.chat_id, receiver_user_id=uid,
                                                       ephemeral_message_id=eid, text=text, parse_mode="HTML")
        except Exception as e:
            log.debug("ephemeral edit failed: %s", e)

    async def _eph_delete(self, uid: int, eid: Optional[int]) -> None:
        if eid is None:
            return
        try:
            await self.bot.delete_ephemeral_message(chat_id=self.chat_id, receiver_user_id=uid,
                                                    ephemeral_message_id=eid)
        except Exception as e:
            log.debug("ephemeral delete failed: %s", e)

    async def tell_creator(self, text: str) -> None:
        try:
            await self._eph_send(self.creator_id, text)
        except Exception:
            try:
                await self.send(text)
            except Exception:
                pass

    def can_invite(self, uid: int) -> Optional[str]:
        if self.status not in ("lobby", "countdown"):
            return "the jackpot is closed"
        if uid in self.bets:
            return "already in"
        if len(self.bets) + len(self.invited) >= MAX_PLAYERS:
            return f"all {MAX_PLAYERS} slots are taken/reserved"
        return None

    async def send_invite(self, uid: int, name: str) -> Optional[str]:
        await self.drop_invite(uid)
        rows = [[btn(f"💰 {money(c)}", JackpotInviteCB(action="bet", chat_id=self.chat_id, user_id=uid, cents=c).pack(),
                     "primary") for c in BET_PRESETS],
                [btn("❌ Decline", JackpotInviteCB(action="no", chat_id=self.chat_id, user_id=uid).pack(), "danger")]]
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        text = (f"👋 {mention(uid, name)}, {mention(self.creator_id, self.creator_name)} invited you to a "
                f"🎰 <b>JACKPOT</b> — winner takes the whole pot!\nPick your bet:")
        try:
            target = await self._eph_send(uid, text, kb)
        except Exception as e:
            log.warning("jackpot invite to %s failed: %s", uid, e)
            return f"{esc(name)}: couldn't deliver the private invite"
        creator = None
        try:
            creator = await self._eph_send(self.creator_id, f"📩 Jackpot invite sent to {esc(name)} — waiting…")
        except Exception:
            pass
        self.invites[uid] = {"name": name, "target": target, "creator": creator}
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
            await self.resolve_invite(uid, text, f"⌛ Invite to {esc(self.invites[uid]['name'])} closed.")
        self.invited.clear()


class JackpotManager:
    def __init__(self) -> None:
        self.sessions: dict[int, JackpotSession] = {}

    def get(self, chat_id: int) -> Optional[JackpotSession]:
        return self.sessions.get(chat_id)

    async def close(self, s: JackpotSession) -> None:
        if self.sessions.get(s.chat_id) is not s:
            return
        del self.sessions[s.chat_id]
        if s.status in ("lobby", "countdown"):
            s.status = "closed"
        for t in (s.lobby_task, s.countdown_task):
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()

    async def shutdown(self) -> None:
        """Redeploy: unstarted jackpots are cancelled (nothing is lost - no balances yet)."""
        for s in list(self.sessions.values()):
            if s.status in ("lobby", "countdown"):
                await s._edit_caption("⚠️ Bot restarted — this jackpot was cancelled. Use /jackpot to start again.",
                                      markup=None)
                await s.close_invites("⌛ This jackpot was cancelled.")
        self.sessions.clear()


manager = JackpotManager()
