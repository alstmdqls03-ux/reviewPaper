# papers/ 출처와 라이선스

이 폴더의 PDF는 **제3자 저작물**이다. 저장소를 공개로 바꾸기 전에 각 논문의
재배포 허용 여부를 확인해야 한다 — arXiv에 올라온 논문이라고 모두 재배포가
허용되는 것은 아니다. arXiv의 기본 라이선스(non-exclusive license to distribute)는
**arXiv가 배포할 권리**이지 제3자의 재배포 권리가 아니다. CC-BY / CC-BY-SA로
올라온 것만 조건을 지키면 재배포할 수 있다.

PDF 본문에서 자동 추출한 결과, **9편 모두 라이선스 표기를 찾지 못했다.**
arXiv ID도 3편만 본문에서 잡혔다. 나머지는 abstract 페이지에서 직접 확인해야 한다.

| 파일 | 제목 | arXiv | 라이선스 |
|---|---|---|---|
| `01-deep-learning-in-em.pdf` | Review: Deep Learning in Electron Microscopy (Ede 2021) | **미확인** | ❓ 미확인 |
| `02-segmentation-survey.pdf` | Segmentation in Large-Scale Cellular EM: A Literature Survey (Aswath 2023) | [2206.07171](https://arxiv.org/abs/2206.07171) | ❓ 미확인 |
| `03-microscopy-metadata.pdf` | A Perspective on Microscopy Metadata: Provenance & Quality Control (Huisman 2019) | **미확인** | ❓ 미확인 |
| `04-connectomics-metadata.pdf` | EM & XRM Connectomics Imaging & Experimental Metadata Standards (Wimbish 2024) | [2401.15251](https://arxiv.org/abs/2401.15251) | ❓ 미확인 |
| `05-ml-materials-microscopy.pdf` | ML for Electron & Scanning Probe Microscopy — Mic-Hackathon 2024 | **미확인** | ❓ 미확인 |
| `06-microscopy-image-enhancement-survey.pdf` | Recent Advancements in Microscopy Image Enhancement using Deep Learning: A Survey (Dutta 2025) | [2509.15363](https://arxiv.org/abs/2509.15363) | ❓ 미확인 |
| `07-automated-multidim-tem-roadmap.pdf` | A Roadmap for Edge-Computing-Enabled Automated Multidimensional TEM (Mukherjee 2022) | **미확인** | ❓ 미확인 |
| `08-ai-scientific-inference-nanoparticle-em.pdf` | The Evolution of AI from Image Interpretation toward Scientific Inference in Nanoparticle EM (Toulkeridou 2026) | **미확인** | ❓ 미확인 |
| `09-superres-microscopy-dl-review.pdf` | Advancing Biological Super-Resolution Microscopy through Deep Learning: A Brief Review (Yang 2021) | **미확인** | ❓ 미확인 |

## 확인 방법

arXiv abstract 페이지 우하단의 라이선스 표기를 본다.
- `arXiv.org perpetual, non-exclusive license` → **재배포 불가**. 저장소에서 빼고 다운로드 스크립트로
- `CC BY 4.0` / `CC BY-SA 4.0` → 저작자 표시하면 재배포 가능
- `CC BY-NC` → 비상업 조건. 공개 저장소는 대개 괜찮지만 조건을 명시해야 한다

한 편이라도 재배포 불가면 `papers/*.pdf`를 추적에서 빼고 `fetch_papers.py`로
받아오게 바꾸는 편이 간단하다. 과거 커밋에도 남으므로, 완전 제거가 필요하면
히스토리 재작성(git filter-repo)까지 가야 한다.
