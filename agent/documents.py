"""번호·글머리표·일반 문단에서 원문 단위를 보존한다."""
import re
import hashlib

REPORT_ERROR_PREFIX = "보고서 생성 오류: "


def citation_name(name):
    return name if re.fullmatch(r'[a-z0-9-]+', name) else hashlib.sha256(name.encode()).hexdigest()[:16]


def document_units(body):
    lines = body.strip().splitlines()
    units, block = [], []
    def flush():
        if block:
            text = '\n'.join(block).strip()
            if text:
                units.append(text)
            block.clear()
    list_mode = False
    for line in lines:
        if re.match(r'^\s*#{1,6}\s', line):
            flush()
            list_mode = False
            continue
        if re.match(r'^\s*(?:\d+[.)]|[-*+])\s+', line):
            flush()
            list_mode = True
        elif not line.strip() and not list_mode:
            flush()
            continue
        block.append(line)
    flush()
    return units
