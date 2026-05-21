import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, render_template, request, jsonify
from stock_data import analyze_stock
from market_data  import get_market_overview
from options_data import get_options_stats, get_options_watchlist, get_dynamic_watchlist


TW_TZ = ZoneInfo("Asia/Taipei")

# ── Simple in-memory cache ────────────────────────────────────────────────────

class SimpleCache:
    """Thread-safe TTL cache. Keys are strings; values are any JSON-serialisable object."""

    def __init__(self):
        self._store = {}
        self._lock  = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._store.get(key)
            if entry and time.time() - entry["ts"] < entry["ttl"]:
                return entry["data"]
            return None

    def set(self, key: str, data, ttl: int = 60):
        with self._lock:
            self._store[key] = {"data": data, "ts": time.time(), "ttl": ttl}

    def delete(self, key: str):
        with self._lock:
            self._store.pop(key, None)

    def get_or_fetch(self, key: str, fetch_fn, ttl: int = 60):
        cached = self.get(key)
        if cached is not None:
            return cached
        data = fetch_fn()
        self.set(key, data, ttl)
        return data

    def purge_expired(self):
        with self._lock:
            now = time.time()
            expired = [k for k, v in self._store.items() if now - v["ts"] >= v["ttl"]]
            for k in expired:
                del self._store[k]
        return len(expired)

    def stats(self) -> dict:
        with self._lock:
            now = time.time()
            live    = sum(1 for v in self._store.values() if now - v["ts"] < v["ttl"])
            expired = len(self._store) - live
            return {"live": live, "expired": expired, "total": len(self._store)}

_cache = SimpleCache()

# TTL constants (seconds)
TTL_US_STOCK   = 60    # Alpaca price ~real-time; fundamentals stable for 1 min
TTL_TW_STOCK   = 120   # Yahoo already 15-min delayed; 2 min cache adds little
TTL_MARKET_US  = 60    # Sectors/gainers refresh every minute
TTL_MARKET_TW  = 120   # TW batch download is slow; 2 min is fine
TTL_OPTIONS    = 60    # Options chain changes fast; cap at 1 min

_TW_SYM = re.compile(r'^\d{4,6}[A-Za-z]{0,2}(\.TW[O]?)?$', re.IGNORECASE)

def sort_results(results: list) -> list:
    """Taiwan stocks sorted numerically, US stocks sorted alphabetically.
       Errored rows are kept at the end in original order."""
    tw, us, errors = [], [], []
    for r in results:
        if r.get("error"):
            errors.append(r)
        elif r.get("is_taiwan") or _TW_SYM.match(r.get("display_symbol") or r.get("symbol", "")):
            tw.append(r)
        else:
            us.append(r)

    def tw_key(r):
        sym = r.get("display_symbol") or r.get("symbol", "")
        m = re.match(r"(\d+)", sym)
        return int(m.group(1)) if m else 0

    tw.sort(key=tw_key)
    us.sort(key=lambda r: (r.get("display_symbol") or r.get("symbol", "")).upper())
    return tw + us + errors

_here = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__,
            static_folder=os.path.join(_here, "static"),
            template_folder=os.path.join(_here, "templates"))

# DATA_DIR can be overridden via env var to point at a persistent volume on cloud
_data_dir = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
PROFILES_FILE = os.path.join(_data_dir, "profiles.json")

_profiles_lock = threading.Lock()   # serialise all reads + writes


def load_profiles() -> dict:
    """
    Load profiles with automatic fallback to .bak if the main file is corrupt.
    """
    for path in [PROFILES_FILE, PROFILES_FILE + ".bak"]:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            continue   # try next fallback
    return {}


def save_profiles(profiles: dict) -> None:
    """
    Safe save: validate JSON → write to .tmp → atomic rename.
    A .bak copy of the previous good file is kept for emergency recovery.
    The file lock ensures no two threads write simultaneously.
    """
    # 1. Serialise and validate before touching the disk
    data = json.dumps(profiles, ensure_ascii=False, indent=2)
    json.loads(data)                                    # raises if serialisation produced garbage

    with _profiles_lock:
        tmp = PROFILES_FILE + ".tmp"
        # 2. Write to temp file
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        # 3. Keep the current good file as .bak before replacing
        if os.path.exists(PROFILES_FILE):
            os.replace(PROFILES_FILE, PROFILES_FILE + ".bak")
        # 4. Atomic rename — never leaves PROFILES_FILE partially written
        os.replace(tmp, PROFILES_FILE)


