# 데이터 저장소 설계

작성 2026-07-28 (커밋 `b2e4f0f` 기준). 이전 상태와 무엇이 왜 바뀌었는지까지 적는다.

## 1. 전체 그림

```
app.db  ─ 신원 · 대화 · 소스 (관계형 데이터 전부)
├─ users(id PK, display_name, created_at)
├─ devices(device_id PK, user_id, linked_at)                    idx(user_id, linked_at)
├─ conversations(id PK, user_id, title, created_at, updated_at, deleted)
│                                                idx(user_id, deleted, updated_at DESC, id)
├─ messages(id PK, conv_id, role, content, created_at, meta)    idx(conv_id, created_at, id)
├─ sources(id PK, path UNIQUE, title, owner, sha, added_at)     idx(owner) idx(sha)
└─ conversation_sources(conv_id, source_id) PK(둘)
      ├─ FK conv_id   → conversations(id) ON DELETE CASCADE
      └─ FK source_id → sources(id)       ON DELETE CASCADE

mastery.db ─ 학습 진척
├─ mastery(device_id, concept_id, score, ease, interval, reps, last_reviewed) PK(둘)
├─ notes(id PK, device_id, text, source, concept_id, created_at)   idx(device_id, created_at)
└─ quiz_attempts(id PK, device_id, concept_id, correct, created_at) idx(device_id, created_at)

DB 밖
├─ uploads/<owner>/*.pdf   업로드 원본
├─ text_cache/*.json       pypdf 추출 텍스트 (뷰어 + BM25 공용)
├─ file_cache.json         path → Anthropic Files API file_id
├─ graph.json              개념 그래프 (전역 1개, 프로세스 기동 시 1회 로드)
├─ corpus.json             ⚠️ 이관 원본. 더 이상 아무도 안 읽는다 (백업으로만 보관)
└─ 프로세스 메모리          SessionStore — 라이브 세션, TTL 30분, 재시작하면 소멸
```

## 2. 두 파일로 나뉜 이유 (그리고 그게 옳지 않은 이유)

`mastery.db`가 W2에서 먼저 생겼고 `app.db`가 나중에 프로덕션 레이어로 붙었다.
**도메인 경계가 아니라 개발 순서 경계다.** 증거: `analytics.py`는 두 파일을 다 열어서
조인한다(`Analytics(mastery_db=..., app_db=...)`). 합칠 만한 이유는 충분하지만,
합치는 것 자체가 마이그레이션이라 지금은 남겨뒀다. → 로드맵 Q1.

## 3. 세션은 2겹이고, 라이브 쪽에는 DB가 없다

| | 라이브 세션 | 영속 대화 |
|---|---|---|
| 식별자 | `session_id` | `conversation_id` |
| 담는 것 | 멀티턴 컨텍스트, 턴 수, 다룬 개념, **생성된 퀴즈(정답 포함)** | 제목·메시지·각주·소스 묶음 |
| 저장 | 프로세스 메모리 (`session.py` `SessionStore._sessions`) | `app.db` |
| 수명 | 마지막 활동 후 30분, 재시작하면 전부 소멸 | 영구 (소프트 삭제) |

둘을 잇는 곳은 `app.py`의 `/chat` 한 군데다 — 세션이 비었는데 `conversation_id`가 오면
DB에서 메시지를 읽어 메모리를 재수화하고, 턴 수·다룬 개념까지 되살려 퀴즈 게이트를 유지한다.

**따라오는 결과 3가지**
1. 퀴즈 정답은 DB에 없다. 재시작하면 채점 중이던 퀴즈는 404다.
2. 워커를 2개로 늘리면 세션이 절반씩 사라진다 (로드맵 §5-3의 "한 프로세스 천장").
3. 30분이 지나도 대화는 안 사라진다 — 다음 질문에서 DB로부터 재수화된다.

## 4. 2026-07-28에 바꾼 것

### (a) 인덱스 5개 (`fc15f49`)
그전까지 인덱스는 PK 자동 인덱스뿐이었고, 두 핫패스가 전부 풀스캔이었다.

| | before | after |
|---|---|---|
| `list_conversations` | `SCAN c` + 대화마다 `SCAN m` + `USE TEMP B-TREE` | 양쪽 다 COVERING INDEX, 정렬 없음 |
| 대화 2,000 / 메시지 12,000 실측 | **519.0 ms** | **1.9 ms** |
| `search()` 같은 조건 | **522.4 ms** | **2.4 ms** |

