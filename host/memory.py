"""会话记忆：SQLite 持久化 + 滑动窗口，进程重启后历史仍在。"""
import json
import sqlite3
import weakref
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.db"

# 跟踪存活实例，便于测试或退出时统一关闭连接（Windows 上文件未释放会导致临时目录清理失败）
_INSTANCES = weakref.WeakSet()


def close_all():
    for inst in list(_INSTANCES):
        inst.close()


class SessionMemory:
    """按 session_id 隔离的消息历史。window 控制送回 LLM 的最大消息数。"""

    def __init__(self, session_id: str, window: int = 30):
        self.session_id = session_id
        self.window = window
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(DB_PATH)
        _INSTANCES.add(self)
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS messages(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.conn.commit()

    def add(self, message: dict):
        self.conn.execute(
            "INSERT INTO messages(session_id, payload) VALUES(?, ?)",
            (self.session_id, json.dumps(message, ensure_ascii=False)),
        )
        self.conn.commit()

    def history(self) -> list:
        rows = self.conn.execute(
            "SELECT payload FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (self.session_id, self.window),
        ).fetchall()
        return [json.loads(r[0]) for r in reversed(rows)]

    def clear(self):
        self.conn.execute("DELETE FROM messages WHERE session_id = ?", (self.session_id,))
        self.conn.commit()

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
