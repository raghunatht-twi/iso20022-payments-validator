# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=42.0,<46.0",
#   "kafka-python>=2.0,<3.0",
# ]
# ///
"""Sender agent — publishes ISO 20022 test messages to per-domain Kafka topics.

Each message is signed with the Ed25519 private key from keys/sender_private.pem.
The receiver verifies the signature and routes tampered messages to iso20022.tampered.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from kafka import KafkaProducer
from kafka.admin import KafkaAdminClient, NewTopic


_BASE_DIR    = Path(__file__).parent
TEST_DATA_DIR = _BASE_DIR / "test_data"
SCHEMA_DIR   = _BASE_DIR / "schema"
STATE_DB     = _BASE_DIR / "state.db"
_KEYS_DIR    = _BASE_DIR / "keys"

_BOOTSTRAP       = "localhost:9092"
_TOPIC_PREFIX    = "iso20022"
_DLQ_TOPIC       = f"{_TOPIC_PREFIX}.dlq"
_TAMPERED_TOPIC  = f"{_TOPIC_PREFIX}.tampered"


def _load_private_key() -> Ed25519PrivateKey:
    key_path = _KEYS_DIR / "sender_private.pem"
    if not key_path.exists():
        print(
            f"Error: {key_path} not found.\n"
            "Run:  uv run generate_keys.py",
            file=sys.stderr,
        )
        sys.exit(1)
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        print(f"Error: {key_path} does not contain an Ed25519 private key.", file=sys.stderr)
        sys.exit(1)
    return key


def _sign(private_key: Ed25519PrivateKey, data: bytes) -> str:
    return base64.b64encode(private_key.sign(data)).decode()


def _message_id(domain: str, msg_set: str, xml_bytes: bytes) -> str:
    return hashlib.sha256(f"{domain}.{msg_set}:".encode() + xml_bytes).hexdigest()


def _topic(domain: str, msg_set: str) -> str:
    return f"{_TOPIC_PREFIX}.{domain}.{msg_set}"


def _init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sent_messages (
            message_id   TEXT PRIMARY KEY,
            domain       TEXT NOT NULL,
            message_set  TEXT NOT NULL,
            file_name    TEXT NOT NULL,
            schema_name  TEXT NOT NULL,
            kafka_topic  TEXT NOT NULL,
            sent_at      TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id        TEXT PRIMARY KEY,
            domain            TEXT NOT NULL,
            message_set       TEXT NOT NULL,
            file_name         TEXT NOT NULL,
            validation_status TEXT NOT NULL,
            processed_at      TEXT NOT NULL,
            error_detail      TEXT,
            dlq_topic         TEXT
        );
        CREATE TABLE IF NOT EXISTS duplicate_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id   TEXT NOT NULL,
            domain       TEXT NOT NULL,
            message_set  TEXT NOT NULL,
            file_name    TEXT NOT NULL,
            detected_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tampered_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id      TEXT NOT NULL,
            domain          TEXT NOT NULL,
            message_set     TEXT NOT NULL,
            file_name       TEXT NOT NULL,
            detected_at     TEXT NOT NULL,
            tampered_topic  TEXT NOT NULL
        );
    """)
    conn.commit()


def _discover_files() -> list[tuple[str, str, Path]]:
    results = []
    for xml_path in sorted(TEST_DATA_DIR.rglob("*.xml")):
        parts = xml_path.relative_to(TEST_DATA_DIR).parts
        if len(parts) == 3:
            results.append((parts[0], parts[1], xml_path))
    return results


def _schema_name(domain: str, msg_set: str) -> str:
    xsd_files = sorted((SCHEMA_DIR / domain / msg_set).glob("*.xsd"))
    return xsd_files[0].name if xsd_files else "unknown.xsd"


def _ensure_topics(topics: set[str]) -> None:
    admin = KafkaAdminClient(bootstrap_servers=_BOOTSTRAP)
    existing = set(admin.list_topics())
    to_create = [
        NewTopic(t, num_partitions=3, replication_factor=1)
        for t in topics
        if t not in existing
    ]
    if to_create:
        admin.create_topics(to_create, validate_only=False)
        for t in to_create:
            print(f"  Created topic: {t.name}")
    admin.close()


def main() -> None:
    private_key = _load_private_key()
    print("Private key loaded — messages will be signed with Ed25519.")

    files = _discover_files()
    if not files:
        print(f"No XML test files found under {TEST_DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(STATE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    _init_db(conn)

    all_topics = {_topic(d, m) for d, m, _ in files} | {_DLQ_TOPIC, _TAMPERED_TOPIC}
    print(f"Ensuring {len(all_topics)} Kafka topics exist ...")
    _ensure_topics(all_topics)

    producer = KafkaProducer(
        bootstrap_servers=_BOOTSTRAP,
        key_serializer=str.encode,
        value_serializer=lambda v: json.dumps(v).encode(),
        acks="all",
        retries=5,
    )

    sent = skipped = 0
    for domain, msg_set, xml_path in files:
        xml_bytes = xml_path.read_bytes()
        msg_id    = _message_id(domain, msg_set, xml_bytes)
        topic     = _topic(domain, msg_set)

        already_sent = conn.execute(
            "SELECT 1 FROM sent_messages WHERE message_id = ?", (msg_id,)
        ).fetchone()

        if already_sent:
            print(f"  SKIP   {xml_path.relative_to(_BASE_DIR)}  (already in sent log)")
            skipped += 1
            continue

        signature   = _sign(private_key, xml_bytes)
        schema_nm   = _schema_name(domain, msg_set)
        now         = datetime.now(timezone.utc).isoformat()
        payload = {
            "message_id":  msg_id,
            "domain":      domain,
            "message_set": msg_set,
            "schema_name": schema_nm,
            "file_name":   xml_path.name,
            "sent_at":     now,
            "xml_content": xml_bytes.decode("utf-8", errors="replace"),
            "signature":   signature,
        }
        producer.send(topic, key=msg_id, value=payload)
        conn.execute(
            "INSERT INTO sent_messages VALUES (?,?,?,?,?,?,?)",
            (msg_id, domain, msg_set, xml_path.name, schema_nm, topic, now),
        )
        conn.commit()
        print(f"  SENT   {topic:<32}  {xml_path.name}  [signed]")
        sent += 1

    producer.flush()
    producer.close()
    conn.close()

    print(f"\nSent: {sent}   Already-sent (skipped): {skipped}")
    print("Run:  uv run receiver_agent.py")


if __name__ == "__main__":
    main()
