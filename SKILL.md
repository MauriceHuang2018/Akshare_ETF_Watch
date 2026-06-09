---
name: akshare-etf-watch
description: "ETF 监控：按跟踪指数保留最大规模一只，计算刘晨明对数乖离率减法，输出四类互斥名单。默认新浪数据源（东财被封时用 --data-source sina）。周一刷新静态，每日刷新行情。在用户请求 ETF/ETF基金 监控、乖离率减法、ETF 关注列表时使用。"
---

# Akshare ETF Watch 技能

## 版本说明（v0.1 / v0.2）

| 版本 | 脚本 | 产品池 | 缓存 | 标识 |
|------|------|--------|------|------|
| **v0.2**（默认） | `scripts/etf_watch.py` → `scripts/v02/` | 基金公司白名单（默认华夏、易方达、国泰、景顺长城） | `cache/v02/YYYY-Www/` | `skill_version: "0.2"` |
| **v0.1**（冻结） | `scripts/v01/etf_watch.py` | 全市场 ETF | `cache/YYYY-Www/` | `skill_version: "0.1"` |

**Agent 默认走 v0.2**；用户明确要求「全市场 / v0.1 / 旧版」时用 v01。

v0.2 额外能力：

- `--fund-managers 华夏,易方达`：调整白名单（变更后需 `--force-full`）
- `--theme-dedupe off|exact|enhanced`：第二级主题去重（**默认 enhanced**）
- `--themes-file`：覆盖 `config/themes.json`
- 两级去重：先按 `tracking_index`，再按 `theme_id`（规则 + `config/themes.json`）

## 功能描述

1. 获取 ETF 列表（v0.2：**四家基金公司白名单**；v0.1：全市场），排除无跟踪标的、货币/债券/银行/红利等价值风格；**保留跨境 ETF**。
2. 按 **跟踪标的（指数）** 分组，每组保留 **基金份额最大** 的一只；v0.2 再按 **主题** 合并（默认 enhanced）。
3. 对入选 ETF 计算 **刘晨明乖离率减法（第二版）**：
   - `LOGBIAS = (LN(CLOSE) - EMA(LN(CLOSE), 20)) * 100`
4. 按偏离度划分 **互斥** 四类名单（优先级：离场 > 减仓 > 预警 > 关注）：
   - **关注**：最近 5 个交易日均在 `[-5%, 5%]`
   - **预警**：最近 5 个交易日均在 `(10%, 15%]`
   - **减仓预警**：最近 3 个交易日均 `> 15%`
   - **离场预警**：最近 2 个交易日均 `< -5%`

## 运行模式（静态 / 动态分离）

| 数据类型 | 内容 | 刷新频率 |
|----------|------|----------|
| **静态** | ETF 代码、名称、跟踪指数（`fund_overview_em`）、规模去重结果 | **每周一** 或 `--force-full`（跟踪指数约 3～8 分钟） |
| **动态** | 日线收盘价、偏离度、四类名单 | **每次运行** 拉取历史 K 线（默认新浪 `fund_etf_hist_sina`） |

## 数据源（`--data-source`，东财被封时必看）

| 值 | 列表 | 历史 K 线 | 跟踪指数 | 规模 |
|----|------|-----------|----------|------|
| **sina**（默认） | `fund_etf_category_sina` 一次拉全市场 | `fund_etf_hist_sina` | 优先读缓存 `tracking_map.json`，否则按基金名称推断 | 沪深交易所官方 |
| **em** | `fund_etf_spot_em` | `fund_etf_hist_em` | `fund_overview_em` 逐只（慢，易限流） | 同上 |
| **ths** | `fund_etf_spot_ths` | 同 sina | 同 sina | 同上 |

缓存目录：

- **v0.2（默认）**：`skills/akshare-etf-watch/cache/v02/YYYY-Www/`
- **v0.1**：`skills/akshare-etf-watch/cache/YYYY-Www/`

（`selected.json` 静态；`hist.json` 含 `updated_at` 与 `data`）。

周二～周五：读取当周 `selected.json`，**仍每日更新** 各 ETF 历史收盘价并重算偏离度。

## 前置条件

1. 使用国内镜像安装依赖：

```bash
pip install akshare pandas numpy -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host=mirrors.aliyun.com --upgrade
```

