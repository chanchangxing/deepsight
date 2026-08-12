"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MarketRecord } from "@/lib/market-data";

const RED = "#c83b35";
const GREEN = "#16825d";
const NAVY = "#20364f";

const numberFormatter = new Intl.NumberFormat("zh-CN");
const compactFormatter = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

function shortDate(value: string) {
  return value.slice(5).replace("-", "/");
}

function TooltipContent({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: MarketRecord }>;
}) {
  const record = payload?.[0]?.payload;
  if (!active || !record) return null;

  return (
    <div className="chart-tooltip">
      <strong>{record.date}</strong>
      <span>上证指数 {record.shanghai_close.toFixed(2)}</span>
      <span>
        指数涨跌 {formatSigned(record.shanghai_change, 2)} /{" "}
        {formatSigned(record.shanghai_change_pct, 2)}%
      </span>
      <span>累计差值 {formatSigned(record.cumulative_diff)}</span>
      <span>当日差值 {formatSigned(record.daily_diff)}</span>
      <span>
        上涨 {numberFormatter.format(record.up_count)} · 下跌{" "}
        {numberFormatter.format(record.down_count)} · 平盘{" "}
        {numberFormatter.format(record.flat_count)}
      </span>
      <span>有效样本 {numberFormatter.format(record.valid_count)}</span>
    </div>
  );
}

export function formatSigned(value: number, digits = 0) {
  const formatted = Math.abs(value).toLocaleString("zh-CN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  return `${value > 0 ? "+" : value < 0 ? "−" : ""}${formatted}`;
}

export function MarketCharts({ records }: { records: MarketRecord[] }) {
  return (
    <div className="charts-stack">
      <section className="chart-panel" aria-labelledby="trend-chart-title">
        <div className="chart-heading">
          <div>
            <p className="eyebrow">核心趋势</p>
            <h2 id="trend-chart-title">指数与市场广度</h2>
          </div>
          <p>左右双轴 · 同一交易日时间线</p>
        </div>
        <div className="main-chart" role="img" aria-label="上证指数与累计涨跌差值双轴趋势图">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 900, height: 430 }}>
            <ComposedChart data={records} margin={{ top: 12, right: 2, left: 2, bottom: 2 }}>
              <CartesianGrid stroke="#dfe4e7" strokeDasharray="3 5" vertical={false} />
              <XAxis dataKey="date" tickFormatter={shortDate} tickLine={false} axisLine={false} minTickGap={32} />
              <YAxis yAxisId="index" tickFormatter={(value) => compactFormatter.format(value)} tickLine={false} axisLine={false} width={48} />
              <YAxis yAxisId="breadth" orientation="right" tickFormatter={(value) => compactFormatter.format(value)} tickLine={false} axisLine={false} width={48} />
              <Tooltip content={<TooltipContent />} />
              <Legend verticalAlign="top" align="right" iconType="plainline" wrapperStyle={{ paddingBottom: 20 }} />
              <Line yAxisId="index" type="monotone" dataKey="shanghai_close" name="上证指数" stroke={NAVY} strokeWidth={2.5} dot={false} activeDot={{ r: 4 }} />
              <Line yAxisId="breadth" type="monotone" dataKey="cumulative_diff" name="累计涨跌差值" stroke={RED} strokeWidth={2.5} strokeDasharray="8 5" dot={false} activeDot={{ r: 4 }} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="chart-panel secondary" aria-labelledby="daily-chart-title">
        <div className="chart-heading">
          <div>
            <p className="eyebrow">每日广度</p>
            <h2 id="daily-chart-title">单日涨跌差值</h2>
          </div>
          <p>上涨家数减去下跌家数</p>
        </div>
        <div className="daily-chart" role="img" aria-label="单日涨跌差值柱状图，红色代表正值，绿色代表负值">
          <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 430, height: 430 }}>
            <BarChart data={records} margin={{ top: 8, right: 2, left: 2, bottom: 0 }}>
              <CartesianGrid stroke="#e3e7e9" strokeDasharray="3 5" vertical={false} />
              <XAxis dataKey="date" tickFormatter={shortDate} tickLine={false} axisLine={false} minTickGap={32} />
              <YAxis tickFormatter={(value) => compactFormatter.format(value)} tickLine={false} axisLine={false} width={48} />
              <Tooltip content={<TooltipContent />} cursor={{ fill: "rgba(31, 49, 66, 0.05)" }} />
              <Bar dataKey="daily_diff" radius={[2, 2, 0, 0]} maxBarSize={18}>
                {records.map((record) => (
                  <Cell key={record.date} fill={record.daily_diff >= 0 ? RED : GREEN} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>
    </div>
  );
}
