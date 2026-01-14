"""Smoke test: validate the MER Balance Sheet YAML rulebook.

This is intentionally lightweight and does NOT call external systems.
It catches common issues (missing keys, duplicate rule IDs, unknown evaluation types)
so you can iterate on the rulebook quickly.

Run:
  python scripts/mer_rulebook_smoke.py

Optional env vars:
  MER_RULEBOOK_PATH  (default: data/mer_rulebooks/balance_sheet_review_points.yaml)
"""

from __future__ import annotations

import os
import sys
from collections import Counter

from dotenv import load_dotenv

# Allow running as: `python scripts/mer_rulebook_smoke.py`
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

load_dotenv(override=False)


def _fail(msg: str) -> int:
    print(f"ERROR: {msg}", file=sys.stderr)
    return 1


def main() -> int:
    try:
        import yaml
    except Exception as e:
        return _fail(
            f"PyYAML not installed or import failed: {e}. Run: pip install -r requirements.txt"
        )

    path = os.environ.get(
        "MER_RULEBOOK_PATH",
        os.path.join(_REPO_ROOT, "data", "mer_rulebooks", "balance_sheet_review_points.yaml"),
    )

    if not os.path.exists(path):
        return _fail(f"Rulebook file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    if not isinstance(doc, dict):
        return _fail("Rulebook YAML must parse to a mapping (dict).")

    rulebook = doc.get("rulebook")
    if not isinstance(rulebook, dict):
        return _fail("Missing or invalid top-level key: rulebook")

    for key in ["id", "version", "title", "policies"]:
        if not rulebook.get(key):
            return _fail(f"rulebook.{key} is required")

    rules = doc.get("rules")
    if not isinstance(rules, list) or not rules:
        return _fail("Top-level rules must be a non-empty list")

    # Minimal schema checks
    missing_rule_ids = [i for i, r in enumerate(rules) if not isinstance(r, dict) or not r.get("rule_id")]
    if missing_rule_ids:
        return _fail(f"rules entries missing rule_id at indexes: {missing_rule_ids}")

    rule_ids = [r["rule_id"] for r in rules if isinstance(r, dict) and r.get("rule_id")]
    dupes = [rid for rid, c in Counter(rule_ids).items() if c > 1]
    if dupes:
        return _fail(f"Duplicate rule_id(s): {dupes}")

    # Evaluation types we currently support in code (others are allowed but will be unimplemented).
    supported_eval_types = {
        "balance_sheet_line_items_must_be_zero",
        "mer_line_amount_matches_qbo_line_amount",
        "mer_bank_balance_matches_qbo_bank_balance",
    }

    eval_types = [
        ((r.get("evaluation") or {}).get("type")) if isinstance(r, dict) else None
        for r in rules
    ]
    unknown_eval_types = sorted({t for t in eval_types if t and t not in supported_eval_types})

    print("✅ Rulebook parsed")
    print(f"- Path: {path}")
    print(f"- Rulebook ID: {rulebook.get('id')}")
    print(f"- Version: {rulebook.get('version')}")
    print(f"- Rules: {len(rules)}")

    implemented = sum(1 for t in eval_types if t in supported_eval_types)
    untyped = sum(1 for t in eval_types if not t)
    print(f"- Rules with implemented evaluation_type: {implemented}")
    print(f"- Rules missing evaluation_type (placeholders): {untyped}")
    if unknown_eval_types:
        print("- Unimplemented evaluation_type values found:")
        for t in unknown_eval_types:
            print(f"  - {t}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
