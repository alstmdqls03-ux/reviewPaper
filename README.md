# 리뷰 논문 학습 챗봇

전자현미경 리뷰 논문을 **편하게 공부**하도록 돕는 RAG 기반 학습 도구.
근거 인용 답변 · 위키형 개념 그래프 · 개인 숙련도 추적 · 간격반복 퀴즈 · 노트/내보내기 ·
대화 기록(재개) · 계정 · 논문 업로드 — POC에서 출발해 프로덕션 계층까지 갖췄다.

## 핵심 기능

- **근거 인용 답변 (스트리밍)** — 실시간 스트리밍 + 문장별 원문 논문·페이지 네이티브 Citations (환각 인용 불가)
- **위키형 개념 그래프** — 개념→관련개념→근거논문 탐색, 답변이 다룬 개념 자동 하이라이트, **내 숙련도(new/learning/known)로 노드 색칠**
- **개인 학습 추적** — 디바이스별 **개념 숙련도**, 약한/복습기한 개념을 **간격반복(SM-2-lite)**으로 우선 출제, **"다음에 공부할 개념"** 가이드 경로, 답변→**노트** 저장→**Markdown 내보내기**
- **개념 퀴즈** — 대화가 쌓이면 열림, 다룬 개념 4지선다 → **서버 채점** → 오답은 그래프 개념으로 "복습" 연결, 결과가 숙련도에 반영
- **계정 + 대화 기록** — 익명 계정(닉네임 지정 가능), 대화가 **영속화되어 재개 가능**(다시 열면 문맥까지 복원), 대화 목록/삭제
- **코퍼스 확장** — 논문 5편 제한 해제, **사용자 PDF 업로드**, 규모 커지면 **BM25 하이브리드 검색**으로 질문별 관련 논문만 컨텍스트 투입, 그래프 재생성
- **운영/품질** — 세션별 격리·TTL, **레이트리밋**(디바이스별 토큰버킷), 입력 검증, 구조화 로깅+`/metrics`, 학습 **분석**(`/analytics`), 헬스체크(`/healthz`·`/readyz`), 골드셋 평가 게이트·인젝션 테스트·CI·**Docker**

## 실행

```bash
pip install -r requirements.txt

# (A) 오프라인 데모 — API 키 불필요, 전 기능 mock 구동
MOCK_LLM=1 python app.py            # http://localhost:8000

# (B) 실제 모드
cp .env.example .env                # ANTHROPIC_API_KEY (+ 프로덕션은 APP_SECRET, ADMIN_TOKEN)
python app.py

# (C) Docker
docker compose up --build           # /healthz 헬스체크 포함, ./data 에 DB 영속화
```

## 검증

```bash
# 단위 self-check (전부 stdlib, 키 불필요)
make selfcheck                       # session·mastery·corpus·obs·accounts·history·config·limits·analytics
pytest -q                            # 세션·그래프·인젝션

# 통합 (서버 자동 기동)
./ci.sh                              # pytest + eval_gold 게이트(PASS/FAIL) + injection

# 서버 띄운 상태:
MOCK_LLM=1 python eval.py            # 근거성/충실도 리포트
MOCK_LLM=1 python eval_gold.py       # 골드셋 게이트
locust -f locustfile.py --host http://127.0.0.1:8000 --headless -u 20 -r 10 -t 20s   # 동시성·세션격리
```

`make` 타깃: `run mock test ci eval load selfcheck docker-build docker-up docker-down clean`.

## HTTP API

