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
    names = {"513290": "纳指生物科技ETF"}
    try:
        m = ew._fetch_tracking_map(["513290"], names, tmp, force=True)
        t = m.get("513290", "")
        ok = bool(t) and "无跟踪" not in t
        print(f"       tracking={t}")
        return _ok(ok, "fund_overview_em tracking for 513290")
    except Exception as e:
        return _ok(False, f"tracking overview: {e}")


def test_fund_manager_whitelist() -> bool:
    """Whitelist accepts four default managers; rejects Tianhong; subset works."""
    matchers = ew._build_manager_matchers(ew._DEFAULT_FUND_MANAGERS)
    ok = True
    cases_pass = [
        ("华夏上证50ETF", "华夏"),
        ("易方达创业板ETF", "易方达"),
        ("国泰半导体ETF", "国泰"),
        ("景顺长城中证500ETF", "景顺长城"),
        ("景顺红利ETF", "景顺长城"),
    ]
    for name, expected in cases_pass:
        got = ew._match_fund_manager(name, matchers)
        if got != expected:
            ok = False
            print(f"       {name} got {got} expected {expected}")
    if ew._is_allowed_fund_manager("天弘中证500ETF", matchers):
        ok = False
        print("       天弘 should be rejected")
    subset = ew._build_manager_matchers(("华夏", "易方达"))
    if ew._match_fund_manager("国泰半导体ETF", subset) is not None:
        ok = False
        print("       subset should reject 国泰")
    if ew._match_fund_manager("华夏芯片ETF", subset) != "华夏":
        ok = False
    return _ok(ok, "fund manager whitelist and subset filter")


def _theme_test_items(pairs: list) -> list:
    """Build mock ETF records from (name, scale) pairs."""
    out = []
    for i, (name, scale) in enumerate(pairs):
        out.append({
            "code": f"51000{i}",
            "name": name,
            "tracking_index": name,
            "scale_shares": scale,
            "cross_border": False,
            "fund_manager": "华夏",
            "peer_count": 1,
        })
    return out


def test_theme_dedupe() -> bool:
    """Second-level theme dedupe: enhanced merges synonyms; exact/off behave as spec."""
    themes_cfg = ew._load_themes_config(ew._default_themes_path(ew._skill_root()))
    ok = True

    grid_items = _theme_test_items([
        ("华夏电网设备ETF", 100.0),
        ("国泰电网设备ETF", 200.0),
    ])
    sel, merged = ew._dedupe_by_theme(grid_items, "enhanced", themes_cfg)
    if len(sel) != 1 or merged != 1:
        ok = False
        print(f"       grid enhanced got len={len(sel)} merged={merged}")

    chip_items = _theme_test_items([
        ("华夏半导体ETF", 100.0),
        ("易方达芯片ETF", 150.0),
    ])
    sel, merged = ew._dedupe_by_theme(chip_items, "enhanced", themes_cfg)
    if len(sel) != 1 or merged != 1:
        ok = False
        print(f"       chip/semiconductor enhanced got len={len(sel)} merged={merged}")

    gem_items = _theme_test_items([
        ("华夏创业板ETF", 100.0),
        ("易方达创业板成长ETF", 120.0),
    ])
    sel_enh, merged_enh = ew._dedupe_by_theme(gem_items, "enhanced", themes_cfg)
    sel_ex, merged_ex = ew._dedupe_by_theme(list(gem_items), "exact", themes_cfg)
    if len(sel_enh) != 1 or merged_enh != 1:
        ok = False
        print(f"       gem enhanced got len={len(sel_enh)} merged={merged_enh}")
    if len(sel_ex) != 2 or merged_ex != 0:
        ok = False
        print(f"       gem exact got len={len(sel_ex)} merged={merged_ex}")

    off_items = _theme_test_items([
        ("华夏电网设备ETF", 100.0),
        ("国泰电网设备ETF", 200.0),
    ])
    sel_off, merged_off = ew._dedupe_by_theme(off_items, "off", themes_cfg)
    if len(sel_off) != 2 or merged_off != 0:
        ok = False
        print(f"       off mode got len={len(sel_off)} merged={merged_off}")

    return _ok(ok, "theme dedupe enhanced/exact/off")


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
    sv = result.get("skill_version")
    ok = n == 3 and result.get("lists") is not None and hist_at and sv == "0.2"
    print(
        f"       run_mode={result.get('run_mode')} selected={n} "
        f"skill_version={sv} hist_refreshed_at={hist_at}"
    )
    return _ok(ok, "pipeline static_limit=30 limit=3 end-to-end")


def _run_unit_suite() -> tuple:
    """Run offline unit tests; return (results, passed, total)."""
    steps = [
        test_calc_log_bias_synthetic,
        test_classify_rules,
        test_513290_cross_border,
        test_fund_manager_whitelist,
        test_theme_dedupe,
    ]
    results = [bool(fn()) for fn in steps]
    passed = sum(1 for x in results if x)
    return results, passed, len(steps)


def _run_live_suite() -> tuple:
    """Run network-dependent tests; failures are reported but optional."""
    steps = [
        test_tracking_overview_513290,
        lambda: test_live_codes(["513290"]),
        test_live_pipeline_limit,
    ]
    results = []
    for fn in steps:
        try:
            results.append(bool(fn()))
        except Exception as e:
            print(f"[SKIP] {fn.__name__}: {e}")
            results.append(False)
    passed = sum(1 for x in results if x)
    return results, passed, len(steps)


def _run_full_suite() -> int:
    """Run unit tests plus live tests (live may fail without network)."""
    _, unit_passed, unit_total = _run_unit_suite()
    _, live_passed, live_total = _run_live_suite()
    total = unit_total + live_total
    passed = unit_passed + live_passed
    print(f"\nUnit: {unit_passed}/{unit_total} passed")
    print(f"Live: {live_passed}/{live_total} passed (skipped OK if no network)")
    print(f"Done: {passed}/{total} passed")
    return 0 if unit_passed == unit_total else 1


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
