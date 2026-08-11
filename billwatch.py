#!/usr/bin/env python3
"""
billwatch.py — by-design / software-mandate bill watcher for GitHub Actions.

Writes a dated digest to digests/<UTC-date>.md and maintains state/seen.json.
The workflow commits both back to the repo. No email, no PAT, no connectors.

Pipeline:
  1. LegiScan getSearch per (state, query).
  2. Diff against state/seen.json (bill_id + change_hash) -> NEW / UPDATED.
  3. Relevance gate via the Claude API IF ANTHROPIC_API_KEY is set; otherwise
     every keyword match is kept (works free, just noisier).
  4. Write digests/<UTC-date>.md and update state/seen.json.

Secrets (repo > Settings > Secrets and variables > Actions):
  LEGISCAN_API_KEY   required
  ANTHROPIC_API_KEY  optional   turns on Claude relevance filtering
  ANTHROPIC_MODEL    optional   default claude-haiku-4-5-20251001
"""

import os, sys, json, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timezone

import yaml  # workflow installs this: pip install pyyaml

CONFIG_PATH = os.environ.get("BILLWATCH_CONFIG", "config.yaml")
STATE_PATH = os.environ.get("BILLWATCH_STATE", "state/seen.json")
LEGISCAN_URL = "https://api.legiscan.com/"

RELEVANCE_CRITERIA = (
    "A bill is RELEVANT only if, when enacted, it would require a specific "
    "technical feature, capability, or implementation to be built into "
    "software, hardware, or an online service - e.g. honoring an opt-out "
    "preference signal, age verification/estimation, a mandated "
    "interoperability or data-portability API, right-to-repair access, or AI "
    "content labeling/provenance/watermarking. It is NOT relevant if it only "
    "funds, studies, sets liability, or imposes disclosure paperwork with no "
    "required technical build."
)


def env(name, required=False, default=""):
    val = (os.environ.get(name) or "").strip()
    if required and not val:
        sys.exit(f"ERROR: required secret {name} is missing or empty.")
    return val or default


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
            print(f"LegiScan HTTP {e.code} [{state} {query}] (invalid/empty key?).")
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


def anthropic_json(key, model, prompt):
    body = json.dumps({
        "model": model, "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in data.get("content", [])).strip().strip("`")
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return json.loads(text)


def relevance_filter(candidates):
    """Returns (kept, gate_on). Fails open. Skipped entirely if no API key."""
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return candidates, False
    model = env("ANTHROPIC_MODEL", default="claude-haiku-4-5-20251001")
    kept, BATCH = [], 20
    for i in range(0, len(candidates), BATCH):
        chunk = candidates[i:i + BATCH]
        listing = "\n".join(
            f'{j}. {b.get("state")} {b.get("bill_number")}: {b.get("title", "")}'
            for j, b in enumerate(chunk)
        )
        prompt = (
            f"{RELEVANCE_CRITERIA}\n\nFor each numbered bill, judge relevance.\n"
            f"{listing}\n\nRespond with ONLY a JSON array, one object per bill: "
            '[{"i": <number>, "relevant": true|false, "reason": "<=12 words"}]. '
            "No prose, no markdown fences."
        )
        try:
            by_i = {d.get("i"): d for d in anthropic_json(key, model, prompt)}
        except Exception as e:
            print(f"Relevance batch {i // BATCH} error ({e}); keeping unfiltered.")
            by_i = {}
        for j, b in enumerate(chunk):
            d = by_i.get(j, {"relevant": True, "reason": ""})
            if d.get("relevant"):
                b["_reason"] = d.get("reason", "")
                kept.append(b)
        time.sleep(0.3)
    return kept, True


def write_digest(bills, gated, scanned, new_count):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"digests/{today}.md"
    lines = [f"# Bill watch — {today}", "",
             f"Scanned {scanned} bills; {new_count} new/updated; "
             f"{len(bills)} relevant "
             f"({'Claude relevance filter' if gated else 'keyword match only'}).", ""]
    if not bills:
        lines.append("_No relevant bills this run._")
    else:
        by_state = {}
        for b in bills:
            by_state.setdefault(b.get("state", "?"), []).append(b)
        for state in sorted(by_state):
            lines.append(f"## {state}")
            for b in sorted(by_state[state], key=lambda x: x.get("bill_number", "")):
                tag = b.get("_status", "NEW")
                num = b.get("bill_number", "")
                url = b.get("url", "")
                title = b.get("title", "")
                last = b.get("last_action", "")
                reason = b.get("_reason", "")
                line = f"- **[{tag}]** [{num}]({url}) — {title}"
                if last:
                    line += f" _(last action: {last})_"
                if reason:
                    line += f" — {reason}"
                lines.append(line)
            lines.append("")
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Wrote {path}")


def main():
    cfg = load_yaml(CONFIG_PATH)
    key = env("LEGISCAN_API_KEY", required=True)
    seen = load_json(STATE_PATH, {})
    print(f"Watching {len(cfg['states'])} states x {len(cfg['queries'])} queries; "
          f"relevance filter {'ON' if env('ANTHROPIC_API_KEY') else 'OFF (keyword only)'}.")

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
            r["_status"] = "NEW"; candidates.append(r)
        elif prev.get("change_hash") != r.get("change_hash"):
            r["_status"] = "UPDATED"; candidates.append(r)
    print(f"Scanned {len(found)} bills; {len(candidates)} new/updated.")

    relevant, gated = relevance_filter(candidates) if candidates else ([], False)
    print(f"{len(relevant)} relevant.")
    write_digest(relevant, gated, len(found), len(candidates))

    for bid, r in found.items():
        seen[bid] = {"change_hash": r.get("change_hash"),
                     "bill_number": r.get("bill_number"), "state": r.get("state")}
    save_json(STATE_PATH, seen)
    print("State updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
