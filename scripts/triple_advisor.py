import argparse
import datetime
import json
import math
import os
import pathlib
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import build_data  # noqa: E402

JST = build_data.JST
UTC = build_data.UTC

TRIPLE_FUND_ID = "auam-triple-ndx"
PORTFOLIO_JSON_PATH = ROOT_DIR / "portfolio.json"
TRIPLE_DATA_JSON_PATH = build_data.DOCS_DIR / "triple_data.json"
TRIPLE_NOTIFY_STATE_PATH = build_data.DOCS_DIR / "triple_notify_state.json"
PUBLIC_TRIPLE_URL = "https://msmttng.github.io/fund-forecast/triple.html"
PUBLIC_TRIPLE_ORDERS_URL = "https://msmttng.github.io/fund-forecast/triple_orders.html"
DEFAULT_ORDERS_URL = (
    "https://script.google.com/macros/s/"
    "AKfycbwaZu4G_PhrOQItGpvqBTwjVer2KeV-1884-c5YUGpaZJV5eOkcBcYL77g9VxoyY6u32g/exec"
)
LOCAL_ORDERS_SECRET_PATH = ROOT_DIR / "gas_triple_orders" / ".orders_secret.json"
TAX_RATE_SPECIFIED_ACCOUNT = 0.20315

WEEKDAYS_JP = ["月", "火", "水", "木", "金", "土", "日"]

# FOMC 政策金利発表日（米国現地日付＝日本時間ではその日の深夜〜翌早朝3:00/4:00に発表）
FOMC_DECISION_US_DATES: Set[str] = {
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
    "2027-01-27",
    "2027-03-17",
    "2027-04-28",
    "2027-06-09",
    "2027-07-28",
    "2027-09-15",
    "2027-10-27",
    "2027-12-08",
}


# ============================================================================
# 米国市場休場日・申込日・約定日カレンダー計算
# ============================================================================

def _easter_sunday(year: int) -> datetime.date:
    """グレゴリオ暦の復活祭（イースター）日曜日を算出する（聖金曜日 Good Friday 判定用）。"""
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
    """指定年月の第 n 週の指定曜日 (0=月..6=日) を返す。"""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + 7 * (n - 1))


def _last_weekday_of_month(year: int, month: int, weekday: int) -> datetime.date:
    """指定年月の最終の指定曜日 (0=月..6=日) を返す。"""
    if month == 12:
        last_day = datetime.date(year, 12, 31)
    else:
        last_day = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - datetime.timedelta(days=offset)


def _observed_us_holiday(d: datetime.date) -> datetime.date:
    """米国の固定祝日が土曜なら前日金曜、日曜なら翌日月曜の振替休場日を返す。"""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def get_us_market_holidays(year: int) -> Dict[datetime.date, str]:
    """NYSE / NASDAQ / CME 株式先物終日休場日（ファンド申込不可日）の辞書を返す。"""
    holidays: Dict[datetime.date, str] = {}

    # 1. 元日（翌年元日が土曜で12/31金曜振替になるケースを含む）
    nyd = _observed_us_holiday(datetime.date(year, 1, 1))
    if nyd.year == year:
        holidays[nyd] = "米国元日休場"
    next_nyd = _observed_us_holiday(datetime.date(year + 1, 1, 1))
    if next_nyd.year == year:
        holidays[next_nyd] = "米国元日振替休場"

    # 2. キング牧師記念日（1月第3月曜）
    holidays[_nth_weekday_of_month(year, 1, 0, 3)] = "キング牧師記念日(米休場)"
    # 3. 大統領の日（2月第3月曜）
    holidays[_nth_weekday_of_month(year, 2, 0, 3)] = "大統領の日(米休場)"
    # 4. 聖金曜日 Good Friday（復活祭の2日前）
    holidays[_easter_sunday(year) - datetime.timedelta(days=2)] = "聖金曜日(米休場)"
    # 5. 戦没将兵追悼記念日（5月最終月曜）
    holidays[_last_weekday_of_month(year, 5, 0)] = "メモリアルデー(米休場)"
    # 6. ジューンティーンス（6月19日）
    holidays[_observed_us_holiday(datetime.date(year, 6, 19))] = "ジューンティーンス(米休場)"
    # 7. 独立記念日（7月4日）
    holidays[_observed_us_holiday(datetime.date(year, 7, 4))] = "米独立記念日(米休場)"
    # 8. レイバー・デー（9月第1月曜）
    holidays[_nth_weekday_of_month(year, 9, 0, 1)] = "レイバー・デー(米休場)"
    # 9. 感謝祭（11月第4木曜）
    holidays[_nth_weekday_of_month(year, 11, 3, 4)] = "感謝祭(米休場)"
    # 10. クリスマス（12月25日）
    holidays[_observed_us_holiday(datetime.date(year, 12, 25))] = "クリスマス(米休場)"

    return holidays


def us_holiday_name(d: datetime.date) -> Optional[str]:
    """指定日が米国市場休場日なら祝日名を返し、通常取引日なら None を返す。"""
    return get_us_market_holidays(d.year).get(d)


def is_us_trading_day(d: datetime.date) -> bool:
    """米国株式市場の取引日かどうかを判定する。"""
    if d.weekday() >= 5:
        return False
    return us_holiday_name(d) is None


def is_fund_order_day(d: datetime.date) -> bool:
    """auAMトリプル・NASDAQ100 の申込受付可能営業日（日本営業日かつ米国市場営業日）かを判定する。"""
    return build_data.is_jp_business_day(d) and is_us_trading_day(d)


def format_jp_mdw(d_str: str) -> str:
    """'2026-11-02' を '11/2(月)' に整形する。"""
    dt = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
    return f"{dt.month}/{dt.day}({WEEKDAYS_JP[dt.weekday()]})"


