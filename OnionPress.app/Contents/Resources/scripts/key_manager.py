#!/usr/bin/env python3
"""
Key Management for onionpress
Converts Tor v3 Ed25519 private keys to/from BIP39 mnemonic words with checksums
"""

import subprocess
import base64

try:
    from mnemonic import Mnemonic
except ImportError:
    raise ImportError(
        "Required 'mnemonic' package is not installed. "
        "Install it with: pip3 install mnemonic"
    )

# Initialize BIP39 mnemonic encoder with English wordlist
mnemo = Mnemonic("english")

def bytes_to_mnemonic(key_bytes):
    """
    Convert 64-byte Ed25519 key to BIP39 mnemonic words with proper checksums

    Ed25519 keys are 64 bytes (512 bits). We split this into two 32-byte chunks:
    - First 32 bytes → 24-word mnemonic (with checksum)
    - Second 32 bytes → 24-word mnemonic (with checksum)
    Total: 48 words with proper BIP39 checksums for validation
    """
    if len(key_bytes) != 64:
        raise ValueError(f"Expected 64 bytes, got {len(key_bytes)}")

    # Split key into two 32-byte (256-bit) chunks
    first_half = key_bytes[:32]
    second_half = key_bytes[32:]

    # Convert each half to BIP39 mnemonic (24 words each with checksum)
    mnemonic_first = mnemo.to_mnemonic(first_half)
    mnemonic_second = mnemo.to_mnemonic(second_half)

    # Combine with separator
    return f"{mnemonic_first} | {mnemonic_second}"

def mnemonic_to_bytes(mnemonic):
    """
    Convert BIP39 mnemonic words back to 64-byte Ed25519 key
    Validates checksums before returning
    Returns exactly 64 bytes
    """
    # Split the two 24-word mnemonics
    if '|' not in mnemonic:
        raise ValueError("Invalid mnemonic format. Expected two 24-word mnemonics separated by '|'")

    parts = mnemonic.split('|')
    if len(parts) != 2:
        raise ValueError("Invalid mnemonic format. Expected exactly two mnemonics separated by '|'")

    mnemonic_first = parts[0].strip()
    mnemonic_second = parts[1].strip()

    # Validate both mnemonics (includes checksum validation)
    if not mnemo.check(mnemonic_first):
        raise ValueError("Invalid mnemonic (first half): checksum validation failed")
    if not mnemo.check(mnemonic_second):
        raise ValueError("Invalid mnemonic (second half): checksum validation failed")

    # Convert back to bytes
    first_half = mnemo.to_entropy(mnemonic_first)
    second_half = mnemo.to_entropy(mnemonic_second)

    # Combine to get 64-byte key
    key_bytes = first_half + second_half

    if len(key_bytes) != 64:
        raise ValueError(f"Invalid key size after decoding: {len(key_bytes)} bytes (expected 64)")

    return key_bytes

def extract_private_key():
    """
    Extract the Tor v3 private key from the running container
    Returns the raw key bytes (64 bytes)
    """
    try:
        # Read the secret key file from the Tor container
        result = subprocess.run(
            ['docker', 'exec', 'onionpress-tor', 'cat',
             '/var/lib/tor/hidden_service/wordpress/hs_ed25519_secret_key'],
            capture_output=True,
            timeout=10
        )

        if result.returncode != 0:
            raise Exception("Could not read Tor private key from container")

        # The key file format is:
        # "== ed25519v1-secret: type0 =="
        # followed by 64 bytes of key data
        key_data = result.stdout

        # Find the key data (skip the header)
        # The header is 32 bytes, then 64 bytes of actual key
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

def export_key_as_mnemonic():
    """
    Export the current Tor private key as BIP39 mnemonic words
    Returns 48 words (two 24-word mnemonics) with proper checksums
    """
    key_bytes = extract_private_key()
    return bytes_to_mnemonic(key_bytes)

def import_key_from_mnemonic(mnemonic):
    """
    Import a Tor private key from BIP39 mnemonic words
    Validates checksums and key format
    Returns the key bytes ready to be written to the container
    """
    # mnemonic_to_bytes already validates:
    # - Checksum for both halves
    # - 64-byte total length
    # - BIP39 word validity
    key_bytes = mnemonic_to_bytes(mnemonic)

    # Additional Ed25519 validation
    # The key should be 64 bytes (512 bits) for Ed25519 private key
    if len(key_bytes) != 64:
        raise ValueError(f"Invalid Ed25519 key size: {len(key_bytes)} bytes (expected 64)")

    # Basic sanity check - key shouldn't be all zeros
    if key_bytes == b'\x00' * 64:
        raise ValueError("Invalid key: all zeros")

    # Basic sanity check - key shouldn't be all 0xFF
    if key_bytes == b'\xFF' * 64:
        raise ValueError("Invalid key: all ones")

    return key_bytes

def write_private_key(key_bytes):
    """
    Write a new private key to the Tor container using secure Python file I/O
    This will change your onion address!
    """
    import tempfile
    import os

    try:
        # The Tor key file format includes a 32-byte header
        # Header: "== ed25519v1-secret: type0 =="
        header = b'== ed25519v1-secret: type0 =='

        # Pad header to 32 bytes
        header = header.ljust(32, b'\x00')

        # Combine header + key
        full_key = header + key_bytes

        # Write to a temporary file with restricted permissions
        with tempfile.NamedTemporaryFile(mode='wb', delete=False) as temp_file:
            temp_path = temp_file.name
            # Set restrictive permissions immediately
            os.chmod(temp_path, 0o600)
            temp_file.write(full_key)

        try:
            # Copy file to container using docker cp (safer than shell pipeline)
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
            # Securely delete temporary file (multi-pass for SSD wear leveling)
            if os.path.exists(temp_path):
                file_len = len(full_key)
                # Pass 1: overwrite with random bytes
                with open(temp_path, 'wb') as f:
                    f.write(os.urandom(file_len))
                    f.flush()
                    os.fsync(f.fileno())
                # Pass 2: overwrite with zeros
                with open(temp_path, 'wb') as f:
                    f.write(b'\x00' * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                # Pass 3: overwrite with ones
                with open(temp_path, 'wb') as f:
                    f.write(b'\xff' * file_len)
                    f.flush()
                    os.fsync(f.fileno())
                os.unlink(temp_path)

    except Exception as e:
        raise Exception(f"Failed to write private key: {e}")

if __name__ == "__main__":
    # Test the functionality
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == 'export':
        try:
            mnemonic = export_key_as_mnemonic()
            print("Your private key as mnemonic words:")
            print()
            print(mnemonic)
            print()
            print(f"({len(mnemonic.split())} words)")
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Usage: key_manager.py export")
