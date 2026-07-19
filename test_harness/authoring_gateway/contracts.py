"""Pure, SDK-free validation for model authoring output contracts."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

SUPPORTED_KINDS = frozenset(
    {
        "attack_dsl",
        "flat_recipe",
        "cluster_seed",
        "needs_harness_extension",
        "campaign_request",
        "api_plugin_candidate",
        "visual_review_report",
    }
)
FILESYSTEM_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

VISUAL_REVIEW_PLAUSIBILITY = frozenset({"plausible", "suspect", "implausible"})
VISUAL_REVIEW_CONSISTENCY = frozenset({"consistent", "inconsistent", "unclear"})
VISUAL_REVIEW_MISUSE_FLAGS = frozenset(
    {"tool_misplaced", "scale_suspect", "empty_result", "view_mismatch", "other"}
)
VISUAL_REVIEW_TOP_LEVEL_FIELDS = frozenset(
    {"kind", "schema_version", "case_reviews", "overall_notes_zh_cn"}
)
VISUAL_REVIEW_CASE_FIELDS = frozenset(
    {
        "case_id",
        "geometry_plausibility",
        "view_consistency",
        "misuse_flags",
        "confidence",
        "notes_zh_cn",
        "fault_hint",
    }
)
VISUAL_REVIEW_FAULT_HINTS = frozenset({"test_expectation", "geometry", "transport", "tooling", "unclear"})
# Advisory output must never smuggle execution authority into the harness.
VISUAL_REVIEW_AUTHORITY_FIELDS = frozenset(
    {
        "command",
        "commands",
        "tool",
        "runner",
        "approve",
        "approval",
        "execute",
        "execution",
        "decision",
        "gate",
    }
)
VISUAL_REVIEW_MAX_CASES = 64
VISUAL_REVIEW_MAX_FLAGS = 16
VISUAL_REVIEW_MAX_CASE_NOTES_CHARS = 500
VISUAL_REVIEW_MAX_OVERALL_NOTES_CHARS = 1000


def normalize_visual_review_candidate(value: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministically drop null / non-string misuse_flags entries.

    Fixed host-side normalization for a known vision-model quirk
    (``"misuse_flags": [null]``); every other field is left untouched and the
    raw candidate remains preserved in the attempt evidence.
    """

    candidate = dict(value)
    reviews = candidate.get("case_reviews")
    if isinstance(reviews, list):
        normalized_reviews: list[Any] = []
        for item in reviews:
            if not isinstance(item, dict):
                normalized_reviews.append(item)
                continue
            entry = dict(item)
            flags = entry.get("misuse_flags")
            if isinstance(flags, list):
                entry["misuse_flags"] = [
                    flag for flag in flags if isinstance(flag, str) and flag.strip()
                ]
            normalized_reviews.append(entry)
        candidate["case_reviews"] = normalized_reviews
    return candidate


@dataclass(frozen=True)
class ContractDiagnostic:
    severity: str
    error_code: str
    path: str
    message: str
    repair_hint: str
    expected_shape: Any | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "severity": self.severity,
            "error_code": self.error_code,
            "path": self.path,
            "message": self.message,
            "repair_hint": self.repair_hint,
        }
        if self.expected_shape is not None:
            result["expected_shape"] = self.expected_shape
        return result


@dataclass
class ContractReport:
    ok: bool
    kind: str = ""
    diagnostics: list[ContractDiagnostic] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "error_count": sum(item.severity == "error" for item in self.diagnostics),
            "warning_count": sum(item.severity == "warning" for item in self.diagnostics),
            "diagnostics": [item.as_dict() for item in self.diagnostics],
        }


def _error(
    code: str,
    path: str,
    message: str,
    hint: str,
    expected: Any | None = None,
) -> ContractDiagnostic:
    return ContractDiagnostic("error", code, path, message, hint, expected)


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _contains_secret(value: Any, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in secrets)
    if isinstance(value, list):
        return any(_contains_secret(item, secrets) for item in value)
    if isinstance(value, dict):
        return any(
            _contains_secret(str(key), secrets) or _contains_secret(item, secrets)
            for key, item in value.items()
        )
    return False


def _validate_case_id(value: Any, path: str, diagnostics: list[ContractDiagnostic]) -> None:
    if _nonempty_string(value) and not FILESYSTEM_SAFE_ID.fullmatch(str(value)):
        diagnostics.append(
            _error(
                "CASE_ID_NOT_FILESYSTEM_SAFE",
                path,
                "case_id must be 1-128 ASCII letters, digits, dot, underscore, or hyphen "
                "and start with a letter or digit.",
                "Replace case_id with a stable filesystem-safe identifier.",
            )
        )


