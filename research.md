# research.md — reviewPaper 딥리서치: 벤치마킹 · 미래형 학습법 · LLM 위키

> 대상: **reviewPaper** (FastAPI + 롱컨텍스트 RAG / 전자현미경 리뷰논문 · 네이티브 인용 · Cytoscape 개념 그래프 · 개념별 숙련도+간격반복 퀴즈 · 노트 · "이해했어요" 트래킹).
> 방법: 5개 앵글 병렬 웹서치 → 26개 소스 페치 → 117개 주장 추출 → 상위 25개 3표 적대적 검증(2/3 반증 시 기각).
> 결과: **21 confirmed · 1 refuted · 3 unverified**. 작성 2026-07-24.

---

## TL;DR (3줄)

1. **벤치마킹**: 평가는 "RAG Triad(context relevance·groundedness·answer relevance)"를 LLM-as-judge로 재는 게 표준이고, **과학논문 QA엔 결정론적 인용지표(citation precision/accuracy/coverage)를 반드시 추가**해야 한다 — LLM-judge 점수는 포화돼 시스템 구분을 못 하지만 인용지표는 잘 한다. 그리고 **인용 correctness ≠ faithfulness**(맞는 인용의 최대 57%가 unfaithful).
2. **학습 루프**: 메타분석(Donoghue & Hattie 2021)이 **분산연습(d=0.85)·인출연습(테스트효과, d=0.74)**을 최상위로 랭크 → 우리의 "간격반복 퀴즈 + 이해체크" 루프가 근거로 검증됨. 단 **피드백 있는 테스트**여야 효과가 견고.
3. **LLM 위키**: DeepWiki(Devin)가 가장 근접한 작동 모델 — 코퍼스 자동 인덱싱 → 요약·다이어그램·소스 링크가 달린 위키 페이지 자동 생성. 우리 개념 그래프 + 개념 간 wikilink + 인용 근거 위키로 매핑 가능. **단 GraphRAG는 만능이 아님**(구성 비용·엔티티 커버리지 한계).

---

## Q1. 벤치마킹 — 어떻게 엄밀히 평가하나 (2024–2026)

### 핵심 프레임워크

