"""
market_data.py — Daily market overview for US and Taiwan markets.
Sections: Top 10 Gainers, Top 10 Volume, Top Sectors, Expert Picks
"""
import requests
import xml.etree.ElementTree as ET
import yfinance as yf
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
from email.utils import parsedate_to_datetime

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
                "symbol":         q.get("symbol", ""),
                "name":           q.get("shortName") or q.get("longName", ""),
                "price":          round(q.get("regularMarketPrice", 0), 2),
                "price_fmt":      f"${q.get('regularMarketPrice', 0):,.2f}",
                "change_pct":     round(q.get("regularMarketChangePercent", 0), 2),
                "volume":         q.get("regularMarketVolume", 0),
                "volume_fmt":     _fmt_vol(q.get("regularMarketVolume")),
                "market_cap_raw": q.get("marketCap"),          # raw int for filtering
                "market_cap":     _fmt_cap(q.get("marketCap")),
            }
            for q in quotes[:count]
        ]
    except Exception:
        return []


def _enrich_with_alpaca(rows: list, sort_key: str = "change_pct") -> list:
    """
    Take Yahoo screener rows, replace price/change with Alpaca real-time data,
    then re-sort by sort_key. Falls back to original rows on any error.
    """
    symbols = [r["symbol"] for r in rows if r.get("symbol")]
    if not symbols:
        return rows
    try:
        from alpaca_data import get_us_snapshots
        snaps = get_us_snapshots(symbols)
        for r in rows:
            snap = snaps.get(r["symbol"], {})
            if snap.get("price"):
                r["price_fmt"]  = f"${snap['price']:,.2f}"
                r["change_pct"] = snap.get("change_pct")
            if snap.get("volume"):
                r["volume"]     = snap["volume"]
                r["volume_fmt"] = _fmt_vol(snap["volume"])
        rows.sort(key=lambda x: x.get(sort_key) or -999, reverse=True)
    except Exception:
        pass
    return rows


MIN_MARKET_CAP = 5_000_000_000   # $5B — large-cap floor; filters micro/small/mid caps

def get_us_gainers(count=10):
    # Fetch 4× to ensure enough remain after market-cap filter
    rows = _us_screener("day_gainers", count * 4)
    rows = [r for r in rows if (r.get("market_cap_raw") or 0) >= MIN_MARKET_CAP]
    rows = _enrich_with_alpaca(rows[:count * 2], sort_key="change_pct")
    return rows[:count]


def get_us_volume(count=10):
    rows = _us_screener("most_actives", count)
    return _enrich_with_alpaca(rows, sort_key="volume")


# Motley Fool's publicly documented long-term favourite stocks
# (repeatedly featured in their free articles, Stock Advisor & Rule Breakers)
_MF_POOL = [
    "AMZN","AAPL","GOOGL","META","MSFT","NVDA","TSLA","NFLX",
    "SHOP","MELI","SQ","MA","V","PYPL","CRWD","NET","ZS","PANW",
    "CRM","TEAM","DDOG","SNOW","MDB","PLTR","NOW","HUBS","OKTA",
    "UBER","SPOT","DIS","ROKU","TTD","ASML","TSM","ADBE","INTU",
]

def _fetch_mf_stock(sym: str):
    """Fetch one MF pool stock; returns None if not qualifying."""
    try:
        info    = yf.Ticker(sym).info or {}
        rec     = info.get("recommendationKey", "")
        n_ana   = info.get("numberOfAnalystOpinions") or 0
        price   = info.get("currentPrice") or info.get("regularMarketPrice")
        target  = info.get("targetMeanPrice")
        chg_pct = info.get("regularMarketChangePercent")
        if not price or n_ana < 5:
            return None
        upside = round((target - price) / price * 100, 1) if target else None
        return {
            "symbol":     sym,
            "name":       info.get("shortName", sym),
            "price_fmt":  f"${price:,.2f}",
            "change_pct": round(chg_pct, 2) if chg_pct else None,
            "upside_pct": upside,
            "n_analysts": n_ana,
            "volume_fmt": _fmt_vol(info.get("regularMarketVolume")),
            "market_cap": _fmt_cap(info.get("marketCap")),
            "rec":        rec,
        }
    except Exception:
        return None