def _validate_common(candidate: Mapping[str, Any], diagnostics: list[ContractDiagnostic]) -> None:
    for key in ("notes", "commands"):
        if key in candidate and not isinstance(candidate[key], list):
            diagnostics.append(
                _error(
                    f"{key.upper()}_NOT_ARRAY",
                    f"$.{key}",
                    f"{key} must be an array when present.",
                    f"Use an array for {key} or omit the field.",
                )
            )
        elif key == "notes" and isinstance(candidate.get(key), list) and any(
            not isinstance(item, str) for item in candidate[key]
        ):
            diagnostics.append(
                _error(
                    "NOTES_ITEM_NOT_STRING",
                    "$.notes",
                    "notes entries must all be strings.",
                    "Remove non-string notes.",
                )
            )
    if "commands" in candidate or "command" in candidate:
        diagnostics.append(
            _error(
                "FREEFORM_COMMAND_FIELD_FORBIDDEN",
                "$.commands" if "commands" in candidate else "$.command",
                "The Message API contract cannot carry executable command fields.",
                "Remove command/commands. Campaigns use campaign_request with a profile_id and typed args.",
            )
        )


def _validate_attack_dsl(candidate: Mapping[str, Any], diagnostics: list[ContractDiagnostic]) -> None:
    dsl = candidate.get("dsl")
    if not isinstance(dsl, dict):
        diagnostics.append(
            _error(
                "ATTACK_DSL_MISSING_DSL",
                "$.dsl",
                "attack_dsl requires a dsl object.",
                "Wrap the DSL in {\"kind\":\"attack_dsl\",\"dsl\":{...}}.",
            )
        )
        return
    cases = dsl.get("cases")
    clusters = dsl.get("parameter_clusters")
    has_clusters = isinstance(clusters, list) and bool(clusters)
    if (not isinstance(cases, list) or not cases) and not has_clusters:
        diagnostics.append(
            _error(
                "ATTACK_DSL_CASES_MISSING",
                "$.dsl.cases",
                "attack_dsl requires a non-empty cases array unless parameter_clusters is present.",
                "Return at least one bounded case or a parameter_clusters array.",
            )
        )
        return
    if isinstance(cases, list) and len(cases) > 256:
        diagnostics.append(
            _error(
                "ATTACK_DSL_CASE_LIMIT",
                "$.dsl.cases",
                "A single model response may contain at most 256 cases.",
                "Use parameter_clusters, a cluster_seed, or a deterministic campaign profile for larger expansions.",
            )
        )
    for index, case in enumerate(cases if isinstance(cases, list) else []):
        if not isinstance(case, dict):
            diagnostics.append(
                _error(
                    "ATTACK_DSL_CASE_NOT_OBJECT",
                    f"$.dsl.cases[{index}]",
                    "Each DSL case must be an object.",
                    "Replace the entry with a case object.",
                )
            )
        elif not _nonempty_string(case.get("case_id")):
            diagnostics.append(
                _error(
                    "ATTACK_DSL_CASE_ID_MISSING",
                    f"$.dsl.cases[{index}].case_id",
                    "Each DSL case requires a non-empty case_id.",
                    "Add a stable filesystem-safe case_id.",
                )
            )
        else:
            _validate_case_id(case.get("case_id"), f"$.dsl.cases[{index}].case_id", diagnostics)
    if clusters is None:
        return
    if not isinstance(clusters, list):
        diagnostics.append(
            _error(
                "ATTACK_DSL_CLUSTERS_NOT_ARRAY",
                "$.dsl.parameter_clusters",
                "parameter_clusters must be an array when present.",
                "Use an array of parameter cluster definitions or omit the field.",
            )
        )
        return
    if len(clusters) > 4096:
        diagnostics.append(
            _error(
                "ATTACK_DSL_CLUSTER_LIMIT",
                "$.dsl.parameter_clusters",
                "A single model response may contain at most 4096 parameter cluster definitions.",
                "Split the expansion across more bases per cluster instead of more definitions.",
            )
        )
    bases = dsl.get("cluster_bases")
    if clusters and not isinstance(bases, dict):
        diagnostics.append(
            _error(
                "ATTACK_DSL_CLUSTER_BASES_MISSING",
                "$.dsl.cluster_bases",
                "parameter_clusters requires a cluster_bases object.",
                "Add cluster_bases mapping base ids to target/tool base cases.",
            )
        )
    elif isinstance(bases, dict) and len(bases) > 1024:
        diagnostics.append(
            _error(
                "ATTACK_DSL_CLUSTER_BASE_LIMIT",
                "$.dsl.cluster_bases",
                "A single model response may contain at most 1024 cluster bases.",
                "Reduce the number of named base geometries.",
            )
        )
    for index, cluster in enumerate(clusters):
        if not isinstance(cluster, dict):
            diagnostics.append(
                _error(
                    "ATTACK_DSL_CLUSTER_NOT_OBJECT",
                    f"$.dsl.parameter_clusters[{index}]",
                    "Each parameter cluster must be an object.",
                    "Replace the entry with a cluster definition object.",
                )
            )
            continue
        if not _nonempty_string(cluster.get("cluster_id")):
            diagnostics.append(
                _error(
                    "ATTACK_DSL_CLUSTER_ID_MISSING",
                    f"$.dsl.parameter_clusters[{index}].cluster_id",
                    "Each parameter cluster requires a non-empty cluster_id.",
                    "Add a stable filesystem-safe cluster_id.",
                )
            )
        else:
            _validate_case_id(
                cluster.get("cluster_id"),
                f"$.dsl.parameter_clusters[{index}].cluster_id",
                diagnostics,
            )
        if not _nonempty_string(cluster.get("type")):
            diagnostics.append(
                _error(
                    "ATTACK_DSL_CLUSTER_TYPE_MISSING",
                    f"$.dsl.parameter_clusters[{index}].type",
                    "Each parameter cluster requires a non-empty type.",
                    "Set type to one of the registered parameter cluster types.",
                )
            )


