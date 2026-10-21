"""샘플, 실제 로컬 호스트, 사용자가 지정한 Prometheus를 구분해 읽는다."""
import asyncio
import json
import math
import os
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil
from .settings import policy

ROOT = Path(__file__).resolve().parents[1]


def collection_scope(data):
    """응답에 실제 들어온 지표와 자동으로 확인하지 않은 항목을 구분한다."""
    names = {'cpu_percent': 'CPU', 'memory_percent': '시스템 메모리', 'disk_percent': '디스크'}
    measured = [label for key, label in names.items()
                if isinstance(data.get(key), (int, float)) and not isinstance(data[key], bool)]
    return {'measured': measured,
            'not_checked': 'p95 지연시간·커넥션 풀·로그·배포 이력·프로세스별 사용량·호출 경로',
            'history': '최근 표본은 요청 시점에만 저장되며 정기 수집 이력이 아닙니다.'}


def sources():
    path = os.getenv('MONITORING_SOURCES_PATH', '')
    if not path:
        return {}
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError('모니터링 대상 설정은 객체여야 합니다.')
    return data


def known_systems():
    mode = os.getenv('MONITORING_BACKEND', 'hybrid')
    if mode not in ('sample', 'local', 'hybrid', 'prometheus'):
        raise ValueError('MONITORING_BACKEND 설정을 확인하세요.')
    names = set(json.loads((ROOT / 'data/monitoring.json').read_text())) if mode in ('sample', 'hybrid') else set()
    if mode in ('local', 'hybrid'):
        names.add('local-host')
    return names | set(sources())


def warning_alerts(values, system_name, overrides=None):
    settings = policy()
    thresholds = {**settings.warning_thresholds, **settings.system_thresholds.get(system_name, {}), **(overrides or {})}
    for value in thresholds.values():
        if not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError('모니터링 경고 임계값은 0~100이어야 합니다.')
    alerts = [f'{key} warning threshold exceeded' for key, threshold in thresholds.items()
              if isinstance(values.get(key), (int, float)) and values[key] >= threshold]
    return alerts, thresholds


def _local():
    cpu = psutil.cpu_percent(interval=0.25)
    memory = psutil.virtual_memory().percent
    disk = psutil.disk_usage('/').percent
    values = {'cpu_percent': cpu, 'memory_percent': memory, 'disk_percent': disk}
    alerts, thresholds = warning_alerts(values, 'local-host')
    return {**values, 'status': 'degraded' if alerts else 'healthy',
            'alerts': alerts, 'warning_thresholds': thresholds, 'data_origin': 'local'}



async def _prometheus(config, system_name="prometheus"):
    endpoint = os.getenv('PROMETHEUS_URL', '').rstrip('/')
    if not endpoint:
        raise RuntimeError('PROMETHEUS_URL이 없습니다. 샘플로 대신 응답하지 않습니다.')
    headers = {}
    token = os.getenv('PROMETHEUS_TOKEN', '')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    queries = config['queries']
    if not all(k in queries for k in ('up', 'cpu_percent', 'memory_percent')):
        raise ValueError('up, cpu_percent, memory_percent PromQL이 필요합니다.')
    async with httpx.AsyncClient(timeout=12) as client:
        async def read(key, query):
            response = await client.get(endpoint + '/api/v1/query', params={'query': query}, headers=headers)
            if response.status_code != 200:
                raise RuntimeError('Prometheus 조회 실패. 연결과 권한을 확인하세요.')
            body = response.json()
            result = body.get('data', {}).get('result', [])
            if body.get('status') != 'success' or len(result) != 1 or 'value' not in result[0]:
                raise RuntimeError('PromQL은 대상별 단일 값으로 집계해야 합니다. 비어 있거나 여러 시계열입니다.')
            timestamp, value = result[0]['value']
            value = float(value)
            if not math.isfinite(value) or abs(time.time() - float(timestamp)) > 120:
                raise RuntimeError('Prometheus 측정값이 오래되었거나 유효하지 않습니다.')
            if key != 'up' and not 0 <= value <= 100:
                raise RuntimeError('사용률 PromQL은 0~100 백분율이어야 합니다.')
            return key, value
        values = dict(await asyncio.gather(*(read(k, v) for k, v in queries.items())))
    up = values.pop('up')
    if up not in (0, 1):
        raise RuntimeError('up 쿼리는 0 또는 1이어야 합니다.')
    alerts, thresholds = warning_alerts(values, system_name, config.get('warning_thresholds'))
    return {**values, 'status': 'down' if up == 0 else 'degraded' if alerts else 'healthy',
            'alerts': alerts, 'warning_thresholds': thresholds, 'data_origin': 'prometheus'}


def remember(data):
    path = Path(os.getenv('MONITORING_HISTORY_PATH', str(ROOT / 'data/monitoring_history.sqlite3')))
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS snapshots (system TEXT, timestamp TEXT, data TEXT)')
        conn.execute('INSERT INTO snapshots VALUES (?,?,?)',
                     (data['system_name'], data['last_checked'], json.dumps(data)))
        rows = conn.execute('SELECT data FROM snapshots WHERE system=? ORDER BY rowid DESC LIMIT 5',
                            (data['system_name'],)).fetchall()
        conn.execute('DELETE FROM snapshots WHERE rowid NOT IN (SELECT rowid FROM snapshots '
                     'ORDER BY rowid DESC LIMIT 10000)')
    return [json.loads(r[0]) for r in reversed(rows)]


async def get_status(system_name):
    if system_name not in known_systems():
        raise ValueError('등록되지 않은 모니터링 대상입니다.')
    configured = sources()
    if system_name in configured:
        data = await _prometheus(configured[system_name], system_name)
    elif system_name == 'local-host':
        data = await asyncio.to_thread(_local)
    else:
        base = json.loads((ROOT / 'data/monitoring.json').read_text())[system_name]
        jitter = random.uniform(-3, 8)
        data = {'status': base['status'], 'cpu_percent': round(min(100, max(0, base['cpu_base'] + jitter)), 1),
                'memory_percent': round(min(100, max(0, base['memory_base'] + jitter / 2)), 1),
                'alerts': base.get('alerts', []), 'data_origin': 'sample'}
    data.update(system_name=system_name, last_checked=datetime.now(timezone.utc).isoformat())
    data['recent_samples'] = await asyncio.to_thread(remember, data)
    return data
