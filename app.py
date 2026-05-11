import json
import os
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from stock_data import analyze_stock

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


def load_profiles() -> dict:
    if not os.path.exists(PROFILES_FILE):
        return {}
    with open(PROFILES_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_profiles(profiles: dict) -> None:
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)


# ── pages ────────────────────────────────────────────────────────────────────

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


# ── ad-hoc stock lookup ───────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.get_json()
    symbols_raw = data.get("symbols", "")
    symbols = [s.strip() for s in symbols_raw.replace(",", " ").split() if s.strip()]
    if not symbols:
        return jsonify({"error": "請輸入至少一個股票代碼。"})
    results = sort_results([analyze_stock(s) for s in symbols[:10]])
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
    results = sort_results([analyze_stock(s) for s in stocks])
    profiles[name]["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    profiles[name]["cache"] = results
    save_profiles(profiles)
    return jsonify({"results": results, "last_sync": profiles[name]["last_sync"]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
