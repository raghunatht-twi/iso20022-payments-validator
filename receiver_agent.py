# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "kafka-python>=2.0,<3.0",
#   "lxml>=5.0,<6.0",
# ]
# ///
"""Receiver agent — one consumer thread per domain/message-set.

Processing guarantee: at-least-once Kafka delivery + idempotent SQLite write
= exactly-once processing semantics. Duplicate Kafka redeliveries and
re-sent messages are both detected via the processed_messages primary key.
"""
from __future__ import annotations

import json
import signal
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from kafka import KafkaConsumer, KafkaProducer
from lxml import etree


_BASE_DIR = Path(__file__).parent
SCHEMA_DIR = _BASE_DIR / "schema"
STATE_DB = _BASE_DIR / "state.db"

_BOOTSTRAP = "localhost:9092"
_TOPIC_PREFIX = "iso20022"
_DLQ_TOPIC = f"{_TOPIC_PREFIX}.dlq"
_CONSUMER_GROUP = "iso20022-receivers"
_POLL_MS = 1_000

_SAFE_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    huge_tree=False,
    load_dtd=False,
)

_shutdown = threading.Event()


def _topic(domain: str, msg_set: str) -> str:
    return f"{_TOPIC_PREFIX}.{domain}.{msg_set}"


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


def _run_receiver(
    domain: str,
    msg_set: str,
    schema: etree.XMLSchema | None,
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
        enable_auto_commit=False,  # manual commit — ensures at-least-once on crash
    )
    print(f"  {label} listening on {topic}")

    try:
        while not _shutdown.is_set():
            batch = consumer.poll(timeout_ms=_POLL_MS)
            for records in batch.values():
                for record in records:
                    payload = record.value
                    msg_id = payload.get("message_id", "")
                    file_name = payload.get("file_name", "")
                    xml_content = payload.get("xml_content", "")

                    if _is_already_processed(conn, msg_id):
                        # Message seen before — record the duplicate and move on
                        print(f"  {label} DUPLICATE  {file_name}")
                        _record_duplicate(conn, msg_id, domain, msg_set, file_name)
                        consumer.commit()
                        continue

                    if schema is None:
                        valid, error = False, "Schema unavailable"
                    else:
                        valid, error = _validate(xml_content, schema)

                    if valid:
                        print(f"  {label} PASS       {file_name}")
                        _record_processed(
                            conn, msg_id, domain, msg_set, file_name,
                            "pass", None, None,
                        )
                    else:
                        print(f"  {label} FAIL       {file_name}  — {error[:100]}")
                        dlq_payload = {**payload, "validation_error": error}
                        producer.send(
                            _DLQ_TOPIC,
                            key=msg_id.encode(),
                            value=json.dumps(dlq_payload).encode(),
                        )
                        _record_processed(
                            conn, msg_id, domain, msg_set, file_name,
                            "fail", error, _DLQ_TOPIC,
                        )

                    # Commit offset only after DB write — guarantees idempotency on crash
                    consumer.commit()
    finally:
        consumer.close()
        conn.close()


def main() -> None:
    if not STATE_DB.exists():
        print(
            f"Error: {STATE_DB} not found. Run sender_agent.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    signal.signal(signal.SIGINT, lambda *_: _shutdown.set())
    signal.signal(signal.SIGTERM, lambda *_: _shutdown.set())

    schemas = _discover_schemas()
    if not schemas:
        print(f"No XSD schemas found under {SCHEMA_DIR}", file=sys.stderr)
        sys.exit(1)

    producer = KafkaProducer(
        bootstrap_servers=_BOOTSTRAP,
        acks="all",
        retries=5,
    )

    print(f"Starting {len(schemas)} receiver agent(s) ...\n")
    threads = [
        threading.Thread(
            target=_run_receiver,
            args=(domain, msg_set, _load_schema(xsd_path), producer),
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
