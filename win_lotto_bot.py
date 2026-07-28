# bingo_bot_vps.py
import asyncio
import json
import os
import random
import string
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery

import aiosqlite


# -------------------- CONFIG --------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE")
DB_PATH = os.getenv("DB_PATH", "bingo.db")

BOARD_SIZE = 5
FREE_CENTER = True
AUTO_CALL_SECONDS = int(os.getenv("AUTO_CALL_SECONDS", "10"))

# Put your Amharic bingo phrases here (MUST be JSON-like python list)
BINGO_ITEMS = [
    # examples (replace with real items; keep >= 25 UNIQUE)
    "ሰላም","አዲስ አበባ","ኢትዮጵያ","አንቺ","አንታላች","ወርቅ","አበባ","አትክልት",
    "ትምህርት","እውቀት","ስፖርት","ሙዚቃ","መጽሐፍ","ታሪክ","ገበያ","አውራጃ",
    "ጉዞ","መዝናኛ","ምግብ","መጠጥ","ቤት","ጓደኛ","ቤተሰብ","እቅድ","እርምጃ"
]

# ------------------------------------------------


dp = Dispatcher()
active_autocall_tasks: Dict[int, asyncio.Task] = {}  # chat_id -> task


def _validate_items(items: List[str]) -> List[str]:
    items = [x.strip() for x in items if x and x.strip()]
    items = list(dict.fromkeys(items))  # unique while preserving order
    if len(items) < 25:
        raise ValueError("Need at least 25 unique Amharic bingo items in BINGO_ITEMS")
    return items


POOL = _validate_items(BINGO_ITEMS)


def rc_to_cell_index(r: int, c: int) -> int:
    return r * BOARD_SIZE + c


def check_win(board: List[List[str]], marked: Set[str]) -> bool:
    def is_marked(cell_text: str) -> bool:
        if cell_text == "FREE":
            return True
        return cell_text in marked

    # rows
    for r in range(BOARD_SIZE):
        if all(is_marked(board[r][c]) for c in range(BOARD_SIZE)):
            return True
    # cols
    for c in range(BOARD_SIZE):
        if all(is_marked(board[r][c]) for r in range(BOARD_SIZE)):
            return True
    # diag
    if all(is_marked(board[i][i]) for i in range(BOARD_SIZE)):
        return True
    # anti diag
    if all(is_marked(board[i][BOARD_SIZE - 1 - i]) for i in range(BOARD_SIZE)):
        return True
    return False


def format_board(board: List[List[str]], marked: Set[str]) -> str:
    lines = []
    for r in range(BOARD_SIZE):
        row_cells = []
        for c in range(BOARD_SIZE):
            text = board[r][c]
            if text == "FREE":
                row_cells.append("⭐ FREE")
            elif text in marked:
                row_cells.append(f"✅ {text}")
            else:
                row_cells.append(f"▫ {text}")
        lines.append(" | ".join(row_cells))
    return "\n".join(lines)


