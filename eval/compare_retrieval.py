"""동일한 증상·관측 자료에서 기존 단일 규칙과 실제 임베딩 검색의 문서 선택을 비교."""
import asyncio
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
os.environ['LANGCHAIN_TRACING_V2'] = 'false'
os.environ['LANGSMITH_TRACING'] = 'false'
from eval.legacy_retrieval import _classify_incident_type
from agent.rag import RunbookIndex
from agent.retrieval_policy import system_search_text


def metrics(expected, predicted):
    expected, predicted = set(expected), set(predicted)
    hit = len(expected & predicted)
    return {'recall': hit / len(expected), 'precision': hit / len(predicted) if predicted else 0,
            'exact_match': expected == predicted}


async def main():
    cases = json.loads((ROOT / 'eval/retrieval_cases.json').read_text())
    index = RunbookIndex()
    rows = []
    for case in cases:
        before = [_classify_incident_type([{'source': 'monitoring', 'data': d} for d in case['monitors']])]
        first, all_docs, searches = [], [], []
        for monitor in case['monitors']:
            query = system_search_text(case['query'], monitor, [m['system_name'] for m in case['monitors']])
            result = await index.search(query, int(os.getenv('RAG_TOP_K', '1')))
            matches = result['matches']
            searches.append(result.get('query_results', []))
            if matches:
                first.append(matches[0]['incident_type'])
            all_docs.extend(d['incident_type'] for d in matches)
        rows.append({'id': case['id'], 'expected': case['expected'], 'before': before,
                     'rag_first_per_system': sorted(set(first)), 'rag_context': sorted(set(all_docs)),
                     'before_metrics': metrics(case['expected'], before),
                     'after_metrics': metrics(case['expected'], first),
                     'context_metrics': metrics(case['expected'], all_docs), 'searches': searches})
    summary = {'case_count': len(rows), 'backend': index.embedder.backend, 'model': index.embedder.model}
    for phase in ['before', 'after', 'context']:
        for name in ['recall', 'precision', 'exact_match']:
            summary[f'{phase}_{name}'] = round(sum(row[f'{phase}_metrics'][name] for row in rows) / len(rows), 4)
    result = {'summary': summary, 'cases': rows,
              'limitation': '수동 작성한 12개 사례와 런북 6개 기준. 실제 운영 및 독립 평가 데이터의 성능을 보장하지 않습니다.'}
    (ROOT / 'eval/retrieval_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
