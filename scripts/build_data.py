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
from typing import Any, Dict, List, Optional, Set, Tuple

import jpholiday
import requests

JST = datetime.timezone(datetime.timedelta(hours=9))
UTC = datetime.timezone.utc

ROOT_DIR = pathlib.Path(__file__).resolve().parent.parent
FUNDS_JSON_PATH = ROOT_DIR / "funds.json"
DOCS_DIR = ROOT_DIR / "docs"
DATA_JSON_PATH = DOCS_DIR / "data.json"
NAV_CACHE_PATH = DOCS_DIR / "nav_cache.json"
FORECAST_LOG_PATH = DOCS_DIR / "history" / "forecast_log.json"
ANCHOR_CACHE_PATH = ROOT_DIR / ".market_anchor_cache.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

# Yahoo JPY=X (インターバンク Bid) と公示仲値 TTM (インターバンク Mid) の半スプレッド補正幅（円）
YAHOO_FX_BID_TO_MID_SPREAD = 0.05

# 1回の更新内での重複URL取得防止とリクエスト間隔（1秒以上）管理
_LAST_HTTP_TS: float = 0.0
_RUN_HTTP_CACHE: Dict[str, requests.Response] = {}


def reset_http_run_cache() -> None:
    """1回の更新実行ごとにURL重複取得キャッシュを初期化する。"""
    _RUN_HTTP_CACHE.clear()


def http_get(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    min_interval: float = 1.05,
) -> requests.Response:
    """同一更新内で同じURLは1回までに制限し、外部リクエスト間を1秒以上あけてGETする。"""
    global _LAST_HTTP_TS
    if url in _RUN_HTTP_CACHE:
        return _RUN_HTTP_CACHE[url]

    elapsed = time.time() - _LAST_HTTP_TS
    if _LAST_HTTP_TS > 0 and elapsed < min_interval:
        time.sleep(min_interval - elapsed)

    req_headers = {"User-Agent": USER_AGENT}
    if headers:
        req_headers.update(headers)

    resp = requests.get(url, headers=req_headers, timeout=timeout)
    _LAST_HTTP_TS = time.time()
    resp.raise_for_status()
    _RUN_HTTP_CACHE[url] = resp
    return resp


def http_post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 20,
    min_interval: float = 1.05,
) -> requests.Response:
    """同一更新内で同じURL+JSONボディは1回までに制限し、外部リクエスト間を1秒以上あけてPOSTする。"""
    global _LAST_HTTP_TS
    body_str = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    cache_key = f"POST:{url}:{body_str}"
    if cache_key in _RUN_HTTP_CACHE:
        return _RUN_HTTP_CACHE[cache_key]

    elapsed = time.time() - _LAST_HTTP_TS
    if _LAST_HTTP_TS > 0 and elapsed < min_interval:
        time.sleep(min_interval - elapsed)

    req_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if headers:
        req_headers.update(headers)

    resp = requests.post(url, headers=req_headers, data=body_str, timeout=timeout)
    _LAST_HTTP_TS = time.time()
    resp.raise_for_status()
    _RUN_HTTP_CACHE[cache_key] = resp
    return resp


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