def make_board(pool: List[str]) -> List[List[str]]:
    all_cells = BOARD_SIZE * BOARD_SIZE
    if FREE_CENTER:
        center_idx = rc_to_cell_index(BOARD_SIZE // 2, BOARD_SIZE // 2)
        chosen = random.sample(pool, all_cells - 1)  # 24
        board_flat = []
        it = iter(chosen)
        for idx in range(all_cells):
            if idx == center_idx:
                board_flat.append("FREE")
            else:
                board_flat.append(next(it))
    else:
        chosen = random.sample(pool, all_cells)
        board_flat = chosen

    return [[board_flat[rc_to_cell_index(r, c)] for c in range(BOARD_SIZE)] for r in range(BOARD_SIZE)]


def make_game_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=8))


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS games (
            chat_id INTEGER PRIMARY KEY,
            game_id TEXT NOT NULL,
            status TEXT NOT NULL,
            called TEXT NOT NULL,
            last_called_idx INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS players (
            chat_id INTEGER NOT NULL,
            game_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            board_json TEXT NOT NULL,
            marked_json TEXT NOT NULL,
            has_won INTEGER NOT NULL,
            PRIMARY KEY (chat_id, game_id, user_id)
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS winners (
            chat_id INTEGER NOT NULL,
            game_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            won_at TEXT NOT NULL
        )
        """)
        await db.commit()


async def get_game(chat_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM games WHERE chat_id = ?", (chat_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def upsert_game(chat_id: int, game_id: str, status: str, called: List[str], last_called_idx: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO games(chat_id, game_id, status, called, last_called_idx, created_at)
            VALUES(?,?,?,?,?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET
                game_id=excluded.game_id,
                status=excluded.status,
                called=excluded.called,
                last_called_idx=excluded.last_called_idx
            """,
            (chat_id, game_id, status, json.dumps(called, ensure_ascii=False), last_called_idx),
        )
        await db.commit()


async def set_game_status(chat_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE games SET status=? WHERE chat_id=?", (status, chat_id))
        await db.commit()


async def get_or_create_player(chat_id: int, game_id: str, user_id: int) -> Dict[str, Any]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM players WHERE chat_id=? AND game_id=? AND user_id=?",
            (chat_id, game_id, user_id),
        )
        row = await cur.fetchone()
        if row:
            return dict(row)

        board = make_board(POOL)
        await db.execute(
            """
            INSERT INTO players(chat_id, game_id, user_id, board_json, marked_json, has_won)
            VALUES(?,?,?,?,?,0)
            """,
            (chat_id, game_id, user_id, json.dumps(board, ensure_ascii=False), json.dumps([], ensure_ascii=False)),
        )
        await db.commit()

        # return inserted
        cur2 = await db.execute(
            "SELECT * FROM players WHERE chat_id=? AND game_id=? AND user_id=?",
            (chat_id, game_id, user_id),
        )
        row2 = await cur2.fetchone()
        return dict(row2)


async def list_players(chat_id: int, game_id: str) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM players WHERE chat_id=? AND game_id=?", (chat_id, game_id))
        return [dict(r) for r in await cur.fetchall()]


async def update_player_marked(chat_id: int, game_id: str, user_id: int, marked: List[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET marked_json=? WHERE chat_id=? AND game_id=? AND user_id=?",
            (json.dumps(marked, ensure_ascii=False), chat_id, game_id, user_id),
        )
        await db.commit()


async def mark_player_won(chat_id: int, game_id: str, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE players SET has_won=1 WHERE chat_id=? AND game_id=? AND user_id=?",
            (chat_id, game_id, user_id),
        )
        await db.execute(
            "INSERT INTO winners(chat_id, game_id, user_id, won_at) VALUES(?,?,?, datetime('now'))",
            (chat_id, game_id, user_id),
        )
        await db.commit()


def bingo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📣 Call next", callback_data="call_next")]
        ]
    )


async def auto_call_loop(chat_id: int, bot: Bot):
    try:
        while True:
            game = await get_game(chat_id)
            if not game or game["status"] != "running":
                return

            called = json.loads(game["called"])
            called_set = set(called)
            remaining = [x for x in POOL if x not in called_set]
            if not remaining:
                await bot.send_message(chat_id, "⏹️ No more bingo items. Game over!")
                await set_game_status(chat_id, "finished")
                return

            next_item = random.choice(remaining)
            called.append(next_item)

            await upsert_game(
                chat_id=chat_id,
                game_id=game["game_id"],
                status="running",
                called=called,
                last_called_idx=game["last_called_idx"] + 1,
            )

            # Update players & check wins
            game_after = await get_game(chat_id)
            players = await list_players(chat_id, game_after["game_id"])

            called_set = set(called)
            winners_found: List[int] = []

            for p in players:
                if int(p["has_won"]) == 1:
                    continue

                board = json.loads(p["board_json"])
                board_flat = sum(board, [])
                recomputed = [item for item in called_set if item in board_flat and item != "FREE"]

                await update_player_marked(chat_id, game_after["game_id"], p["user_id"], recomputed)

                if check_win(board, set(recomputed)):
                    await mark_player_won(chat_id, game_after["game_id"], p["user_id"])
                    winners_found.append(p["user_id"])

            await bot.send_message(chat_id, f"📣 Called: {next_item}")

            if winners_found:
                winners_unique = list(dict.fromkeys(winners_found))
                if len(winners_unique) == 1:
                    await bot.send_message(chat_id, f"🏆 Winner: {winners_unique[0]}")
                else:
                    await bot.send_message(chat_id, f"🏆 Winners: {', '.join(map(str, winners_unique))}")
                await set_game_status(chat_id, "finished")
                return

            await asyncio.sleep(AUTO_CALL_SECONDS)
    except asyncio.CancelledError:
        return


