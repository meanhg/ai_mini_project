# 운영 인시던트 대응 AI 에이전트 — MVP

**서비스 장애 증상을 입력하면, 모니터링 상태와 대응 매뉴얼을 조회해 원인 후보와 대응 절차를 설명하는 AI 채팅 서비스입니다.**

이 문서 하나에서 프로젝트 목적, 현재 완성 범위, 구조, 동작, 실행 방법과 확장 위치를 확인할 수 있습니다. 기존 분석·실행 안내·개선 기록은 이 문서로 통합했으며 원본은 `doc/archive/`에 보관했습니다.

## 1. 무엇을 만들고 있나

운영 담당자는 서비스가 느려지거나 CPU·메모리 사용량이 올라가면 서버 상태와 대응 문서를 따로 확인해야 합니다. 이 프로젝트는 그 조사 과정을 채팅 한 번으로 연결하는 것을 목적으로 합니다.

```text
사용자: payments-service CPU 급증 원인을 진단해줘

에이전트:
  1) 현재 상태·CPU·메모리 확인
  2) CPU 대응 매뉴얼을 찾아 원인 후보와 조건부 대응 안내
  3) 추가로 확인할 항목과 참고한 문서 표시
```

현재 목표는 **추가 기능을 확장할 수 있는 정상 작동하는 베이스 MVP**입니다. 프로젝트 가이드의 ‘Base AI Project 개선’ 방식을 선택했으며, 다음 핵심 흐름을 완성 범위로 삼았습니다.

**질문 입력 → AI 요청 판단 → MCP 자료 조회 → RAG 근거 검색 → 답변 생성·검증 → Vue 화면 표시**

실제 시스템에 명령을 내려 재시작하거나 배포를 롤백하는 서비스는 아닙니다. 조사 결과와 대응 후보를 안내하며 실행 판단은 사용자가 합니다.

## 2. 지금 사용할 수 있는 기능

| 기능 | 사용자에게 보이는 동작 |
| --- | --- |
| 장애 진단 채팅 | 서비스 이름과 증상을 입력하면 조사 결과와 대응 절차 제공 |
| 모델 선택 | OpenAI / Ollama / 데모 중 선택 |
| 모니터링 조회 | 샘플 서비스 상태 또는 이 PC의 실제 사용률 조회 |
| 매뉴얼 검색 | 증상과 관련된 문서를 찾아 답변에 출처 표시 |
| 복수 대상·복합 증상 | 여러 서비스 또는 같은 서비스의 여러 증상을 조사 |
| 진행 상황 | 계획·조회·검색·작성·검증 과정을 화면에 표시 |
| 대화 유지 | 새로고침·서버 재시작 후 같은 대화 복원 |
| 후속 질문 | 요청과 이전 조사 문맥을 모델이 해석해 생략된 대상 연결. 모호하면 확인 질문 |
| 새 대화·예제 질문 | 이전 기록을 삭제하지 않고 새 대화를 시작하거나 예제를 바로 실행 |
| 실패 처리 | 조회 실패·검증 실패를 알리고, 중단 작업은 이어서 실행하거나 종료 |

채팅과 근거 기반 진단이 현재 MVP의 중심입니다. 이전 개선에서 구현한 복구·의미 검증·Prometheus 연결부 등은 유지하지만 추가 기능 확장의 선행 과제로 더 고도화하지 않습니다.

## 3. 가장 빠르게 실행하기

프로젝트 폴더: `/Users/user/mini-project/all_total_vue_student`

### 현재 컴퓨터

실행 환경과 `.env`가 준비되어 있습니다. 프로젝트 폴더에서 다음 명령을 사용합니다.

```bash
cd /Users/user/mini-project/all_total_vue_student
bash sh/run_live.sh
```