# ── pages ────────────────────────────────────────────────────────────────────

@app.after_request
def no_cache_html(resp):
    if "text/html" in resp.content_type:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp

def _is_mobile():
    ua = request.user_agent.string.lower()
    return any(k in ua for k in ["android", "iphone", "ipad", "mobile", "tablet"])

@app.route("/")
def index():
    if _is_mobile():
        return render_template("mobile.html")
    return render_template("index.html")

@app.route("/m")
def mobile():
    return render_template("mobile.html")

@app.route("/desktop")
def desktop():
    return render_template("index.html")

@app.route("/market")
def market_page():
    if _is_mobile():
        return render_template("market_mobile.html")
    return render_template("market.html")

@app.route("/options")
def options_page():
    if _is_mobile():
        return render_template("options_mobile.html")
    return render_template("options.html")

@app.route("/options/data")
def options_data_api():
    raw     = request.args.get("symbols", "").strip()
    symbols = [s.strip().upper() for s in raw.replace(",", " ").split() if s.strip()]
    if not symbols:
        symbols = get_dynamic_watchlist(15)
    symbols = symbols[:15]
    _cache.purge_expired()   # lazy cleanup on each options request
    # Split into cache hits (instant) and misses (need parallel fetch)
    hits, misses = {}, []
    for s in symbols:
        cached = _cache.get(f"opts:{s}")
        if cached is not None:
            hits[s] = cached
        else:
            misses.append(s)

    # Parallel-fetch all cache misses at once, then store
    if misses:
        fresh = get_options_watchlist(misses)
        for r in fresh:
            sym = r.get("symbol", "")
            if sym and not r.get("error"):
                _cache.set(f"opts:{sym}", r, TTL_OPTIONS)
            hits[sym] = r

    results = [hits.get(s, {"symbol": s, "error": "fetch failed"}) for s in symbols]
    return jsonify({"results": results, "count": len(results)})

