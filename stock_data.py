import re
import time
import threading
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone

# ── ticker.info cache (10-min TTL) ───────────────────────────────────────────
# Fundamentals (PE, targets, analyst counts) change slowly.
# Caching eliminates the biggest bottleneck: parallel Yahoo Finance requests.
class _InfoCache:
    TTL = 30 * 60   # 30 minutes — fundamentals change slowly

    def __init__(self):
        self._s = {}
        self._l = threading.Lock()

    def get(self, sym):
        with self._l:
            e = self._s.get(sym)
            return e["data"] if e and time.time() - e["ts"] < self.TTL else None

    def set(self, sym, data):
        with self._l:
            self._s[sym] = {"data": data, "ts": time.time()}

_info_cache = _InfoCache()

# ── Rating-change daily cache ─────────────────────────────────────────────────
# get_rating_change_today costs ~0.63s for TW (rarely returns data) and
# ~0.09s for US. Cache by symbol+date so it's only fetched once per day.
_rating_cache      = {}
_rating_cache_lock = threading.Lock()

def _get_rating_cached(ticker, sym: str, is_taiwan: bool):
    """Skip entirely for TW; cache result for US by date."""
    if is_taiwan:
        return None
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    with _rating_cache_lock:
        entry = _rating_cache.get(sym)
        if entry and entry["date"] == today:
            return entry["result"]
    result = get_rating_change_today(ticker)
    with _rating_cache_lock:
        _rating_cache[sym] = {"date": today, "result": result}
    return result

# ── History cache ─────────────────────────────────────────────────────────────
# Daily OHLCV bars change slowly; cache for 2 hours so repeated analyze_stock
# calls (e.g. profile sync after cache bust) skip the expensive download.

class _HistCache:
    TTL = 2 * 3600   # 2 hours

    def __init__(self):
        self._store = {}
        self._lock  = threading.Lock()

    def get(self, key: str):
        with self._lock:
            e = self._store.get(key)
            if e and time.time() - e["ts"] < self.TTL:
                return e["data"]
        return None

    def set(self, key: str, data):
        with self._lock:
            self._store[key] = {"data": data, "ts": time.time()}

    def stats(self):
        with self._lock:
            now  = time.time()
            live = sum(1 for v in self._store.values() if now - v["ts"] < self.TTL)
            return {"live": live, "total": len(self._store)}

_hist_cache = _HistCache()

# Taiwan stock/ETF: 4-6 digits, optional 1-2 trailing letters (e.g. 00663L, 00632R)
# optionally already suffixed with .TW / .TWO
_TW_BARE = re.compile(r'^\d{4,6}[A-Za-z]{0,2}$')
_TW_FULL = re.compile(r'^\d{4,6}[A-Za-z]{0,2}\.(TW|TWO)$', re.IGNORECASE)


def _resolve_symbol(raw: str):
    """
    Returns (yf_symbol, is_taiwan).
    Bare 4-6 digit numbers are treated as Taiwan stocks; .TW is tried first,
    then .TWO as fallback (handled in analyze_stock).
    """
    if _TW_FULL.match(raw):
        return raw.upper(), True
    if _TW_BARE.match(raw):
        return raw + '.TW', True          # caller will retry with .TWO if needed
    return raw.upper(), False


def _fmt_price(price, currency: str) -> str:
    if currency == 'TWD':
        return f"NT${price:,.0f}"
    return f"${price:,.2f}"


