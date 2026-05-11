import re
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timezone

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
        info = ticker.info or {}
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
            info = ticker.info or {}

        else:
            symbol = raw
            ticker = yf.Ticker(symbol)
            info = ticker.info or {}

        if not info or info.get("quoteType") is None:
            return {"symbol": symbol,
                    "error": f"找不到股票代碼「{symbol}」，請確認後重試。"}

        current_price = (
            info.get("currentPrice")
            or info.get("regularMarketPrice")
            or info.get("previousClose")
        )
        if current_price is None:
            return {"symbol": symbol,
                    "error": f"無法取得「{symbol}」的報價資料。"}

        currency    = info.get("currency", "USD")
        day_change  = info.get("regularMarketChange")
        day_chg_pct = info.get("regularMarketChangePercent")

        target_price = info.get("targetMeanPrice")
        forward_pe   = info.get("forwardPE")
        if forward_pe and (forward_pe < 0 or forward_pe > 1000):
            forward_pe = None

        peg_ratio = info.get("pegRatio")
        if peg_ratio and (peg_ratio < 0 or peg_ratio > 200):
            peg_ratio = None

        rev_growth  = info.get("revenueGrowth")
        week52_high = info.get("fiftyTwoWeekHigh")
        pct_from_52w_high = (
            round((current_price - week52_high) / week52_high * 100, 1)
            if week52_high else None
        )

        hist = ticker.history(period="1y")
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
