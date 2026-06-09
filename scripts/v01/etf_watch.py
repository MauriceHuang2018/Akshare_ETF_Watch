#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ETF watch script: universe, dedupe by tracking index, Liu Chenming log-bias indicator, four lists.
Static data (codes, tracking index, scale dedupe): Monday or --force-full.
Dynamic data (daily close, deviation): refreshed on every run.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import akshare as ak
import numpy as np
import pandas as pd

_SKILL_VERSION = "0.1"

_FETCH_RETRIES = 4
_RETRY_BASE_SLEEP = 1.2
_EMA_SPAN = 20
_HIST_BARS = 45
# Eastmoney ETF hist: one HTTP per code; ~1.5s gap avoids RemoteDisconnected storm
_HIST_SLEEP_OK_SEC = 2.0
_HIST_SLEEP_FAIL_SEC = 8.0
_HIST_RETRIES = 3
_HIST_RETRY_SLEEP = 2.0
_HIST_CHECKPOINT_EVERY = 25
_HIST_CALENDAR_DAYS = 90
# Single-day |return| above this: treat as split/merge (unadjusted Sina), trim prior bars
_PRICE_BREAK_THRESH = 0.25

# Exclude value-style / fixed-income ETFs (name or tracking index); cross-border overrides exclude.
_EXCLUDE_KEYWORDS = (
    "货币", "快线", "国债", "债券", "债ETF", "信用债", "利率债",
    "银行", "红利", "高股息", "股息", "低波红利", "价值ETF", "价值指数",
    "中证红利", "红利低波", "红利指数", "现金流", "自由现金流",
)
_CROSS_BORDER_KEYWORDS = (
    "纳指", "纳斯达克", "标普", "美股", "道琼斯", "道指", "中概", "互联",
    "恒生", "港股", "H股", "日经", "日本", "德国", "法国", "越南", "沙特",
    "全球", "海外", "QDII", "跨境", "中韩", "法国CAC", "标普500", "生物科技",
    "韩国", "新加坡", "印度", "欧洲", "MSCI", "日经225", "德国DAX",
)
# Per-code overview fetch interval (Eastmoney static refresh only)
_TRACKING_FETCH_SLEEP = 0.18
# Default data source when Eastmoney is rate-limited
_DEFAULT_DATA_SOURCE = "sina"
_VALID_DATA_SOURCES = ("sina", "em", "ths")
# Fund company tokens stripped for name-based tracking index inference
_FUND_MANAGER_TOKENS = (
    "华夏", "易方达", "嘉实", "南方", "广发", "汇添富", "博时", "富国", "华泰柏瑞",
    "国泰", "工银", "招商", "鹏华", "银华", "景顺长城", "中欧", "永赢", "华安",
    "天弘", "平安", "建信", "中银", "摩根", "国投瑞银", "申万菱信", "万家", "国联",
    "海富通", "大成", "华宝", "东财", "中信建投", "国寿安保", "安信", "红土创新",
)


def _retryable_network_error(exc: BaseException) -> bool:
    """Return True if the exception looks like a transient network failure."""
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    s = str(exc).lower()
    needles = (
        "connection aborted", "remote disconnected", "remote end closed",
        "connection reset", "timeout", "timed out", "10054", "broken pipe",
    )
    return any(n in s for n in needles)


def _call_with_retry(
    func,
    *args,
    max_retries: int = _FETCH_RETRIES,
    retry_sleep: float = _RETRY_BASE_SLEEP,
    **kwargs,
):
    """Call akshare API with retries on network errors."""
    last_err = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt + 1 < max_retries and _retryable_network_error(e):
                time.sleep(retry_sleep * (attempt + 1))
                continue
            raise
    raise last_err


def _normalize_code(raw: str) -> str:
    """Return 6-digit fund code string (strip sh/sz prefix and .SH/.SZ suffix)."""
    s = str(raw).strip().lower().split(".")[0]
    if s.startswith(("sh", "sz")) and len(s) > 2:
        s = s[2:]
    if s.isdigit():
        return s.zfill(6)
    return s


