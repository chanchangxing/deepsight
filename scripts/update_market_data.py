"""Command-line entry point for publishing market breadth data."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.market_data.pipeline import MarketDataPipeline


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update A-share market breadth history")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--full", action="store_true", help="rebuild from 2025-01-01")
    parser.add_argument(
        "--force",
        action="store_true",
        help="alias for --full; rebuild the complete history",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = MarketDataPipeline().run(args.date, full=args.full or args.force)
    if result.skipped:
        print("No trading-day update required.")
    else:
        print(f"Published {len(result.updated_dates)} trading day(s): {', '.join(result.updated_dates)}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
