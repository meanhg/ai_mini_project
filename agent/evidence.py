"""원문 근거 단위를 선택하고 설정된 레이아웃으로 보고서를 조립한다."""
import re
from datetime import datetime, timedelta, timezone
from .documents import document_units
from .settings import policy
from .monitoring import collection_scope


def _cell(value):
    return str(value).replace('|', ' / ').replace('\n', ' ')


def _measured_at(value):
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is None:
            return '측정 시각의 시간대 미제공'
        return date.astimezone(timezone(timedelta(hours=9))).strftime('%m/%d %H:%M:%S KST')
    except (TypeError, ValueError, AttributeError):
        return '측정 시각 미제공'


def _status_summary(monitors):
    statuses = [d.get('status') for d in monitors]
    if not statuses or any(s not in ('healthy', 'degraded', 'down') for s in statuses):
        return '**판단: 확인되지 않은 대상이 있습니다.**'
    if 'down' in statuses:
        return '**판단: 중단 상태가 보고되었습니다.**'
    if 'degraded' in statuses:
        return '**판단: 주의가 필요한 관측이 있습니다.**'
    return '**판단: 현재 조회된 상태는 정상 범위입니다.**'


def runbook_steps(collected):
    result = {}
    for document in collected:
        if document.get('source') != 'runbook' or not document.get('citation_id'):
            continue
        citation = document['citation_id']
        used = set()
        for i, text in enumerate(document_units(document.get('data', '')), 1):
            match = re.match(r'^\s*(\d+)[.)]\s+', text)
            number = match[1] if match else str(i)
            if number in used:
                number = str(i) + '-' + str(len(used))
            used.add(number)
            text = re.sub(r'^\s*(?:\d+[.)]|[-*+])\s+', '', text).strip()
            key = f'{citation}:S{number}'
            if document.get('system_name'):
                key += '@' + document['system_name']
            result[key] = {'step_id': key, 'citation_id': citation, 'text': text,
                           'system_name': document.get('system_name')}
    return result


def render_report(report, collected):
    steps = runbook_steps(collected)
    if (not isinstance(report.get('diagnosis'), str) or not isinstance(report.get('action_steps'), list)
            or not isinstance(report.get('check_steps'), list)):
        return '보고서 구조가 올바르지 않습니다. 원인 후보와 원문 단계 ID를 다시 작성하세요.'
    selected = report['action_steps'] + report['check_steps']
    if any(not isinstance(key, str) or key not in steps for key in selected):
        return '조회한 대응 매뉴얼에 없는 단계가 선택되었습니다. 제공된 단계 ID만 선택하세요.'
    if re.search(r'\d+(?:\.\d+)?\s*(?:%|퍼센트|ms|GB|MB)', report['diagnosis'], re.I):
        return '원인 후보에 정량 수치를 반복하지 마세요. 측정값은 관측 정보에만 표시합니다.'
    abnormal = any(c.get('source') == 'monitoring' and
                   c.get('data', {}).get('status') != 'healthy' for c in collected)
    if abnormal and not selected:
        return '근거가 되는 점검 또는 대응 절차를 하나 이상 선택하세요.'
    if any(len(report[key]) > policy().max_selected_steps for key in ('action_steps', 'check_steps')):
        return '설정된 최대 근거 단계 수를 초과했습니다.'
    monitors = [c['data'] for c in collected if c.get('source') == 'monitoring' and isinstance(c.get('data'), dict)]
    parts = {'observations': ['| 대상 | 항목 | 측정값 |', '| --- | --- | --- |'] if monitors else [],
        'diagnosis': [_status_summary(monitors), report['diagnosis']], 'actions': [
        '매뉴얼의 조건을 담당자가 확인한 뒤 판단할 조치입니다. 자동 실행하지 않았습니다.'
        if report['action_steps'] else '조치 조건이 확인되지 않아 우선 아래 점검이 필요합니다.'
        if abnormal else '현재 관측값 기준으로 즉시 필요한 조치는 확인되지 않았습니다.'],
        'checks': [], 'limitations': [], 'sources': []}
    for d in monitors:
        for key, label in [('cpu_percent', 'CPU'), ('memory_percent', '메모리'), ('disk_percent', '디스크')]:
            value = f"{d[key]}%" if isinstance(d.get(key), (int, float)) else '미조회'
            parts['observations'].append(f"| {_cell(d['system_name'])} | {label} | {value} |")
    if monitors:
        parts['observations'].append('')
    for d in monitors:
        origin = {'local': '실제 로컬 측정', 'prometheus': '실제 Prometheus 조회', 'sample': '샘플 시나리오'}.get(d.get('data_origin'), '출처 미제공')
        status = {'healthy': '정상', 'degraded': '주의', 'down': '중단'}.get(d.get('status'), '확인 불가')
        parts['observations'].append(f"- {_cell(d['system_name'])}: {status} · {origin} · {_measured_at(d.get('last_checked'))}")
        scope = collection_scope(d)
        parts['limitations'].append(f"- {_cell(d['system_name'])} 자동 조회: " + '·'.join(scope['measured']) + '. 미조회 항목은 미확인 상태입니다.')
    if monitors:
        parts['limitations'].append('담당자 별도 확인: ' + collection_scope(monitors[0])['not_checked'] + '. 매뉴얼의 점검 지시가 확인 완료를 뜻하지 않습니다.')
    used = set()
    for field, destination in [('action_steps', 'actions'), ('check_steps', 'checks')]:
        for key in dict.fromkeys(report[field]):
            if key in used:
                continue
            used.add(key)
            step = steps[key]
            target = f"{step['system_name']}: " if step.get('system_name') else ''
            parts[destination].append(f"- {target}{step['text']} [{step['citation_id']}]")
    parts['sources'] = list(dict.fromkeys(f"- [{d['citation_id']}] {d.get('title', '')}" for d in collected if d.get('citation_id')))
    if not selected and parts['sources']:
        parts['actions'].append('참고한 매뉴얼: ' + ' '.join(dict.fromkeys(
            f"[{d['citation_id']}]" for d in collected if d.get('citation_id'))))
    samples = [c['data'].get('recent_samples', []) for c in collected if c.get('source') == 'monitoring']
    if any(samples):
        parts['limitations'].append('최근 표본은 요청 시점의 기록입니다. 지속 장애·메모리 누수를 확정할 수 없습니다.')
    lines = []
    for section in policy().report_sections:
        content = [line for kind in section.content for line in parts[kind] if line.strip()]
        if not content:
            continue
        number = sum(1 for line in lines if re.match(r'^\d+\) ', line)) + 1
        lines.append(f'{number}) {section.title}')
        lines.extend(content)
        lines.append('')
    return '\n'.join(lines).strip()
