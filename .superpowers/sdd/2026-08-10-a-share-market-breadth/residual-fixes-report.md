# A 股市场广度：残余修复报告

## 结论

**READY EXCEPT EXTERNAL BLOCKERS**

`final-rereview.md` 的三个开放 Important 已在代码提交 `05751b7`（`fix: close residual market data review gaps`）中处理。本轮未联网、未回填真实行情，未生成 `og.png`，未 push、deploy 或 publish。

## 修复摘要

1. **Canonical 可恢复一致性**
   - `public/data/current.json` 是唯一发布权威与逻辑提交点；`data/market_history.csv` 是由 active version CSV 精确派生的 review mirror。
   - 每次启动在获取指数、股票数据及任何 early no-op 前，先把 canonical 原子重建为 manifest 所指 CSV 的逐字节副本。
   - 发布异常不再盲目回滚 canonical。代码重新读取当前可见 manifest 并做 best-effort reconciliation；若进程终止，下一次启动按当时可见的 old/new manifest 确定性修复。
   - 覆盖 manifest replace 成功后 public-data fsync 抛错、old/new manifest crash boundary、重启修复及 routine early no-op 修复。

2. **Checkpoint 新鲜度**
   - cache namespace 升级为 `v3-akshare-unadjusted`，使用 workflow run generation 和不可变 `run.json` metadata。
   - 同一个 `github.run_id` 的 run attempts 复用已完成 symbol；不同 generation 不把失败中的部分数据当成权威。
   - 新 full generation 总是重新请求完整源区间，可吸收旧日期修订。
   - routine generation 从最近成功 generation 取得前缀，但至少重抓最近五个指数交易日到 `as_of`，并用响应覆盖该区间，可吸收修订或删除。
   - 只有发布成功或规范内容完全相同后才切换 `latest-success.json`；成功后清理旧 completed generations。

3. **资源规模约束**
   - 每个 symbol 在写 checkpoint 和进入聚合前，裁剪为一条最新的、正价且非停牌的 2025 窗口前基准，加 2025 起的统计窗口行。
   - 跨年长停牌复牌口径仍保留，同时 checkpoint 与聚合行数不再随全部上市年限增长。
   - 数十年响应 fixture 验证落盘和返回均只保留 baseline + window；120-symbol 合成测试和成功代际清理测试约束总 cache 行数近似 `O(symbols × window trading days)`。

## TDD 证据

- Canonical 三个初始回归：`3 failed`，分别显示新 manifest/旧 canonical 混版、重启不修复、early no-op 不修复；实现后 focused tests 全绿。
- Checkpoint freshness 初始回归：同 generation resume、新 full revision、routine overlap revision 三项均因缺少 generation/freshness contract 失败；实现后 focused `4 passed`。
- 资源约束初始回归：数十年单 symbol 保留 8,000+ 旧行，120-symbol fixture 保留 `1,067,040` 行；裁剪后 focused tests 全绿。
- 成功 generation 清理初始回归保留 old/current 两个目录；实现后只保留 current generation。

## 最终验证

- `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest tests/data tests/build -q`
  - `63 passed, 78 warnings in 15.51s`
  - warnings 为 pandas FutureWarning，无失败。
- `PATH=/Users/chenchanghang/.nvm/versions/node/v22.22.1/bin:$PATH npm test -- --run`
  - `16 passed`。
- `npm run lint`
  - 退出码 0。
- `npm run build`
  - 退出码 0；仅 Node `punycode` deprecation 与 vinext route-classification 提示。
- `git diff --check`
  - 退出码 0。

## 剩余外部阻塞与关注点

- AKShare/代理网络仍阻止真实 2025 至今全量回填；`current.json` 仍指向空 bootstrap，真实市场规模、连续性和数值尚未完成生产验收。
- 尚未 push、部署或验证 Sites 自动重建；本轮明确不执行这些动作。
- 上游 Sina adapter 可能先下载长历史再过滤；本轮把持久 cache 和 pandas 聚合内存裁剪为有界规模，但未改变第三方 adapter 自身单请求的瞬时下载行为。
- pandas FutureWarning 仍是非阻断的后续兼容性清理项。
