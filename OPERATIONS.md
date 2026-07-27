# 운영 · 품질 · 인수인계 (Phase 4)

프로덕션 운영에 필요한 최소 런북. 스택은 stdlib 위주(벡터DB·빌드체인 없음)라 이관이 단순하다.

## 1. 품질 게이트 (인용 정확도 · 거부율)

CI에서 MOCK 서버를 띄우고 골드셋으로 판정한다. 실패하면 exit 1 → 배포 중단.

```bash
make ci        # 유닛 self-check + MOCK 골드셋 게이트 + 인젝션 테스트
# 또는 수동:
MOCK_LLM=1 python app.py &          # 서버
MOCK_LLM=1 python eval_gold.py      # 게이트 (exit 1 이면 실패)
```

판정 지표 (`eval_gold.py`, 환경변수로 임계 조정):

| 지표 | 뜻 | 기본 게이트 | MOCK |
|---|---|---|---|
| `grounded` | 답변당 인용 ≥1 비율 | `MIN_GROUNDED=0.8` | 통과(매턴 인용) |
| `concept_cov` | 기대 개념 커버율 | `MIN_CONCEPT_COV=0.5` | 통과 |
| `refusal (answerable)` | 답변가능 질문을 거부한 비율 | `MAX_REFUSAL=0.34` | 0 (통과) |
| `faithfulness` | LLM judge 충실도/5 | `MIN_FAITHFULNESS=0`(꺼짐) | judge=4 |

**정직 포인트 (실키 필요):**
- **인용 정확도(faithfulness)·거부 품질**은 실제 근거 답변이 있어야 유의미. `ANTHROPIC_API_KEY` 연결 후 `MIN_FAITHFULNESS`를 올려 게이트한다.
- **범위밖 질문을 올바르게 거부하는지**(예: "김치찌개 레시피")는 MOCK canned 답변이 거부를 못 하므로 실키 전용 검증. 지금은 답변가능셋 거부율(≈0)만 게이트.

## 2. 비용 모니터링 (캐싱 · 모델 선택)

`GET /metrics` → `total_est_cost_usd` + 경로별 비용. 실토큰 사용량 기반(`/chat` 스트림 종료 시 `usage`로 집계).

```bash
curl -s localhost:8000/metrics | python -m json.tool
```

- **MOCK은 항상 $0** (canned 응답 = 토큰 소비 0, 정직). 실키에서만 non-zero.
- **모델 선택**으로 비용 조절: `MODEL` 환경변수 (`config.py`). 요율(`obs._RATES`, $/1M): opus-4-8 5/25 · sonnet-5 3/15 · haiku-4-5 1/5. 저비용은 `MODEL=claude-haiku-4-5`.
- **캐싱**: 문서 블록 마지막에 프롬프트 캐시 적용(`cache_last=True`) → 반복 질의의 입력 토큰이 캐시read(≈10% 요율)로 청구. `/metrics` 비용에 반영됨.
- 학생 1인 월 추정: Opus ~7만원 / Haiku ~1.5만원 (사용량 기반 실비).

## 3. 반 단위 진척 (조교/교수)

- `GET /analytics` (코호트 집계) · `GET /analytics/students` (학생별) — 둘 다 `X-Admin-Token: $ADMIN_TOKEN` 필요.
- UI: `/teacher.html` (토큰 입력 → 학생 테이블 + 반 평균).

## 4. 배포 · 상태

- 배포/영속/코퍼스 재생성은 [DESIGN·DEPLOY.md](DEPLOY.md) 참조.
- 헬스: `/healthz`(설정 요약) · `/readyz`(실키 모드는 논문 업로드 완료 후 200).
- 레이트리밋: 디바이스별 토큰버킷(`RATE_LIMIT`/`RATE_WINDOW`), POST만 대상.

## 5. 인수인계 체크리스트

- [ ] `.env`: `ANTHROPIC_API_KEY`, `APP_SECRET`(랜덤), `ADMIN_TOKEN`, `MODEL`
- [ ] `make ci` 그린 (게이트 통과)
- [ ] `/metrics` 비용이 실사용량 반영 확인 (실키)
- [ ] 각 모듈 self-check: `make selfcheck`
- [ ] 코퍼스 확정 + `graph.json` 재생성 (실키: `python generate_graph.py`)

## 6. 모듈 지도 (인수인계용)

| 파일 | 책임 |
|---|---|
| `app.py` | FastAPI 엔드포인트·미들웨어(레이트리밋·메트릭) |
| `llm.py` | 스트리밍 채팅·퀴즈·judge (MOCK/실키 분기, usage 방출) |
| `mastery.py` | 숙련도(SM-2-lite)·대시보드 지표·계정 병합·코호트 |
| `analytics.py` | 코호트 집계 (읽기전용) |
| `obs.py` | 구조화 로깅·메트릭·비용 추정 |
| `session.py`·`history.py`·`accounts.py` | 세션·대화영속·계정 |
| `corpus.py`·`papers.py`·`generate_graph.py` | 코퍼스·업로드·그래프 |
| `eval_gold.py`·`eval.py`·`ci.sh` | 품질 게이트 |
