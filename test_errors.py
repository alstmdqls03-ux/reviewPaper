"""API 실패 경로: 실패는 우리가 만들 수 있으므로 실키 없이 판정한다.

실키가 필요한 건 "성공이 어떻게 생겼나"이고, 실패는 SDK 예외를 던지는 스텁으로
그대로 재현된다. 여기서 보는 건 세 가지다.

  1. 화면에 무엇이 뜨나  — 예전엔 `RateLimitError: Error code: 429 - {...}`가 그대로
  2. 429의 Retry-After  — 예전엔 읽지 않고 버렸다
  3. 부분 답변          — 예전엔 기록에 안 남아, 새로고침하면 통째로 사라졌다.
                          토큰 비용은 이미 나간 뒤다 (300쪽을 읽힌 답일 수 있다)

`python test_errors.py` 또는 `pytest -q`. MOCK-safe — 키도 네트워크도 안 쓴다.
"""
import asyncio
import json
import os

os.environ.setdefault("MOCK_LLM", "1")

import app  # noqa: E402
import llm  # noqa: E402


class _Resp:
    def __init__(self, headers=None):
        self.headers = headers or {}


class FakeAPIError(Exception):
    """anthropic의 APIStatusError와 같은 모양 (status_code + response.headers)."""

    def __init__(self, status, headers=None, body="Error code: %s - {'error': ...}"):
        super().__init__(body % status if "%s" in body else body)
        self.status_code = status
        self.response = _Resp(headers)


def _events(body, fail_after=0, exc=None):
    """/chat을 돌리고 SSE 이벤트를 리스트로 돌려준다.

    fail_after=0 이면 아무것도 흘리기 전에 죽는다(호출 자체가 실패한 경우).
    fail_after=N 이면 텍스트 조각 N개를 보낸 뒤 죽는다(스트림 중단 재현).
    """
    real = llm.stream_chat

    async def flaky(messages, system):
        if not fail_after:
            raise exc                     # 첫 토큰 전에 실패
        n = 0
        async for kind, payload in real(messages, system):
            if kind == "text":
                n += 1
                if n > fail_after:
                    raise exc
            yield kind, payload

    llm.stream_chat = flaky if exc else real
    try:
        async def run():
            resp = await app.chat(body, x_device_id="errdev")
            out = []
            async for chunk in resp.body_iterator:
                for line in chunk.split("\n"):
                    if line.startswith("data:"):
                        out.append(json.loads(line[5:].strip()))
            return out
        return asyncio.run(run())
    finally:
        llm.stream_chat = real


def _err(evs):
    return next((e for e in evs if e.get("type") == "error"), None)


def test_happy_path_still_works():
    """폴트 주입 장치가 정상 경로를 망가뜨리지 않는다 (다른 검사의 기준선)."""
    evs = _events(app.ChatIn(message="세그멘테이션이란?"))
    assert _err(evs) is None, _err(evs)
    assert any(e["type"] == "done" for e in evs)
    assert "".join(e["text"] for e in evs if e["type"] == "text").strip()
    print("happy path ok")


def test_rate_limit_says_when_to_retry():
    """429는 사람이 읽는 문구 + Retry-After 초를 화면으로 내보낸다."""
    evs = _events(app.ChatIn(message="429 테스트"),
                  exc=FakeAPIError(429, {"retry-after": "12"}))
    e = _err(evs)
    assert e, evs
    assert e["retry_after"] == 12, e
    assert "12초" in e["message"], e
    # 원본 예외 문자열이 화면에 새면 안 된다 (요청 내용이 되비칠 수 있다)
    assert "Error code" not in e["message"] and "RateLimitError" not in e["message"], e
    print(f"429 ok: {e['message']}")


def test_overloaded_and_auth_and_unknown():
    """529/401/알 수 없는 예외도 각각 다른 문구로 나간다 (같은 문구면 대응이 안 된다)."""
    cases = [
        (FakeAPIError(529), "혼잡", True),      # 재시도 안내가 있어야 한다
        (FakeAPIError(401), "관리자", False),   # 사용자가 할 일이 없다
        (RuntimeError("boom"), "다시 시도", False),
    ]
    seen = set()
    for exc, want, has_retry in cases:
        e = _err(_events(app.ChatIn(message="실패 테스트"), exc=exc))
        assert e, exc
        assert want in e["message"], (exc, e)
        assert bool(e["retry_after"]) is has_retry, (exc, e)
        assert "boom" not in e["message"], e   # 내부 메시지 노출 금지
        seen.add(e["message"])
    assert len(seen) == 3, seen                # 세 상황이 구분돼야 한다
    print("529/401/unknown ok")


def test_partial_answer_survives_a_broken_stream():
    """중간에 끊겨도 받은 답은 기록에 남는다 — 토큰은 이미 지불했다."""
    body = app.ChatIn(message="중간에 끊기는 답변")
    evs = _events(body, fail_after=2, exc=FakeAPIError(529))
    got = "".join(e["text"] for e in evs if e["type"] == "text")
    assert got.strip(), "끊기기 전에 아무 텍스트도 안 나왔다 — 검사가 성립 안 한다"
    e = _err(evs)
    assert e and e["partial_saved"] is True, e

    conv_id = next(x["conversation_id"] for x in evs if x["type"] == "session")
    msgs = app.HIST.get_messages(conv_id)
    assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
    assert msgs[1]["content"] == got, (msgs[1]["content"], got)
    meta = msgs[1].get("meta") or {}
    assert meta.get("partial") is True, meta   # 완전한 답인 척하면 안 된다
    print(f"partial saved ok: {len(got)}자")


def test_nothing_streamed_means_nothing_saved():
    """첫 조각 전에 죽으면 빈 답변을 기록에 남기지 않는다."""
    body = app.ChatIn(message="즉시 실패")
    evs = _events(body, exc=FakeAPIError(429))
    e = _err(evs)
    assert e["partial_saved"] is False, e
    conv_id = next(x["conversation_id"] for x in evs if x["type"] == "session")
    assert app.HIST.get_messages(conv_id) == [], app.HIST.get_messages(conv_id)
    print("empty failure leaves no row ok")


if __name__ == "__main__":
    test_happy_path_still_works()
    test_rate_limit_says_when_to_retry()
    test_overloaded_and_auth_and_unknown()
    test_partial_answer_survives_a_broken_stream()
    test_nothing_streamed_means_nothing_saved()
    print("all error-path self-checks passed")
