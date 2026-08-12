# Task 4 静态数据看板实施报告

## 状态

已完成静态市场广度看板。页面使用 `/data/current.json` 作为唯一入口，优先解析规范字段 `history_json` 与 `history_csv`，再请求版本化历史 JSON；CSV 下载链接来自同一清单。为兼容当前尚未迁移的发布端，解析器临时接受旧字段 `json` 与 `csv`。

交付内容包括：

- 严格的市场历史记录和发布清单解析、按最新交易日计算的 1 月/3 月/6 月/1 年/全部区间筛选。
- 最新累计涨跌差值、上证指数、上涨/下跌家数、当日差值和数据截至日期。
- 同一时间轴上的上证指数/累计差值双 Y 轴折线图，以及按 A 股习惯红涨绿跌的每日差值柱状图。
- 鼠标与触摸提示框，提示完整当日字段；清单驱动的 CSV 下载；加载中、空数据和加载失败状态。
- 专业克制的响应式单页版式、键盘可操作范围按钮、可访问图表名称、减少动态效果偏好支持。
- 已移除 starter skeleton、`react-loading-skeleton`、starter 元数据与旧 skeleton 测试，并更新站点名称、标题和描述。

## TDD 证据

### RED

先创建 `components/MarketDashboard.test.tsx`，覆盖无效记录拒绝、日期排序、相对范围筛选、最新累计值、范围切换、清单两阶段加载、CSV 链接和加载失败状态。

运行：

```text
npm test -- --run components/MarketDashboard.test.tsx
```

预期失败：Vitest 成功启动，因 `./MarketDashboard` 不存在而失败：

```text
Failed to resolve import "./MarketDashboard"
Test Files 1 failed (1)
```

### GREEN

实现解析器、看板与图表后重新运行同一目标测试；最终结果：

```text
Test Files 1 passed (1)
Tests 6 passed (6)
```

测试夹具通过真实 `ResizeObserver` 回调尺寸驱动 Recharts，最终输出无零尺寸告警。

## 最终验证

使用 Node.js 22.22.1 运行：

```text
npm test -- --run
  Test Files 1 passed (1)
  Tests 6 passed (6)

npm run lint
  exit 0，ESLint 无错误或警告

npm run build
  vinext 五阶段构建完成，exit 0

git diff --check
  无空白错误
```

另检查 `app/_sites-preview` 已移除，且 app/依赖与锁文件中无 `codex-preview`、`react-loading-skeleton`、starter 标题或说明残留。

`npx tsc --noEmit` 仍会报告仓库既有 Cloudflare Worker 全局类型缺失（`cloudflare:workers`、`Fetcher`、`D1Database`）；这些错误位于未修改的 `db/index.ts` 与 `worker/index.ts`。本任务修改的 TypeScript 已由 vinext 生产构建和 ESLint 验证。

## 提交

提交主题：`feat: build market breadth dashboard`。本报告与实现随同该提交；精确提交 SHA 以 Git 历史为准。

## 自审

- 需求覆盖：核心指标、双轴图、红涨绿跌柱状图、五档范围、完整提示框、截至日期、错误状态、CSV 下载均已实现。
- 接口边界：只消费静态清单和清单指向的数据文件，不在浏览器访问行情服务。
- 视觉与无障碍：首屏聚焦指标与图形，无通用后台导航；移动端转为单列；控件有 pressed 状态和焦点样式；图表有文本名称；未自绘 SVG。
- 变更边界：未修改数据管道或其测试；保留现有 favicon，未引入与任务无关的功能。

## Concerns

1. 当前数据管道和 bootstrap 清单仍发布旧键 `json`/`csv`，而规范接口要求 `history_json`/`history_csv`。前端已以新键为主并兼容旧键，避免整合窗口内页面不可用；后续整合必须统一发布端及 Python 测试，届时可删除旧键兼容。
2. bootstrap 历史 JSON 当前为空，因此直接使用仓库 bootstrap 数据时页面会显示明确的空数据状态；实际行情发布后才会呈现图表。
3. vinext 构建会输出其已知的路由静态分类提示以及 Node `punycode` 弃用警告，但构建成功，均非本任务代码错误。