| 엔드포인트 | 설명 |
|---|---|
| `POST /chat` | SSE 스트리밍 답변+인용. `{session_id, conversation_id, message}`, 헤더 `X-Device-Id` |
| `POST /quiz` · `POST /quiz/grade` | 간격반복 퀴즈 생성 / 서버 채점 |
| `GET /mastery` | 숙련도 + 다음에 공부할 개념 |
| `POST/GET /notes` · `GET /notes/export` | 노트 저장/목록/Markdown 내보내기 |
| `GET/POST /account` · `/account/name` | 익명 계정 조회/닉네임 |
| `GET /conversations` · `GET/DELETE /conversations/{id}` | 대화 기록 목록/재개/삭제 |
| `POST /upload` | 논문 PDF 추가 |
| `GET /metrics` · `GET /analytics` | 요청 메트릭 / 학습 분석(ADMIN_TOKEN 게이트) |
| `GET /healthz` · `GET /readyz` | 헬스/레디니스 |
| `GET /graph` | 개념 그래프 |

## 구조

```
app.py            FastAPI — 위 엔드포인트, 레이트리밋+관측 미들웨어, lifespan 코퍼스 업로드
session.py        인메모리 세션(라이브 멀티턴)·TTL·세션락·개념매칭
mastery.py        디바이스별 숙련도·간격반복·가이드경로·노트 (SQLite mastery.db)
accounts.py       익명 계정·디바이스 매핑·서명 토큰 (SQLite app.db)
history.py        영속·재개 가능한 대화/메시지 (SQLite app.db)
corpus.py         코퍼스 레지스트리·PDF 추출·BM25 하이브리드 문서선택·그래프 재생성
llm.py            스트리밍 채팅·퀴즈 생성·충실도 심판 (MOCK 내장)
config.py         환경설정 싱글턴   limits.py  토큰버킷 레이트리밋+입력검증
obs.py            구조화 로깅·메트릭·비용추정   analytics.py  학습 분석 집계
papers.py         Files API 업로드/캐시   generate_graph.py  그래프 생성
static/index.html 채팅·퀴즈·숙련도·노트·업로드·계정·대화기록·분석 UI + Cytoscape 그래프
eval.py / eval_gold.py   평가 하네스 / 골드셋 게이트
locustfile.py     동시성·세션격리 부하   test_*.py  pytest
Dockerfile / docker-compose.yml / Makefile / ci.sh / .github/workflows/ci.yml
papers/           시드 리뷰 논문 9편 (출처·라이선스: papers/SOURCES.md)
```

모델: `claude-opus-5` (`MODEL` 환경변수로 교체). 논문 합계 47MB > 32MB 요청 한도 → Files API 필수.

## 아키텍처 메모

- **인용 ⊥ structured output**: 채팅은 네이티브 Citations, 퀴즈·그래프·심판은 structured output. 배타라 호출 분리.
- **저장소**: 라이브 세션은 인메모리(휘발 OK), 계정/대화/숙련도/노트는 SQLite(재시작 후 유지). 전부 단일 프로세스(이벤트 루프 1스레드) 직렬 — 멀티워커 시 세션→Redis, DB→커넥션 풀.
- **레이트리밋**: 디바이스(또는 IP)별 토큰버킷, 인메모리. 멀티워커 시 공유 스토어로 교체.
- **BM25 하이브리드**: 코퍼스 ≤ 5편이면 전부 롱컨텍스트, 초과 시 질문별 상위만. 어휘검색이 KO↔EN 동의어에서 밀리면 임베딩으로.
- **MOCK 모드**: 키 없이 전 기능(세션·스트리밍·퀴즈·숙련도·계정·기록·업로드·부하·평가) 검증용.

## 남은 작업 (실제 키/외부 의존)

- [ ] 실제 답변 **원문 인용 품질** 확인, 업로드 논문 기반 그래프 재생성
- [ ] `eval_gold.py` keyword/충실도 임계 상향, 회귀 추적 / injection 실모델 탈옥테스트(현재 MOCK skip)
- [ ] BM25 하이브리드를 6편+ 실제 코퍼스로 리콜 측정, locust 실부하(실 LLM 지연·비용) p95
- [ ] 프로덕션 시크릿: `APP_SECRET`·`ADMIN_TOKEN` 설정, 멀티워커 시 Redis 세션/레이트리밋
- [ ] 실제 계정 로그인(매직링크 SMTP)·클라우드 배포 호스팅