def _code_to_sina_symbol(code: str) -> str:
    """Map 6-digit code to Sina symbol sh510050 / sz159915."""
    c = _normalize_code(code)
    if c.startswith(("5", "6")):
        return f"sh{c}"
    return f"sz{c}"


def _infer_tracking_from_name(name: str) -> str:
    """
    Infer tracking-index group key from fund name when Eastmoney overview is unavailable.
    Used with cached tracking_map.json when possible.
    """
    s = str(name or "").strip()
    for token in ("ETF", "etf", "基金", "联接", "LOF", "交易型开放式指数"):
        s = s.replace(token, "")
    for token in _FUND_MANAGER_TOKENS:
        s = s.replace(token, "")
    s = s.strip(" -·")
    return s if len(s) >= 2 else str(name or "").strip()


def _hist_sleep_ok(data_source: str) -> float:
    """Inter-request delay after successful hist fetch."""
    if data_source == "sina":
        return 0.35
    return _HIST_SLEEP_OK_SEC


def _is_cross_border(name: str, tracking: str) -> bool:
    """True if ETF is treated as cross-border (always kept when matched)."""
    text = f"{name or ''}{tracking or ''}"
    return any(k in text for k in _CROSS_BORDER_KEYWORDS)


def _is_value_style_excluded(name: str, tracking: str) -> bool:
    """Exclude currency/bond/bank/dividend value-style unless cross-border."""
    if _is_cross_border(name, tracking):
        return False
    text = f"{name or ''}{tracking or ''}"
    if any(k in text for k in _EXCLUDE_KEYWORDS):
        return True
    if tracking and ("债券" in tracking or "货币" in tracking):
        return True
    return False


def _skill_root() -> Path:
    """Return skill root directory (handles v01/v02 script layout)."""
    script_dir = Path(__file__).resolve().parent
    if script_dir.name in ("v01", "v02"):
        return script_dir.parent.parent
    return script_dir.parent


def _iso_week_key(dt: datetime) -> str:
    """Cache folder key: YYYY-Www."""
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _cache_dir(skill_root: Path, week_key: str) -> Path:
    """Path to this week's cache directory."""
    return skill_root / "cache" / week_key


def _should_refresh_static(force_full: bool, cache_selected: Path) -> bool:
    """Refresh universe/selected on Monday, --force-full, or missing cache."""
    if force_full:
        return True
    if not cache_selected.is_file():
        return True
    return datetime.now().weekday() == 0


def _refresh_static_data(
    cache_root: Path,
    week_key: str,
    force_tracking: bool,
    static_limit: int = None,
    data_source: str = _DEFAULT_DATA_SOURCE,
) -> tuple:
    """
    Fetch spot, tracking (overview per code), scale; build universe and selected; write static cache.

    Returns:
        (selected list, universe_count, excluded dict)
    """
    spot = _fetch_etf_spot(data_source)
    scale_map = _fetch_scale_map()
    code_col = "代码" if "代码" in spot.columns else "基金代码"
    name_col = "名称" if "名称" in spot.columns else "基金简称"
    candidate_codes = []
    names_by_code = {}
    for _, row in spot.iterrows():
        code = _normalize_code(row[code_col])
        name = str(row.get(name_col, "") or "")
        names_by_code[code] = name
        if _is_value_style_excluded(name, ""):
            continue
        candidate_codes.append(code)

    if static_limit and static_limit > 0:
        candidate_codes = candidate_codes[:static_limit]

    cache_tracking = cache_root / "tracking_map.json"
    print(
        f"fetching tracking index for {len(candidate_codes)} ETFs (source={data_source})...",
        file=sys.stderr,
    )
    tracking_map = _fetch_tracking_map(
        candidate_codes,
        names_by_code,
        cache_tracking,
        force=force_tracking,
        data_source=data_source,
    )
    universe, excluded = _build_universe(spot, tracking_map, scale_map)
    selected, all_count = _dedupe_by_index(universe)
    cache_universe = cache_root / "universe.json"
    cache_selected = cache_root / "selected.json"
    cache_meta = cache_root / "meta.json"
    _save_json(cache_universe, {
        "universe_count": all_count,
        "excluded": excluded,
        "items": universe,
        "static_refreshed_at": datetime.now().isoformat(),
    })
    _save_json(cache_selected, selected)
    meta = _load_json(cache_meta) or {}
    meta.update({
        "week_key": week_key,
        "static_refreshed_at": datetime.now().isoformat(),
        "selected_count": len(selected),
        "tracking_map_refreshed_at": datetime.now().isoformat(),
    })
    _save_json(cache_meta, meta)
    return selected, all_count, excluded