| 지표/프레임워크 | 무엇 | 우리 적용 | 근거 |
|---|---|---|---|
| **RAG Triad** (TruLens 발원) | context relevance(검색) · groundedness(근거) · answer relevance(질문 적합) 3개를 파이프라인 단계별 1:1 매핑, LLM이 채점 | `eval.py`의 충실도 심판을 이 3축으로 재구성 | [Snowflake](https://www.snowflake.com/en/blog/engineering/benchmarking-LLM-as-a-judge-RAG-triad-metrics/), [arXiv 2407.11005] |
| **RAGalyst 지표셋** | Answer Correctness(vs 정답) · Answerability(문맥만으로 답 가능?) · Faithfulness · Answer Relevance + 검색 **Recall@K·MRR** | 우리 eval 하네스 지표 리스트로 **그대로 채택 가능** | [arXiv 2511.04502] |
| **결정론적 인용지표** | citation precision, citation accuracy(=인용이 원문의 검증가능한 substring인 비율), section coverage | **가장 중요** — 버전 랭킹은 LLM-judge 말고 이걸로 | [ResearchQA] |

### 왜 인용지표가 결정적인가 (confirmed, high)
- ResearchQA에서 **citation precision은 0.491–0.776(약 28pt 폭)**으로 시스템을 잘 구분하는데, LLM-evaluator factual accuracy는 **4.745–5.000(0.255pt)**로 포화돼 구분을 못 한다.
- → **함의**: reviewPaper의 버전 비교/회귀추적은 LLM-judge 점수가 아니라 **인용-substring 검증**을 1차 기준으로. (우리 papers.py가 페이지·인용텍스트를 갖고 있으니 substring 매칭은 저비용.)

### correctness ≠ faithfulness (confirmed, high) — 신뢰의 함정
- "인용이 주장을 뒷받침한다(correctness)"와 "모델이 실제로 그 인용에 의존했다(faithfulness)"는 다르다. 현행 attributed-RAG에서 **맞는 인용의 최대 57%가 unfaithful**(사후 합리화). [arXiv 2412.18004]
- → **함의**: 우리도 "인용이 우연히 맞는지"가 아니라 **ablation/attribution으로 faithfulness를 측정**해야 진짜 신뢰. 실험 파라미터(전압·배율)는 틀리면 치명적이므로 특히.

### 구현 가능한 인용 개선법 — SelfCite (confirmed, high)
- 자기지도(human label 불필요) 문장단위 인용 개선. **context-ablation reward의 두 조건**:
  - **necessity**: 인용 텍스트를 빼면 응답이 바뀐다/못 만든다.
  - **sufficiency**: 인용 텍스트만 남겨도 응답이 유지된다.
- LongBench-Cite에서 **citation F1 최대 +5.3pt**(최댓값, 평균 아님). [arXiv 2502.09604]
- → **함의**: 이 necessity+sufficiency ablation은 **학습 신호이자, 우리 인용을 채점하는 확률적 metric**으로 재사용 가능. MOCK 말고 실키 붙으면 우선 적용 후보.

### LLM-as-judge는 사람 대체 가능한가 (confirmed, high) — 단 조건부
- RAGalyst Answer Correctness가 사람과 **Spearman 0.874**(cosine 0.622·RAGAS 0.843 상회). TREC 2024 RAG Track: GPT-4o support 판정이 사람과 **56% 완전일치(후편집 시 72%)**, 심지어 두 번째 사람보다 상관 높음. [arXiv 2511.04502, 2504.15205]
- **반대 근거(주의)**: off-the-shelf LLM judge는 프롬프트에 민감해 신뢰성 제한적 → eval-guided 최적화 필요. 30쿼리 통제연구에서 RAGAS·DeepEval·LLM-judge가 같은 출력에 **substantially 불일치**(groundedness는 1.0 근처로 포화). [ResearchGate 30-query study]
- → **함의**: LLM-judge는 쓰되 **결정론적 인용지표를 앵커**로 삼고, judge 프롬프트를 우리 골드셋으로 보정.

### 바로 쓸 수 있는 벤치마크/코퍼스
- **ResearchQA** — 494편 open-access 논문서 **6,211 single-paper QA**, 4문항유형: lookup/extractive · comprehension/abstractive · multi-hop · **adversarial false-premise(정답=근거 기반 거부)**. → 우리 EM 리뷰논문용 골드셋 만들 **템플릿**. (특히 false-premise→거부는 `eval_gold.py`에 추가할 것.)
- **SCALAR** — 인용구조로 **정답 자동생성**(사람 주석 X), 난이도 조절·동적 갱신. 임계: 사람 >90%, 대부분 LLM은 어려운 cloze 인용예측서 **>50% 못 넘김**. → "지속 갱신되는 오염저항 벤치" 설계 모델. [arXiv 2502.13753]
- **SummHay** — 다문서(~93k토큰) 요약을 **Coverage(핵심 insight 포함) + Citation(출처 정확도)** 2축 자동채점. → 우리 다논문 종합답변 평가 템플릿. [arXiv 2407.01370]

### 우리가 채택할 임계(초안)
- 검색: **Recall@K, MRR** 로깅.
- 생성: RAG Triad 3축 + Answerability.
- **인용(1차 랭킹지표)**: citation accuracy(=substring 검증 비율) 목표 초안 **≥0.8**, false-premise 질문에 **거부율 ≥0.9**.
- faithfulness: SelfCite necessity/sufficiency ablation을 샘플에 적용(실키 이후).

---

## Q2. 미래형 학습 — 가장 효과적인 학습 루프

### 근거가 우리 설계를 검증한다 (confirmed, high)
- Donoghue & Hattie(2021) 메타분석(242연구·169,179명, 10기법 전부 교육 벤치 0.40 초과):
  - **분산/간격 연습(distributed practice) d=0.85** — 단일 최고.
  - **인출연습/테스트효과(practice testing) d=0.74** — 2위. Adesope 2017(g=0.74)로 독립 확증.
- **테스트효과의 우위는 "피드백이 있을 때" 가장 견고**.
- → **함의**: reviewPaper의 **간격반복 퀴즈 + "이해했어요" 체크 루프가 정확히 이 두 최강 기법**. 단 **오답 시 피드백**(왜 틀렸는지 + 원문 인용)이 반드시 붙어야 효과 유지. 지금 오답→약점개념 복습 연결이 이 방향.

### 주의: 효과크기는 방향성으로만
- 분산연습 **d=0.85는 문헌 상단값**(Cepeda 2006·2025 교실 메타 d≈0.54는 더 낮음). 이 연구의 결론으론 정확하나 **보편 상수로 과신 금지**.

### 검증 안 된 것 (open) — 스케줄러는 별도 조사 필요
- **SM-2 vs FSRS 등 구체 스케줄링 알고리즘 비교는 이번에 검증된 근거로 확보 못 함**. 우리 SM-2-lite를 FSRS로 갈지는 **타깃 후속조사 필요**.
- AI 튜터의 개인별 마스터리 모델(우리 개념별 숙련도와 유사) 주장은 **인프라 에러로 미검증**(neither confirmed nor refuted).
- ⚠️ **반증됨(1-2)**: "AI 튜터가 활성사용자 성적을 최대 15퍼센타일 올렸다(N=51)" 주장은 **신뢰 금지**. [arXiv 2309.13060]
- LECTOR(LLM 강화 적응형 간격반복, 의미유사도로 개념 혼동 감소) — 미검증(2 errored). [arXiv 2508.03275]

### 설계 결론
- 코어 루프 = **간격반복 스케줄 + 피드백 있는 인출 퀴즈** (근거 강함, 유지).
- "이해했어요"를 **가르치기 게이트**(3문장 설명 통과)로 올리는 건 자기설명/테스트효과 원리엔 부합하나, **효과 수치는 근거 약함** → 실험적 기능으로.
- FSRS 도입은 **근거 확인 후** 결정(현 오픈 이슈).

---

## Q3. LLM 위키 — 챗봇에 위키형 지식베이스 얹기

### 참조 모델: DeepWiki/Devin (confirmed, medium)
- 코퍼스 자동 인덱싱 → **요약 + 아키텍처 다이어그램 + 소스 라인 참조**가 달린 위키 페이지 생성 + 대화형 Q&A. [Devin docs]
- → **우리 매핑**: 개념(=그래프 노드)마다 **자동 생성 위키 페이지** = {요약 + 인용 근거(cited_text·page) + 인접 개념 wikilink + 숙련도 상태}. 이미 있는 Cytoscape 그래프가 위키의 네비게이션, "개념 패널"이 위키 페이지의 씨앗.
- 신뢰도 medium: 단일 벤더 1차 소스(2-0), "논문 코퍼스 위키" 프레이밍은 코드리포 제품에서의 외삽.

### GraphRAG는 만능이 아니다 (소스 추출 근거 — 상위25 검증셋 밖, 1차 소스)
> 아래는 페치된 1차 소스에서 추출된 pitfall들. 적대적 3표 검증셋(상위25)엔 안 들었지만, 소스 품질은 primary. 설계 리스크로 참고.
- **엔티티 커버리지 한계**: 구성된 KG에 답 엔티티의 **~65%만 등장**(HotpotQA 65.8%, NQ 65.5%) → 쿼리 전에 이미 상한. [RAG vs GraphRAG systematic eval, arXiv 2502.11371]
- **비용**: 풀 Microsoft GraphRAG는 LLM 기반 엔티티/관계 추출로 인덱싱 비쌈. **LazyGraphRAG는 NLP 명사구 추출로 대체 + LLM은 쿼리시로 미룸 → 인덱싱 비용이 vector RAG 수준(풀 GraphRAG의 0.1%)**. [Microsoft LazyGraphRAG]
- **VectorRAG 한계**: 2–3 hop 넘는 다단계 추론서 성능 **지수적 저하** → 개념 연결형 QA엔 취약. [FalkorDB]
- → **우리 결론(ponytail)**: 우리는 논문 ≤5편 롱컨텍스트라 **풀 GraphRAG 불필요**. 위키 페이지는 **LLM이 개념별로 1회 생성 + 인용 근거 첨부**(LazyGraphRAG 정신: 무거운 그래프 구성 대신 필요할 때 생성)로 충분. 개념 그래프는 이미 우리가 손수 가진 것.

### 구현 스케치 (우리 스택에 얹는 최소안)
1. **개념 위키 페이지 = 그래프 노드의 확장**: 노드 클릭 시 지금은 요약/출처만 → 여기에 **자동 생성 위키 섹션**(정의·핵심·관련개념 wikilink·인용 근거·내 숙련도) 추가.
2. **wikilink**: 위키 텍스트 내 다른 개념명을 `[[개념]]`으로 링크 → 클릭 시 그 노드로 이동(우리 `focusConcept` 재사용).
3. **인용 근거 필수**: 위키 각 문장에 원문 substring 인용(Q1의 결정론적 지표로 검증). 근거 없으면 "본문에 없음".
4. **증분 갱신**: 새 PDF 업로드 시 해당 개념 페이지만 재생성(전체 재빌드 금지).
5. **검증 게이트**: 위키 페이지도 citation accuracy로 채점(할루시네이션 위키가 최악).

---

## 종합 로드맵 (우리 프로젝트 반영)

| 우선 | 항목 | 근거 | 상태 |
|---|---|---|---|
| P1 | eval에 **결정론적 인용지표**(citation accuracy=substring) 추가, 버전 랭킹 1차 기준화 | Q1 ResearchQA | 신규 |
| P1 | `eval_gold.py`에 **adversarial false-premise → 거부** 케이스 추가 | Q1 ResearchQA | 신규 |
| P2 | 퀴즈에 **오답 피드백(원문 인용)** 보강 — 테스트효과 견고화 | Q2 Hattie | 일부 존재 |
| P2 | 개념 노드 패널 → **자동 생성 위키 섹션 + wikilink + 인용근거** | Q3 DeepWiki | 신규(그래프·패널 기반 존재) |
| P3 | (실키 후) **SelfCite necessity/sufficiency**로 인용 faithfulness 측정 | Q1 SelfCite | 신규 |
| P3 | FSRS 도입 여부 — **근거 후속조사 후 결정** | Q2 open | 보류 |

---

## Caveats (신뢰도 경계)
- 급변 분야: SCALAR/ResearchQA 임계(사람>90%, LLM cloze<50%)는 2025초~2026중 모델 스냅샷 — 신모델로 이동.
- RAGalyst(2511.04502)는 미심사 preprint·자기지표 자기보고·좁은 500쌍 STS-B·내부표 불일치(0.836 vs 0.843).
- RAG Triad 신뢰성 주장은 벤더(Snowflake/TruLens) 블로그 의존, 그들 후속연구는 off-the-shelf judge가 중간 신뢰라고 밝힘.
- 분산연습 d=0.85는 상단값(다른 연구 ~0.54) — 방향성으로만.
- DeepWiki는 단일 벤더 1차소스(2-0), 논문-코퍼스 위키 프레이밍은 코드리포 제품서 외삽.

## Open Questions
1. SM-2 vs FSRS 등 **스케줄러 알고리즘** 근거 — 미확보, 후속 필요.
2. 프로덕션에서 인용 **faithfulness 측정**(necessity/sufficiency ablation)의 런타임 비용·보정.
3. AI 학습툴의 **검증된 retention/mastery 목표치** — 이번엔 미확보(튜터 효능 주장은 반증/미검증).
4. **증분 KB 갱신 · GraphRAG식 엔티티 추출 + wikilink**의 구체 파이프라인·pitfall — DeepWiki 유추 이상은 미검증.

## Refuted (신뢰 금지)
- "AI 튜터('Ambassador')가 활성사용자 성적 최대 15퍼센타일↑(N=51)" — 3표 중 1-2로 **반증**. [arXiv 2309.13060]

---

## 소스 (26 페치, 인용된 것 위주)
**RAG 평가/지표**
- Snowflake — RAG Triad LLM-judge 벤치: https://www.snowflake.com/en/blog/engineering/benchmarking-LLM-as-a-judge-RAG-triad-metrics/
- RAG Triad 학술(RAGBench/CCRS): https://arxiv.org/pdf/2407.11005
- RAGalyst(도메인 특화 지표셋): https://arxiv.org/pdf/2511.04502
- TREC 2024 RAG Track(human vs LLM judge): https://arxiv.org/pdf/2504.15205
- 30-query abstention 통제연구(RAGAS/DeepEval/judge 불일치): https://www.researchgate.net/publication/399331938

**과학논문 QA / 롱컨텍스트 / 인용**
- ResearchQA(6,211 QA·인용지표): https://www.researchgate.net/publication/409442431_ResearchQA_Benchmarking_Citation-Grounded_Question-Answering_on_Scientific_Papers
- SCALAR(오염저항 live 벤치): https://arxiv.org/pdf/2502.13753
- SummHay(Coverage+Citation 자동채점): https://arxiv.org/pdf/2407.01370
- SelfCite(necessity+sufficiency ablation): https://arxiv.org/pdf/2502.09604
- Citation correctness≠faithfulness(57% unfaithful): https://arxiv.org/pdf/2412.18004

**학습과학 / 튜터링**
- Donoghue & Hattie 2021 메타분석(분산·인출 최상위): https://www.frontiersin.org/journals/education/articles/10.3389/feduc.2021.581216/full
- FSRS vs SM-2(블로그): https://www.antiagent.io/blog/fsrs-vs-sm-2 · https://help.remnote.com/en/articles/9124137
- AI 튜터(반증된 주장 출처): https://arxiv.org/pdf/2309.13060
- LECTOR(미검증): https://arxiv.org/pdf/2508.03275

**GraphRAG / 자동 위키 KB**
- DeepWiki(Devin): https://docs.devin.ai/work-with-devin/deepwiki
- Microsoft GraphRAG: https://github.com/microsoft/graphrag
- LazyGraphRAG(0.1% 비용): https://www.microsoft.com/en-us/research/blog/lazygraphrag-setting-a-new-standard-for-quality-and-cost/
- RAG vs GraphRAG systematic eval(65% 엔티티 커버리지): https://arxiv.org/html/2502.11371v3
- LightRAG: https://learnopencv.com/lightrag/ · Graphiti(KG memory): https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/
- VectorRAG vs GraphRAG 한계(블로그): https://www.falkordb.com/blog/vectorrag-vs-graphrag-technical-challenges-enterprise-ai-march25/

*통계: 5앵글 · 26소스 · 117주장 추출 · 25검증 · 21 confirmed / 1 refuted / 3 unverified · 108 에이전트.*