def calculate_bollinger(prices, period=20, std_dev=2):
    """Returns (bb_pct, bb_bw, label, bw_label, is_bullish)."""
    sma = prices.rolling(window=period).mean()
    std = prices.rolling(window=period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std

    p = float(prices.iloc[-1])
    u = float(upper.iloc[-1])
    l = float(lower.iloc[-1])
    m = float(sma.iloc[-1])

    bb_pct = round((p - l) / (u - l), 3) if (u - l) > 0 else 0.5
    bb_bw  = round((u - l) / m, 3)       if m > 0       else 0.0

    if bb_pct > 1.0:    label, bull = "突破上軌 ↑", True
    elif bb_pct > 0.8:  label, bull = "近上軌",     True
    elif bb_pct >= 0.4: label, bull = "中性",       None
    elif bb_pct >= 0.2: label, bull = "近下軌",     False
    elif bb_pct >= 0:   label, bull = "接近超賣",   False
    else:               label, bull = "突破下軌 ↓", False

    if bb_bw < 0.08:    bw_label = "⚡ 收縮"
    elif bb_bw < 0.15:  bw_label = "低波動"
    elif bb_bw < 0.25:  bw_label = "正常"
    else:               bw_label = "高波動"

    return bb_pct, bb_bw, label, bw_label, bull


def calculate_ema_signal(prices):
    """Returns (signal_label, is_bullish, ema20, ema50)."""
    ema20 = prices.ewm(span=20, adjust=False).mean()
    ema50 = prices.ewm(span=50, adjust=False).mean()

    p   = float(prices.iloc[-1])
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    # look back 3 bars to detect fresh crossover
    pe20 = float(ema20.iloc[-4]) if len(ema20) >= 4 else e20
    pe50 = float(ema50.iloc[-4]) if len(ema50) >= 4 else e50

    golden = e20 > e50 and pe20 <= pe50
    death  = e20 < e50 and pe20 >= pe50

    if golden:               return "⭐ 黃金交叉", True,  e20, e50
    if death:                return "💀 死亡交叉", False, e20, e50
    if p > e20 and e20 > e50:return "強勢多頭 ↑",  True,  e20, e50
    if p > e50 and e20 > e50:return "多頭回檔",    True,  e20, e50
    if p < e20 and e20 < e50:return "空頭排列 ↓",  False, e20, e50
    if e20 > e50:            return "多頭整理",    None,  e20, e50
    return                          "趨勢整理",    None,  e20, e50


def calculate_volume_ratio(volumes):
    """Latest volume vs 20-day average. Returns float or None."""
    if len(volumes) < 22:
        return None
    avg = float(volumes.iloc[-22:-2].mean())
    cur = float(volumes.iloc[-1])
    return round(cur / avg, 2) if avg > 0 else None


def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 1)


def calculate_macd(prices):
    """Returns (label, is_bullish). Detects fresh crossover vs sustained trend."""
    ema12 = prices.ewm(span=12, adjust=False).mean()
    ema26 = prices.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()

    cur_macd, cur_sig = macd_line.iloc[-1], signal_line.iloc[-1]
    prv_macd, prv_sig = macd_line.iloc[-2], signal_line.iloc[-2]

    crossed_up   = cur_macd > cur_sig and prv_macd <= prv_sig
    crossed_down = cur_macd < cur_sig and prv_macd >= prv_sig

    if crossed_up:    return "金叉 ↑", True
    elif crossed_down:return "死叉 ↓", False
    elif cur_macd > cur_sig: return "多頭 ↑", True
    else:             return "空頭 ↓", False


def map_recommendation(rec_key):
    mapping = {
        "strong_buy": "強力買入", "strongBuy": "強力買入",
        "buy": "買入",
        "hold": "持有", "neutral": "持有",
        "underperform": "低配",
        "sell": "賣出",
        "strong_sell": "強力賣出", "strongSell": "強力賣出",
    }
    return mapping.get(rec_key, "")


def score_recommendation(rsi, upside_pct):
    score = 0
    if upside_pct > 25:   score += 2
    elif upside_pct > 10: score += 1
    elif upside_pct < -10:score -= 2
    elif upside_pct < 0:  score -= 1
    if rsi < 35:  score += 1
    elif rsi > 70:score -= 1

    if score >= 2:    return "強力買入"
    elif score == 1:  return "買入"
    elif score == 0:  return "持有"
    elif score == -1: return "低配"
    else:             return "賣出"


def get_next_earnings(ticker):
    try:
        cal = ticker.calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            dates = cal.get("Earnings Date", [])
        elif isinstance(cal, pd.DataFrame):
            dates = cal.get("Earnings Date", pd.Series()).tolist()
        else:
            return None
        for d in (dates if hasattr(dates, "__iter__") else [dates]):
            ts = pd.Timestamp(d)
            if ts.tzinfo is not None:
                ts = ts.tz_convert("UTC").tz_localize(None)
            if ts > pd.Timestamp.now():
                return ts.to_pydatetime()
    except Exception:
        pass
    return None


