# 沪深 A 股累计涨跌差值网站实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个从 2025 年首个交易日起展示上证指数、每日沪深 A 股涨跌差值及其累计值，并在收盘后自动更新的公开网站。

**Architecture:** Python 数据管道通过 AKShare 拉取指数与股票行情，将验证后的日级聚合数据写入 CSV，并原子生成前端 JSON。React 静态单页只读取发布数据并绘制双轴趋势图和单日柱状图；GitHub Actions 负责每日双时点采集、提交数据和触发静态部署。

**Tech Stack:** Python 3.11、AKShare、pandas、pytest、React、TypeScript、Vite/vinext、Recharts、Vitest、Testing Library、GitHub Actions、Sites 静态托管。

## Global Constraints

- 数据起始边界为 2025-01-01，实际首条为之后首个沪深交易日。
- 股票范围仅含沪深 A 股，包含 ST 与 `*ST`，排除北交所、B 股、基金、债券、可转债和指数。
- 使用不复权日线；无前一有效收盘价、停牌或无有效价格者不计入当日统计。
- `daily_diff = up_count - down_count`，`cumulative_diff` 从零开始按日期累计所有 `daily_diff`。
- 工作日北京时间 15:35 与 17:05 调度；临时错误按 30、90、180 秒重试。
- 任一关键校验失败时不替换正式数据、不提交变更、不发布半成品。
- 网站采用浅色、专业行情风格，A 股红涨绿跌，支持桌面与移动端。
- 不实现账户、盘中实时刷新、选股和交易建议。

---

## 文件结构

```text
app/
  layout.tsx                 站点元数据和全局布局
  page.tsx                   页面入口和数据加载状态
  globals.css                响应式视觉样式
components/
  MarketDashboard.tsx        区间筛选、指标卡和图表组合
  MarketCharts.tsx           双轴折线图及单日差值柱状图
lib/
  market-data.ts             前端数据类型、解析和区间筛选
scripts/
  market_data/
    models.py                Python 日记录数据模型
    calculations.py          涨跌分类、累计和指数变化计算
    akshare_client.py         AKShare 请求、重试和接口适配
    pipeline.py               回溯、补缺、校验与原子发布
  update_market_data.py      命令行入口
data/market_history.csv      主数据
public/data/market_history.json  前端发布数据
tests/data/                  Python 数据层测试
components/*.test.tsx        前端交互测试
.github/workflows/update-data.yml  收盘后自动更新
requirements.txt             Python 依赖
```

### Task 1: 数据模型与纯计算函数

**Files:**
- Create: `scripts/market_data/__init__.py`
- Create: `scripts/market_data/models.py`
- Create: `scripts/market_data/calculations.py`
- Create: `tests/data/test_calculations.py`
- Create: `requirements.txt`

**Interfaces:**
- Consumes: pandas DataFrame，至少含 `date`、`close` 或股票 `code`、`current_close`、`previous_close`。
- Produces: `classify_breadth(frame) -> BreadthCounts`、`add_cumulative_diff(frame) -> DataFrame`、`calculate_index_changes(frame) -> DataFrame` 和 `MarketRecord`。

- [ ] **Step 1: 写涨跌分类和累计计算的失败测试**

```python
def test_classify_breadth_excludes_missing_previous_close():
    frame = pd.DataFrame({
        "current_close": [11.0, 9.0, 10.0, 8.0],
        "previous_close": [10.0, 10.0, 10.0, None],
    })
    assert classify_breadth(frame) == BreadthCounts(up=1, down=1, flat=1, valid=3)

def test_add_cumulative_diff_rebuilds_from_zero():
    frame = pd.DataFrame({"date": ["2025-01-02", "2025-01-03", "2025-01-06"], "daily_diff": [10, 10, -5]})
    assert add_cumulative_diff(frame)["cumulative_diff"].tolist() == [10, 20, 15]
```

- [ ] **Step 2: 运行测试并确认因模块不存在而失败**

Run: `python -m pytest tests/data/test_calculations.py -v`
Expected: FAIL，提示无法导入 `scripts.market_data.calculations`。

- [ ] **Step 3: 实现最小数据模型和纯函数**

