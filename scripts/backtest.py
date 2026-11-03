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


def _easter_sunday(year: int) -> datetime.date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l_val = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_val) // 451
    month = (h + l_val - 7 * m + 114) // 31
    day = ((h + l_val - 7 * m + 114) % 31) + 1
    return datetime.date(year, month, day)


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> datetime.date:
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> datetime.date:
    if month == 12:
        last_day = datetime.date(year, 12, 31)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - datetime.timedelta(days=offset)


def _observed_us_holiday(d: datetime.date) -> datetime.date:
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def get_us_market_holidays(year: int) -> Dict[datetime.date, str]:
    holidays: Dict[datetime.date, str] = {}
    nyd = _observed_us_holiday(datetime.date(year, 1, 1))
    if nyd.year == year:
        holidays[nyd] = "米国元日休場"
    next_nyd = _observed_us_holiday(datetime.date(year + 1, 1, 1))
    if next_nyd.year == year:
        holidays[next_nyd] = "米国元日振替休場"
    holidays[_nth_weekday_of_month(year, 1, 0, 3)] = "キング牧師記念日(米休場)"
    holidays[_nth_weekday_of_month(year, 2, 0, 3)] = "大統領の日(米休場)"
    holidays[_easter_sunday(year) - datetime.timedelta(days=2)] = "聖金曜日(米休場)"
    holidays[_last_weekday_of_month(year, 5, 0)] = "メモリアルデー(米休場)"
    holidays[_observed_us_holiday(datetime.date(year, 6, 19))] = "ジューンティーンス(米休場)"
    holidays[_observed_us_holiday(datetime.date(year, 7, 4))] = "米独立記念日(米休場)"
    holidays[_nth_weekday_of_month(year, 9, 0, 1)] = "レイバー・デー(米休場)"
    holidays[_nth_weekday_of_month(year, 11, 3, 4)] = "感謝祭(米休場)"
    holidays[_observed_us_holiday(datetime.date(year, 12, 25))] = "クリスマス(米休場)"
    return holidays


def is_us_trading_day(d: datetime.date) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in get_us_market_holidays(d.year)


def last_us_trading_day_before(d_str: str) -> str:
    cur = datetime.datetime.strptime(d_str, "%Y-%m-%d").date() - datetime.timedelta(days=1)
    while not is_us_trading_day(cur):
        cur -= datetime.timedelta(days=1)
    return cur.strftime("%Y-%m-%d")


