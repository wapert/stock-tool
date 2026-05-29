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

@app.route("/search/tw_stocks")
def search_tw_stocks():
    import urllib.request, json as _json
    from urllib.parse import quote
    q = request.args.get("q", "").strip()
    if len(q) < 1:
        return jsonify([])
    cached = _cache.get(f"twsearch:{q}")
    if cached:
        return jsonify(cached)
    results = []
    seen = set()
    try:
        url = (f"https://query2.finance.yahoo.com/v1/finance/search"
               f"?q={quote(q)}&quotesCount=10&newsCount=0"
               f"&enableFuzzyQuery=false&region=TW&lang=zh-TW")
        req = urllib.request.Request(url, headers={"User-Agent":"Mozilla/5.0"})
        data = _json.loads(urllib.request.urlopen(req, timeout=4).read())
        for item in data.get("quotes", []):
            sym = item.get("symbol","")
            typ = item.get("quoteType","")
            if not sym or typ not in ("EQUITY","ETF","MUTUALFUND"):
                continue
            # Keep only TW/TWO listed stocks (4-digit numeric prefix)
            bare = re.sub(r'\.(TW[O]?)$', '', sym, flags=re.IGNORECASE)
            if not re.match(r'^\d{4,6}$', bare):
                continue
            name = item.get("longname") or item.get("shortname") or bare
            if bare not in seen:
                seen.add(bare)
                results.append({"s": bare, "n": name})
    except Exception:
        pass
    _cache.set(f"twsearch:{q}", results, ttl=3600)
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


_TW_SCAN_LIST = [
    {"s":"2330","n":"台積電"},{"s":"2317","n":"鴻海"},{"s":"2454","n":"聯發科"},
    {"s":"2303","n":"聯電"},{"s":"2308","n":"台達電"},{"s":"2881","n":"富邦金"},
    {"s":"2882","n":"國泰金"},{"s":"2412","n":"中華電"},{"s":"2002","n":"中鋼"},
    {"s":"1301","n":"台塑"},{"s":"1303","n":"南亞"},{"s":"1326","n":"台化"},
    {"s":"2886","n":"兆豐金"},{"s":"2884","n":"玉山金"},{"s":"2891","n":"中信金"},
    {"s":"2892","n":"第一金"},{"s":"5880","n":"合庫金"},{"s":"2885","n":"元大金"},
    {"s":"2887","n":"台新金"},{"s":"2883","n":"開發金"},
    {"s":"3711","n":"日月光投控"},{"s":"3008","n":"大立光"},{"s":"2379","n":"瑞昱"},
    {"s":"2344","n":"華邦電"},{"s":"3034","n":"聯詠"},{"s":"2382","n":"廣達"},
    {"s":"2357","n":"華碩"},{"s":"2353","n":"宏碁"},{"s":"2376","n":"技嘉"},
    {"s":"2395","n":"研華"},{"s":"3045","n":"台灣大"},{"s":"4904","n":"遠傳"},
    {"s":"2609","n":"陽明海運"},{"s":"2603","n":"長榮"},{"s":"2615","n":"萬海"},
    {"s":"1101","n":"台泥"},{"s":"1216","n":"統一"},{"s":"2912","n":"統一超"},
    {"s":"6505","n":"台塑化"},{"s":"2327","n":"國巨"},{"s":"3037","n":"欣興"},
    {"s":"2049","n":"上銀"},{"s":"6669","n":"緯穎"},{"s":"4938","n":"和碩"},
    {"s":"2408","n":"南亞科"},{"s":"3231","n":"緯創"},{"s":"2409","n":"友達"},
    {"s":"3481","n":"群創"},{"s":"2207","n":"和泰車"},{"s":"9910","n":"豐泰"},
    {"s":"2301","n":"光寶科"},{"s":"6415","n":"矽力-KY"},{"s":"6414","n":"樺漢"},
    {"s":"3443","n":"創意"},{"s":"2356","n":"英業達"},{"s":"2337","n":"旺宏"},
    {"s":"2360","n":"致茂"},{"s":"2385","n":"群光"},{"s":"2439","n":"美律"},
    {"s":"3702","n":"大聯大"},{"s":"2301","n":"光寶科"},{"s":"4958","n":"臻鼎-KY"},
    {"s":"2392","n":"正崴"},{"s":"3006","n":"晶豪科"},{"s":"3088","n":"艾訊"},
    {"s":"2388","n":"威盛"},{"s":"0050","n":"元大台灣50 ETF"},{"s":"0056","n":"元大高股息 ETF"},
    {"s":"00631L","n":"元大台灣50正2 ETF"},{"s":"00663L","n":"國泰台灣加權正2 ETF"},
]

