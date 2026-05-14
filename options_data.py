"""
options_data.py — US equity options overview.

Data source: yfinance (15-min delayed, free)
IV Rank / IV Percentile: calculated from 252-day historical realized volatility
(true IV Rank requires paid data; HV-based is the standard free approximation)
"""
import requests
import time
import threading
import numpy as np
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

_NY_TZ = ZoneInfo("America/New_York")

# ── Shared history cache (re-use stock_data's 2h cache if available) ─────────
try:
    from stock_data import _hist_cache as _opt_hist_cache
except ImportError:
    class _FallbackCache:
        TTL = 2 * 3600
        def __init__(self): self._s = {}; self._l = threading.Lock()
        def get(self, k):
            with self._l:
                e = self._s.get(k)
                return e["data"] if e and time.time()-e["ts"] < self.TTL else None
        def set(self, k, d):
            with self._l: self._s[k] = {"data": d, "ts": time.time()}
    _opt_hist_cache = _FallbackCache()

# ── Options chain cache (5-min TTL) ──────────────────────────────────────────
# Chains don't change second-by-second; caching avoids re-download on auto-sync
class _ChainCache:
    TTL = 5 * 60   # 5 minutes

    def __init__(self):
        self._s = {}
        self._l = threading.Lock()

    def get(self, key):
        with self._l:
            e = self._s.get(key)
            return e["data"] if e and time.time() - e["ts"] < self.TTL else None

    def set(self, key, data):
        with self._l:
            self._s[key] = {"data": data, "ts": time.time()}

_chain_cache = _ChainCache()

# ── Company name cache (24h TTL) ──────────────────────────────────────────────
# ticker.info is the slowest yfinance call; names don't change daily
class _NameCache:
    TTL = 24 * 3600

    def __init__(self):
        self._s = {}
        self._l = threading.Lock()

    def get(self, sym):
        with self._l:
            e = self._s.get(sym)
            return e["name"] if e and time.time() - e["ts"] < self.TTL else None

    def set(self, sym, name):
        with self._l:
            self._s[sym] = {"name": name, "ts": time.time()}

    def bulk_set(self, mapping: dict):
        """Pre-populate from screener results {sym: name}."""
        with self._l:
            ts = time.time()
            for sym, name in mapping.items():
                if sym not in self._s:   # don't overwrite fresher entries
                    self._s[sym] = {"name": name, "ts": ts}

_name_cache = _NameCache()
_YAHOO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Fallback list — top 30 consistently active US options
_FALLBACK_WATCHLIST = [
    "SPY","QQQ","AAPL","NVDA","TSLA","AMD","META","AMZN","MSFT","GOOGL",
    "NFLX","IWM","GLD","BABA","PLTR","BAC","SOFI","COIN","F","MARA",
    "RIOT","NIO","INTC","DIS","UBER","MSTR","ARM","GM","RIVN","VXX",
]


def get_dynamic_watchlist(count: int = 30) -> list:
    """
    Fetch the current most-active US stocks from Yahoo Finance screener.
    These consistently have the highest options volume too.
    Falls back to hardcoded list on any error.
    """
    try:
        url = (
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
            f"?formatted=false&lang=en-US&region=US&scrIds=most_actives&count={count}"
        )
        r = requests.get(url, headers=_YAHOO_HEADERS, timeout=10)
        quotes = r.json()["finance"]["result"][0]["quotes"]
        syms = [q["symbol"] for q in quotes if q.get("symbol")]
        if len(syms) >= 10:
            # Pre-warm name cache — names arrive free with screener
            _name_cache.bulk_set({
                q["symbol"]: q.get("shortName") or q.get("longName", q["symbol"])
                for q in quotes if q.get("symbol")
            })
            return syms[:count]
    except Exception:
        pass
    return _FALLBACK_WATCHLIST[:count]


# Kept for backward compat (routes that reference DEFAULT_WATCHLIST)
DEFAULT_WATCHLIST = _FALLBACK_WATCHLIST


def _fmt_vol(v):
    if v is None or v == 0: return "—"
    if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
    if v >= 1_000:     return f"{v/1_000:.0f}K"
    return str(int(v))


def _iv_rank_pctl(hist_closes):
    """
    Returns (iv_rank, iv_pctl) using 20-day realized volatility over 1 year.
    IV Rank  = (HV_now - HV_52w_low) / (HV_52w_high - HV_52w_low) × 100
    IV Pctl  = % of days past year where HV was ≤ current HV
    """
    try:
        log_ret = np.log(hist_closes / hist_closes.shift(1)).dropna()
        hv = log_ret.rolling(20).std() * np.sqrt(252) * 100
        hv = hv.dropna()
        if len(hv) < 30:
            return None, None
        cur  = float(hv.iloc[-1])
        low  = float(hv.min())
        high = float(hv.max())
        rank = round((cur - low) / (high - low) * 100, 1) if high > low else 50.0
        pctl = round(float((hv <= cur).mean()) * 100, 1)
        return rank, pctl
    except Exception:
        return None, None


