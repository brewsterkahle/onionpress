"""Database secrets for OnionPress (~/.onionpress/secrets).

Random per install, generated once by ensure_secrets() and read by
load_secrets(); never defaults, never committed or logged.
"""

import os
import secrets
from dataclasses import dataclass


@dataclass
class Secrets:
    """Database secrets for docker compose."""
    wordpress_db_password: str
    mysql_password: str
    mysql_root_password: str

    def as_env(self) -> dict:
        """Return as environment variables for docker compose."""
        return {
            "WORDPRESS_DB_PASSWORD": self.wordpress_db_password,
            "MYSQL_PASSWORD": self.mysql_password,
            "MYSQL_ROOT_PASSWORD": self.mysql_root_password,
        }


def _generate_password(length: int = 32) -> str:
    """Generate a random alphanumeric password."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_secrets(path: str) -> Secrets:
    """Load secrets from a shell-style secrets file.

    Format: KEY='value' (single-quoted) or KEY=value (bare).
    """
    values = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                # Strip single quotes
                if val.startswith("'") and val.endswith("'"):
                    val = val[1:-1]
                values[key] = val

    return Secrets(
        wordpress_db_password=values.get("WORDPRESS_DB_PASSWORD", ""),
        mysql_password=values.get("MYSQL_PASSWORD", ""),
        mysql_root_password=values.get("MYSQL_ROOT_PASSWORD", ""),
    )


def ensure_secrets(path: str) -> Secrets:
    """Generate secrets file if missing, then load and return Secrets.

    Sets file permissions to 600 (owner-only read/write).
    """
    if not os.path.exists(path):
        wp_pass = _generate_password()
        root_pass = _generate_password()

        # Create with restricted permissions
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("# Database passwords - auto-generated\n")
            f.write("# DO NOT SHARE THESE PASSWORDS\n")
            f.write(f"WORDPRESS_DB_PASSWORD='{wp_pass}'\n")
            f.write(f"MYSQL_PASSWORD='{wp_pass}'\n")
            f.write(f"MYSQL_ROOT_PASSWORD='{root_pass}'\n")

    return load_secrets(path)