def _fetch_etf_spot(data_source: str = _DEFAULT_DATA_SOURCE) -> pd.DataFrame:
    """All ETF spot/list rows from configured AKShare source."""
    if data_source == "em":
        return _call_with_retry(ak.fund_etf_spot_em)
    if data_source == "ths":
        return _call_with_retry(ak.fund_etf_spot_ths, date="")
    return _call_with_retry(ak.fund_etf_category_sina, symbol="ETF基金")


def _parse_overview_tracking(df: pd.DataFrame) -> str:
    """Extract tracking index from fund_overview_em single-fund dataframe."""
    if df is None or len(df) == 0:
        return ""
    col = "跟踪标的"
    if col not in df.columns:
        return ""
    val = str(df.iloc[0][col]).strip()
    if not val or val.lower() in ("nan", "none"):
        return ""
    if "无跟踪" in val:
        return ""
    return val


def _load_tracking_cache(cache_path: Path) -> dict:
    """Load tracking_map.json if present."""
    if not cache_path.is_file():
        return {}
    raw = _load_json(cache_path)
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    if isinstance(raw, dict):
        return raw
    return {}


def _fetch_tracking_map(
    codes: list,
    names_by_code: dict,
    cache_path: Path,
    force: bool,
    data_source: str = _DEFAULT_DATA_SOURCE,
) -> dict:
    """
    Build code -> tracking_index.
    em: fund_overview_em per code (slow, Eastmoney).
    sina/ths: reuse cache then infer from fund name (fast, no Eastmoney).
    """
    data = _load_tracking_cache(cache_path) if not force else dict(_load_tracking_cache(cache_path))

    if data_source == "em":
        total = len(codes)
        for i, code in enumerate(codes):
            try:
                df = _call_with_retry(ak.fund_overview_em, symbol=code)
                data[code] = _parse_overview_tracking(df)
            except Exception:
                data[code] = data.get(code, "")
            time.sleep(_TRACKING_FETCH_SLEEP)
            if (i + 1) % 50 == 0:
                print(f"tracking progress {i+1}/{total}", file=sys.stderr)
    else:
        for code in codes:
            if not force and data.get(code):
                continue
            name = names_by_code.get(code, "")
            cached = data.get(code, "")
            data[code] = cached if cached else _infer_tracking_from_name(name)

    _save_json(cache_path, {
        "updated_at": datetime.now().isoformat(),
        "count": len(data),
        "source": data_source,
        "data": data,
    })
    return data


def _fetch_scale_map() -> dict:
    """Map fund code -> share scale (份). SSE needs date; SZSE is latest."""
    scale = {}
    today = datetime.now()
    for delta in range(0, 10):
        d = today - timedelta(days=delta)
        date_str = d.strftime("%Y%m%d")
        try:
            sse = _call_with_retry(ak.fund_etf_scale_sse, date=date_str)
            if sse is not None and len(sse) > 0:
                for _, row in sse.iterrows():
                    code = _normalize_code(row.get("基金代码", ""))
                    val = row.get("基金份额")
                    if code and pd.notna(val):
                        scale[code] = float(val)
                break
        except Exception:
            continue
    try:
        sz = _call_with_retry(ak.fund_etf_scale_szse)
        if sz is not None and len(sz) > 0:
            for _, row in sz.iterrows():
                code = _normalize_code(row.get("基金代码", ""))
                val = row.get("基金份额")
                if code and pd.notna(val):
                    if code not in scale or float(val) > scale.get(code, 0):
                        scale[code] = float(val)
    except Exception:
        pass
    return scale