def _validate_flat_recipe(candidate: Mapping[str, Any], diagnostics: list[ContractDiagnostic]) -> None:
    recipe = candidate.get("recipe")
    if not isinstance(recipe, dict):
        diagnostics.append(
            _error(
                "FLAT_RECIPE_MISSING_RECIPE",
                "$.recipe",
                "flat_recipe requires a recipe object.",
                "Wrap the recipe in {\"kind\":\"flat_recipe\",\"recipe\":{...}}.",
            )
        )
        return
    for key in ("api", "case_id"):
        if not _nonempty_string(recipe.get(key)):
            diagnostics.append(
                _error(
                    f"FLAT_RECIPE_{key.upper()}_MISSING",
                    f"$.recipe.{key}",
                    f"flat_recipe.recipe requires a non-empty {key}.",
                    f"Set recipe.{key} to a supported value.",
                )
            )
    _validate_case_id(recipe.get("case_id"), "$.recipe.case_id", diagnostics)


def _validate_cluster_seed(candidate: Mapping[str, Any], diagnostics: list[ContractDiagnostic]) -> None:
    for key in ("cluster_id", "contact_path"):
        if not _nonempty_string(candidate.get(key)):
            diagnostics.append(
                _error(
                    f"CLUSTER_SEED_{key.upper()}_MISSING",
                    f"$.{key}",
                    f"cluster_seed requires a non-empty {key}.",
                    f"Set {key} using the source predicate represented by this seed.",
                )
            )
    if not isinstance(candidate.get("contact_value"), int | float) or isinstance(
        candidate.get("contact_value"), bool
    ):
        diagnostics.append(
            _error(
                "CLUSTER_SEED_CONTACT_VALUE_INVALID",
                "$.contact_value",
                "cluster_seed requires a numeric contact_value.",
                "Set contact_value to the base numeric predicate value.",
            )
        )
    for key in ("target", "tool"):
        if not isinstance(candidate.get(key), dict):
            diagnostics.append(
                _error(
                    f"CLUSTER_SEED_{key.upper()}_MISSING",
                    f"$.{key}",
                    f"cluster_seed requires a {key} object.",
                    f"Provide the base {key} body chain.",
                )
            )