```python
@dataclass(frozen=True)
class BreadthCounts:
    up: int
    down: int
    flat: int
    valid: int

def add_cumulative_diff(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.sort_values("date").reset_index(drop=True).copy()
    result["cumulative_diff"] = result["daily_diff"].astype(int).cumsum()
    return result
```

实现严格数值转换、缺失前收盘价排除，以及根据相邻指数收盘价计算 `shanghai_change`、`shanghai_change_pct`。

- [ ] **Step 4: 运行数据计算测试**

Run: `python -m pytest tests/data/test_calculations.py -v`
Expected: PASS。

- [ ] **Step 5: 提交独立变更**

```bash
git add requirements.txt scripts/market_data tests/data/test_calculations.py
git commit -m "feat: add market breadth calculations"
```

### Task 2: AKShare 客户端与失败重试

**Files:**
- Create: `scripts/market_data/akshare_client.py`
- Create: `tests/data/test_akshare_client.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: AKShare 的 `stock_zh_index_daily_em`、`stock_sh_a_spot_em`、`stock_sz_a_spot_em` 和个股历史日线接口。
- Produces: `AkshareClient.get_index_history(start, end)`、`get_current_market()`、`get_stock_history(symbol, start, end)`；所有返回值使用规范化英文列名。

- [ ] **Step 1: 写临时错误重试和结构规范化测试**

```python
def test_retry_uses_three_delays(monkeypatch):
    outcomes = iter([TimeoutError(), TimeoutError(), TimeoutError(), SAMPLE_FRAME])
    delays = []
    client = AkshareClient(api=object(), sleeper=delays.append)

    def operation():
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    result = client._with_retry(operation)
    assert result is SAMPLE_FRAME
    assert delays == [30, 90, 180]

def test_current_market_filters_to_shanghai_and_shenzhen_a_shares():
    result = client.get_current_market()
    assert set(result["exchange"]) == {"SH", "SZ"}
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/data/test_akshare_client.py -v`
Expected: FAIL，提示 `AkshareClient` 未定义。

- [ ] **Step 3: 实现接口适配与可测试重试器**

```python
class AkshareClient:
    def __init__(self, api=ak, sleeper=time.sleep, delays=(30, 90, 180)):
        self.api = api
        self.sleeper = sleeper
        self.delays = delays

    def _with_retry(self, operation):
        try:
            return operation()
        except TRANSIENT_ERRORS as first_error:
            error = first_error
            for delay in self.delays:
                self.sleeper(delay)
                try:
                    return operation()
                except TRANSIENT_ERRORS as next_error:
                    error = next_error
            raise MarketDataUnavailable(str(error)) from error
```

只对连接、超时和限流类异常重试；缺列、类型错误等结构变化直接抛出 `MarketDataSchemaError`。

- [ ] **Step 4: 运行客户端测试**

Run: `python -m pytest tests/data/test_akshare_client.py -v`
Expected: PASS，且测试中不访问公网。

- [ ] **Step 5: 提交独立变更**

```bash
git add requirements.txt scripts/market_data/akshare_client.py tests/data/test_akshare_client.py
git commit -m "feat: add resilient AKShare client"
```

### Task 3: 数据管道、补缺与原子发布

**Files:**
- Create: `scripts/market_data/pipeline.py`
- Create: `scripts/update_market_data.py`
- Create: `tests/data/test_pipeline.py`
- Create: `data/market_history.csv`
- Create: `public/data/market_history.json`

**Interfaces:**
- Consumes: Task 1 计算函数、Task 2 `AkshareClient`、现有 CSV。
- Produces: `MarketDataPipeline.run(as_of: date, full: bool = False) -> UpdateResult`；CLI 参数 `--date YYYY-MM-DD`、`--full`、`--force`。

- [ ] **Step 1: 写幂等、补缺、校验失败不发布测试**

```python
def test_rerun_replaces_date_and_rebuilds_cumulative(tmp_path):
    pipeline.run(date(2025, 1, 6))
    pipeline.run(date(2025, 1, 6))
    frame = pd.read_csv(tmp_path / "data/market_history.csv")
    assert frame["date"].is_unique
    assert frame["cumulative_diff"].tolist() == frame["daily_diff"].cumsum().tolist()

