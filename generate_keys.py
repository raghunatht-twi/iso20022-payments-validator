# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "cryptography>=42.0,<46.0",
# ]
# ///
"""Generate an Ed25519 key pair for ISO 20022 message signing."""
from __future__ import annotations

import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


_BASE_DIR = Path(__file__).parent
_KEYS_DIR = _BASE_DIR / "keys"


def main() -> None:
    _KEYS_DIR.mkdir(exist_ok=True)
    private_path = _KEYS_DIR / "sender_private.pem"
    public_path  = _KEYS_DIR / "sender_public.pem"

    if private_path.exists():
        print(
            f"Error: {private_path} already exists.\n"
            "Delete it first if you intentionally want to rotate keys.",
            file=sys.stderr,
        )
        sys.exit(1)

    private_key = Ed25519PrivateKey.generate()

    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    private_path.chmod(0o600)

    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    print("Ed25519 key pair generated.")
    print(f"  Private key : {private_path}   (keep secret — never commit)")
    print(f"  Public key  : {public_path}    (safe to share with receivers)")
    print()
    print("Next steps:")
    print("  uv run sender_agent.py    — signs every message with the private key")
    print("  uv run receiver_agent.py  — verifies signatures using the public key")


if __name__ == "__main__":
    main()
