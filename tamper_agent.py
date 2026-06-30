# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=42.0,<46.0",
#   "kafka-python>=2.0,<3.0",
# ]
# ///
"""Tamper agent — publishes XML messages whose content has been modified after signing."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kafka import KafkaProducer


_BASE_DIR      = Path(__file__).parent
_TEST_DATA_DIR = _BASE_DIR / "test_data"
_SCHEMA_DIR    = _BASE_DIR / "schema"
_KEYS_DIR      = _BASE_DIR / "keys"

_BOOTSTRAP    = "localhost:9092"
_TOPIC_PREFIX = "iso20022"

_DEFAULT_COUNT = 3


def _load_private_key() -> Ed25519PrivateKey:
    key_path = _KEYS_DIR / "sender_private.pem"
    if not key_path.exists():
        print(f"Error: {key_path} not found.\nRun:  uv run generate_keys.py", file=sys.stderr)
        sys.exit(1)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"Error: {key_path} is not an Ed25519 private key.", file=sys.stderr)
        sys.exit(1)
    return key


def _topic(domain: str, msg_set: str) -> str:
    return f"{_TOPIC_PREFIX}.{domain}.{msg_set}"


def _tampered_message_id(domain: str, msg_set: str, xml_bytes: bytes) -> str:
    original_hash = hashlib.sha256(f"{domain}.{msg_set}:".encode() + xml_bytes).hexdigest()
    return f"tampered-{original_hash[:48]}"


def _inject_tamper(xml: str) -> str:
    def multiply_amount(m: re.Match) -> str:
        tag, val, close = m.group(1), m.group(2), m.group(3)
        try:
            new_val = f"{float(val) * 10:.2f}"
        except ValueError:
            new_val = val
        return f"{tag}{new_val}{close}"

    tampered = re.sub(
        r'(<[^>]*Amt[^>]*>)([\d.]+)(</[^>]*Amt[^>]*>)',
        multiply_amount,
        xml,
        count=1,
    )

    if tampered == xml:
        tampered = re.sub(r'([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)', r'XXXXXXXX', xml, count=1)

    if tampered == xml:
        tampered = xml.rstrip() + "<!-- TAMPERED -->"

    return tampered


def _spread_files(
    by_set: dict[str, list[tuple[str, str, Path]]], count: int
) -> list[tuple[str, str, Path]]:
    selected: list[tuple[str, str, Path]] = []
    sets = list(by_set.values())
    i = 0
    while len(selected) < count and sets:
        bucket = sets[i % len(sets)]
        selected.append(bucket[0])
        sets[i % len(sets)] = bucket[1:]
        if not sets[i % len(sets)]:
            sets.pop(i % len(sets))
        else:
            i += 1
    return selected[:count]


def _discover_files(count: int) -> list[tuple[str, str, Path]]:
    all_files: list[tuple[str, str, Path]] = []
    for xml_path in sorted(_TEST_DATA_DIR.rglob("*.xml")):
        parts = xml_path.relative_to(_TEST_DATA_DIR).parts
        if len(parts) == 3:
            all_files.append((parts[0], parts[1], xml_path))

    if not all_files:
        return []

    by_set: dict[str, list[tuple[str, str, Path]]] = {}
    for domain, msg_set, path in all_files:
        by_set.setdefault(f"{domain}.{msg_set}", []).append((domain, msg_set, path))

    return _spread_files(by_set, count)


def _schema_name(domain: str, msg_set: str) -> str:
    xsd_files = sorted((_SCHEMA_DIR / domain / msg_set).glob("*.xsd"))
    return xsd_files[0].name if xsd_files else "unknown.xsd"


def main() -> None:
    count = _DEFAULT_COUNT
    if len(sys.argv) > 1:
        try:
            count = int(sys.argv[1])
        except ValueError:
            print("Usage: uv run tamper_agent.py [count]", file=sys.stderr)
            sys.exit(1)

    private_key = _load_private_key()
    files = _discover_files(count)
    if not files:
        print(f"No XML test files found under {_TEST_DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    producer = KafkaProducer(
        bootstrap_servers=_BOOTSTRAP,
        key_serializer=str.encode,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks="all",
        retries=5,
    )

    print(f"\nPublishing {len(files)} tampered message(s):\n")

    for domain, msg_set, xml_path in files:
        original_bytes = xml_path.read_bytes()
        original_xml   = original_bytes.decode("utf-8", errors="replace")

        signature    = base64.b64encode(private_key.sign(original_bytes)).decode()
        tampered_xml = _inject_tamper(original_xml)

        msg_id    = _tampered_message_id(domain, msg_set, original_bytes)
        topic     = _topic(domain, msg_set)
        now       = datetime.now(timezone.utc).isoformat()

        payload = {
            "message_id":  msg_id,
            "domain":      domain,
            "message_set": msg_set,
            "schema_name": _schema_name(domain, msg_set),
            "file_name":   f"TAMPERED-{xml_path.name}",
            "sent_at":     now,
            "xml_content": tampered_xml,   # ← modified after signing
            "signature":   signature,      # ← valid signature for ORIGINAL bytes
        }
        producer.send(topic, key=msg_id, value=payload)

        changed = original_xml != tampered_xml
        print(f"  → {topic:<32}  TAMPERED-{xml_path.name}  (content changed: {changed})")

    producer.flush()
    producer.close()

    print(f"""
{len(files)} tampered message(s) sent.

When receiver_agent.py runs, each will produce:
  [pain.xxx] TAMPERED   TAMPERED-<file>  — signature verification failed

Then check reconciliation_report.py — the "Tampered Message Log" section
and the "Tampered Messages by Schema" DuckDB analytics will show these entries.

Inspect state.db directly:
  sqlite3 state.db "SELECT file_name, message_set, detected_at FROM tampered_messages;"
""")


if __name__ == "__main__":
    main()
