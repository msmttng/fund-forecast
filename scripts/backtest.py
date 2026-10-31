import argparse
import csv
import datetime
import io
import json
import pathlib
import re
import sys
import time
import zoneinfo
from typing import Any, Dict, List, Optional, Tuple

import jpholiday
import requests

JST = datetime.timezone(datetime.timedelta(hours=9))
UTC = datetime.timezone.utc

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
FUNDS_JSON_PATH = ROOT_DIR / "funds.json"
DOCS_DIR = ROOT_DIR / "docs"
FORECAST_LOG_PATH = DOCS_DIR / "history" / "forecast_log.json"
CACHE_DIR = ROOT_DIR / ".backtest_cache"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

YAHOO_FX_BID_TO_MID_SPREAD = 0.05


def resolve_tz(tz_name: Optional[str], gmtoffset: Optional[int] = None) -> datetime.tzinfo:
    if tz_name in ("Asia/Tokyo", "JST"):
        return JST
    if tz_name:
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            pass
    if gmtoffset is not None:
        return datetime.timezone(datetime.timedelta(seconds=int(gmtoffset)))
    fallback_hours = {
        "America/New_York": -4,
        "America/Chicago": -5,
        "Europe/London": 0,
    }
    if tz_name in fallback_hours:
        return datetime.timezone(datetime.timedelta(hours=fallback_hours[tz_name]))
    return UTC


def get_bar_local_date(ts: int, tz_name: Optional[str], gmtoffset: Optional[int] = None) -> str:
    tz = resolve_tz(tz_name, gmtoffset)
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def is_jp_business_day(d: datetime.date) -> bool:
    if d.weekday() >= 5:
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def parse_jp_date(date_str: str) -> Optional[str]:
    s = date_str.strip()
    try:
        if "年" in s and "月" in s and "日" in s:
            return datetime.datetime.strptime(s, "%Y年%m月%d日").strftime("%Y-%m-%d")
        if "/" in s:
            return datetime.datetime.strptime(s, "%Y/%m/%d").strftime("%Y-%m-%d")
        if "-" in s and len(s) == 10:
            return s
    except ValueError:
        return None
    return None