def _build_universe(spot: pd.DataFrame, tracking_map: dict, scale_map: dict) -> list:
    """
    Build filtered ETF records with tracking index and scale.
    Drops no-tracking, value-style; keeps cross-border.
    """
    code_col = "代码" if "代码" in spot.columns else "基金代码"
    name_col = "名称" if "名称" in spot.columns else "基金简称"

    universe = []
    excluded = {"no_tracking": 0, "value_style": 0}
    for _, row in spot.iterrows():
        code = _normalize_code(row[code_col])
        name = str(row.get(name_col, "") or "")
        tracking_index = str(tracking_map.get(code, "") or "").strip()
        if not tracking_index:
            excluded["no_tracking"] += 1
            continue
        if _is_value_style_excluded(name, tracking_index):
            excluded["value_style"] += 1
            continue
        shares = scale_map.get(code)
        if shares is None and "最新份额" in row.index:
            v = row.get("最新份额")
            if pd.notna(v):
                shares = float(v)
        if shares is None and "总市值" in row.index:
            v = row.get("总市值")
            if pd.notna(v):
                shares = float(v)
        universe.append({
            "code": code,
            "name": name,
            "tracking_index": tracking_index,
            "scale_shares": shares,
            "cross_border": _is_cross_border(name, tracking_index),
        })
    return universe, excluded


def _dedupe_by_index(universe: list) -> tuple:
    """Pick largest scale ETF per tracking_index."""
    from collections import defaultdict

    groups = defaultdict(list)
    for item in universe:
        key = item["tracking_index"]
        groups[key].append(item)

    selected = []
    for key, items in groups.items():
        def sort_key(x):
            s = x.get("scale_shares")
            return (s is None, -(s or 0), x["code"])

        best = sorted(items, key=sort_key)[0]
        best["peer_count"] = len(items)
        selected.append(best)
    return selected, len(universe)


def _fetch_etf_hist_sina(code: str, bars: int) -> list:
    """Daily closes from Sina fund_etf_hist_sina (single request per symbol)."""
    sym = _code_to_sina_symbol(code)
    df = _call_with_retry(
        ak.fund_etf_hist_sina,
        symbol=sym,
        max_retries=_HIST_RETRIES,
        retry_sleep=_HIST_RETRY_SLEEP,
    )
    if df is None or len(df) == 0:
        return []
    rows = []
    for _, row in df.iterrows():
        d = row.get("date", "")
        if hasattr(d, "strftime"):
            ds = d.strftime("%Y-%m-%d")
        else:
            ds = str(d)[:10]
        rows.append({"date": ds, "close": float(row["close"])})
    rows.sort(key=lambda x: x["date"])
    rows, _ = _trim_after_price_break(rows)
    return rows[-bars:]


def _fetch_etf_hist_em(code: str, bars: int) -> list:
    """Daily closes from Eastmoney fund_etf_hist_em."""
    end = datetime.now()
    start = end - timedelta(days=_HIST_CALENDAR_DAYS)
    df = _call_with_retry(
        ak.fund_etf_hist_em,
        symbol=code,
        period="daily",
        start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"),
        adjust="",
        max_retries=_HIST_RETRIES,
        retry_sleep=_HIST_RETRY_SLEEP,
    )
    if df is None or len(df) == 0:
        return []
    close_col = "收盘" if "收盘" in df.columns else "close"
    date_col = "日期" if "日期" in df.columns else "date"
    rows = []
    for _, row in df.iterrows():
        d = row[date_col]
        if hasattr(d, "strftime"):
            ds = d.strftime("%Y-%m-%d")
        else:
            ds = str(d)[:10]
        rows.append({"date": ds, "close": float(row[close_col])})
    rows.sort(key=lambda x: x["date"])
    rows, _ = _trim_after_price_break(rows)
    return rows[-bars:]


def _fetch_etf_hist(code: str, bars: int, data_source: str = _DEFAULT_DATA_SOURCE) -> list:
    """Daily closes for log-bias; returns [{date, close}, ...]."""
    if data_source == "em":
        return _fetch_etf_hist_em(code, bars)
    return _fetch_etf_hist_sina(code, bars)


