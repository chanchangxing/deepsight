import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MarketDashboard, MarketDashboardLoader } from "./MarketDashboard";
import {
  filterByRange,
  parseMarketHistory,
  parseMarketManifest,
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
  it("parses generated_at and the updated_at migration alias", () => {
    expect(
      parseMarketManifest({
        version: "20250808T091000Z",
        history_json: "versions/20250808T091000Z/market_history.json",
        history_csv: "versions/20250808T091000Z/market_history.csv",
        generated_at: "2025-08-08T09:10:00Z",
      }),
    ).toEqual({
      version: "20250808T091000Z",
      history_json: "/data/versions/20250808T091000Z/market_history.json",
      history_csv: "/data/versions/20250808T091000Z/market_history.csv",
      generated_at: "2025-08-08T09:10:00Z",
    });

    expect(
      parseMarketManifest({
        version: "20250808T091000Z",
        history_json: "versions/20250808T091000Z/market_history.json",
        history_csv: "versions/20250808T091000Z/market_history.csv",
        updated_at: "2025-08-08T17:10:00+08:00",
      }).generated_at,
    ).toBe("2025-08-08T17:10:00+08:00");
  });

  it("rejects a published manifest without a valid RFC 3339 timestamp", () => {
    const manifest = {
      version: "20250808T091000Z",
      history_json: "versions/20250808T091000Z/market_history.json",
      history_csv: "versions/20250808T091000Z/market_history.csv",
    };

    expect(() => parseMarketManifest(manifest)).toThrow(/generated_at/);
    expect(() =>
      parseMarketManifest({ ...manifest, generated_at: "2025-08-08" }),
    ).toThrow(/generated_at/);
    expect(
      parseMarketManifest({
        ...manifest,
        generated_at: "2025-08-08T09:10:00Z",
        updated_at: "2025-08-08T17:10:00+08:00",
      }).generated_at,
    ).toBe("2025-08-08T09:10:00Z");
    expect(() =>
      parseMarketManifest({
        ...manifest,
        generated_at: "2025-08-08T09:10:00Z",
        updated_at: "2025-08-08T17:11:00+08:00",
      }),
    ).toThrow(/时间字段冲突/);
  });

  it("keeps the timestamp-less bootstrap manifest readable", () => {
    expect(
      parseMarketManifest({
        version: "bootstrap",
        json: "versions/bootstrap/market_history.json",
        csv: "versions/bootstrap/market_history.csv",
      }),
    ).toEqual({
      version: "bootstrap",
      history_json: "/data/versions/bootstrap/market_history.json",
      history_csv: "/data/versions/bootstrap/market_history.csv",
      generated_at: null,
    });
  });

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
        generatedAt="2024-03-01T09:10:00Z"
        now={new Date("2024-03-01T10:00:00Z")}
      />,
    );

    expect(screen.getAllByText("累计涨跌差值").length).toBeGreaterThan(0);
    expect(screen.getByText("+15")).toBeInTheDocument();
    expect(screen.getByText(/数据截至 2024年3月1日/)).toBeInTheDocument();
    expect(screen.getByText(/最近成功更新：2024年3月1日 17:10/)).toBeInTheDocument();
    expect(screen.getByText("数据更新正常")).toBeInTheDocument();
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

  it("warns clearly when the last successful publication missed a Beijing close deadline", () => {
    render(
      <MarketDashboard
        records={records}
        csvHref="/data/versions/test/market_history.csv"
        generatedAt="2024-03-01T09:10:00Z"
        now={new Date("2024-03-04T10:00:00Z")}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("数据可能已过期");
    expect(screen.getByRole("status")).toHaveTextContent(
      "最近成功更新：2024年3月1日 17:10",
    );
  });

  it("does not mark Friday's publication stale before Monday's Beijing deadline", () => {
    render(
      <MarketDashboard
        records={records}
        csvHref="/data/versions/test/market_history.csv"
        generatedAt="2024-03-01T09:10:00Z"
        now={new Date("2024-03-04T09:59:00Z")}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("数据更新正常");
    expect(screen.queryByText("数据可能已过期")).not.toBeInTheDocument();
  });

  it("expires a pre-close generation at the same Beijing weekday deadline", () => {
    render(
      <MarketDashboard
        records={[...records, recordAt("2024-03-04")]}
        csvHref="/data/versions/test/market_history.csv"
        generatedAt="2024-03-04T06:00:00Z"
        now={new Date("2024-03-04T10:00:00Z")}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("数据可能已过期");
  });

  it("warns when a recent manifest points to history behind the latest weekday deadline", () => {
    render(
      <MarketDashboard
        records={records}
        csvHref="/data/versions/test/market_history.csv"
        generatedAt="2024-03-04T10:10:00Z"
        now={new Date("2024-03-04T10:20:00Z")}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("数据可能已过期");
  });

  it("keeps Friday history fresh throughout the weekend", () => {
    render(
      <MarketDashboard
        records={records}
        csvHref="/data/versions/test/market_history.csv"
        generatedAt="2024-03-01T10:10:00Z"
        now={new Date("2024-03-03T12:00:00Z")}
      />,
    );

    expect(screen.getByRole("status")).toHaveTextContent("数据更新正常");
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
            generated_at: "2024-03-01T09:10:00Z",
          }),
        );
      }

      return new Response(JSON.stringify(records));
    });

    render(<MarketDashboardLoader now={new Date("2024-03-01T10:00:00Z")} />);

    expect((await screen.findAllByText("累计涨跌差值")).length).toBeGreaterThan(0);
    expect(fetchCalls).toEqual([
      "/data/current.json",
      "/data/versions/test/market_history.json",
    ]);
    expect(screen.getByRole("link", { name: /下载 CSV/ })).toHaveAttribute(
      "href",
      "/data/versions/test/market_history.csv",
    );
    expect(screen.getByRole("status")).toHaveTextContent("数据更新正常");
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
