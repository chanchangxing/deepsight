import pandas as pd

from .models import BreadthCounts


def classify_breadth(frame: pd.DataFrame) -> BreadthCounts:
    current = pd.to_numeric(frame["current_close"], errors="raise")
    previous = pd.to_numeric(frame["previous_close"], errors="raise")
    valid = current.notna() & previous.notna() & current.gt(0) & previous.gt(0)
    current = current[valid]
    previous = previous[valid]

    return BreadthCounts(
        up=int(current.gt(previous).sum()),
        down=int(current.lt(previous).sum()),
        flat=int(current.eq(previous).sum()),
        valid=int(valid.sum()),
    )


def add_cumulative_diff(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values("date").reset_index(drop=True).copy()
    result["cumulative_diff"] = pd.to_numeric(
        result["daily_diff"], errors="raise"
    ).astype(int).cumsum()
    return result


def calculate_index_changes(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values("date").reset_index(drop=True).copy()
    result["shanghai_close"] = pd.to_numeric(result["close"], errors="raise")
    previous_close = result["shanghai_close"].shift()
    result["shanghai_change"] = (
        result["shanghai_close"].sub(previous_close).fillna(0.0)
    )
    result["shanghai_change_pct"] = (
        result["shanghai_close"].div(previous_close).sub(1).mul(100).fillna(0.0)
    )
    return result
