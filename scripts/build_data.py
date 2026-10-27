import argparse
import csv
import datetime
import io
import json
import pathlib
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
DATA_JSON_PATH = DOCS_DIR / "data.json"
NAV_CACHE_PATH = DOCS_DIR / "nav_cache.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


def is_jp_business_day(d: datetime.date) -> bool:
    """日本の営業日（土日・祝日・12/31〜1/3を除く）かどうかを判定する。"""
    if d.weekday() >= 5:
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    if jpholiday.is_holiday(d):
        return False
    return True


def next_jp_business_day(d: datetime.date) -> datetime.date:
    """指定日の翌営業日を返す。"""
    cur = d + datetime.timedelta(days=1)
    while not is_jp_business_day(cur):
        cur += datetime.timedelta(days=1)
    return cur


def latest_expected_nav_date(now_jst: datetime.datetime) -> datetime.date:
    """現在時刻（JST）時点で期待される最新の基準価額日付を返す。"""
    d = now_jst.date()
    if now_jst.hour < 18 or not is_jp_business_day(d):
        cur = d - datetime.timedelta(days=1)
        while not is_jp_business_day(cur):
            cur -= datetime.timedelta(days=1)
        return cur
    return d


def should_fetch_nav_window(now_jst: datetime.datetime) -> bool:
    """基準価額CSVを取得する時間帯（JST 18:00〜23:59 または 6:00〜9:00）か判定する。"""
    return (18 <= now_jst.hour <= 23) or (6 <= now_jst.hour < 9)


def parse_jp_date(date_str: str) -> Optional[str]:
    """'2026年10月27日' または '2026/10/27' を '2026-10-27' に変換する。"""
    s = date_str.strip()
    try:
        if "年" in s and "月" in s and "日" in s:
            dt = datetime.datetime.strptime(s, "%Y年%m月%d日")
            return dt.strftime("%Y-%m-%d")
        if "/" in s:
            dt = datetime.datetime.strptime(s, "%Y/%m/%d")
            return dt.strftime("%Y-%m-%d")
        if "-" in s and len(s) == 10:
            return s
    except ValueError:
        return None
    return None


def fetch_fund_nav_csv(isin_cd: str, associ_fund_cd: str) -> Tuple[int, str, List[List[Any]]]:
    """投信総合検索ライブラリーから基準価額CSVを取得し、(最新価額, 最新日付, 直近30日履歴) を返す。"""
    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin_cd}&associFundCd={associ_fund_cd}"
    )
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=20)
    resp.raise_for_status()
    text = resp.content.decode("cp932", errors="replace")
    reader = csv.reader(io.StringIO(text))
    history: List[List[Any]] = []
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
        history.append([d_str, nav_val])
    if not history:
        raise ValueError(f"CSVに有効な基準価額行がありません: {isin_cd}")
    history.sort(key=lambda x: x[0])
    recent_30 = history[-30:]
    latest_date, latest_nav = recent_30[-1][0], recent_30[-1][1]
    return latest_nav, latest_date, recent_30