def _trim_after_price_break(hist_rows: list, thresh: float = _PRICE_BREAK_THRESH) -> tuple:
    """
    Drop history before a one-day jump (unadjusted price split/merge).
    Returns (trimmed_rows, break_note or None).
    """
    if len(hist_rows) < 2:
        return hist_rows, None
    for i in range(1, len(hist_rows)):
        prev_c = float(hist_rows[i - 1]["close"])
        cur_c = float(hist_rows[i]["close"])
        if prev_c <= 0:
            continue
        ret = abs(cur_c / prev_c - 1.0)
        if ret > thresh:
            pct = (cur_c / prev_c - 1.0) * 100.0
            note = f"{hist_rows[i]['date']}:daily_return={pct:.1f}%"
            return hist_rows[i:], note
    return hist_rows, None


def calc_log_bias(closes: list) -> list:
    """
    Liu Chenming subtraction: (ln(close) - EMA20(ln(close))) * 100 per bar.
    Tongdaxin-style EMA: ewm(span=20, adjust=False).
    """
    if len(closes) < _EMA_SPAN:
        return []
    ln_c = np.log(np.array(closes, dtype=float))
    ema = pd.Series(ln_c).ewm(span=_EMA_SPAN, adjust=False).mean().values
    dev = (ln_c - ema) * 100.0
    # Align with Tongdaxin: EMA needs warmup; first valid bar is index EMA_SPAN-1
    start = _EMA_SPAN - 1
    return [round(float(x), 4) for x in dev[start:]]


def _attach_deviation_series(hist_rows: list, trim_break: bool = True) -> tuple:
    """
    Add deviation to each hist row where EMA is defined.
    Returns (series, price_break_note).
    """
    if not hist_rows:
        return [], None
    break_note = None
    if trim_break:
        hist_rows, break_note = _trim_after_price_break(hist_rows)
    if len(hist_rows) < _EMA_SPAN:
        return [], break_note
    closes = [r["close"] for r in hist_rows]
    devs = calc_log_bias(closes)
    if not devs:
        return [], break_note
    offset = len(closes) - len(devs)
    out = []
    for i, row in enumerate(hist_rows):
        if i < offset:
            continue
        j = i - offset
        out.append({
            "date": row["date"],
            "close": row["close"],
            "deviation": devs[j],
        })
    return out, break_note


def classify_list(deviation_tail: list) -> tuple:
    """
    Mutually exclusive list assignment; priority: exit > reduce > warning > watch.
    Returns (list_name, reason) or (None, reason).
    """
    if len(deviation_tail) < 2:
        return None, "insufficient_bars"
    d = deviation_tail

    last2 = d[-2:]
    if all(x < -5 for x in last2):
        return "exit_warning", "last_2_days all < -5%"

    if len(d) >= 3:
        last3 = d[-3:]
        if all(x > 15 for x in last3):
            return "reduce_warning", "last_3_days all > 15%"

    if len(d) >= 5:
        last5 = d[-5:]
        if all(-5 <= x <= 5 for x in last5):
            return "watch", "last_5_days in [-5%, 5%]"
        if all(10 < x <= 15 for x in last5):
            return "warning", "last_5_days in (10%, 15%]"

    return None, "no_rule_matched"