def get_us_expert_picks(count=10):
    """Parallel fetch of MF pool — 8 workers instead of sequential."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        raw = list(ex.map(_fetch_mf_stock, _MF_POOL))
    results = [r for r in raw if r is not None]
    _rank = {"strong_buy": 0, "strongBuy": 0, "buy": 1}
    results.sort(key=lambda r: (_rank.get(r["rec"], 2), -(r["upside_pct"] or 0)))
    return results[:count]


# ── US Sectors ────────────────────────────────────────────────────────────────

US_SECTORS = [
    ("科技",     "XLK"), ("醫療",     "XLV"), ("金融",   "XLF"),
    ("能源",     "XLE"), ("工業",     "XLI"), ("通訊",   "XLC"),
    ("消費選擇", "XLY"), ("消費必需", "XLP"), ("原材料", "XLB"),
    ("房地產",   "XLRE"),("公用事業", "XLU"),
]

def get_us_sectors() -> list:
    """
    Sector ETF performance — one Alpaca batch call (real-time IEX),
    falls back to parallel fast_info if Alpaca unavailable.
    """
    syms = [s for _, s in US_SECTORS]
    snaps = {}
    try:
        from alpaca_data import get_us_snapshots
        snaps = get_us_snapshots(syms)
    except Exception:
        pass

    result = []
    for name, sym in US_SECTORS:
        s = snaps.get(sym)
        if s and s.get("price") and s.get("prev_close"):
            chg = s["change_pct"]
            result.append({"name": name, "symbol": sym,
                            "change_pct": chg,
                            "price_fmt": f"${s['price']:,.2f}"})
        else:
            # per-ticker fast_info fallback
            try:
                fi  = yf.Ticker(sym).fast_info
                p   = float(fi.last_price)
                pre = float(fi.previous_close)
                chg = round((p - pre) / pre * 100, 2) if pre else None
                result.append({"name": name, "symbol": sym,
                                "change_pct": chg, "price_fmt": f"${p:,.2f}"})
            except Exception:
                result.append({"name": name, "symbol": sym,
                                "change_pct": None, "price_fmt": "N/A"})

    result.sort(key=lambda x: x["change_pct"] or -999, reverse=True)
    return result


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


def _fetch_tw_expert_stock(sym: str):
    try:
        info   = yf.Ticker(sym + ".TW").info or {}
        rec    = info.get("recommendationKey", "")
        n_ana  = info.get("numberOfAnalystOpinions") or 0
        price  = info.get("currentPrice") or info.get("regularMarketPrice")
        target = info.get("targetMeanPrice")
        if rec not in ("strong_buy", "strongBuy", "buy") or n_ana < 3 or not price:
            return None
        upside = round((target - price) / price * 100, 1) if target else None
        return {
            "symbol":     sym,
            "name":       _tw_name(sym),
            "price_fmt":  f"NT${price:,.0f}",
            "change_pct": None,
            "upside_pct": upside,
            "n_analysts": n_ana,
            "volume_fmt": "",
        }
    except Exception:
        return None


def get_tw_expert_picks(count=10) -> list:
    """Parallel fetch of TW expert pool."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        raw = list(ex.map(_fetch_tw_expert_stock, TW_EXPERT_POOL))
    results = [r for r in raw if r is not None]
    results.sort(key=lambda x: x.get("upside_pct") or 0, reverse=True)
    return results[:count]


# ── News ─────────────────────────────────────────────────────────────────────

def _parse_rss(url: str, count: int = 8) -> list:
    """Fetch and parse an RSS feed. Returns list of news dicts."""
    try:
        r = requests.get(url, headers=_YAHOO_HEADERS, timeout=12)
        root = ET.fromstring(r.text)
        items = root.findall(".//item")
        result = []
        for it in items[:count]:
            title  = (it.find("title") or it.find("{*}title"))
            link   = (it.find("link")  or it.find("{*}link"))
            pub    = (it.find("pubDate") or it.find("{*}pubDate"))
            source = (it.find("source") or it.find("{*}source"))

            title_text = title.text.strip()  if title  is not None else ""
            link_text  = link.text.strip()   if link   is not None else "#"
            pub_text   = pub.text.strip()    if pub    is not None else ""
            src_text   = (source.text or source.get("url","")).strip() if source is not None else ""

            # Clean Google News redirect links → extract real URL
            if "news.google.com" in link_text:
                import re
                m = re.search(r'url=([^&]+)', link_text)
                if m:
                    from urllib.parse import unquote
                    link_text = unquote(m.group(1))

            # Parse publish time → relative string
            age = ""
            try:
                dt = parsedate_to_datetime(pub_text)
                diff = datetime.now(dt.tzinfo) - dt
                mins = int(diff.total_seconds() / 60)
                if mins < 60:   age = f"{mins}分鐘前"
                elif mins < 1440: age = f"{mins//60}小時前"
                else:           age = f"{mins//1440}天前"
            except Exception:
                age = pub_text[:16] if pub_text else ""

            if title_text:
                result.append({
                    "title":  title_text,
                    "link":   link_text,
                    "age":    age,
                    "source": src_text,
                })
        return result
    except Exception:
        return []