@app.route("/options/oi")
def options_oi():
    import yfinance as yf
    sym    = request.args.get("symbol", "").strip().upper()
    expiry = request.args.get("expiry", "").strip()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    try:
        ticker = yf.Ticker(sym)
        exps   = ticker.options
        if not exps:
            return jsonify({"error": "No options data"}), 404
        if expiry not in exps:
            expiry = exps[0]
        cache_key = f"{sym}:{expiry}"
        from options_data import _chain_cache
        chain = _chain_cache.get(cache_key)
        if chain is None:
            chain = ticker.option_chain(expiry)
            _chain_cache.set(cache_key, chain)
        calls = chain.calls[["strike","openInterest","volume"]].fillna(0)
        puts  = chain.puts[["strike","openInterest","volume"]].fillna(0)
        merged = calls.merge(puts, on="strike", suffixes=("_c","_p"))
        try:
            price = float(ticker.fast_info.last_price)
            merged = merged[(merged["strike"] >= price*0.75) & (merged["strike"] <= price*1.25)]
        except Exception:
            price = None
        max_pain = None
        try:
            pain = {}
            for s in merged["strike"]:
                cp = float(merged[merged["strike"]>=s]["openInterest_c"].sum()*(merged[merged["strike"]>=s]["strike"]-s).mean() or 0)
                pp = float(merged[merged["strike"]<=s]["openInterest_p"].sum()*(s-merged[merged["strike"]<=s]["strike"]).mean() or 0)
                pain[s] = cp + pp
            if pain: max_pain = min(pain, key=pain.get)
        except Exception:
            pass
        return jsonify({
            "symbol": sym, "expiry": expiry, "expirations": list(exps[:8]),
            "price": price, "max_pain": max_pain,
            "strikes": merged["strike"].tolist(),
            "call_oi": merged["openInterest_c"].astype(int).tolist(),
            "put_oi":  merged["openInterest_p"].astype(int).tolist(),
            "call_vol":merged["volume_c"].astype(int).tolist(),
            "put_vol": merged["volume_p"].astype(int).tolist(),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/search/stocks")
def search_stocks():
    import urllib.request, json as _json
    from urllib.parse import quote
    q = request.args.get("q", "").strip().upper()
    if len(q) < 1:
        return jsonify([])
    cached = _cache.get(f"stocksearch:{q}")
    if cached:
        return jsonify(cached)
    results = []
    seen = set()
    try:
        url = (f"https://query2.finance.yahoo.com/v1/finance/search"
               f"?q={quote(q)}&quotesCount=10&newsCount=0"
               f"&enableFuzzyQuery=false&region=US&lang=en-US")
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        data = _json.loads(urllib.request.urlopen(req, timeout=4).read())
        for item in data.get("quotes", []):
            sym = item.get("symbol","")
            typ = item.get("quoteType","")
            ex  = item.get("exchange","") or item.get("exchDisp","")
            # Keep US equities and ETFs; skip options, bonds, foreign markets
            if not sym or typ not in ("EQUITY","ETF","MUTUALFUND"):
                continue
            # Filter out obviously non-US: TW, HK, LSE, etc.
            if any(sym.endswith(x) for x in (".TW",".TWO",".HK",".L",".PA",".DE",".T")):
                continue
            name = item.get("longname") or item.get("shortname") or sym
            if sym not in seen:
                seen.add(sym)
                results.append({"s": sym, "n": name})
    except Exception:
        pass
    _cache.set(f"stocksearch:{q}", results, ttl=3600)
    return jsonify(results)

@app.route("/news")
def stock_news():
    import yfinance as yf
    import urllib.request
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime

    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"error": "symbol required"}), 400

    cached = _cache.get(f"news:{sym}")
    if cached:
        return jsonify(cached)

    is_tw  = bool(_TW_SYM.match(sym))
    yf_sym = (sym + ".TW") if is_tw else sym
    articles, seen = [], set()

    def _add(title, url, source, ts=0):
        t = (title or "").strip()
        if not t or t in seen or not url:
            return
        seen.add(t)
        articles.append({"title": t, "url": url, "source": source, "time": int(ts)})

    headers = {"User-Agent": "Mozilla/5.0 (compatible; StockTool/1.0)"}

    def _fetch_url(url, timeout=5):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()

    # ── 1. yfinance news ──────────────────────────────────────────────────────
    try:
        news_items = yf.Ticker(yf_sym).news or []
        for item in news_items[:8]:
            _add(item.get("title", ""),
                 item.get("link") or item.get("url", ""),
                 item.get("publisher", "Yahoo Finance"),
                 item.get("providerPublishTime", 0))
    except Exception:
        pass

    # ── 2. 鉅亨網 (TW stocks) ─────────────────────────────────────────────────
    if is_tw:
        try:
            code = re.sub(r'\.(TW[O]?)$', '', sym, flags=re.IGNORECASE)
            url  = f"https://api.cnyes.com/media/api/v1/search?keyword={code}&type=news&limit=8"
            data = json.loads(_fetch_url(url))
            for item in data.get("items", {}).get("data", []):
                nid = item.get("newsId") or item.get("id", "")
                _add(item.get("title", ""),
                     f"https://news.cnyes.com/news/id/{nid}",
                     "鉅亨網",
                     item.get("publishAt", 0))
        except Exception:
            pass

    # ── 3. Google News RSS ────────────────────────────────────────────────────
    try:
        if is_tw:
            code = re.sub(r'\.(TW[O]?)$', '', sym, flags=re.IGNORECASE)
            q   = quote(f"{code} 股票")
            gurl = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
        else:
            q   = quote(f"{sym} stock earnings")
            gurl = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
        xml_bytes = _fetch_url(gurl, timeout=6)
        root = ET.fromstring(xml_bytes)
        for item in root.findall(".//item")[:8]:
            title  = item.findtext("title", "")
            link   = item.findtext("link", "")
            source = item.findtext("source", "Google News")
            pub    = item.findtext("pubDate", "")
            ts = 0
            try:
                ts = int(parsedate_to_datetime(pub).timestamp())
            except Exception:
                pass
            _add(title, link, source, ts)
    except Exception:
        pass

    # ── 4. Yahoo Finance RSS (US) ─────────────────────────────────────────────
    if not is_tw:
        try:
            rurl = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={sym}&region=US&lang=en-US"
            xml_bytes = _fetch_url(rurl, timeout=5)
            root = ET.fromstring(xml_bytes)
            for item in root.findall(".//item")[:6]:
                title  = item.findtext("title", "")
                link   = item.findtext("link", "")
                pub    = item.findtext("pubDate", "")
                ts = 0
                try:
                    ts = int(parsedate_to_datetime(pub).timestamp())
                except Exception:
                    pass
                _add(title, link, "Yahoo Finance", ts)
        except Exception:
            pass

    articles.sort(key=lambda x: x["time"], reverse=True)
    result = {"symbol": sym, "articles": articles[:12]}
    _cache.set(f"news:{sym}", result, ttl=600)   # cache 10 min
    return jsonify(result)