2. 网络可访问东财、沪深交易所基金数据接口。
3. **sina 模式**：v0.2 静态约 **1 分钟**；每日 hist 约 **1.5～2 分钟**（~170 只）。v0.1 约 **5～8 分钟**（~412 只）。**em 模式**易遭东财限流，不建议批量使用。
4. 东财 `RemoteDisconnected` 时改用：`python skills/akshare-etf-watch/scripts/etf_watch.py --data-source sina`
5. 自测：`python skills/akshare-etf-watch/scripts/verify_etf_watch.py`

脚本对断连、超时等网络错误会**自动重试最多 4 次**。

## 执行流程（必须遵守）

1. 收到 ETF 监控请求后，直接执行 exec，无需检查环境。
2. 命令（**默认 v0.2**）：`python skills/akshare-etf-watch/scripts/etf_watch.py [--data-source sina] [--force-full] [--fund-managers 华夏,易方达,国泰,景顺长城] [--theme-dedupe enhanced] [--hist-only] [--limit N] [--code 513290]`
3. workdir 为工作区根目录。
4. 将脚本输出的 JSON 直接作为结果返回给用户，按 `lists` 下四个分类展示。

## 使用方式

```bash
# 常规（默认 v0.2 + 新浪数据源）
python skills/akshare-etf-watch/scripts/etf_watch.py --data-source sina

# 强制刷新静态名单（白名单/主题去重变更后也必须 --force-full）
python skills/akshare-etf-watch/scripts/etf_watch.py --force-full

# 调整基金公司白名单
python skills/akshare-etf-watch/scripts/etf_watch.py --fund-managers 华夏,易方达 --force-full

# 关闭第二级主题去重
python skills/akshare-etf-watch/scripts/etf_watch.py --theme-dedupe off --force-full

# v0.1 全市场（旧版）
python skills/akshare-etf-watch/scripts/v01/etf_watch.py --data-source sina --force-full

# 冒烟：只处理前 5 只入选 ETF
python skills/akshare-etf-watch/scripts/etf_watch.py --limit 5

# 单只 ETF 调试
python skills/akshare-etf-watch/scripts/etf_watch.py --data-source sina --code 589100

# 自测 / 回测冒烟（v0.2 默认）
python skills/akshare-etf-watch/scripts/verify_etf_watch.py

# v0.1 自测
python skills/akshare-etf-watch/scripts/v01/verify_etf_watch.py

# 指定 1～5 只 ETF 代码自测（跳过全市场流水线）
python skills/akshare-etf-watch/scripts/verify_etf_watch.py 515050 515880 513290
python skills/akshare-etf-watch/scripts/verify_etf_watch.py --data-source sina 589100
```

## 输出格式

JSON 主要字段：

- `skill_version`: `"0.2"` 或 `"0.1"`
- `fund_managers` / `theme_dedupe`: v0.2 筛选参数
- `run_mode`: `daily` 或 `static_full+daily`
- `hist_refreshed_at`: 动态数据更新时间
- `week_key`: 缓存周次，如 `2026-W23`
- `indicator_formula`: 指标公式说明
- `summary`: 各名单数量；v0.2 含 `after_index_dedupe_count`、`after_theme_dedupe_count`、`excluded.fund_manager`、`excluded.theme_merged`
- `lists.watch` / `lists.warning` / `lists.reduce_warning` / `lists.exit_warning` / `lists.unclassified`
- 每条 ETF：`code`, `name`, `tracking_index`, `fund_manager`, `theme_id`, `theme_label`, `theme_peer_count`, `log_bias_latest`, `deviation_last5`, `list`, `list_reason`

## 触发示例

- "跑一下 ETF/ETF基金 乖离率监控"
- "刘晨明乖离率 ETF/ETF基金 关注列表"
- "ETF/ETF基金 减仓预警和离场预警"
- "强制刷新 ETF/ETF基金 watch"

## 说明

- 主题同义词可在 `config/themes.json` 人工维护；变更后 `--force-full` 刷新静态缓存。
- 指标更适合趋势性强的成长/行业 ETF；银行、红利等价值风格已排除。
- 每次运行都会更新历史收盘价与偏离度；仅跟踪指数与入选名单在周一（或 `--force-full`）更新。
- 仅供参考，不构成投资建议。
