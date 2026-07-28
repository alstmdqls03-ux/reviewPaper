"""Source selection: ids are stable, and a selection narrows what the model reads.

`python test_sources.py` or `pytest -q`. MOCK-safe — no key, no network.
"""
import os

os.environ.setdefault("MOCK_LLM", "1")

import corpus  # noqa: E402


def test_ids_stable_and_unique():
    reg = corpus.load_corpus()
    assert reg, "corpus registry is empty"
    assert all(e.get("id") for e in reg), "every entry needs an id"
    assert len({e["id"] for e in reg}) == len(reg), "source ids must be unique"
    # stable across calls — the UI persists these in localStorage
    assert [e["id"] for e in corpus.load_corpus()] == [e["id"] for e in reg]
    assert corpus.source_id("papers/x.pdf") == corpus.source_id("papers/x.pdf")
    print(f"source ids ok: {len(reg)} entries")


def test_selection_narrows_titles():
    import app

    reg = corpus.load_corpus()
    # no selection -> the whole registry is reported as used
    _, all_titles = app._pick_sources(None)
    assert all_titles == [e["title"] for e in reg], all_titles

    # a two-source selection -> exactly those two titles, in registry order
    want = [reg[0]["id"], reg[2]["id"]]
    _, titles = app._pick_sources(want)
    assert titles == [reg[0]["title"], reg[2]["title"]], titles

    # deselecting a source removes its title
    _, without = app._pick_sources([e["id"] for e in reg if e["id"] != reg[0]["id"]])
    assert reg[0]["title"] not in without
    assert len(without) == len(reg) - 1

    # 전부 알 수 없는 id면 거절한다. 예전엔 전체 레지스트리로 되돌아갔는데,
    # 그건 "이 3편만 봐줘"라고 말한 사람에게 10편으로 답하는 동작이었다.
    try:
        app._pick_sources(["nope"])
        raise AssertionError("unknown-only selection fell back to the whole corpus")
    except ValueError:
        pass
    # 일부만 알 수 없는 id면 아는 것으로 진행한다 (소스 하나 지워졌다고 막지 않는다)
    _, partial = app._pick_sources([reg[0]["id"], "nope"])
    assert partial == [reg[0]["title"]], partial
    print(f"selection ok: {len(all_titles)} -> {len(titles)} titles")


def test_uploads_are_private_to_their_owner():
    """v2 격리: 남이 올린 PDF는 목록에도, 답변 근거에도 들어오지 않는다."""
    import json
    import app

    # 레지스트리가 SQLite로 옮겨간 뒤로는 테스트 행을 트랜잭션으로 되돌린다.
    # (예전엔 corpus.json 바이트를 백업/복원했다)
    added = []
    try:
        added += [corpus.add_pdf("uploads/userA/a.pdf", "A's private paper", owner="userA")["id"],
                  corpus.add_pdf("uploads/userB/b.pdf", "B's private paper", owner="userB")["id"]]
        a_titles = [e["title"] for e in corpus.visible_corpus("userA")]
        b_titles = [e["title"] for e in corpus.visible_corpus("userB")]
        assert "A's private paper" in a_titles and "B's private paper" not in a_titles
        assert "B's private paper" in b_titles and "A's private paper" not in b_titles
        # shared seeds stay visible to both
        shared = {t for _, t in corpus.shared_papers()}
        assert shared and shared <= set(a_titles) and shared <= set(b_titles)

        # guessing B's source id from A's session must not surface B's paper
        b_id = next(e["id"] for e in corpus.load_corpus() if e["title"] == "B's private paper")
        try:
            _, leaked = app._pick_sources([b_id], user_id="userA")
            raise AssertionError(f"B's paper reachable from A's session: {leaked}")
        except ValueError:
            pass  # 남의 id만 보내면 아무것도 안 보낸다 (예전엔 A의 전체 목록으로 되돌아갔다)
        # A 자신의 것과 섞어 보내도 B의 논문은 절대 안 붙는다
        a_id = next(e["id"] for e in corpus.load_corpus() if e["title"] == "A's private paper")
        _, mixed = app._pick_sources([a_id, b_id], user_id="userA")
        assert mixed == ["A's private paper"], mixed
        print(f"isolation ok: A sees {len(a_titles)}, B sees {len(b_titles)}")
    finally:
        db = corpus.connect()               # never leave test rows in the real registry
        with db:
            for sid in added:
                db.execute("DELETE FROM sources WHERE id = ?", (sid,))
        assert "A's private paper" not in {e["title"] for e in corpus.load_corpus()}


