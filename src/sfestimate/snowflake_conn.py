"""Connect to Snowflake with key-pair authentication, configured from .env.

Snowflake requires MFA for password sign-in by people, so scripts use a key pair:
`sfestimate keygen` makes one, and you register the public key on your user once.
"""

from __future__ import annotations

import os
from pathlib import Path

KEY_DIR = Path("keys")
PRIVATE_KEY = KEY_DIR / "rsa_key.p8"
PUBLIC_KEY = KEY_DIR / "rsa_key.pub"


def _need(pkg: str):
    raise SystemExit(
        f"This command needs the optional Snowflake extras ({pkg} is missing).\n"
        '  pip install -e ".[snowflake]"'
    )


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def keygen(force: bool = False) -> str:
    """Create an RSA key pair and return the SQL that registers it on the user."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except ImportError:
        _need("cryptography")
    load_env()
    if PRIVATE_KEY.exists() and not force:
        raise SystemExit(f"{PRIVATE_KEY} already exists. Use --force to replace it.")
    KEY_DIR.mkdir(exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    PRIVATE_KEY.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    PRIVATE_KEY.chmod(0o600)
    pub_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    PUBLIC_KEY.write_text(pub_pem)
    body = "".join(l for l in pub_pem.splitlines() if "PUBLIC KEY" not in l)
    user = os.getenv("SNOWFLAKE_USER") or "<YOUR_USERNAME>"
    return f"ALTER USER {user} SET RSA_PUBLIC_KEY='{body}';"


def connect(warehouse: str | None = None):
    try:
        import snowflake.connector
        from cryptography.hazmat.primitives import serialization
    except ImportError:
        _need("snowflake-connector-python")
    load_env()
    account = os.getenv("SNOWFLAKE_ACCOUNT")
    user = os.getenv("SNOWFLAKE_USER")
    if not account or not user:
        raise SystemExit("Set SNOWFLAKE_ACCOUNT and SNOWFLAKE_USER in .env (copy .env.example).")
    key_path = Path(os.getenv("SNOWFLAKE_PRIVATE_KEY_PATH") or PRIVATE_KEY)
    if not key_path.exists():
        raise SystemExit(f"No private key at {key_path}. Run `sfestimate keygen` first.")
    pkey = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    der = pkey.private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return snowflake.connector.connect(
        account=account,
        user=user,
        private_key=der,
        role=os.getenv("SNOWFLAKE_ROLE") or None,
        warehouse=warehouse or os.getenv("SNOWFLAKE_WAREHOUSE") or None,
        application="snowflake-migration-estimator",
        session_parameters={"QUERY_TAG": "sfestimate"},
    )


def check() -> str:
    with connect() as conn:
        row = conn.cursor().execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), CURRENT_REGION(), CURRENT_VERSION()"
        ).fetchone()
    return (f"Connected. account={row[0]} user={row[1]} role={row[2]} "
            f"region={row[3]} version={row[4]}")
