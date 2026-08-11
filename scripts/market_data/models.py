from dataclasses import dataclass


@dataclass(frozen=True)
class BreadthCounts:
    up: int
    down: int
    flat: int
    valid: int


@dataclass(frozen=True)
class MarketRecord:
    date: str
    shanghai_close: float
    shanghai_change: float
    shanghai_change_pct: float
    up_count: int
    down_count: int
    flat_count: int
    valid_count: int
    daily_diff: int
    cumulative_diff: int
