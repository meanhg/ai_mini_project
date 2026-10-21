"""고정된 수동 작성 사례로 기존/개선 검증기를 비교한다. LLM 품질 평가는 아니다."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.validation import validate_draft


def baseline_accepts(draft: str, attempt: int) -> bool:
    """변경 전 graph.py: len >= 30 and '1)' 포함, 3번째에는 강제 통과."""
    return (len(draft) >= 30 and "1)" in draft) or attempt >= 3


def compare():
    dataset = json.loads((ROOT / "eval" / "validation_cases.json").read_text())
    rows = []
    for case in dataset["cases"]:
        collected = dataset["collected"]
        if case.get("without_runbook"):
            collected = [c for c in collected if c["source"] != "runbook"]
        old = baseline_accepts(case["draft"], case.get("attempt", 1))
        errors = validate_draft(case["draft"], collected)
        rows.append({"id": case["id"], "expected_accept": case["expected_accept"],
                     "before_accept": old, "after_accept": not errors, "errors": errors})
    summary = {"case_count": len(rows),
               "before_correct": sum(r["before_accept"] == r["expected_accept"] for r in rows),
               "after_correct": sum(r["after_accept"] == r["expected_accept"] for r in rows),
               "note": "수동 작성한 고정 사례의 회귀 평가이며 실제 모델 정확도나 학습 효과를 의미하지 않습니다."}
    result = {"summary": summary, "cases": rows}
    (ROOT / "eval" / "validation_results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return all(r["after_accept"] == r["expected_accept"] for r in rows)


if __name__ == "__main__":
    raise SystemExit(0 if compare() else 1)