@app.route("/ebcshow")
def ebcshow_page():
    return render_template("ebcshow.html")

@app.route("/ebcshow/data")
def ebcshow_data():
    data_file = os.path.join(os.path.dirname(__file__), "static", "ebcshow.json")
    if os.path.exists(data_file):
        with open(data_file, encoding="utf-8") as f:
            return jsonify(json.load(f))
    # No cache yet — trigger a fetch
    try:
        from ebcshow import run_daily_fetch
        return jsonify(run_daily_fetch())
    except Exception as e:
        return jsonify({"error": str(e), "videos": []}), 500

@app.route("/ebcshow/refresh", methods=["POST"])
def ebcshow_refresh():
    try:
        from ebcshow import run_daily_fetch
        result = run_daily_fetch()
        return jsonify({"status": "ok", "count": len(result.get("videos", []))})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

_gemini_jobs = {}   # vid_id → "processing" | "done" | "error"

@app.route("/ebcshow/summarize", methods=["POST"])
def ebcshow_summarize():
    """Start Gemini summary in background thread — returns immediately."""
    vid_id = request.json.get("id","") if request.is_json else ""
    if not vid_id:
        return jsonify({"error": "id required"}), 400

    # Already done?
    if _gemini_jobs.get(vid_id) == "done":
        return jsonify({"status": "done"})
    # Already running?
    if _gemini_jobs.get(vid_id) == "processing":
        return jsonify({"status": "processing"})

    def _run():
        marker = f"/tmp/gemini_done_{vid_id}"
        try:
            from ebcshow import summarize_with_gemini, DATA_FILE
            import re as _re
            url    = f"https://www.youtube.com/watch?v={vid_id}"
            result = summarize_with_gemini(url)
            if not result:
                open(f"/tmp/gemini_err_{vid_id}", "w").close()
                return
            # Atomic write: write to .tmp then rename to avoid race conditions
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                for v in data.get("videos", []):
                    if v["id"] == vid_id:
                        v["gemini"] = result
                        tw = list(v.get("tw_stocks", []))
                        us = list(v.get("us_stocks", []))
                        for s in result.get("stocks", []):
                            code = s.get("code", "")
                            if code and _re.match(r'^\d{4}$', code) and code not in tw:
                                tw.append(code)
                            elif code and _re.match(r'^[A-Z]{2,5}$', code) and code not in us:
                                us.append(code)
                        v["tw_stocks"] = sorted(set(tw))
                        v["us_stocks"] = sorted(set(us))
                        break
                tmp = DATA_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, DATA_FILE)   # atomic rename — never partial
            # Write marker file last (signals "done" to all workers)
            open(marker, "w").close()
        except Exception as e:
            app.logger.error("Gemini job error %s: %s", vid_id, e)
            open(f"/tmp/gemini_err_{vid_id}", "w").close()

    _gemini_jobs[vid_id] = "processing"
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "processing"})

@app.route("/ebcshow/status")
def ebcshow_status():
    """Poll job status using marker files (reliable across all workers)."""
    vid_id = request.args.get("id","")
    if os.path.exists(f"/tmp/gemini_done_{vid_id}"):
        return jsonify({"id": vid_id, "status": "done"})
    if os.path.exists(f"/tmp/gemini_err_{vid_id}"):
        return jsonify({"id": vid_id, "status": "error"})
    return jsonify({"id": vid_id, "status": "processing"})

