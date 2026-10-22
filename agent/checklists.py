"""검증된 진단의 권장 항목을 담당자의 수동 대응 체크리스트로 보관한다."""
import hashlib
import json
import re
import uuid
from .session_store import now

STATUSES = ('pending', 'in_progress', 'done', 'deferred')


def candidates(reply, metadata):
    """저장된 보고서에서 대상·출처가 일치하는 원문 절차만 추출한다."""
    if metadata.get('validation_passed') is not True:
        return []
    sources = {(s.get('citation_id'), s.get('system_name')): s
               for s in metadata.get('sources', [])}
    targets = {m.get('system_name') for m in metadata.get('monitoring', [])}
    result, seen, section = [], set(), ''
    for line in reply.splitlines():
        heading = re.match(r'^\s*\d+\)\s+(.+)$', line)
        if heading:
            section = heading[1]
        match = re.match(r'^\s*-\s*([^:\n]+):\s*(.+?)\s+\[(RB:[a-z0-9-]+:\d+)\]\s*$', line)
        if not match:
            continue
        target, text, citation = match.groups()
        source = sources.get((citation, target))
        if target not in targets or source is None:
            continue
        key = hashlib.sha256(json.dumps([target, text, citation], ensure_ascii=False).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        result.append({'key': key, 'system_name': target, 'text': text, 'section': section,
                       'citation_id': citation, 'source_title': source.get('title') or citation,
                       'source_path': source.get('path')})
    return result


class ChecklistStore:
    def __init__(self, sessions):
        self.sessions = sessions
        self.conn, self.lock = sessions.conn, sessions.lock
        with self.lock, self.conn:
            self.conn.execute('''CREATE TABLE IF NOT EXISTS checklists (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE, thread_id TEXT NOT NULL,
                query TEXT NOT NULL, diagnosis_created TEXT NOT NULL, created TEXT NOT NULL)''')
            self.conn.execute('''CREATE TABLE IF NOT EXISTS checklist_items (
                id TEXT PRIMARY KEY, checklist_id TEXT NOT NULL, system_name TEXT NOT NULL,
                text TEXT NOT NULL, section TEXT NOT NULL, citation_id TEXT NOT NULL,
                source_title TEXT NOT NULL, source_path TEXT, status TEXT NOT NULL DEFAULT 'pending',
                note TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
                FOREIGN KEY(checklist_id) REFERENCES checklists(id))''')
            self.conn.execute('CREATE INDEX IF NOT EXISTS checklist_items_parent ON checklist_items(checklist_id)')

    def _items(self, checklist_id):
        rows = self.conn.execute('''SELECT id,system_name,text,section,citation_id,source_title,
            source_path,status,note,updated FROM checklist_items WHERE checklist_id=? ORDER BY rowid''', (checklist_id,))
        keys = ['id', 'system_name', 'text', 'section', 'citation_id', 'source_title', 'source_path', 'status', 'note', 'updated']
        return [dict(zip(keys, row)) for row in rows]

    def list(self):
        with self.lock:
            rows = self.conn.execute('SELECT id,run_id,thread_id,query,diagnosis_created,created FROM checklists ORDER BY rowid DESC').fetchall()
            keys = ['id', 'run_id', 'thread_id', 'query', 'diagnosis_created', 'created']
            return [{**dict(zip(keys, row)), 'items': self._items(row[0])} for row in rows]

    def create(self, run_id, thread_id):
        with self.lock, self.conn:
            run = self.sessions.run(run_id)
            if not run or run['thread_id'] != thread_id:
                raise LookupError('원본 진단을 찾을 수 없습니다.')
            if run['status'] != 'completed':
                raise ValueError('완료된 진단만 체크리스트에 추가할 수 있습니다.')
            existing = self.conn.execute('SELECT id FROM checklists WHERE run_id=?', (run_id,)).fetchone()
            if existing:
                return {'id': existing[0], 'already_exists': True}
            # 체크 항목의 본문과 근거는 요청 데이터가 아닌 서버의 저장된 답변에서 가져온다.
            message = None
            for content, raw, created in self.conn.execute(
                    "SELECT content,metadata,created FROM messages WHERE thread_id=? AND role='assistant' ORDER BY rowid DESC", (thread_id,)):
                meta = json.loads(raw or '{}')
                if meta.get('run_id') == run_id:
                    message = content, meta, created
                    break
            if message is None:
                raise LookupError('저장된 원본 진단을 찾을 수 없습니다.')
            content, meta, diagnosis_created = message
            selected = candidates(content, meta)
            if not selected:
                raise ValueError('검증된 진단에 저장할 점검·조치 항목이 없습니다.')
            checklist_id, timestamp = str(uuid.uuid4()), now()
            self.conn.execute('INSERT INTO checklists VALUES (?,?,?,?,?,?)',
                              (checklist_id, run_id, thread_id, run['query'], diagnosis_created, timestamp))
            for item in selected:
                item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, run_id + ':' + item['key']))
                self.conn.execute('''INSERT INTO checklist_items
                    (id,checklist_id,system_name,text,section,citation_id,source_title,source_path,status,note,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (item_id, checklist_id, item['system_name'], item['text'], item['section'],
                    item['citation_id'], item['source_title'], item['source_path'], 'pending', '', timestamp))
            return {'id': checklist_id, 'already_exists': False}

    def update(self, checklist_id, item_id, status, note):
        if status not in STATUSES or not isinstance(note, str) or len(note) > 2000:
            raise ValueError('상태 또는 메모 값이 올바르지 않습니다.')
        with self.lock, self.conn:
            cursor = self.conn.execute('UPDATE checklist_items SET status=?,note=?,updated=? WHERE id=? AND checklist_id=?',
                                       (status, note, now(), item_id, checklist_id))
            if not cursor.rowcount:
                raise LookupError('체크 항목을 찾을 수 없습니다.')
            return next(item for item in self._items(checklist_id) if item['id'] == item_id)