@app.route("/chart/ma")
def chart_ma():
    import yfinance as yf
    sym    = request.args.get("symbol", "").strip().upper()
    period = request.args.get("period", "6mo").strip()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    valid_periods = {"1mo","3mo","6mo","1y","2y"}
    if period not in valid_periods:
        period = "6mo"
    try:
        ticker = yf.Ticker(sym)
        hist   = ticker.history(period=period)
        if hist.empty:
            return jsonify({"error": f"No price history for {sym}"}), 404
        close  = [round(float(v), 2) for v in hist["Close"]]
        dates  = [d.strftime("%Y-%m-%d") for d in hist.index]

        def sma(arr, n):
            out = []
            for i in range(len(arr)):
                out.append(round(sum(arr[max(0,i-n+1):i+1]) / min(i+1,n), 2) if i >= n-1 else None)
            return out

        def ema(arr, n):
            k, out = 2/(n+1), []
            for i, v in enumerate(arr):
                out.append(round(v if i==0 else v*k + out[-1]*(1-k), 2))
            return out

        return jsonify({
            "symbol": sym, "period": period,
            "dates":  dates,
            "close":  close,
            "mas": {
                "ema8":   ema(close, 8),
                "ema21":  ema(close, 21),
                "sma5":   sma(close, 5),
                "sma10":  sma(close, 10),
                "sma20":  sma(close, 20),
                "sma50":  sma(close, 50),
                "sma60":  sma(close, 60),
                "sma150": sma(close, 150),
                "sma200": sma(close, 200),
                "sma240": sma(close, 240),
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/options/single")
def options_single():
    sym = request.args.get("symbol", "").strip().upper()
    if not sym:
        return jsonify({"error": "symbol required"}), 400
    result = _cache.get_or_fetch(f"opts:{sym}", lambda: get_options_stats(sym), TTL_OPTIONS)
    return jsonify(result)

@app.route("/market/data")
def market_data_api():
    market = request.args.get("market", "US").upper()
    if market not in ("US", "TW"):
        return jsonify({"error": "market must be US or TW"}), 400
    ttl = TTL_MARKET_US if market == "US" else TTL_MARKET_TW
    data = _cache.get_or_fetch(
        f"market:{market}",
        lambda: get_market_overview(market),
        ttl
    )
    return jsonify(data)


# ── ad-hoc stock lookup ───────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    symbols_raw = data.get("symbols", "")
    if isinstance(symbols_raw, list):
        symbols = [str(s).strip() for s in symbols_raw if str(s).strip()]
    else:
        symbols = [s.strip() for s in str(symbols_raw).replace(",", " ").split() if s.strip()]
    if not symbols:
        return jsonify({"error": "請輸入至少一個股票代碼。"})

    def _fetch(sym):
        is_tw = bool(_TW_SYM.match(sym))
        ttl   = TTL_TW_STOCK if is_tw else TTL_US_STOCK
        return _cache.get_or_fetch(f"stock:{sym}", lambda s=sym: analyze_stock(s), ttl)

    syms = symbols[:10]
    with ThreadPoolExecutor(max_workers=min(8, len(syms))) as ex:
        raw = list(ex.map(_fetch, syms))
    results = sort_results(raw)
    return jsonify({"results": results})


# ── profiles CRUD ─────────────────────────────────────────────────────────────

@app.route("/profiles", methods=["GET"])
def list_profiles():
    profiles = load_profiles()
    summary = {
        name: {
            "stocks": p.get("stocks", []),
            "last_sync": p.get("last_sync"),
        }
        for name, p in profiles.items()
    }
    return jsonify(summary)


@app.route("/profiles", methods=["POST"])
def create_profile():
    data = request.get_json()
    name = (data.get("name") or "").strip()
    stocks = [s.strip().upper() for s in data.get("stocks", []) if s.strip()]
    if not name:
        return jsonify({"error": "請輸入投資組合名稱"}), 400
    profiles = load_profiles()
    if name in profiles:
        return jsonify({"error": f"已存在名為「{name}」的投資組合"}), 400
    profiles[name] = {"stocks": stocks, "last_sync": None, "cache": []}
    save_profiles(profiles)
    return jsonify({"ok": True, "name": name})


@app.route("/profiles/<name>", methods=["GET"])
def get_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    p = profiles[name]
    return jsonify({
        "name": name,
        "stocks": p.get("stocks", []),
        "last_sync": p.get("last_sync"),
        "cache": p.get("cache", []),
    })


@app.route("/profiles/<name>", methods=["PUT"])
def update_profile(name):
    data = request.get_json()
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404

    new_name = (data.get("new_name") or "").strip()
    stocks   = data.get("stocks")

    if new_name and new_name != name:
        if new_name in profiles:
            return jsonify({"error": f"已存在名為「{new_name}」的投資組合"}), 400
        profiles[new_name] = profiles.pop(name)
        name = new_name

    if stocks is not None:
        profiles[name]["stocks"] = [s.strip().upper() for s in stocks if s.strip()]

    save_profiles(profiles)
    return jsonify({"ok": True, "name": name})


@app.route("/profiles/<name>", methods=["DELETE"])
def delete_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    del profiles[name]
    save_profiles(profiles)
    return jsonify({"ok": True})


@app.route("/profiles/<name>/sync", methods=["POST"])
def sync_profile(name):
    profiles = load_profiles()
    if name not in profiles:
        return jsonify({"error": "找不到此投資組合"}), 404
    stocks = profiles[name].get("stocks", [])
    if not stocks:
        return jsonify({"error": "此投資組合沒有股票，請先加入股票"}), 400
    # Bust cache so each worker fetches fresh data
    for s in stocks:
        _cache.delete(f"stock:{s.upper()}")

    # ── Batch prefetch: ONE API call each for Alpaca (US) and Shioaji (TW) ──
    us_syms  = [s for s in stocks if not _TW_SYM.match(s.upper())]
    tw_codes = [re.sub(r'\.(TW[O]?)$', '', s, flags=re.IGNORECASE)
                for s in stocks if _TW_SYM.match(s.upper())]

    def _do_alpaca():
        try:
            from alpaca_data import prefetch_us_snapshots
            prefetch_us_snapshots(us_syms)
        except Exception:
            pass

    def _do_shioaji():
        try:
            from shioaji_data import prefetch_tw_snapshots
            prefetch_tw_snapshots(tw_codes)
        except Exception:
            pass

    # Run both prefetches concurrently — total wait = max(alpaca, shioaji) not sum
    with ThreadPoolExecutor(max_workers=2) as ex:
        f1 = ex.submit(_do_alpaca)  if us_syms  else None
        f2 = ex.submit(_do_shioaji) if tw_codes else None
        if f1: f1.result()
        if f2: f2.result()

    # Parallel fetch — up to 16 workers (Oracle has 6 cores; GCP capped at 8)
    def _fetch_one(sym):
        is_tw = bool(_TW_SYM.match(sym.upper()))
        ttl   = TTL_TW_STOCK if is_tw else TTL_US_STOCK
        return _cache.get_or_fetch(
            f"stock:{sym.upper()}", lambda s=sym: analyze_stock(s), ttl
        )

    max_w   = min(16, len(stocks))
    with ThreadPoolExecutor(max_workers=max_w) as ex:
        raw = list(ex.map(_fetch_one, stocks))
    results = sort_results(raw)
    profiles[name]["last_sync"] = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    profiles[name]["cache"] = results
    save_profiles(profiles)
    return jsonify({"results": results, "last_sync": profiles[name]["last_sync"]})


@app.route("/cache/stats")
def cache_stats():
    from stock_data import _hist_cache
    purged = _cache.purge_expired()
    return jsonify({
        "result_cache":  {**_cache.stats(), "just_purged": purged},
        "history_cache": _hist_cache.stats(),
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