def _validate_extension(candidate: Mapping[str, Any], diagnostics: list[ContractDiagnostic]) -> None:
    for key in ("api", "why_needed", "extension_summary"):
        if not _nonempty_string(candidate.get(key)):
            diagnostics.append(
                _error(
                    f"EXTENSION_{key.upper()}_MISSING",
                    f"$.{key}",
                    f"needs_harness_extension requires a non-empty {key}.",
                    f"Describe {key} concretely from the supplied API evidence.",
                )
            )
    required_types: tuple[tuple[str, type], ...] = (
        ("proposed_recipe_fields", dict),
        ("proposed_artifacts", list),
        ("validation_oracle", dict),
        ("minimum_smoke_case", dict),
        ("patch_plan", list),
    )
    for key, expected_type in required_types:
        if not isinstance(candidate.get(key), expected_type):
            diagnostics.append(
                _error(
                    f"EXTENSION_{key.upper()}_INVALID",
                    f"$.{key}",
                    f"needs_harness_extension requires {key} as {expected_type.__name__}.",
                    f"Return a concrete {key} value matching the output contract.",
                )
            )
    # Structured interface-design fields (interface_dsl_design subagent output).
    # They are optional at the transport layer; the fixed extension gate
    # decides whether they are required for the task type and validates their
    # semantics.
    optional_design_types: tuple[tuple[str, type], ...] = (
        ("interface_signature", dict),
        ("builder_requirements", list),
        ("archetype_match", dict),
        ("parameter_cluster_plan", list),
        ("complexity_plan", dict),
    )
    for key, expected_type in optional_design_types:
        if key in candidate and not isinstance(candidate.get(key), expected_type):
            diagnostics.append(
                _error(
                    f"EXTENSION_{key.upper()}_INVALID",
                    f"$.{key}",
                    f"needs_harness_extension design field {key} must be a {expected_type.__name__} when present.",
                    f"Return {key} as a {expected_type.__name__} matching the interface design contract.",
                )
            )


def _validate_api_plugin_candidate(
    candidate: Mapping[str, Any],
    diagnostics: list[ContractDiagnostic],
) -> None:
    for key in ("api", "description"):
        if not _nonempty_string(candidate.get(key)):
            diagnostics.append(
                _error(
                    f"API_PLUGIN_{key.upper()}_MISSING",
                    f"$.{key}",
                    f"api_plugin_candidate requires a non-empty {key}.",
                    f"Provide {key} from the host-bound API resolution evidence.",
                )
            )
    for key in (
        "adapter_spec",
        "recipe_schema",
        "smoke_recipe",
        "negative_recipe",
        "capability",
        "topotrack",
    ):
        if not isinstance(candidate.get(key), dict):
            diagnostics.append(
                _error(
                    f"API_PLUGIN_{key.upper()}_INVALID",
                    f"$.{key}",
                    f"api_plugin_candidate requires {key} as an object.",
                    "Return the complete fixed-archetype plugin candidate JSON.",
                )
            )


