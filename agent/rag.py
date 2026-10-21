"""런북 청크의 임베딩을 SQLite에 캐시하고 코사인 유사도로 검색한다."""
import asyncio
import hashlib
import json
import math
import os
import re
import sqlite3
from collections import OrderedDict
from pathlib import Path

import httpx
from .retrieval_policy import expand_queries
from .documents import document_units, citation_name
from .settings import policy, runbook_directory

ROOT = Path(__file__).resolve().parents[1]


class Embedder:
    def __init__(self, backend=None):
        self.backend = backend or os.getenv('RAG_EMBEDDING_BACKEND', 'ollama')
        defaults = {'ollama': 'qwen3-embedding:0.6b', 'openai': 'text-embedding-3-small',
                    'local': 'character-ngram-test-v1'}
        if self.backend not in defaults:
            raise ValueError('지원하지 않는 임베딩 설정입니다.')
        self.model = os.getenv(f'{self.backend.upper()}_EMBEDDING_MODEL', defaults[self.backend])

    async def embed(self, texts):
        if self.backend == 'local':
            # 오프라인 테스트용 해시 벡터. 학습된 의미 임베딩으로 표시하지 않는다.
            vectors = []
            for text in texts:
                v = [0.0] * 512
                normalized = re.sub(r'\s+', ' ', text.casefold())
                for n in (2, 3):
                    for i in range(len(normalized) - n + 1):
                        h = int(hashlib.sha256(normalized[i:i+n].encode()).hexdigest()[:8], 16)
                        v[h % len(v)] += 1
                vectors.append(v)
            return vectors
        if self.backend == 'openai':
            key = os.getenv('OPENAI_API_KEY', '')
            if not key:
                raise RuntimeError('OpenAI 임베딩 API 키가 없습니다.')
            url = 'https://api.openai.com/v1/embeddings'
            headers = {'Authorization': f'Bearer {key}'}
            payload = {'model': self.model, 'input': texts}
        else:
            url = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/') + '/api/embed'
            headers = {}
            payload = {'model': self.model, 'input': texts, 'keep_alive': '10m'}
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code >= 400:
                    raise RuntimeError(f'{self.backend} 임베딩 요청 실패(HTTP {response.status_code}). 모델과 설정을 확인하세요.')
                data = response.json()
        except httpx.HTTPError:
            raise RuntimeError(f'{self.backend} 임베딩 서버에 연결할 수 없습니다.') from None
        vectors = ([d['embedding'] for d in sorted(data['data'], key=lambda x: x['index'])]
                   if self.backend == 'openai' else data['embeddings'])
        if len(vectors) != len(texts) or not all(v and all(math.isfinite(x) for x in v) for v in vectors):
            raise RuntimeError('임베딩 응답의 벡터 개수 또는 값이 올바르지 않습니다.')
        return vectors


def cosine(a, b):
    if len(a) != len(b):
        raise ValueError('임베딩 벡터 차원이 다릅니다.')
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b)) / norm if norm else 0.0