def get_options_stats(symbol: str) -> dict:
    """Fetch full options stats for one symbol. Returns dict or error dict."""
    sym = symbol.strip().upper()
    try:
        ticker = yf.Ticker(sym)

        # ── price info ────────────────────────────────────────────
        # Name: 24h cache avoids expensive ticker.info call on repeat requests
        name = _name_cache.get(sym)
        price_source = "Yahoo"

        # Try Alpaca for real-time price first (skips ticker.info entirely when it works)
        snap = {}
        try:
            from alpaca_data import get_us_snapshot_single
            snap = get_us_snapshot_single(sym)
        except Exception:
            pass

        if snap.get("price"):
            price    = snap["price"]
            change   = snap.get("change") or 0
            chg_pct  = snap.get("change_pct") or 0
            price_source = "Alpaca"
            # Fetch name from info only if not cached
            if name is None:
                try:
                    info = ticker.info or {}
                    name = info.get("shortName", sym)
                    _name_cache.set(sym, name)
                except Exception:
                    name = sym
        else:
            # Alpaca unavailable — use ticker.info for both price and name
            info  = ticker.info or {}
            name  = info.get("shortName", sym)
            _name_cache.set(sym, name)
            price = info.get("currentPrice") or info.get("regularMarketPrice")
            change  = info.get("regularMarketChange", 0) or 0
            chg_pct = info.get("regularMarketChangePercent", 0) or 0

        name = name or sym

        if not price:
            return {"symbol": sym, "error": "無報價資料"}

        # ── options chain (nearest expiration) ────────────────────
        exps = ticker.options
        if not exps:
            return {"symbol": sym, "error": "無選擇權資料"}

        # Aggregate put/call vol — use chain cache to skip re-download
        total_call_vol = 0
        total_put_vol  = 0
        atm_iv = None
        nearest_exp_str = exps[0]

        for exp in exps[:2]:
            try:
                cache_key = f"{sym}:{exp}"
                chain = _chain_cache.get(cache_key)
                if chain is None:
                    chain = ticker.option_chain(exp)
                    _chain_cache.set(cache_key, chain)
                calls = chain.calls.fillna(0)
                puts  = chain.puts.fillna(0)
                total_call_vol += int(calls["volume"].sum())
                total_put_vol  += int(puts["volume"].sum())

                if atm_iv is None and len(calls):
                    atm_row = calls.iloc[(calls["strike"] - price).abs().argsort()[:1]]
                    iv_val  = float(atm_row["impliedVolatility"].values[0])
                    if 0 < iv_val < 5:          # sanity: 0–500%
                        atm_iv = round(iv_val * 100, 1)
            except Exception:
                continue

        # ── Expected Move calculations ────────────────────────────
        exp_daily = exp_to_exp = days_to_exp = None
        if atm_iv and price:
            iv_dec = atm_iv / 100
            # Daily expected move (±1σ, 68% probability)
            exp_daily = round(price * iv_dec / (252 ** 0.5), 2)
            # Expected move to nearest expiration
            try:
                exp_date  = datetime.strptime(nearest_exp_str, "%Y-%m-%d")
                days_to_exp = max(1, (exp_date - datetime.now()).days)
                exp_to_exp  = round(price * iv_dec * (days_to_exp / 365) ** 0.5, 2)
            except Exception:
                pass

        total_vol = total_call_vol + total_put_vol
        put_pct  = round(total_put_vol  / total_vol * 100, 1) if total_vol else None
        call_pct = round(total_call_vol / total_vol * 100, 1) if total_vol else None
        pc_ratio = round(total_put_vol  / total_call_vol, 2)  if total_call_vol else None

        # ── IV Rank / Pctl from historical realized vol ───────────
        hist = _opt_hist_cache.get(sym)
        if hist is None:
            hist = ticker.history(period="1y")
            if not hist.empty:
                _opt_hist_cache.set(sym, hist)
        iv_rank, iv_pctl = _iv_rank_pctl(hist["Close"]) if len(hist) >= 60 else (None, None)

        return {
            "error":      None,
            "symbol":     sym,
            "name":       name,
            "price":      round(price, 2),
            "price_fmt":  f"${price:,.2f}",
            "change":     round(change, 2),
            "change_fmt": f"{'+' if change >= 0 else ''}{change:.2f}",
            "chg_pct":    round(chg_pct, 2),
            "atm_iv":     atm_iv,
            "atm_iv_fmt": f"{atm_iv:.1f}%" if atm_iv else "N/A",
            "iv_rank":    iv_rank,
            "iv_rank_fmt":f"{iv_rank:.0f}" if iv_rank is not None else "N/A",
            "iv_pctl":    iv_pctl,
            "iv_pctl_fmt":f"{iv_pctl:.0f}%" if iv_pctl is not None else "N/A",
            "options_vol":     total_vol,
            "options_vol_fmt": _fmt_vol(total_vol),
            "call_vol":   total_call_vol,
            "put_vol":    total_put_vol,
            "put_pct":    put_pct,
            "call_pct":   call_pct,
            "pc_ratio":   pc_ratio,
            "pc_ratio_fmt": f"{pc_ratio:.2f}" if pc_ratio is not None else "N/A",
            # ── Expected Move ──────────────────────────────────────
            "exp_daily":     exp_daily,
            "exp_daily_fmt": f"±${exp_daily:.2f}" if exp_daily else "N/A",
            "exp_to_exp":    exp_to_exp,
            "exp_to_exp_fmt":f"±${exp_to_exp:.2f}" if exp_to_exp else "N/A",
            "days_to_exp":   days_to_exp,
            "nearest_exp":   nearest_exp_str,
            "price_source": price_source,
            "fetched_at": datetime.now(_NY_TZ).strftime("%H:%M ET"),
        }

    except Exception as e:
        return {"symbol": sym, "error": str(e)}


def get_options_watchlist(symbols: list, max_workers: int = 15) -> list:
    """Fetch options stats for multiple symbols in parallel."""
    results = {}
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(get_options_stats, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as e:
                results[sym] = {"symbol": sym, "error": str(e)}
    # Preserve original order
    return [results.get(s, {"symbol": s, "error": "timeout"}) for s in symbols]
