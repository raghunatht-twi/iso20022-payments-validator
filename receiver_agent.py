# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=42.0,<46.0",
#   "kafka-python>=2.0,<3.0",
#   "lxml>=5.0,<6.0",
# ]
# ///
"""Receiver agent — one consumer thread per domain/message-set."""
from __future__ import annotations

import base64
import binascii
import json
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from kafka import KafkaConsumer, KafkaProducer
from lxml import etree


_BASE_DIR  = Path(__file__).parent
SCHEMA_DIR = _BASE_DIR / "schema"
STATE_DB   = _BASE_DIR / "state.db"
_KEYS_DIR  = _BASE_DIR / "keys"

_BOOTSTRAP      = "localhost:9092"
_TOPIC_PREFIX   = "iso20022"
_DLQ_TOPIC      = f"{_TOPIC_PREFIX}.dlq"
_TAMPERED_TOPIC = f"{_TOPIC_PREFIX}.tampered"
_CONSUMER_GROUP = "iso20022-receivers"
_POLL_MS        = 1_000

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)

_shutdown = threading.Event()


def _topic(domain: str, msg_set: str) -> str:
    return f"{_TOPIC_PREFIX}.{domain}.{msg_set}"


def _load_public_key() -> Ed25519PublicKey:
    key_path = _KEYS_DIR / "sender_public.pem"
    if not key_path.exists():
        print(f"Error: {key_path} not found.\nRun:  uv run generate_keys.py", file=sys.stderr)
        sys.exit(1)
    key = serialization.load_pem_public_key(key_path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        print(f"Error: {key_path} does not contain an Ed25519 public key.", file=sys.stderr)
        sys.exit(1)
    return key


def _verify_signature(public_key: Ed25519PublicKey, xml_content: str, signature_b64: str) -> bool:
    try:
        public_key.verify(
            base64.b64decode(signature_b64),
            xml_content.encode("utf-8", errors="replace"),
        )
        return True
    except (InvalidSignature, binascii.Error):
        return False


def _discover_schemas() -> list[tuple[str, str, Path]]:
    results = []
    for xsd_path in sorted(SCHEMA_DIR.rglob("*.xsd")):
        parts = xsd_path.relative_to(SCHEMA_DIR).parts
        if len(parts) == 3:
            results.append((parts[0], parts[1], xsd_path))
    return results


def _load_schema(xsd_path: Path) -> etree.XMLSchema | None:
    try:
        doc = etree.parse(str(xsd_path), _SAFE_PARSER)
        return etree.XMLSchema(doc)
    except (etree.XMLSyntaxError, etree.XMLSchemaParseError) as exc:
        print(f"  Warning: could not load {xsd_path.name}: {exc}", file=sys.stderr)
        return None


def _validate(xml_content: str, schema: etree.XMLSchema) -> tuple[bool, str]:
    try:
        element = etree.fromstring(xml_content.encode(), parser=_SAFE_PARSER)
        if schema.validate(element):
            return True, ""
        errors = "; ".join(str(e.message) for e in schema.error_log)
        return False, errors[:500]
    except etree.XMLSyntaxError as exc:
        return False, f"XML syntax error: {exc}"


def _open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(STATE_DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _is_already_processed(conn: sqlite3.Connection, msg_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?", (msg_id,)
    ).fetchone() is not None


def _record_processed(
    conn: sqlite3.Connection,
    msg_id: str,
    domain: str,
    msg_set: str,
    file_name: str,
    status: str,
    error: str | None,
    dlq_topic: str | None,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO processed_messages VALUES (?,?,?,?,?,?,?,?)",
        (msg_id, domain, msg_set, file_name, status,
         datetime.now(timezone.utc).isoformat(), error, dlq_topic),
    )
    conn.commit()


def _record_duplicate(
    conn: sqlite3.Connection,
    msg_id: str,
    domain: str,
    msg_set: str,
    file_name: str,
) -> None:
    conn.execute(
        "INSERT INTO duplicate_events (message_id, domain, message_set, file_name, detected_at)"
        " VALUES (?,?,?,?,?)",
        (msg_id, domain, msg_set, file_name, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def _record_tampered(
    conn: sqlite3.Connection,
    msg_id: str,
    domain: str,
    msg_set: str,
    file_name: str,
) -> None:
    conn.execute(
        "INSERT INTO tampered_messages"
        " (message_id, domain, message_set, file_name, detected_at, tampered_topic)"
        " VALUES (?,?,?,?,?,?)",
        (msg_id, domain, msg_set, file_name,
         datetime.now(timezone.utc).isoformat(), _TAMPERED_TOPIC),
    )
    conn.commit()


def _process_message(
    conn: sqlite3.Connection,
    consumer: KafkaConsumer,
    producer: KafkaProducer,
    public_key: Ed25519PublicKey,
    schema: etree.XMLSchema | None,
    domain: str,
    msg_set: str,
    label: str,
    payload: dict,
) -> None:
    msg_id      = payload.get("message_id", "")
    file_name   = payload.get("file_name", "")
    xml_content = payload.get("xml_content", "")
    signature   = payload.get("signature", "")

    if _is_already_processed(conn, msg_id):
        print(f"  {label} DUPLICATE  {file_name}")
        _record_duplicate(conn, msg_id, domain, msg_set, file_name)
        consumer.commit()
        return

    if not signature:
        print(f"  {label} TAMPERED   {file_name}  — no signature present")
        producer.send(
            _TAMPERED_TOPIC,
            key=msg_id.encode(),
            value=json.dumps({**payload, "tamper_reason": "missing signature"}).encode(),
        )
        _record_tampered(conn, msg_id, domain, msg_set, file_name)
        consumer.commit()
        return

    if not _verify_signature(public_key, xml_content, signature):
        print(f"  {label} TAMPERED   {file_name}  — signature verification failed")
        producer.send(
            _TAMPERED_TOPIC,
            key=msg_id.encode(),
            value=json.dumps({**payload, "tamper_reason": "signature mismatch"}).encode(),
        )
        _record_tampered(conn, msg_id, domain, msg_set, file_name)
        consumer.commit()
        return

    valid, error = _validate(xml_content, schema) if schema else (False, "Schema unavailable")

    if valid:
        print(f"  {label} PASS       {file_name}")
        _record_processed(conn, msg_id, domain, msg_set, file_name, "pass", None, None)
    else:
        print(f"  {label} FAIL       {file_name}  — {error[:100]}")
        producer.send(
            _DLQ_TOPIC,
            key=msg_id.encode(),
            value=json.dumps({**payload, "validation_error": error}).encode(),
        )
        _record_processed(conn, msg_id, domain, msg_set, file_name, "fail", error, _DLQ_TOPIC)

    # Commit offset only after DB write — guarantees idempotency on crash
    consumer.commit()


def _run_receiver(
    domain: str,
    msg_set: str,
    schema: etree.XMLSchema | None,
    public_key: Ed25519PublicKey,
    producer: KafkaProducer,
) -> None:
    topic = _topic(domain, msg_set)
    label = f"[{domain}.{msg_set}]"
    conn = _open_db()
    consumer = KafkaConsumer(
        topic,
        bootstrap_servers=_BOOTSTRAP,
        group_id=_CONSUMER_GROUP,
        value_deserializer=lambda b: json.loads(b.decode()),
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    print(f"  {label} listening on {topic}")

    try:
        while not _shutdown.is_set():
            batch = consumer.poll(timeout_ms=_POLL_MS)
            for records in batch.values():
                for record in records:
                    _process_message(
                        conn, consumer, producer, public_key, schema,
                        domain, msg_set, label, record.value,
                    )
    finally:
        consumer.close()
        conn.close()


def main() -> None:
    if not STATE_DB.exists():
        print(f"Error: {STATE_DB} not found. Run sender_agent.py first.", file=sys.stderr)
        sys.exit(1)

    public_key = _load_public_key()
    print("Public key loaded — all messages will be signature-verified.")

    signal.signal(signal.SIGINT,  lambda *_: _shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())

    schemas = _discover_schemas()
    if not schemas:
        print(f"No XSD schemas found under {SCHEMA_DIR}", file=sys.stderr)
        sys.exit(1)

    producer = KafkaProducer(bootstrap_servers=_BOOTSTRAP, acks="all", retries=5)

    print(f"Starting {len(schemas)} receiver agent(s) ...\n")
    threads = [
        threading.Thread(
            target=_run_receiver,
            args=(domain, msg_set, _load_schema(xsd_path), public_key, producer),
            name=f"receiver-{domain}-{msg_set}",
            daemon=True,
        )
        for domain, msg_set, xsd_path in schemas
    ]
    for t in threads:
        t.start()

    print("\nAll receivers running. Press Ctrl+C to stop.\n")
    _shutdown.wait()

    print("\nShutting down ...")
    for t in threads:
        t.join(timeout=10)
    producer.flush()
    producer.close()
    print("Done. Run:  uv run reconciliation_report.py")


if __name__ == "__main__":
    main()