def parse_num_str(val: Any) -> Optional[float]:
    """カンマ付き数値文字列を float に変換する（'-' 等は None）。"""
    if val is None:
        return None
    s = str(val).replace(",", "").strip()
    if not s or s in ("-", "--", "---"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_anchor_cache() -> Dict[str, Any]:
    """日次の確定値キャッシュ（仲値・TOPIX過去終値・大証清算値・MSCI終値）を読み込む。"""
    if ANCHOR_CACHE_PATH.exists():
        try:
            data = json.loads(ANCHOR_CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception as e:
            print(f"[WARN] Failed to load anchor cache: {e}", file=sys.stderr)
    return {}


def save_anchor_cache(cache: Dict[str, Any]) -> None:
    """日次の確定値キャッシュを保存する。"""
    try:
        ANCHOR_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except Exception as e:
        print(f"[WARN] Failed to save anchor cache: {e}", file=sys.stderr)


def fetch_fund_nav_csv(isin_cd: str, associ_fund_cd: str) -> Tuple[int, str, List[List[Any]]]:
    """投信総合検索ライブラリーから基準価額CSVを取得し、(最新価額, 最新日付, 直近30日履歴) を返す。"""
    url = (
        "https://toushin-lib.fwg.ne.jp/FdsWeb/FDST030000/csv-file-download"
        f"?isinCd={isin_cd}&associFundCd={associ_fund_cd}"
    )
    resp = http_get(url, timeout=20)
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
    resp = http_get(url, timeout=15)
    data = resp.json()
    res = data.get("chart", {}).get("result")
    if not res:
        raise ValueError(f"Yahoo Finance から結果が返りませんでした: {symbol}")
    return res[0]


def resolve_tz(tz_name: Optional[str], gmtoffset: Optional[int] = None) -> datetime.tzinfo:
    """タイムゾーン名または gmtoffset から tzinfo を返す（Windows tzdata 不在環境にも対応）。"""
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
    """タイムスタンプを取引所の現地日付 ('YYYY-MM-DD') に変換する。"""
    tz = resolve_tz(tz_name, gmtoffset)
    return datetime.datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d")


# ============================================================================
# A: 公示仲値 (MURC TTM) 取得
# ============================================================================

def update_murc_ttm_cache(
    anchor_cache: Dict[str, Any],
    needed_dates: Set[str],
    now_jst: datetime.datetime,
    d1_str: str,
) -> Dict[str, float]:
    """三菱UFJリサーチ&コンサルティングの外国為替相場から USD/JPY 公示仲値 (TTM) を取得する。"""
    ttm_map: Dict[str, float] = anchor_cache.setdefault("murc_ttm", {})

    # 当日ページの確認（必要な日付が未取得、または D1 10:00 JST 以降で D1 仲値が未取得の場合）
    d1_10am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=10, minute=0, tzinfo=JST)
    need_today_page = any(d not in ttm_map for d in needed_dates) or (
        now_jst >= d1_10am and d1_str not in ttm_map
    )

    if need_today_page:
        try:
            resp = http_get("https://www.murc-kawasesouba.jp/fx/index.php", timeout=15)
            txt = resp.content.decode("cp932", errors="replace")
            m_date = re.search(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日現在", txt)
            m_usd = re.search(r"USD</td>\s*<td[^>]*>\s*([0-9.]+)\s*</td>\s*<td[^>]*>\s*([0-9.]+)", txt)
            if m_date and m_usd:
                pub_date = f"{int(m_date.group(1)):04d}-{int(m_date.group(2)):02d}-{int(m_date.group(3)):02d}"
                tts = float(m_usd.group(1))
                ttb = float(m_usd.group(2))
                ttm_map[pub_date] = round((tts + ttb) / 2.0, 4)
        except Exception as e:
            print(f"[WARN] MURC today TTM fetch failed: {e}", file=sys.stderr)

    # 過去営業日の仲値が不足している場合は過去3か月日別ページから補完
    for d_str in sorted(needed_dates):
        if d_str in ttm_map:
            continue
        if d_str > now_jst.strftime("%Y-%m-%d"):
            continue
        ymd = d_str.replace("-", "")[2:]
        url = f"https://www.murc-kawasesouba.jp/fx/past/index.php?id={ymd}"
        try:
            resp = http_get(url, timeout=15)
            txt = resp.content.decode("cp932", errors="replace")
            m_usd = re.search(r"USD</td>\s*<td[^>]*>\s*([0-9.]+)\s*</td>\s*<td[^>]*>\s*([0-9.]+)", txt)
            if m_usd:
                tts = float(m_usd.group(1))
                ttb = float(m_usd.group(2))
                ttm_map[d_str] = round((tts + ttb) / 2.0, 4)
        except Exception as e:
            print(f"[WARN] MURC past TTM fetch failed ({d_str}): {e}", file=sys.stderr)

    return ttm_map


# ============================================================================
# B & D: JPX TOPIX 指数・株探 TOPIX 日足・大阪取引所 先物取得
# ============================================================================

def fetch_jpx_topix_live(anchor_cache: Dict[str, Any], now_jst: datetime.datetime) -> Optional[Dict[str, Any]]:
    """JPX から TOPIX 指数の現在値・当日/前営業日終値を取得する。"""
    topix_daily: Dict[str, float] = anchor_cache.setdefault("topix_daily", {})

    # 1. JPX/QUICK 指数 JSON API（日付・時刻・前日比を含む）
    try:
        url = "https://quote.jpx.co.jp/jpxhp/jcgi/wrap/qjsonp.aspx?F=ctl/index&qcode=151"
        resp = http_get(url, headers={"Referer": "https://quote.jpx.co.jp/"}, timeout=15)
        txt = resp.text.strip()
        if "(" in txt[:20] and txt.endswith(")"):
            txt = txt[txt.find("(") + 1 : txt.rfind(")")]
        data = json.loads(txt)
        q = data.get("section1", {}).get("data", {}).get("151", {})
        price = parse_num_str(q.get("DPP"))
        zxd = parse_jp_date(str(q.get("ZXD", "")))
        dppt = str(q.get("DPPT", "15:30")).strip()
        dywp = parse_num_str(q.get("DYWP"))
        prpt = parse_jp_date(str(q.get("PRPT", "")))
        dps_cnv = str(q.get("DPS_CNV", ""))

        if price and zxd:
            if prpt and dywp is not None:
                topix_daily[prpt] = round(price - dywp, 2)
            if dps_cnv == "終値" or dppt >= "15:30" or zxd < now_jst.strftime("%Y-%m-%d"):
                topix_daily[zxd] = round(price, 2)
            hh, mm = (dppt.split(":") + ["00"])[:2]
            hh_i = int(hh) if hh.isdigit() else 15
            mm_i = int(mm) if mm.isdigit() else 30
            as_of_dt = datetime.datetime.strptime(zxd, "%Y-%m-%d").replace(hour=hh_i, minute=mm_i, tzinfo=JST)
            return {
                "price": price,
                "date": zxd,
                "isClose": (dps_cnv == "終値" or dppt >= "15:30"),
                "asOf": as_of_dt.isoformat(),
            }
    except Exception as e:
        print(f"[WARN] JPX QUICK TOPIX fetch failed: {e}", file=sys.stderr)

    # 2. フォールバック：JPX S3 リアルタイム JSON
    try:
        resp = http_get("https://www.jpx.co.jp/market/indices/indices_stock_price3.txt", timeout=15)
        t_obj = resp.json().get("MainStockIndex", {}).get("Topix", {})
        price = parse_num_str(t_obj.get("currentPrice"))
        t_resp = http_get("https://www.jpx.co.jp/market/indices/indices_stock_price3.time.txt", timeout=15)
        ts_raw = re.sub(r"[^0-9]", "", t_resp.text.strip())
        if price and len(ts_raw) >= 12:
            dt = datetime.datetime.strptime(ts_raw[:12], "%Y%m%d%H%M").replace(tzinfo=JST)
            d_str = dt.strftime("%Y-%m-%d")
            if dt.hour > 15 or (dt.hour == 15 and dt.minute >= 30):
                topix_daily[d_str] = round(price, 2)
            return {
                "price": price,
                "date": d_str,
                "isClose": (dt.hour > 15 or (dt.hour == 15 and dt.minute >= 30)),
                "asOf": dt.isoformat(),
            }
    except Exception as e:
        print(f"[WARN] JPX S3 TOPIX fetch failed: {e}", file=sys.stderr)

    return None


def ensure_topix_history_for_beta(anchor_cache: Dict[str, Any], up_to_d0: str) -> Dict[str, float]:
    """直近60営業日の回帰係数 β 算出に必要な TOPIX 日足終値履歴を確保する。"""
    topix_daily: Dict[str, float] = anchor_cache.setdefault("topix_daily", {})
    valid_count = sum(1 for d in topix_daily if d <= up_to_d0)
    if valid_count >= 65 and up_to_d0 in topix_daily:
        return topix_daily

    # 株探 TOPIX 日足テーブル（1ページ30営業日×3ページ＝約90営業日）で補完
    for page in range(1, 4):
        url = f"https://kabutan.jp/stock/kabuka?code=0010&ashi=day&page={page}"
        try:
            resp = http_get(url, timeout=15)
            txt = resp.content.decode("utf-8", errors="replace")
            for m in re.finditer(r"<time datetime=[\"'](\d{4}-\d{2}-\d{2})[\"']>.*?</time>.*?</tr>", txt, re.S):
                d_str = m.group(1)
                tds = re.findall(r"<td[^>]*>([^<]+)</td>", m.group(0))
                if len(tds) >= 4:
                    c_val = parse_num_str(tds[3])
                    if c_val is not None:
                        topix_daily[d_str] = c_val
        except Exception as e:
            print(f"[WARN] Kabutan TOPIX history fetch failed (page={page}): {e}", file=sys.stderr)
            break

    return topix_daily


def calc_rolling_beta(topix_map: Dict[str, float], n225_map: Dict[str, float], up_to_d0: str, lookback: int = 60) -> float:
    """up_to_d0 までの直近 lookback 営業日の TOPIX と日経平均の日次変化率から回帰係数 β を計算する。"""
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


def resolve_mmdd_to_ymd(mmdd: str, ref_year: int, ref_month: int) -> Optional[str]:
    """'10/30' や '01/05' のような MM/DD を年跨ぎ考慮で 'YYYY-MM-DD' に変換する。"""
    m = re.match(r"^(\d{1,2})/(\d{1,2})$", mmdd.strip())
    if not m:
        return None
    mo, da = int(m.group(1)), int(m.group(2))
    yr = ref_year
    if ref_month == 12 and mo == 1:
        yr += 1
    elif ref_month == 1 and mo == 12:
        yr -= 1
    try:
        return datetime.date(yr, mo, da).strftime("%Y-%m-%d")
    except ValueError:
        return None


def fetch_ose_futures(anchor_cache: Dict[str, Any], now_jst: datetime.datetime) -> Dict[str, Dict[str, Any]]:
    """大阪取引所（JPX/QUICK 日通しAPI）から日経225先物・TOPIX先物の中心限月データを取得する。"""
    ose_settle_cache: Dict[str, Dict[str, float]] = anchor_cache.setdefault("ose_settlements", {})
    out: Dict[str, Dict[str, Any]] = {}

    url = "https://port.jpx.co.jp/jpxhp/jcgi/wrap/qjsonp.aspx?F=ctl/future&DISPTYPE=day_through"
    headers = {"Referer": "https://port.jpx.co.jp/jpxhp/main/index.aspx?F=future&disptype=day_through"}
    try:
        resp = http_get(url, headers=headers, timeout=15)
        txt = resp.text.strip()
        if "(" in txt[:20] and txt.endswith(")"):
            txt = txt[txt.find("(") + 1 : txt.rfind(")")]
        data = json.loads(txt)
        items = data.get("section1", {}).get("data", [])
        target_codes = {"101.555/O": "nikkei225", "151.555/O": "topix"}

        for item in items:
            if not isinstance(item, dict):
                continue
            qcode = str(item.get("qcode", ""))
            if qcode not in target_codes:
                continue
            key = target_codes[qcode]
            contracts = item.get("future", [])
            if not isinstance(contracts, list) or not contracts:
                continue

            # 建玉 (DOI) が最大の中心限月を選択（同数なら先頭限月）
            best_row: Optional[Dict[str, Any]] = None
            best_doi = -1.0
            for row in contracts:
                if not isinstance(row, dict):
                    continue
                st_v = parse_num_str(row.get("ST"))
                dpp_v = parse_num_str(row.get("DPP"))
                if st_v is None and dpp_v is None:
                    continue
                doi_v = parse_num_str(row.get("DOI")) or 0.0
                # 現在値 DPP がある限月を優先しつつ建玉最大を選ぶ
                score = doi_v + (1e9 if dpp_v is not None else 0.0)
                if score > best_doi:
                    best_doi = score
                    best_row = row

            if not best_row:
                continue

            dpp = parse_num_str(best_row.get("DPP"))
            dywp = parse_num_str(best_row.get("DYWP"))
            st_val = parse_num_str(best_row.get("ST"))
            ztd_ymd = resolve_mmdd_to_ymd(str(best_row.get("ZTD", "")), now_jst.year, now_jst.month)
            stt_ymd = resolve_mmdd_to_ymd(str(best_row.get("STT", "")), now_jst.year, now_jst.month)
            dpp_ymd = resolve_mmdd_to_ymd(str(best_row.get("DPP_H", "")), now_jst.year, now_jst.month)
            dppt = str(best_row.get("DPPT", "")).strip()

            if st_val is not None and stt_ymd:
                ose_settle_cache.setdefault(key, {})[stt_ymd] = st_val

            as_of_iso = now_jst.isoformat(timespec="seconds")
            if dpp_ymd and ":" in dppt:
                hh, mm = (dppt.split(":") + ["00"])[:2]
                if hh.isdigit() and mm.isdigit():
                    as_of_iso = datetime.datetime.strptime(dpp_ymd, "%Y-%m-%d").replace(
                        hour=int(hh), minute=int(mm), tzinfo=JST
                    ).isoformat()

            out[key] = {
                "contract": str(best_row.get("DELI", "")),
                "cur": dpp,
                "dywp": dywp,
                "base1530": (dpp - dywp) if (dpp is not None and dywp is not None) else st_val,
                "settle": st_val,
                "settleDate": stt_ymd,
                "tradingDate": ztd_ymd,
                "asOf": as_of_iso,
            }
    except Exception as e:
        print(f"[WARN] OSE futures fetch failed: {e}", file=sys.stderr)

    return out


# ============================================================================
# C: MSCI 公式指数 (ACWI: 892400, KOKUSAI: 991200, USD GRTR) 取得
# ============================================================================

def update_msci_cache(
    anchor_cache: Dict[str, Any],
    now_jst: datetime.datetime,
    rep_d1_str: str,
) -> Dict[str, Dict[str, float]]:
    """MSCI 公式 End-of-Day API から ACWI (892400) と KOKUSAI (991200) の USD GRTR 終値を取得する。"""
    msci_store: Dict[str, Any] = anchor_cache.setdefault("msci_grtr", {})
    acwi_map: Dict[str, float] = msci_store.setdefault("892400", {})
    koku_map: Dict[str, float] = msci_store.setdefault("991200", {})

    # D1 より前の最新米国営業日（土日を除く直近日）が既にキャッシュ済みなら再取得を抑制
    d_check = datetime.datetime.strptime(rep_d1_str, "%Y-%m-%d").date() - datetime.timedelta(days=1)
    while d_check.weekday() >= 5:
        d_check -= datetime.timedelta(days=1)
    expected_us_date = d_check.strftime("%Y-%m-%d")

    need_fetch = (expected_us_date not in acwi_map) or (expected_us_date not in koku_map)
    if not need_fetch:
        return {"892400": acwi_map, "991200": koku_map}

    # 直近30分以内に取得試行済みならキャッシュを使う
    last_fetched_str = msci_store.get("fetchedAt")
    if last_fetched_str and acwi_map and koku_map:
        try:
            last_dt = datetime.datetime.fromisoformat(last_fetched_str)
            if (now_jst - last_dt).total_seconds() < 1800:
                return {"892400": acwi_map, "991200": koku_map}
        except Exception:
            pass

    start_ymd = (now_jst.date() - datetime.timedelta(days=25)).strftime("%Y%m%d")
    end_ymd = now_jst.date().strftime("%Y%m%d")

    for code, target_map in (("892400", acwi_map), ("991200", koku_map)):
        url = (
            "https://app2.msci.com/products/service/index/indexmaster/getLevelDataForGraph"
            f"?currency_symbol=USD&index_variant=GRTR&start_date={start_ymd}&end_date={end_ymd}"
            f"&data_frequency=DAILY&index_codes={code}"
        )
        try:
            resp = http_get(url, timeout=20)
            levels = resp.json().get("indexes", {}).get("INDEX_LEVELS", [])
            for row in levels:
                cd = str(row.get("calc_date", ""))
                if len(cd) == 8:
                    d_str = f"{cd[:4]}-{cd[4:6]}-{cd[6:8]}"
                    target_map[d_str] = float(row["level_eod"])
        except Exception as e:
            print(f"[WARN] MSCI index fetch failed (code={code}): {e}", file=sys.stderr)

    msci_store["fetchedAt"] = now_jst.isoformat(timespec="seconds")
    return {"892400": acwi_map, "991200": koku_map}


# ============================================================================
# I: Solactive 公式指数 (US Growth Mega 10 Select Index NTR: DE000SL0R5Q9) 取得
# ============================================================================

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
            if len(v) >= 2 and v[0] == "Date" and isinstance(v[1], str):
                res: Any = v[1]
            else:
                res = [resolve(x) if isinstance(x, int) else x for x in v]
            memo[idx] = res
            return res
        res_dict = {k: (resolve(val) if isinstance(val, int) else val) for k, val in v.items()}
        memo[idx] = res_dict
        return res_dict

    return resolve(0)


def update_solactive_cache(
    anchor_cache: Dict[str, Any],
    isins: Set[str],
    now_jst: datetime.datetime,
    rep_d1_str: str,
) -> Dict[str, Dict[str, Any]]:
    """Solactive 公式エンドポイントから指定 ISIN の日足終値および米国取引時間中のリアルタイム値を取得する。"""
    sol_store: Dict[str, Any] = anchor_cache.setdefault("solactive", {})
    out: Dict[str, Dict[str, Any]] = {}
    if not isins:
        return out

    d_check = datetime.datetime.strptime(rep_d1_str, "%Y-%m-%d").date() - datetime.timedelta(days=1)
    while d_check.weekday() >= 5:
        d_check -= datetime.timedelta(days=1)
    expected_us_date = d_check.strftime("%Y-%m-%d")

    tz_et = resolve_tz("America/New_York")
    now_et = now_jst.astimezone(tz_et)
    in_us_cash_hours = (
        now_et.weekday() < 5
        and ((now_et.hour == 9 and now_et.minute >= 30) or (10 <= now_et.hour < 17))
    )

    now_ms = int(now_jst.timestamp() * 1000)
    start_45d_ms = int((now_jst.timestamp() - 86400 * 45) * 1000)

    for isin in sorted(isins):
        entry: Dict[str, Any] = sol_store.setdefault(isin, {})
        daily_map: Dict[str, float] = entry.setdefault("daily", {})

        need_daily = expected_us_date not in daily_map
        last_fetched_str = entry.get("fetchedAt")
        if need_daily and last_fetched_str and daily_map:
            try:
                last_dt = datetime.datetime.fromisoformat(str(last_fetched_str))
                if (now_jst - last_dt).total_seconds() < 1800:
                    need_daily = False
            except Exception:
                pass

        if need_daily:
            try:
                resp = http_post_json(
                    "https://www.solactive.com/_actions/getDayHistoryChartData/",
                    {"isin": isin, "indexCreatingTimeStamp": start_45d_ms, "dayDate": now_ms},
                    timeout=20,
                )
                rows = decode_astro_devalue(resp.json())
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("timestamp") is not None and row.get("value") is not None:
                            # 00:00 CET/CEST (UTC+1/+2) のタイムスタンプに +6h して暦日 YYYY-MM-DD を得る
                            ts_sec = float(row["timestamp"]) / 1000.0
                            d_str = datetime.datetime.fromtimestamp(ts_sec + 6 * 3600, UTC).strftime("%Y-%m-%d")
                            daily_map[d_str] = float(row["value"])
                entry["fetchedAt"] = now_jst.isoformat(timespec="seconds")
            except Exception as e:
                print(f"[WARN] Solactive daily fetch failed ({isin}): {e}", file=sys.stderr)

        # 米国市場取引時間中で当日（D1より前）の日足が未確定の場合のみ分足APIから最新値を補完
        live_info: Optional[Dict[str, Any]] = None
        us_today_str = now_et.strftime("%Y-%m-%d")
        if in_us_cash_hours and us_today_str < rep_d1_str and us_today_str not in daily_map:
            try:
                start_18h_ms = int((now_jst.timestamp() - 3600 * 18) * 1000)
                resp_m = http_post_json(
                    "https://www.solactive.com/_actions/getMinuteHistoryChartData/",
                    {"isin": isin, "startOfDayCET": start_18h_ms, "dayDate": now_ms},
                    timeout=20,
                )
                m_rows = decode_astro_devalue(resp_m.json())
                if isinstance(m_rows, list) and m_rows:
                    last_r = m_rows[-1]
                    if isinstance(last_r, dict) and last_r.get("timestamp") and last_r.get("value"):
                        ts_s = float(last_r["timestamp"]) / 1000.0
                        live_info = {
                            "price": float(last_r["value"]),
                            "date": datetime.datetime.fromtimestamp(ts_s, tz_et).strftime("%Y-%m-%d"),
                            "asOf": datetime.datetime.fromtimestamp(ts_s, JST).isoformat(timespec="seconds"),
                        }
                        entry["live"] = live_info
            except Exception as e:
                print(f"[WARN] Solactive intraday fetch failed ({isin}): {e}", file=sys.stderr)

        out[isin] = {"daily": daily_map, "live": live_info}

    return out


# ============================================================================
# F: COMEX 金先物 中心限月（偶数月限月）解決
# ============================================================================

def resolve_active_comex_gold_symbol(anchor_cache: Dict[str, Any], now_jst: datetime.datetime) -> str:
    """COMEX 金先物の出来高最大の偶数月中心限月（例: GCZ26.CMX）を解決し、異限月混入を防ぐ。"""
    gold_cache = anchor_cache.get("active_gold", {})
    if isinstance(gold_cache, dict) and gold_cache.get("symbol") and gold_cache.get("checkedAt"):
        try:
            checked_dt = datetime.datetime.fromisoformat(gold_cache["checkedAt"])
            if (now_jst - checked_dt).total_seconds() < 3600 * 12:
                return str(gold_cache["symbol"])
        except Exception:
            pass

    # COMEX 金の主力偶数月限月コード: 2月=G, 4月=J, 6月=M, 8月=Q, 10月=V, 12月=Z
    even_months = [(2, "G"), (4, "J"), (6, "M"), (8, "Q"), (10, "V"), (12, "Z")]
    candidates: List[str] = []
    yr = now_jst.year
    mo = now_jst.month
    for offset_yr in (0, 1):
        for m_num, m_code in even_months:
            y_val = yr + offset_yr
            if y_val == yr and m_num < mo:
                continue
            candidates.append(f"GC{m_code}{str(y_val)[-2:]}.CMX")
            if len(candidates) >= 2:
                break
        if len(candidates) >= 2:
            break

    best_sym = "GC=F"
    best_vol = -1.0
    for cand in candidates:
        try:
            ch = fetch_yahoo_chart(cand, range_str="10d", interval="1d", events=False)
            vol = float(ch.get("meta", {}).get("regularMarketVolume") or 0.0)
            if vol > best_vol:
                best_vol = vol
                best_sym = cand
        except Exception:
            continue

    anchor_cache["active_gold"] = {
        "symbol": best_sym,
        "checkedAt": now_jst.isoformat(timespec="seconds"),
    }
    return best_sym


# ============================================================================
# Yahoo Finance 市場データ一括取得
# ============================================================================

def build_market_cache(symbols: List[str], active_gold_sym: str) -> Dict[str, Dict[str, Any]]:
    """必要な全シンボルの日足・15分足データを一括取得する。"""
    cache: Dict[str, Dict[str, Any]] = {}
    intraday_symbols = {"JPY=X", "NIY=F", "^N225", "1306.T", "1540.T", "NQ=F", "GC=F", active_gold_sym}
    div_symbols = {"SCHD", "VTI", "VOO", "QQQ", "VT", "ACWI", "TOK", "GLD", "IAU", "GLDM"}

    all_syms = set(symbols) | {active_gold_sym}
    for sym in sorted(all_syms):
        if not sym or sym.startswith("MSCI:") or sym.startswith("SOLACTIVE:") or sym == "TOPIX":
            continue
        entry: Dict[str, Any] = {}
        # ^N225 は直近60営業日の回帰係数 β 計算にも使うため 6mo 日足を取得
        d_range = "6mo" if sym == "^N225" else "15d"
        entry["daily"] = fetch_yahoo_chart(sym, range_str=d_range, interval="1d", events=(sym in div_symbols))
        if sym in intraday_symbols:
            try:
                entry["m15"] = fetch_yahoo_chart(sym, range_str="10d", interval="15m", events=False)
            except Exception as e:
                print(f"[WARN] 15m chart fetch failed for {sym}: {e}", file=sys.stderr)
        cache[sym] = entry
    return cache


# ============================================================================
# 各資産クラスの騰落率計算ロジック
# ============================================================================

def get_fx_data(
    market_cache: Dict[str, Dict[str, Any]],
    ttm_map: Dict[str, float],
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """ドル円 (USD/JPY) の起点（D0 公示仲値 TTM 優先）と現在値を計算する。"""
    jpy_entry = market_cache["JPY=X"]
    daily = jpy_entry["daily"]
    meta = daily["meta"]
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()
    m15 = jpy_entry.get("m15")

    d0_10am = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=10, minute=0, second=0, tzinfo=JST)
    d1_10am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=10, minute=0, second=0, tzinfo=JST)

    bars_15m: List[Tuple[int, float]] = []
    if m15 and "timestamp" in m15:
        ts_list = m15["timestamp"]
        cl_list = m15["indicators"]["quote"][0]["close"]
        bars_15m = [(int(t), float(c)) for t, c in zip(ts_list, cl_list) if c is not None]

    # 1. 起点 (base): D0 の公示仲値 (TTM) を優先し、取れない日のみ Yahoo 10:00 足へフォールバック
    base_val: Optional[float] = ttm_map.get(d0_str)
    estimated_base = False
    if base_val is None:
        estimated_base = True
        if bars_15m:
            d0_target_ts = int((d0_10am - datetime.timedelta(minutes=15)).timestamp())
            best_d0 = min(bars_15m, key=lambda x: abs(x[0] - d0_target_ts))
            if abs(best_d0[0] - d0_target_ts) <= 3600 * 6:
                base_val = best_d0[1]
        if base_val is None:
            ts_list = daily.get("timestamp", [])
            cl_list = daily["indicators"]["quote"][0]["close"]
            valid = [(get_bar_local_date(t, "Asia/Tokyo"), float(c)) for t, c in zip(ts_list, cl_list) if c is not None]
            before_d0 = [c for d, c in valid if d <= d0_str]
            base_val = before_d0[-1] if before_d0 else reg_price

    # 2. 現在値 (cur): D1 の公示仲値が既に公表済みならそれを使用し、未公表時は Yahoo スポット値を Mid 補正して使用
    cur_val: float = reg_price
    estimated = True
    cur_is_yahoo_bid = True

    if now_jst >= d1_10am and d1_str in ttm_map:
        cur_val = ttm_map[d1_str]
        estimated = False
        cur_is_yahoo_bid = False
        as_of_iso = d1_10am.isoformat()
    elif now_jst >= d1_10am and bars_15m:
        d1_target_ts = int((d1_10am - datetime.timedelta(minutes=15)).timestamp())
        best_d1 = min(bars_15m, key=lambda x: abs(x[0] - d1_target_ts))
        if abs(best_d1[0] - d1_target_ts) <= 3600 * 2:
            cur_val = best_d1[1]

    bid_mid_adjusted = False
    if (not estimated_base) and cur_is_yahoo_bid:
        cur_val = cur_val + YAHOO_FX_BID_TO_MID_SPREAD
        bid_mid_adjusted = True

    r_fx = (cur_val - base_val) / base_val if base_val else 0.0
    return {
        "symbol": "JPY=X",
        "base": round(base_val, 2),
        "cur": round(cur_val, 2),
        "rawChange": r_fx,
        "change": round(r_fx * 100.0, 2),
        "estimated": estimated,
        "estimatedBase": estimated_base,
        "bidMidAdjusted": bid_mid_adjusted,
        "asOf": as_of_iso,
    }


def get_overseas_index_change(
    market_cache: Dict[str, Dict[str, Any]],
    symbol: str,
    d0_str: str,
    d1_str: Optional[str] = None,
    adjust_div: bool = False,
) -> Dict[str, Any]:
    """海外指数・ETFの D0 より前の最終終値 (base) と D1 より前の最新値 (cur) から変化率を計算する。"""
    entry = market_cache[symbol]
    daily = entry["daily"]
    meta = daily["meta"]
    tz_name = meta.get("exchangeTimezoneName", "America/New_York")
    gmtoffset = meta.get("gmtoffset")
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(time.time())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()

    ts_list = daily.get("timestamp", [])
    cl_list = daily["indicators"]["quote"][0]["close"]
    dedup: Dict[str, float] = {}
    for t, c in zip(ts_list, cl_list):
        if c is not None:
            dedup[get_bar_local_date(int(t), tz_name, gmtoffset)] = float(c)
    bars = sorted(dedup.items())

    before_d0 = [(d, c) for d, c in bars if d < d0_str]
    base_date, base_val = (before_d0[-1] if before_d0 else (bars[-1] if bars else (d0_str, reg_price)))

    eligible_bars = [(d, c) for d, c in bars if (d1_str is None or d < d1_str)]
    latest_bar_date = eligible_bars[-1][0] if eligible_bars else base_date
    reg_local_date = get_bar_local_date(reg_time, tz_name, gmtoffset)

    if d1_str is not None and reg_local_date >= d1_str:
        # D1 当日夜の海外市場が既に開いている場合は D1 より前の確定終値で固定
        cur_date = latest_bar_date
        cur_price_candidate = eligible_bars[-1][1] if eligible_bars else base_val
    else:
        cur_date = max(latest_bar_date, reg_local_date)
        cur_price_candidate = reg_price

    if cur_date <= base_date:
        cur_val = base_val
        div_adj = 0.0
        r_idx = 0.0
    else:
        cur_val = cur_price_candidate
        div_adj = 0.0
        if adjust_div:
            divs = daily.get("events", {}).get("dividends", {})
            for _, div_obj in divs.items():
                div_ts = int(div_obj.get("date", 0))
                div_date = get_bar_local_date(div_ts, tz_name, gmtoffset)
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


def get_us_futures_change(
    market_cache: Dict[str, Dict[str, Any]],
    symbol: str,
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
    settle_hour_et: int,
    settle_min_et: int,
    align_base_date: Optional[str] = None,
    display_symbol: Optional[str] = None,
) -> Dict[str, Any]:
    """米国先物（NQ=F: 16:00 ET 清算、金先物: 13:30 ET 清算）の清算値基準変化率を計算する。"""
    actual_sym = symbol if symbol in market_cache else "GC=F"
    entry = market_cache[actual_sym]
    daily = entry["daily"]
    meta = daily["meta"]
    tz_name = meta.get("exchangeTimezoneName", "America/New_York")
    gmtoffset = meta.get("gmtoffset")
    tz_et = resolve_tz(tz_name, gmtoffset)
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()

    # 日足マップ (local_date -> close)
    daily_map: Dict[str, float] = {}
    for t, c in zip(daily.get("timestamp", []), daily["indicators"]["quote"][0]["close"]):
        if c is not None:
            daily_map[get_bar_local_date(int(t), tz_name, gmtoffset)] = float(c)
    sorted_daily = sorted(daily_map.items())

    # 15分足から各営業日の清算時刻終了足（15:45 ET または 13:15 ET）と Globex 引け足（16:45 ET）を抽出
    settle_bar_start_dt = (
        datetime.datetime(2000, 1, 1, settle_hour_et, settle_min_et) - datetime.timedelta(minutes=15)
    )
    settle_hhmm = settle_bar_start_dt.strftime("%H:%M")
    settle_15m_map: Dict[str, Tuple[float, int]] = {}
    globex_1645_map: Dict[str, float] = {}

    m15 = entry.get("m15")
    if m15 and "timestamp" in m15:
        for t, c in zip(m15["timestamp"], m15["indicators"]["quote"][0]["close"]):
            if c is None:
                continue
            dt_et = datetime.datetime.fromtimestamp(int(t), tz_et)
            d_et = dt_et.strftime("%Y-%m-%d")
            hm_et = dt_et.strftime("%H:%M")
            if hm_et == settle_hhmm:
                settle_15m_map[d_et] = (float(c), int(t) + 900)
            elif hm_et == "16:45":
                globex_1645_map[d_et] = float(c)

    def resolve_settled_close(d_et: str, d_close: float) -> float:
        """日足 close がまだ 17:00 ET 引け値（未確定）の場合のみ清算時刻15分足で補完し、確定済みなら日足清算値を返す。"""
        g1645 = globex_1645_map.get(d_et)
        s15 = settle_15m_map.get(d_et)
        if g1645 is not None and s15 is not None:
            if abs(d_close - g1645) <= 1e-2 and abs(d_close - s15[0]) > 1e-2:
                return s15[0]
        return d_close

    # 1. 起点 (base): 株式と同じ米国取引日（align_base_date）または D0 より前の最終営業日の清算値
    before_d0 = [(d, c) for d, c in sorted_daily if d < d0_str]
    if align_base_date and align_base_date in daily_map:
        base_date = align_base_date
        base_raw = daily_map[align_base_date]
    elif before_d0:
        base_date, base_raw = before_d0[-1]
    else:
        base_date, base_raw = (sorted_daily[-1] if sorted_daily else (d0_str, reg_price))
    base_val = resolve_settled_close(base_date, base_raw)

    # 2. 現在値 (cur): D1 より前の最新米国取引日の清算時刻前なら現在値、清算時刻経過後はその日の清算値で固定
    candidate_dates = {d for d, _ in sorted_daily if d < d1_str} | {d for d in settle_15m_map.keys() if d < d1_str}
    now_et = now_jst.astimezone(tz_et)
    reg_dt_et = datetime.datetime.fromtimestamp(reg_time, tz_et)
    # 18:00 ET 以降は翌営業日の Globex セッションなので現地日付にそのまま入れない
    if reg_dt_et.hour < 18 and reg_dt_et.strftime("%Y-%m-%d") < d1_str:
        candidate_dates.add(reg_dt_et.strftime("%Y-%m-%d"))

    cur_date = max(candidate_dates) if candidate_dates else base_date

    if cur_date <= base_date:
        cur_val = base_val
        r_idx = 0.0
    else:
        cutoff_et = datetime.datetime.strptime(cur_date, "%Y-%m-%d").replace(
            hour=settle_hour_et, minute=settle_min_et, second=0, tzinfo=tz_et
        )
        if now_et < cutoff_et:
            cur_val = reg_price
        else:
            if cur_date in daily_map:
                cur_val = resolve_settled_close(cur_date, daily_map[cur_date])
                if cur_date in settle_15m_map and cur_val == settle_15m_map[cur_date][0]:
                    as_of_iso = datetime.datetime.fromtimestamp(settle_15m_map[cur_date][1], JST).isoformat()
                else:
                    as_of_iso = cutoff_et.astimezone(JST).isoformat()
            elif cur_date in settle_15m_map:
                cur_val = settle_15m_map[cur_date][0]
                as_of_iso = datetime.datetime.fromtimestamp(settle_15m_map[cur_date][1], JST).isoformat()
            else:
                cur_val = reg_price
        r_idx = (cur_val - base_val) / base_val if base_val else 0.0

    return {
        "symbol": display_symbol or actual_sym,
        "contractSymbol": actual_sym,
        "base": round(base_val, 2),
        "baseDate": base_date,
        "cur": round(cur_val, 2),
        "curDate": cur_date,
        "rawChange": r_idx,
        "change": round(r_idx * 100.0, 2),
        "asOf": as_of_iso,
    }


def get_msci_index_change(
    msci_series: Dict[str, float],
    msci_label: str,
    etf_symbol: str,
    market_cache: Dict[str, Dict[str, Any]],
    d0_str: str,
    d1_str: str,
) -> Dict[str, Any]:
    """公表済み MSCI 指数 (USD GRTR) の変化率を使い、未公表日分がある場合のみ ETF (ACWI/TOK) で補完する。"""
    etf_info = get_overseas_index_change(market_cache, etf_symbol, d0_str, d1_str=d1_str, adjust_div=True)
    if not msci_series:
        return etf_info

    sorted_msci = sorted(msci_series.items())
    before_d0 = [(d, v) for d, v in sorted_msci if d < d0_str]
    before_d1 = [(d, v) for d, v in sorted_msci if d < d1_str]
    if not before_d0 or not before_d1:
        return etf_info

    base_date, base_val = before_d0[-1]
    msci_cur_date, msci_cur_val = before_d1[-1]
    r_msci = ((msci_cur_val - base_val) / base_val) if (msci_cur_date > base_date and base_val > 0) else 0.0

    # ETF 側に MSCI 最新公表日より新しい取引日データがある場合は未公表分だけ ETF 変化率を掛ける
    etf_cur_date = etf_info["curDate"]
    etf_supplement: Optional[Dict[str, Any]] = None
    if etf_cur_date > msci_cur_date:
        # msci_cur_date 当日の ETF 終値を起点にして etf_cur_date までの変化率を算出
        next_day_after_msci = (
            datetime.datetime.strptime(msci_cur_date, "%Y-%m-%d").date() + datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")
        rem_info = get_overseas_index_change(
            market_cache, etf_symbol, next_day_after_msci, d1_str=d1_str, adjust_div=True
        )
        r_rem = rem_info["rawChange"]
        r_total_idx = (1.0 + r_msci) * (1.0 + r_rem) - 1.0
        cur_synth = base_val * (1.0 + r_total_idx)
        etf_supplement = {
            "symbol": etf_symbol,
            "base": rem_info["base"],
            "cur": rem_info["cur"],
            "change": rem_info["change"],
        }
        as_of_iso = rem_info["asOf"]
        final_cur_date = etf_cur_date
    else:
        r_total_idx = r_msci
        cur_synth = msci_cur_val
        as_of_iso = f"{msci_cur_date}T06:00:00+09:00"
        final_cur_date = msci_cur_date

    res: Dict[str, Any] = {
        "symbol": msci_label,
        "base": round(base_val, 2),
        "baseDate": base_date,
        "cur": round(cur_synth, 2),
        "curDate": final_cur_date,
        "rawChange": r_total_idx,
        "change": round(r_total_idx * 100.0, 2),
        "asOf": as_of_iso,
    }
    if etf_supplement:
        res["etfSupplement"] = etf_supplement
    return res


def get_solactive_index_change(
    sol_entry: Dict[str, Any],
    isin: str,
    label: str,
    d0_str: str,
    d1_str: str,
) -> Dict[str, Any]:
    """Solactive 公式指数の D0 より前の最終終値と D1 より前の最新値から変化率を計算する。"""
    daily_map: Dict[str, float] = sol_entry.get("daily", {}) if sol_entry else {}
    if not daily_map:
        raise ValueError(f"Solactive 指数データが空です: {isin}")

    sorted_bars = sorted(daily_map.items())
    before_d0 = [(d, v) for d, v in sorted_bars if d < d0_str]
    before_d1 = [(d, v) for d, v in sorted_bars if d < d1_str]
    if not before_d0 or not before_d1:
        raise ValueError(f"Solactive 指数の対象日付が不足しています: {isin}")

    base_date, base_val = before_d0[-1]
    cur_date, cur_val = before_d1[-1]
    as_of_iso = f"{cur_date}T06:00:00+09:00"

    live = sol_entry.get("live")
    if isinstance(live, dict) and live.get("price") and live.get("date"):
        l_date = str(live["date"])
        if base_date < l_date < d1_str and l_date >= cur_date:
            cur_date = l_date
            cur_val = float(live["price"])
            as_of_iso = str(live.get("asOf") or as_of_iso)

    r_idx = ((cur_val - base_val) / base_val) if (cur_date > base_date and base_val > 0) else 0.0
    return {
        "symbol": label,
        "base": round(base_val, 2),
        "baseDate": base_date,
        "cur": round(cur_val, 2),
        "curDate": cur_date,
        "rawChange": r_idx,
        "change": round(r_idx * 100.0, 2),
        "asOf": as_of_iso,
    }


def get_cme_niy_futures_change(
    market_cache: Dict[str, Dict[str, Any]],
    d0_str: str,
    now_jst: datetime.datetime,
) -> Optional[Dict[str, Any]]:
    """大証先物が取得できない場合のフォールバックとして CME 日経先物 (NIY=F) の D0 15:30 比変化率を返す。"""
    fut_entry = market_cache.get("NIY=F")
    if not fut_entry:
        return None
    fut_daily = fut_entry["daily"]
    fut_cur = float(fut_daily["meta"]["regularMarketPrice"])
    fut_time = int(fut_daily["meta"].get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(fut_time, JST).isoformat()
    fut_m15 = fut_entry.get("m15")
    if not fut_m15 or "timestamp" not in fut_m15:
        return None

    target_1530 = datetime.datetime.strptime(d0_str, "%Y-%m-%d").replace(hour=15, minute=30, second=0, tzinfo=JST)
    target_ts = int(target_1530.timestamp())
    f_bars = [
        (int(t), float(c))
        for t, c in zip(fut_m15["timestamp"], fut_m15["indicators"]["quote"][0]["close"])
        if c is not None
    ]
    if not f_bars:
        return None
    best_f = min(f_bars, key=lambda x: abs(x[0] - target_ts))
    if abs(best_f[0] - target_ts) > 3600 * 4 or best_f[1] <= 0:
        return None

    fut_1530 = best_f[1]
    r_fut = (fut_cur - fut_1530) / fut_1530
    return {
        "symbol": "NIY=F",
        "base": round(fut_1530, 2),
        "cur": round(fut_cur, 2),
        "rawChange": r_fut,
        "change": round(r_fut * 100.0, 2),
        "asOf": as_of_iso,
    }


def get_ose_session_futures_change(
    ose_futures: Dict[str, Dict[str, Any]],
    key: str,
    label: str,
    d0_str: str,
    d1_str: str,
) -> Optional[Dict[str, Any]]:
    """大証先物（nikkei225 または topix）の 15:30（日中終値）からの変化率を返す。"""
    f_obj = ose_futures.get(key)
    if not f_obj:
        return None
    t_date = f_obj.get("tradingDate")
    s_date = f_obj.get("settleDate")
    cur_v = f_obj.get("cur")
    base_v = f_obj.get("base1530")

    # 夜間取引開始前（まだ D0 日中セッション表示中）の場合は変化率 0
    if t_date == d0_str:
        val = cur_v or f_obj.get("settle")
        if val and val > 0:
            return {
                "symbol": label,
                "base": round(val, 2),
                "cur": round(val, 2),
                "rawChange": 0.0,
                "change": 0.0,
                "asOf": f_obj["asOf"],
            }
        return None

    # D1 セッション（夜間〜D1 日中）の取引値がある場合
    if (t_date == d1_str or s_date == d0_str) and cur_v and base_v and base_v > 0:
        r_fut = (cur_v - base_v) / base_v
        return {
            "symbol": label,
            "base": round(base_v, 2),
            "cur": round(cur_v, 2),
            "rawChange": r_fut,
            "change": round(r_fut * 100.0, 2),
            "asOf": f_obj["asOf"],
        }
    return None


def get_nikkei_index_change(
    market_cache: Dict[str, Dict[str, Any]],
    ose_futures: Dict[str, Dict[str, Any]],
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """日経平均 (^N225) の D0 終値からの変化率を計算する（夜間は大証日経225先物を優先し NIY=F にフォールバック）。"""
    entry = market_cache["^N225"]
    daily = entry["daily"]
    meta = daily["meta"]
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()
    reg_date_jst = get_bar_local_date(reg_time, "Asia/Tokyo")

    bars_by_date: Dict[str, float] = {}
    for t, c in zip(daily.get("timestamp", []), daily["indicators"]["quote"][0]["close"]):
        if c is not None:
            bars_by_date[get_bar_local_date(int(t), "Asia/Tokyo")] = float(c)

    base_val = bars_by_date.get(d0_str)
    if base_val is None:
        m15 = entry.get("m15")
        if m15 and "timestamp" in m15:
            d0_closes = [
                float(c)
                for t, c in zip(m15["timestamp"], m15["indicators"]["quote"][0]["close"])
                if c is not None and get_bar_local_date(int(t), "Asia/Tokyo") == d0_str
            ]
            if d0_closes:
                base_val = d0_closes[-1]
        if base_val is None:
            base_val = reg_price

    today_str = now_jst.strftime("%Y-%m-%d")
    # D1 当日 9:00 以降（日中取引中および 15:30 大引け後〜基準価額公表前）は現物指数を使用
    use_cash_d1 = (
        today_str >= d1_str
        and now_jst.hour >= 9
        and (reg_date_jst >= d1_str or d1_str in bars_by_date)
    )

    cur_val = base_val
    futures_info: Optional[Dict[str, Any]] = None

    if use_cash_d1:
        cur_val = bars_by_date.get(d1_str, reg_price) if now_jst.hour >= 16 else reg_price
    else:
        ose_nk = get_ose_session_futures_change(ose_futures, "nikkei225", "日経225先物(大証)", d0_str, d1_str)
        fut = ose_nk or get_cme_niy_futures_change(market_cache, d0_str, now_jst)
        if fut:
            cur_val = base_val * (1.0 + fut["rawChange"])
            as_of_iso = fut["asOf"]
            futures_info = {
                "symbol": fut["symbol"],
                "base": fut["base"],
                "cur": fut["cur"],
                "change": fut["change"],
            }

    r_idx = (cur_val - base_val) / base_val if base_val else 0.0
    res: Dict[str, Any] = {
        "symbol": "^N225",
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


def get_nikkei_leveraged_change(
    market_cache: Dict[str, Dict[str, Any]],
    ose_futures: Dict[str, Dict[str, Any]],
    anchor_cache: Dict[str, Any],
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """4.3倍ブル向けに大証日経225先物の D0 公式清算値からの変化率を計算する（取得不可時は日経変化率へフォールバック）。"""
    f_obj = ose_futures.get("nikkei225")
    cached_settles = anchor_cache.get("ose_settlements", {}).get("nikkei225", {})

    base_st: Optional[float] = None
    if f_obj and f_obj.get("settleDate") == d0_str and f_obj.get("settle"):
        base_st = float(f_obj["settle"])
    elif d0_str in cached_settles:
        base_st = float(cached_settles[d0_str])

    cur_fut: Optional[float] = None
    as_of_iso = now_jst.isoformat(timespec="seconds")
    if f_obj:
        as_of_iso = f_obj["asOf"]
        if f_obj.get("settleDate") == d1_str and f_obj.get("settle"):
            # D1 夕方に D1 清算値が確定済みの場合
            cur_fut = float(f_obj["settle"])
        elif f_obj.get("tradingDate") == d1_str and f_obj.get("cur"):
            cur_fut = float(f_obj["cur"])
        elif f_obj.get("tradingDate") == d0_str and base_st:
            cur_fut = base_st

    if base_st and cur_fut and base_st > 0:
        r_idx = (cur_fut - base_st) / base_st
        return {
            "symbol": "日経225先物(清算値基準)",
            "base": round(base_st, 2),
            "baseDate": d0_str,
            "cur": round(cur_fut, 2),
            "rawChange": r_idx,
            "change": round(r_idx * 100.0, 2),
            "asOf": as_of_iso,
        }

    return get_nikkei_index_change(market_cache, ose_futures, d0_str, d1_str, now_jst)


def get_topix_index_change(
    market_cache: Dict[str, Dict[str, Any]],
    jpx_topix_live: Optional[Dict[str, Any]],
    topix_daily: Dict[str, float],
    ose_futures: Dict[str, Dict[str, Any]],
    topix_beta: float,
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """TOPIX 指数の D0 公式終値からの変化率を計算する（日中: 指数現在値/1306.T、夜間: 大証TOPIX先物/β×日経先物）。"""
    # 1306.T フォールバック用データ
    etf_entry = market_cache["1306.T"]
    etf_daily = etf_entry["daily"]
    etf_reg = float(etf_daily["meta"]["regularMarketPrice"])
    etf_time = int(etf_daily["meta"].get("regularMarketTime", int(now_jst.timestamp())))
    etf_as_of = datetime.datetime.fromtimestamp(etf_time, JST).isoformat()
    etf_bars: Dict[str, float] = {}
    for t, c in zip(etf_daily.get("timestamp", []), etf_daily["indicators"]["quote"][0]["close"]):
        if c is not None:
            etf_bars[get_bar_local_date(int(t), "Asia/Tokyo")] = float(c)
    etf_d0 = etf_bars.get(d0_str, etf_reg)

    # 起点: TOPIX 指数の D0 公式終値（万一欠損時のみ 1306.T）
    if d0_str in topix_daily:
        base_val = float(topix_daily[d0_str])
        sym_label = "TOPIX"
    elif jpx_topix_live and jpx_topix_live.get("date") == d0_str:
        base_val = float(jpx_topix_live["price"])
        sym_label = "TOPIX"
    else:
        base_val = etf_d0
        sym_label = "1306.T"

    today_str = now_jst.strftime("%Y-%m-%d")
    in_d1_cash_session = today_str >= d1_str and now_jst.hour >= 9

    cur_val = base_val
    as_of_iso = jpx_topix_live["asOf"] if jpx_topix_live else etf_as_of
    futures_info: Optional[Dict[str, Any]] = None

    if in_d1_cash_session:
        # 日中〜大引け後：JPX TOPIX 指数の D1 現在値/終値を優先し、取れなければ 1306.T の変化率で補完
        if jpx_topix_live and jpx_topix_live.get("date", "") >= d1_str:
            if sym_label == "TOPIX":
                cur_val = float(jpx_topix_live["price"])
            else:
                cur_val = etf_reg
            as_of_iso = jpx_topix_live["asOf"]
        elif d1_str in topix_daily and sym_label == "TOPIX":
            cur_val = float(topix_daily[d1_str])
        else:
            r_1306 = (etf_reg - etf_d0) / etf_d0 if etf_d0 > 0 else 0.0
            cur_val = base_val * (1.0 + r_1306)
            as_of_iso = etf_as_of
    else:
        # 夜間・時間外：1. 大証 TOPIX先物 (15:30比) → 2. β × 日経先物変化率
        ose_tpx = get_ose_session_futures_change(ose_futures, "topix", "TOPIX先物(大証)", d0_str, d1_str)
        if ose_tpx:
            cur_val = base_val * (1.0 + ose_tpx["rawChange"])
            as_of_iso = ose_tpx["asOf"]
            futures_info = {
                "symbol": ose_tpx["symbol"],
                "base": ose_tpx["base"],
                "cur": ose_tpx["cur"],
                "change": ose_tpx["change"],
            }
        else:
            ose_nk = get_ose_session_futures_change(ose_futures, "nikkei225", "日経225先物(大証)", d0_str, d1_str)
            nk_fut = ose_nk or get_cme_niy_futures_change(market_cache, d0_str, now_jst)
            if nk_fut:
                r_approx = topix_beta * nk_fut["rawChange"]
                cur_val = base_val * (1.0 + r_approx)
                as_of_iso = nk_fut["asOf"]
                futures_info = {
                    "symbol": f"{nk_fut['symbol']}×β({topix_beta:.2f})",
                    "base": nk_fut["base"],
                    "cur": nk_fut["cur"],
                    "change": round(r_approx * 100.0, 2),
                }

    r_idx = (cur_val - base_val) / base_val if base_val else 0.0
    res: Dict[str, Any] = {
        "symbol": sym_label,
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


def get_domestic_close_change(
    market_cache: Dict[str, Dict[str, Any]],
    symbol: str,
    d0_str: str,
    d1_str: str,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """国内ETF（1540.T 等）の D0 当日終値と現在値（開場前は変化なし）から変化率を計算する。"""
    entry = market_cache[symbol]
    daily = entry["daily"]
    meta = daily["meta"]
    reg_price = float(meta["regularMarketPrice"])
    reg_time = int(meta.get("regularMarketTime", int(now_jst.timestamp())))
    as_of_iso = datetime.datetime.fromtimestamp(reg_time, JST).isoformat()
    reg_date_jst = get_bar_local_date(reg_time, "Asia/Tokyo")

    bars_by_date: Dict[str, float] = {}
    for t, c in zip(daily.get("timestamp", []), daily["indicators"]["quote"][0]["close"]):
        if c is not None:
            bars_by_date[get_bar_local_date(int(t), "Asia/Tokyo")] = float(c)

    base_val = bars_by_date.get(d0_str, reg_price)
    today_str = now_jst.strftime("%Y-%m-%d")

    if today_str < d1_str or (today_str == d1_str and now_jst.hour < 9) or reg_date_jst < d1_str:
        cur_val = base_val
    else:
        cur_val = bars_by_date.get(d1_str, reg_price) if now_jst.hour >= 16 else reg_price

    r_idx = (cur_val - base_val) / base_val if base_val else 0.0
    return {
        "symbol": symbol,
        "base": round(base_val, 2),
        "baseDate": d0_str,
        "cur": round(cur_val, 2),
        "rawChange": r_idx,
        "change": round(r_idx * 100.0, 2),
        "asOf": as_of_iso,
    }


# ============================================================================
# 答え合わせログ記録・直近20営業日精度集計
# ============================================================================

def update_forecast_log_and_accuracy(
    out_groups: List[Dict[str, Any]],
    nav_cache: Dict[str, Any],
    now_jst: datetime.datetime,
    window_days: int = 20,
) -> Dict[str, Any]:
    """朝 8:30 JST 時点の予想値を docs/history/forecast_log.json に記録し、公表基準価額との誤差を集計する。"""
    FORECAST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log_data: Dict[str, Any] = {"version": 1, "entries": {}}
    if FORECAST_LOG_PATH.exists():
        try:
            loaded = json.loads(FORECAST_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                log_data = loaded
        except Exception as e:
            print(f"[WARN] Failed to load forecast_log.json: {e}", file=sys.stderr)

    entries: Dict[str, Dict[str, Any]] = log_data["entries"]
    changed = False
    today_str = now_jst.strftime("%Y-%m-%d")
    in_morning_snapshot_window = (
        (now_jst.hour > 8 or (now_jst.hour == 8 and now_jst.minute >= 30))
        and now_jst.hour < 18
    )

    # 1. 予想対象日当日の朝 8:30 JST 以降（18:00 未満）に 1 日 1 回記録
    if in_morning_snapshot_window:
        for g in out_groups:
            for f in g.get("funds", []):
                if f.get("stale") or "forecast" not in f or "targetDate" not in f:
                    continue
                t_date = f["targetDate"]
                if t_date != today_str:
                    continue
                fid = f["id"]
                day_bucket = entries.setdefault(t_date, {})
                existing = day_bucket.get(fid)
                if existing is None or existing.get("source") == "backtest":
                    day_bucket[fid] = {
                        "baseDate": f["navDate"],
                        "baseNav": int(f["nav"]),
                        "forecastNav": int(f["forecast"]),
                        "forecastChange": float(f["change"]),
                        "recordedAt": now_jst.isoformat(timespec="seconds"),
                        "source": "live",
                    }
                    changed = True

    # 2. 実際の基準価額が出たら答え合わせ（ポイント差・円差を記録）
    nav_hist_by_fund: Dict[str, Dict[str, int]] = {}
    for fid, c_entry in nav_cache.items():
        hist_list = c_entry.get("history", [])
        h_map: Dict[str, int] = {}
        for item in hist_list:
            if isinstance(item, list) and len(item) >= 2:
                h_map[str(item[0])] = int(item[1])
        if "navDate" in c_entry and "nav" in c_entry:
            h_map[str(c_entry["navDate"])] = int(c_entry["nav"])
        nav_hist_by_fund[fid] = h_map

    for t_date, day_bucket in entries.items():
        if not isinstance(day_bucket, dict):
            continue
        for fid, rec in day_bucket.items():
            if not isinstance(rec, dict):
                continue
            if "actualNav" in rec and rec["actualNav"] is not None:
                continue
            act_nav = nav_hist_by_fund.get(fid, {}).get(t_date)
            if act_nav is not None and rec.get("baseNav"):
                base_nav = int(rec["baseNav"])
                fc_nav = int(rec["forecastNav"])
                act_chg = (act_nav - base_nav) / base_nav * 100.0
                err_yen = fc_nav - act_nav
                err_pt = (fc_nav - act_nav) / base_nav * 100.0
                rec["actualNav"] = int(act_nav)
                rec["actualChange"] = round(act_chg, 2)
                rec["errorYen"] = int(err_yen)
                rec["errorPt"] = round(err_pt, 4)
                changed = True

    if changed or not FORECAST_LOG_PATH.exists():
        sorted_entries = {k: entries[k] for k in sorted(entries.keys())}
        log_data["entries"] = sorted_entries
        log_data["updatedAt"] = now_jst.isoformat(timespec="seconds")
        FORECAST_LOG_PATH.write_text(
            json.dumps(log_data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # 3. 各ファンドおよびグループ別の直近 window_days 営業日の平均誤差を集計
    sorted_dates = sorted(entries.keys())
    group_summaries: List[Dict[str, Any]] = []
    all_abs_errors: List[float] = []

    for g in out_groups:
        g_abs_errors: List[float] = []
        g_signed_errors: List[float] = []
        g_fund_with_data = 0

        for f in g.get("funds", []):
            fid = f["id"]
            matched_recs: List[Tuple[str, Dict[str, Any]]] = []
            for d_str in sorted_dates:
                rec = entries.get(d_str, {}).get(fid)
                if isinstance(rec, dict) and rec.get("errorPt") is not None:
                    matched_recs.append((d_str, rec))

            recent = matched_recs[-window_days:]
            if recent:
                abs_errs = [abs(float(r["errorPt"])) for _, r in recent]
                sgn_errs = [float(r["errorPt"]) for _, r in recent]
                mae_pt = sum(abs_errs) / len(abs_errs)
                bias_pt = sum(sgn_errs) / len(sgn_errs)
                last_date, last_rec = recent[-1]
                f["accuracy"] = {
                    "days": len(recent),
                    "maePt": round(mae_pt, 2),
                    "biasPt": round(bias_pt, 2),
                    "latest": {
                        "date": last_date,
                        "forecast": int(last_rec["forecastNav"]),
                        "actual": int(last_rec["actualNav"]),
                        "errorYen": int(last_rec["errorYen"]),
                        "errorPt": round(float(last_rec["errorPt"]), 2),
                    },
                }
                g_abs_errors.extend(abs_errs)
                g_signed_errors.extend(sgn_errs)
                all_abs_errors.extend(abs_errs)
                g_fund_with_data += 1

        group_summaries.append({
            "id": g["id"],
            "label": g["label"],
            "fundCount": len(g.get("funds", [])),
            "evaluatedFundCount": g_fund_with_data,
            "sampleCount": len(g_abs_errors),
            "maePt": round(sum(g_abs_errors) / len(g_abs_errors), 2) if g_abs_errors else None,
            "biasPt": round(sum(g_signed_errors) / len(g_signed_errors), 2) if g_signed_errors else None,
        })

    return {
        "windowDays": window_days,
        "overallMaePt": round(sum(all_abs_errors) / len(all_abs_errors), 2) if all_abs_errors else None,
        "totalSamples": len(all_abs_errors),
        "groups": group_summaries,
    }


# ============================================================================
# メインビルド処理
# ============================================================================

def is_nav_cache_fresh_within_hours(cached: Optional[Dict[str, Any]], now_jst: datetime.datetime, ttl_hours: float) -> bool:
    """前回の基準価額取得時刻 (fetchedAt) から ttl_hours 時間以内かどうかを判定する。"""
    if not cached or "nav" not in cached or not cached.get("fetchedAt"):
        return False
    try:
        fetched_dt = datetime.datetime.fromisoformat(str(cached["fetchedAt"]))
        return (now_jst - fetched_dt).total_seconds() < ttl_hours * 3600.0
    except Exception:
        return False


def run_build(
    force_nav: bool = False,
    nav_ttl_hours: float = 6.0,
    next_update_in_sec: Optional[int] = None,
    output_path: pathlib.Path = DATA_JSON_PATH,
) -> Dict[str, Any]:
    """市場データ・基準価額データを取得して予想基準価額を計算し、data.json に書き出す。"""
    reset_http_run_cache()
    now_jst = datetime.datetime.now(JST)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    funds_cfg = json.loads(FUNDS_JSON_PATH.read_text(encoding="utf-8"))
    anchor_cache = load_anchor_cache()

    nav_cache: Dict[str, Any] = {}
    if NAV_CACHE_PATH.exists():
        try:
            nav_cache = json.loads(NAV_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[WARN] Failed to load nav_cache.json: {e}", file=sys.stderr)

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

    # 1. 各ファンドの基準価額を取得（前回取得から nav_ttl_hours 以内ならキャッシュ再利用）
    all_funds: List[Tuple[str, Dict[str, Any]]] = []
    for g in funds_cfg.get("groups", []):
        for f in g.get("funds", []):
            all_funds.append((g["id"], f))

    nav_updated = False
    for _, f in all_funds:
        fid = f["id"]
        cached = nav_cache.get(fid)
        fresh_within_ttl = is_nav_cache_fresh_within_hours(cached, now_jst, nav_ttl_hours)
        need_fetch = force_nav or cached is None or "nav" not in cached
        if not need_fetch and not fresh_within_ttl:
            if in_nav_window and cached.get("navDate") != expected_nav_date_str:
                need_fetch = True

        if need_fetch:
            try:
                latest_nav, latest_date, history = fetch_fund_nav_csv(f["isinCd"], f["associFundCd"])
                # 夕方更新待ちで未反映だった場合は1時間後に再試行できるよう fetchedAt を調整
                if in_nav_window and latest_date != expected_nav_date_str and nav_ttl_hours > 1.0:
                    adj_dt = now_jst - datetime.timedelta(hours=(nav_ttl_hours - 1.0))
                    fetched_iso = adj_dt.isoformat(timespec="seconds")
                else:
                    fetched_iso = now_jst.isoformat(timespec="seconds")
                nav_cache[fid] = {
                    "nav": latest_nav,
                    "navDate": latest_date,
                    "history": history,
                    "fetchedAt": fetched_iso,
                }
                nav_updated = True
                print(f"[NAV] {fid}: {latest_nav} ({latest_date})")
            except Exception as e:
                print(f"[ERROR] NAV fetch failed for {fid}: {e}", file=sys.stderr)

    if nav_updated or not NAV_CACHE_PATH.exists():
        NAV_CACHE_PATH.write_text(
            json.dumps(nav_cache, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )

    # 代表の基準日 D0 と予想対象日 D1 を決定
    nav_dates = [v["navDate"] for v in nav_cache.values() if isinstance(v, dict) and "navDate" in v]
    rep_d0_str = max(set(nav_dates), key=nav_dates.count) if nav_dates else expected_nav_date_str
    rep_d0_date = datetime.datetime.strptime(rep_d0_str, "%Y-%m-%d").date()
    rep_d1_str = next_jp_business_day(rep_d0_date).strftime("%Y-%m-%d")

    # 2. 市場データの取得（Yahoo・MURC仲値・JPX/株探TOPIX・大証先物・MSCI公式指数）
    active_gold_sym = resolve_active_comex_gold_symbol(anchor_cache, now_jst)

    required_symbols = {
        "^GSPC", "^NDX", "NQ=F", "^SOX", "^NYFANG",
        "JPY=X", "^N225", "NIY=F", "1306.T",
        "ACWI", "TOK", "SCHD", "VTI",
        "IGLN.L", "1540.T", "GC=F",
    }
    solactive_isins: Set[str] = set()
    for _, f in all_funds:
        sym_val = str(f.get("symbol") or "")
        if sym_val.startswith("SOLACTIVE:"):
            solactive_isins.add(sym_val.split(":", 1)[1])
        elif sym_val:
            required_symbols.add(sym_val)
        if f.get("futuresSymbol"):
            required_symbols.add(f["futuresSymbol"])
        if f.get("goldSymbol"):
            required_symbols.add(f["goldSymbol"])
        if f.get("goldEtfSymbol"):
            required_symbols.add(f["goldEtfSymbol"])

    market_cache = build_market_cache(list(required_symbols), active_gold_sym)

    distinct_d0_dates = set(nav_dates) | {rep_d0_str}
    ttm_map = update_murc_ttm_cache(anchor_cache, distinct_d0_dates, now_jst, rep_d1_str)
    jpx_topix_live = fetch_jpx_topix_live(anchor_cache, now_jst)
    topix_daily = ensure_topix_history_for_beta(anchor_cache, rep_d0_str)
    ose_futures = fetch_ose_futures(anchor_cache, now_jst)
    msci_maps = update_msci_cache(anchor_cache, now_jst, rep_d1_str)
    solactive_maps = update_solactive_cache(anchor_cache, solactive_isins, now_jst, rep_d1_str)
    save_anchor_cache(anchor_cache)

    # 直近60営業日の TOPIX 対 日経平均 回帰係数 β
    n225_daily_map: Dict[str, float] = {}
    n225_chart = market_cache["^N225"]["daily"]
    for t, c in zip(n225_chart.get("timestamp", []), n225_chart["indicators"]["quote"][0]["close"]):
        if c is not None:
            n225_daily_map[get_bar_local_date(int(t), "Asia/Tokyo")] = float(c)
    topix_beta = round(calc_rolling_beta(topix_daily, n225_daily_map, rep_d0_str, lookback=60), 4)

    # 代表 D0 に対する主要指標サマリー
    rep_fx = get_fx_data(market_cache, ttm_map, rep_d0_str, rep_d1_str, now_jst)
    sp500_m = get_overseas_index_change(market_cache, "^GSPC", rep_d0_str, d1_str=rep_d1_str)
    ndx_m = get_overseas_index_change(market_cache, "^NDX", rep_d0_str, d1_str=rep_d1_str)
    nqf_m = get_us_futures_change(
        market_cache, "NQ=F", rep_d0_str, rep_d1_str, now_jst, settle_hour_et=16, settle_min_et=0
    )
    n225_m = get_nikkei_index_change(market_cache, ose_futures, rep_d0_str, rep_d1_str, now_jst)
    topix_m = get_topix_index_change(
        market_cache, jpx_topix_live, topix_daily, ose_futures, topix_beta, rep_d0_str, rep_d1_str, now_jst
    )
    sox_m = get_overseas_index_change(market_cache, "^SOX", rep_d0_str, d1_str=rep_d1_str)
    fang_m = get_overseas_index_change(market_cache, "^NYFANG", rep_d0_str, d1_str=rep_d1_str)
    acwi_m = get_msci_index_change(
        msci_maps["892400"], "MSCI ACWI", "ACWI", market_cache, rep_d0_str, rep_d1_str
    )
    gold_m = get_us_futures_change(
        market_cache, active_gold_sym, rep_d0_str, rep_d1_str, now_jst,
        settle_hour_et=13, settle_min_et=30, align_base_date=sp500_m["baseDate"], display_symbol="GC=F",
    )

    markets_summary = [
        {"id": "sp500", "label": "S&P500", "symbol": "^GSPC", "base": sp500_m["base"], "cur": sp500_m["cur"], "change": sp500_m["change"], "asOf": sp500_m["asOf"]},
        {"id": "ndx", "label": "NASDAQ100", "symbol": "^NDX", "base": ndx_m["base"], "cur": ndx_m["cur"], "change": ndx_m["change"], "asOf": ndx_m["asOf"]},
        {"id": "nqf", "label": "NASDAQ100先物", "symbol": "NQ=F", "base": nqf_m["base"], "cur": nqf_m["cur"], "change": nqf_m["change"], "asOf": nqf_m["asOf"]},
        {"id": "n225", "label": "日経平均(先物考慮)", "symbol": "^N225", "base": n225_m["base"], "cur": n225_m["cur"], "change": n225_m["change"], "asOf": n225_m["asOf"]},
        {"id": "topix", "label": "TOPIX(先物考慮)", "symbol": "TOPIX", "base": topix_m["base"], "cur": topix_m["cur"], "change": topix_m["change"], "asOf": topix_m["asOf"]},
        {"id": "usdjpy", "label": "ドル円", "symbol": "JPY=X", "base": rep_fx["base"], "cur": rep_fx["cur"], "change": rep_fx["change"], "asOf": rep_fx["asOf"]},
        {"id": "sox", "label": "SOX", "symbol": "^SOX", "base": sox_m["base"], "cur": sox_m["cur"], "change": sox_m["change"], "asOf": sox_m["asOf"]},
        {"id": "fang", "label": "FANG+", "symbol": "^NYFANG", "base": fang_m["base"], "cur": fang_m["cur"], "change": fang_m["change"], "asOf": fang_m["asOf"]},
        {"id": "acwi", "label": "MSCI ACWI(オルカン)", "symbol": acwi_m["symbol"], "base": acwi_m["base"], "cur": acwi_m["cur"], "change": acwi_m["change"], "asOf": acwi_m["asOf"]},
        {"id": "gold", "label": "金先物", "symbol": "GC=F", "base": gold_m["base"], "cur": gold_m["cur"], "change": gold_m["change"], "asOf": gold_m["asOf"]},
    ]

    # 3. 各ファンドの予想基準価額を計算
    out_groups: List[Dict[str, Any]] = []
    for g in funds_cfg.get("groups", []):
        gid = g["id"]
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

                fx_info = rep_fx if d0_str == rep_d0_str else get_fx_data(market_cache, ttm_map, d0_str, d1_str, now_jst)
                r_fx = fx_info["rawChange"]

                model = f["model"]
                sym = f["symbol"]
                lev = float(f.get("leverage", 1))
                hedged = bool(f.get("hedged", False))

                def build_fx_breakdown() -> Dict[str, Any]:
                    fx_b: Dict[str, Any] = {
                        "base": fx_info["base"],
                        "cur": fx_info["cur"],
                        "change": fx_info["change"],
                    }
                    if fx_info.get("estimatedBase"):
                        fx_b["estimatedBase"] = True
                    return fx_b

                breakdown: Dict[str, Any] = {}
                if model in ("standard", "standard_div", "msci_acwi", "msci_kokusai"):
                    if sym == "ACWI" or model == "msci_acwi":
                        idx_info = get_msci_index_change(
                            msci_maps["892400"], "MSCI ACWI", "ACWI", market_cache, d0_str, d1_str
                        )
                    elif sym == "TOK" or model == "msci_kokusai":
                        idx_info = get_msci_index_change(
                            msci_maps["991200"], "MSCI KOKUSAI", "TOK", market_cache, d0_str, d1_str
                        )
                    elif sym.startswith("SOLACTIVE:"):
                        sol_isin = sym.split(":", 1)[1]
                        sol_lbl = "Solactive Mega10" if sol_isin == "DE000SL0R5Q9" else sol_isin
                        idx_info = get_solactive_index_change(
                            solactive_maps.get(sol_isin, {}), sol_isin, sol_lbl, d0_str, d1_str
                        )
                    else:
                        idx_info = get_overseas_index_change(
                            market_cache, sym, d0_str, d1_str=d1_str, adjust_div=(model == "standard_div")
                        )
                    r_idx = idx_info["rawChange"]
                    r_total = (1.0 + r_idx) * (1.0 + r_fx) - 1.0
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "fx": build_fx_breakdown(),
                    }
                    if "dividendAdjusted" in idx_info:
                        breakdown["index"]["dividendAdjusted"] = idx_info["dividendAdjusted"]
                    if "etfSupplement" in idx_info:
                        breakdown["etfSupplement"] = idx_info["etfSupplement"]

                elif model == "leveraged_hedged":
                    if sym == "NQ=F":
                        idx_info = get_us_futures_change(
                            market_cache, "NQ=F", d0_str, d1_str, now_jst, settle_hour_et=16, settle_min_et=0
                        )
                    else:
                        idx_info = get_overseas_index_change(market_cache, sym, d0_str, d1_str=d1_str)
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }

                elif model == "additive":
                    if sym == "NQ=F":
                        idx_info = get_us_futures_change(
                            market_cache, "NQ=F", d0_str, d1_str, now_jst, settle_hour_et=16, settle_min_et=0
                        )
                    else:
                        idx_info = get_overseas_index_change(market_cache, sym, d0_str, d1_str=d1_str)
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx + r_fx
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "fx": build_fx_breakdown(),
                    }

                elif model == "domestic":
                    is_topix = (gid == "topix" or sym in ("1306.T", "TOPIX", "^TOPX"))
                    if is_topix:
                        idx_info = get_topix_index_change(
                            market_cache, jpx_topix_live, topix_daily, ose_futures, topix_beta, d0_str, d1_str, now_jst
                        )
                    else:
                        idx_info = get_nikkei_index_change(market_cache, ose_futures, d0_str, d1_str, now_jst)
                    r_idx = idx_info["rawChange"]
                    r_total = r_idx
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }
                    if "futures" in idx_info:
                        breakdown["futures"] = idx_info["futures"]

                elif model == "domestic_leveraged":
                    idx_info = get_nikkei_leveraged_change(
                        market_cache, ose_futures, anchor_cache, d0_str, d1_str, now_jst
                    )
                    r_idx = idx_info["rawChange"]
                    r_total = lev * r_idx
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }
                    if "futures" in idx_info:
                        breakdown["futures"] = idx_info["futures"]

                elif model == "domestic_close":
                    idx_info = get_domestic_close_change(market_cache, sym, d0_str, d1_str, now_jst)
                    r_idx = idx_info["rawChange"]
                    r_total = r_idx
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        }
                    }

                elif model == "gold_plus":
                    if sym == "ACWI":
                        idx_info = get_msci_index_change(
                            msci_maps["892400"], "MSCI ACWI", "ACWI", market_cache, d0_str, d1_str
                        )
                    else:
                        idx_info = get_overseas_index_change(market_cache, sym, d0_str, d1_str=d1_str)
                    gold_info = get_us_futures_change(
                        market_cache, active_gold_sym, d0_str, d1_str, now_jst,
                        settle_hour_et=13, settle_min_et=30,
                        align_base_date=idx_info["baseDate"],
                        display_symbol=f.get("goldSymbol", "GC=F"),
                    )
                    r_stock = idx_info["rawChange"]
                    r_gold = gold_info["rawChange"]
                    r_total = r_stock + r_fx + r_gold
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "gold": {
                            "symbol": gold_info["symbol"],
                            "base": gold_info["base"],
                            "cur": gold_info["cur"],
                            "change": gold_info["change"],
                            "asOf": gold_info["asOf"],
                        },
                        "fx": build_fx_breakdown(),
                    }
                    if "etfSupplement" in idx_info:
                        breakdown["etfSupplement"] = idx_info["etfSupplement"]

                elif model == "topix_gold_plus":
                    idx_info = get_topix_index_change(
                        market_cache, jpx_topix_live, topix_daily, ose_futures, topix_beta, d0_str, d1_str, now_jst
                    )
                    ref_us_base = get_overseas_index_change(market_cache, "^GSPC", d0_str, d1_str=d1_str)["baseDate"]
                    gold_info = get_us_futures_change(
                        market_cache, active_gold_sym, d0_str, d1_str, now_jst,
                        settle_hour_et=13, settle_min_et=30,
                        align_base_date=ref_us_base,
                        display_symbol=f.get("goldSymbol", "GC=F"),
                    )
                    r_stock = idx_info["rawChange"]
                    gold_etf_sym = f.get("goldEtfSymbol")
                    if gold_etf_sym and gold_etf_sym in market_cache:
                        w_etf = float(f.get("goldEtfWeight", 0.55))
                        g_etf_info = get_overseas_index_change(market_cache, gold_etf_sym, d0_str, d1_str=d1_str)
                        r_g_etf_jpy = (1.0 + g_etf_info["rawChange"]) * (1.0 + r_fx) - 1.0
                        r_gold = w_etf * r_g_etf_jpy + (1.0 - w_etf) * gold_info["rawChange"]
                        gold_disp = {
                            "symbol": f"{gold_info['symbol']}+{gold_etf_sym}(為替込)",
                            "base": gold_info["base"],
                            "cur": gold_info["cur"],
                            "change": round(r_gold * 100.0, 2),
                            "asOf": g_etf_info["asOf"],
                        }
                    else:
                        r_gold = gold_info["rawChange"]
                        gold_disp = {
                            "symbol": gold_info["symbol"],
                            "base": gold_info["base"],
                            "cur": gold_info["cur"],
                            "change": gold_info["change"],
                            "asOf": gold_info["asOf"],
                        }
                    r_total = r_stock + r_gold
                    breakdown = {
                        "index": {
                            "symbol": idx_info["symbol"],
                            "base": idx_info["base"],
                            "cur": idx_info["cur"],
                            "change": idx_info["change"],
                            "asOf": idx_info["asOf"],
                        },
                        "gold": gold_disp,
                    }
                    if "futures" in idx_info:
                        breakdown["futures"] = idx_info["futures"]

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
            "id": gid,
            "label": g["label"],
            "funds": out_funds,
        })

    accuracy_summary = update_forecast_log_and_accuracy(out_groups, nav_cache, now_jst, window_days=20)

    output_data: Dict[str, Any] = {
        "updatedAt": now_jst.isoformat(timespec="seconds"),
        "baseDate": rep_d0_str,
        "targetDate": rep_d1_str,
        "topixBeta": topix_beta,
        "markets": markets_summary,
        "fx": {
            "base": rep_fx["base"],
            "cur": rep_fx["cur"],
            "change": rep_fx["change"],
            "estimated": rep_fx["estimated"],
            "estimatedBase": rep_fx["estimatedBase"],
            "bidMidAdjusted": rep_fx["bidMidAdjusted"],
            "asOf": rep_fx["asOf"],
        },
        "accuracySummary": accuracy_summary,
        "groups": out_groups,
    }
    if next_update_in_sec is not None and next_update_in_sec > 0:
        next_dt = now_jst + datetime.timedelta(seconds=int(next_update_in_sec))
        output_data["updateIntervalSec"] = int(next_update_in_sec)
        output_data["nextUpdateAt"] = next_dt.isoformat(timespec="seconds")

    output_path.write_text(
        json.dumps(output_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[OK] Wrote {output_path} (targetDate={rep_d1_str}, topixBeta={topix_beta})")
    return output_data


def main() -> None:
    parser = argparse.ArgumentParser(description="Build docs/data.json for fund-forecast")
    parser.add_argument("--force-nav", action="store_true", help="時間帯に関わらず基準価額CSVを再取得する")
    parser.add_argument("--nav-ttl-hours", type=float, default=6.0, help="基準価額キャッシュの再利用有効時間（時間）")
    parser.add_argument("--next-update-sec", type=int, default=300, help="次回更新予定までの秒数")
    parser.add_argument("--output", type=str, default=str(DATA_JSON_PATH), help="出力先 data.json パス")
    args = parser.parse_args()

    run_build(
        force_nav=args.force_nav,
        nav_ttl_hours=args.nav_ttl_hours,
        next_update_in_sec=args.next_update_sec,
        output_path=pathlib.Path(args.output),
    )


if __name__ == "__main__":
    main()