def compute_order_schedule(
    now_jst: datetime.datetime,
    launchpad_us_date_str: str,
    cutoff_hour: int = 15,
    cutoff_minute: int = 30,
) -> Dict[str, Any]:
    """現在時刻から注文した場合の申込日・締切日時・約定日・またぐ米国市場の晩数を算出する。"""
    today = now_jst.date()
    today_cutoff = datetime.datetime(
        today.year, today.month, today.day, cutoff_hour, cutoff_minute, 0, tzinfo=JST
    )

    if is_fund_order_day(today) and now_jst < today_cutoff:
        app_date = today
    else:
        cur = today if (now_jst < today_cutoff) else (today + datetime.timedelta(days=1))
        while not is_fund_order_day(cur):
            cur += datetime.timedelta(days=1)
        app_date = cur

    cutoff_dt = datetime.datetime(
        app_date.year, app_date.month, app_date.day, cutoff_hour, cutoff_minute, 0, tzinfo=JST
    )
    # 海外資産型（翌営業日約定）：申込日の翌国内営業日が約定日
    exec_date = build_data.next_jp_business_day(app_date)

    # 申込締切後に約定までに通過する米国市場の取引日リスト
    nights_after_cutoff: List[str] = []
    jp_holidays_between: List[str] = []
    cur_d = app_date
    while cur_d < exec_date:
        if is_us_trading_day(cur_d):
            nights_after_cutoff.append(cur_d.strftime("%Y-%m-%d"))
        if cur_d > app_date and not build_data.is_jp_business_day(cur_d) and cur_d.weekday() < 5:
            h_name = build_data.jpholiday.is_holiday_name(cur_d) or "国内休場"
            jp_holidays_between.append(f"{cur_d.month}/{cur_d.day} {h_name}")
        cur_d += datetime.timedelta(days=1)

    # 発射台（確定済み米国市場）から約定日までに未確定の米国市場取引日リスト
    unsettled_us_nights: List[str] = []
    try:
        lp_us_date = datetime.datetime.strptime(launchpad_us_date_str, "%Y-%m-%d").date()
        chk = lp_us_date + datetime.timedelta(days=1)
        while chk < exec_date:
            if is_us_trading_day(chk):
                unsettled_us_nights.append(chk.strftime("%Y-%m-%d"))
            chk += datetime.timedelta(days=1)
    except Exception:
        unsettled_us_nights = list(nights_after_cutoff)

    fomc_dates_hit = [d for d in nights_after_cutoff if d in FOMC_DECISION_US_DATES]
    minutes_to_cutoff = max(0, int((cutoff_dt - now_jst).total_seconds() // 60))

    # 現在の時間帯ステータス判定
    hm = now_jst.hour * 60 + now_jst.minute
    if not build_data.is_jp_business_day(today):
        window_state = "holiday_or_weekend"
        window_label = "休場日（次回営業日申込扱い）"
    elif not is_us_trading_day(today):
        u_name = us_holiday_name(today) or "米国市場休場"
        window_state = "holiday_or_weekend"
        window_label = f"ファンド申込不可日（{u_name}）"
    elif hm >= cutoff_hour * 60 + cutoff_minute:
        window_state = "after_cutoff"
        window_label = "本日締切終了（次回営業日申込扱い）"
    elif (14 * 60 + 45) <= hm < (cutoff_hour * 60 + cutoff_minute):
        window_state = "golden_window"
        window_label = "注文最終判断ゴールデンタイム（締切直前）"
    elif (10 * 60 + 10) <= hm < (14 * 60 + 45):
        window_state = "pre_cutoff_watch"
        window_label = "仲値確定済・15:05最終ジャッジ待機中"
    else:
        window_state = "morning_pre_ttm"
        window_label = "朝10:00仲値確定前・待機時間帯"

    return {
        "applicationDate": app_date.strftime("%Y-%m-%d"),
        "applicationDateLabel": format_jp_mdw(app_date.strftime("%Y-%m-%d")),
        "cutoffAt": cutoff_dt.isoformat(timespec="seconds"),
        "cutoffTimeStr": f"{cutoff_hour:02d}:{cutoff_minute:02d}",
        "minutesToCutoff": minutes_to_cutoff,
        "executionDate": exec_date.strftime("%Y-%m-%d"),
        "executionDateLabel": format_jp_mdw(exec_date.strftime("%Y-%m-%d")),
        "nightsAfterCutoff": len(nights_after_cutoff),
        "nightsAfterCutoffDates": nights_after_cutoff,
        "unsettledUsNights": len(unsettled_us_nights),
        "unsettledUsNightDates": unsettled_us_nights,
        "jpHolidaysBetween": jp_holidays_between,
        "fomcDatesHit": fomc_dates_hit,
        "windowState": window_state,
        "windowLabel": window_label,
        "isOrderDayToday": (app_date == today and now_jst < cutoff_dt),
    }


# ============================================================================
# テクニカル・市場データ取得（キャッシュ併用で高速・低負荷）
# ============================================================================

def interp_linear(x: float, knots: List[Tuple[float, float]]) -> float:
    """ソート済み (x_i, y_i) 点列に対する区分線形補間（両端クランプ）。"""
    if x <= knots[0][0]:
        return knots[0][1]
    if x >= knots[-1][0]:
        return knots[-1][1]
    for i in range(1, len(knots)):
        x0, y0 = knots[i - 1]
        x1, y1 = knots[i]
        if x0 <= x <= x1:
            if abs(x1 - x0) < 1e-12:
                return y1
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return knots[-1][1]


def calc_wilder_rsi(closes: List[float], period: int = 14) -> float:
    """Wilder 方式の RSI(14) を算出する。"""
    if len(closes) < period + 1:
        return 50.0
    gains: List[float] = []
    losses: List[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(0.0, diff))
        losses.append(max(0.0, -diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss <= 1e-12:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def fetch_triple_technical_series(
    anchor_cache: Dict[str, Any],
    now_jst: datetime.datetime,
) -> Tuple[List[Tuple[str, float]], List[Tuple[str, float]]]:
    """^NDX (6か月日足) と ^VIX (3か月日足) をキャッシュ併用で取得する。"""
    store: Dict[str, Any] = anchor_cache.setdefault("triple_advisor_series", {})

    # 1. ^NDX 6か月日足（30分TTL）
    ndx_box: Dict[str, Any] = store.setdefault("ndx_6mo", {})
    need_ndx = True
    if ndx_box.get("fetchedAt") and ndx_box.get("bars"):
        try:
            dt = datetime.datetime.fromisoformat(str(ndx_box["fetchedAt"]))
            if (now_jst - dt).total_seconds() < 1800:
                need_ndx = False
        except Exception:
            pass

    if need_ndx:
        try:
            ch = build_data.fetch_yahoo_chart("^NDX", range_str="6mo", interval="1d", events=False)
            meta = ch.get("meta", {})
            tz_name = meta.get("exchangeTimezoneName", "America/New_York")
            gmtoff = meta.get("gmtoffset")
            dedup: Dict[str, float] = {}
            for t, c in zip(ch.get("timestamp", []), ch["indicators"]["quote"][0]["close"]):
                if c is not None:
                    dedup[build_data.get_bar_local_date(int(t), tz_name, gmtoff)] = round(float(c), 2)
            ndx_box["bars"] = sorted(dedup.items())[-130:]
            ndx_box["fetchedAt"] = now_jst.isoformat(timespec="seconds")
        except Exception as e:
            print(f"[WARN] ^NDX 6mo fetch failed: {e}", file=sys.stderr)

    # 2. ^VIX 3か月日足（10分TTL）
    vix_box: Dict[str, Any] = store.setdefault("vix_3mo", {})
    need_vix = True
    if vix_box.get("fetchedAt") and vix_box.get("bars"):
        try:
            dt = datetime.datetime.fromisoformat(str(vix_box["fetchedAt"]))
            if (now_jst - dt).total_seconds() < 600:
                need_vix = False
        except Exception:
            pass

    if need_vix:
        try:
            ch_v = build_data.fetch_yahoo_chart("^VIX", range_str="3mo", interval="1d", events=False)
            meta_v = ch_v.get("meta", {})
            tz_v = meta_v.get("exchangeTimezoneName", "America/Chicago")
            gmtoff_v = meta_v.get("gmtoffset")
            dedup_v: Dict[str, float] = {}
            for t, c in zip(ch_v.get("timestamp", []), ch_v["indicators"]["quote"][0]["close"]):
                if c is not None:
                    dedup_v[build_data.get_bar_local_date(int(t), tz_v, gmtoff_v)] = round(float(c), 2)
            reg_v = meta_v.get("regularMarketPrice")
            reg_t = meta_v.get("regularMarketTime")
            if reg_v is not None and reg_t is not None:
                d_live = build_data.get_bar_local_date(int(reg_t), tz_v, gmtoff_v)
                dedup_v[d_live] = round(float(reg_v), 2)
            vix_box["bars"] = sorted(dedup_v.items())[-70:]
            vix_box["cur"] = round(float(reg_v), 2) if reg_v is not None else None
            vix_box["asOf"] = (
                datetime.datetime.fromtimestamp(int(reg_t), JST).isoformat(timespec="seconds")
                if reg_t is not None
                else now_jst.isoformat(timespec="seconds")
            )
            vix_box["fetchedAt"] = now_jst.isoformat(timespec="seconds")
        except Exception as e:
            print(f"[WARN] ^VIX 3mo fetch failed: {e}", file=sys.stderr)

    ndx_bars: List[Tuple[str, float]] = [(str(d), float(v)) for d, v in ndx_box.get("bars", [])]
    vix_bars: List[Tuple[str, float]] = [(str(d), float(v)) for d, v in vix_box.get("bars", [])]
    return ndx_bars, vix_bars


def fetch_live_intraday_lead(
    launchpad_nqf: float,
    launchpad_nqf_asof: str,
    launchpad_fx: float,
    fx_locked_at_ttm: bool,
    now_jst: datetime.datetime,
) -> Dict[str, Any]:
    """発射台（CME 16:00 ET 清算値および 10:00 仲値）以降のリアルタイム時間外 NQ=F とドル円の先行リードを計算する。"""
    live_nqf = launchpad_nqf
    live_nqf_asof = launchpad_nqf_asof
    r_nq_lead = 0.0

    try:
        # build_data.run_build と同一URLのため同一プロセス内ではメモリキャッシュヒット（追加通信0回）
        ch_nq = build_data.fetch_yahoo_chart("NQ=F", range_str="10d", interval="15m", events=False)
        meta_nq = ch_nq.get("meta", {})
        reg_p = float(meta_nq["regularMarketPrice"])
        reg_t = int(meta_nq.get("regularMarketTime", int(now_jst.timestamp())))
        lp_dt = datetime.datetime.fromisoformat(launchpad_nqf_asof)
        reg_dt = datetime.datetime.fromtimestamp(reg_t, JST)
        # 発射台の清算時刻より新しい Globex 取引値がある場合のみ時間外リードとして計上（二重計上防止）
        if reg_dt > lp_dt + datetime.timedelta(minutes=5) and launchpad_nqf > 0:
            live_nqf = round(reg_p, 2)
            live_nqf_asof = reg_dt.isoformat(timespec="seconds")
            r_nq_lead = (reg_p - launchpad_nqf) / launchpad_nqf
    except Exception as e:
        print(f"[WARN] Live NQ=F intraday lead check failed: {e}", file=sys.stderr)

    live_fx_mid = launchpad_fx
    live_fx_asof = now_jst.isoformat(timespec="seconds")
    r_fx_lead = 0.0

    try:
        ch_fx = build_data.fetch_yahoo_chart("JPY=X", range_str="10d", interval="15m", events=False)
        meta_fx = ch_fx.get("meta", {})
        spot_bid = float(meta_fx["regularMarketPrice"])
        spot_t = int(meta_fx.get("regularMarketTime", int(now_jst.timestamp())))
        live_fx_asof = datetime.datetime.fromtimestamp(spot_t, JST).isoformat(timespec="seconds")
        spot_mid = round(spot_bid + build_data.YAHOO_FX_BID_TO_MID_SPREAD, 2)
        # D1 10:00 仲値で発射台の為替がロックされた後のみ、10:00 以降のスポット変動を先行リードに計上
        if fx_locked_at_ttm and launchpad_fx > 0:
            live_fx_mid = spot_mid
            r_fx_lead = (spot_mid - launchpad_fx) / launchpad_fx
        else:
            live_fx_mid = launchpad_fx
            r_fx_lead = 0.0
    except Exception as e:
        print(f"[WARN] Live JPY=X intraday lead check failed: {e}", file=sys.stderr)

    lead_total_raw = 3.0 * r_nq_lead + r_fx_lead
    return {
        "nqfSettled": round(launchpad_nqf, 2),
        "nqfLive": round(live_nqf, 2),
        "nqfLeadPct": round(r_nq_lead * 100.0, 2),
        "nqfTripleImpactPct": round(3.0 * r_nq_lead * 100.0, 2),
        "nqfAsOf": live_nqf_asof,
        "fxBaseForNext": round(launchpad_fx, 2),
        "fxLiveMid": round(live_fx_mid, 2),
        "fxLeadPct": round(r_fx_lead * 100.0, 2),
        "fxLockedAtTtm": fx_locked_at_ttm,
        "fxAsOf": live_fx_asof,
        "totalLeadRaw": lead_total_raw,
        "totalLeadPct": round(lead_total_raw * 100.0, 2),
    }


# ============================================================================
# 購入指示指数（0〜100点）＆ アドバイス生成コアエンジン
# ============================================================================

def find_triple_fund(app_data: Dict[str, Any]) -> Dict[str, Any]:
    """data.json データから auam-triple-ndx のエントリを取得する。"""
    for g in app_data.get("groups", []):
        for f in g.get("funds", []):
            if f.get("id") == TRIPLE_FUND_ID:
                return f
    raise KeyError(f"{TRIPLE_FUND_ID} が data.json 内に見つかりません")


def load_portfolio_config(nav_d0: int = 0) -> Dict[str, Any]:
    """portfolio.json から公開共通のリバランスルール設定（基本目標比率・モード・許容バンド・最低売買額）のみを読み込む。"""
    default_cfg: Dict[str, Any] = {
        "baseTargetPct": 50.0,
        "mode": "dynamic",
        "rebalanceBandPt": 4.0,
        "minTradeYen": 3000,
    }
    if PORTFOLIO_JSON_PATH.exists():
        try:
            loaded = json.loads(PORTFOLIO_JSON_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for k in ("baseTargetPct", "mode", "rebalanceBandPt", "minTradeYen"):
                    if k in loaded:
                        default_cfg[k] = loaded[k]
        except Exception as e:
            print(f"[WARN] Failed to load portfolio.json: {e}", file=sys.stderr)
    return default_cfg


def parse_hhmm_minutes(time_str: str, default_minutes: int = 15 * 60 + 30) -> int:
    """'15:30' 形式の時刻文字列を 0:00 からの経過分に変換する。"""
    if not time_str:
        return default_minutes
    parts = str(time_str).strip().split(":")
    if len(parts) < 2:
        return default_minutes
    try:
        h = int(parts[0])
        m = int(parts[1])
        return h * 60 + m
    except Exception:
        return default_minutes


def compute_order_execution_date(
    order_date_str: str,
    order_time_str: str = "15:00",
    broker: str = "SBI",
    broker_cutoffs: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """注文日・注文時刻・証券会社ごとの締め時刻から、実質申込日と約定予定日（翌営業日）を算出する（4-2）。"""
    cutoffs = {
        "SBI": "15:30",
        "楽天": "15:30",
        "マネックス": "15:30",
        "松井": "15:30",
    }
    if isinstance(broker_cutoffs, dict):
        for k, v in broker_cutoffs.items():
            if v:
                cutoffs[str(k)] = str(v)

    cutoff_str = cutoffs.get(str(broker or "SBI"), "15:30")
    cutoff_min = parse_hhmm_minutes(cutoff_str, 15 * 60 + 30)
    order_min = parse_hhmm_minutes(order_time_str, 15 * 60)

    order_date = datetime.datetime.strptime(str(order_date_str).strip(), "%Y-%m-%d").date()
    after_cutoff = order_min > cutoff_min

    if is_fund_order_day(order_date) and not after_cutoff:
        app_date = order_date
    else:
        cur = order_date if not after_cutoff else (order_date + datetime.timedelta(days=1))
        while not is_fund_order_day(cur):
            cur += datetime.timedelta(days=1)
        app_date = cur

    exec_date = build_data.next_jp_business_day(app_date)
    app_str = app_date.strftime("%Y-%m-%d")
    exec_str = exec_date.strftime("%Y-%m-%d")
    return {
        "applicationDate": app_str,
        "applicationDateLabel": format_jp_mdw(app_str),
        "executionDate": exec_str,
        "executionDateLabel": format_jp_mdw(exec_str),
        "cutoffTime": cutoff_str,
        "isAfterCutoff": after_cutoff,
    }


def get_orders_credentials() -> Tuple[str, str]:
    """環境変数（またはローカル非公開ファイル）から ORDERS_URL と ORDERS_KEY を取得する（値はログに出さない）。"""
    orders_url = os.environ.get("ORDERS_URL", "").strip()
    orders_key = os.environ.get("ORDERS_KEY", "").strip()
    if (not orders_url or not orders_key) and LOCAL_ORDERS_SECRET_PATH.exists():
        try:
            sec = json.loads(LOCAL_ORDERS_SECRET_PATH.read_text(encoding="utf-8"))
            if not orders_url:
                orders_url = str(sec.get("ORDERS_URL") or "").strip()
            if not orders_key:
                orders_key = str(sec.get("ORDERS_KEY") or "").strip()
        except Exception:
            pass
    if not orders_url:
        orders_url = DEFAULT_ORDERS_URL
    return orders_url, orders_key


def fetch_orders_from_gas(
    orders_url: Optional[str] = None,
    orders_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """GAS Web アプリから action=list で orders と settings を取得する。"""
    if orders_url is None or orders_key is None:
        u, k = get_orders_credentials()
        orders_url = orders_url or u
        orders_key = orders_key or k
    if not orders_url or not orders_key:
        return None
    try:
        resp = requests.get(
            orders_url,
            params={"key": orders_key, "action": "list"},
            headers={"User-Agent": build_data.USER_AGENT},
            timeout=20,
        )
        if resp.status_code >= 400:
            print(f"[WARN] GAS list HTTP {resp.status_code}", file=sys.stderr)
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[WARN] GAS list error: {data.get('error') if isinstance(data, dict) else 'unknown'}", file=sys.stderr)
            return None
        return data
    except Exception as e:
        print(f"[WARN] Failed to fetch orders from GAS: {e}", file=sys.stderr)
        return None


def post_orders_to_gas(
    payload: Dict[str, Any],
    orders_url: Optional[str] = None,
    orders_key: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """GAS Web アプリへ JSON ペイロードを POST（Content-Type: text/plain）する。"""
    if orders_url is None or orders_key is None:
        u, k = get_orders_credentials()
        orders_url = orders_url or u
        orders_key = orders_key or k
    if not orders_url or not orders_key:
        return None
    body = dict(payload)
    body["key"] = orders_key
    try:
        resp = requests.post(
            orders_url,
            headers={
                "User-Agent": build_data.USER_AGENT,
                "Content-Type": "text/plain;charset=utf-8",
            },
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            timeout=25,
        )
        if resp.status_code >= 400:
            print(f"[WARN] GAS POST HTTP {resp.status_code}", file=sys.stderr)
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            print(f"[WARN] GAS POST error: {data.get('error') if isinstance(data, dict) else 'unknown'}", file=sys.stderr)
            return None
        return data
    except Exception as e:
        print(f"[WARN] Failed to POST to GAS: {e}", file=sys.stderr)
        return None


def settle_pending_orders_if_needed(
    fund_history: List[List[Any]],
    latest_nav_date: str,
    latest_nav: int,
    gas_data: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """基準価額が出ている pending 注文があれば GAS の action=settle で約定確定させる（同じ注文を2回 settle しない）。"""
    orders_url, orders_key = get_orders_credentials()
    if not orders_url or not orders_key:
        return gas_data

    if gas_data is None:
        gas_data = fetch_orders_from_gas(orders_url, orders_key)
    if not gas_data or not isinstance(gas_data.get("orders"), list):
        return gas_data

    nav_by_date: Dict[str, int] = {}
    for item in fund_history or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            d_s = str(item[0]).strip()
            try:
                v_i = int(item[1])
                if d_s and v_i > 0:
                    nav_by_date[d_s] = v_i
            except Exception:
                pass
    if latest_nav_date and int(latest_nav or 0) > 0:
        nav_by_date[str(latest_nav_date).strip()] = int(latest_nav)

    settings = gas_data.get("settings") if isinstance(gas_data.get("settings"), dict) else {}
    broker_cutoffs = settings.get("brokers") if isinstance(settings.get("brokers"), dict) else {}

    to_settle_dates: Dict[str, int] = {}
    for ord_item in gas_data.get("orders", []):
        if not isinstance(ord_item, dict):
            continue
        if str(ord_item.get("status", "")).lower() != "pending":
            continue
        exec_d = str(ord_item.get("execDate") or "").strip()
        if not exec_d and ord_item.get("orderDate"):
            try:
                sched_info = compute_order_execution_date(
                    order_date_str=str(ord_item["orderDate"]),
                    order_time_str=str(ord_item.get("orderTime") or "15:00"),
                    broker=str(ord_item.get("broker") or "SBI"),
                    broker_cutoffs=broker_cutoffs,
                )
                exec_d = sched_info["executionDate"]
                if ord_item.get("id"):
                    post_orders_to_gas(
                        {"action": "update", "id": ord_item["id"], "execDate": exec_d},
                        orders_url=orders_url,
                        orders_key=orders_key,
                    )
            except Exception:
                exec_d = ""
        if exec_d and exec_d in nav_by_date:
            to_settle_dates[exec_d] = nav_by_date[exec_d]

    if not to_settle_dates:
        return gas_data

    settle_res = post_orders_to_gas(
        {"action": "settle", "navByDate": to_settle_dates},
        orders_url=orders_url,
        orders_key=orders_key,
    )
    if settle_res and settle_res.get("ok"):
        settled_cnt = int(settle_res.get("settledCount") or 0)
        if settled_cnt > 0:
            print(f"[SETTLE] Settled {settled_cnt} pending order(s) for dates {list(to_settle_dates.keys())}.", flush=True)
        return settle_res
    return gas_data


def compute_balance_from_orders(
    orders: List[Dict[str, Any]],
    settings: Dict[str, Any],
    provisional_nav: int,
) -> Dict[str, Any]:
    """注文記録から保有口数・平均取得単価（移動平均法）・現金・約定待ち反映後残高・売却概算税を計算する（4-2・4-3）。"""
    budget_yen = int(round(float(settings.get("budgetYen", 0))))

    def _sort_key(o: Dict[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(o.get("execDate") or o.get("orderDate") or ""),
            f"{o.get('orderDate', '')} {o.get('orderTime', '')}",
            str(o.get("createdAt") or ""),
            str(o.get("id") or ""),
        )

    sorted_orders = sorted([o for o in orders if isinstance(o, dict)], key=_sort_key)

    settled_units = 0
    avg_cost_nav = 0  # 1万口あたり移動平均取得単価（円）
    settled_buy_yen_total = 0
    settled_sell_gross_yen_total = 0
    settled_sell_tax_yen_total = 0
    settled_sell_net_yen_total = 0

    pending_buy_yen = 0
    pending_buy_est_units = 0
    pending_sell_units = 0
    pending_sell_est_yen = 0
    pending_orders_list: List[Dict[str, Any]] = []
    order_tax_map: Dict[str, Dict[str, int]] = {}

    for o in sorted_orders:
        status = str(o.get("status") or "pending").lower()
        if status == "cancelled":
            continue
        side = str(o.get("side") or "BUY").upper()
        spec = str(o.get("spec") or "YEN").upper()

        if status == "settled":
            e_nav = float(o.get("execNav") or 0)
            e_units = int(round(float(o.get("execUnits") or 0)))
            e_yen = int(round(float(o.get("execYen") or 0)))

            if side == "BUY":
                if e_units <= 0 and e_nav > 0 and o.get("amountYen"):
                    e_units = math.floor(float(o["amountYen"]) * 10000.0 / e_nav)
                if e_yen <= 0 and o.get("amountYen"):
                    e_yen = int(round(float(o["amountYen"])))
                if e_units > 0 and e_yen > 0:
                    prev_cost = (settled_units * avg_cost_nav) / 10000.0
                    new_cost = prev_cost + e_yen
                    settled_units += e_units
                    avg_cost_nav = int(round((new_cost * 10000.0) / settled_units)) if settled_units > 0 else 0
                    settled_buy_yen_total += e_yen
            elif side == "SELL":
                if e_units <= 0:
                    if spec == "UNITS" and o.get("units"):
                        e_units = int(round(float(o["units"])))
                    elif spec == "YEN" and o.get("amountYen") and e_nav > 0:
                        e_units = math.ceil(float(o["amountYen"]) * 10000.0 / e_nav)
                sell_u = min(settled_units, max(0, e_units))
                if e_yen <= 0 and e_nav > 0 and sell_u > 0:
                    e_yen = math.floor(sell_u * e_nav / 10000.0)
                if sell_u > 0:
                    cost_basis_yen = int(round((avg_cost_nav * sell_u) / 10000.0))
                    gain_yen = e_yen - cost_basis_yen
                    est_tax_yen = math.floor(gain_yen * TAX_RATE_SPECIFIED_ACCOUNT) if gain_yen > 0 else 0
                    net_proceeds_yen = e_yen - est_tax_yen

                    settled_units = max(0, settled_units - sell_u)
                    if settled_units == 0:
                        avg_cost_nav = 0
                    settled_sell_gross_yen_total += e_yen
                    settled_sell_tax_yen_total += est_tax_yen
                    settled_sell_net_yen_total += net_proceeds_yen
                    if o.get("id"):
                        order_tax_map[str(o["id"])] = {
                            "costBasisYen": cost_basis_yen,
                            "gainYen": gain_yen,
                            "estimatedTaxYen": est_tax_yen,
                            "netProceedsYen": net_proceeds_yen,
                        }
        elif status == "pending":
            pending_orders_list.append(o)
            if side == "BUY":
                if spec == "YEN" and o.get("amountYen"):
                    amt = int(round(float(o["amountYen"])))
                    est_u = math.floor(amt * 10000.0 / provisional_nav) if provisional_nav > 0 else 0
                else:
                    est_u = int(round(float(o.get("units") or 0)))
                    amt = math.floor(est_u * provisional_nav / 10000.0) if provisional_nav > 0 else 0
                pending_buy_yen += amt
                pending_buy_est_units += est_u
            elif side == "SELL":
                if spec == "UNITS" and o.get("units"):
                    u_s = int(round(float(o["units"])))
                elif spec == "YEN" and o.get("amountYen") and provisional_nav > 0:
                    u_s = math.ceil(float(o["amountYen"]) * 10000.0 / provisional_nav)
                else:
                    u_s = 0
                u_s = min(max(0, settled_units - pending_sell_units), max(0, u_s))
                est_y = math.floor(u_s * provisional_nav / 10000.0) if provisional_nav > 0 else 0
                pending_sell_units += u_s
                pending_sell_est_yen += est_y

    settled_cash_yen = budget_yen - settled_buy_yen_total + settled_sell_net_yen_total
    cash_yen = settled_cash_yen - pending_buy_yen
    effective_holding_units = max(0, settled_units - pending_sell_units) + pending_buy_est_units
    effective_cash_yen = max(0, cash_yen)

    return {
        "budgetYen": budget_yen,
        "holdingUnits": settled_units,
        "avgCostNav": avg_cost_nav,
        "settledCashYen": settled_cash_yen,
        "cashYen": cash_yen,
        "effectiveHoldingUnits": effective_holding_units,
        "effectiveCashYen": effective_cash_yen,
        "settledBuyYenTotal": settled_buy_yen_total,
        "settledSellGrossYenTotal": settled_sell_gross_yen_total,
        "settledSellTaxYenTotal": settled_sell_tax_yen_total,
        "settledSellNetYenTotal": settled_sell_net_yen_total,
        "pendingBuyYen": pending_buy_yen,
        "pendingBuyEstUnits": pending_buy_est_units,
        "pendingSellUnits": pending_sell_units,
        "pendingSellEstYen": pending_sell_est_yen,
        "pendingOrdersCount": len(pending_orders_list),
        "orderTaxMap": order_tax_map,
    }


def calc_dynamic_target_pct(base_target_pct: float, score: int) -> float:
    """購入指示指数（0〜100点）に応じて基本比率（例: 50%）を押し目時最大+22pt・過熱時-15ptシフトした目標投信比率を返す。"""
    tilt_pt = interp_linear(
        float(score),
        [
            (0.0, -15.0),
            (15.0, -14.0),
            (25.0, -8.5),
            (35.0, -3.0),
            (45.0, 0.0),
            (55.0, 0.0),
            (65.0, 8.0),
            (75.0, 14.0),
            (85.0, 19.0),
            (100.0, 22.0),
        ],
    )
    return round(max(25.0, min(78.0, base_target_pct + tilt_pt)), 1)


def _solve_single_mode_rebalance(
    target_pct: float,
    holding_units: int,
    cash_yen: int,
    prov_nav: int,
    band_pt: float,
    min_trade_yen: int,
    lead_pct: float,
    nights_cnt: int,
    vix_cur: float,
    fomc_hit: bool,
) -> Dict[str, Any]:
    """指定された目標投信比率に対するリバランス売買計画（金額指定買い増し／口数指定利確売却／ホールド）を計算する。"""
    cur_fund_yen = int(round(holding_units * prov_nav / 10000.0))
    total_yen = cur_fund_yen + cash_yen
    cur_weight_pct = round((cur_fund_yen / total_yen * 100.0), 2) if total_yen > 0 else 0.0
    target_fund_yen = int(round(total_yen * (target_pct / 100.0)))
    raw_diff_yen = target_fund_yen - cur_fund_yen  # 正=買い不足、負=売り超過
    drift_pt = round(cur_weight_pct - target_pct, 2)  # 負=アンダーウェイト(買い)、正=オーバーウェイト(利確売り)

    action = "HOLD"
    order_method = "なし"
    trade_yen = 0
    trade_units = 0
    cap_note: Optional[str] = None

    # 1. アンダーウェイト → 現金から買い増しリバランス（金額指定）
    if drift_pt <= -band_pt and raw_diff_yen >= min_trade_yen and cash_yen >= min_trade_yen:
        if lead_pct >= 4.5:
            action = "HOLD_BOUNCE_TRAP"
            cap_note = f"昼間の時間外リードが {lead_pct:+.2f}% 急反発中のため本日の買い増しは見送り推奨"
        else:
            max_cash_frac = 0.45
            if fomc_hit:
                max_cash_frac = 0.15
                cap_note = "FOMC発表またぎのため1回の買付上限を現金の15%に抑制"
            elif nights_cnt >= 3 or vix_cur >= 35.0:
                max_cash_frac = 0.18
                cap_note = f"米国市場{nights_cnt}晩またぎ/高ボラのため1回の買付上限を現金の18%に抑制"
            elif nights_cnt == 2:
                max_cash_frac = 0.25
                cap_note = f"祝日挟み（米国市場{nights_cnt}晩分）のため1回の買付上限を現金の25%に抑制"

            cash_cap_yen = int(cash_yen * max_cash_frac)
            desired_yen = raw_diff_yen
            if cash_cap_yen >= min_trade_yen and desired_yen > cash_cap_yen:
                desired_yen = cash_cap_yen
            else:
                cap_note = None

            rounded_buy = int(round(desired_yen / 1000.0)) * 1000
            rounded_buy = min(cash_yen, max(0, rounded_buy))
            if rounded_buy >= min_trade_yen:
                action = "BUY"
                order_method = "金額指定買付"
                trade_yen = rounded_buy
                trade_units = int(round(rounded_buy / prov_nav * 10000.0)) if prov_nav > 0 else 0

    # 2. オーバーウェイト → 利確売却リバランス（口数指定）
    elif drift_pt >= band_pt and (-raw_diff_yen) >= min_trade_yen and holding_units > 0:
        if lead_pct <= -4.5:
            action = "HOLD_CRASH_GUARD"
            cap_note = f"昼間の時間外リードが {lead_pct:+.2f}% 急落中のため本日の売却は見送り推奨"
        else:
            excess_yen = -raw_diff_yen
            if nights_cnt >= 2:
                excess_yen = int(round(excess_yen * 0.75))
                cap_note = f"祝日挟み（米国市場{nights_cnt}晩分）のため1回の利確売却を超過分の75%に調整"

            raw_sell_units = (excess_yen / prov_nav * 10000.0) if prov_nav > 0 else 0.0
            rounded_units = int(round(raw_sell_units / 100.0)) * 100
            rounded_units = min(holding_units, max(0, rounded_units))
            est_sell_yen = int(round(rounded_units * prov_nav / 10000.0))
            if est_sell_yen >= min_trade_yen and rounded_units >= 100:
                action = "SELL"
                order_method = "口数指定売却"
                trade_units = rounded_units
                trade_yen = est_sell_yen

    if action == "BUY":
        after_fund_yen = cur_fund_yen + trade_yen
        after_cash_yen = cash_yen - trade_yen
        after_units = holding_units + trade_units
    elif action == "SELL":
        after_fund_yen = max(0, cur_fund_yen - trade_yen)
        after_cash_yen = cash_yen + trade_yen
        after_units = max(0, holding_units - trade_units)
    else:
        after_fund_yen = cur_fund_yen
        after_cash_yen = cash_yen
        after_units = holding_units

    after_total = after_fund_yen + after_cash_yen
    after_weight_pct = round((after_fund_yen / after_total * 100.0), 2) if after_total > 0 else 0.0

    return {
        "targetWeightPct": target_pct,
        "targetFundYen": target_fund_yen,
        "rawDiffYen": raw_diff_yen,
        "driftPt": drift_pt,
        "bandPt": band_pt,
        "action": action,
        "orderMethod": order_method,
        "tradeYen": trade_yen,
        "tradeUnits": trade_units,
        "capNote": cap_note,
        "afterFundYen": after_fund_yen,
        "afterCashYen": after_cash_yen,
        "afterUnits": after_units,
        "afterWeightPct": after_weight_pct,
    }


def compute_portfolio_rebalance(
    portfolio_cfg: Dict[str, Any],
    nav_d0: int,
    launchpad_nav: int,
    prov_nav: int,
    score: int,
    lead_pct: float,
    nights_cnt: int,
    vix_cur: float,
    fomc_hit: bool,
) -> Dict[str, Any]:
    """現在ポートフォリオの評価額推移と、動的リバランス・固定50:50リバランスの両プランおよび次回発動目安価額を計算する。"""
    holding_units = int(portfolio_cfg.get("holdingUnits", 0))
    cash_yen = int(portfolio_cfg.get("cashYen", 0))
    base_target_pct = float(portfolio_cfg.get("baseTargetPct", 50.0))
    mode = str(portfolio_cfg.get("mode", "dynamic"))
    band_pt = float(portfolio_cfg.get("rebalanceBandPt", 4.0))
    min_trade_yen = int(portfolio_cfg.get("minTradeYen", 3000))

    d0_fund_yen = int(round(holding_units * nav_d0 / 10000.0))
    d0_total_yen = d0_fund_yen + cash_yen
    d0_weight_pct = round((d0_fund_yen / d0_total_yen * 100.0), 2) if d0_total_yen > 0 else 0.0

    lp_fund_yen = int(round(holding_units * launchpad_nav / 10000.0))
    lp_total_yen = lp_fund_yen + cash_yen
    lp_weight_pct = round((lp_fund_yen / lp_total_yen * 100.0), 2) if lp_total_yen > 0 else 0.0

    cur_fund_yen = int(round(holding_units * prov_nav / 10000.0))
    cur_total_yen = cur_fund_yen + cash_yen
    cur_weight_pct = round((cur_fund_yen / cur_total_yen * 100.0), 2) if cur_total_yen > 0 else 0.0

    dyn_target_pct = calc_dynamic_target_pct(base_target_pct, score)
    fix_target_pct = round(max(10.0, min(90.0, base_target_pct)), 1)

    dynamic_plan = _solve_single_mode_rebalance(
        target_pct=dyn_target_pct,
        holding_units=holding_units,
        cash_yen=cash_yen,
        prov_nav=prov_nav,
        band_pt=band_pt,
        min_trade_yen=min_trade_yen,
        lead_pct=lead_pct,
        nights_cnt=nights_cnt,
        vix_cur=vix_cur,
        fomc_hit=fomc_hit,
    )
    fixed_plan = _solve_single_mode_rebalance(
        target_pct=fix_target_pct,
        holding_units=holding_units,
        cash_yen=cash_yen,
        prov_nav=prov_nav,
        band_pt=band_pt,
        min_trade_yen=min_trade_yen,
        lead_pct=lead_pct,
        nights_cnt=nights_cnt,
        vix_cur=vix_cur,
        fomc_hit=fomc_hit,
    )

    # 平常中立基準（baseTargetPct ± bandPt）での機械的な次回買い／利確売り発動基準価額の目安
    # (今回の推奨売買を実行した後の口数・現金を基準に「次にいくらで利確・買い増しになるか」を算出)
    active_plan = dynamic_plan if mode == "dynamic" else fixed_plan
    ref_units = active_plan["afterUnits"]
    ref_cash = active_plan["afterCashYen"]

    def _nav_for_weight(w_pct: float) -> Optional[int]:
        w = w_pct / 100.0
        if ref_units <= 0 or ref_cash <= 0 or w <= 0.01 or w >= 0.99:
            return None
        return int(round((10000.0 * ref_cash / ref_units) * (w / (1.0 - w))))

    # 買い増し後に相場が反発して押し目解消（中立50%目標）に戻り利確売りが発動する目安基準価額（必ず現在値より上）
    raw_neutral_sell = _nav_for_weight(min(90.0, base_target_pct + band_pt))
    if raw_neutral_sell is not None:
        min_rebound_nav = int(round(prov_nav * 1.115)) if active_plan["action"] == "BUY" else int(round(prov_nav * 1.04))
        neutral_sell_nav: Optional[int] = max(raw_neutral_sell, min_rebound_nav)
    else:
        neutral_sell_nav = None

    # さらに続落して次回（第2弾）買い増しが発動する目安基準価額（必ず現在値より下）
    next_dip_target = max(15.0, active_plan["afterWeightPct"] - band_pt)
    raw_next_buy = _nav_for_weight(next_dip_target)
    if raw_next_buy is not None:
        next_buy_nav: Optional[int] = min(raw_next_buy, int(round(prov_nav * 0.92)))
    else:
        next_buy_nav = None

    return {
        "config": {
            "setupDate": portfolio_cfg.get("setupDate"),
            "setupNav": portfolio_cfg.get("setupNav"),
            "investedYenAtSetup": portfolio_cfg.get("investedYenAtSetup"),
            "holdingUnits": holding_units,
            "cashYen": cash_yen,
            "baseTargetPct": base_target_pct,
            "mode": mode,
            "rebalanceBandPt": band_pt,
            "minTradeYen": min_trade_yen,
        },
        "valuation": {
            "d0FundYen": d0_fund_yen,
            "d0TotalYen": d0_total_yen,
            "d0WeightPct": d0_weight_pct,
            "launchpadFundYen": lp_fund_yen,
            "launchpadTotalYen": lp_total_yen,
            "launchpadWeightPct": lp_weight_pct,
            "currentFundYen": cur_fund_yen,
            "currentCashYen": cash_yen,
            "currentTotalYen": cur_total_yen,
            "currentWeightPct": cur_weight_pct,
            "diffFromD0Yen": cur_total_yen - d0_total_yen,
        },
        "activeMode": mode,
        "activePlan": active_plan,
        "dynamicPlan": dynamic_plan,
        "fixedPlan": fixed_plan,
        "nextTriggersAfterAction": {
            "profitTakeNavAtNeutral": neutral_sell_nav,
            "nextAddBuyNav": next_buy_nav,
        },
    }


def build_triple_advisor_data(
    app_data: Optional[Dict[str, Any]] = None,
    now_jst: Optional[datetime.datetime] = None,
    output_path: pathlib.Path = TRIPLE_DATA_JSON_PATH,
) -> Dict[str, Any]:
    """トリプルNASDAQ専用の購入指示指数・アドバイスデータを生成して triple_data.json に保存する。"""
    if now_jst is None:
        now_jst = datetime.datetime.now(JST)
    if app_data is None:
        app_data = json.loads(build_data.DATA_JSON_PATH.read_text(encoding="utf-8"))

    fund = find_triple_fund(app_data)
    anchor_cache = build_data.load_anchor_cache()
    ndx_bars, vix_bars = fetch_triple_technical_series(anchor_cache, now_jst)
    build_data.save_anchor_cache(anchor_cache)

    nav_d0 = int(fund["nav"])
    d0_str = str(fund["navDate"])
    d1_str = str(fund["targetDate"])
    launchpad_nav = int(fund["forecast"])
    launchpad_diff = int(fund.get("diff", launchpad_nav - nav_d0))
    launchpad_chg_pct = float(fund["change"])

    b_idx = fund.get("breakdown", {}).get("index", {})
    b_fx = fund.get("breakdown", {}).get("fx", {})
    lp_nqf_base = float(b_idx.get("base") or 0.0)
    lp_nqf_cur = float(b_idx.get("cur") or 0.0)
    lp_nqf_chg = float(b_idx.get("change") or 0.0)
    lp_nqf_asof = str(b_idx.get("asOf") or app_data.get("updatedAt") or now_jst.isoformat())

    lp_fx_base = float(b_fx.get("base") or app_data.get("fx", {}).get("base") or 155.0)
    lp_fx_cur = float(b_fx.get("cur") or app_data.get("fx", {}).get("cur") or lp_fx_base)
    lp_fx_chg = float(b_fx.get("change") or 0.0)

    d1_10am = datetime.datetime.strptime(d1_str, "%Y-%m-%d").replace(hour=10, minute=0, tzinfo=JST)
    fx_locked_at_ttm = (now_jst >= d1_10am) and (not bool(app_data.get("fx", {}).get("estimated", True)))

    # 発射台に反映済みの米国取引日を特定
    markets_by_id = {m["id"]: m for m in app_data.get("markets", []) if isinstance(m, dict) and "id" in m}
    lp_us_date_str = d0_str
    if ndx_bars:
        eligible_ndx = [d for d, _ in ndx_bars if d < d1_str]
        if eligible_ndx:
            lp_us_date_str = eligible_ndx[-1]

    # 1. 昼間の時間外リードと暫定約定スタート目安
    lead_info = fetch_live_intraday_lead(
        launchpad_nqf=lp_nqf_cur,
        launchpad_nqf_asof=lp_nqf_asof,
        launchpad_fx=lp_fx_cur,
        fx_locked_at_ttm=fx_locked_at_ttm,
        now_jst=now_jst,
    )
    provisional_exec_nav = int(round(launchpad_nav * (1.0 + lead_info["totalLeadRaw"])))
    provisional_vs_d0_pct = round((provisional_exec_nav - nav_d0) / nav_d0 * 100.0, 2) if nav_d0 > 0 else 0.0

    # 2. 申込・約定スケジュール（15:30標準 と 15:00証券会社向けの両方）
    sched_1530 = compute_order_schedule(now_jst, lp_us_date_str, cutoff_hour=15, cutoff_minute=30)
    sched_1500 = compute_order_schedule(now_jst, lp_us_date_str, cutoff_hour=15, cutoff_minute=0)

    # 3. テクニカル指標（^NDX 60日高値ドローダウン・RSI14・25日線乖離・3日累積・VIX）
    ndx_closes = [v for _, v in ndx_bars]
    last_ndx_close = ndx_closes[-1] if ndx_closes else (markets_by_id.get("ndx", {}).get("cur") or 28000.0)
    # 時間外 NQ=F のリード分を加味した現在相当の NASDAQ100 水準
    implied_ndx = round(float(last_ndx_close) * (1.0 + lead_info["nqfLeadPct"] / 100.0), 2)

    ndx_series_for_tech = list(ndx_closes)
    if abs(lead_info["nqfLeadPct"]) >= 0.05 and ndx_series_for_tech:
        ndx_series_for_tech.append(implied_ndx)

    recent_60_ndx = ndx_series_for_tech[-60:] if ndx_series_for_tech else [implied_ndx]
    ndx_high_60d = max(recent_60_ndx)
    dd_ndx_pct = ((implied_ndx - ndx_high_60d) / ndx_high_60d * 100.0) if ndx_high_60d > 0 else 0.0
    dd_triple_equiv_pct = round(dd_ndx_pct * 2.85, 1)

    sma25_ndx = (
        sum(ndx_series_for_tech[-25:]) / min(25, len(ndx_series_for_tech))
        if ndx_series_for_tech
        else implied_ndx
    )
    sma25_dev_pct = ((implied_ndx - sma25_ndx) / sma25_ndx * 100.0) if sma25_ndx > 0 else 0.0
    rsi14_ndx = calc_wilder_rsi(ndx_series_for_tech, period=14)

    # ファンド基準価額履歴 + 発射台から直近3営業日累積変化率を算出
    hist_list: List[List[Any]] = fund.get("history", [])
    nav_series_with_lp: List[int] = [int(x[1]) for x in hist_list if isinstance(x, list) and len(x) >= 2]
    if not nav_series_with_lp or nav_series_with_lp[-1] != nav_d0:
        nav_series_with_lp.append(nav_d0)
    nav_series_with_lp.append(launchpad_nav)

    if len(nav_series_with_lp) >= 4 and nav_series_with_lp[-4] > 0:
        cum_3d_pct = (nav_series_with_lp[-1] - nav_series_with_lp[-4]) / nav_series_with_lp[-4] * 100.0
    else:
        cum_3d_pct = launchpad_chg_pct

    vix_cur = vix_bars[-1][1] if vix_bars else 18.0
    vix_prev = vix_bars[-2][1] if len(vix_bars) >= 2 else vix_cur
    vix_chg = round(vix_cur - vix_prev, 2)

    # ========================================================================
    # 4. 購入指示指数（5軸スコアリング：合計 0〜100点）
    # ========================================================================

    # 【軸1】発射台の急落・押し目度（0〜30点）
    effective_dip_pct = min(launchpad_chg_pct, cum_3d_pct * 0.72)
    s_axis1 = round(
        interp_linear(
            effective_dip_pct,
            [
                (-15.0, 30.0),
                (-10.0, 28.0),
                (-6.0, 22.0),
                (-3.0, 15.0),
                (0.0, 8.0),
                (4.0, 4.0),
                (9.0, 0.0),
            ],
        ),
        1,
    )

    # 【軸2】NASDAQ100 60日高値からの調整深さ（0〜25点）
    s_axis2 = round(
        interp_linear(
            dd_ndx_pct,
            [
                (-16.0, 25.0),
                (-10.0, 22.0),
                (-6.0, 17.0),
                (-3.5, 12.5),
                (-1.5, 7.0),
                (0.0, 3.0),
            ],
        ),
        1,
    )

    # 【軸3】テクニカル売られすぎ度：RSI(14) [0〜12] + 25日線乖離 [0〜8] = 0〜20点
    s_rsi = interp_linear(
        rsi14_ndx,
        [
            (25.0, 12.0),
            (32.0, 11.0),
            (40.0, 8.5),
            (50.0, 5.5),
            (60.0, 2.5),
            (70.0, 0.0),
        ],
    )
    s_sma25 = interp_linear(
        sma25_dev_pct,
        [
            (-8.0, 8.0),
            (-4.0, 6.5),
            (0.0, 4.0),
            (3.0, 1.5),
            (6.0, 0.0),
        ],
    )
    s_axis3 = round(s_rsi + s_sma25, 1)

    # 【軸4】昼間の時間外リード・高値掴み回避フィルター（-20〜+15点、中立+8点）
    lead_pct = lead_info["totalLeadPct"]
    s_axis4 = round(
        interp_linear(
            lead_pct,
            [
                (-6.0, 15.0),
                (-3.0, 13.0),
                (-1.0, 10.0),
                (0.0, 8.0),
                (1.5, 5.0),
                (3.0, 0.0),
                (4.5, -10.0),
                (6.5, -20.0),
            ],
        ),
        1,
    )

    # 【軸5】VIX恐怖指数 & 日程リスク補正（-15〜+10点）
    s_vix = interp_linear(
        vix_cur,
        [
            (12.0, 4.0),
            (16.0, 5.0),
            (22.0, 8.0),
            (28.0, 10.0),
            (34.0, 5.0),
            (42.0, -5.0),
        ],
    )
    nights_cnt = sched_1530["nightsAfterCutoff"]
    sched_penalty = 0.0
    if nights_cnt == 2:
        sched_penalty -= 6.0
    elif nights_cnt >= 3:
        sched_penalty -= 12.0
    if sched_1530["fomcDatesHit"]:
        sched_penalty -= 8.0
    s_axis5 = round(max(-15.0, min(10.0, s_vix + sched_penalty)), 1)

    raw_total = s_axis1 + s_axis2 + s_axis3 + s_axis4 + s_axis5
    score = int(round(max(0.0, min(100.0, raw_total))))

    intraday_bounce_trap = lead_pct >= 4.5

    # 5. ランク・推奨投入割合（待機資金に対する1回あたり金額指定比率）の決定
    if intraday_bounce_trap:
        rank = "D"
        rank_label = "昼間急反発・高値掴み警戒"
        base_alloc_pct = 0
    elif score >= 85:
        rank = "S"
        rank_label = "極上バーゲン押し目（強い買い）"
        base_alloc_pct = 45
    elif score >= 80:
        rank = "S"
        rank_label = "大口押し目好機（強い買い）"
        base_alloc_pct = 35
    elif score >= 72:
        rank = "A"
        rank_label = "押し目買い好機（分割買い推奨）"
        base_alloc_pct = 30
    elif score >= 65:
        rank = "A"
        rank_label = "買い好機（分割エントリーOK）"
        base_alloc_pct = 25
    elif score >= 58:
        rank = "B"
        rank_label = "打診買い圏（慎重に少額分割）"
        base_alloc_pct = 15
    elif score >= 50:
        rank = "B"
        rank_label = "軽めの押し目（少額打診ならOK）"
        base_alloc_pct = 10
    elif score >= 35:
        rank = "C"
        rank_label = "様子見・待機（押し目待ち）"
        base_alloc_pct = 0
    else:
        rank = "D"
        rank_label = "高値圏・見送り（キャッシュ温存）"
        base_alloc_pct = 0

    alloc_pct = base_alloc_pct
    alloc_cap_reasons: List[str] = []
    if alloc_pct > 0:
        if nights_cnt == 2 and alloc_pct > 20:
            alloc_pct = 20
            alloc_cap_reasons.append(f"祝日挟みで米国市場{nights_cnt}晩分をまたぐため上限20%に抑制")
        elif nights_cnt >= 3 and alloc_pct > 15:
            alloc_pct = 15
            alloc_cap_reasons.append(f"連休で米国市場{nights_cnt}晩分をまたぐため上限15%に抑制")
        if vix_cur >= 35.0 and alloc_pct > 15:
            alloc_pct = 15
            alloc_cap_reasons.append(f"VIX {vix_cur:.1f} の高ボラ乱高下環境のため上限15%に抑制")
        if sched_1530["fomcDatesHit"] and alloc_pct > 10:
            alloc_pct = 10
            alloc_cap_reasons.append("FOMC政策金利発表をまたぐため上限10%に抑制")

    # 5b. 公開データ用の目標投信比率計算（4-4: 個人の保有口数・現金・金額は公開データに含めない）
    portfolio_cfg = load_portfolio_config(nav_d0)
    base_target_pct = float(portfolio_cfg.get("baseTargetPct", 50.0))
    reb_mode = str(portfolio_cfg.get("mode", "dynamic"))
    band_pt = float(portfolio_cfg.get("rebalanceBandPt", 4.0))
    min_trade_yen = int(portfolio_cfg.get("minTradeYen", 3000))
    dyn_target_pct = calc_dynamic_target_pct(base_target_pct, score)
    fix_target_pct = round(max(10.0, min(90.0, base_target_pct)), 1)

    rebalance_data: Dict[str, Any] = {
        "config": {
            "baseTargetPct": base_target_pct,
            "mode": reb_mode,
            "rebalanceBandPt": band_pt,
            "minTradeYen": min_trade_yen,
        },
        "activeMode": reb_mode,
        "dynamicTargetWeightPct": dyn_target_pct,
        "fixedTargetWeightPct": fix_target_pct,
    }

    # 6. 時間帯×スコアに応じた市場側の公開アクション指示文の生成（個人の金額は含めない）
    w_state = sched_1530["windowState"]
    app_lbl = sched_1530["applicationDateLabel"]
    exec_lbl = sched_1530["executionDateLabel"]
    order_ok_level = (not intraday_bounce_trap) and (score >= 58 or score <= 30)
    order_ready_now = (w_state == "golden_window") and order_ok_level

    if intraday_bounce_trap:
        headline = "【買い増し見送り／取消推奨】昼間の先物が急反発しています"
        action_badge = "注文NG・取消推奨"
        tone = "danger"
        detail_advice = (
            f"発射台は {launchpad_nav:,}円（{launchpad_chg_pct:+.2f}%）ですが、"
            f"朝以降の時間外リードだけで {lead_pct:+.2f}% も反発しています。"
            f"高値掴みを避けるため本日の買い増しリバランスは見送り（朝注文済なら締切前に取消）が合理的です。"
        )
    elif lead_pct <= -4.5 and score <= 35:
        headline = "【利確売り見送り】昼間の時間外先物が急落中のため売却待機"
        action_badge = "売却見送り"
        tone = "wait"
        detail_advice = (
            f"昼間の時間外リードが {lead_pct:+.2f}% と急落しているため、"
            f"安値投げを避けて本日の売却リバランスは見送るのが安全です。"
        )
    elif score >= 58:
        if w_state == "golden_window":
            headline = f"【今なら買い増し好機】動的目標比率 {dyn_target_pct:.1f}%（スコア {score}点・ランク{rank}）"
            action_badge = f"買付好機（目標 {dyn_target_pct:.1f}%）"
            tone = "buy_strong" if score >= 65 else "buy_mild"
            detail_advice = (
                f"締切（{sched_1530['cutoffTimeStr']}）前チェック完了。押し目スコア {score}点（動的目標比率 {dyn_target_pct:.1f}% / 推奨投入目安 {alloc_pct}%）です。"
                f"注文記録の残高と約定待ち注文を反映した推奨金額を確認して金額指定で発注してください（約定日: {exec_lbl}）。"
            )
        else:
            headline = f"【買い増しリバランス圏（目標比率 {dyn_target_pct:.1f}%）】発注は {app_lbl} 15:05 の最終通知後がベスト"
            action_badge = f"{app_lbl} 15:05まで待機"
            tone = "watch_buy"
            detail_advice = (
                f"下落により押し目スコアが {score}点（ランク{rank}）へ上昇し、動的目標比率は {dyn_target_pct:.1f}%（平常 {base_target_pct:.1f}%）です。"
                f"今すぐ入れても {app_lbl} 15:15 に入れても同じ {exec_lbl} 約定になるため、{app_lbl} 15:05 の最終通知を見てから発注してください。"
            )
    elif score <= 30:
        if w_state == "golden_window":
            headline = f"【今なら利確売り候補】動的目標比率 {dyn_target_pct:.1f}%（スコア {score}点・過熱圏）"
            action_badge = f"利確候補（目標 {dyn_target_pct:.1f}%）"
            tone = "buy_mild"
            detail_advice = (
                f"上昇により過熱圏（スコア {score}点）に入り、動的目標比率は {dyn_target_pct:.1f}% に低下しています。"
                f"投信比率が目標バンド（±{band_pt:.1f}%）を超えている場合は、必ず金額指定ではなく「口数指定」で利確売却してください。"
            )
        else:
            headline = f"【利確リバランス圏（目標比率 {dyn_target_pct:.1f}%）】発注は {app_lbl} 15:05 の最終確認後"
            action_badge = f"利確候補（{app_lbl} 15:05確認）"
            tone = "buy_mild"
            detail_advice = (
                f"上昇により動的目標比率が {dyn_target_pct:.1f}% となっています。"
                f"{app_lbl} 15:05 時点で相場が崩れておらず投信比率が目標を超過していれば、「口数指定」での利確売却が合理的です。"
            )
    else:
        headline = f"【中立・様子見圏（動的目標比率 {dyn_target_pct:.1f}%）】許容バンド ±{band_pt:.1f}% 内ならホールド"
        action_badge = "中立・ホールド目安"
        tone = "wait"
        detail_advice = (
            f"現在の購入指示指数は {score}点（ランク{rank}）、動的目標投信比率は {dyn_target_pct:.1f}%（許容バンド ±{band_pt:.1f}%）です。"
            f"保有比率がバンド内であれば無理に売買せず保有継続が合理的です。"
        )

    reasons: List[str] = [
        f"発射台（{format_jp_mdw(d1_str)}見込）: {nav_d0:,}円 → {launchpad_nav:,}円（{launchpad_chg_pct:+.2f}% / 3日累積 {cum_3d_pct:+.2f}%）",
        f"昼間の時間外リード: NQ=F {lead_info['nqfLeadPct']:+.2f}% (×3={lead_info['nqfTripleImpactPct']:+.2f}%) ＋ 為替 {lead_info['fxLeadPct']:+.2f}% ＝ 合計 {lead_pct:+.2f}%",
        f"NASDAQ100 テクニカル: 60日高値比 {dd_ndx_pct:+.2f}%（3倍換算 約{dd_triple_equiv_pct:+.1f}%） / RSI(14) {rsi14_ndx:.1f} / 25日線乖離 {sma25_dev_pct:+.2f}%",
        f"ボラティリティ & 日程: VIX {vix_cur:.2f}（前日比 {vix_chg:+.2f}） / {app_lbl}申込 → {exec_lbl}約定（米国市場 {nights_cnt}晩分）",
    ]
    for cap_r in alloc_cap_reasons:
        reasons.append(f"ロット抑制ルール適用: {cap_r}")
    for hol_s in sched_1530["jpHolidaysBetween"]:
        reasons.append(f"国内祝日挟み注意: {hol_s} を挟むため通常より約定までの米国市場日数が長くなります")

    # シナリオ別約定価格シミュレーション（今夜〜約定までの先物変動別）
    scenarios: List[Dict[str, Any]] = []
    for nq_move_pct, lbl in [
        (-4.0, "さらに急落 (NQ -4% → 3倍 -12%)"),
        (-2.0, "続落 (NQ -2% → 3倍 -6%)"),
        (0.0, "ここから横ばい (NQ ±0%)"),
        (2.0, "半分程度反発 (NQ +2% → 3倍 +6%)"),
        (4.0, "急反発 (NQ +4% → 3倍 +12%)"),
    ]:
        est_nav = int(round(provisional_exec_nav * (1.0 + 3.0 * (nq_move_pct / 100.0))))
        vs_d0 = round((est_nav - nav_d0) / nav_d0 * 100.0, 2) if nav_d0 > 0 else 0.0
        scenarios.append({
            "label": lbl,
            "nqMovePct": nq_move_pct,
            "tripleMovePct": round(nq_move_pct * 3.0, 1),
            "estimatedNav": est_nav,
            "vsFridayOrD0Pct": vs_d0,
        })

    # チャート用：^NDX 直近60営業日と25日移動平均、および実際のファンド基準価額履歴
    chart_ndx_60: List[Dict[str, Any]] = []
    start_idx = max(0, len(ndx_bars) - 60)
    for idx_i in range(start_idx, len(ndx_bars)):
        d_s, val_f = ndx_bars[idx_i]
        win_vals = [v for _, v in ndx_bars[max(0, idx_i - 24) : idx_i + 1]]
        ma_v = round(sum(win_vals) / len(win_vals), 2) if win_vals else val_f
        chart_ndx_60.append({"date": d_s, "close": val_f, "sma25": ma_v})

    axes_breakdown = [
        {
            "id": "launchpad_dip",
            "label": "① 発射台の押し目度（前夜・3日下落）",
            "score": s_axis1,
            "maxScore": 30,
            "minScore": 0,
            "summary": f"1日 {launchpad_chg_pct:+.2f}% / 3日 {cum_3d_pct:+.2f}%",
        },
        {
            "id": "drawdown_depth",
            "label": "② NASDAQ100 高値からの調整深さ",
            "score": s_axis2,
            "maxScore": 25,
            "minScore": 0,
            "summary": f"60日高値({ndx_high_60d:,.0f})比 {dd_ndx_pct:+.2f}%",
        },
        {
            "id": "technical_oversold",
            "label": "③ テクニカル売られすぎ度（RSI・25日線）",
            "score": s_axis3,
            "maxScore": 20,
            "minScore": 0,
            "summary": f"RSI(14)={rsi14_ndx:.1f} / 25日線乖離 {sma25_dev_pct:+.2f}%",
        },
        {
            "id": "intraday_lead",
            "label": "④ 昼間の時間外リード（高値掴み回避）",
            "score": s_axis4,
            "maxScore": 15,
            "minScore": -20,
            "summary": f"時間外3倍+為替リード {lead_pct:+.2f}%",
        },
        {
            "id": "vix_and_schedule",
            "label": "⑤ VIX恐怖指数 & 日程リスク補正",
            "score": s_axis5,
            "maxScore": 10,
            "minScore": -15,
            "summary": f"VIX {vix_cur:.1f} / 約定まで米国 {nights_cnt}晩",
        },
    ]

    notify_state = load_notify_state()

    result: Dict[str, Any] = {
        "updatedAt": app_data.get("updatedAt", now_jst.isoformat(timespec="seconds")),
        "generatedAt": now_jst.isoformat(timespec="seconds"),
        "updateIntervalSec": app_data.get("updateIntervalSec", 300),
        "nextUpdateAt": app_data.get("nextUpdateAt"),
        "ordersApiUrl": DEFAULT_ORDERS_URL,
        "fund": {
            "id": TRIPLE_FUND_ID,
            "name": fund["name"],
            "isinCd": fund.get("isinCd", "JP90C000T9H5"),
            "associFundCd": fund.get("associFundCd", "AY31226A"),
            "navDate": d0_str,
            "navDateLabel": format_jp_mdw(d0_str),
            "nav": nav_d0,
            "launchpadDate": d1_str,
            "launchpadDateLabel": format_jp_mdw(d1_str),
            "launchpadNav": launchpad_nav,
            "launchpadDiff": launchpad_diff,
            "launchpadChangePct": launchpad_chg_pct,
            "provisionalExecNav": provisional_exec_nav,
            "provisionalVsD0Pct": provisional_vs_d0_pct,
            "accuracy": fund.get("accuracy"),
            "history": hist_list,
            "breakdown": {
                "nqfBase": lp_nqf_base,
                "nqfSettled": lp_nqf_cur,
                "nqfSettledChangePct": lp_nqf_chg,
                "fxBase": lp_fx_base,
                "fxLaunchpad": lp_fx_cur,
                "fxLaunchpadChangePct": lp_fx_chg,
            },
        },
        "intradayLead": lead_info,
        "schedule": sched_1530,
        "schedule1500": sched_1500,
        "signal": {
            "score": score,
            "rank": rank,
            "rankLabel": rank_label,
            "tone": tone,
            "actionBadge": action_badge,
            "orderReadyNow": order_ready_now,
            "orderOkLevel": order_ok_level,
            "intradayBounceTrap": intraday_bounce_trap,
            "recommendedPct": alloc_pct,
            "baseRecommendedPct": base_alloc_pct,
            "capReasons": alloc_cap_reasons,
            "headline": headline,
            "detailAdvice": detail_advice,
            "reasons": reasons,
            "axes": axes_breakdown,
        },
        "technicals": {
            "impliedNdx": implied_ndx,
            "ndxHigh60d": ndx_high_60d,
            "drawdownNdxPct": round(dd_ndx_pct, 2),
            "drawdownTripleEquivPct": dd_triple_equiv_pct,
            "rsi14": round(rsi14_ndx, 1),
            "sma25": round(sma25_ndx, 2),
            "sma25DevPct": round(sma25_dev_pct, 2),
            "cum3dChangePct": round(cum_3d_pct, 2),
            "vix": round(vix_cur, 2),
            "vixChange": vix_chg,
        },
        "rebalance": rebalance_data,
        "scenarios": scenarios,
        "charts": {
            "ndx60d": chart_ndx_60,
        },
        "notificationStatus": {
            "webhookConfigured": bool(os.environ.get("DISCORD_WEBHOOK_URL", "").strip()),
            "lastMorningDate": notify_state.get("lastMorningDate"),
            "lastFinalDate": notify_state.get("lastFinalDate"),
            "lastOpportunityAlertDate": notify_state.get("lastOpportunityAlertDate"),
            "lastSentAt": notify_state.get("lastSentAt"),
            "lastSentKind": notify_state.get("lastSentKind"),
        },
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"[OK] Wrote {output_path} (score={score}/100 [{rank}], "
        f"launchpad={launchpad_nav} ({launchpad_chg_pct:+.2f}%), lead={lead_pct:+.2f}%, alloc={alloc_pct}%)"
    )
    return result


# ============================================================================
# GAS 注文記録連携（自動 settle・Discord 通知向けパーソナライズ計算）
# ============================================================================

def compute_personal_rebalance_from_gas(
    triple_data: Dict[str, Any],
    settle_first: bool = True,
    gas_data: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """GAS から注文記録を読み（必要なら settle 実行）、約定待ち注文を反映した個人リバランス計画をメモリ上で計算する（4-2〜4-4）。"""
    f_info = triple_data.get("fund", {})
    nav_d0 = int(f_info.get("nav") or 0)
    d0_str = str(f_info.get("navDate") or "")
    lp_nav = int(f_info.get("launchpadNav") or nav_d0)
    prov_nav = int(f_info.get("provisionalExecNav") or lp_nav)
    hist_list = f_info.get("history") if isinstance(f_info.get("history"), list) else []

    if settle_first:
        gas_data = settle_pending_orders_if_needed(
            fund_history=hist_list,
            latest_nav_date=d0_str,
            latest_nav=nav_d0,
            gas_data=gas_data,
        )
    elif gas_data is None:
        gas_data = fetch_orders_from_gas()

    if not gas_data or not isinstance(gas_data.get("orders"), list):
        return None

    orders = gas_data.get("orders", [])
    settings = gas_data.get("settings") if isinstance(gas_data.get("settings"), dict) else {}
    bal = compute_balance_from_orders(orders=orders, settings=settings, provisional_nav=prov_nav)

    # 約定待ちを反映した後の残高で提案を計算する（4-3: 同じ買い増しを二重に提案しない）
    personal_cfg = {
        "holdingUnits": bal["effectiveHoldingUnits"],
        "cashYen": bal["effectiveCashYen"],
        "baseTargetPct": float(settings.get("baseTargetPct", triple_data.get("rebalance", {}).get("config", {}).get("baseTargetPct", 50.0))),
        "mode": str(settings.get("mode", triple_data.get("rebalance", {}).get("config", {}).get("mode", "dynamic"))),
        "rebalanceBandPt": float(settings.get("rebalanceBandPt", triple_data.get("rebalance", {}).get("config", {}).get("rebalanceBandPt", 4.0))),
        "minTradeYen": int(settings.get("minTradeYen", triple_data.get("rebalance", {}).get("config", {}).get("minTradeYen", 3000))),
    }
    reb_plan = compute_portfolio_rebalance(
        portfolio_cfg=personal_cfg,
        nav_d0=nav_d0,
        launchpad_nav=lp_nav,
        prov_nav=prov_nav,
        score=int(triple_data["signal"]["score"]),
        lead_pct=float(triple_data["intradayLead"]["totalLeadPct"]),
        nights_cnt=int(triple_data["schedule"]["nightsAfterCutoff"]),
        vix_cur=float(triple_data["technicals"]["vix"]),
        fomc_hit=bool(triple_data["schedule"].get("fomcDatesHit")),
    )
    reb_plan["balanceSummary"] = bal
    return reb_plan


# ============================================================================
# Discord Webhook 通知管理（重複送信防止ステート付き）
# ============================================================================

def load_notify_state() -> Dict[str, Any]:
    """通知送信済みステートを読み込む（Webhook URL 等の秘密情報は一切含まない）。"""
    if TRIPLE_NOTIFY_STATE_PATH.exists():
        try:
            obj = json.loads(TRIPLE_NOTIFY_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return {"version": 1}


def save_notify_state(state: Dict[str, Any]) -> None:
    """通知送信済みステートを保存する。"""
    TRIPLE_NOTIFY_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    TRIPLE_NOTIFY_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _embed_color_for_signal(sig: Dict[str, Any], kind: str) -> int:
    """スコア・判定に応じた Discord Embed カラーコードを返す。"""
    if sig.get("intradayBounceTrap"):
        return 0xD6423A  # 赤（高値掴み警戒・取消推奨）
    score = int(sig.get("score", 0))
    if kind == "morning_landing":
        return 0x2B6CB0 if score < 65 else 0x1A7F5A
    if score >= 65:
        return 0x1A7F5A  # 緑（今なら注文OK）
    if score >= 50:
        return 0xD9822B  # アンバー（少額打診OK）
    if score >= 35:
        return 0x64748B  # スレート（見送り）
    return 0xD6423A  # 赤（注文NG）


def build_discord_payload(
    triple_data: Dict[str, Any],
    kind: str,
    personal_rebalance: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """通知種別ごとの Discord Webhook JSON ペイロード（GAS記録から計算したリバランス売買指示付き）を組み立てる。"""
    f_info = triple_data["fund"]
    sig = triple_data["signal"]
    lead = triple_data["intradayLead"]
    sched = triple_data["schedule"]
    tech = triple_data["technicals"]

    reb = personal_rebalance if isinstance(personal_rebalance, dict) else {}
    r_val = reb.get("valuation", {})
    r_plan = reb.get("activePlan", {})
    bal_sum = reb.get("balanceSummary", {})
    r_act = r_plan.get("action", "HOLD")
    has_personal = bool(r_val and r_plan)

    score = sig["score"]
    rank = sig["rank"]
    lp_nav = f_info["launchpadNav"]
    lp_chg = f_info["launchpadChangePct"]
    d0_nav = f_info["nav"]
    prov_nav = f_info["provisionalExecNav"]
    dyn_target_pct = float(
        r_plan.get("targetWeightPct")
        or triple_data.get("rebalance", {}).get("dynamicTargetWeightPct")
        or 50.0
    )

    pending_note = ""
    if bal_sum:
        p_buy = int(bal_sum.get("pendingBuyYen") or 0)
        p_sell_u = int(bal_sum.get("pendingSellUnits") or 0)
        if p_buy > 0 or p_sell_u > 0:
            parts_p = []
            if p_buy > 0:
                parts_p.append(f"約定待ち買付 {p_buy:,}円（見込 +{int(bal_sum.get('pendingBuyEstUnits') or 0):,}口）")
            if p_sell_u > 0:
                parts_p.append(f"約定待ち売却 {p_sell_u:,}口")
            pending_note = f"\n※反映済みの約定待ち注文: {' / '.join(parts_p)}"

    kind_titles = {
        "morning_landing": f"📊 [10:15 着地＆リバランス速報] 指数 {score}/100点（ランク{rank}）",
        "cutoff_final_judge": f"🔔 [15:05 最終リバランスジャッジ] {sig['actionBadge']}（{score}/100点）",
        "intraday_opportunity": f"🚨 [リバランス好機急浮上] 昼間の変動で売買サイン点灯（{score}/100点）",
        "manual_test": f"✅ [テスト通知] トリプルNASDAQ リバランスジャッジ（{score}/100点）",
    }
    title = kind_titles.get(kind, f"トリプルNASDAQ リバランス通知（{score}/100点）")

    if has_personal:
        if kind == "cutoff_final_judge":
            if r_act == "BUY":
                content_line = (
                    f"🟢 **【今なら買い増しOK】** 締切（15:30）までに **金額指定で `{r_plan['tradeYen']:,}円` 買付** 推奨！"
                    f"（買付後：投信 `{r_plan['afterWeightPct']:.1f}%` / 現金残り `{r_plan['afterCashYen']:,}円`）"
                )
            elif r_act == "SELL":
                content_line = (
                    f"🟠 **【今なら利確売りOK】** 締切（15:30）までに **口数指定で `{r_plan['tradeUnits']:,}口`（約`{r_plan['tradeYen']:,}円`分）売却** 推奨！"
                    f"（現金残高を `{r_plan['afterCashYen']:,}円` に補充）"
                )
            elif r_act == "HOLD_BOUNCE_TRAP" or sig["intradayBounceTrap"]:
                content_line = f"⛔ **【買い増し見送り・取消推奨】** 昼間の時間外先物が **{lead['totalLeadPct']:+.2f}%** 急反発中！高値掴み注意"
            else:
                content_line = (
                    f"⚪ **【本日は売買不要（ホールド）】** 現在 投信 `{r_val.get('currentFundYen', 0):,}円 ({r_val.get('currentWeightPct', 50):.1f}%)` : "
                    f"現金 `{r_val.get('currentCashYen', 0):,}円` — 目標バンド内のため現状維持"
                )
        elif kind == "intraday_opportunity":
            if r_act == "SELL":
                content_line = (
                    f"🟠 **【締切前の利確好機】** 昼間の先物上昇（{lead['totalLeadPct']:+.2f}%）で利確ライン到達！"
                    f"口数指定 **`{r_plan['tradeUnits']:,}口`（約`{r_plan['tradeYen']:,}円`）売却** 候補"
                )
            else:
                content_line = (
                    f"🚨 **【締切前の買い増し好機】** 昼間の先物下落（{lead['totalLeadPct']:+.2f}%）で指数 **{score}点** に上昇！"
                    f"金額指定 **`{r_plan.get('tradeYen', 0):,}円` 買付** 候補"
                )
        elif kind == "morning_landing":
            content_line = (
                f"📊 **【10:15 着地速報】** {f_info['launchpadDateLabel']}見込 **{lp_nav:,}円（{lp_chg:+.2f}%）** ｜ "
                f"保有投信 **{r_val.get('launchpadFundYen', 0):,}円** ＋ 現金 **{r_val.get('currentCashYen', 0):,}円**（確定発注は15:05通知まで待機）"
            )
        else:
            content_line = (
                f"✅ **【Discord連携テスト成功】** 現在の判定: **{sig['actionBadge']}**（購入指示指数 **{score}/100点**）"
            )

        if r_act == "BUY":
            reb_action_str = f"🟢 **金額指定で `{r_plan['tradeYen']:,}円` 買い増し**\n（想定 `+{r_plan['tradeUnits']:,}口` → 残り現金 `{r_plan['afterCashYen']:,}円`）"
        elif r_act == "SELL":
            reb_action_str = f"🟠 **口数指定で `{r_plan['tradeUnits']:,}口` 利確売却**\n（想定回収 `+{r_plan['tradeYen']:,}円` → 現金計 `{r_plan['afterCashYen']:,}円`）"
        else:
            reb_action_str = f"⚪ **売買なし（ホールド）**\n（目標 `{dyn_target_pct:.1f}%` との差 `{r_plan.get('driftPt', 0):+.1f}pt`）"

        portfolio_field_val = (
            f"現在(約定待ち反映後): 投信 `{r_val.get('currentFundYen', 0):,}円` (**{r_val.get('currentWeightPct', 50):.1f}%**) / "
            f"現金 `{r_val.get('currentCashYen', 0):,}円` (計 `{r_val.get('currentTotalYen', 0):,}円`)\n"
            f"確定保有: `{int(bal_sum.get('holdingUnits', 0)):,}口` (平均取得単価 `{int(bal_sum.get('avgCostNav', 0)):,}円`)\n"
            f"動的目標比率: **投信 {dyn_target_pct:.1f}%**\n"
            f"{reb_action_str}{pending_note}"
        )
    else:
        content_line = f"🔔 **【{sig['actionBadge']}】** 購入指示指数 **{score}/100点（ランク{rank}）** ｜ 動的目標比率 **{dyn_target_pct:.1f}%**"
        portfolio_field_val = (
            f"動的目標比率: **投信 {dyn_target_pct:.1f}%**（推奨投入目安 `{sig['recommendedPct']}%`）\n"
            f"※GAS注文記録連携（`ORDERS_URL` / `ORDERS_KEY`）から残高を取得できなかったため市場目標比率のみ表示"
        )

    axes_lines = [f"・{a['label']}: **{a['score']:+.1f}** / {a['maxScore']}点" for a in sig["axes"]]

    fields = [
        {
            "name": "ポートフォリオ & リバランス指示",
            "value": portfolio_field_val,
            "inline": False,
        },
        {
            "name": "基準価額（発射台 → 暫定目安）",
            "value": (
                f"前回({f_info['navDateLabel']}): `{d0_nav:,}円`\n"
                f"発射台({f_info['launchpadDateLabel']}): **`{lp_nav:,}円` ({lp_chg:+.2f}%)**\n"
                f"昼間リード反映: `{prov_nav:,}円` ({lead['totalLeadPct']:+.2f}%)"
            ),
            "inline": True,
        },
        {
            "name": "約定日 & テクニカル",
            "value": (
                f"申込: **{sched['applicationDateLabel']} 15:30締切** → 約定: **{sched['executionDateLabel']}**（米{sched['nightsAfterCutoff']}晩）\n"
                f"高値比 `{tech['drawdownNdxPct']:+.2f}%` / RSI `{tech['rsi14']:.1f}` / VIX `{tech['vix']:.2f}`"
            ),
            "inline": True,
        },
        {
            "name": f"購入指示指数 {score}/100点（5軸内訳）",
            "value": "\n".join(axes_lines),
            "inline": False,
        },
    ]

    embed = {
        "title": title,
        "url": PUBLIC_TRIPLE_URL,
        "description": f"**{sig['headline']}**\n{sig['detailAdvice']}",
        "color": _embed_color_for_signal(sig, kind),
        "fields": fields,
        "footer": {
            "text": f"auAMトリプル・NASDAQ100 リバランス運用アドバイザー | 更新 {triple_data['generatedAt'][:16].replace('T', ' ')} JST"
        },
    }

    return {
        "username": "トリプルNASDAQ リバランスジャッジ",
        "content": f"{content_line}\n🔗 ジャッジ: {PUBLIC_TRIPLE_URL} ｜ 📝 注文記録: {PUBLIC_TRIPLE_ORDERS_URL}",
        "embeds": [embed],
    }


def send_discord_webhook(webhook_url: str, payload: Dict[str, Any]) -> None:
    """Discord Webhook へ JSON ペイロードを POST 送信する（URL はログに出力しない）。"""
    resp = requests.post(
        webhook_url,
        headers={
            "User-Agent": build_data.USER_AGENT,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout=15,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Discord Webhook HTTP {resp.status_code}: {resp.text[:200]}")


def evaluate_and_notify_discord(
    triple_data: Dict[str, Any],
    now_jst: Optional[datetime.datetime] = None,
    force_test: bool = False,
    dry_run: bool = False,
    gas_data: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """未約定注文の settle 確認を行い、時刻とスコア条件を判定して Discord Webhook 通知を送信する。"""
    if now_jst is None:
        now_jst = datetime.datetime.now(JST)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    state = load_notify_state()
    today_str = now_jst.strftime("%Y-%m-%d")
    hm = now_jst.hour * 60 + now_jst.minute
    score = int(triple_data["signal"]["score"])
    lead_pct = float(triple_data["intradayLead"]["totalLeadPct"])

    # 1. 基準価額が出ている pending 注文があれば GAS へ settle を書き込む（dry_run でないとき）
    f_info = triple_data.get("fund", {})
    if gas_data is None:
        if not dry_run:
            gas_data = settle_pending_orders_if_needed(
                fund_history=f_info.get("history") or [],
                latest_nav_date=str(f_info.get("navDate") or ""),
                latest_nav=int(f_info.get("nav") or 0),
            )
        else:
            gas_data = fetch_orders_from_gas()

    personal_reb = compute_personal_rebalance_from_gas(
        triple_data=triple_data,
        settle_first=False,
        gas_data=gas_data,
    )

    kind_to_send: Optional[str] = None

    if force_test:
        kind_to_send = "manual_test"
    elif is_fund_order_day(now_jst.date()):
        # 1. 営業日 10:15 着地速報（10:12〜10:50 JST に1日1回）
        if (10 * 60 + 12) <= hm <= (10 * 60 + 50) and state.get("lastMorningDate") != today_str:
            kind_to_send = "morning_landing"
        # 2. 営業日 15:05 締切直前の最終注文ジャッジ（15:02〜15:23 JST に1日1回）
        elif (15 * 60 + 2) <= hm <= (15 * 60 + 23) and state.get("lastFinalDate") != today_str:
            kind_to_send = "cutoff_final_judge"
        # 3. 営業日 13:00〜14:55 のリバランス好機急浮上アラート（買い増し急浮上 または 利確売り急浮上、1日1回）
        elif (13 * 60) <= hm <= (14 * 60 + 55) and state.get("lastOpportunityAlertDate") != today_str:
            reb_act = (personal_reb or {}).get("activePlan", {}).get("action", "HOLD")
            morning_score = state.get("morningScore") if state.get("lastMorningDate") == today_str else None
            jumped_from_morning = (morning_score is not None) and (score - int(morning_score) >= 10) and (score >= 65)
            afternoon_bargain = (reb_act == "BUY") and (score >= 68) and (lead_pct <= -1.5)
            afternoon_profit_take = (reb_act == "SELL") and (score <= 30) and (lead_pct >= 2.0)
            if jumped_from_morning or afternoon_bargain or afternoon_profit_take:
                kind_to_send = "intraday_opportunity"

    if not kind_to_send:
        return None

    payload = build_discord_payload(triple_data, kind_to_send, personal_rebalance=personal_reb)

    if dry_run:
        print(f"[DRY-RUN] Would send Discord notification kind='{kind_to_send}' (score={score}).")
        return kind_to_send

    if not webhook_url:
        print(
            f"[INFO] Discord trigger '{kind_to_send}' matched, but DISCORD_WEBHOOK_URL is not set. Skipping.",
            flush=True,
        )
        return None

    send_discord_webhook(webhook_url, payload)
    print(f"[DISCORD] Sent '{kind_to_send}' notification at {now_jst.isoformat(timespec='seconds')} (score={score}).", flush=True)

    # 重複送信防止ステートの更新
    if kind_to_send == "morning_landing":
        state["lastMorningDate"] = today_str
        state["morningScore"] = score
    elif kind_to_send == "cutoff_final_judge":
        state["lastFinalDate"] = today_str
        state["finalScore"] = score
    elif kind_to_send == "intraday_opportunity":
        state["lastOpportunityAlertDate"] = today_str
        state["opportunityScore"] = score

    state["lastSentAt"] = now_jst.isoformat(timespec="seconds")
    state["lastSentKind"] = kind_to_send
    save_notify_state(state)

    # triple_data.json 内の notificationStatus も即時同期
    triple_data["notificationStatus"] = {
        "webhookConfigured": True,
        "lastMorningDate": state.get("lastMorningDate"),
        "lastFinalDate": state.get("lastFinalDate"),
        "lastOpportunityAlertDate": state.get("lastOpportunityAlertDate"),
        "lastSentAt": state.get("lastSentAt"),
        "lastSentKind": state.get("lastSentKind"),
    }
    TRIPLE_DATA_JSON_PATH.write_text(
        json.dumps(triple_data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return kind_to_send


def main() -> None:
    parser = argparse.ArgumentParser(description="Build docs/triple_data.json and send Discord alerts for Triple NASDAQ")
    parser.add_argument("--output", type=str, default=str(TRIPLE_DATA_JSON_PATH), help="出力先 triple_data.json パス")
    parser.add_argument("--send-test", action="store_true", help="DISCORD_WEBHOOK_URL へテスト通知を即時送信する")
    parser.add_argument("--dry-run", action="store_true", help="Webhook送信を行わずペイロード生成のみ確認する")
    args = parser.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    now_jst = datetime.datetime.now(JST)
    t_data = build_triple_advisor_data(app_data=None, now_jst=now_jst, output_path=pathlib.Path(args.output))
    evaluate_and_notify_discord(t_data, now_jst=now_jst, force_test=args.send_test, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
