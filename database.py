"""Persistent storage on MongoDB Atlas (PyMongo async API).

Only players who FINISH a match are stored (id, name, stats). Nothing is dropped
or reset on start-up - indexes are only created if missing.
"""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument

log = logging.getLogger("pvp.db")

client: Optional[AsyncMongoClient] = None
db = None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PlayerResult:
    user_id: int
    name: str
    series_wins: int
    total_points: int


async def init_db(uri: str, db_name: str = "pvp_bot") -> None:
    global client, db
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=10000)
    db = client[db_name]
    await client.admin.command("ping")  # fails fast with a clear error if URI/password/IP is wrong
    await db.users.create_index([("wins", DESCENDING), ("matches_played", ASCENDING)])
    await db.matches.create_index([("players.user_id", ASCENDING), ("_id", DESCENDING)])
    log.info("Connected to MongoDB database '%s'", db_name)


async def close_db() -> None:
    if client is not None:
        await client.close()


def _user(doc: Optional[dict]) -> Optional[SimpleNamespace]:
    if not doc:
        return None
    return SimpleNamespace(
        id=doc["_id"], full_name=doc.get("full_name", ""),
        matches_played=doc.get("matches_played", 0), wins=doc.get("wins", 0),
        losses=doc.get("losses", 0), rounds_won=doc.get("rounds_won", 0),
        total_points=doc.get("total_points", 0))


async def get_user(uid: int):
    return _user(await db.users.find_one({"_id": uid}))


async def record_match(
    *, chat_id: int, chat_title: str, game: str, mode: str, rolls: int, target_wins: int,
    started_at: datetime, winner_id: int, winner_name: str, players: list[PlayerResult],
) -> int:
    counter = await db.counters.find_one_and_update(
        {"_id": "matches"}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER)
    match_id = counter["seq"]
    now = utcnow()

    await db.matches.insert_one({
        "_id": match_id, "chat_id": chat_id, "chat_title": chat_title or "", "game": game,
        "mode": mode, "rolls": rolls, "target_wins": target_wins, "player_count": len(players),
        "winner_id": winner_id, "winner_name": winner_name, "started_at": started_at,
        "finished_at": now,
        "players": [{"user_id": p.user_id, "name": p.name, "series_wins": p.series_wins,
                     "total_points": p.total_points, "is_winner": p.user_id == winner_id}
                    for p in players],
    })

    for p in players:  # atomic $inc per user - safe with many groups finishing at once
        won = p.user_id == winner_id
        await db.users.update_one(
            {"_id": p.user_id},
            {"$inc": {"matches_played": 1, "wins": 1 if won else 0, "losses": 0 if won else 1,
                      "rounds_won": p.series_wins, "total_points": p.total_points},
             "$set": {"full_name": p.name, "updated_at": now},
             "$setOnInsert": {"created_at": now}},
            upsert=True)
    return match_id


async def top_players(limit: int = 10):
    cur = (db.users.find({"matches_played": {"$gt": 0}})
           .sort([("wins", DESCENDING), ("matches_played", ASCENDING)]).limit(limit))
    return [_user(d) for d in await cur.to_list(length=limit)]


async def recent_matches(uid: int, limit: int = 5):
    cur = db.matches.find({"players.user_id": uid}).sort("_id", DESCENDING).limit(limit)
    rows = []
    for m in await cur.to_list(length=limit):
        me = next((p for p in m["players"] if p["user_id"] == uid), {})
        rows.append(SimpleNamespace(
            Match=SimpleNamespace(
                id=m["_id"], game=m["game"], mode=m["mode"], player_count=m["player_count"],
                target_wins=m["target_wins"], winner_name=m.get("winner_name", ""),
                finished_at=m["finished_at"]),
            is_winner=me.get("is_winner", False), series_wins=me.get("series_wins", 0)))
    return rows


# --------------------------------------------------------------------------- running matches
# A running match is saved after every roll, so a redeploy/restart resumes it instead of cancelling.
async def save_active(snapshot: dict) -> None:
    await db.active_matches.replace_one({"_id": snapshot["_id"]}, snapshot, upsert=True)


async def delete_active(chat_id: int) -> None:
    await db.active_matches.delete_one({"_id": chat_id})


async def load_active() -> list[dict]:
    return await db.active_matches.find({}).to_list(length=None)
