#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Smoke and unit checks for akshare-etf-watch (no full-market run required).

Default: fixed regression suite. With positional ETF codes (1-5): live self-test only.
"""

import argparse
import sys
from pathlib import Path

# Import from sibling module
sys.path.insert(0, str(Path(__file__).resolve().parent))
import etf_watch as ew

_MAX_LIVE_CODES = 5


def _ok(cond: bool, msg: str) -> bool:
    """Print pass/fail line and return cond."""
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {msg}")
    return cond


def _collect_codes(raw_codes: list[str]) -> list[str]:
    """Normalize ETF codes from CLI tokens; dedupe while preserving order."""
    seen = set()
    out = []
    for raw in raw_codes:
        for part in str(raw).replace(",", " ").split():
            part = part.strip()
            if not part:
                continue
            code = ew._normalize_code(part)
            if code and code not in seen:
                seen.add(code)
                out.append(code)
    return out


def test_calc_log_bias_synthetic() -> bool:
    """Flat closes -> log-bias near zero after EMA warms up."""
    closes = [10.0] * 40
    devs = ew.calc_log_bias(closes)
    ok = len(devs) >= 1
    if devs:
        ok = ok and abs(devs[-1]) < 0.01
    return _ok(ok, f"calc_log_bias flat last={devs[-1] if devs else None}")


def test_classify_rules() -> bool:
    """Boundary checks for mutually exclusive list rules."""
    cases = [
        (["-6", "-6"], "exit_warning"),
        (["16", "17", "18"], "reduce_warning"),
        (["11", "12", "13", "14", "15"], "warning"),
        (["0", "1", "2", "3", "4"], "watch"),
        (["8", "9", "10", "11", "12"], None),
        (["20", "0", "0", "0", "0"], None),
    ]
    ok = True
    for tail, expected in cases:
        vals = [float(x) for x in tail]
        name, _ = ew.classify_list(vals)
        if name != expected:
            ok = False
            print(f"       classify {tail} got {name} expected {expected}")
    return _ok(ok, "classify_list boundary cases")


def test_513290_cross_border() -> bool:
    """513290 should not be excluded as value-style when cross-border keywords match."""
    name = "纳指生物科技ETF"
    tracking = "纳斯达克生物科技"
    ex = ew._is_value_style_excluded(name, tracking)
    cb = ew._is_cross_border(name, tracking)
    return _ok(not ex and cb, "513290 style filter keeps cross-border ETF")


def test_live_codes(codes: list[str], data_source: str = ew._DEFAULT_DATA_SOURCE) -> bool:
    """Fetch hist for each ETF code and compute log-bias classification."""
    try:
        import akshare  # noqa: F401
    except ImportError:
        return _ok(False, "akshare not installed")

    all_ok = True
    for code in codes:
        try:
            rows = ew._fetch_etf_hist(code, ew._HIST_BARS, data_source=data_source)
        except Exception as e:
            all_ok = False
            _ok(False, f"live hist fetch {code}: {e}")
            continue

        series, break_note = ew._attach_deviation_series(rows)
        devs = [x["deviation"] for x in series]
        list_name, reason = ew.classify_list(devs)
        tail5 = devs[-5:] if len(devs) >= 5 else devs
        latest_date = series[-1]["date"] if series else None
        print(
            f"       code={code} bars={len(rows)} latest_date={latest_date} "
            f"log_bias={round(devs[-1], 4) if devs else None} "
            f"deviation_last5={[round(x, 2) for x in tail5]} "
            f"list={list_name} ({reason}) price_break_trimmed={break_note or False}"
        )
        ok = len(rows) > 0
        if not _ok(ok, f"live ETF {code} ({data_source})"):
            all_ok = False
    return all_ok


def test_tracking_overview_513290() -> bool:
    """fund_overview_em returns tracking index for ETF 513290."""
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "etf_watch_tracking_test.json"
    try:
        m = ew._fetch_tracking_map(["513290"], tmp, force=True)
        t = m.get("513290", "")
        ok = bool(t) and "无跟踪" not in t
        print(f"       tracking={t}")
        return _ok(ok, "fund_overview_em tracking for 513290")
    except Exception as e:
        return _ok(False, f"tracking overview: {e}")


def test_live_pipeline_limit() -> bool:
    """Small static sample + 3 ETF daily hist refresh."""
    root = ew._skill_root()
    try:
        result = ew.run_pipeline(
            root,
            force_full=True,
            static_limit=30,
            limit=3,
        )
    except Exception as e:
        return _ok(False, f"pipeline limit=3 exception: {e}")

    n = result.get("summary", {}).get("selected_etf_count")
    hist_at = result.get("hist_refreshed_at")
    ok = n == 3 and result.get("lists") is not None and hist_at
    print(
        f"       run_mode={result.get('run_mode')} selected={n} "
        f"hist_refreshed_at={hist_at}"
    )
    return _ok(ok, "pipeline static_limit=30 limit=3 end-to-end")


def _run_full_suite() -> int:
    """Run fixed regression steps (no user codes)."""
    steps = [
        test_calc_log_bias_synthetic,
        test_classify_rules,
        test_513290_cross_border,
        test_tracking_overview_513290,
        lambda: test_live_codes(["513290"]),
        test_live_pipeline_limit,
    ]
    results = [bool(fn()) for fn in steps]
    passed = sum(1 for x in results if x)
    total = len(results)
    print(f"\nDone: {passed}/{total} passed")
    return 0 if passed == total else 1


def _run_user_codes(codes: list[str], data_source: str) -> int:
    """Run quick unit checks plus live fetch for user-supplied ETF codes."""
    print(f"Self-test mode: {len(codes)} code(s), data_source={data_source}")
    print(f"Codes: {', '.join(codes)}\n")

    steps = [
        test_calc_log_bias_synthetic,
        test_classify_rules,
        lambda: test_live_codes(codes, data_source=data_source),
    ]
    results = [bool(fn()) for fn in steps]
    passed = sum(1 for x in results if x)
    total = len(results)
    print(f"\nDone: {passed}/{total} passed")
    return 0 if passed == total else 1


def main():
    """Run verification: full suite or user ETF code self-test (max 5)."""
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(
        description="akshare-etf-watch smoke tests; optional 1-5 ETF codes for live self-test",
    )
    parser.add_argument(
        "codes",
        nargs="*",
        help="ETF fund codes (max 5), e.g. 515050 515880 or 515050,515880",
    )
    parser.add_argument(
        "--data-source",
        choices=ew._VALID_DATA_SOURCES,
        default=ew._DEFAULT_DATA_SOURCE,
        help="hist data source for live self-test (default: sina)",
    )
    args = parser.parse_args()

    codes = _collect_codes(args.codes)
    if len(codes) > _MAX_LIVE_CODES:
        print(
            f"Error: at most {_MAX_LIVE_CODES} ETF codes allowed, got {len(codes)}: "
            f"{', '.join(codes)}",
            file=sys.stderr,
        )
        sys.exit(2)

    if codes:
        sys.exit(_run_user_codes(codes, args.data_source))
    sys.exit(_run_full_suite())


if __name__ == "__main__":
    main()