def get_analyst_stats(ticker, info):
    """Returns (n_analysts, buy_pct). buy_pct is 0-100 float or None."""
    n_analysts = info.get("numberOfAnalystOpinions")
    buy_pct = None
    try:
        summary = ticker.recommendations_summary
        if summary is not None and not summary.empty:
            latest = summary.iloc[0]
            strong_buy  = int(latest.get("strongBuy", 0))
            buy         = int(latest.get("buy", 0))
            hold        = int(latest.get("hold", 0))
            sell        = int(latest.get("sell", 0))
            strong_sell = int(latest.get("strongSell", 0))
            total = strong_buy + buy + hold + sell + strong_sell
            if total > 0:
                buy_pct = round((strong_buy + buy) / total * 100, 0)
    except Exception:
        pass
    return n_analysts, buy_pct


def generate_comment(symbol, current_price, target_price, rsi, forward_pe,
                     upside_pct, next_earnings, macd_label, rev_growth, is_taiwan=False):
    parts = []

    if rsi > 75:
        parts.append("RSI 超買，短線留意回調壓力")
    elif rsi > 65:
        parts.append("多頭動能強勁")
    elif 45 <= rsi <= 65:
        parts.append("形成牛旗形態")
    elif rsi < 30:
        parts.append("RSI 超賣，技術面存在反彈機會")
    else:
        parts.append("技術面整理中")

    if macd_label and "金叉" in macd_label:
        parts.append("MACD 剛形成金叉，動能轉強")

    if next_earnings:
        days = (next_earnings - datetime.now()).days
        if 0 < days <= 90:
            parts.append(f"市場靜待 {next_earnings.strftime('%-m/%-d')} 財報")

    if rev_growth is not None:
        if rev_growth > 0.3:
            parts.append(f"營收年增 {rev_growth*100:.0f}%，高速成長")
        elif rev_growth < 0:
            parts.append(f"營收年減 {abs(rev_growth)*100:.0f}%，留意衰退")

    if target_price and current_price:
        if upside_pct > 20:
            parts.append("目標價持續上修")
        elif upside_pct < -10:
            parts.append(f"現價已超分析師目標 {abs(upside_pct):.0f}%，評估空間有限")

    if forward_pe and forward_pe > 0:
        # Taiwan market trades at lower PE multiples; adjust thresholds
        high_pe = 30 if is_taiwan else 50
        low_pe  = 10 if is_taiwan else 12
        if forward_pe > high_pe:
            parts.append("估值偏高，需財報驗證")
        elif forward_pe < low_pe:
            parts.append("估值合理偏低，具安全邊際")

    return "，".join(parts) + "。" if parts else "基本面穩健，持續關注。"


_ACTION_LABEL = {
    "up":   ("⬆ 升評", "up"),
    "down": ("⬇ 降評", "down"),
    "init": ("★ 首評", "init"),
}

def get_rating_change_today(ticker):
    """
    Return today's most significant analyst rating change, or None.
    Prioritises upgrades > downgrades > initiations; ignores maintained/reiterated.
    """
    from zoneinfo import ZoneInfo
    try:
        ud = ticker.upgrades_downgrades
        if ud is None or ud.empty:
            return None

        today_et = datetime.now(ZoneInfo("America/New_York")).date()

        # GradeDate index may be tz-aware or tz-naive
        idx = ud.index
        if hasattr(idx, "tzinfo") and idx.tzinfo is not None:
            dates = idx.tz_convert("America/New_York").date
        else:
            dates = idx.date if hasattr(idx, "date") else [d.date() for d in idx]

        today_rows = ud[[d == today_et for d in dates]]
        if today_rows.empty:
            return None

        # Priority: up > down > init; skip main/reit
        priority = {"up": 0, "down": 1, "init": 2}
        best = None
        for _, row in today_rows.iterrows():
            act = str(row.get("Action", "")).lower()
            if act not in priority:
                continue
            if best is None or priority[act] < priority[best["action"]]:
                best = {
                    "action":    act,
                    "firm":      str(row.get("Firm", "")).strip(),
                    "to_grade":  str(row.get("ToGrade", "")).strip(),
                    "from_grade":str(row.get("FromGrade", "")).strip(),
                }
        if not best:
            return None

        label, kind = _ACTION_LABEL[best["action"]]
        detail = best["to_grade"]
        if best["from_grade"] and best["from_grade"] != best["to_grade"]:
            detail = f"{best['from_grade']} → {best['to_grade']}"
        return {
            "kind":    kind,                            # "up" | "down" | "init"
            "label":   label,                           # "⬆ 升評" etc.
            "firm":    best["firm"],
            "detail":  detail,
            "summary": f"{label} {best['firm']} ({detail})",
        }
    except Exception:
        return None