@dp.message(F.Command("start"))
async def on_start(message: Message):
    await message.answer(
        "ቢንጎ ቦት ይጀምሩ።\n"
        "/bingo ጨዋታ ጀምር\n"
        "/join ለመቀላቀል\n"
        "/board የእርስዎን ቦርድ ለማየት"
    )


@dp.message(F.Command("bingo"))
async def on_bingo(message: Message, bot: Bot):
    chat_id = message.chat.id

    # stop existing autocall task
    task = active_autocall_tasks.get(chat_id)
    if task:
        task.cancel()
        active_autocall_tasks.pop(chat_id, None)

    await upsert_game(chat_id, make_game_id(), "running", [], 0)
    await message.answer("🎲 ቢንጎ ጨዋታ ተጀምሯል! /join ተጫዋቾች ይቀላቀሉ።")
    await message.answer("ይጫኑ:", reply_markup=bingo_keyboard())

    active_autocall_tasks[chat_id] = asyncio.create_task(auto_call_loop(chat_id, bot))


@dp.message(F.Command("join"))
async def on_join(message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not user:
        return
    user_id = user.id

    game = await get_game(chat_id)
    if not game:
        await message.answer("መጀመሪያ /bingo ጨዋታ ይጀምሩ።")
        return

    game_id = game["game_id"]
    player = await get_or_create_player(chat_id, game_id, user_id)

    board = json.loads(player["board_json"])
    called = json.loads(game["called"])
    called_set = set(called)

    board_flat = sum(board, [])
    marked = [x for x in called_set if x in board_flat and x != "FREE"]
    await update_player_marked(chat_id, game_id, user_id, marked)

    await message.answer("✅ የእርስዎ ቦርድ:\n" + format_board(board, set(marked)))


@dp.message(F.Command("board"))
async def on_board(message: Message):
    chat_id = message.chat.id
    user = message.from_user
    if not user:
        return
    user_id = user.id

    game = await get_game(chat_id)
    if not game:
        await message.answer("መጀመሪያ /bingo ጨዋታ ይጀምሩ።")
        return

    game_id = game["game_id"]
    player = await get_or_create_player(chat_id, game_id, user_id)

    board = json.loads(player["board_json"])
    called = json.loads(game["called"])
    called_set = set(called)
    board_flat = sum(board, [])
    marked = {x for x in called_set if x in board_flat and x != "FREE"}

    await message.answer("🧾 የእርስዎ ቦርድ:\n" + format_board(board, marked))


@dp.callback_query(F.data == "call_next")
async def on_call_next(callback: CallbackQuery, bot: Bot):
    message = callback.message
    if not message:
        return
    chat_id = message.chat.id

    game = await get_game(chat_id)
    if not game or game["status"] != "running":
        await callback.answer("Game not running.", show_alert=False)
        return

    called = json.loads(game["called"])
    called_set = set(called)
    remaining = [x for x in POOL if x not in called_set]
    if not remaining:
        await set_game_status(chat_id, "finished")
        await callback.answer("No items left.", show_alert=False)
        return

    next_item = random.choice(remaining)
    called.append(next_item)

    await upsert_game(chat_id, game["game_id"], "running", called, game["last_called_idx"] + 1)

    game_after = await get_game(chat_id)
    players = await list_players(chat_id, game_after["game_id"])

    called_set = set(called)
    winners_found: List[int] = []

    for p in players:
        if int(p["has_won"]) == 1:
            continue
        board = json.loads(p["board_json"])
        board_flat = sum(board, [])
        recomputed = [item for item in called_set if item in board_flat and item != "FREE"]
        await update_player_marked(chat_id, game_after["game_id"], p["user_id"], recomputed)
        if check_win(board, set(recomputed)):
            await mark_player_won(chat_id, game_after["game_id"], p["user_id"])
            winners_found.append(p["user_id"])

    await bot.send_message(chat_id, f"📣 Called (manual): {next_item}")
    if winners_found:
        winners_unique = list(dict.fromkeys(winners_found))
        if len(winners_unique) == 1:
            await bot.send_message(chat_id, f"🏆 Winner: {winners_unique[0]}")
        else:
            await bot.send_message(chat_id, f"🏆 Winners: {', '.join(map(str, winners_unique))}")
        await set_game_status(chat_id, "finished")

    await callback.answer("Called next!", show_alert=False)


async def main():
    if "PASTE_YOUR_TELEGRAM_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var (recommended).")

    await init_db()
    bot = Bot(BOT_TOKEN)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