def _parse_anue(count: int = 8) -> list:
    """
    Fetch Taiwan stock news from Anue (鉅亨網) JSON API.
    Always passes startAt/endAt to guarantee only recent articles are returned.
    Tries 24 h first; widens to 3 days if fewer than 3 results.
    """
    import time

    def _fetch(hours_back: int) -> list:
        now_ts   = int(time.time())
        start_ts = now_ts - hours_back * 3600
        url = (
            f"https://news.cnyes.com/api/v3/news/category/tw_stock"
            f"?limit={count}&startAt={start_ts}&endAt={now_ts}"
        )
        r     = requests.get(url, headers=_YAHOO_HEADERS, timeout=12)
        items = r.json().get("items", {}).get("data", [])
        result = []
        for it in items[:count]:
            pub_ts = it.get("publishAt", 0)
            try:
                dt   = datetime.fromtimestamp(pub_ts, tz=_TW_TZ)
                diff = datetime.now(_TW_TZ) - dt
                mins = int(diff.total_seconds() / 60)
                age  = (f"{mins}分鐘前" if mins < 60
                        else f"{mins//60}小時前" if mins < 1440
                        else f"{mins//1440}天前")
            except Exception:
                age = ""
            title = it.get("title", "").strip()
            if title:
                result.append({
                    "title":  title,
                    "link":   f"https://news.cnyes.com/news/id/{it.get('newsId','')}",
                    "age":    age,
                    "source": "鉅亨網",
                })
        return result

    try:
        news = _fetch(24)               # try last 24 hours first
        if len(news) < 3:
            news = _fetch(72)           # widen to 3 days if not enough
        return news
    except Exception:
        return []


def get_us_news(count: int = 8) -> list:
    """Top US market news — Google News RSS last 24 h (primary) + Yahoo Finance (fallback)."""
    # tbs=qdr:d  → past 24 hours
    news = _parse_rss(
        "https://news.google.com/rss/search?q=stock+market+finance+S%26P500"
        "&hl=en-US&gl=US&ceid=US:en&tbs=qdr:d", count
    )
    if len(news) < 3:                   # widen to 3 days
        news = _parse_rss(
            "https://news.google.com/rss/search?q=stock+market+finance+S%26P500"
            "&hl=en-US&gl=US&ceid=US:en&tbs=qdr:3d", count
        )
    if len(news) < 3:                   # last fallback
        news = _parse_rss("https://finance.yahoo.com/rss/topstories", count)
    return news[:count]


def get_tw_news(count: int = 8) -> list:
    """Top Taiwan market news — Anue 24 h (primary) + Google News TW (fallback)."""
    news = _parse_anue(count)           # already handles 24 h → 3 day fallback
    if len(news) < 3:
        news = _parse_rss(
            "https://news.google.com/rss/search?q=台灣股市+台股+股票"
            "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant&tbs=qdr:d", count
        )
    return news[:count]


# ── Combined entry point ──────────────────────────────────────────────────────

def get_market_overview(market: str) -> dict:
    """Return all 4 sections + market status for a market. market='US' or 'TW'."""
    try:
        status = get_market_status()
        if market == "US":
            # Run all sections + news concurrently
            with ThreadPoolExecutor(max_workers=5) as ex:
                f = {
                    "gainers": ex.submit(get_us_gainers, 10),
                    "volume":  ex.submit(get_us_volume, 10),
                    "sectors": ex.submit(get_us_sectors),
                    "expert":  ex.submit(get_us_expert_picks, 10),
                    "news":    ex.submit(get_us_news, 8),
                }
                results = {k: v.result() for k, v in f.items()}
            return {
                **results,
                "market_status": status["US"],
                "fetch_time":    status["fetch_time"],
                "expert_source": "Motley Fool 精選池 (公開推薦股)",
                "sources": {
                    "gainers": "Yahoo + Alpaca",
                    "volume":  "Yahoo + Alpaca",
                    "sectors": "Alpaca IEX",
                    "expert":  "Yahoo",
                },
            }
        else:
            # TW: batch download once, then run expert + news concurrently
            with ThreadPoolExecutor(max_workers=3) as ex:
                f_batch  = ex.submit(_get_tw_batch)
                f_expert = ex.submit(get_tw_expert_picks, 10)
                f_news   = ex.submit(get_tw_news, 8)
                batch  = f_batch.result()
                expert = f_expert.result()
                news   = f_news.result()
            return {
                "gainers": get_tw_gainers(10, batch),
                "volume":  get_tw_volume(10, batch),
                "sectors": get_tw_sectors(batch),
                "expert":  expert,
                "news":    news,
                "market_status": status["TW"],
                "fetch_time":    status["fetch_time"],
                "expert_source": "法人強力買入評級",
                "sources": {
                    "gainers": "Yahoo",
                    "volume":  "Yahoo",
                    "sectors": "Yahoo",
                    "expert":  "Yahoo",
                },
            }
    except Exception as e:
        return {"error": str(e)}