def load_or_fetch_nav_history(fund: Dict[str, Any]) -> Dict[str, int]:
    """投信総合検索ライブラリーから全期間の基準価額履歴を取得（キャッシュ利用）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fid = fund["id"]
    cpath = CACHE_DIR / f"nav_{fid}.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={fund['isinCd']}&associFundCd={fund['associFundCd']}"
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
    resp.raise_for_status()
    text = resp.content.decode("cp932", errors="replace")
    reader = csv.reader(io.StringIO(text))
    hist: Dict[str, int] = {}
    for row in reader:
        if len(row) < 2:
            continue
        d_str = parse_jp_date(row[0])
        if not d_str:
            continue
        try:
            nav_val = int(float(row[1].replace(",", "").strip()))
        except ValueError:
            continue
        hist[d_str] = nav_val

    cpath.write_text(json.dumps(hist, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    time.sleep(1.1)
    return hist


def load_or_fetch_yahoo(symbol: str, range_str: str, interval: str, events: bool = False) -> Dict[str, Any]:
    """Yahoo Finance チャートを取得（キャッシュ利用）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
    ev_tag = "_div" if events else ""
    cpath = CACHE_DIR / f"yahoo_{safe_sym}_{range_str}_{interval}{ev_tag}.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}"
    if events:
        url += "&events=div"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    res = resp.json()["chart"]["result"][0]
    cpath.write_text(json.dumps(res, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    time.sleep(1.1)
    return res


def load_or_fetch_murc_ttm(dates: List[str]) -> Dict[str, float]:
    """MURC 外国為替相場から指定営業日の USD/JPY 公示仲値 (TTM) を取得（キャッシュ利用）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / "murc_ttm.json"
    ttm_map: Dict[str, float] = {}
    if cpath.exists():
        ttm_map = json.loads(cpath.read_text(encoding="utf-8"))

    missing = [d for d in sorted(set(dates)) if d not in ttm_map]
    updated = False
    for d_str in missing:
        ymd = d_str.replace("-", "")[2:]  # YYMMDD
        url = f"https://www.murc-kawasesouba.jp/fx/past/index.php?id={ymd}"
        try:
            resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
            if resp.status_code == 200:
                txt = resp.content.decode(resp.apparent_encoding or "cp932", errors="replace")
                m = re.search(r"USD</td>\s*<td[^>]*>\s*([0-9.]+)\s*</td>\s*<td[^>]*>\s*([0-9.]+)", txt)
                if m:
                    tts = float(m.group(1))
                    ttb = float(m.group(2))
                    ttm_map[d_str] = round((tts + ttb) / 2.0, 4)
                    updated = True
        except Exception as e:
            print(f"[WARN] MURC TTM fetch failed for {d_str}: {e}", file=sys.stderr)
        time.sleep(1.1)

    if updated or not cpath.exists():
        cpath.write_text(json.dumps(dict(sorted(ttm_map.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return ttm_map


def load_or_fetch_topix_daily() -> Dict[str, float]:
    """株探の TOPIX 時系列ページ（JPX 公式終値と一致）から直近約120営業日分の終値を取得する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / "topix_daily.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    topix_map: Dict[str, float] = {}
    for page in range(1, 6):
        url = f"https://kabutan.jp/stock/kabuka?code=0010&ashi=day&page={page}"
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
        resp.raise_for_status()
        txt = resp.content.decode("utf-8", errors="replace")
        for m in re.finditer(
            r"<time datetime=[\"'](\d{4}-\d{2}-\d{2})[\"']>.*?</time>.*?</tr>",
            txt,
            re.S,
        ):
            d_str = m.group(1)
            tds = re.findall(r"<td[^>]*>([^<]+)</td>", m.group(0))
            # 始値, 高値, 安値, 終値
            if len(tds) >= 4:
                try:
                    close_val = float(tds[3].replace(",", "").strip())
                    topix_map[d_str] = close_val
                except ValueError:
                    pass
        time.sleep(1.1)

    cpath.write_text(json.dumps(dict(sorted(topix_map.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return topix_map


def load_or_fetch_msci(index_code: str, start_ymd: str = "20260501", end_ymd: str = "20261031") -> Dict[str, float]:
    """MSCI 公式 End-of-Day API から GRTR (USD) 指数終値を取得する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / f"msci_{index_code}_GRTR.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    url = (
        "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
        f"?currency_symbol=USD&index_variant=GRTR&start_date={start_ymd}&end_date={end_ymd}"
        f"&data_frequency=DAILY&index_codes={index_code}"
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    levels = data.get("indexes", {}).get("INDEX_LEVELS", [])
    out: Dict[str, float] = {}
    for row in levels:
        cd = str(row["calc_date"])
        d_str = f"{cd[:4]}-{cd[4:6]}-{cd[6:8]}"
        out[d_str] = float(row["level_eod"])

    cpath.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    time.sleep(1.2)
    return out


def parse_daily_series(chart: Dict[str, Any]) -> Tuple[List[Tuple[str, float]], Dict[str, float]]:
    meta = chart["meta"]
    tz_name = meta.get("exchangeTimezoneName", "America/New_York")
    gmtoffset = meta.get("gmtoffset")
    ts_list = chart.get("timestamp", [])
    cl_list = chart["indicators"]["quote"][0]["close"]
    bars: List[Tuple[str, float]] = []
    for t, c in zip(ts_list, cl_list):
        if c is not None:
            bars.append((get_bar_local_date(int(t), tz_name, gmtoffset), float(c)))
    # 同一日付が重複した場合は後ろを優先
    dedup: Dict[str, float] = {}
    for d, c in bars:
        dedup[d] = c
    sorted_bars = sorted(dedup.items())

    divs_map: Dict[str, float] = {}
    divs = chart.get("events", {}).get("dividends", {})
    for _, div_obj in divs.items():
        div_ts = int(div_obj.get("date", 0))
        div_date = get_bar_local_date(div_ts, tz_name, gmtoffset)
        divs_map[div_date] = divs_map.get(div_date, 0.0) + float(div_obj.get("amount", 0.0))
    return sorted_bars, divs_map


def parse_hourly_bars(chart: Dict[str, Any]) -> List[Tuple[int, float, float]]:
    """1時間足の (timestamp, open, close) リストを返す。"""
    ts_list = chart.get("timestamp", [])
    q = chart["indicators"]["quote"][0]
    op_list = q.get("open", [])
    cl_list = q.get("close", [])
    out: List[Tuple[int, float, float]] = []
    for t, o, c in zip(ts_list, op_list, cl_list):
        if c is not None:
            o_val = float(o) if o is not None else float(c)
            out.append((int(t), o_val, float(c)))
    return out


def find_hourly_price_at(h_bars: List[Tuple[int, float, float]], target_dt_jst: datetime.datetime, use_open: bool = False) -> Optional[float]:
    """指定時刻（JST）以前で最も近い 1 時間足の値を返す。"""
    target_ts = int(target_dt_jst.timestamp())
    if not h_bars:
        return None
    # target_ts 以下のバーを探す
    candidates = [b for b in h_bars if b[0] <= target_ts]
    if not candidates:
        return None
    best = candidates[-1]
    if abs(target_ts - best[0]) > 3600 * 12:
        return None
    # 1時間足で 08:30 JST を再現する場合：08:00 JST 開始足 (best[0] == 08:00) があれば open（08:00時点）または close（09:00時点だが8:30未来値混入を避けるため open）を使う
    if use_open:
        return best[1]
    return best[2]


def calc_rolling_beta(topix_map: Dict[str, float], n225_map: Dict[str, float], up_to_d0: str, lookback: int = 60) -> float:
    """up_to_d0 までの直近 lookback 営業日の TOPIX と日経平均の日次変化率から単回帰係数 β を計算する。"""
    common_dates = sorted(d for d in topix_map.keys() if d in n225_map and d <= up_to_d0)
    if len(common_dates) < 15:
        return 1.0
    recent_dates = common_dates[-(lookback + 1):]
    r_t: List[float] = []
    r_n: List[float] = []
    for i in range(1, len(recent_dates)):
        d_prev, d_cur = recent_dates[i - 1], recent_dates[i]
        t0, t1 = topix_map[d_prev], topix_map[d_cur]
        n0, n1 = n225_map[d_prev], n225_map[d_cur]
        if t0 > 0 and n0 > 0:
            r_t.append((t1 - t0) / t0)
            r_n.append((n1 - n0) / n0)
    if len(r_n) < 10:
        return 1.0
    mean_t = sum(r_t) / len(r_t)
    mean_n = sum(r_n) / len(r_n)
    cov = sum((rt - mean_t) * (rn - mean_n) for rt, rn in zip(r_t, r_n))
    var_n = sum((rn - mean_n) ** 2 for rn in r_n)
    if var_n <= 1e-12:
        return 1.0
    return cov / var_n


def get_series_change_between(
    bars: List[Tuple[str, float]],
    divs_map: Dict[str, float],
    d0_str: str,
    d1_str: str,
    adjust_div: bool = False,
) -> Tuple[float, str, float, str, float]:
    """D0 より前の最終終値と D1 より前の最終終値から (r_idx, base_date, base_val, cur_date, cur_val) を返す。"""
    before_d0 = [(d, c) for d, c in bars if d < d0_str]
    before_d1 = [(d, c) for d, c in bars if d < d1_str]
    if not before_d0 or not before_d1:
        return 0.0, d0_str, 0.0, d0_str, 0.0
    base_date, base_val = before_d0[-1]
    cur_date, cur_val = before_d1[-1]
    if cur_date <= base_date or base_val <= 0:
        return 0.0, base_date, base_val, base_date, base_val
    div_adj = 0.0
    if adjust_div:
        for dd, amt in divs_map.items():
            if base_date < dd <= cur_date:
                div_adj += amt
    r_idx = ((cur_val + div_adj) - base_val) / base_val
    return r_idx, base_date, base_val, cur_date, cur_val


def simulate_fund_day(
    fund: Dict[str, Any],
    group_id: str,
    d0_str: str,
    d1_str: str,
    nav0: int,
    mode: str,
    daily_series: Dict[str, Tuple[List[Tuple[str, float]], Dict[str, float]]],
    hourly_series: Dict[str, List[Tuple[int, float, float]]],
    ttm_map: Dict[str, float],
    topix_map: Dict[str, float],
    n225_map: Dict[str, float],
    msci_acwi_map: Dict[str, float],
    msci_kokusai_map: Dict[str, float],
    auam_formula: str = "additive",
    gold_plus_formula: str = "additive",
) -> Optional[int]:
    """指定日の朝 8:30 JST 時点で得られたデータから予想基準価額を計算する。"""
    d0_9am = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=9, minute=0, tzinfo=JST)
    d0_15pm = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=15, minute=0, tzinfo=JST)
    d1_8am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=8, minute=0, tzinfo=JST)

    # 1. 為替変化率 r_fx（D1 朝 8:30 JST 時点の現在値は 08:00 足 open）
    jpy_h = hourly_series.get("JPY=X", [])
    jpy_d_bars, _ = daily_series["JPY=X"]
    fx_cur = find_hourly_price_at(jpy_h, d1_8am, use_open=True)
    if fx_cur is None:
        b_d1 = [c for d, c in jpy_d_bars if d <= d1_str]
        fx_cur = b_d1[-1] if b_d1 else 150.0

    if mode == "before":
        # 改善前：D0 10:00 JST（09:00 開始 1h 足の close）
        fx_base = find_hourly_price_at(jpy_h, d0_9am, use_open=False)
        if fx_base is None:
            b_d0 = [c for d, c in jpy_d_bars if d <= d0_str]
            fx_base = b_d0[-1] if b_d0 else fx_cur
    else:
        # 改善後：D0 の公示仲値 (TTM: Mid) を優先し、Yahoo Bid 現在値には半スプレッド補正 (+0.05円) を適用
        fx_base = ttm_map.get(d0_str)
        if fx_base is not None:
            fx_cur = fx_cur + YAHOO_FX_BID_TO_MID_SPREAD
        else:
            fx_base = find_hourly_price_at(jpy_h, d0_9am, use_open=False)
            if fx_base is None:
                b_d0 = [c for d, c in jpy_d_bars if d <= d0_str]
                fx_base = b_d0[-1] if b_d0 else fx_cur

    r_fx = (fx_cur - fx_base) / fx_base if fx_base else 0.0

    model = fund["model"]
    sym = fund["symbol"]
    lev = float(fund.get("leverage", 1))

    # 2. モデル別計算
    if model in ("standard", "standard_div", "msci_acwi", "msci_kokusai"):
        # 改善後かつ MSCI 対象ファンド（ACWI / TOK または model 指定）の場合は MSCI 指数を使用
        use_msci_map: Optional[Dict[str, float]] = None
        etf_sym = sym
        if mode == "after":
            if sym == "ACWI" or model == "msci_acwi":
                use_msci_map = msci_acwi_map
                etf_sym = "ACWI"
            elif sym == "TOK" or model == "msci_kokusai":
                use_msci_map = msci_kokusai_map
                etf_sym = "TOK"

        if use_msci_map is not None:
            msci_bars = sorted(use_msci_map.items())
            r_msci, b_date, _, c_date, _ = get_series_change_between(msci_bars, {}, d0_str, d1_str, adjust_div=False)
            etf_bars, etf_divs = daily_series[etf_sym]
            _, _, _, etf_c_date, _ = get_series_change_between(etf_bars, etf_divs, d0_str, d1_str, adjust_div=True)
            if etf_c_date > c_date:
                # 未公表分を ETF で補完
                r_rem, _, _, _, _ = get_series_change_between(etf_bars, etf_divs, c_date + "z", d1_str, adjust_div=True)
                r_idx = (1.0 + r_msci) * (1.0 + r_rem) - 1.0
            else:
                r_idx = r_msci
        else:
            adj = (model == "standard_div") or (mode == "after" and sym in ("VOO", "VTI", "QQQ", "VT", "SCHD"))
            bars, divs = daily_series[sym]
            r_idx, _, _, _, _ = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=adj)

        r_total = (1.0 + r_idx) * (1.0 + r_fx) - 1.0

    elif model == "leveraged_hedged":
        bars, divs = daily_series[sym]
        r_idx, b_date, b_val, c_date, c_val = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=False)
        if mode == "before" and sym == "NQ=F" and b_val > 0 and c_date > b_date:
            # 改善前：朝 8:30 JST 時点では翌営業日 Globex の regularMarketPrice を参照していた
            nq_830 = find_hourly_price_at(hourly_series.get("NQ=F", []), d1_8am, use_open=True)
            if nq_830 is not None:
                r_idx = (nq_830 - b_val) / b_val
        r_total = lev * r_idx

    elif model in ("additive", "leveraged_unhedged"):
        bars, divs = daily_series[sym]
        r_idx, b_date, b_val, c_date, c_val = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=False)
        if mode == "before" and sym == "NQ=F" and b_val > 0 and c_date > b_date:
            nq_830 = find_hourly_price_at(hourly_series.get("NQ=F", []), d1_8am, use_open=True)
            if nq_830 is not None:
                r_idx = (nq_830 - b_val) / b_val
        if mode == "before" or auam_formula == "additive":
            r_total = lev * r_idx + r_fx
        else:
            r_total = (1.0 + lev * r_idx) * (1.0 + r_fx) - 1.0

    elif model in ("domestic", "domestic_leveraged"):
        # 朝 8:30 JST 時点では東証開場前のため、夜間先物変化率を使用
        niy_h = hourly_series.get("NIY=F", [])
        f_base = find_hourly_price_at(niy_h, d0_15pm, use_open=False)
        f_cur = find_hourly_price_at(niy_h, d1_8am, use_open=True)
        r_fut = (f_cur - f_base) / f_base if (f_base and f_cur and f_base > 0) else 0.0

        is_topix = (group_id == "topix" or sym in ("1306.T", "^TOPX", "TOPIX"))
        if mode == "after" and is_topix:
            beta = calc_rolling_beta(topix_map, n225_map, d0_str, lookback=60)
            r_idx = beta * r_fut
        else:
            r_idx = r_fut
        r_total = lev * r_idx if model == "domestic_leveraged" else r_idx

    elif model == "domestic_close":
        # 朝 8:30 JST 時点では東証開場前のため変化率 0
        r_total = 0.0

    elif model in ("gold_plus", "topix_gold_plus"):
        gold_sym = fund.get("goldSymbol", "GC=F")
        g_bars, g_divs = daily_series[gold_sym]
        r_gold, gb_date, gb_val, gc_date, _ = get_series_change_between(g_bars, g_divs, d0_str, d1_str, adjust_div=False)

        if mode == "before" and gb_val > 0 and gc_date > gb_date:
            # 改善前：朝 8:30 JST の Globex 価格を参照していた
            gc_830 = find_hourly_price_at(hourly_series.get("GC=F", []), d1_8am, use_open=True)
            if gc_830 is not None:
                r_gold = (gc_830 - gb_val) / gb_val

        if model == "topix_gold_plus" or sym in ("TOPIX", "^TOPX"):
            niy_h = hourly_series.get("NIY=F", [])
            f_base = find_hourly_price_at(niy_h, d0_15pm, use_open=False)
            f_cur = find_hourly_price_at(niy_h, d1_8am, use_open=True)
            r_fut = (f_cur - f_base) / f_base if (f_base and f_cur and f_base > 0) else 0.0
            beta = calc_rolling_beta(topix_map, n225_map, d0_str, lookback=60)
            r_stock_jpy = beta * r_fut
            r_total = r_stock_jpy + r_gold
        else:
            if mode == "after" and sym == "ACWI":
                msci_bars = sorted(msci_acwi_map.items())
                r_stock, _, _, _, _ = get_series_change_between(msci_bars, {}, d0_str, d1_str, adjust_div=False)
            else:
                s_bars, s_divs = daily_series[sym]
                r_stock, _, _, _, _ = get_series_change_between(s_bars, s_divs, d0_str, d1_str, adjust_div=False)

            if mode == "before" or gold_plus_formula == "additive":
                r_total = r_stock + r_fx + r_gold
            else:
                r_total = ((1.0 + r_stock) * (1.0 + r_fx) - 1.0) + r_gold

    else:
        return None

    return int(round(nav0 * (1.0 + r_total)))


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest fund-forecast for recent 60 business days")
    parser.add_argument("--days", type=int, default=60, help="検証する直近営業日数（デフォルト: 60）")
    parser.add_argument("--seed-log", choices=["none", "before", "after"], default="none", help="forecast_log.json にバックテスト結果を初期投入する")
    parser.add_argument("--auam-formula", choices=["additive", "multiplicative"], default="additive", help="改善後の auAM 計算式")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    funds_cfg = json.loads(FUNDS_JSON_PATH.read_text(encoding="utf-8"))

    # 1. 全ファンドの基準価額履歴を取得
    nav_histories: Dict[str, Dict[str, int]] = {}
    all_funds: List[Tuple[str, str, Dict[str, Any]]] = []
    for g in funds_cfg["groups"]:
        for f in g["funds"]:
            nav_histories[f["id"]] = load_or_fetch_nav_history(f)
            all_funds.append((g["id"], g["label"], f))

    # 代表ファンド (emaxis-slim-sp500) の日付列から直近 N 営業日の (D0, D1) ペアを作成
    ref_dates = sorted(nav_histories["emaxis-slim-sp500"].keys())
    if len(ref_dates) < args.days + 1:
        raise RuntimeError("基準価額履歴の日数が不足しています")
    target_window = ref_dates[-(args.days + 1):]
    day_pairs: List[Tuple[str, str]] = [
        (target_window[i], target_window[i + 1]) for i in range(len(target_window) - 1)
    ]

    all_d0_dates = [d0 for d0, _ in day_pairs]

    # 2. 市場データの取得
    yahoo_symbols = {"JPY=X", "NIY=F", "^N225", "1306.T", "1540.T", "^GSPC", "^NDX", "NQ=F", "ACWI", "TOK", "SCHD", "VTI", "IGLN.L", "GC=F"}
    for _, _, f in all_funds:
        if f.get("symbol") and not str(f["symbol"]).startswith("MSCI:") and f["symbol"] != "TOPIX":
            yahoo_symbols.add(f["symbol"])
        if f.get("goldSymbol"):
            yahoo_symbols.add(f["goldSymbol"])

    div_syms = {"SCHD", "VTI", "VOO", "QQQ", "VT", "ACWI", "TOK", "GLD", "IAU", "GLDM"}
    daily_series: Dict[str, Tuple[List[Tuple[str, float]], Dict[str, float]]] = {}
    for sym in sorted(yahoo_symbols):
        ch = load_or_fetch_yahoo(sym, range_str="6mo", interval="1d", events=(sym in div_syms))
        daily_series[sym] = parse_daily_series(ch)

    hourly_series: Dict[str, List[Tuple[int, float, float]]] = {}
    for sym in ["JPY=X", "NIY=F", "NQ=F", "GC=F"]:
        ch_h = load_or_fetch_yahoo(sym, range_str="6mo", interval="1h", events=False)
        hourly_series[sym] = parse_hourly_bars(ch_h)

    ttm_map = load_or_fetch_murc_ttm(all_d0_dates)
    topix_map = load_or_fetch_topix_daily()
    n225_map = dict(daily_series["^N225"][0])
    msci_acwi_map = load_or_fetch_msci("892400")
    msci_kokusai_map = load_or_fetch_msci("991200")

    # 3. auAM 2倍・3倍の加算型 vs 掛け算型 比較（改善後データ条件で比較）
    print(f"=== バックテスト期間: {day_pairs[0][1]} 〜 {day_pairs[-1][1]} ({len(day_pairs)} 営業日) ===\n")
    print("=== auAM レバレッジ（為替ヘッジ無し）加算型 vs 掛け算型 比較 ===")
    print("| ファンド | 加算型 `L*r + r_fx` 平均絶対誤差 | 掛け算型 `(1+L*r)(1+r_fx)-1` 平均絶対誤差 | 判定 |")
    print("|---|---:|---:|---|")
    auam_best_counts = {"additive": 0, "multiplicative": 0}
    for _, _, f in all_funds:
        if f["id"] in ("auam-triple-ndx", "auam-lev-ndx"):
            errs_add: List[float] = []
            errs_mul: List[float] = []
            h = nav_histories[f["id"]]
            for d0, d1 in day_pairs:
                if d0 not in h or d1 not in h:
                    continue
                nav0, act = h[d0], h[d1]
                fc_add = simulate_fund_day(
                    f, "nasdaq100", d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    auam_formula="additive",
                )
                fc_mul = simulate_fund_day(
                    f, "nasdaq100", d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    auam_formula="multiplicative",
                )
                if fc_add is not None and fc_mul is not None:
                    errs_add.append(abs((fc_add - act) / nav0 * 100.0))
                    errs_mul.append(abs((fc_mul - act) / nav0 * 100.0))
            mae_add = sum(errs_add) / len(errs_add) if errs_add else 0.0
            mae_mul = sum(errs_mul) / len(errs_mul) if errs_mul else 0.0
            better = "掛け算型" if mae_mul < mae_add else "加算型"
            if mae_mul < mae_add:
                auam_best_counts["multiplicative"] += 1
            else:
                auam_best_counts["additive"] += 1
            print(f"| {f['name']} ({len(errs_add)}日) | {mae_add:.4f}pt | {mae_mul:.4f}pt | {better} |")

    chosen_auam = args.auam_formula
    print(f"\n採用式（指定値）: {chosen_auam}\n")

    # 4. グループ別 改善前 vs 改善後 の平均絶対誤差（MAE）比較
    print("=== グループ別 平均絶対誤差（MAE, 単位: pt）改善前 vs 改善後 ===")
    print("| グループ | 本数 | サンプル数 | 改善前 MAE | 改善後 MAE | 改善幅 (後-前) | 改善前 Bias | 改善後 Bias |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")

    all_before_abs: List[float] = []
    all_after_abs: List[float] = []
    seed_records: Dict[str, Dict[str, Dict[str, Any]]] = {}

    for g in funds_cfg["groups"]:
        gid = g["id"]
        glabel = g["label"]
        g_before_abs: List[float] = []
        g_after_abs: List[float] = []
        g_before_sgn: List[float] = []
        g_after_sgn: List[float] = []

        for f in g["funds"]:
            fid = f["id"]
            h = nav_histories[fid]
            for d0, d1 in day_pairs:
                if d0 not in h or d1 not in h:
                    continue
                nav0, act = h[d0], h[d1]
                fc_bef = simulate_fund_day(
                    f, gid, d0, d1, nav0, "before",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    auam_formula="additive",
                )
                fc_aft = simulate_fund_day(
                    f, gid, d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    auam_formula=chosen_auam,
                )
                if fc_bef is None or fc_aft is None:
                    continue

                err_bef = (fc_bef - act) / nav0 * 100.0
                err_aft = (fc_aft - act) / nav0 * 100.0
                g_before_abs.append(abs(err_bef))
                g_after_abs.append(abs(err_aft))
                g_before_sgn.append(err_bef)
                g_after_sgn.append(err_aft)
                all_before_abs.append(abs(err_bef))
                all_after_abs.append(abs(err_aft))

                if args.seed_log in ("before", "after"):
                    fc_use = fc_bef if args.seed_log == "before" else fc_aft
                    err_use = err_bef if args.seed_log == "before" else err_aft
                    fc_chg = (fc_use - nav0) / nav0 * 100.0
                    act_chg = (act - nav0) / nav0 * 100.0
                    seed_records.setdefault(d1, {})[fid] = {
                        "baseDate": d0,
                        "baseNav": int(nav0),
                        "forecastNav": int(fc_use),
                        "forecastChange": round(fc_chg, 2),
                        "actualNav": int(act),
                        "actualChange": round(act_chg, 2),
                        "errorYen": int(fc_use - act),
                        "errorPt": round(err_use, 4),
                        "recordedAt": f"{d1}T08:30:00+09:00",
                        "source": "backtest",
                    }

        if g_before_abs:
            m_bef = sum(g_before_abs) / len(g_before_abs)
            m_aft = sum(g_after_abs) / len(g_after_abs)
            b_bef = sum(g_before_sgn) / len(g_before_sgn)
            b_aft = sum(g_after_sgn) / len(g_after_sgn)
            diff = m_aft - m_bef
            print(
                f"| {glabel} | {len(g['funds'])} | {len(g_before_abs)} | "
                f"{m_bef:.4f}pt | {m_aft:.4f}pt | {diff:+.4f}pt | {b_bef:+.4f}pt | {b_aft:+.4f}pt |"
            )

    if all_before_abs:
        tot_bef = sum(all_before_abs) / len(all_before_abs)
        tot_aft = sum(all_after_abs) / len(all_after_abs)
        print(
            f"| **全体** | **{len(all_funds)}** | **{len(all_before_abs)}** | "
            f"**{tot_bef:.4f}pt** | **{tot_aft:.4f}pt** | **{tot_aft - tot_bef:+.4f}pt** | ― | ― |"
        )

    # β の最新値も参考出力
    latest_d0 = day_pairs[-1][0]
    latest_beta = calc_rolling_beta(topix_map, n225_map, latest_d0, lookback=60)
    print(f"\n直近 60 営業日の TOPIX 対 日経平均 回帰係数 β ({latest_d0} 時点): {latest_beta:.4f}")

    if args.seed_log != "none":
        FORECAST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing_log: Dict[str, Any] = {"version": 1, "entries": {}}
        if FORECAST_LOG_PATH.exists():
            try:
                existing_log = json.loads(FORECAST_LOG_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        entries = existing_log.setdefault("entries", {})
        for d1_str, day_map in seed_records.items():
            bucket = entries.setdefault(d1_str, {})
            for fid, rec in day_map.items():
                # 既に live で記録されたものがあれば上書きしない
                if bucket.get(fid, {}).get("source") == "live":
                    continue
                bucket[fid] = rec
        existing_log["entries"] = {k: entries[k] for k in sorted(entries.keys())}
        existing_log["updatedAt"] = datetime.datetime.now(JST).isoformat(timespec="seconds")
        FORECAST_LOG_PATH.write_text(
            json.dumps(existing_log, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        print(f"[OK] Seeded {FORECAST_LOG_PATH} (mode={args.seed_log}, days={len(seed_records)})")


if __name__ == "__main__":
    main()
