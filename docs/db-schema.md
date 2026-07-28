# 데이터 저장소 설계

작성 2026-07-28 (커밋 `bfa5ad6` 기준). 이전 상태와 무엇이 왜 바뀌었는지까지 적는다.

## 1. 전체 그림

```
app.db  ─ 전부 한 파일 (신원 · 대화 · 소스 · 학습 진척)
├─ users(id PK, display_name, created_at)
├─ devices(device_id PK, user_id, linked_at)                    idx(user_id, linked_at)
├─ conversations(id PK, user_id, title, created_at, updated_at, deleted)
│                                                idx(user_id, deleted, updated_at DESC, id)
├─ messages(id PK, conv_id, role, content, created_at, meta)    idx(conv_id, created_at, id)
├─ sources(id PK, path UNIQUE, title, owner, sha, added_at)     idx(owner) idx(sha)
├─ conversation_sources(conv_id, source_id) PK(둘)
│     ├─ FK conv_id   → conversations(id) ON DELETE CASCADE
│     └─ FK source_id → sources(id)       ON DELETE CASCADE
├─ mastery(user_id, concept_id, score, ease, interval, reps, last_reviewed) PK(둘)
│     └─ FK user_id → users(id) ON DELETE CASCADE
├─ notes(id PK, user_id, text, source, concept_id, created_at, conv_id)  idx(user_id, created_at)
│     ├─ FK user_id → users(id)         ON DELETE CASCADE
│     └─ FK conv_id → conversations(id) ON DELETE SET NULL   ← 노트는 대화보다 오래 산다
├─ quiz_attempts(id PK, user_id, concept_id, correct, created_at)  idx(user_id, created_at)
│     └─ FK user_id → users(id) ON DELETE CASCADE
└─ schema_migrations(name PK, applied_at)   1회성 데이터 이관 원장

DB 밖
├─ uploads/<owner>/*.pdf   업로드 원본
├─ text_cache/*.json       pypdf 추출 텍스트 (뷰어 + BM25 공용)
├─ file_cache.json         path → Anthropic Files API file_id
├─ graph.json              개념 그래프 (전역 1개, 파일 변경 감지해 자동 재적재)
├─ corpus.json             ⚠️ 이관 원본. 더 이상 아무도 안 읽는다 (백업으로만 보관)
├─ mastery.db              ⚠️ 이관 원본. 더 이상 아무도 안 읽는다 (백업으로만 보관)
└─ 프로세스 메모리          SessionStore — 라이브 세션, TTL 30분, 재시작하면 소멸
```

## 2. 두 파일로 나뉘어 있던 이유 (2026-07-28 병합 완료)

`mastery.db`가 W2에서 먼저 생겼고 `app.db`가 나중에 프로덕션 레이어로 붙었다.
**도메인 경계가 아니라 개발 순서 경계였다.** 그 결과 진척과 계정 사이에 FK를 걸 수
없었고 `analytics.py`는 두 파일을 열어 손으로 조인했다. 지금은 한 파일이고 FK가 있다.

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

### (d) mastery.db → app.db 병합 + `device_id` → `user_id` (`4d99c75`)
컬럼 이름이 거짓말이었다. 값은 늘 계정 id였는데(`learner()`가 기기→계정을 먼저 푼다)
이름은 기기라고 말했고, accounts 레이어 이전에 쓰인 34개 행은 **실제로** 기기 id를 키로
쓰고 있어서 그 진척이 계정으로 안 따라왔다.

이관 결과: `{'rows': 91, 'folded': 34, 'orphan_users': 1, 'collisions': 10}`
**유실·약화된 (학습자, 개념) 0건** — 원본의 모든 쌍이 귀속 계정 아래 같거나 높은 점수로 존재.
충돌 시 규칙은 `merge_learner`와 같다(점수 높은 쪽). 귀속처 불명 1건은 그 id로 계정을
만들어 보존했다 — 모른다고 학습 기록을 말없이 버리지 않는다.

