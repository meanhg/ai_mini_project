"""대화·출처·실행 상태를 SQLite에 보존한다. 기존 messages 테이블은 마이그레이션한다."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

DB_PATH = Path(__file__).resolve().parents[1] / 'session.db'


def now():
    return datetime.now(timezone.utc).isoformat()


class SqliteSessionStore:
    def __init__(self, db_path=DB_PATH):
        self.db_path = Path(db_path)
        self.lock = RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('CREATE TABLE IF NOT EXISTS messages '
                          '(thread_id TEXT, role TEXT, content TEXT, created TEXT)')
        columns = {r[1] for r in self.conn.execute('PRAGMA table_info(messages)')}
        if 'metadata' not in columns:
            self.conn.execute("ALTER TABLE messages ADD COLUMN metadata TEXT DEFAULT '{}'")
        self.conn.execute('CREATE INDEX IF NOT EXISTS message_thread ON messages(thread_id)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS contexts (thread_id TEXT PRIMARY KEY, data TEXT)')
        self.conn.execute('CREATE TABLE IF NOT EXISTS runs '
                          '(id TEXT PRIMARY KEY, thread_id TEXT, query TEXT, backend TEXT, '
                          'status TEXT, created TEXT, updated TEXT)')
        self.conn.execute("UPDATE runs SET status='interrupted' WHERE status='running'")
        self.conn.commit()

    def load(self, thread_id):
        with self.lock:
            return [{'role': r, 'content': c} for r, c in self.conn.execute(
                'SELECT role,content FROM messages WHERE thread_id=? ORDER BY rowid', (thread_id,))]

    def history(self, thread_id, limit=100, before=None):
        with self.lock:
            rows = self.conn.execute('SELECT rowid,role,content,created,metadata FROM messages '
                'WHERE thread_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?',
                (thread_id, before or 9223372036854775807, max(1, min(limit, 100)))).fetchall()
            return [{'id': i, 'role': r, 'content': c, 'created': t, 'metadata': json.loads(m or '{}')}
                    for i, r, c, t, m in reversed(rows)]

    def context(self, thread_id):
        with self.lock:
            row = self.conn.execute('SELECT data FROM contexts WHERE thread_id=?', (thread_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def append(self, thread_id, role, content):
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO messages (thread_id,role,content,created,metadata) '
                              'VALUES (?,?,?,?,?)', (thread_id, role, content, now(), '{}'))

    def start_run(self, run_id, thread_id, query, backend):
        with self.lock, self.conn:
            self.conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?)',
                              (run_id, thread_id, query, backend, 'running', now(), now()))

    def run(self, run_id):
        with self.lock:
            row = self.conn.execute('SELECT id,thread_id,query,backend,status FROM runs WHERE id=?',
                                    (run_id,)).fetchone()
            return dict(zip(['id', 'thread_id', 'query', 'backend', 'status'], row)) if row else None

    def pending(self, thread_id):
        with self.lock:
            rows = self.conn.execute("SELECT id FROM runs WHERE thread_id=? AND status!='completed' "
                                     'ORDER BY created', (thread_id,)).fetchall()
            return [self.run(r[0]) for r in rows]

    def status(self, run_id, status):
        with self.lock, self.conn:
            self.conn.execute('UPDATE runs SET status=?,updated=? WHERE id=?', (status, now(), run_id))

    def finish(self, run_id, reply, metadata, context):
        with self.lock, self.conn:
            run = self.run(run_id)
            if run['status'] == 'completed':
                return  # 체크포인트 종료 직후 재시도에도 대화를 중복 저장하지 않는다.
            for role, content, meta in [('user', run['query'], {}), ('assistant', reply, metadata)]:
                self.conn.execute('INSERT INTO messages (thread_id,role,content,created,metadata) '
                    'VALUES (?,?,?,?,?)', (run['thread_id'], role, content, now(), json.dumps(meta, ensure_ascii=False)))
            self.conn.execute('INSERT OR REPLACE INTO contexts VALUES (?,?)',
                              (run['thread_id'], json.dumps(context, ensure_ascii=False)))
            self.conn.execute("UPDATE runs SET status='completed',updated=? WHERE id=?", (now(), run_id))

    def close(self):
        self.conn.close()