def _validate_visual_review_report(
    candidate: Mapping[str, Any],
    diagnostics: list[ContractDiagnostic],
) -> None:
    """Validate advisory vision-review output; it carries no execution authority."""

    unknown_top_level = sorted(str(key) for key in candidate if key not in VISUAL_REVIEW_TOP_LEVEL_FIELDS)
    if unknown_top_level:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_FIELDS_UNKNOWN",
                "$",
                f"visual_review_report contains forbidden or unknown fields: {unknown_top_level}.",
                "Use only kind, schema_version, case_reviews, and overall_notes_zh_cn.",
            )
        )
    authority_fields = sorted(str(key) for key in candidate if str(key) in VISUAL_REVIEW_AUTHORITY_FIELDS)
    if authority_fields:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_AUTHORITY_FIELD_FORBIDDEN",
                "$",
                f"visual_review_report must not carry execution-authority fields: {authority_fields}.",
                "Visual review is advisory only; remove approval/execution fields.",
            )
        )
    if candidate.get("schema_version") != 1:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_SCHEMA_VERSION_INVALID",
                "$.schema_version",
                "visual_review_report requires schema_version 1.",
                "Set schema_version to 1.",
                1,
            )
        )
    reviews = candidate.get("case_reviews")
    if not isinstance(reviews, list) or not reviews:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_CASES_MISSING",
                "$.case_reviews",
                "visual_review_report requires a non-empty case_reviews array.",
                "Return one review object per inspected case.",
            )
        )
        reviews = []
    elif len(reviews) > VISUAL_REVIEW_MAX_CASES:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_CASE_LIMIT",
                "$.case_reviews",
                f"A single visual review may contain at most {VISUAL_REVIEW_MAX_CASES} case reviews.",
                "Split the review across multiple bounded tasks.",
            )
        )
    for index, review in enumerate(reviews if isinstance(reviews, list) else []):
        path = f"$.case_reviews[{index}]"
        if not isinstance(review, dict):
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CASE_NOT_OBJECT",
                    path,
                    "Each case review must be an object.",
                    "Replace the entry with a case review object.",
                )
            )
            continue
        unknown_fields = sorted(str(key) for key in review if key not in VISUAL_REVIEW_CASE_FIELDS)
        if unknown_fields:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CASE_FIELDS_UNKNOWN",
                    path,
                    f"Case review contains forbidden or unknown fields: {unknown_fields}.",
                    "Use only case_id, geometry_plausibility, view_consistency, misuse_flags, confidence, "
                    "notes_zh_cn, fault_hint.",
                )
            )
        case_authority = sorted(str(key) for key in review if str(key) in VISUAL_REVIEW_AUTHORITY_FIELDS)
        if case_authority:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_AUTHORITY_FIELD_FORBIDDEN",
                    path,
                    f"Case review must not carry execution-authority fields: {case_authority}.",
                    "Visual review is advisory only; remove approval/execution fields.",
                )
            )
        if not _nonempty_string(review.get("case_id")):
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CASE_ID_MISSING",
                    f"{path}.case_id",
                    "Each case review requires a non-empty case_id.",
                    "Echo the case_id from the prompt's image listing.",
                )
            )
        else:
            _validate_case_id(review.get("case_id"), f"{path}.case_id", diagnostics)
        plausibility = review.get("geometry_plausibility")
        if plausibility not in VISUAL_REVIEW_PLAUSIBILITY:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_PLAUSIBILITY_INVALID",
                    f"{path}.geometry_plausibility",
                    "geometry_plausibility must be plausible, suspect, or implausible.",
                    "Choose one of the declared plausibility values.",
                    sorted(VISUAL_REVIEW_PLAUSIBILITY),
                )
            )
        consistency = review.get("view_consistency")
        if consistency not in VISUAL_REVIEW_CONSISTENCY:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CONSISTENCY_INVALID",
                    f"{path}.view_consistency",
                    "view_consistency must be consistent, inconsistent, or unclear.",
                    "Choose one of the declared consistency values.",
                    sorted(VISUAL_REVIEW_CONSISTENCY),
                )
            )
        flags = review.get("misuse_flags")
        if not isinstance(flags, list) or any(not isinstance(item, str) for item in flags):
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_FLAGS_INVALID",
                    f"{path}.misuse_flags",
                    "misuse_flags must be an array of declared flag strings (empty allowed).",
                    "Use an array of declared misuse flag strings.",
                    sorted(VISUAL_REVIEW_MISUSE_FLAGS),
                )
            )
        else:
            if len(flags) > VISUAL_REVIEW_MAX_FLAGS:
                diagnostics.append(
                    _error(
                        "VISUAL_REVIEW_FLAGS_LIMIT",
                        f"{path}.misuse_flags",
                        f"misuse_flags may contain at most {VISUAL_REVIEW_MAX_FLAGS} entries.",
                        "Keep only the most relevant flags.",
                    )
                )
            unknown_flags = sorted(item for item in flags if item not in VISUAL_REVIEW_MISUSE_FLAGS)
            if unknown_flags:
                diagnostics.append(
                    _error(
                        "VISUAL_REVIEW_FLAGS_UNKNOWN",
                        f"{path}.misuse_flags",
                        f"Unknown misuse flags: {unknown_flags}.",
                        "Use only the declared misuse flag values.",
                        sorted(VISUAL_REVIEW_MISUSE_FLAGS),
                    )
                )
        confidence = review.get("confidence")
        if (
            not isinstance(confidence, int | float)
            or isinstance(confidence, bool)
            or not 0.0 <= confidence <= 1.0
        ):
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CONFIDENCE_INVALID",
                    f"{path}.confidence",
                    "confidence must be a number between 0 and 1.",
                    "Return a bounded numeric confidence.",
                )
            )
        notes = review.get("notes_zh_cn")
        if not isinstance(notes, str) or len(notes) > VISUAL_REVIEW_MAX_CASE_NOTES_CHARS:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_CASE_NOTES_INVALID",
                    f"{path}.notes_zh_cn",
                    f"notes_zh_cn must be a string of at most {VISUAL_REVIEW_MAX_CASE_NOTES_CHARS} characters.",
                    "Keep the per-case Chinese note within the bound.",
                )
            )
        fault_hint = review.get("fault_hint")
        if fault_hint is not None and fault_hint not in VISUAL_REVIEW_FAULT_HINTS:
            diagnostics.append(
                _error(
                    "VISUAL_REVIEW_FAULT_HINT_INVALID",
                    f"{path}.fault_hint",
                    "fault_hint must be test_expectation, geometry, transport, tooling, or unclear when present.",
                    "Choose one of the declared fault hint values or omit the field.",
                    sorted(VISUAL_REVIEW_FAULT_HINTS),
                )
            )
    overall = candidate.get("overall_notes_zh_cn")
    if not isinstance(overall, str) or len(overall) > VISUAL_REVIEW_MAX_OVERALL_NOTES_CHARS:
        diagnostics.append(
            _error(
                "VISUAL_REVIEW_OVERALL_NOTES_INVALID",
                "$.overall_notes_zh_cn",
                f"overall_notes_zh_cn must be a string of at most {VISUAL_REVIEW_MAX_OVERALL_NOTES_CHARS} characters.",
                "Keep the overall Chinese note within the bound.",
            )
        )