FK가 자체검사를 한 번 깨뜨렸다: 계정을 안 만들고 진척부터 넣고 있었다. 실제 코드는 늘
`ACCOUNTS.resolve`를 먼저 부르므로, **테스트가 현실을 모델링하지 않고 있던 것**이다.

### (e) 노트에 대화 스코프 (`b189022`)
`notes.conv_id → conversations(id) ON DELETE SET NULL`. CASCADE가 아닌 이유: 노트는
대화보다 오래 사는 산출물이다. 대화를 지웠다고 정리해둔 노트가 사라지면 안 된다.
`list_notes`가 제목을 조인해 카드에 "💬 <대화 제목>"을 띄우고, 누르면 그 대화로 돌아간다.

### (f) 그래프 자동 재적재 (`24504b3`)
`corpus.graph_data()`가 `(mtime, size)` 변화를 보고 다시 읽는다. 전에는 `app.py`가
import 시점에 1회 로드해서, 업로드가 그래프를 다시 써도 재시작 전까지 옛 노드를 썼다.
실측: 프로세스를 띄운 채 노드 추가 → 18→19 감지.

### (g) 이관 완료 판정을 원장으로 (`bfa5ad6`)
"대상이 비었으면 아직 이관 안 함"은 **'이관 전'과 '사용자가 지운 뒤'를 구분하지 못한다.**
실측으로 재현: 83행 → 전부 삭제 → 재기동 **83행**(지운 진척이 부활).
`schema_migrations` 원장 도입 후: 재기동 0행.

## 5. 남아 있는 문제

1. **한 프로세스·한 워커를 넘지 못한다.** `SessionStore`가 인메모리이고 SQLite 연결이
   단일 연결이다. 워커를 늘리면 세션이 절반씩 사라진다. → 세션을 Redis로, DB를 Postgres로.
   동시 사용자 50명을 넘길 때 필요하다 — 그 전에는 하지 마라.
2. **퀴즈 정답이 DB에 없다.** 재시작하면 채점 중이던 퀴즈가 404다 (§3).
3. **노트가 인용을 서식째 보존하지 않는다.** 첫 인용의 제목 문자열 하나만 저장한다.
   노트→소스 변환도 없다. 학습 루프의 마지막 칸 — 로드맵 Q2.
4. **개념 그래프는 여전히 전역 1개다.** 내가 올린 PDF는 그래프에 안 들어온다
   (`regenerate_graph`가 `shared_papers()`만 쓴다). 사용자별 그래프는 `graph.json`
   단일 파일 구조를 바꿔야 한다.
5. **`ALTER TABLE`로 붙인 컬럼에는 FK가 없다.** SQLite의 `ALTER`가 FK 절을 못 받는다.
   `notes.conv_id`는 새로 만든 DB에만 FK가 있고, 이관된 옛 DB에는 컬럼만 있다.
   전부 맞추려면 테이블 재작성(rename→create→copy→drop)이 필요하다.

## 6. 마이그레이션 방식

전부 **기동 시 자동, 별도 스텝 없음**:
- 테이블·인덱스: `CREATE ... IF NOT EXISTS`
- 컬럼 추가: `PRAGMA table_info` 확인 후 `ALTER TABLE`  (`messages.meta`가 선례)
- 데이터 이관: `migrations.claim(db, name)` 원장으로 1회 판정 (비었는지로 판정하지 않는다)

**프레임워크는 안 만들었다.** 스키마 변경은 위 둘로 충분하고(멱등), Alembic은 이 앱에
없는 문제(브랜치·다운그레이드·자동 diff)를 풀면서 의존성을 늘린다. 컬럼 *의미*가 바뀌는
변경이 나오면 그때 다시 판단한다.

되돌리려면 커밋을 revert하고 `app.db`에서 새 테이블을 drop한 뒤
`schema_migrations`의 해당 행을 지운다. 원본 `corpus.json`과 `mastery.db`는
지우지 않고 남겨뒀으므로 이전 코드가 그대로 다시 읽는다.
