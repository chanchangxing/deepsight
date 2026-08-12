export type MarketRecord = {
  date: string;
  shanghai_close: number;
  shanghai_change: number;
  shanghai_change_pct: number;
  up_count: number;
  down_count: number;
  flat_count: number;
  valid_count: number;
  daily_diff: number;
  cumulative_diff: number;
};

export type MarketRange = "1M" | "3M" | "6M" | "1Y" | "ALL";

export type MarketManifest = {
  version: string;
  history_json: string;
  history_csv: string;
  generated_at: string | null;
};

const RFC_3339_INSTANT =
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

const NUMERIC_FIELDS: (keyof Omit<MarketRecord, "date">)[] = [
  "shanghai_close",
  "shanghai_change",
  "shanghai_change_pct",
  "up_count",
  "down_count",
  "flat_count",
  "valid_count",
  "daily_diff",
  "cumulative_diff",
];

function asDataPath(value: string): string {
  return value.startsWith("/") ? value : `/data/${value}`;
}

export function parseMarketManifest(input: unknown): MarketManifest {
  if (!input || typeof input !== "object") {
    throw new Error("数据清单格式无效");
  }

  const manifest = input as Record<string, unknown>;
  const historyJson = manifest.history_json ?? manifest.json;
  const historyCsv = manifest.history_csv ?? manifest.csv;
  const generatedAt = manifest.generated_at ?? manifest.updated_at;

  if (
    manifest.generated_at !== undefined &&
    manifest.updated_at !== undefined &&
    (typeof manifest.generated_at !== "string" ||
      typeof manifest.updated_at !== "string" ||
      Date.parse(manifest.generated_at) !== Date.parse(manifest.updated_at))
  ) {
    throw new Error("数据清单的 generated_at 与 updated_at 时间字段冲突");
  }

  if (
    typeof manifest.version !== "string" ||
    typeof historyJson !== "string" ||
    typeof historyCsv !== "string"
  ) {
    throw new Error("数据清单缺少版本或文件路径");
  }

  if (manifest.version !== "bootstrap") {
    if (
      typeof generatedAt !== "string" ||
      !RFC_3339_INSTANT.test(generatedAt) ||
      !Number.isFinite(Date.parse(generatedAt))
    ) {
      throw new Error("数据清单缺少有效的 generated_at 时间");
    }
  } else if (
    generatedAt !== undefined &&
    (typeof generatedAt !== "string" ||
      !RFC_3339_INSTANT.test(generatedAt) ||
      !Number.isFinite(Date.parse(generatedAt)))
  ) {
    throw new Error("数据清单的 generated_at 时间无效");
  }

  return {
    version: manifest.version,
    history_json: asDataPath(historyJson),
    history_csv: asDataPath(historyCsv),
    generated_at: typeof generatedAt === "string" ? generatedAt : null,
  };
}

export function parseMarketHistory(input: unknown): MarketRecord[] {
  if (!Array.isArray(input)) {
    throw new Error("市场历史数据必须为数组");
  }

  const records = input.map((item, index) => {
    if (!item || typeof item !== "object") {
      throw new Error(`第 ${index + 1} 条市场记录格式无效`);
    }

    const raw = item as Record<string, unknown>;
    if (
      typeof raw.date !== "string" ||
      !/^\d{4}-\d{2}-\d{2}$/.test(raw.date) ||
      Number.isNaN(Date.parse(`${raw.date}T00:00:00Z`))
    ) {
      throw new Error(`第 ${index + 1} 条记录的 date 无效`);
    }

    for (const field of NUMERIC_FIELDS) {
      if (typeof raw[field] !== "number" || !Number.isFinite(raw[field])) {
        throw new Error(`第 ${index + 1} 条记录的 ${field} 无效`);
      }
    }

    return raw as MarketRecord;
  });

  return records.sort((left, right) => left.date.localeCompare(right.date));
}

const RANGE_MONTHS: Record<Exclude<MarketRange, "ALL">, number> = {
  "1M": 1,
  "3M": 3,
  "6M": 6,
  "1Y": 12,
};

function subtractCalendarMonths(date: Date, months: number): Date {
  const originalDay = date.getUTCDate();
  const target = new Date(date);

  target.setUTCDate(1);
  target.setUTCMonth(target.getUTCMonth() - months);
  const finalDay = new Date(
    Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0),
  ).getUTCDate();
  target.setUTCDate(Math.min(originalDay, finalDay));

  return target;
}

export function filterByRange(
  records: MarketRecord[],
  range: MarketRange,
): MarketRecord[] {
  if (range === "ALL" || records.length === 0) return records;

  const latest = new Date(`${records.at(-1)!.date}T00:00:00Z`);
  const boundary = subtractCalendarMonths(latest, RANGE_MONTHS[range]);

  return records.filter(
    ({ date }) => new Date(`${date}T00:00:00Z`) >= boundary,
  );
}