def _schema_type_matches(value: Any, expected: str) -> bool:
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return False


def _validate_typed_args(
    args: Mapping[str, Any],
    schema: Any,
    diagnostics: list[ContractDiagnostic],
) -> None:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        diagnostics.append(
            _error(
                "CAMPAIGN_PROFILE_SCHEMA_INVALID",
                "$contract.allowed_campaign_profiles",
                "The selected campaign profile has no valid object args_schema.",
                "Regenerate the prompt pack from the canonical campaign profile registry.",
            )
        )
        return
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        diagnostics.append(
            _error(
                "CAMPAIGN_PROFILE_PROPERTIES_INVALID",
                "$contract.allowed_campaign_profiles",
                "Campaign args_schema.properties must be an object.",
                "Regenerate the prompt pack from the canonical campaign profile registry.",
            )
        )
        return
    required_raw = schema.get("required", [])
    required = required_raw if isinstance(required_raw, list) else []
    for key in required:
        if isinstance(key, str) and key not in args:
            diagnostics.append(
                _error(
                    "CAMPAIGN_ARG_REQUIRED",
                    f"$.args.{key}",
                    f"Campaign argument {key!r} is required.",
                    "Provide the required typed argument.",
                )
            )
    unknown = sorted(str(key) for key in args if key not in properties)
    if unknown:
        diagnostics.append(
            _error(
                "CAMPAIGN_ARGS_UNKNOWN",
                "$.args",
                f"Campaign args contain unknown keys: {unknown}.",
                "Use only keys declared by the selected profile args_schema.",
                sorted(properties),
            )
        )
    for key, value in args.items():
        field_schema = properties.get(key)
        if not isinstance(field_schema, dict):
            continue
        expected_type = field_schema.get("type")
        if not isinstance(expected_type, str) or not _schema_type_matches(value, expected_type):
            diagnostics.append(
                _error(
                    "CAMPAIGN_ARG_TYPE_INVALID",
                    f"$.args.{key}",
                    f"Campaign argument {key!r} must have type {expected_type!r}.",
                    "Return the argument using the exact declared JSON type.",
                )
            )
            continue
        enum = field_schema.get("enum")
        if isinstance(enum, list) and value not in enum:
            diagnostics.append(
                _error(
                    "CAMPAIGN_ARG_ENUM_INVALID",
                    f"$.args.{key}",
                    f"Campaign argument {key!r} is not an allowed enum value.",
                    "Choose one of the declared values.",
                    enum,
                )
            )
        if isinstance(value, int | float) and not isinstance(value, bool):
            minimum = field_schema.get("minimum")
            maximum = field_schema.get("maximum")
            if isinstance(minimum, int | float) and value < minimum:
                diagnostics.append(
                    _error(
                        "CAMPAIGN_ARG_BELOW_MINIMUM",
                        f"$.args.{key}",
                        f"Campaign argument {key!r} is below minimum {minimum}.",
                        "Use a value within the bounded profile range.",
                    )
                )
            if isinstance(maximum, int | float) and value > maximum:
                diagnostics.append(
                    _error(
                        "CAMPAIGN_ARG_ABOVE_MAXIMUM",
                        f"$.args.{key}",
                        f"Campaign argument {key!r} is above maximum {maximum}.",
                        "Use a value within the bounded profile range.",
                    )
                )


