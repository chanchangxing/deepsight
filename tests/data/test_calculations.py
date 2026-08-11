from dataclasses import asdict

import pandas as pd
import pytest

from scripts.market_data.calculations import (
    add_cumulative_diff,
    calculate_index_changes,
    classify_breadth,
)
from scripts.market_data.models import BreadthCounts, MarketRecord


def test_classify_breadth_excludes_missing_previous_close():
    frame = pd.DataFrame(
        {
            "current_close": [11.0, 9.0, 10.0, 8.0],
            "previous_close": [10.0, 10.0, 10.0, None],
        }
    )

    assert classify_breadth(frame) == BreadthCounts(up=1, down=1, flat=1, valid=3)


def test_add_cumulative_diff_rebuilds_from_zero():
    frame = pd.DataFrame(
        {"date": ["2025-01-02", "2025-01-03", "2025-01-06"], "daily_diff": [10, 10, -5]}
    )
    assert add_cumulative_diff(frame)["cumulative_diff"].tolist() == [10, 20, 15]


def test_calculate_index_changes_uses_adjacent_closes():
    frame = pd.DataFrame(
        {
            "date": ["2025-01-03", "2025-01-02", "2025-01-06"],
            "close": [3_250.0, 3_200.0, 3_217.5],
        }
    )

    result = calculate_index_changes(frame)

    assert result["date"].tolist() == ["2025-01-02", "2025-01-03", "2025-01-06"]
    assert result["shanghai_change"].tolist() == [0.0, 50.0, -32.5]
    assert result["shanghai_change_pct"].tolist() == pytest.approx([0.0, 1.5625, -1.0])


def test_market_record_contains_published_market_history_fields():
    record = MarketRecord(
        date="2025-01-02",
        shanghai_close=3_200.0,
        shanghai_change=0.0,
        shanghai_change_pct=0.0,
        up_count=1000,
        down_count=900,
        flat_count=100,
        valid_count=2000,
        daily_diff=100,
        cumulative_diff=100,
    )

    assert asdict(record)["valid_count"] == 2000
