from api_service.services import rate_limit
from api_service.services.rate_limit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_allows_burst_up_to_capacity_then_blocks():
    limiter = RateLimiter(per_minute=3, clock=FakeClock())
    assert [limiter.allow("a") for _ in range(4)] == [True, True, True, False]


def test_refills_over_time():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, clock=clock)
    for _ in range(60):
        assert limiter.allow("a")
    assert not limiter.allow("a")

    clock.now += 1.0  # 60/min refills one token per second
    assert limiter.allow("a")
    assert not limiter.allow("a")


def test_keys_are_independent():
    limiter = RateLimiter(per_minute=1, clock=FakeClock())
    assert limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.allow("b")


def test_tracked_keys_are_bounded(monkeypatch):
    monkeypatch.setattr(rate_limit, "MAX_TRACKED_KEYS", 10)
    clock = FakeClock()
    limiter = RateLimiter(per_minute=60, clock=clock)
    for i in range(10):
        limiter.allow(f"k{i}")

    clock.now += 120  # every bucket has refilled
    limiter.allow("new")

    assert len(limiter._buckets) == 1
