"""
market_data.py — Daily market overview for US and Taiwan markets.
Sections: Top 10 Gainers, Top 10 Volume, Top Sectors, Expert Picks
"""
import requests
import yfinance as yf
import pandas as pd
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

_NY_TZ = ZoneInfo("America/New_York")
_TW_TZ = ZoneInfo("Asia/Taipei")


# ── Market status & timestamps ────────────────────────────────────────────────

def get_market_status() -> dict:
    """Return open/closed status and local time for US (NYSE) and TW (TWSE)."""
    now_ny = datetime.now(_NY_TZ)
    now_tw = datetime.now(_TW_TZ)
    now_utc_tw = datetime.now(_TW_TZ)   # data-fetch time in TW timezone

    # NYSE: Mon-Fri 09:30-16:00 ET (simplified, no holiday check)
    us_open = (
        now_ny.weekday() < 5
        and dtime(9, 30) <= now_ny.time() <= dtime(16, 0)
    )
    # TWSE: Mon-Fri 09:00-13:30 CST
    tw_open = (
        now_tw.weekday() < 5
        and dtime(9, 0) <= now_tw.time() <= dtime(13, 30)
    )

    return {
        "fetch_time":  now_utc_tw.strftime("%Y-%m-%d %H:%M"),   # TW time for display
        "fetch_time_short": now_utc_tw.strftime("%H:%M"),
        "US": {
            "local_time": now_ny.strftime("%m/%d %H:%M"),
            "timezone":   "ET",
            "is_open":    us_open,
            "status":     "🟢 開市中" if us_open else "🔴 休市",
            "hours":      "09:30–16:00 ET (Mon–Fri)",
        },
        "TW": {
            "local_time": now_tw.strftime("%m/%d %H:%M"),
            "timezone":   "CST",
            "is_open":    tw_open,
            "status":     "🟢 開市中" if tw_open else "🔴 休市",
            "hours":      "09:00–13:30 CST (Mon–Fri)",
        },
    }

# ── Helpers ──────────────────────────────────────────────────────────────────

_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}


def _fmt_vol(v):
    if v is None: return "N/A"
    if v >= 1_000_000_000: return f"{v/1_000_000_000:.1f}B"
    if v >= 1_000_000:     return f"{v/1_000_000:.1f}M"
    if v >= 1_000:         return f"{v/1_000:.0f}K"
    return str(v)


def _fmt_cap(v):
    if v is None: return ""
    if v >= 1_000_000_000_000: return f"${v/1_000_000_000_000:.1f}T"
    if v >= 1_000_000_000:     return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:         return f"${v/1_000_000:.0f}M"
    return ""


# ── US Screener (Yahoo Finance API) ──────────────────────────────────────────

def _us_screener(scr_id: str, count: int = 10) -> list:
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&lang=en-US&region=US&scrIds={scr_id}&count={count}"
    )
    try:
        r = requests.get(url, headers=_YAHOO_HEADERS, timeout=15)
        quotes = r.json()["finance"]["result"][0]["quotes"]
        return [
            {
                "symbol":     q.get("symbol", ""),
                "name":       q.get("shortName") or q.get("longName", ""),
                "price":      round(q.get("regularMarketPrice", 0), 2),
                "price_fmt":  f"${q.get('regularMarketPrice', 0):,.2f}",
                "change_pct": round(q.get("regularMarketChangePercent", 0), 2),
                "volume":     q.get("regularMarketVolume", 0),
                "volume_fmt": _fmt_vol(q.get("regularMarketVolume")),
                "market_cap": _fmt_cap(q.get("marketCap")),
            }
            for q in quotes[:count]
        ]
    except Exception:
        return []


def get_us_gainers(count=10):
    return _us_screener("day_gainers", count)


def get_us_volume(count=10):
    return _us_screener("most_actives", count)


# Motley Fool's publicly documented long-term favourite stocks
# (repeatedly featured in their free articles, Stock Advisor & Rule Breakers)
_MF_POOL = [
    "AMZN","AAPL","GOOGL","META","MSFT","NVDA","TSLA","NFLX",
    "SHOP","MELI","SQ","MA","V","PYPL","CRWD","NET","ZS","PANW",
    "CRM","TEAM","DDOG","SNOW","MDB","PLTR","NOW","HUBS","OKTA",
    "UBER","SPOT","DIS","ROKU","TTD","ASML","TSM","ADBE","INTU",
]

