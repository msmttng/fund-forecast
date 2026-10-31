import argparse
import datetime
import json
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
TRIPLE_DATA_JSON_PATH = build_data.DOCS_DIR / "triple_data.json"
TRIPLE_NOTIFY_STATE_PATH = build_data.DOCS_DIR / "triple_notify_state.json"
PUBLIC_TRIPLE_URL = "https://msmttng.github.io/fund-forecast/triple.html"

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

    # 6. 時間帯×スコアに応じた最終アクション指示文の生成
    w_state = sched_1530["windowState"]
    app_lbl = sched_1530["applicationDateLabel"]
    exec_lbl = sched_1530["executionDateLabel"]
    order_ok_level = (score >= 50) and (alloc_pct > 0) and (not intraday_bounce_trap)
    order_ready_now = (w_state == "golden_window") and order_ok_level

    if intraday_bounce_trap:
        headline = "【注文見送り／取消推奨】昼間の先物が急反発しています"
        action_badge = "注文NG・取消推奨"
        tone = "danger"
        detail_advice = (
            f"発射台は {launchpad_nav:,}円（{launchpad_chg_pct:+.2f}%）ですが、"
            f"朝以降の時間外リードだけで {lead_pct:+.2f}% も反発しています。"
            f"今から買うと高値掴みになりやすいため見送り（朝注文済なら締切前に取消）が合理的です。"
        )
    elif w_state == "golden_window":
        if score >= 65 and alloc_pct > 0:
            headline = f"【今なら注文OK】買付待機資金の {alloc_pct}% を金額指定で発注推奨"
            action_badge = "今なら注文OK"
            tone = "buy_strong"
            detail_advice = (
                f"締切（{sched_1530['cutoffTimeStr']}）前の最終チェックをクリアしました。"
                f"発射台 {launchpad_nav:,}円（{launchpad_chg_pct:+.2f}%）・昼間リード {lead_pct:+.2f}% で割安な水準です。"
                f"一括は避け、待機資金の {alloc_pct}% を「金額指定」で注文するのが理にかなっています（約定日: {exec_lbl}）。"
            )
        elif score >= 50 and alloc_pct > 0:
            headline = f"【少額打診なら注文OK】待機資金の {alloc_pct}% 程度で慎重にエントリー"
            action_badge = "少額打診OK"
            tone = "buy_mild"
            detail_advice = (
                f"締切直前チェック完了。発射台 {launchpad_nav:,}円（{launchpad_chg_pct:+.2f}%）で押し目圏に入っています。"
                f"まだ続落余地も残るため、待機資金の {alloc_pct}% だけ金額指定で打診買いし、残りは次の下落に温存してください（約定日: {exec_lbl}）。"
            )
        else:
            headline = "【本日は注文見送り】優位性のある押し目ではありません"
            action_badge = "本日は見送り"
            tone = "wait"
            detail_advice = (
                f"購入指示指数は {score}点 です。3倍レバレッジは横ばい・高値圏で持つと減価しやすいため、"
                f"指数が 65点 以上に上がる急落日までキャッシュを温存してください。"
            )
    elif order_ok_level:
        headline = f"【買い圏到達（{score}点）】確定注文は {app_lbl} 15:05 の最終通知後がベスト"
        action_badge = f"{app_lbl} 15:05まで待機"
        tone = "watch_buy"
        detail_advice = (
            f"発射台が {launchpad_nav:,}円（前営業日比 {launchpad_chg_pct:+.2f}%）まで下がり、"
            f"待機資金の {alloc_pct}% 投入目安の買い圏に入っています。"
            f"ただし今注文しても {app_lbl} 15:15 に注文しても同じ {exec_lbl} 約定になるため、"
            f"{app_lbl} 15:05 の最終ジャッジ（昼間の先物急反発がないかの確認）を見てから発注するのが最も安全です。"
        )
    else:
        headline = f"【様子見・キャッシュ温存（{score}点）】次の急落押し目を待機"
        action_badge = "押し目待ち"
        tone = "wait"
        detail_advice = (
            f"現在の購入指示指数は {score}点 です。無理にエントリーせず、"
            f"購入指示指数が 50〜65点 以上に上昇する押し目局面まで待機資金を温存してください。"
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


def build_discord_payload(triple_data: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """通知種別ごとの Discord Webhook JSON ペイロードを組み立てる。"""
    f_info = triple_data["fund"]
    sig = triple_data["signal"]
    lead = triple_data["intradayLead"]
    sched = triple_data["schedule"]
    tech = triple_data["technicals"]

    score = sig["score"]
    rank = sig["rank"]
    alloc_pct = sig["recommendedPct"]
    lp_nav = f_info["launchpadNav"]
    lp_chg = f_info["launchpadChangePct"]
    d0_nav = f_info["nav"]
    prov_nav = f_info["provisionalExecNav"]

    kind_titles = {
        "morning_landing": f"📊 [10:15 着地速報] 購入指示指数 {score}/100点（ランク{rank}）",
        "cutoff_final_judge": f"🔔 [15:05 最終注文ジャッジ] {sig['actionBadge']}（{score}/100点）",
        "intraday_opportunity": f"🚨 [買い好機急浮上] 昼間の下落で指数が {score}/100点 に上昇！",
        "manual_test": f"✅ [テスト通知] トリプルNASDAQ 購入指示指数 {score}/100点（ランク{rank}）",
    }
    title = kind_titles.get(kind, f"トリプルNASDAQ 通知（{score}/100点）")

    # メンション不要でも一目で結論がわかる先頭サマリーテキスト
    if kind == "cutoff_final_judge":
        if score >= 65 and alloc_pct > 0 and not sig["intradayBounceTrap"]:
            content_line = f"🟢 **【今なら注文OK】** 購入指示指数 **{score}点** — 締切（15:30）までに待機資金の **{alloc_pct}%** を金額指定で発注推奨！"
        elif score >= 50 and alloc_pct > 0 and not sig["intradayBounceTrap"]:
            content_line = f"🟡 **【少額打診ならOK】** 購入指示指数 **{score}点** — 待機資金の **{alloc_pct}%** 程度の打診買い圏（締切 15:30）"
        elif sig["intradayBounceTrap"]:
            content_line = f"⛔ **【注文NG・取消推奨】** 昼間の時間外先物が **{lead['totalLeadPct']:+.2f}%** 急反発中！高値掴み注意"
        else:
            content_line = f"⚪ **【本日は注文見送り】** 購入指示指数 **{score}点** — 押し目待ち（投入推奨 0%）"
    elif kind == "intraday_opportunity":
        content_line = (
            f"🚨 **【締切前の買い好機】** 昼間の先物・為替リード（{lead['totalLeadPct']:+.2f}%）により"
            f"購入指示指数が **{score}点**（推奨投入 **{alloc_pct}%**）に上昇しました！"
        )
    elif kind == "morning_landing":
        content_line = (
            f"📊 **【10:15 着地速報】** {f_info['launchpadDateLabel']}夕方見込 **{lp_nav:,}円（{lp_chg:+.2f}%）** / "
            f"現在スコア **{score}点**（確定注文は15:05の最終通知を待ってください）"
        )
    else:
        content_line = (
            f"✅ **【Discord連携テスト成功】** 現在の購入指示指数は **{score}/100点（{sig['actionBadge']}）** です。"
        )

    axes_lines = [f"・{a['label']}: **{a['score']:+.1f}** / {a['maxScore']}点" for a in sig["axes"]]
    example_300k = int(round(300000 * alloc_pct / 100.0))

    fields = [
        {
            "name": "判定・推奨ロット",
            "value": (
                f"**{sig['rankLabel']}**\n"
                f"推奨投入: **待機資金の {alloc_pct}%**（金額指定）\n"
                f"※待機30万円なら **{example_300k:,}円**"
            ),
            "inline": True,
        },
        {
            "name": "基準価額（発射台 → 暫定目安）",
            "value": (
                f"前回({f_info['navDateLabel']}): `{d0_nav:,}円`\n"
                f"発射台({f_info['launchpadDateLabel']}): **`{lp_nav:,}円` ({lp_chg:+.2f}%)**\n"
                f"昼間リード反映目安: `{prov_nav:,}円` ({lead['totalLeadPct']:+.2f}%)"
            ),
            "inline": True,
        },
        {
            "name": "約定スケジュール・テクニカル",
            "value": (
                f"申込枠: **{sched['applicationDateLabel']} 15:30締切** → 約定日: **{sched['executionDateLabel']}**（米国市場 **{sched['nightsAfterCutoff']}晩**）\n"
                f"60日高値比: `{tech['drawdownNdxPct']:+.2f}%`（3倍換算 `{tech['drawdownTripleEquivPct']:+.1f}%`） / "
                f"RSI(14): `{tech['rsi14']:.1f}` / 25日線乖離: `{tech['sma25DevPct']:+.2f}%` / VIX: `{tech['vix']:.2f}`"
            ),
            "inline": False,
        },
        {
            "name": "5軸スコア内訳",
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
            "text": f"auAMトリプル・NASDAQ100 専用アドバイザー | 更新 {triple_data['generatedAt'][:16].replace('T', ' ')} JST"
        },
    }

    return {
        "username": "トリプルNASDAQ 購入ジャッジ",
        "content": f"{content_line}\n🔗 専用サイト: {PUBLIC_TRIPLE_URL}",
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
) -> Optional[str]:
    """時刻とスコア条件を判定し、該当時に Discord Webhook 通知を送信して通知種別を返す。"""
    if now_jst is None:
        now_jst = datetime.datetime.now(JST)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    state = load_notify_state()
    today_str = now_jst.strftime("%Y-%m-%d")
    hm = now_jst.hour * 60 + now_jst.minute
    score = int(triple_data["signal"]["score"])
    lead_pct = float(triple_data["intradayLead"]["totalLeadPct"])

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
        # 3. 営業日 13:00〜14:55 の買い好機急浮上アラート（1日1回）
        elif (13 * 60) <= hm <= (14 * 60 + 55) and state.get("lastOpportunityAlertDate") != today_str:
            morning_score = state.get("morningScore") if state.get("lastMorningDate") == today_str else None
            jumped_from_morning = (morning_score is not None) and (score - int(morning_score) >= 10) and (score >= 65)
            afternoon_bargain = (score >= 68) and (lead_pct <= -1.5)
            if jumped_from_morning or afternoon_bargain:
                kind_to_send = "intraday_opportunity"

    if not kind_to_send:
        return None

    payload = build_discord_payload(triple_data, kind_to_send)

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
