# DESIGN.md — 리뷰 논문 학습 챗봇 (Spasex-adapted)

SpaceX("Spasex") 디자인 언어를 **읽기형 학습 앱**에 맞게 적응한 것.
원 스펙은 풀블리드 사진 마케팅용이라, 정신(블랙 캔버스·엔지니어드 미니멀·대문자 크롬·
고스트 필·무채색)은 이식하되 **본문 가독성은 지킨다**.

## 적응 원칙 (원 스펙 → 이 앱)

| 원 스펙 | 이 앱에서 |
|---|---|
| 풀블리드 로켓 사진이 곧 섹션 | 사진 없음 → **순수 블랙 캔버스**가 배경. 깊이는 헤어라인 + 근흑(#0a0a0a) 표면으로 |
| 80px 대문자 D-DIN 히어로 | **크롬·헤더·아이브로우·버튼만** 대문자 D-DIN(대체 Inter 700, +tracking). 답변/노트 본문은 **문장 케이스·읽기용** |
| 무채색(브랜드 액센트 금지) | 유지 — **블루 액센트 제거**. 상호작용은 화이트/고스트로. 퀴즈 정오답만 절제된 시맨틱 표시 |
| 고스트 필 CTA만 | 유지 — 투명 배경 + 1px 화이트 보더 + 대문자, radius 32px. 그림자·그라데이션 금지 |
| 그래프 노드 색상 | **무채색 노드**. 숙련도는 색이 아니라 **채움/외곽선/링**으로 구분 |

## 토큰

```
--canvas:#000000;  --canvas-soft:#0a0a0a;  --surface:#0a0a0a;
--hairline:#3a3a3f;  --on:#ffffff;  --on-mute:#a9a9b2;  --ink-faint:#6a6a72;
--danger:#ff5a5a;  (오류 텍스트만 — 브랜드 색 아님)
--sans:'Inter', -apple-system, 'Arial Narrow', Arial, sans-serif;   /* D-DIN 대체 */
--radius-xs:4px; --radius-sm:8px; --radius-md:16px; --radius-pill:32px;
간격 4/8/12/16/18/24/32/48
```

## 타이포 (Inter, D-DIN 대체)
- 디스플레이/크롬/버튼: **UPPERCASE**, 700, letter-spacing +1.2~1.6px, line-height 0.95~1.2.
- 아이브로우/섹션라벨/nav: micro-cap 12px, uppercase, tracking +0.96px.
- 본문(답변·노트·퀴즈 지문): **문장 케이스**, 400, 15px, line-height 1.6, `--on-mute`. 제목은 `--on`.
- 데이터(페이지번호 인용·메트릭·점수): 살짝 tabular 느낌, uppercase/트래킹.

## 컴포넌트
- **ghost pill 버튼**: 투명 배경, 1px `--on` 보더, `--on` 텍스트, uppercase 13px/700/+1.17px, padding 12–14px 20px, radius pill. hover = 배경 `rgba(255,255,255,.08)`. active scale .97. 그림자·그라데이션 없음.
- **버블**: user = 1px 화이트 보더 + `rgba(255,255,255,.06)` 배경, 화이트 텍스트. bot = `--canvas-soft` + 1px `--hairline`, 본문 `--on-mute`. 색 버블 금지.
- **표면/오버레이(퀴즈·노트·기록·모달)**: `--canvas-soft` + 1px `--hairline`, 그림자 없음. 모달 백드롭 `rgba(0,0,0,.7)`.
- **입력**: `--canvas-soft` + 1px `--hairline`, 포커스 시 1px `--on` + 화이트 링(접근성).
- **아이콘**: 화이트 stroke SVG(현 상태 유지), 미니멀 셰브론.

## 성장 그래프 (Obsidian식) — 시그니처
- graph.json 전체는 JS에 **참조로만** 보관, 시작 시 cytoscape는 **빈 상태**(중앙에 은은한 안내).
- `revealConcepts(ids)`: 아직 없는 개념만 노드로 추가 + 양끝이 모두 나타난 엣지 연결 → 페이드/스케일 인 + 부드러운 재배치.
- 트리거: 채팅 `done.concepts`, 숙련도 로드(누적 개념 복원), 퀴즈 개념. → **공부할수록 지도가 자란다.**
- 무채색 숙련도: known=채운 밝은 노드, learning=흐린/외곽선, due=점선 링. 색 없음.
- 노드 탭 패널·highlight·focus는 나타난 노드에만 동작.

## 반응형·접근성
- ≤900px 세로 스택 유지. 대비: 본문 `--on-mute`(#a9a9b2) on 블랙 ≈ 8:1 (AA 통과). 포커스 링 유지. `prefers-reduced-motion` 대응.

## 금지
- 브랜드 액센트 색(블루 등) 추가 금지. 다크 위 그림자/그라데이션 금지. 디스플레이 문장케이스 금지(크롬은 대문자). 채운 컬러 버튼 금지.