def get_us_expert_picks(count=10):
    """
    Motley Fool-inspired picks: filter their publicly known stock pool
    by current analyst buy consensus + highest analyst target upside.
    Note: MF's actual paid picks are paywalled; this uses their
    publicly documented 'forever stocks' filtered by live analyst data.
    """
    results = []
    for sym in _MF_POOL:
        try:
            info     = yf.Ticker(sym).info or {}
            rec      = info.get("recommendationKey", "")
            n_ana    = info.get("numberOfAnalystOpinions") or 0
            price    = info.get("currentPrice") or info.get("regularMarketPrice")
            target   = info.get("targetMeanPrice")
            chg_pct  = info.get("regularMarketChangePercent")
            if not price or n_ana < 5:
                continue
            upside = round((target - price) / price * 100, 1) if target else None
            results.append({
                "symbol":     sym,
                "name":       info.get("shortName", sym),
                "price_fmt":  f"${price:,.2f}",
                "change_pct": round(chg_pct, 2) if chg_pct else None,
                "upside_pct": upside,
                "n_analysts": n_ana,
                "volume_fmt": _fmt_vol(info.get("regularMarketVolume")),
                "market_cap": _fmt_cap(info.get("marketCap")),
                "rec":        rec,
            })
        except Exception:
            continue

    # Sort: strong buy first, then by upside potential
    _rank = {"strong_buy":0,"strongBuy":0,"buy":1}
    results.sort(key=lambda r: (
        _rank.get(r["rec"], 2),
        -(r["upside_pct"] or 0)
    ))
    return results[:count]


# ── US Sectors ────────────────────────────────────────────────────────────────

US_SECTORS = [
    ("科技",     "XLK"), ("醫療",     "XLV"), ("金融",   "XLF"),
    ("能源",     "XLE"), ("工業",     "XLI"), ("通訊",   "XLC"),
    ("消費選擇", "XLY"), ("消費必需", "XLP"), ("原材料", "XLB"),
    ("房地產",   "XLRE"),("公用事業", "XLU"),
]

def get_us_sectors() -> list:
    syms = [s for _, s in US_SECTORS]
    try:
        raw = yf.download(syms, period="5d", auto_adjust=True, progress=False)
        closes = raw["Close"]
        result = []
        for name, sym in US_SECTORS:
            try:
                s   = closes[sym].dropna()
                chg = round((float(s.iloc[-1]) - float(s.iloc[-2])) / float(s.iloc[-2]) * 100, 2)
                result.append({"name": name, "symbol": sym,
                                "change_pct": chg, "price_fmt": f"${float(s.iloc[-1]):,.2f}"})
            except Exception:
                result.append({"name": name, "symbol": sym, "change_pct": None, "price_fmt": "N/A"})
        result.sort(key=lambda x: x["change_pct"] or -999, reverse=True)
        return result
    except Exception:
        return [{"name": n, "symbol": s, "change_pct": None, "price_fmt": "N/A"} for n, s in US_SECTORS]


# ── Taiwan stocks ─────────────────────────────────────────────────────────────

TW_MAJOR = [
    "2330","2454","2317","2382","2308","2303","2379","3034",
    "3711","2344","2357","4938","2882","2881","2891","2886",
    "2412","4904","2002","1301","1303","6505","2912","2207",
    "2353","2884","5871","2885","2892","3008","2395","6669",
    "2330","2454","00878","0050","0056",
]
TW_MAJOR = list(dict.fromkeys(TW_MAJOR))  # deduplicate

TW_SECTORS = [
    ("半導體",   ["2330","2454","2303","3034","2344"]),
    ("電子代工", ["2317","2382","2308"]),
    ("金融",     ["2882","2881","2891","2886","2884"]),
    ("電信",     ["2412","4904"]),
    ("傳產鋼鐵", ["2002","1301","1303"]),
    ("ETF",      ["0050","0056","00878"]),
]

TW_EXPERT_POOL = [
    "2330","2454","2317","2382","2303","2308","2881","2882",
    "2412","3711","3008","6669","2395","4938","6505",
]


def _tw_name(code: str) -> str:
    try:
        import twstock
        info = twstock.codes.get(code)
        if info and info.name:
            return info.name
    except Exception:
        pass
    return code