def test_source_page_and_suggestions():
    """v5: 원문 뷰어가 읽는 페이지 텍스트와, 선택에서 파생되는 추천 질문."""
    import asyncio
    import app
    from fastapi import HTTPException

    reg = corpus.load_corpus()
    sid = reg[2]["id"]  # 가장 작은 시드 논문
    page = asyncio.run(app.source_page(sid, 1, x_device_id="testdev"))
    assert page["page"] == 1 and page["total_pages"] > 1, page
    assert page["text"].strip(), "first page extracted no text"
    assert page["title"] == reg[2]["title"]

    for bad in [(sid, 0), (sid, page["total_pages"] + 1)]:
        try:
            asyncio.run(app.source_page(*bad, x_device_id="testdev"))
            raise AssertionError(f"out-of-range page {bad[1]} should 404")
        except HTTPException as e:
            assert e.status_code == 404

    try:  # a source id that isn't visible must 404, not leak
        asyncio.run(app.source_page("deadbeefdead", 1, x_device_id="testdev"))
        raise AssertionError("unknown source should 404")
    except HTTPException as e:
        assert e.status_code == 404

    # suggestions: narrowing the selection narrows the questions
    wide = asyncio.run(app.suggestions(sources="", x_device_id="testdev"))
    narrow = asyncio.run(app.suggestions(sources=sid, x_device_id="testdev"))
    assert 1 <= len(wide["suggestions"]) <= 4, wide
    assert 1 <= len(narrow["suggestions"]) <= 4, narrow
    assert narrow["from_sources"] == 1 and wide["from_sources"] == len(reg)
    assert all(s.strip().endswith("?") or "설명해줘" in s for s in narrow["suggestions"]), narrow
    print(f"viewer+suggestions ok: {page['total_pages']} pages, {len(narrow['suggestions'])} questions")


def test_empty_selection_is_not_everything():
    """소스를 전부 해제한 채로 보내면 전체 코퍼스로 되돌아가면 안 된다.

    이전 동작: `picked or reg` 때문에 sources=[]가 조용히 "전부"가 됐다. 채팅은
    클라이언트 가드가 막고 있었지만 /quiz는 그대로 통과해서, 방금 해제한 10편에서
    문제가 생성되고 문제 카드 '출처:'에 해제한 논문 제목이 찍혔다.
    """
    import asyncio

    import app
    from fastapi import HTTPException

    reg = corpus.load_corpus()
    _, all_titles = app._pick_sources(None)          # 미선택은 지금도 전체
    assert len(all_titles) == len(reg)

    for empty in ([], ):                              # 빈 선택은 거절
        try:
            app._pick_sources(empty)
            raise AssertionError("empty selection silently fell back to the whole corpus")
        except ValueError as e:
            assert "1개 이상" in str(e), e

    try:                                              # 존재하지 않는 id만 보내도 거절
        app._pick_sources(["deadbeefdead"])
        raise AssertionError("unknown-only selection fell back to the whole corpus")
    except ValueError:
        pass

    # 엔드포인트는 500이 아니라 400으로 나가야 한다 (고칠 수 있는 문구와 함께)
    body = app.QuizIn(session_id="nosuch", sources=[])
    try:
        asyncio.run(app.quiz(body, x_device_id="testdev"))
        raise AssertionError("quiz with an empty selection should not succeed")
    except HTTPException as e:
        assert e.status_code == 400, e.status_code

    # 추천 질문은 막지 않고 빈 목록으로 물러난다 (질문창을 잠글 이유가 없다)
    out = asyncio.run(app.suggestions(sources="deadbeefdead", x_device_id="testdev"))
    assert out["suggestions"] == [] and out["from_sources"] == 0, out
    print("empty-selection guard ok")