def fetch_yahoo_chart(symbol: str, range_str: str = "10d", interval: str = "1d", events: bool = False) -> Dict[str, Any]:
    """Yahoo Finance チャート API からデータを取得する。"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_str}&interval={interval}"
    if events:
        url += "&events=div"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    res = data.get("chart", {}).get("result")
    if not res:
        raise ValueError(f"Yahoo Finance から結果が返りませんでした: {symbol}")
    return res[0]


def get_bar_local_date(ts: int, tz_name: Optional[str]) -> str:
    """タイムスタンプを取引所の現地日付 ('YYYY-MM-DD') に変換する。"""
    try:
        tz = zoneinfo.ZoneInfo(tz_name) if tz_name else UTC
    except Exception:
        tz = UTC
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


def build_market_cache(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """必要な全シンボルの日足・15分足データを一括取得する。"""
    cache: Dict[str, Dict[str, Any]] = {}
    intraday_symbols = {"JPY=X", "NIY=F", "^N225", "1306.T", "1540.T"}
    div_symbols = {"SCHD", "VTI"}

    for sym in sorted(set(symbols)):
        entry: Dict[str, Any] = {}
        entry["daily"] = fetch_yahoo_chart(sym, range_str="10d", interval="1d", events=(sym in div_symbols))
        if sym in intraday_symbols:
            try:
                entry["m15"] = fetch_yahoo_chart(sym, range_str="5d", interval="15m", events=False)
            except Exception as e:
                print(f"[WARN] 15m chart fetch failed for {sym}: {e}", file=sys.stderr)
        cache[sym] = entry
        time.sleep(0.15)
    return cache


def get_fx_data(market_cache: Dict[str, Dict[str, Any]], d0_str: str, d1_str: str, now_jst: datetime.datetime) -> Dict[str, Any]:
    """ドル円 (JPY=X) の起点（D0 10:00 JST）と現在値（D1 10:00 JST 以降は D1 10:00 JST、それ以前は最新値）を計算する。"""
    jpy_entry = market_cache["JPY=X"]
    daily = jpy_entry["daily"]
    meta = daily["meta"]
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()

    m15 = jpy_entry.get("m15")
    base_val: Optional[float] = None
    cur_val: float = reg_price
    estimated = True

    d0_10am = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(
        hour=10, minute=0, second=0, tzinfo=JST
    )
    d1_10am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(
        hour=10, minute=0, second=0, tzinfo=JST
    )

    if m15 and "timestamp" in m15:
        ts_list = m15["timestamp"]
        cl_list = m15["indicators"]["quote"][0]["close"]
        bars = [(int(t), float(c)) for t, c in zip(ts_list, cl_list) if c is not None]
        if bars:
            # 15分足の 09:45 JST (10:00終了足) または 10:00 JST (01:00 UTC) 付近の close
            # 09:45 JST のタイムスタンプを優先的に探し、なければ 10:00 JST に最も近い足を取る
            d0_target_ts = int((d0_10am - datetime.timedelta(minutes=15)).timestamp())
            best_d0 = min(bars, key=lambda x: abs(x[0] - d0_target_ts))
            if abs(best_d0[0] - d0_target_ts) <= 3600 * 6:
                base_val = best_d0[1]

            if now_jst >= d1_10am:
                d1_target_ts = int((d1_10am - datetime.timedelta(minutes=15)).timestamp())
                best_d1 = min(bars, key=lambda x: abs(x[0] - d1_target_ts))
                if abs(best_d1[0] - d1_target_ts) <= 3600 * 2:
                    cur_val = best_d1[1]
                    estimated = False

    if base_val is None:
        # フォールバック：日足から取得
        ts_list = daily.get("timestamp", [])
        cl_list = daily["indicators"]["quote"][0]["close"]
        valid = [(get_bar_local_date(t, "Asia/Tokyo"), float(c)) for t, c in zip(ts_list, cl_list) if c is not None]
        before_d0 = [c for d, c in valid if d <= d0_str]
        base_val = before_d0[-1] if before_d0 else reg_price

    r_fx = (cur_val - base_val) / base_val if base_val else 0.0
    return {
        "symbol": "JPY=X",
        "base": round(base_val, 2),
        "cur": round(cur_val, 2),
        "rawChange": r_fx,
        "change": round(r_fx * 100.0, 2),
        "estimated": estimated,
        "asOf": as_of_iso,
    }


def get_overseas_index_change(
    market_cache: Dict[str, Dict[str, Any]],
    symbol: str,
    d0_str: str,
    adjust_div: bool = False,
) -> Dict[str, Any]:
    """海外指数・ETF・先物の D0 より前の最終終値 (base) と最新値 (cur) から変化率を計算する。"""
    entry = market_cache[symbol]
    daily = entry["daily"]
    meta = daily["meta"]
    tz_name = meta.get("exchangeTimezoneName", "America/New_York")
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(time.time())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()

    ts_list = daily.get("timestamp", [])
    cl_list = daily["indicators"]["quote"][0]["close"]
    bars: List[Tuple[str, float]] = []
    for t, c in zip(ts_list, cl_list):
        if c is not None:
            bars.append((get_bar_local_date(int(t), tz_name), float(c)))

    before_d0 = [(d, c) for d, c in bars if d < d0_str]
    if not before_d0:
        base_date, base_val = (bars[-1] if bars else (d0_str, reg_price))
    else:
        base_date, base_val = before_d0[-1]

    latest_bar_date = bars[-1][0] if bars else base_date
    reg_local_date = get_bar_local_date(reg_time, tz_name)
    cur_date = max(latest_bar_date, reg_local_date)

    # 休場等で D0 より前の取引日から進んでいない場合は未反映変化なし (r_idx = 0)
    if cur_date <= base_date:
        cur_val = base_val
        div_adj = 0.0
        r_idx = 0.0
    else:
        cur_val = reg_price
        div_adj = 0.0
        if adjust_div:
            divs = daily.get("events", {}).get("dividends", {})
            for _, div_obj in divs.items():
                div_ts = int(div_obj.get("date", 0))
                div_date = get_bar_local_date(div_ts, tz_name)
                if base_date < div_date <= cur_date:
                    div_adj += float(div_obj.get("amount", 0.0))
        r_idx = ((cur_val + div_adj) - base_val) / base_val if base_val else 0.0

    res: Dict[str, Any] = {
        "symbol": symbol,
        "base": round(base_val, 2),
        "baseDate": base_date,
        "cur": round(cur_val, 2),
        "curDate": cur_date,
        "rawChange": r_idx,
        "change": round(r_idx * 100.0, 2),
        "asOf": as_of_iso,
    }
    if div_adj > 0:
        res["dividendAdjusted"] = round(div_adj, 4)
    return res


def get_domestic_index_change(
    market_cache: Dict[str, Dict[str, Any]],
    symbol: str,
    d0_str: str,
    now_jst: datetime.datetime,
    use_futures: bool = True,
    futures_symbol: str = "NIY=F",
) -> Dict[str, Any]:
    """国内指数・ETFの D0 当日終値 (base) と現在値（時間外は先物変化率で近似）から変化率を計算する。"""
    entry = market_cache[symbol]
    daily = entry["daily"]
    meta = daily["meta"]
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()

    ts_list = daily.get("timestamp", [])
    cl_list = daily["indicators"]["quote"][0]["close"]
    bars_by_date: Dict[str, Optional[float]] = {}
    for t, c in zip(ts_list, cl_list):
        d_local = get_bar_local_date(int(t), "Asia/Tokyo")
        bars_by_date[d_local] = float(c) if c is not None else None

    base_val = bars_by_date.get(d0_str)
    # ^N225 の最新日足 close が null になる場合の対応
    if base_val is None:
        m15 = entry.get("m15")
        if m15 and "timestamp" in m15:
            m15_ts = m15["timestamp"]
            m15_cl = m15["indicators"]["quote"][0]["close"]
            d0_closes = [
                float(c)
                for t, c in zip(m15_ts, m15_cl)
                if c is not None and get_bar_local_date(int(t), "Asia/Tokyo") == d0_str
            ]
            if d0_closes:
                base_val = d0_closes[-1]
        if base_val is None:
            base_val = reg_price

    # 平日（日本の営業日）9:00〜15:30 JST かどうか判定
    in_tokyo_hours = (
        is_jp_business_day(now_jst.date())
        and (
            (now_jst.hour > 9 or (now_jst.hour == 9 and now_jst.minute >= 0))
            and (now_jst.hour < 15 or (now_jst.hour == 15 and now_jst.minute <= 30))
        )
        and now_jst.strftime("%Y-%m-%d") > d0_str
    )

    cur_val = reg_price
    futures_info: Optional[Dict[str, Any]] = None

    if not use_futures:
        # 三菱UFJ 純金ファンド (1540.T) など当日終値ベース
        if now_jst.strftime("%Y-%m-%d") <= d0_str or (
            now_jst.strftime("%Y-%m-%d") > d0_str and now_jst.hour < 9
        ):
            cur_val = base_val
        else:
            cur_val = reg_price
    else:
        if in_tokyo_hours:
            cur_val = reg_price
        else:
            fut_entry = market_cache.get(futures_symbol)
            if fut_entry:
                fut_daily = fut_entry["daily"]
                fut_cur = float(fut_daily["meta"]["regularMarketPrice"])
                fut_time = int(fut_daily["meta"].get("regularMarketTime", reg_time))
                as_of_iso = datetime.datetime.fromtimestamp(fut_time, JST).isoformat()
                fut_m15 = fut_entry.get("m15")
                fut_1530: Optional[float] = None
                if fut_m15 and "timestamp" in fut_m15:
                    target_1530 = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(
                        hour=15, minute=30, second=0, tzinfo=JST
                    )
                    target_ts = int(target_1530.timestamp())
                    f_bars = [
                        (int(t), float(c))
                        for t, c in zip(fut_m15["timestamp"], fut_m15["indicators"]["quote"][0]["close"])
                        if c is not None
                    ]
                    if f_bars:
                        best_f = min(f_bars, key=lambda x: abs(x[0] - target_ts))
                        if abs(best_f[0] - target_ts) <= 3600 * 4:
                            fut_1530 = best_f[1]
                if fut_1530 and fut_1530 > 0:
                    cur_val = base_val * (fut_cur / fut_1530)
                    fut_r = (fut_cur - fut_1530) / fut_1530
                    futures_info = {
                        "symbol": futures_symbol,
                        "base": round(fut_1530, 2),
                        "cur": round(fut_cur, 2),
                        "change": round(fut_r * 100.0, 2),
                    }

    r_idx = (cur_val - base_val) / base_val if base_val else 0.0
    res: Dict[str, Any] = {
        "symbol": symbol,
        "base": round(base_val, 2),
        "baseDate": d0_str,
        "cur": round(cur_val, 2),
        "rawChange": r_idx,
        "change": round(r_idx * 100.0, 2),
        "asOf": as_of_iso,
    }
    if futures_info:
        res["futures"] = futures_info
    return res


def main() -> None:
    parser = argparse.ArgumentParser(description="Build docs/data.json for fund-forecast")
    parser.add_argument("--force-nav", action="store_true", help="時間帯に関わらず基準価額CSVを再取得する")
    args = parser.parse_args()

    now_jst = datetime.datetime.now(JST)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    funds_cfg = json.loads(FUNDS_JSON_PATH.read_text(encoding="utf-8"))

    nav_cache: Dict[str, Any] = {}
    if NAV_CACHE_PATH.exists():
        try:
            nav_cache = json.loads(NAV_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to load nav_cache.json: {e}", file=sys.stderr)

    prev_data: Dict[str, Any] = {}
    prev_funds_by_id: Dict[str, Dict[str, Any]] = {}
    if DATA_JSON_PATH.exists():
        try:
            prev_data = json.loads(DATA_JSON_PATH.read_text(encoding="utf-8"))
            for g in prev_data.get("groups", []):
                for f in g.get("funds", []):
                    if "id" in f:
                        prev_funds_by_id[f["id"]] = f
        except Exception:
            pass

    expected_nav_date_str = latest_expected_nav_date(now_jst).strftime("%Y-%m-%d")
    in_nav_window = should_fetch_nav_window(now_jst)

    # 1. 各ファンドの基準価額を取得（またはキャッシュ利用）
    all_funds: List[Dict[str, Any]] = []
    for g in funds_cfg.get("groups", []):
        for f in g.get("funds", []):
            all_funds.append(f)

    nav_updated = False
    for idx, f in enumerate(all_funds):
        fid = f["id"]
        cached = nav_cache.get(fid)
        need_fetch = (
            args.force_nav
            or cached is None
            or "nav" not in cached
            or (in_nav_window and cached.get("navDate") != expected_nav_date_str)
        )
        if need_fetch:
            try:
                latest_nav, latest_date, history = fetch_fund_nav_csv(f["isinCd"], f["associFundCd"])
                nav_cache[fid] = {
                    "nav": latest_nav,
                    "navDate": latest_date,
                    "history": history,
                    "fetchedAt": now_jst.isoformat(timespec="seconds"),
                }
                nav_updated = True
                print(f"[NAV] {fid}: {latest_nav} ({latest_date})")
            except Exception as e:
                print(f"[ERROR] NAV fetch failed for {fid}: {e}", file=sys.stderr)
            if idx < len(all_funds) - 1:
                time.sleep(1.0)

    if nav_updated or not NAV_CACHE_PATH.exists():
        NAV_CACHE_PATH.write_text(
            json.dumps(nav_cache, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # 代表の基準日 D0 を決定（最も多い最新基準価額日）
    nav_dates = [v["navDate"] for v in nav_cache.values() if "navDate" in v]
    rep_d0_str = max(set(nav_dates), key=nav_dates.count) if nav_dates else expected_nav_date_str
    rep_d0_date = datetime.datetime.strptime(rep_d0_str, "%Y-%m-%d").date()
    rep_d1_str = next_jp_business_day(rep_d0_date).strftime("%Y-%m-%d")

    # 2. 市場データの取得
    required_symbols = {
        "^GSPC", "^NDX", "NQ=F", "^SOX", "^NYFANG",
        "JPY=X", "^N225", "NIY=F", "1306.T",
        "ACWI", "TOK", "SCHD", "VTI",
        "IGLN.L", "1540.T", "GC=F",
    }
    for f in all_funds:
        if "symbol" in f:
            required_symbols.add(f["symbol"])
        if "futuresSymbol" in f:
            required_symbols.add(f["futuresSymbol"])
        if "goldSymbol" in f:
            required_symbols.add(f["goldSymbol"])

    market_cache = build_market_cache(list(required_symbols))

    # 代表 D0 に対する FX と市場サマリー
    rep_fx = get_fx_data(market_cache, rep_d0_str, rep_d1_str, now_jst)

    sp500_m = get_overseas_index_change(market_cache, "^GSPC", rep_d0_str)
    ndx_m = get_overseas_index_change(market_cache, "^NDX", rep_d0_str)
    nqf_m = get_overseas_index_change(market_cache, "NQ=F", rep_d0_str)
    n225_m = get_domestic_index_change(market_cache, "^N225", rep_d0_str, now_jst, use_futures=True, futures_symbol="NIY=F")
    sox_m = get_overseas_index_change(market_cache, "^SOX", rep_d0_str)
    fang_m = get_overseas_index_change(market_cache, "^NYFANG", rep_d0_str)
    acwi_m = get_overseas_index_change(market_cache, "ACWI", rep_d0_str)
    gold_m = get_overseas_index_change(market_cache, "GC=F", rep_d0_str)

    markets_summary = [
        {"id": "sp500", "label": "S&P500", "symbol": "^GSPC", "base": sp500_m["base"], "cur": sp500_m["cur"], "change": sp500_m["change"], "asOf": sp500_m["asOf"]},
        {"id": "ndx", "label": "NASDAQ100", "symbol": "^NDX", "base": ndx_m["base"], "cur": ndx_m["cur"], "change": ndx_m["change"], "asOf": ndx_m["asOf"]},
        {"id": "nqf", "label": "NASDAQ100先物", "symbol": "NQ=F", "base": nqf_m["base"], "cur": nqf_m["cur"], "change": nqf_m["change"], "asOf": nqf_m["asOf"]},
        {"id": "n225", "label": "日経平均(先物考慮)", "symbol": "^N225", "base": n225_m["base"], "cur": n225_m["cur"], "change": n225_m["change"], "asOf": n225_m["asOf"]},
        {"id": "usdjpy", "label": "ドル円", "symbol": "JPY=X", "base": rep_fx["base"], "cur": rep_fx["cur"], "change": rep_fx["change"], "asOf": rep_fx["asOf"]},
        {"id": "sox", "label": "SOX", "symbol": "^SOX", "base": sox_m["base"], "cur": sox_m["cur"], "change": sox_m["change"], "asOf": sox_m["asOf"]},
        {"id": "fang", "label": "FANG+", "symbol": "^NYFANG", "base": fang_m["base"], "cur": fang_m["cur"], "change": fang_m["change"], "asOf": fang_m["asOf"]},
        {"id": "acwi", "label": "ACWI(オルカン)", "symbol": "ACWI", "base": acwi_m["base"], "cur": acwi_m["cur"], "change": acwi_m["change"], "asOf": acwi_m["asOf"]},
        {"id": "gold", "label": "金先物", "symbol": "GC=F", "base": gold_m["base"], "cur": gold_m["cur"], "change": gold_m["change"], "asOf": gold_m["asOf"]},
    ]

    # 3. 各ファンドの予想基準価額を計算
    out_groups: List[Dict[str, Any]] = []
    for g in funds_cfg.get("groups", []):
        out_funds: List[Dict[str, Any]] = []
        for f in g.get("funds", []):
            fid = f["id"]
            try:
                c_nav = nav_cache.get(fid)
                if not c_nav:
                    raise RuntimeError("基準価額キャッシュがありません")
                nav0 = int(c_nav["nav"])
                d0_str = c_nav["navDate"]
                d0_date = datetime.datetime.strptime(d0_str, "%Y-%m-%d").date()
                d1_str = next_jp_business_day(d0_date).strftime("%Y-%m-%d")

                fx_info = rep_fx if d0_str == rep_d0_str else get_fx_data(market_cache, d0_str, d1_str, now_jst)
                r_fx = fx_info["rawChange"]

                model = f["model"]
                sym = f["symbol"]
                lev = float(f.get("leverage", 1))
                hedged = bool(f.get("hedged", False))

                breakdown: Dict[str, Any] = {}
                if model in ("standard", "standard_div"):
                    idx_info = get_overseas_index_change(market_cache, sym, d0_str, adjust_div=(model == "standard_div"))
                    r_idx = idx_info["rawChange"]
                    r_total = (1.0 + r_idx) * (1.0 + r_fx) - 1.0
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "fx": {
                            "base": fx_info["base"],
                            "cur": fx_info["cur"],
                            "change": fx_info["change"],
                        },
                    }
                    if "dividendAdjusted" in idx_info:
                        breakdown["index"]["dividendAdjusted"] = idx_info["dividendAdjusted"]

                elif model == "leveraged_hedged":
                    idx_info = get_overseas_index_change(market_cache, sym, d0_str)
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }

                elif model == "additive":
                    idx_info = get_overseas_index_change(market_cache, sym, d0_str)
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx + r_fx
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "fx": {
                            "base": fx_info["base"],
                            "cur": fx_info["cur"],
                            "change": fx_info["change"],
                        },
                    }

                elif model in ("domestic", "domestic_leveraged"):
                    fut_sym = f.get("futuresSymbol", "NIY=F")
                    idx_info = get_domestic_index_change(market_cache, sym, d0_str, now_jst, use_futures=True, futures_symbol=fut_sym)
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx if model == "domestic_leveraged" else r_idx
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }
                    if "futures" in idx_info:
                        breakdown["futures"] = idx_info["futures"]

                elif model == "domestic_close":
                    idx_info = get_domestic_index_change(market_cache, sym, d0_str, now_jst, use_futures=False)
                    r_idx = idx_info["rawChange"]
                    r_total = r_idx
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }

                elif model == "gold_plus":
                    idx_info = get_overseas_index_change(market_cache, sym, d0_str)
                    gold_sym = f.get("goldSymbol", "GC=F")
                    gold_info = get_overseas_index_change(market_cache, gold_sym, d0_str)
                    r_stock = idx_info["rawChange"]
                    r_gold = gold_info["rawChange"]
                    r_total = r_stock + r_fx + r_gold
                    breakdown = {
                        "index": {
                            "symbol": sym,
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "gold": {
                            "symbol": gold_sym,
                            "base": gold_info["base"],
                            "cur": gold_info["cur"],
                            "change": gold_info["change"],
                            "asOf": gold_info["asOf"],
                        },
                        "fx": {
                            "base": fx_info["base"],
                            "cur": fx_info["cur"],
                            "change": fx_info["change"],
                        },
                    }
                else:
                    raise ValueError(f"Unknown model: {model}")

                forecast_nav = int(round(nav0 * (1.0 + r_total)))
                fund_entry: Dict[str, Any] = {
                    "id": fid,
                    "name": f["name"],
                    "isinCd": f["isinCd"],
                    "associFundCd": f["associFundCd"],
                    "nav": nav0,
                    "navDate": d0_str,
                    "targetDate": d1_str,
                    "forecast": forecast_nav,
                    "diff": forecast_nav - nav0,
                    "change": round(r_total * 100.0, 2),
                    "leverage": f.get("leverage", 1),
                    "hedged": hedged,
                    "model": model,
                    "breakdown": breakdown,
                    "history": c_nav.get("history", []),
                }
                if "leverageLabel" in f:
                    fund_entry["leverageLabel"] = f["leverageLabel"]
                out_funds.append(fund_entry)

            except Exception as e:
                print(f"[ERROR] Calculation failed for {fid}: {e}", file=sys.stderr)
                prev_f = prev_funds_by_id.get(fid)
                if prev_f:
                    stale_f = dict(prev_f)
                    stale_f["stale"] = True
                    stale_f["error"] = str(e)
                    out_funds.append(stale_f)
                else:
                    out_funds.append({
                        "id": fid,
                        "name": f["name"],
                        "stale": True,
                        "error": str(e),
                    })

        out_groups.append({
            "id": g["id"],
            "label": g["label"],
            "funds": out_funds,
        })

    output_data = {
        "updatedAt": now_jst.isoformat(timespec="seconds"),
        "baseDate": rep_d0_str,
        "targetDate": rep_d1_str,
        "markets": markets_summary,
        "fx": {
            "base": rep_fx["base"],
            "cur": rep_fx["cur"],
            "change": rep_fx["change"],
            "estimated": rep_fx["estimated"],
            "asOf": rep_fx["asOf"],
        },
        "groups": out_groups,
    }

    DATA_JSON_PATH.write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[OK] Wrote {DATA_JSON_PATH} (targetDate={rep_d1_str})")


if __name__ == "__main__":
    main()