def test_validation_failure_preserves_published_files(tmp_path):
    before = published_json.read_bytes()
    with pytest.raises(MarketDataValidationError):
        broken_pipeline.run(date(2025, 1, 7))
    assert published_json.read_bytes() == before
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m pytest tests/data/test_pipeline.py -v`
Expected: FAIL，提示 `MarketDataPipeline` 未定义。

- [ ] **Step 3: 实现流水线与命令行入口**

```python
REQUIRED_COLUMNS = [
    "date", "shanghai_close", "shanghai_change", "shanghai_change_pct",
    "up_count", "down_count", "flat_count", "valid_count",
    "daily_diff", "cumulative_diff",
]

def validate_history(frame: pd.DataFrame) -> None:
    if frame["date"].duplicated().any():
        raise MarketDataValidationError("duplicate dates")
    if not (frame["daily_diff"] == frame["up_count"] - frame["down_count"]).all():
        raise MarketDataValidationError("daily diff mismatch")
    if not (frame["cumulative_diff"] == frame["daily_diff"].cumsum()).all():
        raise MarketDataValidationError("cumulative diff mismatch")
```

用指数交易日序列发现缺口；日常模式重算最近 5 个交易日，全量模式从 2025-01-01 回溯。先写同目录临时文件，`fsync` 后用 `os.replace` 替换正式 CSV 与 JSON。

- [ ] **Step 4: 运行数据管道测试和一次离线夹具演练**

Run: `python -m pytest tests/data -v`
Expected: PASS；测试只使用固定夹具，不访问公网。

- [ ] **Step 5: 提交独立变更**

```bash
git add scripts/update_market_data.py scripts/market_data/pipeline.py tests/data/test_pipeline.py data/market_history.csv public/data/market_history.json
git commit -m "feat: add atomic market data pipeline"
```

### Task 4: 静态数据看板

**Files:**
- Create or Modify: `app/layout.tsx`
- Create or Modify: `app/page.tsx`
- Create or Modify: `app/globals.css`
- Create: `components/MarketDashboard.tsx`
- Create: `components/MarketCharts.tsx`
- Create: `lib/market-data.ts`
- Create: `components/MarketDashboard.test.tsx`
- Modify: `package.json`

**Interfaces:**
- Consumes: `/data/market_history.json`，字段与 Task 3 的 `REQUIRED_COLUMNS` 完全一致。
- Produces: `parseMarketHistory(input: unknown): MarketRecord[]`、`filterByRange(records, range)`、`MarketDashboard({ records })`。

- [ ] **Step 1: 写数据解析、累计值展示与范围切换测试**

```tsx
it("shows the latest cumulative breadth and switches range", async () => {
  render(<MarketDashboard records={records} />)
  expect(screen.getByText("累计涨跌差值")).toBeInTheDocument()
  expect(screen.getByText("+15")).toBeInTheDocument()
  await userEvent.click(screen.getByRole("button", { name: "近 1 月" }))
  expect(screen.getByRole("button", { name: "近 1 月" })).toHaveAttribute("aria-pressed", "true")
})
```

- [ ] **Step 2: 运行前端测试并确认失败**

Run: `npm test -- --run components/MarketDashboard.test.tsx`
Expected: FAIL，提示组件不存在。

- [ ] **Step 3: 实现页面、图表和响应式样式**

```ts
export type MarketRecord = {
  date: string
  shanghai_close: number
  shanghai_change: number
  shanghai_change_pct: number
  up_count: number
  down_count: number
  flat_count: number
  valid_count: number
  daily_diff: number
  cumulative_diff: number
}
```

主图使用同一 X 轴、左右两个 Y 轴；上证指数与累计值使用不同颜色和线型。辅助柱状图按 `daily_diff >= 0` 使用红色，否则绿色。提供 1 月、3 月、6 月、1 年、全部区间、触摸提示、数据截至时间、加载失败状态和 CSV 下载。

- [ ] **Step 4: 运行前端测试和生产构建**

Run: `npm test -- --run && npm run build`
Expected: 全部 PASS，构建成功。

- [ ] **Step 5: 提交独立变更**

```bash
git add app components lib package.json package-lock.json
git commit -m "feat: build market breadth dashboard"
```

### Task 5: 自动更新工作流

**Files:**
- Create: `.github/workflows/update-data.yml`
- Create: `tests/data/test_workflow_config.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `python scripts/update_market_data.py` 和仓库内 CSV/JSON。
- Produces: UTC `35 7 * * 1-5` 与 `5 9 * * 1-5` 调度、`workflow_dispatch` 全量选项、仅数据变化时提交。