@app.route("/calendar")
def calendar_page():
    return render_template("calendar.html")

@app.route("/calendar/events")
def calendar_events():
    import yfinance as yf
    import json as _json, datetime as _dt, calendar as _cal
    import urllib.request as _ur
    from concurrent.futures import ThreadPoolExecutor

    market = request.args.get("market", "US").upper()
    ym     = request.args.get("month", _dt.date.today().strftime("%Y-%m"))
    try:
        year, month = map(int, ym.split("-"))
    except Exception:
        return jsonify({"error": "invalid month"}), 400

    cache_key = f"cal:{market}:{ym}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    first = _dt.date(year, month, 1)
    last  = _dt.date(year, month, _cal.monthrange(year, month)[1])
    events = []

    if market == "US":
        # ── Load stock list (top 100 + profile stocks) ──────────────────────
        static_path = os.path.join(os.path.dirname(__file__), "static", "us_stocks_top.json")
        with open(static_path) as f:
            top_list = _json.load(f)
        name_map = {s["s"]: s["n"] for s in top_list}
        syms = [s["s"] for s in top_list[:100]]

        # Add profile stocks
        try:
            profiles_path = os.path.join(os.path.dirname(__file__), "profiles.json")
            with open(profiles_path) as f:
                profs = _json.load(f)
            for prof in profs.values():
                for s in prof.get("stocks", []):
                    sym = s.upper()
                    if not _TW_SYM.match(sym) and sym not in syms:
                        syms.append(sym)
        except Exception:
            pass

        def fetch_cal(sym):
            try:
                cal = yf.Ticker(sym).calendar
                if not cal:
                    return []
                result = []
                # Earnings
                for ed in (cal.get("Earnings Date") or []):
                    if hasattr(ed, 'date'): ed = ed.date()
                    if isinstance(ed, str): ed = _dt.datetime.strptime(ed[:10],"%Y-%m-%d").date()
                    if first <= ed <= last:
                        eps_est = cal.get("Earnings Average")
                        result.append({"date": ed.strftime("%Y-%m-%d"), "type": "earnings",
                                       "symbol": sym, "name": name_map.get(sym, sym),
                                       "detail": f"EPS Est: ${eps_est:.2f}" if eps_est else ""})
                # Ex-dividend
                exd = cal.get("Ex-Dividend Date")
                if exd:
                    if hasattr(exd, 'date'): exd = exd.date()
                    if first <= exd <= last:
                        result.append({"date": exd.strftime("%Y-%m-%d"), "type": "exdiv",
                                       "symbol": sym, "name": name_map.get(sym, sym), "detail": ""})
                # Dividend pay date
                dd = cal.get("Dividend Date")
                if dd:
                    if hasattr(dd, 'date'): dd = dd.date()
                    if first <= dd <= last:
                        result.append({"date": dd.strftime("%Y-%m-%d"), "type": "dividend",
                                       "symbol": sym, "name": name_map.get(sym, sym), "detail": ""})
                return result
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=25) as ex:
            for res in ex.map(fetch_cal, syms):
                events.extend(res)

        # US market holidays 2025-2026
        us_holidays = {
            "2025-01-01":"New Year's Day", "2025-01-20":"MLK Day",
            "2025-02-17":"Presidents Day", "2025-04-18":"Good Friday",
            "2025-05-26":"Memorial Day",   "2025-06-19":"Juneteenth",
            "2025-07-04":"Independence Day","2025-09-01":"Labor Day",
            "2025-11-27":"Thanksgiving",    "2025-12-25":"Christmas Day",
            "2026-01-01":"New Year's Day", "2026-01-19":"MLK Day",
            "2026-02-16":"Presidents Day", "2026-04-03":"Good Friday",
            "2026-05-25":"Memorial Day",   "2026-06-19":"Juneteenth",
            "2026-07-03":"Independence Day","2026-09-07":"Labor Day",
            "2026-11-26":"Thanksgiving",    "2026-11-27":"Black Friday (½ day)",
            "2026-12-25":"Christmas Day",
        }
        for hdate, hname in us_holidays.items():
            if hdate.startswith(ym):
                events.append({"date": hdate, "type": "holiday",
                                "symbol": "", "name": hname, "detail": "市場休市"})

        # FOMC meeting dates (announcement day = 2nd day of meeting)
        # * = includes Summary of Economic Projections (dot plot)
        fomc = {
            # 2025
            "2025-01-28":{"start":"2025-01-28","end":"2025-01-29","dot":False},
            "2025-01-29":{"start":"2025-01-28","end":"2025-01-29","dot":False,"announce":True},
            "2025-03-18":{"start":"2025-03-18","end":"2025-03-19","dot":True},
            "2025-03-19":{"start":"2025-03-18","end":"2025-03-19","dot":True,"announce":True},
            "2025-05-06":{"start":"2025-05-06","end":"2025-05-07","dot":False},
            "2025-05-07":{"start":"2025-05-06","end":"2025-05-07","dot":False,"announce":True},
            "2025-06-17":{"start":"2025-06-17","end":"2025-06-18","dot":True},
            "2025-06-18":{"start":"2025-06-17","end":"2025-06-18","dot":True,"announce":True},
            "2025-07-29":{"start":"2025-07-29","end":"2025-07-30","dot":False},
            "2025-07-30":{"start":"2025-07-29","end":"2025-07-30","dot":False,"announce":True},
            "2025-09-16":{"start":"2025-09-16","end":"2025-09-17","dot":True},
            "2025-09-17":{"start":"2025-09-16","end":"2025-09-17","dot":True,"announce":True},
            "2025-10-28":{"start":"2025-10-28","end":"2025-10-29","dot":False},
            "2025-10-29":{"start":"2025-10-28","end":"2025-10-29","dot":False,"announce":True},
            "2025-12-09":{"start":"2025-12-09","end":"2025-12-10","dot":True},
            "2025-12-10":{"start":"2025-12-09","end":"2025-12-10","dot":True,"announce":True},
            # 2026
            "2026-01-27":{"start":"2026-01-27","end":"2026-01-28","dot":False},
            "2026-01-28":{"start":"2026-01-27","end":"2026-01-28","dot":False,"announce":True},
            "2026-03-17":{"start":"2026-03-17","end":"2026-03-18","dot":True},
            "2026-03-18":{"start":"2026-03-17","end":"2026-03-18","dot":True,"announce":True},
            "2026-04-28":{"start":"2026-04-28","end":"2026-04-29","dot":False},
            "2026-04-29":{"start":"2026-04-28","end":"2026-04-29","dot":False,"announce":True},
            "2026-06-16":{"start":"2026-06-16","end":"2026-06-17","dot":True},
            "2026-06-17":{"start":"2026-06-16","end":"2026-06-17","dot":True,"announce":True},
            "2026-07-28":{"start":"2026-07-28","end":"2026-07-29","dot":False},
            "2026-07-29":{"start":"2026-07-28","end":"2026-07-29","dot":False,"announce":True},
            "2026-09-15":{"start":"2026-09-15","end":"2026-09-16","dot":True},
            "2026-09-16":{"start":"2026-09-15","end":"2026-09-16","dot":True,"announce":True},
            "2026-10-27":{"start":"2026-10-27","end":"2026-10-28","dot":False},
            "2026-10-28":{"start":"2026-10-27","end":"2026-10-28","dot":False,"announce":True},
            "2026-12-08":{"start":"2026-12-08","end":"2026-12-09","dot":True},
            "2026-12-09":{"start":"2026-12-08","end":"2026-12-09","dot":True,"announce":True},
        }
        for fdate, info in fomc.items():
            if fdate.startswith(ym):
                is_announce = info.get("announce", False)
                dot_str     = " (含點陣圖)" if info["dot"] else ""
                label       = f"FOMC 利率決策{dot_str} ⚡" if is_announce else f"FOMC 會議 Day1"
                detail      = f"2:00 PM ET 公布利率決策{dot_str}" if is_announce else f"會議第一天 ({info['start']} ~ {info['end']})"
                events.append({"date": fdate, "type": "fed",
                                "symbol": "FED", "name": label, "detail": detail})

    else:  # TW
        # ── TWSE ex-dividend/rights calendar ──────────────────────────────
        date_start = first.strftime("%Y%m%d")
        date_end   = last.strftime("%Y%m%d")
        url = (f"https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
               f"?response=json&strDate={date_start}&endDate={date_end}")
        try:
            req = _ur.Request(url, headers={"User-Agent":"Mozilla/5.0"})
            data = _json.loads(_ur.urlopen(req, timeout=8).read())
            for row in data.get("data", []):
                try:
                    roc_str = str(row[0]).strip()          # e.g. "115年05月28日"
                    parts   = roc_str.replace("年","/").replace("月","/").replace("日","").split("/")
                    gy = int(parts[0]) + 1911
                    gm = int(parts[1]); gd = int(parts[2])
                    etype = "exdiv" if "息" in str(row[6]) else "exright"
                    events.append({
                        "date":   f"{gy}-{gm:02d}-{gd:02d}",
                        "type":   etype,
                        "symbol": str(row[1]).strip(),
                        "name":   str(row[2]).strip(),
                        "detail": f"除{'息' if etype=='exdiv' else '權'}  參考價:{row[4]}",
                    })
                except Exception:
                    pass
        except Exception:
            pass

        # TW profile stocks' earnings from yfinance
        try:
            profiles_path = os.path.join(os.path.dirname(__file__), "profiles.json")
            with open(profiles_path) as f:
                profs = _json.load(f)
            tw_syms = []
            for prof in profs.values():
                for s in prof.get("stocks", []):
                    if _TW_SYM.match(s.upper()):
                        tw_syms.append(s.upper() + ".TW")
            def fetch_tw_cal(yf_sym):
                try:
                    cal = yf.Ticker(yf_sym).calendar
                    if not cal: return []
                    result = []
                    sym = yf_sym.replace(".TW","")
                    for ed in (cal.get("Earnings Date") or []):
                        if hasattr(ed,'date'): ed = ed.date()
                        if isinstance(ed, str): ed = _dt.datetime.strptime(ed[:10],"%Y-%m-%d").date()
                        if first <= ed <= last:
                            result.append({"date":ed.strftime("%Y-%m-%d"),"type":"earnings",
                                           "symbol":sym,"name":sym,"detail":""})
                    return result
                except Exception:
                    return []
            with ThreadPoolExecutor(max_workers=10) as ex:
                for res in ex.map(fetch_tw_cal, tw_syms[:30]):
                    events.extend(res)
        except Exception:
            pass

        # ── FOMC (US Fed impacts TW tech stocks significantly) ────────────
        fomc_announce = {
            "2025-01-29","2025-03-19","2025-05-07","2025-06-18",
            "2025-07-30","2025-09-17","2025-10-29","2025-12-10",
            "2026-01-28","2026-03-18","2026-04-29","2026-06-17",
            "2026-07-29","2026-09-16","2026-10-28","2026-12-09",
        }
        fomc_dot = {"2025-03-19","2025-06-18","2025-09-17","2025-12-10",
                    "2026-03-18","2026-06-17","2026-09-16","2026-12-09"}
        for fdate in fomc_announce:
            if fdate.startswith(ym):
                dot = fdate in fomc_dot
                events.append({"date": fdate, "type": "fed", "symbol": "FED",
                    "name": f"美國FOMC利率決策{'(含點陣圖)' if dot else ''}⚡",
                    "detail": "2:00 PM ET — 台股外資動向重要參考"})

        # ── Taiwan CBC 央行理監事會議 (quarterly) ─────────────────────────
        cbc_dates = {
            # 2025
            "2025-03-20":"台灣央行利率決策 (Q1 2025)",
            "2025-06-19":"台灣央行利率決策 (Q2 2025)",
            "2025-09-18":"台灣央行利率決策 (Q3 2025)",
            "2025-12-18":"台灣央行利率決策 (Q4 2025)",
            # 2026
            "2026-03-19":"台灣央行利率決策 (Q1 2026)",
            "2026-06-18":"台灣央行利率決策 (Q2 2026)",
            "2026-09-17":"台灣央行利率決策 (Q3 2026)",
            "2026-12-17":"台灣央行利率決策 (Q4 2026)",
        }
        for cdate, cname in cbc_dates.items():
            if cdate.startswith(ym):
                events.append({"date": cdate, "type": "cbc", "symbol": "CBC",
                    "name": cname, "detail": "台灣央行理監事聯席會議決議"})

        # ── TAIFEX 台指期/選擇權結算日 ───────────────────────────────────
        # Third Wednesday of each month (with exceptions for holidays)
        taifex_2026 = {
            "2026-01-21","2026-02-23",  # Feb delayed due to Lunar New Year
            "2026-03-18","2026-04-15","2026-05-20","2026-06-17",
            "2026-07-15","2026-08-19","2026-09-16","2026-10-21",
            "2026-11-18","2026-12-16",
        }
        taifex_2025 = {
            "2025-01-15","2025-02-19","2025-03-19","2025-04-16",
            "2025-05-21","2025-06-18","2025-07-16","2025-08-20",
            "2025-09-17","2025-10-15","2025-11-19","2025-12-17",
        }
        for sdate in taifex_2026 | taifex_2025:
            if sdate.startswith(ym):
                events.append({"date": sdate, "type": "taifex", "symbol": "結算",
                    "name": "台指期/選擇權結算日", "detail": "13:30 停止交易，以均價結算"})

        # ── Taiwan market holidays 2025-2026 ─────────────────────────────
        tw_holidays = {
            "2025-01-01":"元旦", "2025-01-27":"農曆年封關",
            "2025-01-28":"春節","2025-01-29":"春節","2025-01-30":"春節",
            "2025-01-31":"春節","2025-02-03":"彈性補假",
            "2025-02-28":"和平紀念日",
            "2025-04-03":"兒童節(補假)","2025-04-04":"清明節",
            "2025-05-01":"勞動節",
            "2025-05-30":"端午節(補假)","2025-05-31":"端午節(補假)",
            "2025-10-10":"國慶日",
            "2026-01-01":"元旦",
            "2026-02-16":"農曆年封關","2026-02-17":"春節",
            "2026-02-18":"春節","2026-02-19":"春節",
            "2026-02-20":"春節","2026-02-23":"春節後開盤",
            "2026-02-28":"和平紀念日(補假)",
            "2026-04-04":"兒童節/清明節",
            "2026-06-19":"端午節",
            "2026-09-25":"中秋節",
            "2026-10-09":"國慶日(補假)","2026-10-10":"國慶日",
        }
        for hdate, hname in tw_holidays.items():
            if hdate.startswith(ym):
                events.append({"date": hdate, "type": "holiday", "symbol": "",
                    "name": hname, "detail": "台股休市" if "封關" not in hname and "開盤" not in hname else ""})

        # ── Taiwan earnings season 財報季 ─────────────────────────────────
        # Q4 results: Mar 31 deadline | Q1: May 15 | Q2: Aug 14 | Q3: Nov 14
        tw_deadlines = {
            "2025-03-31":"Q4財報截止日 (上市公司年報)",
            "2025-05-15":"Q1財報截止日",
            "2025-08-14":"Q2財報截止日 (除息旺季結束)",
            "2025-11-14":"Q3財報截止日",
            "2026-03-31":"Q4財報截止日 (上市公司年報)",
            "2026-05-15":"Q1財報截止日",
            "2026-08-14":"Q2財報截止日 (除息旺季結束)",
            "2026-11-14":"Q3財報截止日",
        }
        for ddate, dname in tw_deadlines.items():
            if ddate.startswith(ym):
                events.append({"date": ddate, "type": "deadline", "symbol": "財報",
                    "name": dname, "detail": "上市/上櫃公司財務報告申報截止"})

    payload = {"market": market, "month": ym, "events": events}
    _cache.set(cache_key, payload, ttl=43200)   # 12h cache
    return jsonify(payload)

