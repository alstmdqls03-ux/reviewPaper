# papers/ 출처와 라이선스

이 폴더의 PDF는 **저장소에 포함하지 않는다.** `python fetch_papers.py`로 arXiv에서
받아온다 (목록·체크섬: `papers/papers.json`).

## 왜 빼는가

9편의 라이선스를 arXiv abstract 페이지에서 하나씩 확인한 결과, **재배포할 수 없는
논문이 섞여 있었다.**

| 파일 | arXiv | 라이선스 | 재배포 |
|---|---|---|---|
| `01-deep-learning-in-em.pdf` | [2009.08328](https://arxiv.org/abs/2009.08328) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `02-segmentation-survey.pdf` | [2206.07171](https://arxiv.org/abs/2206.07171) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `03-microscopy-metadata.pdf` | [1910.11370](https://arxiv.org/abs/1910.11370) | CC BY-NC-SA 4.0 | ⚠️ 비상업 + 동일조건변경허락 |
| `04-connectomics-metadata.pdf` | [2401.15251](https://arxiv.org/abs/2401.15251) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `05-ml-materials-microscopy.pdf` | [2506.08423](https://arxiv.org/abs/2506.08423) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `06-microscopy-image-enhancement-survey.pdf` | [2509.15363](https://arxiv.org/abs/2509.15363) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `07-automated-multidim-tem-roadmap.pdf` | [2210.02538](https://arxiv.org/abs/2210.02538) | arXiv nonexclusive-distrib 1.0 | ❌ 재배포 불가 |
| `08-ai-scientific-inference-nanoparticle-em.pdf` | [2607.10388](https://arxiv.org/abs/2607.10388) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |
| `09-superres-microscopy-dl-review.pdf` | [2106.13064](https://arxiv.org/abs/2106.13064) | CC BY 4.0 | ✅ 재배포 가능 (저작자 표시) |

- **07 (2210.02538)** 은 arXiv의 기본 라이선스(`nonexclusive-distrib/1.0`)다. 이건
  **arXiv가 배포할 권리**를 저자가 arXiv에 준 것이고, 제3자가 다시 배포할 권리는 아니다.
- **03 (1910.11370)** 은 CC BY-NC-SA 4.0이다. 비상업 조건이라 이 저장소에 두면
  저장소 전체의 사용 조건에 영향을 준다.

한 편만 문제여도 폴더 단위로 빼는 편이 간단하다 — 어느 파일이 어느 조건인지
사람이 매번 기억해야 하는 구조를 만들지 않는다.

## 남은 한계

**과거 커밋에는 PDF가 그대로 남아 있다.** 지금 조치는 앞으로의 커밋과 clone 용량을
줄이지만, `git log`를 파면 여전히 나온다. 완전히 없애려면 히스토리 재작성이 필요하다
(`git filter-repo --path papers/ --invert-paths`). 그건 커밋 해시를 전부 바꾸므로
공개 전에 한 번만, 의도적으로 해야 한다.

## 새로 논문을 추가할 때

`papers/papers.json`에 `path`·`arxiv_id`·`title`·`license`·`sha256`을 추가한다.
라이선스는 **추측하지 말고** abstract 페이지에서 확인해서 적는다 — PDF 본문에는
라이선스 표기가 없다 (9편 모두 없었다).