def _validate_campaign_request(
    candidate: Mapping[str, Any],
    diagnostics: list[ContractDiagnostic],
    allowed_campaign_profiles: Mapping[str, Any],
) -> None:
    allowed_top_level = {"kind", "profile_id", "args", "notes", "expected_artifacts"}
    unknown_top_level = sorted(str(key) for key in candidate if key not in allowed_top_level)
    if unknown_top_level:
        diagnostics.append(
            _error(
                "CAMPAIGN_REQUEST_FIELDS_UNKNOWN",
                "$",
                f"campaign_request contains forbidden or unknown fields: {unknown_top_level}.",
                "Use only kind, profile_id, args, notes, and expected_artifacts; never return command/tool paths.",
            )
        )
    profile_id = candidate.get("profile_id")
    if not _nonempty_string(profile_id):
        diagnostics.append(
            _error(
                "CAMPAIGN_PROFILE_ID_MISSING",
                "$.profile_id",
                "campaign_request requires a non-empty profile_id.",
                "Choose a profile_id declared in allowed_campaign_profiles.",
                sorted(allowed_campaign_profiles),
            )
        )
        return
    profile = allowed_campaign_profiles.get(str(profile_id))
    if not isinstance(profile, dict):
        diagnostics.append(
            _error(
                "CAMPAIGN_PROFILE_NOT_ALLOWED",
                "$.profile_id",
                f"Campaign profile {profile_id!r} is not allowed for this task.",
                "Choose a profile_id declared in allowed_campaign_profiles.",
                sorted(allowed_campaign_profiles),
            )
        )
        return
    forbidden_profile_fields = sorted(
        set(profile) & {"command", "commands", "tool", "runner", "dataset", "out"}
    )
    if forbidden_profile_fields:
        diagnostics.append(
            _error(
                "CAMPAIGN_PROFILE_EXECUTION_FIELD_FORBIDDEN",
                "$contract.allowed_campaign_profiles",
                f"Campaign profile metadata contains forbidden execution fields: {forbidden_profile_fields}.",
                "Regenerate the manifest from the typed campaign profile registry without command paths.",
            )
        )
    args = candidate.get("args")
    if not isinstance(args, dict):
        diagnostics.append(
            _error(
                "CAMPAIGN_ARGS_NOT_OBJECT",
                "$.args",
                "campaign_request requires args as an object.",
                "Return typed bounded args for the selected profile.",
            )
        )
        return
    forbidden_args = sorted(set(args) & {"command", "commands", "tool", "runner", "dataset", "out"})
    if forbidden_args:
        diagnostics.append(
            _error(
                "CAMPAIGN_ARGS_EXECUTION_FIELD_FORBIDDEN",
                "$.args",
                f"Campaign args contain forbidden execution fields: {forbidden_args}.",
                "Only deterministic typed profile arguments are accepted.",
            )
        )
    _validate_typed_args(args, profile.get("args_schema"), diagnostics)
    defaults = profile.get("defaults") if isinstance(profile.get("defaults"), dict) else {}
    effective_args = dict(defaults)
    effective_args.update(args)
    shard_count = effective_args.get("shard_count")
    shard_index = effective_args.get("shard_index")
    if (
        isinstance(shard_count, int)
        and not isinstance(shard_count, bool)
        and isinstance(shard_index, int)
        and not isinstance(shard_index, bool)
        and (shard_count <= 0 or shard_index < 0 or shard_index >= shard_count)
    ):
        diagnostics.append(
            _error(
                "CAMPAIGN_SHARD_RANGE_INVALID",
                "$.args.shard_index",
                "shard_index must satisfy 0 <= shard_index < shard_count.",
                "Choose a valid bounded shard index.",
            )
        )
    expected_artifacts = candidate.get("expected_artifacts")
    if expected_artifacts is not None and (
        not isinstance(expected_artifacts, list)
        or any(not isinstance(item, str) for item in expected_artifacts)
    ):
        diagnostics.append(
            _error(
                "CAMPAIGN_EXPECTED_ARTIFACTS_NOT_ARRAY",
                "$.expected_artifacts",
                "campaign_request.expected_artifacts must be an array of strings when present.",
                "Use an array of expected artifact labels or omit the field.",
            )
        )


