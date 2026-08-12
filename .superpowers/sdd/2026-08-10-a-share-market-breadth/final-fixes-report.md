# A 股市场广度网站：最终修复报告

## 结论

**READY EXCEPT EXTERNAL BLOCKERS**

`final-review.md` 中 1 个 Critical 和 6 个 Important 本地问题已全部修复并由离线回归测试覆盖。代码修复提交为 `d6631b7`（`fix: resolve final market breadth review blockers`）。

本轮按要求没有联网回填真实行情，没有生成 `og.png`，没有部署、推送或发布。

## 已修复项

1. Clean checkout 构建：Sites Vite 插件迁至 tracked 的 `tooling/sites-vite-plugin.ts`，并新增从候选 `git archive` 构建的回归测试。
2. Actions 检查点：工作流在更新前恢复稳定的 `v2-akshare-unadjusted` cache namespace，在更新后以 `if: always()` 保存；检查点按基线、symbol 和覆盖终点组织，可跨 runner 与后续 `as_of` 增量复用，并严格校验 schema、symbol、日期范围、数值和停牌标记。
3. 幂等/no-op：同一交易日已有有效记录、休市日没有新指数交易日、或重算后的规范 CSV 内容未变化时，不创建 UUID、不切换 manifest。
4. 跨年停牌基准：个股历史使用有界的 `1990-12-01` 基线，确保 2025 窗口前最后一个有效非停牌收盘可用于复牌日比较；新增跨年连续停牌回归样本。
5. 完整性门槛：近期 `valid_count` 下降不再因任意一只停牌股而全局绕过；分交易所校验使用与广度分类一致的有效价格条件，排除正数陈旧停牌价。
6. Canonical CSV：新增 tracked 的 `data/market_history.csv` bootstrap；发布时 canonical CSV 与不可变版本 CSV 使用相同字节，并在 manifest 切换失败时回滚 canonical，避免指针与长期主数据不一致。
7. 更新时间与过期状态：新 manifest 写入 RFC 3339 UTC `generated_at`；前端兼容 `updated_at` 迁移别名和无时间戳 bootstrap，显示北京时间的最近成功更新时间，并在错过工作日发布窗口或最新记录落后时显示琥珀色过期状态。

## 验证证据

- `PYTHONDONTWRITEBYTECODE=1 PATH=/Users/chenchanghang/.nvm/versions/node/v22.22.1/bin:$PATH python3 -m pytest tests/data tests/build -q`
  - `53 passed, 58 warnings in 4.64s`
  - warnings 均为已有 pandas FutureWarning；无失败。
- `PATH=/Users/chenchanghang/.nvm/versions/node/v22.22.1/bin:$PATH npm test -- --run`
  - `1 passed` test file，`16 passed` tests。
- `PATH=/Users/chenchanghang/.nvm/versions/node/v22.22.1/bin:$PATH npm run lint`
  - 退出码 0。
- `PATH=/Users/chenchanghang/.nvm/versions/node/v22.22.1/bin:$PATH npm run build`
  - 退出码 0；仅 Node `punycode` deprecation 与 vinext route-classification 提示。
- `git diff --check`
  - 退出码 0。
- 真正的 committed archive 验证：
  - `git archive --format=tar --output=/private/tmp/market-breadth-archive.kVRULF/source.tar HEAD`
  - 解压至 `/private/tmp/market-breadth-archive.kVRULF/checkout`，仅将仓库 `node_modules` symlink 进去；确认 tracked `tooling/sites-vite-plugin.ts` 存在、旧 ignored `build/sites-vite-plugin.ts` 不存在。
  - 在该目录运行 production build，退出码 0。

## 仍存外部阻塞与关注点

- AKShare/代理网络阻塞仍未解除，因此真实 2025-01-01 至今历史尚未完成回填，`current.json` 仍安全指向空 bootstrap；不能声称真实全量数据已验收。
- 尚未 push、部署或验证首次 Sites 发布及后续数据提交触发重建；这些动作本轮未获授权且被明确排除。
- 前端 stale 逻辑按工作日和北京时间 18:00 保守判断，未内置中国法定节假日日历；节假日可能显示保守的“数据可能已过期”，不会把旧数据误标为正常。
- 测试仍报告 pandas FutureWarning，属于非阻断的后续兼容性清理项。
