#!/usr/bin/env python3
"""
billwatch.py — fetch + diff only, for use inside a Claude Code routine.

It does NOT judge relevance or write the digest — the routine's Claude does
that (free, no API key). This script:
  1. Queries LegiScan getSearch per (state, query).
  2. Diffs against state/seen.json by bill_id + change_hash -> NEW / UPDATED.
  3. For each new/updated bill, calls LegiScan getBill for its text-document
     list, then getBillText for the most recent one, and attaches a plain-text
     excerpt (text_excerpt) of the actual bill language. This gives the
     routine real bill content straight from LegiScan's API — no scraping
     legiscan.com pages or falling back to web search to judge relevance.
  4. Writes candidates.json (bills to consider) and updates state/seen.json.

Hardened: strips whitespace off the key and surfaces LegiScan errors instead
of failing silently.

Env (set in the routine's cloud environment):
  LEGISCAN_API_KEY   required
"""

import os, re, sys, json, time, base64, html, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

import yaml  # installed by the routine setup script: pip install pyyaml

CONFIG_PATH = os.environ.get("BILLWATCH_CONFIG", "config.yaml")
STATE_PATH = os.environ.get("BILLWATCH_STATE", "state/seen.json")
OUT_PATH = os.environ.get("BILLWATCH_OUT", "candidates.json")
LEGISCAN_URL = "https://api.legiscan.com/"


def load_yaml(path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save_json(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)


def legiscan_search(key, state, query, year):
    params = urllib.parse.urlencode(
        {"key": key, "op": "getSearch", "state": state, "query": query, "year": year}
    )
    url = f"{LEGISCAN_URL}?{params}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            print(f"LegiScan HTTP {e.code} [{state} {query}] "
                  f"(likely an invalid/empty key or a blocked IP).")
            return []
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if data.get("status") != "OK":
        alert = data.get("alert", {}) or {}
        print(f"LegiScan non-OK [{state} {query}]: {data.get('status')} "
              f"{alert.get('message', '')}")
        return []
    sr = data.get("searchresult", {})
    return [v for k, v in sr.items() if k != "summary" and isinstance(v, dict)]


def legiscan_get_bill(key, bill_id):
    params = urllib.parse.urlencode({"key": key, "op": "getBill", "id": bill_id})
    url = f"{LEGISCAN_URL}?{params}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            print(f"LegiScan HTTP {e.code} [getBill {bill_id}] "
                  f"(likely an invalid/empty key or a blocked IP).")
            return None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if data.get("status") != "OK":
        alert = data.get("alert", {}) or {}
        print(f"LegiScan non-OK [getBill {bill_id}]: {data.get('status')} "
              f"{alert.get('message', '')}")
        return None
    return data.get("bill", {})


def legiscan_get_bill_text(key, doc_id):
    params = urllib.parse.urlencode({"key": key, "op": "getBillText", "id": doc_id})
    url = f"{LEGISCAN_URL}?{params}"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            print(f"LegiScan HTTP {e.code} [getBillText {doc_id}] "
                  f"(likely an invalid/empty key or a blocked IP).")
            return None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    if data.get("status") != "OK":
        alert = data.get("alert", {}) or {}
        print(f"LegiScan non-OK [getBillText {doc_id}]: {data.get('status')} "
              f"{alert.get('message', '')}")
        return None
    return data.get("text", {})


STYLE_SCRIPT_RE = re.compile(r"<(style|script)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t]+")
BLANKLINES_RE = re.compile(r"\n\s*\n+")


def html_to_text(markup):
    text = STYLE_SCRIPT_RE.sub("\n", markup)
    text = TAG_RE.sub("\n", text)
    text = html.unescape(text)
    text = WS_RE.sub(" ", text)
    text = BLANKLINES_RE.sub("\n", text)
    return text.strip()


EXCERPT_CHARS = 6000


def fetch_text_excerpt(key, bill_id):
    bill = legiscan_get_bill(key, bill_id)
    texts = (bill or {}).get("texts") or []
    if not texts:
        return ""
    latest = max(texts, key=lambda t: t.get("date", ""))
    if latest.get("mime_id") != 1:  # only text/html is handled
        return ""
    doc = legiscan_get_bill_text(key, latest["doc_id"])
    if not doc or not doc.get("doc"):
        return ""
    markup = base64.b64decode(doc["doc"]).decode("utf-8", errors="replace")
    return html_to_text(markup)[:EXCERPT_CHARS]


def main():
    cfg = load_yaml(CONFIG_PATH)
    key = (os.environ.get("LEGISCAN_API_KEY") or "").strip()
    if not key:
        sys.exit("ERROR: LEGISCAN_API_KEY is missing or empty.")
    seen = load_json(STATE_PATH, {})

    found = {}
    for state in cfg["states"]:
        for query in cfg["queries"]:
            for r in legiscan_search(key, state, query, cfg["year"]):
                bid = str(r.get("bill_id"))
                if bid:
                    found[bid] = r
            time.sleep(0.3)

    candidates = []
    for bid, r in found.items():
        prev = seen.get(bid)
        if prev is None:
            r["_status"] = "NEW"
            candidates.append(r)
        elif prev.get("change_hash") != r.get("change_hash"):
            r["_status"] = "UPDATED"
            candidates.append(r)

    for bid, r in found.items():
        seen[bid] = {"change_hash": r.get("change_hash"),
                     "bill_number": r.get("bill_number"), "state": r.get("state")}

    for r in candidates:
        r["text_excerpt"] = fetch_text_excerpt(key, r.get("bill_id"))
        time.sleep(0.3)

    save_json(OUT_PATH, {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(candidates),
        "bills": candidates,
    })
    save_json(STATE_PATH, seen)
    print(f"Scanned {len(found)} bills; {len(candidates)} new/updated -> {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
