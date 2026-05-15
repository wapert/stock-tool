"""
shioaji_data.py — Real-time Taiwan stock quotes via Shioaji (永豐金證券).
Replaces yfinance price/history data for Taiwan stocks.
Credentials loaded from .env (never hardcoded).
"""
import os
import time
import threading
import pandas as pd
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Singleton API connection ──────────────────────────────────────────────────

_api        = None
_api_lock   = threading.Lock()
_connected  = False
_simulation = True   # set False when you have a live/production API key


def _get_api():
    """Return a connected Shioaji API instance (lazy init, auto-reconnect)."""
    global _api, _connected
    with _api_lock:
        if _api is not None and _connected:
            return _api
        try:
            import shioaji as sj
            key    = os.getenv("SHIOAJI_KEY")
            secret = os.getenv("SHIOAJI_SECRET")
            if not key or not secret:
                raise RuntimeError("Shioaji credentials missing — check .env")
            api = sj.Shioaji(simulation=_simulation)
            api.login(api_key=key, secret_key=secret)
            _api       = api
            _connected = True
            return _api
        except Exception as e:
            _connected = False
            raise


def _exchange(code: str):
    """Return TSE or OTC contract, trying TSE first."""
    api = _get_api()
    try:
        return api.Contracts.Stocks.TSE[code]
    except Exception:
        pass
    try:
        return api.Contracts.Stocks.OTC[code]
    except Exception:
        return None


# ── Snapshots (real-time price) ───────────────────────────────────────────────

def get_tw_snapshots(codes: list) -> dict:
    """
    Batch real-time snapshot for Taiwan stocks.
    Returns { code: {price, prev_close, change, change_pct, volume, ...} }
    Falls back to empty dict on any error so callers use yfinance instead.
    """
    if not codes:
        return {}
    try:
        api       = _get_api()
        contracts = [c for c in (_exchange(code) for code in codes) if c is not None]
        if not contracts:
            return {}
        snaps  = api.snapshots(contracts)
        result = {}
        for s in snaps:
            try:
                price     = float(s.close)
                change    = float(s.change_price)
                chg_pct   = float(s.change_rate)
                prev      = round(price - change, 2)
                result[s.code] = {
                    "price":      price,
                    "prev_close": prev,
                    "change":     round(change, 2),
                    "change_pct": round(chg_pct, 2),
                    "volume":     int(s.volume),
                    "day_high":   float(s.high)  if hasattr(s, "high")  else None,
                    "day_low":    float(s.low)   if hasattr(s, "low")   else None,
                    "open":       float(s.open)  if hasattr(s, "open")  else None,
                    "source":     "Shioaji",
                }
            except Exception:
                continue
        return result
    except Exception:
        return {}


def get_tw_snapshot_single(code: str) -> dict:
    """Convenience wrapper for one symbol — checks prefetch cache first."""
    with _prefetch_lock:
        if code in _prefetch_cache:
            return _prefetch_cache.pop(code)
    return get_tw_snapshots([code]).get(code, {})


# ── Prefetch cache (batch before parallel analyze) ────────────────────────────
_prefetch_cache: dict = {}
_prefetch_lock  = threading.Lock()


def prefetch_tw_snapshots(codes: list) -> None:
    """
    Batch-fetch snapshots for all codes in ONE Shioaji call.
    Call this before the parallel profile sync so each analyze_stock
    reads from cache instead of making individual Shioaji calls.
    """
    if not codes:
        return
    snaps = get_tw_snapshots(codes)
    with _prefetch_lock:
        _prefetch_cache.update(snaps)


def prewarm() -> bool:
    """Connect to Shioaji at startup so the first user request isn't slow."""
    try:
        _get_api()
        return True
    except Exception:
        return False


# ── Historical OHLCV (for RSI / MACD / Bollinger / EMA) ──────────────────────

def get_tw_history(code: str, days: int = 180) -> pd.DataFrame:
    """
    Fetch daily OHLCV bars for a Taiwan stock.
    Returns a DataFrame with columns [Open, High, Low, Close, Volume]
    or an empty DataFrame on failure.
    """
    try:
        api      = _get_api()
        contract = _exchange(code)
        if contract is None:
            return pd.DataFrame()

        end_date   = date.today()
        start_date = end_date - timedelta(days=days + 30)   # buffer for holidays

        kbars = api.kbars(
            contract = contract,
            start    = str(start_date),
            end      = str(end_date),
        )
        df = pd.DataFrame({**kbars})
        if df.empty:
            return pd.DataFrame()

        df.columns = [c.capitalize() for c in df.columns]
        if "Ts" in df.columns:
            df.index = pd.to_datetime(df["Ts"], unit="ns")
            df = df.drop(columns=["Ts"])
        df = df.rename(columns={"Open":"Open","High":"High","Low":"Low",
                                 "Close":"Close","Volume":"Volume"})
        return df[["Open","High","Low","Close","Volume"]].tail(days)

    except Exception:
        return pd.DataFrame()