def _load_json(path: Path):
    """Load JSON file if exists."""
    if not path.is_file():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_json(path: Path, data) -> None:
    """Write JSON with UTF-8."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _save_hist_cache(cache_hist_path: Path, hist: dict, errors: int) -> None:
    """Write hist.json checkpoint."""
    ok = sum(1 for v in hist.values() if len(v) >= _EMA_SPAN)
    _save_json(cache_hist_path, {
        "updated_at": datetime.now().isoformat(),
        "bars": _HIST_BARS,
        "errors": errors,
        "ok_count": ok,
        "data": hist,
    })


def _fetch_all_hist(selected: list, cache_hist_path: Path, data_source: str = _DEFAULT_DATA_SOURCE) -> dict:
    """
    Fetch latest daily closes per code (sequential; Eastmoney rate-limits bursts).
    Typical: 412 * (~1s request + 1.5s gap) ~= 17 min when stable; much slower if throttled.
    """
    hist = {}
    total = len(selected)
    errors = 0
    t_batch = time.time()
    for i, item in enumerate(selected):
        code = item["code"]
        t0 = time.time()
        try:
            rows = _fetch_etf_hist(code, _HIST_BARS, data_source=data_source)
            hist[code] = rows
            if len(rows) < _EMA_SPAN:
                errors += 1
                time.sleep(_HIST_SLEEP_FAIL_SEC)
            else:
                time.sleep(_hist_sleep_ok(data_source))
        except Exception as e:
            hist[code] = []
            item["hist_error"] = str(e)
            errors += 1
            time.sleep(_HIST_SLEEP_FAIL_SEC)
        elapsed = time.time() - t0
        if (i + 1) % 20 == 0:
            ok_n = sum(1 for v in hist.values() if len(v) >= _EMA_SPAN)
            avg = (time.time() - t_batch) / (i + 1)
            eta_min = avg * (total - i - 1) / 60.0
            print(
                f"hist progress {i+1}/{total} ok={ok_n} err={errors} "
                f"last={elapsed:.1f}s eta={eta_min:.0f}min",
                file=sys.stderr,
            )
        if (i + 1) % _HIST_CHECKPOINT_EVERY == 0:
            _save_hist_cache(cache_hist_path, hist, errors)
    _save_hist_cache(cache_hist_path, hist, errors)
    return hist


def _load_hist_map(cache_hist_path: Path) -> dict:
    """Load hist payload; support legacy plain dict format."""
    raw = _load_json(cache_hist_path)
    if raw is None:
        return {}
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw if isinstance(raw, dict) else {}


def run_pipeline(
    skill_root: Path,
    force_full: bool,
    single_code: str = None,
    limit: int = None,
    static_limit: int = None,
    hist_only: bool = False,
    data_source: str = _DEFAULT_DATA_SOURCE,
) -> dict:
    """Main pipeline: static (weekly) -> daily hist -> classify -> JSON result."""
    week_key = _iso_week_key(datetime.now())
    cache_root = _cache_dir(skill_root, week_key)
    cache_meta = cache_root / "meta.json"
    cache_universe = cache_root / "universe.json"
    cache_selected = cache_root / "selected.json"
    cache_hist = cache_root / "hist.json"

    refresh_static = (not hist_only) and _should_refresh_static(force_full, cache_selected)

    warnings = []
    if single_code:
        code = _normalize_code(single_code)
        hist_rows = _fetch_etf_hist(code, _HIST_BARS, data_source=data_source)
        series, break_note = _attach_deviation_series(hist_rows)
        devs = [x["deviation"] for x in series]
        list_name, reason = classify_list(devs)
        return {
            "mode": "single",
            "code": code,
            "deviation_series": series[-10:],
            "log_bias_latest": devs[-1] if devs else None,
            "price_break_trimmed": break_note,
            "list": list_name,
            "list_reason": reason,
        }

    excluded = None
    all_count = None
    if refresh_static:
        static_mode = "static_full"
        selected, all_count, excluded = _refresh_static_data(
            cache_root,
            week_key,
            force_tracking=True,
            static_limit=static_limit,
            data_source=data_source,
        )
    else:
        static_mode = "static_cache"
        selected = _load_json(cache_selected) or []
        if not selected:
            warnings.append("selected cache empty; falling back to static full")
            selected, all_count, excluded = _refresh_static_data(
                cache_root,
                week_key,
                force_tracking=force_full,
                static_limit=static_limit,
                data_source=data_source,
            )
            static_mode = "static_full"

    if limit and limit > 0:
        selected = selected[:limit]

    print(f"refreshing hist for {len(selected)} ETFs...", file=sys.stderr)
    hist_map = _fetch_all_hist(selected, cache_hist_path=cache_hist, data_source=data_source)
    hist_payload = _load_json(cache_hist) or {}
    hist_updated_at = hist_payload.get("updated_at") if isinstance(hist_payload, dict) else None

    meta = _load_json(cache_meta) or {}
    meta["hist_refreshed_at"] = hist_updated_at or datetime.now().isoformat()
    _save_json(cache_meta, meta)

    run_mode = "static_full+daily" if static_mode == "static_full" else "daily"

    lists = {
        "watch": [],
        "warning": [],
        "reduce_warning": [],
        "exit_warning": [],
        "unclassified": [],
    }

    for item in selected:
        code = item["code"]
        hist_rows = hist_map.get(code, [])
        series, break_note = _attach_deviation_series(hist_rows)
        devs = [x["deviation"] for x in series]
        list_name, reason = classify_list(devs)
        entry = {
            "code": code,
            "name": item.get("name", ""),
            "tracking_index": item.get("tracking_index", ""),
            "scale_shares": item.get("scale_shares"),
            "cross_border": item.get("cross_border", False),
            "peer_count": item.get("peer_count"),
            "latest_date": series[-1]["date"] if series else None,
            "close_latest": series[-1]["close"] if series else None,
            "log_bias_latest": devs[-1] if devs else None,
            "deviation_last5": devs[-5:] if len(devs) >= 5 else devs,
            "price_break_trimmed": break_note,
            "list": list_name,
            "list_reason": reason,
        }
        if break_note:
            warnings.append(f"{code} price break trimmed at {break_note}")
        if list_name and list_name in lists:
            lists[list_name].append(entry)
        else:
            lists["unclassified"].append(entry)

    hist_ok = sum(1 for c in selected if len(hist_map.get(c["code"], [])) >= _EMA_SPAN)
    result = {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "week_key": week_key,
        "skill_version": _SKILL_VERSION,
        "run_mode": run_mode,
        "static_mode": static_mode,
        "force_full": force_full,
        "hist_refreshed_at": hist_updated_at,
        "data_source": data_source,
        "indicator_formula": "ln(close)-ema20(ln(close))*100",
        "meta": {
            "ema_span": _EMA_SPAN,
            "data_source": data_source,
            "cache_dir": str(cache_root),
            "lists_mutually_exclusive": True,
            "list_priority": ["exit_warning", "reduce_warning", "warning", "watch"],
        },
        "summary": {
            "selected_etf_count": len(selected),
            "hist_sufficient_count": hist_ok,
            "watch_count": len(lists["watch"]),
            "warning_count": len(lists["warning"]),
            "reduce_warning_count": len(lists["reduce_warning"]),
            "exit_warning_count": len(lists["exit_warning"]),
            "unclassified_count": len(lists["unclassified"]),
        },
        "lists": lists,
        "warnings": warnings,
    }
    if all_count is not None:
        result["summary"]["all_etf_after_filter"] = all_count
        result["summary"]["excluded"] = excluded
    elif cache_universe.is_file():
        u = _load_json(cache_universe)
        result["summary"]["all_etf_after_filter"] = u.get("universe_count")
        result["summary"]["excluded"] = u.get("excluded")
    _save_json(cache_root / "result.json", result)
    return result


def main():
    """CLI entry."""
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="AKShare ETF watch with log-bias lists")
    parser.add_argument("--force-full", action="store_true", help="Refresh static universe/selected (also on Monday)")
    parser.add_argument("--code", type=str, default=None, help="Single ETF code debug mode")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N selected ETFs (smoke test)")
    parser.add_argument(
        "--static-limit",
        type=int,
        default=None,
        help="On static refresh, fetch tracking for first N name-filtered ETFs only",
    )
    parser.add_argument(
        "--hist-only",
        action="store_true",
        help="Skip static refresh; reload selected.json and refetch all hist",
    )
    parser.add_argument(
        "--data-source",
        choices=_VALID_DATA_SOURCES,
        default=_DEFAULT_DATA_SOURCE,
        help="sina (default): Sina hist+list; em: Eastmoney; ths: THS list + Sina hist",
    )
    args = parser.parse_args()

    skill_root = _skill_root()
    try:
        result = run_pipeline(
            skill_root,
            force_full=args.force_full,
            single_code=args.code,
            limit=args.limit,
            static_limit=args.static_limit,
            hist_only=args.hist_only,
            data_source=args.data_source,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
