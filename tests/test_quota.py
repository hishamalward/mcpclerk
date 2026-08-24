from mcpclerk.quota import QuotaBook


def test_per_run_exhaustion():
    q = QuotaBook(clock=lambda: 0.0)
    assert q.try_acquire("t", 2, None) is None
    assert q.try_acquire("t", 2, None) is None
    r = q.try_acquire("t", 2, None)
    assert r is not None and "per-run quota of 2" in r.reason and r.retry_after_s is None
    assert q.used("t") == 2  # the refused call was not counted


def test_per_minute_sliding_window():
    now = [0.0]
    q = QuotaBook(clock=lambda: now[0])
    assert q.try_acquire("t", None, 2) is None
    now[0] = 10.0
    assert q.try_acquire("t", None, 2) is None
    now[0] = 20.0
    r = q.try_acquire("t", None, 2)
    assert r is not None and r.retry_after_s == 40.0 and "2/min" in r.reason
    now[0] = 60.5  # first call has aged out
    assert q.try_acquire("t", None, 2) is None


def test_unlimited_and_per_name_isolation():
    q = QuotaBook(clock=lambda: 0.0)
    for _ in range(50):
        assert q.try_acquire("a", None, None) is None
    assert q.try_acquire("b", 1, None) is None
    assert q.try_acquire("b", 1, None) is not None
    assert q.try_acquire("a", None, None) is None