@app.route("/stock/institutional")
def stock_institutional():
    import yfinance as yf
    import urllib.request as _ur
    import json as _json
    import datetime as _dt
    import pandas as _pd

    sym    = request.args.get("symbol","").strip().upper()
    is_tw  = bool(_TW_SYM.match(sym))
    cache_key = f"inst:{sym}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    result = {"symbol": sym, "type": "TW" if is_tw else "US"}

    if is_tw:
        # ── Taiwan: TWSE T86 三大法人 (last 5 trading days) ──────────────────
        rows, today = [], _dt.date.today()
        checked = 0
        while len(rows) < 5 and checked < 14:
            d = today - _dt.timedelta(days=checked)
            checked += 1
            if d.weekday() >= 5:
                continue
            date_str = d.strftime("%Y%m%d")
            try:
                url = (f"https://www.twse.com.tw/fund/T86?response=json"
                       f"&date={date_str}&selectType=ALLBUT0999")
                req = _ur.Request(url, headers={"User-Agent":"Mozilla/5.0"})
                data = _json.loads(_ur.urlopen(req, timeout=6).read())
                if data.get("stat") != "OK" or not data.get("data"):
                    continue
                for row in data["data"]:
                    code = str(row[0]).strip()
                    if code == sym:
                        def _n(v):
                            try: return int(str(v).replace(",","").replace(" ",""))
                            except: return 0
                        rows.append({
                            "date":        date_str[:4]+"/"+date_str[4:6]+"/"+date_str[6:],
                            "foreign_net": _n(row[4]),   # 外陸資買賣超
                            "trust_net":   _n(row[10]),  # 投信買賣超
                            "dealer_net":  _n(row[11]),  # 自營商買賣超
                            "total_net":   _n(row[18]),  # 三大法人合計
                        })
                        break
            except Exception:
                continue
        result["rows"] = rows
        # 5-day cumulative
        if rows:
            result["foreign_5d"] = sum(r["foreign_net"] for r in rows)
            result["trust_5d"]   = sum(r["trust_net"]   for r in rows)
            result["dealer_5d"]  = sum(r["dealer_net"]  for r in rows)
            result["total_5d"]   = sum(r["total_net"]   for r in rows)

    else:
        # ── USA: yfinance insider trading ─────────────────────────────────────
        try:
            ticker  = yf.Ticker(sym)
            summary = ticker.insider_purchases
            if summary is not None and not summary.empty:
                def _val(label):
                    rows = summary[summary.iloc[:,0].astype(str).str.contains(label, na=False)]
                    return rows.iloc[0,1] if not rows.empty else None
                result["buy_shares"]  = _val("Purchases")
                result["sell_shares"] = _val("Sales")
                result["net_shares"]  = _val("Net Shares")
                result["pct_net"]     = _val("% Net")
                result["period"]      = "6 months"

            # Recent transactions (last 10)
            trans = ticker.insider_transactions
            if trans is not None and not trans.empty:
                trans = trans.head(10)
                result["transactions"] = []
                for _, row in trans.iterrows():
                    tx = row.get("Transaction","")
                    shares = row.get("Shares", 0)
                    try: shares = int(float(str(shares).replace(",",""))) if shares and str(shares)!="nan" else 0
                    except: shares = 0
                    date_val = row.get("Start Date","")
                    result["transactions"].append({
                        "date":     str(date_val)[:10] if date_val else "",
                        "insider":  str(row.get("Insider",""))[:30],
                        "position": str(row.get("Position",""))[:25],
                        "type":     "買入" if "Purchase" in str(tx) or "Acquisition" in str(tx) else "賣出",
                        "shares":   shares,
                    })
        except Exception as e:
            result["error"] = str(e)

    _cache.set(cache_key, result, ttl=3600)
    return jsonify(result)

