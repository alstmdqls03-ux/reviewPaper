# 배포 (Phase 2)

## 로컬 / 오프라인 데모 (실키 불필요)
```bash
make mock          # MOCK_LLM=1, http://localhost:8000
```

## Docker (실사용)
```bash
cp .env.example .env          # ANTHROPIC_API_KEY, APP_SECRET(랜덤), ADMIN_TOKEN 채우기
mkdir -p data/text_cache data/uploads
touch data/mastery.db data/app.db data/corpus.json   # 파일로 마운트되도록 선생성
make docker-up                # = docker compose up --build -d
```
상태는 `./data`에 영속(bind mount) — 컨테이너를 지워도 진척/계정/노트 유지.

## 코퍼스 확정 + 그래프 재생성
- 대상 논문 PDF를 `papers/`에 두거나 UI "논문 추가"로 업로드 → `corpus.json` 등록.
- 그래프는 업로드 시 자동 재생성(`corpus.regenerate_graph()`, 실키 필요). 오프라인/MOCK에서는 스킵되고 기존 `graph.json` 사용.
- 수동 재생성: `ANTHROPIC_API_KEY=... python generate_graph.py`

## 계정별 진척 영속 (Phase 2 핵심)
- 진척(숙련도·퀴즈·노트)은 **device가 아니라 계정(user_id)** 키로 저장 → 같은 계정이면 여러 기기/브라우저에서 동일 진척.
- 다른 기기에서 이어가기: 기존 기기의 **복원 코드(토큰, `/account`의 token)** 를 새 기기에 입력(`POST /account/claim`) → 기기 연결 + 익명 진척 병합.

## 체크리스트
- [ ] `.env`의 `APP_SECRET`을 랜덤 값으로 (기본값은 토큰 위조 가능)
- [ ] `ADMIN_TOKEN` 설정 시 `/analytics` 보호됨
- [ ] `/readyz` 200 확인(실키 모드는 논문 업로드 완료 후 ready)

## 정직 포인트 — MOCK vs 실키
- MOCK: 대시보드·퀴즈·점수·계정 영속·claim 전부 동작(오프라인 데모 가능).
- 실키 필요: 실제 논문 근거 **인용 답변** 품질, **그래프 재생성**(generate_graph).
