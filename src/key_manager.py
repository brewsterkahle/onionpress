#!/usr/bin/env python3
"""
Key Management for OnionPress
Extract and write Tor v3 Ed25519 private keys to/from the onionpress-tor container.
"""

import subprocess


def extract_private_key():
    """
    Extract the Tor v3 private key from the running container.
    Returns the raw key bytes (64 bytes).
    """
    try:
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-tor', 'cat',
             '/var/lib/tor/hidden_service/wordpress/hs_ed25519_secret_key'],
            capture_output=True,
            timeout=10
        )

        if result.returncode != 0:
            raise Exception("Could not read Tor private key from container")

        # The key file format is:
        # "== ed25519v1-secret: type0 ==" (32-byte header)
        # followed by 64 bytes of key data
        key_data = result.stdout

        expected_header = b'== ed25519v1-secret: type0 =='
        if len(key_data) == 96:
            header = key_data[:32]
            if not header.startswith(expected_header):
                raise Exception(
                    "Key file header mismatch: expected ed25519v1-secret header. "
                    "File may be corrupt or in an unsupported format."
                )
            return key_data[32:]
        elif len(key_data) == 64:
            # Already just the key (no header)
            return key_data
        else:
            raise Exception(f"Unexpected key file size: {len(key_data)} bytes")

    except Exception as e:
        raise Exception(f"Failed to extract private key: {e}")


def write_private_key(key_bytes):
    """
    Write a new private key to the Tor container using secure Python file I/O.
    This will change your onion address!
    """
    import tempfile
    import os

    try:
        # The Tor key file format includes a 32-byte header
        header = b'== ed25519v1-secret: type0 =='
        header = header.ljust(32, b'\x00')
        full_key = header + key_bytes

        # Write to a temporary file with restricted permissions
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_file:
            temp_path = temp_file.name
            os.chmod(temp_path, 0o600)
            temp_file.write(full_key)

        try:
            # Copy file to container using docker cp
            result = subprocess.run(
                ['docker', 'cp', temp_path,
                 'onionpress-tor:/var/lib/tor/hidden_service/wordpress/hs_ed25519_secret_key'],
                capture_output=True,
                timeout=10
            )

            if result.returncode != 0:
                raise Exception(f"Failed to copy key to container: {result.stderr.decode()}")

            # Set proper permissions inside container
            result = subprocess.run(
                ['docker', 'exec', 'onionpress-tor', 'chmod', '600',
                 '/var/lib/tor/hidden_service/wordpress/hs_ed25519_secret_key'],
                capture_output=True,
                timeout=10
            )

            if result.returncode != 0:
                raise Exception(f"Failed to set key permissions: {result.stderr.decode()}")

            # Restart Tor container to regenerate public key and hostname
            subprocess.run(['docker', 'restart', 'onionpress-tor'],
                         capture_output=True, timeout=30)

            return True

        finally:
            # Securely delete temporary file (multi-pass overwrite)
            if os.path.exists(temp_path):
                file_len = len(full_key)
                with open(temp_path, 'wb') as f:
                    f.write(os.urandom(file_len))
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, 'wb') as f:
                    f.write(b'\x00' * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                with open(temp_path, 'wb') as f:
                    f.write(b'\xff' * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                os.unlink(temp_path)

    except Exception as e:
        raise Exception(f"Failed to write private key: {e}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == 'extract':
        try:
            key_bytes = extract_private_key()
            print(f"Successfully extracted {len(key_bytes)}-byte private key")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: key_manager.py extract")