@app.route("/serenity")
def serenity_page():
    return render_template("serenity.html")

@app.route("/scan")
def scan_page():
    if _is_mobile():
        return render_template("scan.html")   # same template, responsive
    return render_template("scan.html")

@app.route("/scan/run")
def scan_run():
    import yfinance as yf
    import json as _json

    market = request.args.get("market", "US").upper()
    cache_key = f"scan:{market}"
    cached = _cache.get(cache_key)
    if cached:
        return jsonify(cached)

    # Build ticker list
    if market == "TW":
        stock_list = _TW_SCAN_LIST
        yf_tickers = [s["s"] + ".TW" for s in stock_list]
        name_map   = {s["s"] + ".TW": s["n"] for s in stock_list}
    else:
        static_path = os.path.join(os.path.dirname(__file__), "static", "us_stocks_top.json")
        with open(static_path) as f:
            us_list = _json.load(f)
        stock_list  = us_list[:150]     # top 150 by popularity
        yf_tickers  = [s["s"] for s in stock_list]
        name_map    = {s["s"]: s["n"] for s in stock_list}

    # Batch download — one call for all tickers
    try:
        raw = yf.download(
            yf_tickers, period="6mo",
            group_by="ticker", auto_adjust=True,
            threads=True, progress=False
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    from stock_data import (detect_movement_signals, detect_tw_signals,
                            calculate_rsi, calculate_volume_ratio,
                            calculate_momentum_score)
    results = []

    for sym in yf_tickers:
        try:
            hist = raw[sym].dropna(how="all") if len(yf_tickers) > 1 else raw
            if hist is None or len(hist) < 25:
                continue
            closes  = hist["Close"]
            volumes = hist.get("Volume")
            price   = float(closes.iloc[-1])
            prev    = float(closes.iloc[-2])
            chg_pct = round((price - prev) / prev * 100, 2)
            rsi       = calculate_rsi(closes)
            vol_ratio = calculate_volume_ratio(volumes) if volumes is not None else None
            w52h      = float(closes.tail(252).max())

            if market == "TW":
                signals, k_val, d_val = detect_tw_signals(
                    hist, price, rsi, vol_ratio, w52h)
            else:
                signals = detect_movement_signals(hist, price, rsi, vol_ratio, w52h)
                k_val, d_val = None, None

            if not signals:
                continue
            score   = calculate_momentum_score(signals)
            display = sym.replace(".TW","").replace(".TWO","")
            results.append({
                "symbol":    display,
                "name":      name_map.get(sym, sym),
                "price":     round(price, 2),
                "chg_pct":   chg_pct,
                "rsi":       round(rsi, 1) if rsi else None,
                "kd_k":      k_val,
                "kd_d":      d_val,
                "vol_ratio": round(vol_ratio, 1) if vol_ratio else None,
                "signals":   signals,
                "score":     score,
                "currency":  "TWD" if market == "TW" else "USD",
            })
        except Exception:
            continue

    # Sort by momentum score descending
    results.sort(key=lambda x: x["score"], reverse=True)

    payload = {
        "market":         market,
        "total_scanned":  len(yf_tickers),
        "hits":           len(results),
        "results":        results,
        "ts":             int(time.time()),
    }
    _cache.set(cache_key, payload, ttl=1800)   # cache 30 min
    return jsonify(payload)

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
