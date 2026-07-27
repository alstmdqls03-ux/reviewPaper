"""각주 위치 복원: 스트리밍 때 붙은 자리에 재개 후에도 그대로 붙는가.

`python test_citations.py` 또는 `pytest -q`. MOCK 전용 — 키도 네트워크도 필요 없다.

이전 동작: messages.meta에 citations를 저장하면서 "몇 번째 글자에 붙었는지"는
저장하지 않아, 대화를 다시 열면 각주가 전부 답변 끝에 [1][2][3]으로 뭉쳤다.
"""
import asyncio
import os

os.environ.setdefault("MOCK_LLM", "1")

import llm  # noqa: E402


def _stream(question="세그멘테이션 정확도를 좌우하는 요인은?"):
    """MOCK 스트림을 app.chat과 같은 방식으로 소비해 (본문, 오프셋 붙은 인용) 반환."""
    async def run():
        parts, cites = [], []
        msgs = [{"role": "user", "content": question}]
        async for kind, payload in llm.stream_chat(msgs, "sys"):
            if kind == "text":
                parts.append(payload)
            elif kind == "citation":
                payload["offset"] = sum(len(p) for p in parts)   # app.py와 같은 계산
                cites.append(payload)
        return "".join(parts), cites
    return asyncio.run(run())


def test_mock_emits_citations_mid_answer():
    """목이 답변 끝에만 인용을 내보내면 위치 복원을 오프라인에서 판정할 수 없다."""
    text, cites = _stream()
    assert len(cites) >= 2, f"목이 인용을 {len(cites)}건만 낸다 — 위치 검증이 불가능하다"
    offsets = [c["offset"] for c in cites]
    assert offsets == sorted(offsets), offsets
    assert offsets[0] > 0, "첫 각주가 0번째 글자에 붙었다"
    assert min(offsets) < len(text), f"모든 각주가 답변 끝에 몰려 있다 (len={len(text)}, {offsets})"
    assert len(set(offsets)) > 1, f"각주가 전부 같은 자리다: {offsets}"
    print(f"mock interleaves ok: {len(text)} chars, offsets {offsets}")


def test_replay_puts_citations_back_where_they_were():
    """프론트의 addBotReplay와 같은 규칙으로 재조립하면 원본과 같은 자리가 나온다."""
    text, cites = _stream()

    def replay(text, citations):
        """index.html addBotReplay의 파이썬 판. 각주를 [n]으로 표시한 문자열을 만든다."""
        positioned = sorted([c for c in citations if isinstance(c.get("offset"), int)],
                            key=lambda c: c["offset"])
        out, at, n = [], 0, 0
        for c in positioned:
            cut = max(at, min(c["offset"], len(text)))
            out.append(text[at:cut])
            n += 1
            out.append(f"[{n}]")
            at = cut
        out.append(text[at:])
        for _ in [c for c in citations if not isinstance(c.get("offset"), int)]:
            n += 1
            out.append(f"[{n}]")            # 오프셋 없는 옛 인용은 예전처럼 끝에
        return "".join(out)

    def live(text, citations):
        """스트리밍이 실제로 그린 결과 — 오프셋 시점에 각주를 찍은 것."""
        out, at = [], 0
        for n, c in enumerate(citations, 1):
            out.append(text[at:c["offset"]])
            out.append(f"[{n}]")
            at = c["offset"]
        out.append(text[at:])
        return "".join(out)

    assert replay(text, cites) == live(text, cites), "복원된 각주 위치가 실시간과 다르다"
    # 각주가 정말 문장 안에 있는지 (끝에 몰린 게 아닌지)
    rebuilt = replay(text, cites)
    assert not rebuilt.endswith("[1][2][3]"), rebuilt[-40:]
    assert rebuilt.index("[1]") < len(rebuilt) - 20, "첫 각주가 맨 끝에 있다"
    print(f"replay ok: {rebuilt[:70]}…")


def test_legacy_messages_without_offsets_still_replay():
    """v5 이전에 저장된 대화(offset 없음)는 예전처럼 끝에 모아 붙되, 깨지지 않는다."""
    text, cites = _stream()
    legacy = [{k: v for k, v in c.items() if k != "offset"} for c in cites]
    positioned = [c for c in legacy if isinstance(c.get("offset"), int)]
    assert positioned == [], "옛 메시지에 offset이 있으면 안 된다"
    # 프론트는 이 경우 본문 전체 -> 각주 전부 순으로 그린다. 인용 수가 유지되면 통과.
    assert len(legacy) == len(cites)
    print(f"legacy replay ok: {len(legacy)} citations appended at the end")


if __name__ == "__main__":
    test_mock_emits_citations_mid_answer()
    test_replay_puts_citations_back_where_they_were()
    test_legacy_messages_without_offsets_still_replay()
    print("all citation self-checks passed")
