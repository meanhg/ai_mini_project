"""응답 형식과 CPU/메모리 인용 수치를 검사하는 결정론적 검증기.

자연어 전체의 진위를 판정하는 LLM 심판은 아니다. 지원하는 수치 표현을
수집한 모니터링 데이터와 대조하고, 검증 실패 시 원본 데이터로만 응답한다.
"""
import math
import re

_METRIC = re.compile(
    r"(?P<metric>CPU|메모리|memory|힙|heap|디스크|disk)\s*"
    r"(?:(?:사용률|사용량|usage|percent|은|는|이|가|현재|약|[:=])\s*)*"
    r"(?:\|\s*)?"
    r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_SYSTEM = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-(?:service|\d+)\b")
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)\s*%")


def valid_runbook(data: object) -> bool:
    return (isinstance(data, str) and bool(data.strip())
            and "runbook not found" not in data.casefold())


def validate_draft(draft: str, collected: list[dict]) -> list[str]:
    draft = draft.replace('**', '').replace('`', '')
    errors = []
    if len(draft.strip()) < 30:
        errors.append("응답을 30자 이상으로 구체적으로 작성하세요.")
    if not re.search(r"(?:^|\s)1\)", draft):
        errors.append("보고서를 1)부터 시작하는 번호 영역으로 작성하세요.")
    headings = [int(n) for n in re.findall(r'^\s*(\d+)\)\s', draft, re.M)]
    if headings and headings != list(range(1, len(headings) + 1)):
        errors.append('보고서 영역 번호를 1)부터 순서대로 작성하세요.')

    monitoring = {c["data"]["system_name"]: c["data"] for c in collected
                  if c.get("source") == "monitoring"
                  and isinstance(c.get("data"), dict)
                  and c["data"].get("system_name")
                  and c["data"].get("status") != "unknown"}
    if not monitoring:
        errors.append("확인된 모니터링 데이터가 없어 진단을 확정할 수 없습니다.")
    runbooks = [c["data"] for c in collected
                if c.get("source") == "runbook" and valid_runbook(c.get("data"))]
    if not runbooks:
        errors.append("유효한 대응 매뉴얼이 없습니다. 추가 확인이 필요합니다.")
    provided = {c['citation_id'] for c in collected if c.get('citation_id')}
    cited = set(re.findall(r'\[(RB:[a-z0-9-]+:\d+)\]', draft))
    if provided and not (provided & cited):
        errors.append('대응 절차에 제공된 [RB:문서:청크] 출처를 인용하세요.')
    if cited - provided:
        errors.append('조회하지 않은 문서 출처를 인용했습니다. 제공된 출처만 사용하세요.')

    # 런북의 조건부 임계값(예: CPU 90%를 넘으면)은 현재 측정값과 구분한다.
    thresholds = {float(m.group(1)) for body in runbooks for m in _PERCENT.finditer(body)}
    for match in _METRIC.finditer(draft):
        value = float(match["value"])
        metric = ('cpu_percent' if match['metric'].casefold() == 'cpu' else
                  'disk_percent' if match['metric'].casefold() in ('디스크', 'disk') else 'memory_percent')
        if match['metric'].casefold() in ('힙', 'heap'):
            errors.append('힙 사용률은 현재 수집한 메모리 사용률과 다른 지표입니다. 임의로 환산하지 마세요.')
            continue
        label = {'cpu_percent': 'CPU', 'disk_percent': '디스크', 'memory_percent': '메모리'}[metric]
        tail = re.split(r"[\n。;]|\.(?!\d)", draft[match.end():], maxsplit=1)[0]
        conditional = bool(re.search(r"넘|이상|이하|초과|미만|임계|threshold", tail[:30]))
        if conditional and value in thresholds:
            continue

        # 같은 줄에서 가장 가까이 앞서 등장한 시스템에 수치를 연결한다.
        prefix = draft[draft.rfind("\n", 0, match.start()) + 1:match.start()]
        named = [(m.start(), name) for name in monitoring
                 for m in re.finditer(rf'(?<![a-z0-9-]){re.escape(name)}(?![a-z0-9-])', prefix)]
        mentioned = list(_SYSTEM.finditer(prefix))
        target = max(named)[1] if named else mentioned[-1].group() if mentioned else None
        candidates = ([monitoring[target]] if target in monitoring else
                      list(monitoring.values()) if target is None else [])
        supported = any(isinstance(d.get(metric), (int, float)) and
                        math.isclose(value, d[metric], abs_tol=0.05, rel_tol=0)
                        for d in candidates)
        if not supported:
            scope = target or "조사 대상"
            errors.append(f"{scope}의 {label} {value:g}%는 수집된 측정값과 일치하지 않습니다.")
    return list(dict.fromkeys(errors))


def verified_fallback(collected: list[dict], reason='validation_failed') -> str:
    messages = {
        'validation_failed': '답변이 검증 기준을 통과하지 못했습니다.',
        'insufficient_evidence': '진단에 필요한 근거가 부족합니다.',
        'not_validated': '진단 보고서 검증을 완료하지 못했습니다.',
    }
    lines = [messages.get(reason, messages['not_validated']) + ' 확인된 모니터링 정보만 안내합니다.']
    for c in collected:
        d = c.get("data")
        if c.get("source") != "monitoring" or not isinstance(d, dict):
            continue
        if d.get("status") == "unknown":
            lines.append(f"- {d.get('system_name', '대상 시스템')}: 등록된 모니터링 정보가 없습니다.")
            continue
        lines.append(f"- {d.get('system_name')}: 상태={d.get('status')}, "
                     f"CPU {d.get('cpu_percent')}%, 메모리 {d.get('memory_percent')}% (출처={d.get('data_origin', 'sample')})")
    lines.append("원인과 조치를 확정할 수 없습니다. 담당자에게 추가 확인을 요청하세요.")
    return "\n".join(lines)
