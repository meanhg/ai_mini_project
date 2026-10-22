"""모델이 선택한 조사 대상을 사용자 입력·이전 문맥 범위로 제한한다."""
import re


def explicit_whole_scope(query):
    # 전체 조회는 모델의 boolean이 아니라 실제 사용자 표현으로 허용한다.
    text = re.sub(r'(?:전체|모두|전부|둘\s*다|모든)\s*(?:는\s*)?(?:말고|제외|아니)', '', query, flags=re.I)
    return bool(re.search(r'전체|모두|전부|모든|둘\s*다|두\s*(?:서버|서비스|시스템)|\b(?:all|both)\b', text, re.I))


def restrict_scope(query, context, targets, mentioned, registered):
    """확인 가능한 대상 근거가 없으면 조회 전에 반드시 질문한다."""
    explicit = set(mentioned)
    if 'local-host' in registered and re.search(r'내\s*(?:PC|컴퓨터|서버)|이\s*컴퓨터|로컬\s*호스트|\blocalhost\b', query, re.I):
        explicit.add('local-host')
    previous = set(context.get('targets', [])) & registered
    if explicit:
        selected = [t for t in targets if t in explicit]
        return (selected, '') if selected else ([], '확인할 시스템을 지정해주세요: ' + ', '.join(sorted(explicit)))
    if len(previous) > 1:
        if explicit_whole_scope(query):
            selected = [t for t in targets if t in previous]
            return (selected, '') if selected else ([], '이전 조사 대상 중 확인할 시스템을 지정해주세요.')
        return [], '이전 조사 대상 중 어떤 시스템을 확인할지 지정해주세요: ' + ', '.join(sorted(previous))
    if len(previous) == 1:
        selected = [t for t in targets if t in previous]
        return (selected, '') if selected else ([], '확인할 시스템을 지정해주세요.')
    # 새 대화에서 전체 시스템을 명시적으로 요청한 경우에만 전체 범위가 가능하다.
    if explicit_whole_scope(query) and re.search(r'서버|서비스|시스템|\bsystems?\b|\bservers?\b', query, re.I):
        return targets, ''
    return [], '확인할 시스템 이름을 알려주세요.'