### (b) 소스 레지스트리 `corpus.json` → `sources` 테이블 (`988a0ec`)
레지스트리만 유일하게 DB 밖에 있었다. 통파일 read-modify-write라 동시 등록에 취약했다.

| 스레드 40개가 동시에 등록 | 결과 |
|---|---|
| JSON 통파일 | 11/40 등록, `JSONDecodeError` 14건, **파일 자체가 깨짐** |
| SQLite | **40/40 등록, 예외 0건** |
| 같은 경로 20건 동시 | 행 **1개** (스키마가 강제), 예외 0건 |

핵심 설계: **경로 중복을 코드가 아니라 스키마가 막는다.** `path TEXT UNIQUE` +
`INSERT … ON CONFLICT(path) DO NOTHING`. 확인-후-삽입 사이의 틈을 코드로 메우려 하지 않고,
경합에서 지면 먼저 들어간 행을 돌려준다. sha(내용) 중복 검사는 소유자 가시성에 따라
달라지는 조건이라 UNIQUE로 표현할 수 없어 **조언적**이라고 명시했다.

**순서가 중요하다**: `ORDER BY rowid`가 곧 레지스트리 순서이고, 그게 프롬프트 문서 블록
순서 = 1h 캐시 프리픽스다. 흔들리면 캐시가 매 질문 새로 쓰여 비용이 5배가 된다
(콜드 $3.70 vs 웜 $0.20). 이관도 `corpus.json` 순서를 그대로 옮겼다.

### (c) `conversation_sources` — 대화가 자기 소스 묶음을 갖는다 (`b2e4f0f`)
이게 "노트북"의 마지막 절반이다. 어젯밤에는 localStorage로만 했다.

- `get_sources()`는 행이 없으면 `None`(=선택을 손댄 적 없음=전부), 있으면 리스트.
  **`[]`(0편 고름)과 `None`은 다르다** — 전자는 400으로 거절된다.
- FK가 실제로 일을 한다: 논문을 지우면 그 논문을 가리키던 선택이 자동으로 빠진다.
  localStorage 시절엔 지운 논문의 id가 선택에 영원히 남아 있었다.
- `PRAGMA foreign_keys = ON`은 **연결마다** 켜야 한다(DB 속성이 아니다). 연결을 만드는
  세 곳(`history._connect`, `corpus.connect`)에 넣었다.

## 5. 남아 있는 문제

1. **`mastery.device_id`가 두 종류의 id를 섞어 담는다.** 실측: 19개 키 중 10개는
   `users.id`, 8개는 `devices.device_id`, 1개는 어디에도 없는 값. accounts 레이어
   이전에 쓰인 행들이라 **개념 진척 약 30건이 계정으로 안 따라온다.** `merge_learner`는
   복원 코드를 쓸 때만 돌지 이 행들을 백필하지 않는다. → 백필 정책 결정 필요.
2. **`mastery.db`의 세 테이블에 FK가 없다.** `app.db`와 다른 파일이라 FK를 걸 수 없다.
   두 파일을 합치는 것이 선행.
3. **노트에 대화 스코프가 없다.** `notes.device_id`만 있고 `conv_id`가 없어서, 여러
   대화에서 저장한 노트가 한 목록에 뒤섞인다 (갭 분석 G7). → 로드맵 Q2.
4. **`graph.json`은 여전히 파일이고 프로세스 기동 시 1회 로드다.** 업로드가 그래프를
   다시 써도 `app.NODES`는 재시작 전까지 옛 값이다 (갭 분석 G19).
5. **마이그레이션 프레임워크가 없다.** `PRAGMA table_info` 확인 후 조건부 `ALTER`,
   `CREATE IF NOT EXISTS`로 버틴다. 지금 규모에선 충분하지만 컬럼 의미가 바뀌는
   변경은 감당 못 한다.

## 6. 마이그레이션 방식

전부 **기동 시 자동, 별도 스텝 없음**:
- 테이블·인덱스: `CREATE ... IF NOT EXISTS`
- 컬럼 추가: `PRAGMA table_info` 확인 후 `ALTER TABLE`  (`messages.meta`가 선례)
- 데이터 이관: 대상 테이블이 비었을 때만 1회 (`corpus._migrate_from_json`)

되돌리려면 커밋을 revert하고 `app.db`의 `sources`/`conversation_sources`를 drop하면
`corpus.json`이 그대로 남아 있어 이전 코드가 다시 읽는다.