def _get_tw_batch(extra_codes=None) -> dict:
    """One yfinance batch call for all Taiwan major stocks. Returns {code: {...}}."""
    codes = list(TW_MAJOR)
    if extra_codes:
        codes += [c for c in extra_codes if c not in codes]
    syms = [c + ".TW" for c in codes]

    try:
        raw  = yf.download(syms, period="5d", auto_adjust=True, progress=False)
        closes  = raw["Close"]
        volumes = raw["Volume"]
        result  = {}

        for sym in syms:
            code = sym.replace(".TW", "")
            try:
                cs  = closes[sym].dropna()
                vs  = volumes[sym].dropna()
                if len(cs) < 2: continue
                price   = float(cs.iloc[-1])
                prev    = float(cs.iloc[-2])
                vol     = int(vs.iloc[-1]) if len(vs) else 0
                chg     = round((price - prev) / prev * 100, 2)
                result[code] = {
                    "symbol":     code,
                    "name":       _tw_name(code),
                    "price":      round(price, 0),
                    "price_fmt":  f"NT${price:,.0f}",
                    "change_pct": chg,
                    "volume":     vol,
                    "volume_fmt": _fmt_vol(vol),
                }
            except Exception:
                continue
        return result
    except Exception:
        return {}


def get_tw_gainers(count=10, batch=None) -> list:
    data = batch or _get_tw_batch()
    return sorted(data.values(), key=lambda x: x["change_pct"], reverse=True)[:count]


def get_tw_volume(count=10, batch=None) -> list:
    data = batch or _get_tw_batch()
    return sorted(data.values(), key=lambda x: x["volume"], reverse=True)[:count]


def get_tw_sectors(batch=None) -> list:
    all_codes = [c for _, codes in TW_SECTORS for c in codes]
    data = batch or _get_tw_batch(extra_codes=all_codes)
    result = []
    for name, codes in TW_SECTORS:
        chgs = [data[c]["change_pct"] for c in codes if c in data]
        avg  = round(sum(chgs) / len(chgs), 2) if chgs else None
        result.append({"name": name, "change_pct": avg})
    result.sort(key=lambda x: x["change_pct"] or -999, reverse=True)
    return result


def get_tw_expert_picks(count=10) -> list:
    """Taiwan stocks with strong analyst buy consensus."""
    results = []
    for sym in TW_EXPERT_POOL:
        try:
            info   = yf.Ticker(sym + ".TW").info or {}
            rec    = info.get("recommendationKey", "")
            n_ana  = info.get("numberOfAnalystOpinions") or 0
            price  = info.get("currentPrice") or info.get("regularMarketPrice")
            target = info.get("targetMeanPrice")
            if rec in ("strong_buy", "strongBuy", "buy") and n_ana >= 3 and price:
                upside = round((target - price) / price * 100, 1) if target else None
                results.append({
                    "symbol":     sym,
                    "name":       _tw_name(sym),
                    "price_fmt":  f"NT${price:,.0f}",
                    "change_pct": None,
                    "upside_pct": upside,
                    "n_analysts": n_ana,
                    "volume_fmt": "",
                })
        except Exception:
            continue
    results.sort(key=lambda x: x.get("upside_pct") or 0, reverse=True)
    return results[:count]


# ── Combined entry point ──────────────────────────────────────────────────────

def get_market_overview(market: str) -> dict:
    """Return all 4 sections + market status for a market. market='US' or 'TW'."""
    try:
        status = get_market_status()
        if market == "US":
            return {
                "gainers": get_us_gainers(10),
                "volume":  get_us_volume(10),
                "sectors": get_us_sectors(),
                "expert":  get_us_expert_picks(10),
                "market_status": status["US"],
                "fetch_time":    status["fetch_time"],
                "expert_source": "Motley Fool 精選池 (公開推薦股)",
            }
        else:
            batch = _get_tw_batch()
            return {
                "gainers": get_tw_gainers(10, batch),
                "volume":  get_tw_volume(10, batch),
                "sectors": get_tw_sectors(batch),
                "expert":  get_tw_expert_picks(10),
                "market_status": status["TW"],
                "fetch_time":    status["fetch_time"],
                "expert_source": "法人強力買入評級",
            }
    except Exception as e:
        return {"error": str(e)}