def _get_tw_chinese_name(code: str):
    """Look up Chinese short name from twstock's bundled code table."""
    try:
        import twstock
        info = twstock.codes.get(code)
        if info and info.name:
            return info.name
    except Exception:
        pass
    return None


def _fetch_tw(raw: str):
    """Try TWSE (.TW) then TPEx (.TWO) for a bare Taiwan stock number."""
    for suffix in ['.TW', '.TWO']:
        sym = raw + suffix
        ticker = yf.Ticker(sym)
        info = _info_cache.get(sym)
        if info is None:
            info = ticker.info or {}
            if info.get("quoteType"):
                _info_cache.set(sym, info)
        price = (info.get("currentPrice") or info.get("regularMarketPrice")
                 or info.get("previousClose"))
        if info.get("quoteType") and price is not None:
            return ticker, info, sym
    return None, {}, raw + '.TW'


def analyze_stock(symbol: str) -> dict:
    raw = symbol.strip().upper()
    try:
        is_taiwan = False

        if _TW_BARE.match(raw):
            # Bare Taiwan number — try TWSE then TPEx
            is_taiwan = True
            ticker, info, resolved = _fetch_tw(raw)
            if ticker is None:
                return {"symbol": raw,
                        "error": f"找不到台股代碼「{raw}」，請確認後重試。"}
            symbol = resolved

        elif _TW_FULL.match(raw):
            is_taiwan = True
            symbol = raw
            ticker = yf.Ticker(symbol)
            info = _info_cache.get(symbol)
            if info is None:
                info = ticker.info or {}
                if info.get("quoteType"):
                    _info_cache.set(symbol, info)

        else:
            symbol = raw
            ticker = yf.Ticker(symbol)
            info = _info_cache.get(symbol)
            if info is None:
                info = ticker.info or {}
                if info.get("quoteType"):
                    _info_cache.set(symbol, info)

        if not info or info.get("quoteType") is None:
            return {"symbol": symbol,
                    "error": f"找不到股票代碼「{symbol}」，請確認後重試。"}

        # ── Price data ────────────────────────────────────────────
        # Priority:  1) Alpaca IEX (US only, real-time)
        #            2) yfinance fast_info (US, ~1-5 min delay)
        #            3) yfinance info (Taiwan + fallback)
        currency = info.get("currency", "USD")
        price_source = "Yahoo"          # default; overwritten below
        if not is_taiwan:
            snap = {}
            try:
                from alpaca_data import get_us_snapshot_single
                snap = get_us_snapshot_single(symbol)
            except Exception:
                pass

            if snap.get("price"):
                current_price = snap["price"]
                day_change    = snap["change"]
                day_chg_pct   = snap["change_pct"]
                week52_high   = info.get("fiftyTwoWeekHigh")
                price_source  = "Alpaca"
            else:
                try:
                    fi            = ticker.fast_info
                    current_price = float(fi.last_price)
                    prev_close    = float(fi.previous_close) if fi.previous_close else None
                    day_change    = round(current_price - prev_close, 2) if prev_close else None
                    day_chg_pct   = round(day_change / prev_close * 100, 2) if prev_close and day_change else None
                    week52_high   = float(fi.year_high) if fi.year_high else info.get("fiftyTwoWeekHigh")
                    currency      = fi.currency or currency
                    price_source  = "Yahoo"
                except Exception:
                    current_price = info.get("currentPrice") or info.get("regularMarketPrice") or info.get("previousClose")
                    day_change    = info.get("regularMarketChange")
                    day_chg_pct   = info.get("regularMarketChangePercent")
                    week52_high   = info.get("fiftyTwoWeekHigh")
        else:
            # Taiwan: try Shioaji (real-time) first, fall back to yfinance
            tw_code = re.sub(r'\.(TW[O]?)$', '', symbol, flags=re.IGNORECASE)
            tw_snap = {}
            try:
                from shioaji_data import get_tw_snapshot_single
                tw_snap = get_tw_snapshot_single(tw_code)
            except Exception:
                pass

            if tw_snap.get("price"):
                current_price = tw_snap["price"]
                day_change    = tw_snap.get("change")
                day_chg_pct   = tw_snap.get("change_pct")
                week52_high   = info.get("fiftyTwoWeekHigh")
                price_source  = "Shioaji"
            else:
                current_price = (info.get("currentPrice")
                                 or info.get("regularMarketPrice")
                                 or info.get("previousClose"))
                day_change    = info.get("regularMarketChange")
                day_chg_pct   = info.get("regularMarketChangePercent")
                week52_high   = info.get("fiftyTwoWeekHigh")

        if current_price is None:
            return {"symbol": symbol,
                    "error": f"無法取得「{symbol}」的報價資料。"}

        pct_from_52w_high = (
            round((current_price - week52_high) / week52_high * 100, 1)
            if week52_high else None
        )

        # ── Fundamentals (info only, not in fast_info) ────────────
        target_price = info.get("targetMeanPrice")
        forward_pe   = info.get("forwardPE")
        if forward_pe and (forward_pe < 0 or forward_pe > 1000):
            forward_pe = None

        peg_ratio = info.get("pegRatio")
        if peg_ratio and (peg_ratio < 0 or peg_ratio > 200):
            peg_ratio = None

        rev_growth = info.get("revenueGrowth")

        hist = _hist_cache.get(symbol)
        if hist is None:
            if hist is None:
                if is_taiwan:
                    try:
                        from shioaji_data import get_tw_history
                        _tw_code = re.sub(r'\.(TW[O]?)$', '', symbol, flags=re.IGNORECASE)
                        hist = get_tw_history(_tw_code, days=180)
                    except Exception:
                        hist = pd.DataFrame()
                if hist is None or (hasattr(hist, 'empty') and hist.empty):
                    hist = ticker.history(period="6mo")
            if not hist.empty:
                _hist_cache.set(symbol, hist)
        closes  = hist["Close"]
        volumes = hist["Volume"]

        rsi = calculate_rsi(closes) if len(closes) >= 15 else None
        macd_label, macd_bullish = (None, None)
        if len(closes) >= 35:
            macd_label, macd_bullish = calculate_macd(closes)

        # Bollinger Bands
        bb_pct, bb_bw, bb_label, bb_bw_label, bb_bullish = (
            calculate_bollinger(closes) if len(closes) >= 20
            else (None, None, "N/A", "N/A", None)
        )

        # EMA 20/50
        ema_sig, ema_bullish, ema20_val, ema50_val = (
            calculate_ema_signal(closes) if len(closes) >= 50
            else ("N/A", None, None, None)
        )

        # Volume vs 20D avg
        vol_ratio = calculate_volume_ratio(volumes)

        # Fundamentals
        gross_margin    = info.get("grossMargins")
        roe             = info.get("returnOnEquity")
        de_raw          = info.get("debtToEquity")        # yf returns as %, e.g. 172 = 1.72
        debt_equity     = round(de_raw / 100, 2) if de_raw is not None else None
        fcf             = info.get("freeCashflow")
        total_revenue   = info.get("totalRevenue")
        fcf_margin      = (
            round(fcf / total_revenue * 100, 1)
            if fcf and total_revenue and total_revenue > 0 else None
        )

        rec_key = info.get("recommendationKey", "")
        upside_pct = (
            ((target_price - current_price) / current_price * 100)
            if target_price and current_price else 0
        )
        recommendation = map_recommendation(rec_key) or score_recommendation(
            rsi or 50, upside_pct
        )

        next_earnings   = get_next_earnings(ticker)
        n_analysts, buy_pct = get_analyst_stats(ticker, info)
        rating_change   = _get_rating_cached(ticker, symbol, is_taiwan)
        comment = generate_comment(
            symbol, current_price, target_price, rsi or 50,
            forward_pe, upside_pct, next_earnings, macd_label, rev_growth,
            is_taiwan=is_taiwan,
        )

        # Display symbol: strip .TW / .TWO suffix for cleaner badge
        display_symbol = re.sub(r'\.(TW|TWO)$', '', symbol, flags=re.IGNORECASE)

        # Chinese name for Taiwan stocks; fall back to yfinance shortName
        if is_taiwan:
            company_name = _get_tw_chinese_name(display_symbol) or info.get("shortName", symbol)
        else:
            company_name = info.get("shortName", symbol)

        return {
            "error": None,
            "symbol": symbol,                  # full yfinance symbol
            "display_symbol": display_symbol,  # badge label (no suffix)
            "is_taiwan": is_taiwan,
            "currency": currency,
            "company_name": company_name,
            "price_source":      price_source,
            "current_price":     round(float(current_price), 4),
            "current_price_fmt": _fmt_price(current_price, currency),
            "day_change":     round(day_change, 2)  if day_change  is not None else None,
            "day_chg_pct":    round(day_chg_pct, 2) if day_chg_pct is not None else None,
            "day_change_fmt": (
                f"{'+'if day_change>=0 else '-'}"
                f"{'NT$' if currency=='TWD' else '$'}"
                f"{abs(day_change):,.{'0' if currency=='TWD' else '2'}f}"
            ) if day_change is not None else None,
            "day_chg_pct_fmt": (
                f"{'+' if day_chg_pct >= 0 else ''}{day_chg_pct:.2f}%"
                if day_chg_pct is not None else None
            ),
            "target_price": target_price,
            "target_price_fmt": _fmt_price(target_price, currency) if target_price else "N/A",
            "forward_pe_fmt": f"{forward_pe:.0f}x" if forward_pe else "N/A",
            "rsi": rsi,
            "rsi_fmt": str(int(rsi)) if rsi is not None else "N/A",
            "recommendation": recommendation,
            "comment": comment,
            "upside_pct": round(upside_pct, 1),
            "peg_ratio": peg_ratio,
            "peg_fmt": f"{peg_ratio:.1f}" if peg_ratio else "N/A",
            "macd_label": macd_label or "N/A",
            "macd_bullish": macd_bullish,
            "pct_from_52w_high": pct_from_52w_high,
            "pct_from_52w_high_fmt": (
                f"{pct_from_52w_high:+.1f}%" if pct_from_52w_high is not None else "N/A"
            ),
            "next_earnings_fmt": (
                next_earnings.strftime("%-m/%-d") if next_earnings else "N/A"
            ),
            "rev_growth": rev_growth,
            "rev_growth_fmt": (
                f"{rev_growth*100:+.1f}%" if rev_growth is not None else "N/A"
            ),
            "n_analysts": n_analysts,
            "buy_pct": buy_pct,
            "analyst_fmt": (
                f"{int(n_analysts)}人 / {int(buy_pct)}%買"
                if n_analysts and buy_pct is not None else
                (f"{int(n_analysts)}人" if n_analysts else "N/A")
            ),
            "rating_change": rating_change,
            # ── new indicators ──────────────────────────────────
            # Bollinger Bands
            "bb_pct":      bb_pct,
            "bb_pct_fmt":  f"{bb_pct:.2f}" if bb_pct is not None else "N/A",
            "bb_bw":       bb_bw,
            "bb_bw_fmt":   f"{bb_bw:.2f}"  if bb_bw  is not None else "N/A",
            "bb_label":    bb_label,
            "bb_bw_label": bb_bw_label,
            "bb_bullish":  bb_bullish,
            # EMA 20/50
            "ema_signal":  ema_sig,
            "ema_bullish": ema_bullish,
            "ema20_fmt":   _fmt_price(ema20_val, currency) if ema20_val else "N/A",
            "ema50_fmt":   _fmt_price(ema50_val, currency) if ema50_val else "N/A",
            # Volume ratio
            "vol_ratio":     vol_ratio,
            "vol_ratio_fmt": f"{vol_ratio:.1f}x" if vol_ratio is not None else "N/A",
            # Fundamentals
            "fcf_margin":       fcf_margin,
            "fcf_margin_fmt":   f"{fcf_margin:+.1f}%" if fcf_margin is not None else "N/A",
            "gross_margin":     gross_margin,
            "gross_margin_fmt": f"{gross_margin*100:.1f}%" if gross_margin else "N/A",
            "roe":              roe,
            "roe_fmt":          f"{roe*100:.1f}%" if roe else "N/A",
            "debt_equity":      debt_equity,
            "debt_equity_fmt":  f"{debt_equity:.2f}" if debt_equity is not None else "N/A",
        }

    except Exception as e:
        return {"symbol": raw, "error": f"分析「{raw}」時發生錯誤：{str(e)}"}
