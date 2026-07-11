"""Helpers for enriching fixed harness diagnostics from diagnostic_catalog.json.

The catalog is metadata for saved-output repair prompts and deterministic preflight reports.
These helpers are read-only and do not change gate outcomes.
"""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CATALOG = REPO_ROOT / "test_harness" / "diagnostic_catalog.json"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_diagnostic_catalog(path: str | Path = DEFAULT_CATALOG) -> dict[str, Any]:
    target = Path(path)
    if not target.is_absolute():
        target = REPO_ROOT / target
    try:
        loaded = read_json(target)
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def diagnostic_code(item: dict[str, Any]) -> str:
    return str(item.get("error_code") or item.get("code") or item.get("finding_code") or "").strip()


def catalog_codes(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = catalog.get("codes")
    return {str(key): value for key, value in raw.items() if isinstance(value, dict)} if isinstance(raw, dict) else {}


def catalog_families(catalog: dict[str, Any]) -> list[dict[str, Any]]:
    raw = catalog.get("families")
    result: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("pattern"), str):
            continue
        try:
            pattern = re.compile(item["pattern"])
        except re.error:
            continue
        result.append({"pattern": item["pattern"], "regex": pattern, "record": item})
    return result


def catalog_lookup(code: str, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    if catalog is None:
        catalog = load_diagnostic_catalog()
    if not code:
        return {"coverage": "missing_code"}
    exact = catalog_codes(catalog).get(code)
    if exact is not None:
        return {"coverage": "exact", "record": exact}
    for family in catalog_families(catalog):
        if family["regex"].search(code):
            return {
                "coverage": "family",
                "record": family["record"],
                "family_pattern": family["pattern"],
            }
    return {"coverage": "none"}


def enrich_diagnostic(item: dict[str, Any], catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    result = dict(item)
    code = diagnostic_code(result)
    if code and not result.get("error_code"):
        result["error_code"] = code

    lookup = catalog_lookup(code, catalog)
    coverage = str(lookup.get("coverage") or "none")
    result["catalog_coverage"] = coverage

    record = lookup.get("record") if isinstance(lookup.get("record"), dict) else {}
    if not record:
        return result

    if lookup.get("family_pattern"):
        result["catalog_family_pattern"] = lookup["family_pattern"]

    category = record.get("category")
    if isinstance(category, str) and category:
        result["catalog_category"] = category
    operator_action = record.get("operator_action")
    if isinstance(operator_action, str) and operator_action:
        result["operator_action"] = operator_action
    if isinstance(record.get("model_visible"), bool):
        result["model_visible"] = record["model_visible"]

    severity = record.get("severity") or record.get("default_severity")
    if not result.get("severity") and isinstance(severity, str) and severity:
        result["severity"] = severity

    message = record.get("message")
    if not result.get("message") and isinstance(message, str) and message:
        result["message"] = message
        result["message_source"] = f"catalog_{coverage}"

    repair_hint = record.get("repair_hint")
    if isinstance(repair_hint, str) and repair_hint:
        if not result.get("repair_hint"):
            result["repair_hint"] = repair_hint
            result["repair_hint_source"] = f"catalog_{coverage}"
        else:
            result["repair_hint_source"] = result.get("repair_hint_source") or "diagnostic"
            if result.get("repair_hint") != repair_hint:
                result["catalog_repair_hint"] = repair_hint

    return result


def enrich_diagnostics(diagnostics: list[Any], catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if catalog is None:
        catalog = load_diagnostic_catalog()
    return [enrich_diagnostic(item, catalog) for item in diagnostics if isinstance(item, dict)]


def enrich_diagnostic_payload(payload: Any, catalog: dict[str, Any] | None = None) -> Any:
    if catalog is None:
        catalog = load_diagnostic_catalog()
    if isinstance(payload, list):
        return [enrich_diagnostic_payload(item, catalog) for item in payload]
    if not isinstance(payload, dict):
        return payload
    result = dict(payload)
    if isinstance(result.get("diagnostics"), list):
        result["diagnostics"] = enrich_diagnostics(result["diagnostics"], catalog)
    elif diagnostic_code(result):
        result = enrich_diagnostic(result, catalog)
    return result


def catalog_summary(diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    coverage = Counter(str(item.get("catalog_coverage") or "none") for item in diagnostics)
    categories = Counter(str(item.get("catalog_category") or "uncategorized") for item in diagnostics)
    actions = Counter(str(item.get("operator_action") or "unspecified") for item in diagnostics)
    return {
        "coverage": dict(sorted(coverage.items())),
        "categories": dict(sorted(categories.items())),
        "operator_actions": dict(sorted(actions.items())),
        "uncataloged_count": int(coverage.get("none", 0)),
        "family_only_count": int(coverage.get("family", 0)),
        "exact_count": int(coverage.get("exact", 0)),
    }
