"""답변 깊이와 후속 제안을 현재 요청·실제 관측 범위에 맞춘다."""
import math
import re
from datetime import datetime
from .settings import policy
from .monitoring import known_systems


def request_context(collected):
    return next((c['data'] for c in collected if c.get('source') == 'request_context'), {})


def observations(collected):
    return [c['data'] for c in collected if c.get('source') == 'monitoring'
            and isinstance(c.get('data'), dict)]


def normal_observations(collected):
    values = observations(collected)
    if not values:
        return False
    for data in values:
        if data.get('status') != 'healthy' or data.get('alerts'):
            return False
        thresholds = {**policy().warning_thresholds,
                      **policy().system_thresholds.get(data.get('system_name'), {}),
                      **data.get('warning_thresholds', {})}
        measured = False
        for key, threshold in thresholds.items():
            value = data.get(key)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
            if not 0 <= value < threshold:
                return False
            measured = True
        if not measured:
            return False
    return True


def concise_status(collected):
    context = request_context(collected)
    return (context.get('request_kind') == 'status' and context.get('reported_symptoms') is False
            and normal_observations(collected))


def report_policy_errors(report, collected):
    if not concise_status(collected):
        return []
    errors = []
    if report.get('action_steps') or report.get('check_steps'):
        errors.append('현재 관측이 정상인 단순 상태 조회입니다. 조치·점검 배열을 비우세요. 매뉴얼을 읽었다는 이유만으로 힙 덤프·배포 점검을 권하지 마세요.')
    if re.search(r'누수|OOM|힙|덤프|원인.{0,6}(?:의심|가능)|장애.{0,6}가능', report.get('diagnosis', ''), re.I):
        errors.append('정상 범위의 사용률과 요청 시점 표본 변동만으로 장애·누수·OOM 가능성을 제기하지 마세요. 현재 관측 범위의 상태만 설명하세요.')
    return errors


def previous_sample(data):
    """동일 출처이며 현재 측정보다 앞선 표본만 비교한다."""
    try:
        now = datetime.fromisoformat(data['last_checked'].replace('Z', '+00:00'))
        candidates = []
        for sample in data.get('recent_samples', []):
            try:
                when = datetime.fromisoformat(sample['last_checked'].replace('Z', '+00:00'))
                if (when < now and sample.get('system_name') == data.get('system_name')
                        and sample.get('data_origin') == data.get('data_origin')):
                    candidates.append((when, sample))
            except (KeyError, ValueError, TypeError):
                continue
        return max(candidates, key=lambda row: row[0])[1] if candidates else None
    except (KeyError, ValueError, TypeError):
        return None


def followup_suggestions(result):
    if result.get('route') != 'incident':
        return []
    if any(e['type'] == 'ClarificationRequired' for e in result.get('events', [])):
        previous = result.get('conversation_context', {}).get('targets', [])
        choices = [t for t in previous if t in known_systems()]
        if not choices and 'local-host' in known_systems():
            return [{'label': '내 PC 상태 확인', 'query': 'local-host CPU와 메모리 상태 확인'}]
        return [{'label': t, 'query': t} for t in choices[:2]]
    if result.get('validation_passed') is not True or result.get('evidence_gaps'):
        return []
    monitors = observations(result.get('collected', []))
    targets = list(dict.fromkeys(m['system_name'] for m in monitors
                                 if m.get('status') != 'unknown' and m.get('system_name') in known_systems()))
    if not targets:
        return []
    names = ', '.join(targets)
    suggestions = [{'label': '현재 상태 다시 확인',
                    'query': names + ' 현재 CPU·메모리·디스크 상태를 다시 확인해줘'}]
    if any(previous_sample(m) for m in monitors):
        suggestions.append({'label': '이전 표본과 비교',
                            'query': names + ' 현재 측정값을 이전에 저장된 표본과 비교해줘'})
    return suggestions
