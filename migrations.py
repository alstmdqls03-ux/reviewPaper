"""1회성 데이터 이관이 이미 돌았는지 기록하는 원장.

**프레임워크가 아니다.** 스키마 변경은 지금처럼 `CREATE TABLE IF NOT EXISTS`와
조건부 `ALTER`로 충분하다 (그 둘은 몇 번 돌려도 결과가 같다). 이 파일이 푸는 문제는
딱 하나: **데이터 이관의 완료 판정**.

전에는 "대상 테이블이 비었으면 아직 안 한 것"으로 판정했다. 그 판정은 사용자가
데이터를 지운 상태와 이관 전 상태를 구분하지 못한다. 실측:

    1차 이관 후 mastery 행: 83
    사용자가 전부 삭제  -> 0
    재기동 후            -> 83   ← 지운 데이터가 되살아났다

원장이 있으면 "이관은 이미 했다"와 "데이터가 비어 있다"가 별개의 사실이 된다.

ponytail: 테이블 하나 + 함수 셋. Alembic은 이 앱에 없는 문제(브랜치·다운그레이드·
자동 diff)를 풀고 의존성을 하나 늘린다. 스키마 변경이 컬럼 의미를 바꾸기 시작하면
그때 다시 판단한다.
"""
import time

LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations(
    name TEXT PRIMARY KEY,
    applied_at REAL
);
"""


def ensure(db):
    db.executescript(LEDGER_DDL)
    db.commit()


def applied(db, name) -> bool:
    ensure(db)
    return db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (name,)).fetchone() is not None


def mark(db, name, now=None):
    """이 이관을 완료로 기록. 두 번 불러도 안전하다."""
    ensure(db)
    db.execute("INSERT OR IGNORE INTO schema_migrations(name, applied_at) VALUES(?,?)",
               (name, time.time() if now is None else now))
    db.commit()


def claim(db, name, already_done=False) -> bool:
    """이 이관을 지금 실행해야 하면 True(그리고 완료로 표시), 아니면 False.

    `already_done`은 **원장이 생기기 전에 이미 이관된 DB**를 위한 것이다. 원장에
    기록이 없는데 이 값이 참이면(예: 대상 테이블에 이미 행이 있다) 다시 돌리지 않고
    기록만 남긴다 — 원장 도입 자체가 이관을 한 번 더 트리거하면 안 된다.
    """
    if applied(db, name):
        return False
    if already_done:
        mark(db, name)
        return False
    mark(db, name)
    return True


if __name__ == "__main__":
    import sqlite3

    db = sqlite3.connect(":memory:")
    assert not applied(db, "m1")
    assert claim(db, "m1") is True, "처음이면 실행해야 한다"
    assert applied(db, "m1")
    assert claim(db, "m1") is False, "두 번째는 실행하지 않는다"

    # 원장 이전에 이미 이관된 DB: 실행하지 않고 기록만
    assert claim(db, "m2", already_done=True) is False
    assert applied(db, "m2"), "기록은 남아야 다음에도 안 돈다"
    assert claim(db, "m2") is False

    # 데이터를 지워도 이관은 다시 돌지 않는다 (이 파일의 존재 이유)
    assert claim(db, "m1") is False, "데이터 유무와 이관 완료는 별개의 사실이다"

    mark(db, "m1")  # 중복 mark 안전
    assert db.execute("SELECT COUNT(*) FROM schema_migrations WHERE name='m1'").fetchone()[0] == 1
    print("migrations.py self-check ok")
