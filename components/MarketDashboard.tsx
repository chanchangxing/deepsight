"use client";

import { useEffect, useMemo, useState } from "react";
import { MarketCharts, formatSigned } from "./MarketCharts";
import {
  filterByRange,
  parseMarketHistory,
  parseMarketManifest,
  type MarketRange,
  type MarketRecord,
} from "@/lib/market-data";

const ranges: Array<{ value: MarketRange; label: string }> = [
  { value: "1M", label: "近 1 月" },
  { value: "3M", label: "近 3 月" },
  { value: "6M", label: "近 6 月" },
  { value: "1Y", label: "近 1 年" },
  { value: "ALL", label: "全部" },
];

const integerFormatter = new Intl.NumberFormat("zh-CN");

function formatDate(date: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date(`${date}T00:00:00Z`));
}

function metricTone(value: number) {
  return value > 0 ? "metric-up" : value < 0 ? "metric-down" : "";
}

export function MarketDashboard({
  records,
  csvHref,
}: {
  records: MarketRecord[];
  csvHref: string;
}) {
  const [range, setRange] = useState<MarketRange>("ALL");
  const filteredRecords = useMemo(
    () => filterByRange(records, range),
    [records, range],
  );
  const latest = records.at(-1);

  if (!latest) {
    return <DashboardError message="已发布的数据中暂时没有交易记录。" />;
  }

  return (
    <main>
      <header className="site-header">
        <a href="#main-chart" className="brand" aria-label="沪深市场广度首页">
          <span className="brand-mark" aria-hidden="true">D/N</span>
          <span>沪深市场广度</span>
        </a>
        <div className="header-meta">
          <span className="status-dot" aria-hidden="true" />
          数据截至 {formatDate(latest.date)}
        </div>
      </header>

      <section className="hero">
        <div className="hero-copy">
          <p className="eyebrow">A 股全市场温度计</p>
          <h1>穿透指数，观察市场真实广度</h1>
          <p>
            追踪沪深 A 股每日上涨家数与下跌家数之差，并将其持续累加，和上证指数放在同一时间线上观察。
          </p>
        </div>
        <a className="download-link" href={csvHref} download>
          下载 CSV <span aria-hidden="true">↓</span>
        </a>
      </section>

      <section className="metrics" aria-label="最新市场指标">
        <article className="metric-card primary-metric">
          <span>累计涨跌差值</span>
          <strong className={metricTone(latest.cumulative_diff)}>
            {formatSigned(latest.cumulative_diff)}
          </strong>
          <small>自数据起始日累计 N</small>
        </article>
        <article className="metric-card">
          <span>上证指数</span>
          <strong>{latest.shanghai_close.toFixed(2)}</strong>
          <small className={metricTone(latest.shanghai_change)}>
            {formatSigned(latest.shanghai_change, 2)} / {formatSigned(latest.shanghai_change_pct, 2)}%
          </small>
        </article>
        <article className="metric-card">
          <span>上涨家数</span>
          <strong className="metric-up">{integerFormatter.format(latest.up_count)}</strong>
          <small>纳入统计股票</small>
        </article>
        <article className="metric-card">
          <span>下跌家数</span>
          <strong className="metric-down">{integerFormatter.format(latest.down_count)}</strong>
          <small>纳入统计股票</small>
        </article>
        <article className="metric-card">
          <span>当日涨跌差值</span>
          <strong className={metricTone(latest.daily_diff)}>{formatSigned(latest.daily_diff)} D</strong>
          <small>当日 D</small>
        </article>
      </section>

      <section className="dashboard-frame" id="main-chart">
        <div className="range-toolbar">
          <div className="range-group" aria-label="图表时间范围">
            {ranges.map(({ value, label }) => (
              <button
                key={value}
                type="button"
                aria-pressed={range === value}
                onClick={() => setRange(value)}
              >
                {label}
              </button>
            ))}
          </div>
          <span>{filteredRecords.length} 个交易日</span>
        </div>
        <MarketCharts records={filteredRecords} />
      </section>

      <section className="methodology" aria-labelledby="method-title">
        <div>
          <p className="eyebrow">数据口径</p>
          <h2 id="method-title">如何理解 D 与 N</h2>
        </div>
        <div className="method-grid">
          <p><strong>D · 单日差值</strong>当日上涨股票数减去下跌股票数。正值代表上涨家数占优。</p>
          <p><strong>N · 累计差值</strong>从起始日开始连续累加每日 D，用来观察指数之外的市场参与广度。</p>
          <p><strong>统计范围</strong>沪深 A 股，使用不复权收盘价；停牌及缺少有效比较价格的股票不纳入当天统计。</p>
        </div>
      </section>

      <footer>
        <span>数据仅供市场观察，不构成投资建议。</span>
        <span>有效样本 {integerFormatter.format(latest.valid_count)} 只</span>
      </footer>
    </main>
  );
}

function DashboardError({ message }: { message: string }) {
  return (
    <main className="state-shell">
      <div className="state-card" role="alert">
        <p className="eyebrow">数据连接异常</p>
        <h1>暂时无法载入市场数据</h1>
        <p>{message}</p>
        <button type="button" onClick={() => window.location.reload()}>重新载入</button>
      </div>
    </main>
  );
}

export function MarketDashboardLoader() {
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; records: MarketRecord[]; csvHref: string }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        const manifestResponse = await fetch("/data/current.json", {
          cache: "no-store",
        });
        if (!manifestResponse.ok) throw new Error("数据清单请求失败");
        const manifest = parseMarketManifest(await manifestResponse.json());

        const historyResponse = await fetch(manifest.history_json, {
          cache: "no-store",
        });
        if (!historyResponse.ok) throw new Error("历史数据请求失败");
        const records = parseMarketHistory(await historyResponse.json());

        if (!cancelled) {
          setState({ status: "ready", records, csvHref: manifest.history_csv });
        }
      } catch (error) {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : "请稍后再试。",
          });
        }
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (state.status === "loading") {
    return (
      <main className="state-shell" aria-busy="true">
        <div className="loading-state" role="status">
          <span aria-hidden="true" />
          正在载入市场数据…
        </div>
      </main>
    );
  }

  if (state.status === "error") return <DashboardError message={state.message} />;
  return <MarketDashboard records={state.records} csvHref={state.csvHref} />;
}