def test_processing_sources_are_not_used_as_evidence():
    """업로드 직후 아직 처리 중인 소스는 답변 근거에 안 들어간다.

    Files API에 안 올라갔고 텍스트 추출도 전이라 근거가 될 수 없다. 조용히 빼면
    "고른 소스가 답변에 없다"가 되므로, 그것만 골랐을 때는 이유를 말해야 한다.
    """
    import app

    added = None
    try:
        e = corpus.add_pdf("uploads/proc/pending.pdf", "처리 중인 논문",
                           owner="procuser", status="processing")
        added = e["id"]
        assert e["status"] == "processing", e

        # 전체 선택(None)에서도 빠진다
        _, titles = app._pick_sources(None, user_id="procuser")
        assert "처리 중인 논문" not in titles, titles

        # 그것만 고르면 400이 아니라 "처리 중"이라고 말한다
        try:
            app._pick_sources([added], user_id="procuser")
            raise AssertionError("처리 중인 소스가 근거로 쓰였다")
        except ValueError as msg:
            assert "처리 중" in str(msg), msg

        # ready가 되면 즉시 쓰인다
        corpus.set_status(added, "ready")
        _, after = app._pick_sources([added], user_id="procuser")
        assert after == ["처리 중인 논문"], after

        # error 상태도 근거에서 빠진다
        corpus.set_status(added, "error", "추출 실패")
        _, titles2 = app._pick_sources(None, user_id="procuser")
        assert "처리 중인 논문" not in titles2, titles2
        print("processing/error 소스 제외 ok")
    finally:
        if added:
            db = corpus.connect()
            with db:
                db.execute("DELETE FROM sources WHERE id=?", (added,))


def test_context_budget_blocks_before_the_api_does():
    """고른 소스가 모델 창을 넘으면 400으로 먼저 막는다.

    안 막으면 실키에서 API가 400을 던지고, 화면에는 무엇을 줄여야 하는지 없는
    실패만 남는다. 추정은 보수적이어야 한다 — 실제보다 크게 잡아야 먼저 막힌다.
    """
    import app
    from config import settings

    reg = corpus.ensure_estimates(corpus.load_corpus())
    total = sum(e["est_tokens"] or 0 for e in reg)
    assert total > 0, "토큰 추정이 하나도 안 채워졌다"

    # 지금 코퍼스는 한도 안이라 통과해야 한다
    _, titles = app._pick_sources(None)
    assert len(titles) == len(reg)

    orig = settings.MAX_CONTEXT_TOKENS
    try:
        settings.MAX_CONTEXT_TOKENS = total // 2          # 한도를 반으로 낮춘다
        try:
            app._pick_sources([e["id"] for e in reg])
            raise AssertionError("한도를 넘겼는데 통과했다")
        except ValueError as e:
            msg = str(e)
            assert "너무 커요" in msg, msg
            assert "토큰" in msg and "해제" in msg, msg   # 무엇을 해야 하는지 적혀 있어야 한다
            assert "가장 큰 소스" in msg, msg              # 어느 것부터 뺄지도
        # 한 편만 고르면 다시 통과
        smallest = min(reg, key=lambda e: e["est_tokens"] or 0)
        _, one = app._pick_sources([smallest["id"]])
        assert one == [smallest["title"]]
    finally:
        settings.MAX_CONTEXT_TOKENS = orig
    print(f"context budget ok: {total:,} tokens across {len(reg)} sources")


if __name__ == "__main__":
    test_ids_stable_and_unique()
    test_selection_narrows_titles()
    test_uploads_are_private_to_their_owner()
    test_source_page_and_suggestions()
    test_empty_selection_is_not_everything()
    test_processing_sources_are_not_used_as_evidence()
    test_context_budget_blocks_before_the_api_does()
    print("all source self-checks passed")
