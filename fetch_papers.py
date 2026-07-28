#!/usr/bin/env python3
"""시드 논문 PDF를 arXiv에서 받아온다.

PDF는 저장소에 넣지 않는다. 9편 중 1편(2210.02538)이 arXiv의 기본 라이선스
(nonexclusive-distrib 1.0)로 올라와 있어 **제3자 재배포가 허용되지 않고**,
1편은 CC BY-NC-SA(비상업)다. 목록·라이선스는 papers/papers.json,
배경은 papers/SOURCES.md에 있다.

    python fetch_papers.py            # 없는 것만 받는다
    python fetch_papers.py --force    # 전부 다시 받는다

표준 라이브러리만 쓴다 — 첫 실행이 `pip install` 앞에 와도 되게.
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MANIFEST = Path("papers/papers.json")
UA = "reviewPaper-fetch/1.0 (+https://github.com/alstmdqls03-ux/reviewPaper)"
DELAY_SEC = 3      # arXiv는 연속 요청 사이에 여유를 두라고 안내한다


def fetch(entry: dict, force: bool) -> str:
    """받아서 저장하고 결과 한 줄을 돌려준다. 예외는 호출부가 센다."""
    dest = Path(entry["path"])
    if dest.exists() and not force:
        return f"skip  {dest.name} (이미 있음)"
    url = f"https://arxiv.org/pdf/{entry['arxiv_id']}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise ValueError(f"PDF가 아닌 응답 ({len(data)}바이트) — {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    got = hashlib.sha256(data).hexdigest()
    if got != entry.get("sha256"):
        # 실패가 아니다. arXiv는 최신 버전(v7 등)을 준다 — 원본과 다른 판일 수 있다.
        # 조용히 넘기면 "왜 인용 쪽 번호가 어긋나지"의 원인이 안 보인다.
        return (f"ok    {dest.name} ({len(data)/1e6:.1f}MB) "
                f"⚠ 내용이 기록된 판과 다름 (arXiv가 최신 버전을 준다)")
    return f"ok    {dest.name} ({len(data)/1e6:.1f}MB)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="이미 있어도 다시 받는다")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"{MANIFEST}가 없어요.", file=sys.stderr)
        return 1
    entries = json.loads(MANIFEST.read_text())
    failed = []
    for i, e in enumerate(entries):
        try:
            print(fetch(e, args.force), flush=True)
        except (urllib.error.URLError, OSError, ValueError) as ex:
            failed.append((e["path"], f"{type(ex).__name__}: {ex}"))
            print(f"FAIL  {Path(e['path']).name} — {type(ex).__name__}: {ex}", flush=True)
        if i < len(entries) - 1:
            time.sleep(DELAY_SEC)

    have = sum(1 for e in entries if Path(e["path"]).exists())
    print(f"\n{have}/{len(entries)}편 준비됨.")
    for path, why in failed:
        # 한 편이 없어도 앱은 나머지로 돈다 — 무엇이 빠졌는지만 분명히 한다.
        print(f"  빠짐: {path} ({why})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