class _USDstTz(datetime.tzinfo):
    """米国夏時間（3月第2日曜 02:00〜11月第1日曜 02:00）対応の tzinfo（Windows tzdata 不在時のフォールバック用）。"""

    def __init__(self, std_hours: int, std_name: str, dst_name: str) -> None:
        self._std_hours = std_hours
        self._std_offset = datetime.timedelta(hours=std_hours)
        self._dst_offset = datetime.timedelta(hours=std_hours + 1)
        self._std_name = std_name
        self._dst_name = dst_name

    def _is_dst_local(self, dt: Optional[datetime.datetime]) -> bool:
        if dt is None:
            return False
        y = dt.year
        mar_sun2 = _nth_weekday_of_month(y, 3, 6, 2)
        nov_sun1 = _nth_weekday_of_month(y, 11, 6, 1)
        start_local = datetime.datetime(y, 3, mar_sun2.day, 2, 0)
        end_local = datetime.datetime(y, 11, nov_sun1.day, 2, 0)
        naive = dt.replace(tzinfo=None)
        return start_local <= naive < end_local

    def utcoffset(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return self._dst_offset if self._is_dst_local(dt) else self._std_offset

    def dst(self, dt: Optional[datetime.datetime]) -> datetime.timedelta:
        return datetime.timedelta(hours=1) if self._is_dst_local(dt) else datetime.timedelta(0)

    def tzname(self, dt: Optional[datetime.datetime]) -> str:
        return self._dst_name if self._is_dst_local(dt) else self._std_name

    def fromutc(self, dt: datetime.datetime) -> datetime.datetime:
        utc_naive = dt.replace(tzinfo=None)
        y = utc_naive.year
        mar_sun2 = _nth_weekday_of_month(y, 3, 6, 2)
        nov_sun1 = _nth_weekday_of_month(y, 11, 6, 1)
        start_utc = datetime.datetime(y, 3, mar_sun2.day, 2 - self._std_hours, 0)
        end_utc = datetime.datetime(y, 11, nov_sun1.day, 1 - self._std_hours, 0)
        offset = self._dst_offset if (start_utc <= utc_naive < end_utc) else self._std_offset
        return (utc_naive + offset).replace(tzinfo=self)


_US_EASTERN_TZ = _USDstTz(-5, "EST", "EDT")
_US_CENTRAL_TZ = _USDstTz(-6, "CST", "CDT")


def resolve_tz(tz_name: Optional[str], gmtoffset: Optional[int] = None) -> datetime.tzinfo:
    if tz_name in ("Asia/Tokyo", "JST"):
        return JST
    if tz_name:
        try:
            return zoneinfo.ZoneInfo(tz_name)
        except Exception:
            pass
    if tz_name == "America/New_York":
        return _US_EASTERN_TZ
    if tz_name == "America/Chicago":
        return _US_CENTRAL_TZ
    if gmtoffset is not None:
        return datetime.timezone(datetime.timedelta(seconds=int(gmtoffset)))
    fallback_hours = {
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


NAV_CACHE_PATH = DOCS_DIR / "nav_cache.json"
ANCHOR_CACHE_PATH = ROOT_DIR / ".market_anchor_cache.json"


def is_domestic_snapshot_fund(fund: Dict[str, Any]) -> bool:
    """答え合わせ（forecast_log.json）の記録時刻を 14:50 JST とする国内株・国内ETF連動ファンドか判定する。"""
    return str(fund.get("model") or "") in (
        "domestic",
        "domestic_leveraged",
        "domestic_close",
        "topix_gold_plus",
    )


def load_or_fetch_nav_history(fund: Dict[str, Any]) -> Dict[str, int]:
    """投信総合検索ライブラリーから全期間の基準価額履歴を取得（キャッシュ利用＋nav_cache補完）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fid = fund["id"]
    cpath = CACHE_DIR / f"nav_{fid}.json"
    hist: Dict[str, int] = {}
    if cpath.exists():
        hist = json.loads(cpath.read_text(encoding="utf-8"))
    else:
        url = (
            "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
            f"?isinCd={fund['isinCd']}&associFundCd={fund['associFundCd']}"
        )
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=25)
        resp.raise_for_status()
        text = resp.content.decode("cp932", errors="replace")
        reader = csv.reader(io.StringIO(text))
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

    if NAV_CACHE_PATH.exists():
        try:
            nav_cache = json.loads(NAV_CACHE_PATH.read_text(encoding="utf-8"))
            entry = nav_cache.get(fid)
            if isinstance(entry, dict):
                for h_row in entry.get("history", []):
                    if isinstance(h_row, list) and len(h_row) >= 2:
                        hist[str(h_row[0])] = int(h_row[1])
                if entry.get("navDate") and entry.get("nav") is not None:
                    hist[str(entry["navDate"])] = int(entry["nav"])
        except Exception:
            pass
    return hist


def load_or_fetch_yahoo(
    symbol: str,
    range_str: str,
    interval: str,
    events: bool = False,
    min_last_ts: Optional[int] = None,
) -> Dict[str, Any]:
    """Yahoo Finance チャートを取得（キャッシュ利用、min_last_ts より古い場合は再取得）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe_sym = re.sub(r"[^A-Za-z0-9._-]", "_", symbol)
    ev_tag = "_div" if events else ""
    cpath = CACHE_DIR / f"yahoo_{safe_sym}_{range_str}_{interval}{ev_tag}.json"
    if cpath.exists():
        cached = json.loads(cpath.read_text(encoding="utf-8"))
        ts_list = cached.get("timestamp") or []
        if min_last_ts is None or (ts_list and int(ts_list[-1]) >= min_last_ts):
            return cached

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
    if ANCHOR_CACHE_PATH.exists():
        try:
            anc = json.loads(ANCHOR_CACHE_PATH.read_text(encoding="utf-8"))
            for k, v in (anc.get("murc_ttm") or {}).items():
                ttm_map[str(k)] = float(v)
        except Exception:
            pass

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
    topix_map: Dict[str, float] = {}
    if cpath.exists():
        topix_map = json.loads(cpath.read_text(encoding="utf-8"))
    if ANCHOR_CACHE_PATH.exists():
        try:
            anc = json.loads(ANCHOR_CACHE_PATH.read_text(encoding="utf-8"))
            for k, v in (anc.get("topix_daily") or {}).items():
                topix_map[str(k)] = float(v)
        except Exception:
            pass
    if topix_map:
        return topix_map

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


def decode_astro_devalue(arr: Any) -> Any:
    """Solactive 公式サイト (Astro Actions) の devalue 形式 JSON 配列をデコードする。"""
    if not isinstance(arr, list) or not arr:
        return None
    memo: Dict[int, Any] = {}

    def resolve(idx: int) -> Any:
        if idx < 0 or idx >= len(arr):
            return None
        if idx in memo:
            return memo[idx]
        v = arr[idx]
        if not isinstance(v, (list, dict)):
            memo[idx] = v
            return v
        if isinstance(v, list):
            res: Any = [resolve(x) if isinstance(x, int) else x for x in v]
            memo[idx] = res
            return res
        res_dict = {k: (resolve(val) if isinstance(val, int) else val) for k, val in v.items()}
        memo[idx] = res_dict
        return res_dict

    return resolve(0)


def load_or_fetch_solactive(isin: str) -> Dict[str, float]:
    """Solactive 公式エンドポイントから指定 ISIN の日足終値を取得（キャッシュ利用）する。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = CACHE_DIR / f"solactive_{isin}.json"
    if cpath.exists():
        return json.loads(cpath.read_text(encoding="utf-8"))

    now_ms = int(time.time() * 1000)
    start_ms = int((time.time() - 86400 * 220) * 1000)
    resp = requests.post(
        "https://www.solactive.com/_actions/getDayHistoryChartData/",
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=json.dumps({"isin": isin, "indexCreatingTimeStamp": start_ms, "dayDate": now_ms}),
        timeout=20,
    )
    resp.raise_for_status()
    rows = decode_astro_devalue(resp.json())
    out: Dict[str, float] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("timestamp") is not None and row.get("value") is not None:
                ts_sec = float(row["timestamp"]) / 1000.0
                d_str = datetime.datetime.fromtimestamp(ts_sec + 6 * 3600, UTC).strftime("%Y-%m-%d")
                out[d_str] = float(row["value"])

    cpath.write_text(json.dumps(dict(sorted(out.items())), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    time.sleep(1.2)
    return out


def parse_daily_series(chart: Dict[str, Any]) -> Tuple[List[Tuple[str, float]], Dict[str, float]]:
    meta = chart["meta"]
    tz_name = meta.get("exchangeTimezoneName", "America/New_York")
    gmtoffset = meta.get("gmtoffset")
    tz = resolve_tz(tz_name, gmtoffset)
    inst_type = str(meta.get("instrumentType") or "")
    sym = str(meta.get("symbol") or "")
    is_us_future = (inst_type == "FUTURE" or sym in ("NQ=F", "GC=F") or sym.endswith(".CMX")) and tz_name in (
        "America/New_York",
        "America/Chicago",
    )
    ts_list = chart.get("timestamp", [])
    cl_list = chart["indicators"]["quote"][0]["close"]
    dedup: Dict[str, float] = {}
    for t, c in zip(ts_list, cl_list):
        if c is None:
            continue
        t_int = int(t)
        dt_loc = datetime.datetime.fromtimestamp(t_int, tz)
        d_str = dt_loc.strftime("%Y-%m-%d")
        if is_us_future:
            # 米国先物の確定日足バーは 00:00 ET かつ米国市場営業日のみ（進行中Globexバーの上書き・日曜バー混入を防止）
            if dt_loc.hour != 0 or dt_loc.minute != 0:
                continue
            if not is_us_trading_day(dt_loc.date()):
                continue
            if d_str not in dedup:
                dedup[d_str] = float(c)
        else:
            dedup[d_str] = float(c)
    sorted_bars = sorted(dedup.items())

    divs_map: Dict[str, float] = {}
    divs = chart.get("events", {}).get("dividends", {})
    for _, div_obj in divs.items():
        div_ts = int(div_obj.get("date", 0))
        div_date = get_bar_local_date(div_ts, tz_name, gmtoffset)
        divs_map[div_date] = divs_map.get(div_date, 0.0) + float(div_obj.get("amount", 0.0))
    return sorted_bars, divs_map


def parse_hourly_bars(chart: Dict[str, Any]) -> List[Tuple[int, float, float]]:
    """1時間足・15分足の (timestamp, open, close) リストを返す。"""
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
        # 英国冬時間(GMT)の月曜朝など 08:00 JST 足が存在せず 09:00 JST 足 open が週明け最初のバーとなる場合
        if use_open:
            next_cands = [b for b in h_bars if target_ts < b[0] <= target_ts + 3600]
            if next_cands:
                return next_cands[0][1]
        return None
    # 1時間足で 08:30 JST を再現する場合：08:00 JST 開始足 (best[0] == 08:00) があれば open（08:00時点）または close（09:00時点だが8:30未来値混入を避けるため open）を使う
    if use_open:
        return best[1]
    return best[2]


def find_intraday_price_at_830(
    m15_bars: List[Tuple[int, float, float]],
    h_bars: List[Tuple[int, float, float]],
    d1_str: str,
) -> Optional[float]:
    """指定営業日 d1_str の朝 08:30 JST 時点（Yahoo 10分ディレイ考慮で 08:00 15分足 close または 08:00 1時間足 open）の価格を返す。"""
    d1_7am_ts = int(datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=7, minute=0, tzinfo=JST).timestamp())
    d1_800_ts = int(datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=8, minute=0, tzinfo=JST).timestamp())
    if m15_bars:
        cands = [b for b in m15_bars if d1_7am_ts <= b[0] <= d1_800_ts]
        if cands:
            return cands[-1][2]
    d1_8am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=8, minute=0, tzinfo=JST)
    return find_hourly_price_at(h_bars, d1_8am, use_open=True)


def simulate_us_futures_at_830(
    d0_str: str,
    d1_str: str,
    settle_hour_et: int,
    settle_min_et: int,
    daily_bars: List[Tuple[str, float]],
    m15_bars: List[Tuple[int, float, float]],
    h_bars: List[Tuple[int, float, float]],
    lock_settled: bool,
    align_base_date: Optional[str] = None,
) -> Tuple[float, str, float, str, float]:
    """D1 朝 08:30 JST 時点での get_us_futures_change の挙動を再現し (r_idx, base_date, base_val, cur_date, cur_val) を返す。
    lock_settled=False: 週明け（日曜夜Globex）・米国祝日・朝8:30の新セッション現在値が混入していた改修前の挙動
    lock_settled=True : D1 の基準価額に反映される最終米国セッション (target_us_date) の清算値で固定する改修後の挙動
    """
    tz_et = resolve_tz("America/New_York")
    daily_map = dict(daily_bars)

    settle_bar_start_dt = (
        datetime.datetime(2000, 1, 1, settle_hour_et, settle_min_et) - datetime.timedelta(minutes=15)
    )
    settle_hhmm = settle_bar_start_dt.strftime("%H:%M")
    settle_15m_map: Dict[str, float] = {}
    globex_1645_map: Dict[str, float] = {}

    for t, _, c in m15_bars:
        dt_et = datetime.datetime.fromtimestamp(int(t), tz_et)
        if not is_us_trading_day(dt_et.date()):
            continue
        d_et = dt_et.strftime("%Y-%m-%d")
        hm_et = dt_et.strftime("%H:%M")
        if hm_et == settle_hhmm:
            settle_15m_map[d_et] = float(c)
        elif hm_et == "16:45":
            globex_1645_map[d_et] = float(c)

    def resolve_settled_close(d_et: str, d_close: float) -> float:
        g1645 = globex_1645_map.get(d_et)
        s15 = settle_15m_map.get(d_et)
        if g1645 is not None and s15 is not None:
            if abs(d_close - g1645) <= 1e-2 and abs(d_close - s15) > 1e-2:
                return s15
        return d_close

    def get_settled_price(d_et: str) -> Optional[float]:
        if d_et in daily_map:
            return resolve_settled_close(d_et, daily_map[d_et])
        if d_et in settle_15m_map:
            return settle_15m_map[d_et]
        return None

    expected_base = align_base_date if align_base_date else last_us_trading_day_before(d0_str)
    b_settled = get_settled_price(expected_base)
    before_d0 = [(d, c) for d, c in daily_bars if d < d0_str]
    if b_settled is not None:
        base_date = expected_base
        base_val = b_settled
    elif before_d0:
        base_date, b_raw = before_d0[-1]
        base_val = resolve_settled_close(base_date, b_raw)
    else:
        return 0.0, d0_str, 0.0, d0_str, 0.0

    target_us_date = last_us_trading_day_before(d1_str)

    if lock_settled:
        if target_us_date <= base_date or base_val <= 0:
            return 0.0, base_date, base_val, base_date, base_val
        c_settled = get_settled_price(target_us_date)
        if c_settled is not None:
            cur_date = target_us_date
            cur_val = c_settled
        else:
            before_d1 = [(d, c) for d, c in daily_bars if base_date < d <= target_us_date]
            if before_d1:
                cur_date, c_raw = before_d1[-1]
                cur_val = resolve_settled_close(cur_date, c_raw)
            else:
                return 0.0, base_date, base_val, base_date, base_val
        return (cur_val - base_val) / base_val, base_date, base_val, cur_date, cur_val

    # lock_settled=False（改修前）:
    # D1 朝 08:30 JST (= 前日 18:30/19:30 ET) 時点では Yahoo 1d チャート末尾に新 Globex セッションのライブバーが
    # 現地日付 (D1 - 1日 < D1) で付与され、daily_map[D1 - 1日] を新セッション現在値で生成・上書きしていた
    live_830 = find_intraday_price_at_830(m15_bars, h_bars, d1_str)
    d1_830_jst = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=8, minute=30, tzinfo=JST)
    live_et_date = d1_830_jst.astimezone(tz_et).strftime("%Y-%m-%d")
    if live_830 is not None and live_et_date > base_date and base_val > 0:
        return (live_830 - base_val) / base_val, base_date, base_val, live_et_date, live_830

    if target_us_date <= base_date or base_val <= 0:
        return 0.0, base_date, base_val, base_date, base_val
    c_settled = get_settled_price(target_us_date)
    cur_val = c_settled if c_settled is not None else base_val
    return (cur_val - base_val) / base_val, base_date, base_val, target_us_date, cur_val


def find_intraday_price_at_1450(
    m15_bars: List[Tuple[int, float, float]],
    h_bars: List[Tuple[int, float, float]],
    d1_str: str,
) -> Optional[float]:
    """指定営業日 d1_str の 14:50 JST 時点の価格を Yahoo 15分足（14:45足 close、60日超の過去日は1時間足14:00足 close）から返す。"""
    d1_start_ts = int(datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=9, minute=0, tzinfo=JST).timestamp())
    d1_1450_ts = int(datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=14, minute=50, tzinfo=JST).timestamp())
    if m15_bars:
        cands = [b for b in m15_bars if d1_start_ts <= b[0] <= d1_1450_ts]
        if cands:
            return cands[-1][2]
    if h_bars:
        cands_h = [b for b in h_bars if d1_start_ts <= b[0] <= d1_1450_ts]
        if cands_h:
            return cands_h[-1][2]
    return None


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
    solactive_maps: Optional[Dict[str, Dict[str, float]]] = None,
    m15_series: Optional[Dict[str, List[Tuple[int, float, float]]]] = None,
    auam_formula: str = "additive",
    gold_plus_formula: str = "additive",
    domestic_record_time: str = "auto",
    futures_lock: Optional[bool] = None,
) -> Optional[int]:
    """指定日の記録時刻（海外 8:30 JST / 国内 14:50 JST）時点で得られたデータから予想基準価額を計算する。"""
    d0_9am = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=9, minute=0, tzinfo=JST)
    d0_15pm = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=15, minute=0, tzinfo=JST)
    d1_8am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=8, minute=0, tzinfo=JST)
    m15_map = m15_series or {}
    lock_fut = futures_lock if futures_lock is not None else (mode == "after")

    use_1450_domestic = (
        domestic_record_time == "14:50"
        or (domestic_record_time == "auto" and mode == "after")
    )

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
        elif sym.startswith("SOLACTIVE:"):
            sol_isin = sym.split(":", 1)[1]
            sol_bars = sorted((solactive_maps or {}).get(sol_isin, {}).items())
            r_idx, _, _, _, _ = get_series_change_between(sol_bars, {}, d0_str, d1_str, adjust_div=False)
        else:
            eval_sym = "^GSPC" if (mode == "before" and fund.get("id") == "sbi-v-sp500") else sym
            adj = False if (mode == "before" and fund.get("id") == "sbi-v-sp500") else (
                (model == "standard_div") or (mode == "after" and eval_sym in ("VOO", "VTI", "QQQ", "VT", "SCHD"))
            )
            bars, divs = daily_series[eval_sym]
            r_idx, _, _, _, _ = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=adj)

        r_total = (1.0 + r_idx) * (1.0 + r_fx) - 1.0

    elif model == "leveraged_hedged":
        bars, divs = daily_series[sym]
        if sym == "NQ=F":
            r_idx, _, _, _, _ = simulate_us_futures_at_830(
                d0_str, d1_str, 16, 0, bars, m15_map.get("NQ=F", []), hourly_series.get("NQ=F", []), lock_settled=lock_fut
            )
        else:
            r_idx, _, _, _, _ = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=False)
        r_total = lev * r_idx

    elif model in ("additive", "leveraged_unhedged"):
        bars, divs = daily_series[sym]
        if sym == "NQ=F":
            r_idx, _, _, _, _ = simulate_us_futures_at_830(
                d0_str, d1_str, 16, 0, bars, m15_map.get("NQ=F", []), hourly_series.get("NQ=F", []), lock_settled=lock_fut
            )
        else:
            r_idx, _, _, _, _ = get_series_change_between(bars, divs, d0_str, d1_str, adjust_div=False)
        if mode == "before" or auam_formula == "additive":
            r_total = lev * r_idx + r_fx
        else:
            r_total = (1.0 + lev * r_idx) * (1.0 + r_fx) - 1.0

    elif model in ("domestic", "domestic_leveraged"):
        is_topix = (group_id == "topix" or sym in ("1306.T", "^TOPX", "TOPIX"))
        if use_1450_domestic:
            # 14:50 JST 記録：Yahoo 15分足（^N225 / 1306.T の変化率で TOPIX を推定）
            if is_topix:
                etf_bars, etf_divs = daily_series["1306.T"]
                etf_d0_map = dict(etf_bars)
                b_1306 = etf_d0_map.get(d0_str)
                if b_1306 is None:
                    prev_1306 = [c for d, c in etf_bars if d <= d0_str]
                    b_1306 = prev_1306[-1] if prev_1306 else 0.0
                c_1306 = find_intraday_price_at_1450(m15_map.get("1306.T", []), hourly_series.get("1306.T", []), d1_str)
                if c_1306 is None:
                    c_1306 = etf_d0_map.get(d1_str, b_1306)
                div_1306 = float(etf_divs.get(d1_str, 0.0))
                r_idx = ((c_1306 + div_1306) - b_1306) / b_1306 if (b_1306 and b_1306 > 0) else 0.0
            else:
                b_n225 = n225_map.get(d0_str)
                if b_n225 is None:
                    n_bars, _ = daily_series["^N225"]
                    prev_n = [c for d, c in n_bars if d <= d0_str]
                    b_n225 = prev_n[-1] if prev_n else 0.0
                c_n225 = find_intraday_price_at_1450(m15_map.get("^N225", []), hourly_series.get("^N225", []), d1_str)
                if c_n225 is None:
                    c_n225 = n225_map.get(d1_str, b_n225)
                r_idx = (c_n225 - b_n225) / b_n225 if (b_n225 and b_n225 > 0) else 0.0
        else:
            # 朝 8:30 JST 時点では東証開場前のため、夜間先物変化率を使用
            niy_h = hourly_series.get("NIY=F", [])
            f_base = find_hourly_price_at(niy_h, d0_15pm, use_open=False)
            f_cur = find_hourly_price_at(niy_h, d1_8am, use_open=True)
            r_fut = (f_cur - f_base) / f_base if (f_base and f_cur and f_base > 0) else 0.0
            if mode == "after" and is_topix:
                beta = calc_rolling_beta(topix_map, n225_map, d0_str, lookback=60)
                r_idx = beta * r_fut
            else:
                r_idx = r_fut
        r_total = lev * r_idx if model == "domestic_leveraged" else r_idx

    elif model == "domestic_close":
        if use_1450_domestic:
            # 14:50 JST 記録：1540.T 15分足の D0 終値比
            dc_bars, dc_divs = daily_series[sym]
            dc_map = dict(dc_bars)
            b_dc = dc_map.get(d0_str)
            if b_dc is None:
                prev_dc = [c for d, c in dc_bars if d <= d0_str]
                b_dc = prev_dc[-1] if prev_dc else 0.0
            c_dc = find_intraday_price_at_1450(m15_map.get(sym, []), hourly_series.get(sym, []), d1_str)
            if c_dc is None:
                c_dc = dc_map.get(d1_str, b_dc)
            div_dc = float(dc_divs.get(d1_str, 0.0))
            r_total = ((c_dc + div_dc) - b_dc) / b_dc if (b_dc and b_dc > 0) else 0.0
        else:
            # 朝 8:30 JST 時点では東証開場前のため変化率 0
            r_total = 0.0

    elif model in ("gold_plus", "topix_gold_plus"):
        gold_sym = fund.get("goldSymbol", "GC=F")
        g_bars, _ = daily_series[gold_sym]
        ref_us_bars, _ = daily_series["^GSPC"]
        _, ref_us_b_date, _, _, _ = get_series_change_between(ref_us_bars, {}, d0_str, d1_str, adjust_div=False)
        r_gold, _, _, _, _ = simulate_us_futures_at_830(
            d0_str, d1_str, 13, 30, g_bars, m15_map.get(gold_sym, []), hourly_series.get(gold_sym, []),
            lock_settled=lock_fut, align_base_date=ref_us_b_date,
        )

        if model == "topix_gold_plus" or sym in ("TOPIX", "^TOPX"):
            if use_1450_domestic:
                # 14:50 JST 記録：TOPIX 部分は 1306.T 15分足変化率、為替は D1 仲値確定済み
                etf_bars, etf_divs = daily_series["1306.T"]
                etf_d0_map = dict(etf_bars)
                b_1306 = etf_d0_map.get(d0_str)
                if b_1306 is None:
                    prev_1306 = [c for d, c in etf_bars if d <= d0_str]
                    b_1306 = prev_1306[-1] if prev_1306 else 0.0
                c_1306 = find_intraday_price_at_1450(m15_map.get("1306.T", []), hourly_series.get("1306.T", []), d1_str)
                if c_1306 is None:
                    c_1306 = etf_d0_map.get(d1_str, b_1306)
                div_1306 = float(etf_divs.get(d1_str, 0.0))
                r_stock_jpy = ((c_1306 + div_1306) - b_1306) / b_1306 if (b_1306 and b_1306 > 0) else 0.0

                fx_1450_cur = ttm_map.get(d1_str, fx_cur)
                r_fx_1450 = (fx_1450_cur - fx_base) / fx_base if fx_base else r_fx
                gold_etf_sym = fund.get("goldEtfSymbol")
                if gold_etf_sym and gold_etf_sym in daily_series:
                    w_etf = float(fund.get("goldEtfWeight", 0.55))
                    ge_bars, ge_divs = daily_series[gold_etf_sym]
                    r_ge, _, _, _, _ = get_series_change_between(ge_bars, ge_divs, d0_str, d1_str, adjust_div=False)
                    r_gold_leg = w_etf * ((1.0 + r_ge) * (1.0 + r_fx_1450) - 1.0) + (1.0 - w_etf) * r_gold
                else:
                    r_gold_leg = r_gold
                r_total = r_stock_jpy + r_gold_leg
            else:
                niy_h = hourly_series.get("NIY=F", [])
                f_base = find_hourly_price_at(niy_h, d0_15pm, use_open=False)
                f_cur = find_hourly_price_at(niy_h, d1_8am, use_open=True)
                r_fut = (f_cur - f_base) / f_base if (f_base and f_cur and f_base > 0) else 0.0
                if mode == "after":
                    beta = calc_rolling_beta(topix_map, n225_map, d0_str, lookback=60)
                    r_stock_jpy = beta * r_fut
                    gold_etf_sym = fund.get("goldEtfSymbol")
                    if gold_etf_sym and gold_etf_sym in daily_series:
                        w_etf = float(fund.get("goldEtfWeight", 0.55))
                        ge_bars, ge_divs = daily_series[gold_etf_sym]
                        r_ge, _, _, _, _ = get_series_change_between(ge_bars, ge_divs, d0_str, d1_str, adjust_div=False)
                        r_gold_leg = w_etf * ((1.0 + r_ge) * (1.0 + r_fx) - 1.0) + (1.0 - w_etf) * r_gold
                    else:
                        r_gold_leg = r_gold
                    r_total = r_stock_jpy + r_gold_leg
                else:
                    r_total = r_fut + r_gold
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

    # forecast_log.json に既存の過去日（例: 61営業日前の 2026-08-03）がある場合はシード対象ペアに含める
    seed_window = ref_dates[-(args.days + 2):] if len(ref_dates) >= args.days + 2 else target_window
    seed_day_pairs: List[Tuple[str, str]] = [
        (seed_window[i], seed_window[i + 1]) for i in range(len(seed_window) - 1)
    ]

    all_d0_dates = sorted({d0 for d0, _ in seed_day_pairs} | {d1 for _, d1 in seed_day_pairs})

    # 2. 市場データの取得
    yahoo_symbols = {"JPY=X", "NIY=F", "^N225", "1306.T", "1540.T", "^GSPC", "^NDX", "NQ=F", "ACWI", "TOK", "SCHD", "VTI", "IGLN.L", "GC=F", "GCZ26.CMX"}
    solactive_isins = set()
    for _, _, f in all_funds:
        sym_val = str(f.get("symbol") or "")
        if sym_val.startswith("SOLACTIVE:"):
            solactive_isins.add(sym_val.split(":", 1)[1])
        elif sym_val and not sym_val.startswith("MSCI:") and sym_val != "TOPIX":
            yahoo_symbols.add(sym_val)
        if f.get("goldSymbol"):
            yahoo_symbols.add(f["goldSymbol"])
        if f.get("goldEtfSymbol"):
            yahoo_symbols.add(f["goldEtfSymbol"])

    div_syms = {"SCHD", "VTI", "VOO", "QQQ", "VT", "ACWI", "TOK", "GLD", "IAU", "GLDM"}
    daily_series: Dict[str, Tuple[List[Tuple[str, float]], Dict[str, float]]] = {}
    for sym in sorted(yahoo_symbols):
        ch = load_or_fetch_yahoo(sym, range_str="6mo", interval="1d", events=(sym in div_syms))
        daily_series[sym] = parse_daily_series(ch)

    hourly_series: Dict[str, List[Tuple[int, float, float]]] = {}
    min_intraday_ts = int(datetime.datetime.strptime(day_pairs[-1][1], "%Y-%m-%d").replace(hour=8, minute=30, tzinfo=JST).timestamp())
    for sym in ["JPY=X", "NIY=F", "NQ=F", "GC=F", "GCZ26.CMX", "^N225", "1306.T", "1540.T"]:
        ch_h = load_or_fetch_yahoo(sym, range_str="6mo", interval="1h", events=False, min_last_ts=min_intraday_ts)
        hourly_series[sym] = parse_hourly_bars(ch_h)

    m15_series: Dict[str, List[Tuple[int, float, float]]] = {}
    for sym in ["^N225", "1306.T", "1540.T", "NQ=F", "GC=F", "GCZ26.CMX"]:
        ch_15 = load_or_fetch_yahoo(sym, range_str="60d", interval="15m", events=False, min_last_ts=min_intraday_ts)
        m15_series[sym] = parse_hourly_bars(ch_15)

    ttm_map = load_or_fetch_murc_ttm(all_d0_dates)
    topix_map = load_or_fetch_topix_daily()
    n225_map = dict(daily_series["^N225"][0])
    msci_acwi_map = load_or_fetch_msci("892400")
    msci_kokusai_map = load_or_fetch_msci("991200")
    solactive_maps = {isin: load_or_fetch_solactive(isin) for isin in sorted(solactive_isins)}

    print(f"=== バックテスト期間: {day_pairs[0][1]} 〜 {day_pairs[-1][1]} ({len(day_pairs)} 営業日) ===\n")

    # 3. 国内株グループ・ファンド別 平均誤差（朝 8:30 記録 vs 14:50 記録）
    print("=== 国内株グループ別 平均絶対誤差（MAE, 単位: pt）朝 8:30 記録 vs 14:50 記録 ===")
    print("| グループ | 対象本数 | サンプル数 | 8:30記録 MAE | 14:50記録 MAE | 改善幅 (14:50-8:30) | 8:30 Bias | 14:50 Bias |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")

    dom_total_830_abs: List[float] = []
    dom_total_1450_abs: List[float] = []
    dom_total_830_sgn: List[float] = []
    dom_total_1450_sgn: List[float] = []
    dom_fund_rows: List[str] = []

    for g in funds_cfg["groups"]:
        gid = g["id"]
        dom_funds = [f for f in g["funds"] if is_domestic_snapshot_fund(f)]
        if not dom_funds:
            continue
        g_830_abs: List[float] = []
        g_1450_abs: List[float] = []
        g_830_sgn: List[float] = []
        g_1450_sgn: List[float] = []

        for f in dom_funds:
            fid = f["id"]
            h = nav_histories[fid]
            f_830_abs: List[float] = []
            f_1450_abs: List[float] = []
            f_830_sgn: List[float] = []
            f_1450_sgn: List[float] = []
            for d0, d1 in day_pairs:
                if d0 not in h or d1 not in h:
                    continue
                nav0, act = h[d0], h[d1]
                fc_830 = simulate_fund_day(
                    f, gid, d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    solactive_maps=solactive_maps, m15_series=m15_series,
                    auam_formula=args.auam_formula, domestic_record_time="08:30",
                )
                fc_1450 = simulate_fund_day(
                    f, gid, d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    solactive_maps=solactive_maps, m15_series=m15_series,
                    auam_formula=args.auam_formula, domestic_record_time="14:50",
                )
                if fc_830 is None or fc_1450 is None:
                    continue
                e830 = (fc_830 - act) / nav0 * 100.0
                e1450 = (fc_1450 - act) / nav0 * 100.0
                f_830_abs.append(abs(e830))
                f_1450_abs.append(abs(e1450))
                f_830_sgn.append(e830)
                f_1450_sgn.append(e1450)
                g_830_abs.append(abs(e830))
                g_1450_abs.append(abs(e1450))
                g_830_sgn.append(e830)
                g_1450_sgn.append(e1450)
                dom_total_830_abs.append(abs(e830))
                dom_total_1450_abs.append(abs(e1450))
                dom_total_830_sgn.append(e830)
                dom_total_1450_sgn.append(e1450)

            if f_830_abs:
                fm_830 = sum(f_830_abs) / len(f_830_abs)
                fm_1450 = sum(f_1450_abs) / len(f_1450_abs)
                fb_830 = sum(f_830_sgn) / len(f_830_sgn)
                fb_1450 = sum(f_1450_sgn) / len(f_1450_sgn)
                dom_fund_rows.append(
                    f"| {g['label']} | {f['name']} | {len(f_830_abs)} | "
                    f"{fm_830:.4f}pt | {fm_1450:.4f}pt | {fm_1450 - fm_830:+.4f}pt | {fb_830:+.4f}pt | {fb_1450:+.4f}pt |"
                )

        if g_830_abs:
            gm_830 = sum(g_830_abs) / len(g_830_abs)
            gm_1450 = sum(g_1450_abs) / len(g_1450_abs)
            gb_830 = sum(g_830_sgn) / len(g_830_sgn)
            gb_1450 = sum(g_1450_sgn) / len(g_1450_sgn)
            g_title = g["label"] if len(dom_funds) == len(g["funds"]) else f"{g['label']}（国内対象{len(dom_funds)}本）"
            print(
                f"| {g_title} | {len(dom_funds)} | {len(g_830_abs)} | "
                f"{gm_830:.4f}pt | {gm_1450:.4f}pt | {gm_1450 - gm_830:+.4f}pt | {gb_830:+.4f}pt | {gb_1450:+.4f}pt |"
            )

    if dom_total_830_abs:
        dm_830 = sum(dom_total_830_abs) / len(dom_total_830_abs)
        dm_1450 = sum(dom_total_1450_abs) / len(dom_total_1450_abs)
        db_830 = sum(dom_total_830_sgn) / len(dom_total_830_sgn)
        db_1450 = sum(dom_total_1450_sgn) / len(dom_total_1450_sgn)
        print(
            f"| **国内株対象 合計** | **14** | **{len(dom_total_830_abs)}** | "
            f"**{dm_830:.4f}pt** | **{dm_1450:.4f}pt** | **{dm_1450 - dm_830:+.4f}pt** | "
            f"**{db_830:+.4f}pt** | **{db_1450:+.4f}pt** |"
        )

    print("\n=== 国内株ファンド個別 平均絶対誤差（MAE, 単位: pt）朝 8:30 記録 vs 14:50 記録 ===")
    print("| グループ | ファンド | サンプル数 | 8:30記録 MAE | 14:50記録 MAE | 改善幅 (14:50-8:30) | 8:30 Bias | 14:50 Bias |")
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    for r_line in dom_fund_rows:
        print(r_line)
    print()

    chosen_auam = args.auam_formula

    # 4. グループ別 改善前 vs 改善後 の平均絶対誤差（MAE）比較
    print("=== 全グループ別 平均絶対誤差（MAE, 単位: pt）改善前 vs 改善後（海外8:30 / 国内14:50） ===")
    print("| グループ | 本数 | 記録時刻 | サンプル数 | 改善前 MAE | 改善後 MAE | 改善幅 (後-前) | 改善前 Bias | 改善後 Bias |")
    print("|---|---:|:---:|---:|---:|---:|---:|---:|---:|")

    all_before_abs: List[float] = []
    all_after_abs: List[float] = []

    for g in funds_cfg["groups"]:
        gid = g["id"]
        glabel = g["label"]
        g_before_abs: List[float] = []
        g_after_abs: List[float] = []
        g_before_sgn: List[float] = []
        g_after_sgn: List[float] = []
        has_dom = any(is_domestic_snapshot_fund(f) for f in g["funds"])
        has_ovr = any(not is_domestic_snapshot_fund(f) for f in g["funds"])
        rec_time_lbl = "8:30 / 14:50" if (has_dom and has_ovr) else ("14:50" if has_dom else "8:30")

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
                    solactive_maps=solactive_maps, m15_series=m15_series,
                    auam_formula="additive",
                )
                fc_aft = simulate_fund_day(
                    f, gid, d0, d1, nav0, "after",
                    daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                    solactive_maps=solactive_maps, m15_series=m15_series,
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

        if g_before_abs:
            m_bef = sum(g_before_abs) / len(g_before_abs)
            m_aft = sum(g_after_abs) / len(g_after_abs)
            b_bef = sum(g_before_sgn) / len(g_before_sgn)
            b_aft = sum(g_after_sgn) / len(g_after_sgn)
            diff = m_aft - m_bef
            print(
                f"| {glabel} | {len(g['funds'])} | {rec_time_lbl} | {len(g_before_abs)} | "
                f"{m_bef:.4f}pt | {m_aft:.4f}pt | {diff:+.4f}pt | {b_bef:+.4f}pt | {b_aft:+.4f}pt |"
            )

    if all_before_abs:
        tot_bef = sum(all_before_abs) / len(all_before_abs)
        tot_aft = sum(all_after_abs) / len(all_after_abs)
        print(
            f"| **全体** | **{len(all_funds)}** | **8:30 / 14:50** | **{len(all_before_abs)}** | "
            f"**{tot_bef:.4f}pt** | **{tot_aft:.4f}pt** | **{tot_aft - tot_bef:+.4f}pt** | ― | ― |"
        )

    # β の最新値も参考出力
    latest_d0 = day_pairs[-1][0]
    latest_beta = calc_rolling_beta(topix_map, n225_map, latest_d0, lookback=60)
    print(f"\n直近 60 営業日の TOPIX 対 日経平均 回帰係数 β ({latest_d0} 時点): {latest_beta:.4f}")

    # 5. 週明け・祝日明け・米国休場明けの朝 8:30 記録における米国先物補正（改修前 vs 改修後）比較
    us_futures_fund_ids = [
        "auam-triple-ndx",
        "auam-lev-ndx",
        "ifree-lev-ndx",
        "tracers-sp500-gold",
        "tracers-ndx-gold",
        "tracers-acwi-gold",
    ]
    fund_by_id: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for gid, _, f in all_funds:
        if f["id"] in us_futures_fund_ids:
            fund_by_id[f["id"]] = (gid, f)

    weekend_holiday_pairs: List[Tuple[str, str, str]] = []
    for d0, d1 in day_pairs:
        dt0 = datetime.date.fromisoformat(d0)
        dt1 = datetime.date.fromisoformat(d1)
        gap_days = (dt1 - dt0).days
        if dt1.weekday() == 0 and gap_days == 3:
            day_type = "月曜"
        elif gap_days > 1:
            day_type = f"連休・祝日明け({gap_days}日)"
        elif not is_us_trading_day(dt0):
            day_type = "米休場明け"
        else:
            continue
        weekend_holiday_pairs.append((d0, d1, day_type))

    print(
        f"\n=== 米国先物利用ファンド 週明け・祝日明け・米休場明け（{len(weekend_holiday_pairs)}営業日）朝 8:30 記録 MAE比較（先物補正・改修前 vs 改修後） ==="
    )
    print(
        "| ファンド | 対象日数 | 週明け等 改修前 MAE | 週明け等 改修後 MAE | 改善幅 (後-前) | 週明け等 改修前 Bias | 週明け等 改修後 Bias | 全60日 改修前 MAE | 全60日 改修後 MAE |"
    )
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")

    wh_all_bef_abs: List[float] = []
    wh_all_aft_abs: List[float] = []
    wh_all_bef_sgn: List[float] = []
    wh_all_aft_sgn: List[float] = []
    full_all_bef_abs: List[float] = []
    full_all_aft_abs: List[float] = []

    for fid in us_futures_fund_ids:
        if fid not in fund_by_id:
            continue
        gid, f = fund_by_id[fid]
        h = nav_histories[fid]
        wh_bef_abs: List[float] = []
        wh_aft_abs: List[float] = []
        wh_bef_sgn: List[float] = []
        wh_aft_sgn: List[float] = []
        f60_bef_abs: List[float] = []
        f60_aft_abs: List[float] = []
        wh_d1_set = {d1 for _, d1, _ in weekend_holiday_pairs}

        for d0, d1 in day_pairs:
            if d0 not in h or d1 not in h:
                continue
            nav0, act = h[d0], h[d1]
            fc_unlk = simulate_fund_day(
                f, gid, d0, d1, nav0, "after",
                daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                solactive_maps=solactive_maps, m15_series=m15_series,
                auam_formula=chosen_auam, futures_lock=False,
            )
            fc_lock = simulate_fund_day(
                f, gid, d0, d1, nav0, "after",
                daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                solactive_maps=solactive_maps, m15_series=m15_series,
                auam_formula=chosen_auam, futures_lock=True,
            )
            if fc_unlk is None or fc_lock is None:
                continue
            e_unlk = (fc_unlk - act) / nav0 * 100.0
            e_lock = (fc_lock - act) / nav0 * 100.0
            f60_bef_abs.append(abs(e_unlk))
            f60_aft_abs.append(abs(e_lock))
            full_all_bef_abs.append(abs(e_unlk))
            full_all_aft_abs.append(abs(e_lock))
            if d1 in wh_d1_set:
                wh_bef_abs.append(abs(e_unlk))
                wh_aft_abs.append(abs(e_lock))
                wh_bef_sgn.append(e_unlk)
                wh_aft_sgn.append(e_lock)
                wh_all_bef_abs.append(abs(e_unlk))
                wh_all_aft_abs.append(abs(e_lock))
                wh_all_bef_sgn.append(e_unlk)
                wh_all_aft_sgn.append(e_lock)

        if wh_bef_abs:
            m_b = sum(wh_bef_abs) / len(wh_bef_abs)
            m_a = sum(wh_aft_abs) / len(wh_aft_abs)
            b_b = sum(wh_bef_sgn) / len(wh_bef_sgn)
            b_a = sum(wh_aft_sgn) / len(wh_aft_sgn)
            m60_b = sum(f60_bef_abs) / len(f60_bef_abs)
            m60_a = sum(f60_aft_abs) / len(f60_aft_abs)
            print(
                f"| {f['name']} | {len(wh_bef_abs)} | {m_b:.4f}pt | {m_a:.4f}pt | "
                f"{m_a - m_b:+.4f}pt | {b_b:+.4f}pt | {b_a:+.4f}pt | {m60_b:.4f}pt | {m60_a:.4f}pt |"
            )

    if wh_all_bef_abs:
        mb_all = sum(wh_all_bef_abs) / len(wh_all_bef_abs)
        ma_all = sum(wh_all_aft_abs) / len(wh_all_aft_abs)
        bb_all = sum(wh_all_bef_sgn) / len(wh_all_bef_sgn)
        ba_all = sum(wh_all_aft_sgn) / len(wh_all_aft_sgn)
        m60b_all = sum(full_all_bef_abs) / len(full_all_bef_abs)
        m60a_all = sum(full_all_aft_abs) / len(full_all_aft_abs)
        print(
            f"| **先物利用 6本平均** | **{len(wh_all_bef_abs)}** | **{mb_all:.4f}pt** | **{ma_all:.4f}pt** | "
            f"**{ma_all - mb_all:+.4f}pt** | **{bb_all:+.4f}pt** | **{ba_all:+.4f}pt** | "
            f"**{m60b_all:.4f}pt** | **{m60a_all:.4f}pt** |"
        )

    print(
        f"\n=== 週明け・祝日明け・米休場明け（{len(weekend_holiday_pairs)}営業日）日付別 誤差（pt）詳細比較（改修前 -> 改修後） ==="
    )
    print(
        "| 対象日 (D0->D1) | 区分 | NQ=F 変化率 (前->後) | auAMトリプル (前->後) | auAMレバ (前->後) | iFreeレバ (前->後) | Tracers S&P500金 (前->後) | Tracersオルカン金 (前->後) |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|")
    nq_d_bars, _ = daily_series["NQ=F"]
    for d0, d1, day_type in weekend_holiday_pairs:
        r_nq_bef, _, _, _, _ = simulate_us_futures_at_830(
            d0, d1, 16, 0, nq_d_bars, m15_series.get("NQ=F", []), hourly_series.get("NQ=F", []), lock_settled=False
        )
        r_nq_aft, _, _, _, _ = simulate_us_futures_at_830(
            d0, d1, 16, 0, nq_d_bars, m15_series.get("NQ=F", []), hourly_series.get("NQ=F", []), lock_settled=True
        )
        cells: List[str] = []
        for fid in ["auam-triple-ndx", "auam-lev-ndx", "ifree-lev-ndx", "tracers-sp500-gold", "tracers-acwi-gold"]:
            gid, f = fund_by_id[fid]
            h = nav_histories[fid]
            if d0 not in h or d1 not in h:
                cells.append("―")
                continue
            nav0, act = h[d0], h[d1]
            fc_b = simulate_fund_day(
                f, gid, d0, d1, nav0, "after",
                daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                solactive_maps=solactive_maps, m15_series=m15_series,
                auam_formula=chosen_auam, futures_lock=False,
            )
            fc_a = simulate_fund_day(
                f, gid, d0, d1, nav0, "after",
                daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                solactive_maps=solactive_maps, m15_series=m15_series,
                auam_formula=chosen_auam, futures_lock=True,
            )
            if fc_b is None or fc_a is None:
                cells.append("―")
                continue
            eb = (fc_b - act) / nav0 * 100.0
            ea = (fc_a - act) / nav0 * 100.0
            cells.append(f"{eb:+.2f}pt -> **{ea:+.2f}pt**")
        print(
            f"| {d0[5:]}->{d1} | {day_type} | {r_nq_bef*100:+.2f}% -> {r_nq_aft*100:+.2f}% | "
            + " | ".join(cells)
            + " |"
        )

    # 6. 2026-11-02 08:34:41 JST 時点の再現検証テーブル
    if FORECAST_LOG_PATH.exists():
        try:
            flog_data = json.loads(FORECAST_LOG_PATH.read_text(encoding="utf-8"))
            log_1102 = (flog_data.get("entries") or {}).get("2026-11-02") or {}
            if log_1102:
                # 11/2 08:34:41 JST 記録時の実効為替変化率を eMAXIS Slim S&P500 記録値から逆算（157.54 -> 157.69, +0.0944%）
                sp_rec = log_1102.get("emaxis-slim-sp500", {})
                sp_bars, _ = daily_series["^GSPC"]
                r_sp_1102, _, _, _, _ = get_series_change_between(sp_bars, {}, "2026-10-30", "2026-11-02", adjust_div=False)
                if sp_rec.get("baseNav") and sp_rec.get("forecastNav"):
                    r_tot_sp = (float(sp_rec["forecastNav"]) / float(sp_rec["baseNav"])) - 1.0
                    r_fx_1102 = (1.0 + r_tot_sp) / (1.0 + r_sp_1102) - 1.0
                else:
                    r_fx_1102 = (157.69 - 157.54) / 157.54

                r_nq_1102, nq_b_d, nq_b_v, nq_c_d, nq_c_v = simulate_us_futures_at_830(
                    "2026-10-30", "2026-11-02", 16, 0,
                    daily_series["NQ=F"][0], m15_series.get("NQ=F", []), hourly_series.get("NQ=F", []),
                    lock_settled=True,
                )
                r_gc_1102, gc_b_d, gc_b_v, gc_c_d, gc_c_v = simulate_us_futures_at_830(
                    "2026-10-30", "2026-11-02", 13, 30,
                    daily_series["GCZ26.CMX"][0], m15_series.get("GCZ26.CMX", []), hourly_series.get("GCZ26.CMX", []),
                    lock_settled=True, align_base_date=nq_b_d,
                )
                print("\n=== 2026-11-02 08:34:41 JST 再現検証（11/2 朝記録 改修前 vs 改修後 vs 実際の基準価額） ===")
                print(
                    f"  NQ=F     : base={nq_b_v:.2f} ({nq_b_d}) -> 改修前 cur=28645.50 (2026-11-01 18:24 EST Globex, -3.10%) / 改修後 cur={nq_c_v:.2f} ({nq_c_d} 16:00 EDT 清算値, {r_nq_1102*100:+.2f}%)"
                )
                print(
                    f"  GCZ26.CMX: base={gc_b_v:.2f} ({gc_b_d}) -> 改修前 cur=3904.40 (2026-11-01 18:24 EST Globex, -3.15%) / 改修後 cur={gc_c_v:.2f} ({gc_c_d} 13:30 EDT 清算値, {r_gc_1102*100:+.2f}%)"
                )
                print(
                    "| ファンド | 前日基準価額 | 改修前予想 (11/2 8:34) | 改修後予想 (再現) | 実際の基準価額 | 改修前誤差 | 改修後誤差 | 改善幅 |"
                )
                print("|---|---:|---:|---:|---:|---:|---:|---:|")
                r_gold_diff = r_gc_1102 - ((3904.4 - gc_b_v) / gc_b_v)
                for fid in us_futures_fund_ids:
                    if fid not in log_1102 or fid not in fund_by_id:
                        continue
                    rec = log_1102[fid]
                    _, f = fund_by_id[fid]
                    b_nav = int(rec["baseNav"])
                    bef_nav = int(rec["forecastNav"])
                    bef_chg = float(rec["forecastChange"])
                    act_nav = int(rec["actualNav"])
                    act_chg = float(rec["actualChange"])
                    bef_err_pt = (bef_nav - act_nav) / b_nav * 100.0
                    lev_f = float(f.get("leverage", 1))
                    if f["model"] == "leveraged_hedged":
                        r_aft = lev_f * r_nq_1102
                    elif f["model"] in ("additive", "leveraged_unhedged"):
                        r_aft = lev_f * r_nq_1102 + r_fx_1102
                    else:
                        r_bef_raw = (bef_nav / b_nav) - 1.0
                        r_aft = r_bef_raw + r_gold_diff
                    aft_nav = int(round(b_nav * (1.0 + r_aft)))
                    aft_chg = (aft_nav - b_nav) / b_nav * 100.0
                    aft_err_pt = (aft_nav - act_nav) / b_nav * 100.0
                    print(
                        f"| {f['name']} | {b_nav:,}円 | {bef_nav:,}円 ({bef_chg:+.2f}%) | "
                        f"**{aft_nav:,}円 ({aft_chg:+.2f}%)** | {act_nav:,}円 ({act_chg:+.2f}%) | "
                        f"{bef_nav - act_nav:+,}円 ({bef_err_pt:+.2f}pt) | "
                        f"**{aft_nav - act_nav:+,}円 ({aft_err_pt:+.2f}pt)** | "
                        f"{abs(aft_err_pt) - abs(bef_err_pt):+.2f}pt |"
                    )
        except Exception as e:
            print(f"[WARN] 11/2 再現検証テーブル出力スキップ: {e}")

    if args.seed_log != "none":
        seed_records: Dict[str, Dict[str, Dict[str, Any]]] = {}
        domestic_fids = {f["id"] for _, _, f in all_funds if is_domestic_snapshot_fund(f)}
        for g in funds_cfg["groups"]:
            gid = g["id"]
            for f in g["funds"]:
                fid = f["id"]
                is_dom = fid in domestic_fids
                h = nav_histories[fid]
                for d0, d1 in seed_day_pairs:
                    if d0 not in h or d1 not in h:
                        continue
                    nav0, act = h[d0], h[d1]
                    fc_use = simulate_fund_day(
                        f, gid, d0, d1, nav0, args.seed_log,
                        daily_series, hourly_series, ttm_map, topix_map, n225_map, msci_acwi_map, msci_kokusai_map,
                        solactive_maps=solactive_maps, m15_series=m15_series,
                        auam_formula=chosen_auam if args.seed_log == "after" else "additive",
                    )
                    if fc_use is None:
                        continue
                    err_use = (fc_use - act) / nav0 * 100.0
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
                        "recordedAt": f"{d1}T14:50:00+09:00" if (is_dom and args.seed_log == "after") else f"{d1}T08:30:00+09:00",
                        "source": "backtest-proxy" if (is_dom and args.seed_log == "after") else "backtest",
                    }

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
                cur_rec = bucket.get(fid, {})
                if fid in domestic_fids and args.seed_log == "after":
                    # 国内株は過去の 8:30 記録（live 含む）をすべて 14:50 相当（backtest-proxy）で作り直す
                    cur_time = str(cur_rec.get("recordedAt") or "")
                    if cur_rec.get("source") == "live" and len(cur_time) >= 16 and cur_time[11:16] >= "14:50":
                        continue
                    bucket[fid] = rec
                else:
                    # 海外ファンドは既に live で記録されたものがあれば上書きしない
                    if cur_rec.get("source") == "live":
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