- [ ] **Step 1: 写工作流配置测试**

```python
def test_workflow_has_two_schedules_and_manual_full_mode():
    workflow = yaml.safe_load(Path(".github/workflows/update-data.yml").read_text())
    schedules = [item["cron"] for item in workflow["on"]["schedule"]]
    assert schedules == ["35 7 * * 1-5", "5 9 * * 1-5"]
    assert "workflow_dispatch" in workflow["on"]
```

- [ ] **Step 2: 运行配置测试并确认失败**

Run: `python -m pytest tests/data/test_workflow_config.py -v`
Expected: FAIL，提示工作流文件不存在。

- [ ] **Step 3: 实现最小权限、并发保护和幂等提交**

```yaml
on:
  workflow_dispatch:
    inputs:
      full:
        type: boolean
        default: false
  schedule:
    - cron: "35 7 * * 1-5"
    - cron: "5 9 * * 1-5"
permissions:
  contents: write
concurrency:
  group: market-data-update
  cancel-in-progress: false
```

工作流固定 Python 3.11，安装 `requirements.txt`，根据输入运行日常或全量命令，执行数据测试，仅在 CSV/JSON 有差异时提交。README 写明统计口径、自动更新、手动补跑和失败排查入口。

- [ ] **Step 4: 运行全部自动化测试**

Run: `python -m pytest tests/data -v && npm test -- --run && npm run build`
Expected: 全部 PASS。

- [ ] **Step 5: 提交独立变更**

```bash
git add .github/workflows/update-data.yml tests/data/test_workflow_config.py README.md
git commit -m "ci: automate post-close market updates"
```

### Task 6: 真实数据初始化、最终验证与公开发布

**Files:**
- Modify: `data/market_history.csv`
- Modify: `public/data/market_history.json`
- Modify: `app/layout.tsx`
- Create: `public/og.png`（仅在生成图通过文字和视觉检查时）

**Interfaces:**
- Consumes: 2025-01-01 至当前日期的 AKShare 行情、完整应用构建。
- Produces: 经过校验的历史数据、公开 URL、可运行的定时任务。

- [ ] **Step 1: 运行全量回溯并保存检查点**

Run: `python scripts/update_market_data.py --full`
Expected: 从 2025 年首个交易日开始生成连续记录；网络中断后可从最后检查点恢复。

- [ ] **Step 2: 校验历史数据不变量**

Run: `python -m pytest tests/data -v`
Expected: 日期唯一递增，全部恒等式成立，无未解释的交易日缺口。

- [ ] **Step 3: 生成并检查站点社交分享图**

基于最终页面标题、红绿行情配色、上证指数线与累计广度线生成一张横向分享图；仅在文字准确且无虚构数值时保存为 `public/og.png` 并配置 Open Graph/X 元数据，否则不声明 `og:image`。

- [ ] **Step 4: 执行最终验证**

Run: `python -m pytest tests/data -v && npm test -- --run && npm run build && git diff --check`
Expected: 所有命令成功，工作区仅包含预期历史数据和分享图变化。

- [ ] **Step 5: 提交初始化数据并发布**

```bash
git add data/market_history.csv public/data/market_history.json app/layout.tsx
# 如果分享图通过检查，再同时添加 public/og.png
git commit -m "data: initialize market breadth history"
```

使用 Sites 发布构建产物，确认公开 URL 可以加载最新 JSON。随后将仓库推送到 GitHub，启用 Actions 写权限和失败通知；手动运行一次非全量工作流，确认无数据变化时不会产生空提交。