class RunbookIndex:
    def __init__(self, directory=None, db_path=None, embedder=None):
        self.directory = Path(directory or runbook_directory())
        self.db_path = Path(db_path or os.getenv('RAG_INDEX_PATH', str(ROOT / 'data/rag_index.sqlite3')))
        self.embedder = embedder or Embedder()
        self._lock = asyncio.Lock()
        self._queries = OrderedDict()
        self._plans = OrderedDict()
        self._planner = None

    def chunks(self):
        result = []
        if not self.directory.exists():
            return result
        for path in sorted(p for p in self.directory.iterdir() if p.is_file() and p.suffix.lower() in ('.md', '.txt')):
            body = path.read_text(encoding='utf-8').strip()
            if not body:
                continue
            title = body.splitlines()[0].lstrip('# ').strip()
            # 번호가 붙은 단계와 그 조건은 한 덩어리로 보존한다.
            units = document_units(body)
            sections, buffer = [], ''
            for unit in units:
                if buffer and len(buffer) + len(unit) > policy().chunk_chars:
                    sections.append(buffer.strip())
                    buffer = ''
                buffer += '\n' + unit
            if buffer.strip():
                sections.append(buffer.strip())
            for i, section in enumerate(sections, 1):
                content = section
                if not content.strip():
                    continue
                identity = path.stem if sum(p.stem == path.stem for p in self.directory.iterdir() if p.is_file()) == 1 else path.name
                result.append({'citation_id': f'RB:{citation_name(identity)}:{i}', 'title': title,
                               'incident_type': path.stem, 'path': str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                               'content': content, 'embedding_text': f'{title}\n{content}'})
        return result

    async def _vectors(self, chunks):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute('CREATE TABLE IF NOT EXISTS vectors '
                               '(cache_key TEXT PRIMARY KEY, vector TEXT NOT NULL)')
            keys = [hashlib.sha256((self.embedder.backend + ':' + self.embedder.model + ':'
                    + c['embedding_text']).encode()).hexdigest() for c in chunks]
            cached = {}
            for key in keys:
                row = connection.execute('SELECT vector FROM vectors WHERE cache_key=?', (key,)).fetchone()
                if row:
                    cached[key] = json.loads(row[0])
            missing = [i for i, key in enumerate(keys) if key not in cached]
            if missing:
                vectors = await self.embedder.embed([chunks[i]['embedding_text'] for i in missing])
                for i, vector in zip(missing, vectors):
                    cached[keys[i]] = vector
                    connection.execute('INSERT OR REPLACE INTO vectors VALUES (?,?)',
                                       (keys[i], json.dumps(vector)))
                connection.commit()
            return [cached[key] for key in keys]
        finally:
            connection.close()

    async def _search_queries(self, query):
        if self.embedder.backend == 'local' or os.getenv('LLM_BACKEND') == 'fake':
            return expand_queries(query)
        catalog = sorted(set(c['title'] for c in self.chunks()))
        plan_key = (query, tuple(catalog))
        if plan_key not in self._plans:
            if self._planner is None:
                backend = os.getenv('LLM_BACKEND', 'openai')
                if backend == 'ollama':
                    from .ollama_llm import OllamaFastLLM
                    self._planner = OllamaFastLLM(os.getenv('OLLAMA_FAST_MODEL', 'qwen3.5:2b'))
                else:
                    from .openai_llm import OpenAIFastLLM
                    self._planner = OpenAIFastLLM(os.getenv('OPENAI_FAST_MODEL', 'gpt-4o-mini'))
            queries, _ = await self._planner.search_queries(query, catalog=catalog)
            self._plans[plan_key] = queries
        result = self._plans[plan_key]
        while len(self._plans) > policy().query_cache_size:
            self._plans.popitem(last=False)
        return result

    async def search(self, query, top_k=2, queries=None):
        if not query.strip():
            return {'matches': [], 'query_results': [], 'backend': self.embedder.backend, 'model': self.embedder.model}
        async with self._lock:
            chunks = self.chunks()
            if not chunks:
                return {'matches': [], 'query_results': [], 'backend': self.embedder.backend, 'model': self.embedder.model}
            vectors = await self._vectors(chunks)
            automatic = not queries
            queries = queries or await self._search_queries(query)
            queries = list(dict.fromkeys(q.strip() for q in queries if isinstance(q, str) and q.strip()))[:policy().max_search_queries]
            if not queries:
                queries = [query]
            missing = [q for q in queries if q not in self._queries]
            if missing:
                for q, v in zip(missing, await self.embedder.embed(missing)):
                    self._queries[q] = v
            minimum = float(os.getenv('RAG_MIN_SCORE', '0' if self.embedder.backend == 'local' else '0.35'))
            found, query_results = {}, []
            for q in queries:
                ranked = sorted(((cosine(self._queries[q], v), c) for c, v in zip(chunks, vectors)),
                                key=lambda pair: pair[0], reverse=True)
                hits = []
                for score, chunk in ranked[:max(1, min(top_k, policy().max_results_per_query))]:
                    if score < minimum:
                        continue
                    match = {**{k: v for k, v in chunk.items() if k != 'embedding_text'}, 'score': round(score, 5)}
                    key = chunk['citation_id']
                    hits.append(key)
                    if key not in found or found[key]['score'] < score:
                        found[key] = match
                # 다른 질의로 찾은 문서도 이 질의를 실제 유사도 기준으로 뒷받침할 수 있다.
                # 후보 선택(top-k)과 근거 충족 판정(보존 문서 전체)을 구분한다.
                query_results.append({'query': q, 'citations': [c['citation_id'] for score, c in ranked if score >= minimum],
                                      'first_candidate': hits[0] if hits else None})
            # 각 질의의 첫 근거를 우선 확보해 한 증상의 다수 청크가 다른 증상을 밀어내지 않는다.
            order = list(dict.fromkeys([r['first_candidate'] for r in query_results if r['first_candidate']]
                    + sorted(found, key=lambda key: found[key]['score'], reverse=True)))
            matches = [found[key] for key in order[:policy().max_search_results]]
            if automatic and self._planner is not None:
                matches, _ = await self._planner.select_evidence(query, matches)
            retained = {m['citation_id'] for m in matches}
            for row in query_results:
                row['citations'] = [c for c in row['citations'] if c in retained]
            while len(self._queries) > policy().query_cache_size:
                self._queries.popitem(last=False)
            return {'matches': matches, 'backend': self.embedder.backend,
                    'model': self.embedder.model, 'document_count': len(chunks),
                    'query_count': len(queries), 'query_results': query_results, 'min_score': minimum}
