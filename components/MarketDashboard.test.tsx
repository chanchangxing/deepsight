import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarketDashboard, MarketDashboardLoader } from "./MarketDashboard";
import {
  filterByRange,
  parseMarketHistory,
  type MarketRecord,
} from "@/lib/market-data";

const records: MarketRecord[] = [
  {
    date: "2024-01-31",
    shanghai_close: 2788.55,
    shanghai_change: 12.3,
    shanghai_change_pct: 0.44,
    up_count: 2400,
    down_count: 2600,
    flat_count: 100,
    valid_count: 5100,
    daily_diff: -200,
    cumulative_diff: -200,
  },
  {
    date: "2024-02-15",
    shanghai_close: 2865.9,
    shanghai_change: 36.2,
    shanghai_change_pct: 1.28,
    up_count: 3200,
    down_count: 1800,
    flat_count: 100,
    valid_count: 5100,
    daily_diff: 1400,
    cumulative_diff: 1200,
  },
  {
    date: "2024-03-01",
    shanghai_close: 3027.02,
    shanghai_change: 11.85,
    shanghai_change_pct: 0.39,
    up_count: 2555,
    down_count: 2540,
    flat_count: 5,
    valid_count: 5100,
    daily_diff: 15,
    cumulative_diff: 15,
  },
];

function recordAt(date: string): MarketRecord {
  return { ...records[0], date };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("market data", () => {
  it("rejects a history row with a missing numeric field", () => {
    expect(() =>
      parseMarketHistory([{ ...records[0], valid_count: undefined }]),
    ).toThrow(/valid_count/);
  });

  it("sorts valid history chronologically", () => {
    expect(parseMarketHistory([records[2], records[0], records[1]])).toEqual(
      records,
    );
  });

  it("filters relative ranges from the latest trading date", () => {
    expect(filterByRange(records, "1M").map(({ date }) => date)).toEqual([
      "2024-02-15",
      "2024-03-01",
    ]);
    expect(filterByRange(records, "ALL")).toEqual(records);
  });

  it("clamps a one-month range to the target calendar month's last day", () => {
    const monthEndRecords = [
      recordAt("2025-02-27"),
      recordAt("2025-02-28"),
      recordAt("2025-03-03"),
      recordAt("2025-03-31"),
    ];

    expect(
      filterByRange(monthEndRecords, "1M").map(({ date }) => date),
    ).toEqual(["2025-02-28", "2025-03-03", "2025-03-31"]);
  });
});

describe("MarketDashboard", () => {
  it("shows the latest cumulative breadth and switches range", async () => {
    const user = userEvent.setup();
    render(
      <MarketDashboard
        records={records}
        csvHref="/data/versions/test/market_history.csv"
      />,
    );

    expect(screen.getAllByText("累计涨跌差值").length).toBeGreaterThan(0);
    expect(screen.getByText("+15")).toBeInTheDocument();
    expect(screen.getByText(/数据截至 2024年3月1日/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /下载 CSV/ })).toHaveAttribute(
      "href",
      "/data/versions/test/market_history.csv",
    );

    await user.click(screen.getByRole("button", { name: "近 1 月" }));
    expect(screen.getByRole("button", { name: "近 1 月" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText("2 个交易日")).toBeInTheDocument();
  });

  it("loads the versioned history and CSV paths from the current manifest", async () => {
    const fetchCalls: string[] = [];
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      const url = String(input);
      fetchCalls.push(url);

      if (url === "/data/current.json") {
        return new Response(
          JSON.stringify({
            version: "test",
            history_json: "versions/test/market_history.json",
            history_csv: "versions/test/market_history.csv",
          }),
        );
      }

      return new Response(JSON.stringify(records));
    });

    render(<MarketDashboardLoader />);

    expect((await screen.findAllByText("累计涨跌差值")).length).toBeGreaterThan(0);
    expect(fetchCalls).toEqual([
      "/data/current.json",
      "/data/versions/test/market_history.json",
    ]);
    expect(screen.getByRole("link", { name: /下载 CSV/ })).toHaveAttribute(
      "href",
      "/data/versions/test/market_history.csv",
    );
  });

  it("shows a waiting state when published history loads successfully but is empty", async () => {
    vi.stubGlobal("fetch", async (input: RequestInfo | URL) => {
      if (String(input) === "/data/current.json") {
        return new Response(
          JSON.stringify({
            version: "bootstrap",
            history_json: "versions/bootstrap/market_history.json",
            history_csv: "versions/bootstrap/market_history.csv",
          }),
        );
      }

      return new Response("[]");
    });

    render(<MarketDashboardLoader />);

    expect(
      await screen.findByRole("heading", { name: "暂无可展示数据" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/等待首次市场数据发布/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("数据连接异常")).not.toBeInTheDocument();
  });

  it("shows a useful state when published data cannot be loaded", async () => {
    vi.stubGlobal(
      "fetch",
      async () => new Response("unavailable", { status: 503 }),
    );

    render(<MarketDashboardLoader />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "暂时无法载入市场数据",
    );
  });
});
