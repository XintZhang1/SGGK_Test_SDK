"""Append-only, hash-chained evidence ledger for one investigator session."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _redact(value: Any, secrets: tuple[str, ...]) -> Any:
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): _redact(item, secrets) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    text = value
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    kind: str
    source: str
    created_at: str
    payload: Any
    previous_sha256: str
    record_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source": self.source,
            "created_at": self.created_at,
            "payload": self.payload,
            "previous_sha256": self.previous_sha256,
            "record_sha256": self.record_sha256,
        }


class EvidenceLedger:
    def __init__(
        self,
        *,
        prefix: str,
        output_path: Path,
        secret_values: Iterable[str] = (),
        max_payload_bytes: int = 256_000,
    ) -> None:
        self.prefix = "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in prefix)[:32]
        self.output_path = output_path
        self.secret_values = tuple(value for value in secret_values if value)
        self.max_payload_bytes = max_payload_bytes
        self.records: list[EvidenceRecord] = []

    @property
    def evidence_ids(self) -> set[str]:
        return {record.evidence_id for record in self.records}

    def append(self, *, kind: str, source: str, payload: Any) -> EvidenceRecord:
        safe_payload = _redact(payload, self.secret_values)
        payload_bytes = _canonical(safe_payload)
        if len(payload_bytes) > self.max_payload_bytes:
            safe_payload = {
                "truncated": True,
                "original_bytes": len(payload_bytes),
                "sha256": hashlib.sha256(payload_bytes).hexdigest(),
                "preview": payload_bytes[: min(16_000, self.max_payload_bytes)].decode(
                    "utf-8", errors="replace"
                ),
            }
        evidence_id = f"ev_{self.prefix}_{len(self.records) + 1:04d}"
        created_at = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        previous = self.records[-1].record_sha256 if self.records else ""
        unsigned = {
            "evidence_id": evidence_id,
            "kind": kind,
            "source": source,
            "created_at": created_at,
            "payload": safe_payload,
            "previous_sha256": previous,
        }
        record = EvidenceRecord(
            evidence_id=evidence_id,
            kind=kind,
            source=source,
            created_at=created_at,
            payload=safe_payload,
            previous_sha256=previous,
            record_sha256=hashlib.sha256(_canonical(unsigned)).hexdigest(),
        )
        self.records.append(record)
        self.flush()
        return record

    def flush(self) -> None:
        payload = {
            "schema_version": 1,
            "append_only": True,
            "record_count": len(self.records),
            "head_sha256": self.records[-1].record_sha256 if self.records else "",
            "records": [record.as_dict() for record in self.records],
        }
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def prompt_view(self, *, max_chars: int = 120_000) -> list[dict[str, Any]]:
        view: list[dict[str, Any]] = []
        used = 0
        for record in self.records:
            item = {
                "evidence_id": record.evidence_id,
                "kind": record.kind,
                "source": record.source,
                "payload": record.payload,
            }
            size = len(json.dumps(item, ensure_ascii=False))
            if used + size > max_chars:
                view.append(
                    {
                        "evidence_id": "ledger_compacted",
                        "kind": "compaction",
                        "source": "host",
                        "payload": {
                            "omitted_records": len(self.records) - len(view),
                            "head_sha256": self.records[-1].record_sha256,
                        },
                    }
                )
                break
            view.append(item)
            used += size
        return view