- 채팅 화면: [http://localhost:8600](http://localhost:8600)
- API 설명·직접 요청: [http://localhost:8100/docs](http://localhost:8100/docs)
- 서버 준비 확인: [http://localhost:8100/health](http://localhost:8100/health)

백엔드 준비를 확인한 뒤 화면 서버를 시작합니다. 실행 중인 프로젝트가 있다면 기존 화면을 사용하면 됩니다. 종료는 실행한 터미널에서 `Ctrl+C`입니다. 대화 DB는 삭제하지 않습니다.

### 처음 받는 컴퓨터

Python 3.11 이상을 기준으로 환경을 준비합니다.

```bash
bash sh/setup_venv.sh
cp .env.example .env
```

이미 `.env`가 있다면 덮어쓰지 않고 필요한 설정만 수정합니다. 실제 키는 `.env`에 입력하며 `.env.example`은 공유용 빈 양식입니다.

기본 설정은 OpenAI 답변 + Ollama 임베딩입니다. 따라서 OpenAI 키와 로컬 Ollama 임베딩 모델이 모두 필요합니다.

| 용도 | 기본 모델 |
| --- | --- |
| OpenAI 분류·계획·일반 대화 | `gpt-4o-mini` |
| OpenAI 문맥 해석·보고서·의미 검증 | `gpt-4o` |
| Ollama 분류·계획·일반 대화 | `qwen3.5:2b` |
| Ollama 문맥 해석·보고서·의미 검증 | `qwen3:8b` |
| 기본 RAG 임베딩 | Ollama `qwen3-embedding:0.6b` |

Ollama를 사용하는 설정에서는 Ollama를 실행하고 필요한 모델을 준비합니다. 이미 실행 중이면 `ollama serve`를 다시 실행할 필요가 없습니다.

```bash
ollama serve
# 다른 터미널에서 필요한 모델만 받기
ollama pull qwen3-embedding:0.6b
ollama pull qwen3.5:2b
ollama pull qwen3:8b
```

OpenAI만으로 사용하려면 `.env`의 `RAG_EMBEDDING_BACKEND=openai`로 지정할 수 있습니다. 이때 채팅과 임베딩 모두 API 사용량에 따라 비용이 발생합니다. 화면에서 Ollama 답변을 선택하려면 로컬 채팅 모델도 필요합니다.

API 키나 Ollama 없이 동작 흐름만 확인할 때는 아래 데모를 사용합니다. 실제 MCP와 그래프를 실행하되, 답변은 규칙 기반이고 검색 벡터는 테스트용입니다.

```bash
bash sh/run_demo.sh
```

실제 모델 확인은 `run_live.sh`, 외부 모델 없는 흐름 확인은 `run_demo.sh`로 구분하면 됩니다. Vue는 CDN을 사용하므로 현재 화면은 Vue 라이브러리를 처음 불러올 때 인터넷 연결이 필요합니다.

## 4. 1분 사용 순서

1. 화면을 열고 답변 모델을 선택합니다. 기본은 `.env`의 `LLM_BACKEND`입니다.
2. 이전 기록이 있다면 **새 대화**를 누릅니다. 기존 DB 기록은 보존합니다.
3. **CPU 문제 진단** 예제를 누르거나 `payments-service CPU 급증 원인을 진단해줘`를 입력합니다.
4. 조사 진행 표시가 끝나면 관측 정보·원인 후보·대응 절차·참고 매뉴얼을 확인합니다.
5. `그 서버 다시 확인해줘`로 같은 대상을 재조회합니다.

추가 예제:

| 입력 | 확인할 동작 |
| --- | --- |
| `cache-service 메모리 사용량이 계속 증가합니다` | 메모리 대응 매뉴얼 검색 |
| `payments-service CPU 급증과 cache-service 메모리 증가를 함께 진단해줘` | 대상별 조사와 근거 연결 |
| `payments-service CPU 급증과 메모리 누수를 함께 진단해줘` | 같은 대상의 복합 증상 검색 |
| `local-host CPU와 메모리 상태를 확인해줘` | 현재 PC의 실제 지표 조회 |
| `billing-service 장애를 진단해줘` | 미등록 대상을 임의로 바꾸지 않고 이름 확인 |
| `안녕` | 장애 조사 단계를 거치지 않는 일반 대화 |

보고서는 검증 후 표시합니다. 기본 추가 조사 1회·답변 작성 3회이며 `data/agent_policy.json`에서 조정할 수 있습니다. 요청한 대상·증상별 근거가 부족하면 그 사실을 표시하고 관측 정보만 안내합니다.

## 5. 전체 구조

```text
브라우저: web_chat.html (Vue 3)
  │ 질문 전송 / 진행 상황·답변 표시
  ▼
web_demo_server.py :8600
  │ HTML 제공 + API 요청 중계
  ▼
server.py :8100 (FastAPI)
  ├─ 모델 선택·대화 이력·실행 저장
  └─ agent/graph.py (LangGraph)
       ├─ AI 모델: OpenAI / Ollama / fake
       └─ agent/mcp_client.py
            ├─ ops MCP: 모니터링·RAG·런북·표준 진단 절차
            └─ docs MCP: 다중 서버 탐색을 위한 기존 실습 서버

저장: SQLite 대화·실행 상태 / 문서 벡터 캐시 / 측정 이력
자료: data/monitoring.json + data/runbooks/*.md
```

| 기술 | 이 프로젝트에서 하는 일 |
| --- | --- |
| Vue 3 | 채팅창, 모델 선택, 진행 상황과 결과 표시. 별도 npm/Vite 빌드 없음 |
| FastAPI | 화면 제공·요청 중계와 백엔드 API |
| LangGraph | 조사 단계, 분기, 제한된 반복을 제어 |
| OpenAI / Ollama | 질문 분류, 계획, 비교 분석, 일반 대화, 보고서 작성 |
| MCP | 도구·자료·프롬프트를 공통 방식으로 호출 |
| RAG | 관련 문서를 먼저 검색해 모델이 근거를 참고하도록 함 |
| SQLite | 대화·체크포인트·벡터·측정값 저장 |
| LangSmith | 설정했을 때 실행 과정을 추적하는 선택 기능 |

Supervisor 안에 여러 역할의 노드가 있는 구조입니다. MCP 서버 2개가 각각 독립적인 AI 에이전트라는 뜻은 아닙니다.

## 6. 질문 하나가 처리되는 과정

```text
route: 일반 대화인가, 장애 요청인가?
  ├─ 일반 대화 → chat_answer → 답변·기록 저장
  └─ 장애 요청
       plan → gate → research(관측) → prepare_evidence → research(매뉴얼)
                         ↑                                  │
                         └──── replan_check ←───────────────┘
                              자료 부족 시 제한된 추가 조사
       → analysis(비교가 필요할 때) → write → validate
                                     ↑        │ 반려 시 설정된 작성 상한 내 재작성
                                     └────────┘
       → finalize → 답변·출처·기록 저장 → 화면 표시
```

| 단계 | 쉬운 설명 |
| --- | --- |
| `route` | 질문의 종류 판단 |
| `plan` | 명시된 시스템을 조사할 작업 목록 작성 |
| `gate` | 필요 없는 비교 분석 작업 제거 |
| `research` | MCP로 모니터링 또는 매뉴얼 검색 실행 |
| `prepare_evidence` | 관측 후 첫 매뉴얼 조회 구성. 추가 조사 횟수를 소비하지 않음 |
| `replan_check` | 요청한 대상·증상별 관측과 문서 근거를 확인하고 부족한 부분만 추가 조사 |
| `analysis` | 여러 시스템의 관측 결과 비교. 관련 작업이 있을 때 실행 |
| `write` | 원인 후보와 인용할 매뉴얼 원문 단위 선택 |
| `validate` | 형식·측정값·출처 확인. 설정에 따라 의미 검증도 실행 |
| `finalize` | 검증된 보고서 또는 관측 정보만 제공하는 대체 답변 확정 |

시스템을 특정할 수 없으면 재질문합니다. 일반 대화는 토큰을 중계하고, 장애 보고서는 미검증 초안을 공개하지 않습니다. SSE는 이 진행 상황과 결과를 브라우저로 보내는 통신 방식입니다.

### RAG와 답변 작성

1. `RUNBOOK_DIRECTORY`(기본 `data/runbooks/`)의 Markdown·TXT를 목록·문단 단위로 나눕니다. 문서를 추가하면 다음 검색에 반영합니다.
2. 임베딩 모델로 문서를 숫자 벡터로 바꾸고 SQLite에 캐시합니다.
3. 실제 모델이 현재 요청·대화 문맥을 해석하고 계획 단계에서 대상별·증상별 검색 질의를 만듭니다. 고정된 6개 장애 유형으로 매핑하지 않습니다.
4. 유사도가 높은 문서 본문·문서 ID·출처를 모델에 제공합니다.
5. 검색 후보는 모델이 원래 증상과의 직접 관련성을 다시 확인합니다. 보고서 모델은 보존한 근거에서 원인 후보와 원문 단위 ID를 선택합니다.
6. 코드는 실제 관측값과 선택한 원문을 조합하고, 정책 파일의 영역 순서·제목에 따라 보고서를 표시합니다.

따라서 ‘CPU가 일정 수준을 넘고 지속되면’ 같은 조건을 모델이 임의로 요약하지 않습니다. `[RB:cpu-spike:1]`은 조회한 매뉴얼의 청크 ID입니다. 검색·검증이 모든 원인과 조치의 타당성을 보장하는 것은 아닙니다.

## 7. 데이터는 어디서 오나

| 대상 | 데이터 | 표현하는 상황 |
| --- | --- | --- |
| `auth-service` | 샘플 | 로그인 응답 지연·연결 풀 문제 |
| `web-01` | 샘플 | 정상 웹 서버 |
| `payments-service` | 샘플 | CPU 급증 |
| `cache-service` | 샘플 | 메모리 증가 |
| `gateway-service` | 샘플 | 네트워크 타임아웃 |
| `log-service` | 샘플 | 디스크 부족 |
| `local-host` | 실제 PC 측정 | CPU·메모리·디스크 사용률 |

샘플은 JSON 기준값에 작은 변동을 더합니다. 실제 회사 서비스와 연결된 값이 아닙니다. `local-host`는 요청 시 측정하며 지속적인 시계열 수집기는 아닙니다. 답변과 메타데이터에 데이터 출처·측정 시간을 구분합니다.

운영 서버용 Prometheus 연결 코드는 있습니다. 사용할 때 `data/monitoring_sources.example.json`을 참고해 실제 대상·PromQL·주소를 설정해야 합니다. 운영 서버 연결과 현장 성능 검증은 현재 MVP의 완료 조건에 포함하지 않습니다.

## 8. 파일별 역할과 수정 위치

```text
all_total_vue_student/
├── README.md                 현재 프로젝트 통합 문서
├── server.py                 백엔드 API·모델·세션·실행 연결
├── web_chat.html             Vue 채팅 화면
├── web_demo_server.py        화면 제공·백엔드 중계
├── mcp_server.py             실제 사용하는 ops 도구·자료·프롬프트
├── mcp_server_docs.py        다중 MCP 서버 탐색 실습용
├── agent/                   AI 처리 흐름과 구성 요소
├── data/                    샘플 상태·매뉴얼·캐시
├── sh/                      설치·실행 스크립트
├── scripts/                 서버 실행기·실제 연결 확인
├── tests/                   자동 검사
├── eval/                    비교 입력·평가 결과
└── doc/                     화면 이미지·archive/의 이전 설명 문서
```

| 파일 | 역할 |
| --- | --- |
| `agent/graph.py` | 처리 상태, 노드, 분기·반복 정의. 새 처리 단계를 붙일 위치 |
| `agent/chat_service.py` | HTTP와 분리된 대화 실행·이벤트 전달·저장·체크포인트 복구 |
| `static/markdown.js` | HTML을 실행하지 않는 답변 표시기. 제목·목록·표·코드·링크 지원 |
| `agent/deps.py` | 모델·MCP·저장소를 묶어 노드에 전달 |
| `agent/llm_common.py` | 분류·계획·분석·대화·보고서·의미 검증의 공통 규칙 |
| `agent/http_llm.py` | 모델 HTTP 호출, 스트리밍, 재시도·시간 제한, 토큰·선택 단가 |
| `agent/openai_llm.py`, `agent/ollama_llm.py` | 제공자별 모델 어댑터 |
| `agent/fake_llm.py` | 네트워크 없는 데모·테스트용 결정론적 응답 |
| `agent/mcp_client.py` | MCP 연결을 유지하고 도구·자료·프롬프트 호출 |
| `agent/monitoring.py` | 샘플/로컬/Prometheus 상태 조회와 최근 측정 저장 |
| `agent/rag.py` | 문서 분할·임베딩·캐시·유사도 검색 |
| `agent/retrieval_policy.py` | 단독 호출·데모의 검색 문맥 보조 구성. 실모델 경로의 질의 분해는 모델이 수행 |
| `agent/settings.py`, `data/agent_policy.json` | 설정 검증과 경고 임계값·실행 제한·보고서 구성. 다음 요청에 변경 적용 |
| `agent/coverage.py` | 대상·증상별 관측과 실제 검색 근거의 누락 확인 |
| `agent/documents.py` | 번호·글머리표·일반 문단에서 원문 단위 추출 |
| `agent/evidence.py` | 원문 단계 추출, 실제 관측값과 원문으로 보고서 조립 |
| `agent/validation.py` | 수치·출처·형식 확인, 검증 실패 시 대체 답변 |
| `agent/retry.py` | 일시적 도구 오류 재시도 |
| `agent/conversation.py` | 후속 질문 대상 연결과 모델 입력 이력 길이 관리 |
| `agent/session_store.py` | 대화·출처·실행 상태 저장과 기존 DB 마이그레이션 |
| `agent/supervisor_v1.py` | 비교용 이전 처리 방식. 현재 MVP 기본은 v2 |
| `data/monitoring.json` | 샘플 서비스의 기준 상태·알림 |
| `data/runbooks/*.md`, `*.txt` | 현재 매뉴얼 6종과 추가할 문서. 문서 개수·장애 유형을 코드에 고정하지 않음 |
| `data/monitoring_sources.example.json` | 선택형 Prometheus 대상·쿼리 설정 예시 |
| `scripts/run_service.py` | 백엔드 준비 확인 후 화면 서버 실행, 자신이 시작한 프로세스 정리 |
| `scripts/package_project.py` | 실제 설정·대화·체크포인트·측정·벡터 DB를 제외한 ZIP 생성 |
| `scripts/setup_ollama.py` | 기존 Ollama 재사용, 현재 설정의 채팅·임베딩 모델 준비 |
| `scripts/verify_live.py` | 실제 모델·임베딩·전체 흐름·검색 비교 확인 |
| `sh/run_live.sh`, `sh/run_demo.sh` | 실제 모델 실행 / 외부 모델 없는 데모 실행 |
| `sh/setup_venv.sh` | Python 가상환경·패키지 설치 |
| `sh/run_agent_openai.sh`, `sh/run_agent_ollma.sh`, `sh/run_frontend.sh` | 개별 서버 실행용 기존 보조 명령 |
| `sh/setup_ollama.sh`, `sh/make_student_zip.sh` | 모델 준비 / 실행 기록을 제외한 배포 ZIP 생성 |
| `requirements.txt`, `requirements.lock.txt` | 필요한 패키지 / 현재 환경의 고정 버전 기록 |

## 9. 설정·저장 파일

| 설정 | 의미 |
| --- | --- |
| `LLM_BACKEND` | 기본 답변 모델 `openai`, `ollama`, `fake` |
| `OPENAI_API_KEY` | OpenAI 호출에 사용하는 키 |
| `OPENAI_FAST_MODEL`, `OPENAI_QUALITY_MODEL` | OpenAI 단계별 모델 |
| `OLLAMA_BASE_URL`, `OLLAMA_FAST_MODEL`, `OLLAMA_QUALITY_MODEL` | Ollama 주소·모델 |
| `RAG_ENABLED`, `RAG_EMBEDDING_BACKEND` | 검색 사용 여부·임베딩 제공자 |
| `OLLAMA_EMBEDDING_MODEL`, `OPENAI_EMBEDDING_MODEL` | 제공자별 임베딩 모델 |
| `RAG_TOP_K`, `RAG_MIN_SCORE` | 증상별 검색 청크 수·최소 유사도 |
| `MONITORING_BACKEND` | 기본 `hybrid`: 샘플 + 실제 로컬. `sample`, `local`, `prometheus`도 지원 |
| `AGENT_POLICY_PATH`, `RUNBOOK_DIRECTORY` | 정책 JSON 파일·검색 문서 폴더. 기본 경로는 `data/agent_policy.json`, `data/runbooks/` |
| `SEMANTIC_VALIDATION` | 추가 LLM 의미 검증 사용 여부 |
| `MODEL_DEADLINE_SECONDS` | 모델 호출의 대기·재시도·응답 전체 제한 |
| `MODEL_PRICES_JSON` | 선택형 모델별 입력/출력 단가. 미설정 시 금액 미산정 |
| `LANGCHAIN_TRACING_V2` | 선택형 LangSmith 추적. 기본 흐름 확인에 필수 아님 |

`SUPERVISOR_VERSION`은 비교용 v1/v2 선택 설정이며, 통합 실행기는 MVP의 v2로 고정합니다. 나머지 선택 설정은 `.env.example`에서 확인할 수 있습니다.

| 생성 파일 | 보관 내용 |
| --- | --- |
| `session.db` | 대화·출처·사용량·실행 상태 |
| `session.db.checkpoints.db` | 중단 단계 복구용 LangGraph 상태 |
| `data/rag_index.sqlite3` | 문서 임베딩 캐시 |
| `data/monitoring_history.sqlite3` | 요청 때 측정한 상태 이력 |

새 대화는 새 ID를 발급합니다. 새로고침은 같은 ID의 기록을 복원합니다. 복구 버튼은 저장된 관측 시점부터 이어가므로 최신 상태는 새 요청으로 조회합니다. 금액은 실제 청구액이 아니며 단가 미설정 시 계산하지 않습니다. Ollama의 외부 API 호출료 0에는 장비·전력 비용이 포함되지 않습니다.

## 10. 베이스에서 무엇을 보완했나

| 원본 베이스의 문제 | 현재 동작 |
| --- | --- |
| 설정 파일·실제 모델 실행 경로가 혼동됨 | `.env` 실행 설정, `.env.example` 빈 양식, 통합 실행 명령으로 구분 |
| CPU 999% 같은 근거 없는 수치도 형식만 맞으면 통과 가능 | 관측값·출처 대조, 상한 도달 후에도 실패 상태 유지 |
| 미검증 보고서 초안이 먼저 보임 | 장애 보고서는 검증 후 공개 |
| 여러 증상이 있어도 단일 키워드로 매뉴얼 선택 | 시스템별·증상별 RAG로 관련 매뉴얼 검색 |
| 미등록 대상을 다른 서비스로 치환 가능 | 대상 이름 확인 요청 |
| 재시작·새로고침 시 대화 유지가 불완전 | DB 보존·대화 ID 유지·화면 기록 복원 |
| 실제 모델 호출·오류 처리에 편차 | OpenAI/Ollama 공통 비동기 호출과 오류 처리 |
| 첫 사용에 기존 기록·진행 표시가 혼란스러움 | 새 대화·예제 질문·완료 표시 추가 |

이번 MVP 정리에서는 이 기반을 유지하고 실행 준비 순서와 화면 사용 흐름만 보완했습니다. 새로운 도메인 기능은 아직 추가하지 않았습니다.

## 11. 확인 방법과 완료 기준

```bash
# 외부 모델 호출 없는 자동 검사
.venv/bin/python -m unittest discover -s tests -q

# 실제 OpenAI·Ollama·RAG 전체 확인: API 사용료 발생 가능
.venv/bin/python scripts/verify_live.py --full

# 검색 비교를 별도로 확인할 때
.venv/bin/python scripts/verify_live.py --retrieval-eval
.venv/bin/python scripts/verify_live.py --extended-retrieval-eval
```

2026년 10월 20일 MVP 정리 후 자동 검사 42개를 통과했고, 고정 규칙 제거를 위한 추가 검사를 포함해 정합성 회귀 검사 15개를 추가하여 현재 **76개를 통과**했습니다. Markdown 렌더링·삽입 공격 검사 7개도 통과했습니다. 실제 MCP·그래프·임시 DB를 함께 확인합니다. 브라우저에서는 새 대화 → CPU 예제 → 실제 OpenAI·MCP·RAG → 검증 통과 → 출처 표시 → 새로고침 후 복원을 확인했습니다. 실제 모델로도 OpenAI와 Ollama의 복수 시스템 진단을 확인했습니다. 이전 검색 비교는 기존 수동 사례 12개와 추가 사례 8개에서 기대 결과와 일치했습니다. 현재 검색 회귀는 기존 12건 중 11건에서 문서 목록이 정확히 일치했고 필요한 문서의 평균 회수율은 100%였습니다. 한 사례에 불필요한 문서가 추가로 포함돼, 문서 목록 정확 일치율은 91.7%입니다. 추가 복합·무관 질문 8건은 모두 기대 결과와 일치했습니다. 현재의 모델 기반 질의 분해·문맥·범위 검증은 OpenAI·Ollama 합계 12건을 통과했으며 `eval/dynamic_verification.json`에 기록합니다. 두 백엔드의 전체 진단도 실제 호출로 확인했습니다. 작은 회귀 사례이며 운영 환경의 정확도를 뜻하지 않습니다. 근거는 `tests/`와 `eval/`에 보관합니다.

**MVP 완료 기준:** 한 명의 사용자가 화면에서 질문을 보내고, 실제 모델·MCP·검색을 거쳐 출처 있는 결과를 확인하며, 이어서 질문할 수 있습니다. 실패하면 멈춘 이유를 안내하고 다시 진행할 수 있어야 합니다.

현재 베이스의 남은 현실적 범위는 샘플 서비스 데이터, 소규모 매뉴얼, 모델에 따른 답변 편차와 지연입니다. 운영 서버 연동·인증·대규모 검색 최적화는 확장할 때 해당 기능에 필요한 만큼 결정합니다.

## 12. 추가 기능은 어디에 붙이나

| 확장하려는 것 | 시작할 위치 |
| --- | --- |
| 새로운 사용자 기능·화면 | `web_chat.html` + 필요한 `server.py` API |
| 새로운 외부 자료·도구 | `mcp_server.py` + `agent/mcp_client.py` |
| 다른 주제의 문서 검색 | `data/runbooks/` + 필요하면 `agent/rag.py`의 문서 형식 |
| 새로운 처리 단계·분기 | `agent/graph.py` + 상태·노드 정의 |
| 다른 출력 형식 | `agent/llm_common.py` + `agent/evidence.py` |
| 새 도메인 전환 | 문서·도구·대상 인식·보고서 규칙을 함께 수정 |

단순히 문서만 교체하면 모든 주제에 자동으로 맞춰지는 범용 서비스는 아닙니다. 현재 업무 규칙이 장애 대응에 맞춰져 있으므로, 추가 아이디어가 정해지면 그 기능에 필요한 부분부터 바꾸면 됩니다.

프로젝트 가이드 PDF와 `Capstone/`은 과제·선행 학습 자료입니다. 실행 서비스가 직접 읽는 데이터와 구분해 원래 폴더에 유지했습니다. 이번에 통합한 기존 설명 MD 9개는 `doc/archive/`의 과거 기록이며 현재 동작의 기준 문서는 이 README입니다.

## 13. 고정 규칙을 실제 판단·설정으로 변경한 부분

| 이전 | 현재 |
| --- | --- |
| CPU·메모리 등 6종 증상을 정규식으로 나눔 | 실제 모델이 증상을 자유롭게 분리하고 검색. 새로운 문서·장애 유형도 코드 수정 없이 반영 |
| ‘그 서버’, ‘다시 확인’ 등의 표현 목록 | 현재 요청과 이전 대상·보류 질문·대화 이력을 함께 해석. 문맥이 있는 해석은 상위 모델 사용 |
| 매뉴얼 하나만 있으면 조사 충분 | 요청한 각 대상의 모니터링과 각 검색 질의의 보존된 근거를 검사. 부족한 부분만 추가 조사 |
| 모든 실제 대상의 경고 기준이 85% | 정책 JSON의 공통·시스템별 임계값 적용. Prometheus 대상 설정에서 별도 지정도 가능 |
| 번호가 있는 Markdown과 고정 보고서 3개 영역 | Markdown·TXT의 번호·글머리표·일반 문단 지원. 보고서 제목·순서·내용은 정책에서 선택 |
| 조사·작성·검색 수가 코드 상수 | 정책 JSON의 상한 적용. 종료 상한에 도달해도 실패를 합격으로 바꾸지 않음 |

정책은 `data/agent_policy.json`에서 수정합니다. 파일 변경은 다음 요청부터 읽습니다. 잘못된 값은 명시적으로 오류 처리합니다.

```json
{
  "max_research_rounds": 2,
  "max_validation_attempts": 3,
  "max_search_queries": 4,
  "max_search_results": 5,
  "warning_thresholds": {"cpu_percent": 85, "memory_percent": 90},
  "system_thresholds": {"local-host": {"cpu_percent": 95}},
  "report_sections": [
    {"title": "관측값", "content": ["observations"]},
    {"title": "분석", "content": ["diagnosis"]},
    {"title": "권장 조치", "content": ["actions"]},
    {"title": "추가 확인", "content": ["checks", "limitations"]},
    {"title": "출처", "content": ["sources"]}
  ]
}
```

위 값은 변경 형식의 예시입니다. 실제 적용 값은 정책 파일을 기준으로 확인합니다. 형식·타입·등록 대상 검사와 종료 상한 같은 프로그램 계약은 유지합니다. 샘플 6종과 `fake` 모델은 시연·오프라인 테스트용으로 명시적으로 남겨두었습니다. 실제 운영 서버를 조회하려면 실제 모니터링 주소와 대상 설정이 필요합니다. 문맥 해석과 질의 분해를 모델이 수행하므로 규칙 처리에 비해 비용·지연이 늘 수 있고 해석 오류 가능성도 남습니다.

```bash
# 실제 모델로 자유로운 후속 표현·제외 대상·새 TXT/Markdown 문서 검색 확인
.venv/bin/python scripts/verify_live.py --dynamic-checks
```

## 14. 정합성 점검 후 정리한 부분

기존 API 주소·대화 DB 형식·모델 선택·샘플/실측 모니터링·일반 대화 스트리밍·근거 검색·중단 복구를 유지했습니다. 실행 중인 기존 서버의 DB는 수정하지 않고 임시 DB로 검증합니다.

| 점검한 문제 | 수정한 동작 |
| --- | --- |
| 복수 대상 질의가 섞임 | 모델이 만든 대상별 검색 질의를 그대로 사용. 이전 체크포인트의 대상 목록 접두사도 처리 |
| 계획 보정 중복 | 등록 대상과 작업 정규화는 그래프 진입점에서 한 번 수행 |
| 첫 검색부터 추가 조사 횟수 사용 | 기본 관측·매뉴얼 조회와 근거 부족에 따른 추가 조사를 분리 |
| MCP 호출 직렬화 | 요청 ID 기반 동시 호출과 최대 4개 동시 실행 제한 |
| 도구 대기가 끝나지 않음 | 호출별 실제 시간 제한 및 모니터링·검색·문서 조회의 시간 초과 재시도 |
| 정상 상태에서도 대응 강제 | 모든 관측이 healthy이면 조치 없이 결과·참고 근거만 제공 가능 |
| 근거 부족을 검증 실패로 안내 | 근거 부족·검증 실패·검증 미완료 사유를 구분 |
| 누적 측정이 모델에 전달되지 않음 | 최근 표본의 시각·수치를 현재 값과 구분해 전달. 정기 수집 이력이 아님을 명시 |
| 보고서 빈 영역·형식 불일치 | 빈 영역 생략, 연속 번호 확인, 실제 모델·데모의 공통 보고서 조립기 사용 |
| 화면에서 Markdown 원문 표시 | 제목·목록·표·코드 등을 표시하고 원문 HTML·실행 링크는 차단 |
| 프롬프트 표시 UI 미연결 | SSE 최종 결과에 마지막 프롬프트를 연결. 접힌 상세 영역에서 확인 |
| 서버 파일에 실행 로직 집중 | server.py는 API·자원 준비, chat_service.py는 실행·저장·복구를 담당 |
| 보조 실행 명령마다 다른 정리 방식 | 실제·데모·화면 단독 실행이 공통 실행기를 사용. 다른 포트 프로세스 강제 종료 제거 |
| Ollama 준비 설정 불일치 | .env의 채팅·임베딩 모델 준비. 실행 중인 Ollama 재사용 |
| ZIP에 실행 DB가 포함됨 | .env와 백업본, 대화·체크포인트·WAL/SHM·측정·벡터 DB 제외 |

정책 JSON에는 선택적으로 `tool_timeout_seconds`(기본 15초), `retrieval_timeout_seconds`(기본 180초), `tool_max_attempts`(기본 총 3회)를 지정할 수 있습니다. 검색은 처음 문서를 임베딩하는 시간을 고려해 별도의 상한을 사용합니다. 모델 자체의 대기·재시도 제한은 기존 `MODEL_DEADLINE_SECONDS`를 따릅니다.

```bash
# Python 회귀 검사: 현재 76개
.venv/bin/python -m unittest discover -s tests -q
# 화면 표시·삽입 공격 검사: 7개
node tests/test_markdown.cjs
# 실제 로컬 지표를 사용하는 전체 진단: 임시 대화·측정 DB 사용
.venv/bin/python scripts/verify_live.py --backend openai --local-check
```

OpenAI와 Ollama의 실제 복수 대상 진단, OpenAI의 local-host 실측 진단을 다시 확인했습니다. 모델·검색 평가의 기존 소규모 데이터 한계는 그대로 적용됩니다. 복구 검사에는 새 상태 필드가 없는 이전 형식의 체크포인트도 포함합니다. 코드 변경 적용은 실행 터미널에서 Ctrl+C 후 `bash sh/run_live.sh`로 재시작하고 화면을 새로고침합니다. .env와 저장된 대화는 유지합니다.

## 15. 답변 가독성과 매뉴얼 조회 범위 마무리

새 수집 도구나 기능은 추가하지 않았습니다. 기존 보고서와 매뉴얼의 표현만 맞췄습니다.

- 기본 답변 순서: 진단 요약 → 측정값 표 → 권장 조치 → 추가 확인·조회 한계.
- 측정값은 대상별 CPU·메모리·디스크 표로 표시합니다. 제공되지 않은 값은 `미조회`이며 알림 문구의 숫자로 채우지 않습니다.
- 실제 로컬·Prometheus·샘플 출처를 구분하고 측정 시각은 한국 시간(KST)으로 표시합니다.
- 조사 과정·모델/토큰 정보·근거 매뉴얼은 접힌 상세 영역에서 확인합니다. 새로고침 후 대화와 출처 복원은 유지합니다.
- p95 지연시간, 커넥션 풀, 로그, 배포 이력, 프로세스별 사용량, 실제 호출 경로는 현재 기본 도구의 자동 조회 범위 밖입니다. 담당자의 별도 확인 대상으로 표시합니다.
- 매뉴얼 6종에서 조회 도구가 실제로 제공하지 않는 정보를 가져오는 것처럼 적힌 문장을 수정했습니다. 디스크 수치가 없으면 미조회이며, 시스템 메모리를 프로세스 OOM·힙 사용률로 대체하지 않습니다.
- 조치 조건이 확인되지 않은 이상 상태에서는 조치를 강제하지 않고 근거가 있는 점검만 제안할 수 있습니다. 조건부 조치의 원문과 출처는 유지합니다.

Python 회귀 검사 76개와 Markdown 표시 검사 7개를 통과했습니다. 표에서도 다른 대상의 수치를 바꿔 넣거나 미조회 디스크에 메모리 값을 넣으면 검증이 거절됩니다.

![요약·측정값 표·조회 한계 표시](/Users/user/mini-project/all_total_vue_student/doc/readability-scope.png)

## 16. 진단 워크스페이스 UI

로켓펀치 채용 페이지의 왼쪽 메뉴·상단 카드·설정 바·중앙 콘텐츠·오른쪽 보조 정보 배치를 참고하고, 화면과 문구는 운영 진단에 맞게 구성했습니다. 수정 범위는 `web_chat.html`의 화면·스타일·표시 상태이며 백엔드 API와 대화 저장 형식은 유지했습니다.

- 왼쪽: 새 대화, 진단 채팅 입력 포커스, 빠른 질문 선택, 사용 안내.
- 상단: 실제 로컬 조회와 샘플 시나리오의 빠른 시작 카드, 답변 모델 선택.
- 중앙: 대화, 진행 과정, 검증된 보고서, 출처·사용량·프롬프트 상세, 중단 작업 재개·종료.
- 오른쪽: `/health` 응답에 따른 선택 모델과 임베딩 설정, 백엔드 연결 확인, 데이터 출처와 조회 범위 안내.
- 대화 중 빠른 질문을 열어도 대화 기록을 지우지 않습니다. 새 대화는 기존과 동일하게 새 대화 ID를 발급합니다.
- 모바일: 사이드바·보조 패널을 줄이고 모델 정보·데이터 안내를 접힌 영역으로 제공합니다. 새 대화와 입력창은 유지합니다.

기존 Python 회귀 검사 76개와 Markdown 표시 검사 7개를 통과했습니다. 브라우저에서 실제 진단, 모델 전환, 빠른 질문, 새 대화, 새로고침 후 기록 복원과 390px 모바일 폭의 가로 넘침 여부를 확인했습니다. 화면 변경은 브라우저 새로고침으로 확인할 수 있습니다.

![진단 워크스페이스 시작 화면](/Users/user/mini-project/all_total_vue_student/doc/agent-workspace.png)

## MVP 실행 화면

![질문부터 조회·검증·출처 표시까지 동작하는 MVP](/Users/user/mini-project/all_total_vue_student/doc/mvp.jpg)

실제 화면에서도 기존 표현 목록에 없는 “방금 살펴본 곳 한 번 더 살펴봐줄래?”를 입력해 이전 대상을 재조회하고 검증된 보고서를 받는 동작을 확인했습니다.

![문맥을 해석해 실제 재조회한 결과](/Users/user/mini-project/all_total_vue_student/doc/dynamic-behavior.jpg)