def validate_candidate(
    candidate: Any,
    output_contract: Any,
    *,
    allowed_campaign_profiles: Mapping[str, Any] | None = None,
    secret_values: Iterable[str] = (),
) -> ContractReport:
    diagnostics: list[ContractDiagnostic] = []
    if not isinstance(output_contract, dict):
        diagnostics.append(
            _error(
                "OUTPUT_CONTRACT_NOT_OBJECT",
                "$contract",
                "The gateway task output_contract must be an object.",
                "Regenerate the model prompt pack.",
            )
        )
        return ContractReport(False, diagnostics=diagnostics)
    if output_contract.get("type") != "json_object":
        diagnostics.append(
            _error(
                "OUTPUT_CONTRACT_TYPE_INVALID",
                "$contract.type",
                "The gateway supports only type=json_object output contracts.",
                "Regenerate the task with a canonical JSON-object contract.",
                "json_object",
            )
        )
    if output_contract.get("kind_field", "kind") != "kind":
        diagnostics.append(
            _error(
                "OUTPUT_CONTRACT_KIND_FIELD_INVALID",
                "$contract.kind_field",
                "The canonical discriminator field must be kind.",
                "Set kind_field to kind.",
                "kind",
            )
        )
    allowed_raw = output_contract.get("allowed_kinds")
    allowed = [item for item in allowed_raw if isinstance(item, str)] if isinstance(allowed_raw, list) else []
    if (
        not allowed
        or not isinstance(allowed_raw, list)
        or len(allowed) != len(allowed_raw)
        or any(item not in SUPPORTED_KINDS for item in allowed)
    ):
        diagnostics.append(
            _error(
                "OUTPUT_CONTRACT_ALLOWED_KINDS_INVALID",
                "$contract.allowed_kinds",
                "allowed_kinds must be a non-empty array of supported kinds.",
                "Regenerate the model prompt pack with the canonical output kinds.",
                sorted(SUPPORTED_KINDS),
            )
        )
    if not isinstance(candidate, dict):
        diagnostics.append(
            _error(
                "MODEL_OUTPUT_NOT_OBJECT",
                "$",
                "Model output must be exactly one JSON object.",
                "Return one JSON object in choices[0].message.content with no wrapper text.",
            )
        )
        return ContractReport(False, diagnostics=diagnostics)

    secrets = tuple(secret for secret in secret_values if secret)
    if _contains_secret(candidate, secrets):
        diagnostics.append(
            _error(
                "SECRET_VALUE_DETECTED",
                "$",
                "The candidate contains a configured credential value and cannot be staged or promoted.",
                "Remove credentials and provider configuration from the output.",
            )
        )

    kind = candidate.get("kind")
    if not _nonempty_string(kind):
        diagnostics.append(
            _error(
                "MODEL_OUTPUT_KIND_MISSING",
                "$.kind",
                "Model output requires a non-empty kind field.",
                "Set kind to one of output_contract.allowed_kinds.",
                allowed,
            )
        )
        return ContractReport(False, diagnostics=diagnostics)
    kind = str(kind).strip()
    if kind not in allowed:
        diagnostics.append(
            _error(
                "MODEL_OUTPUT_KIND_NOT_ALLOWED",
                "$.kind",
                f"Output kind {kind!r} is not allowed for this task.",
                "Return one of output_contract.allowed_kinds.",
                allowed,
            )
        )
    if kind not in SUPPORTED_KINDS:
        return ContractReport(False, kind=kind, diagnostics=diagnostics)

    _validate_common(candidate, diagnostics)
    if kind == "attack_dsl":
        _validate_attack_dsl(candidate, diagnostics)
    elif kind == "flat_recipe":
        _validate_flat_recipe(candidate, diagnostics)
    elif kind == "cluster_seed":
        _validate_cluster_seed(candidate, diagnostics)
    elif kind == "needs_harness_extension":
        _validate_extension(candidate, diagnostics)
    elif kind == "campaign_request":
        _validate_campaign_request(candidate, diagnostics, allowed_campaign_profiles or {})
    elif kind == "api_plugin_candidate":
        _validate_api_plugin_candidate(candidate, diagnostics)
    elif kind == "visual_review_report":
        _validate_visual_review_report(candidate, diagnostics)
    return ContractReport(
        not any(item.severity == "error" for item in diagnostics),
        kind=kind,
        diagnostics=diagnostics,
    )


def response_schema_for_contract(output_contract: Mapping[str, Any]) -> dict[str, Any]:
    """Build a provider-portable minimal schema; fixed validation remains authoritative."""

    raw_allowed = output_contract.get("allowed_kinds")
    allowed = [item for item in raw_allowed if item in SUPPORTED_KINDS] if isinstance(raw_allowed, list) else []
    if not allowed:
        allowed = sorted(SUPPORTED_KINDS)
    return {
        "type": "object",
        "properties": {"kind": {"type": "string", "enum": allowed}},
        "required": ["kind"],
        "additionalProperties": True,
    }
