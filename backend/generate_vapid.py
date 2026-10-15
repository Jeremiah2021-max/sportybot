"""
generate_vapid.py – One-time VAPID key generation for Web Push
==============================================================
Run ONCE before starting the server:
    python generate_vapid.py

Writes vapid_keys.json to the backend directory.
The public key is fetched by the frontend at /api/push/vapid-public-key.
"""

import json
import pathlib

try:
    from py_vapid import Vapid
except ImportError:
    print("Installing py_vapid...")
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "py-vapid"])
    from py_vapid import Vapid

OUTPUT = pathlib.Path(__file__).parent / "vapid_keys.json"

if OUTPUT.exists():
    keys = json.loads(OUTPUT.read_text())
    print("VAPID keys already exist:")
    print(f"  Public key:  {keys['public_key'][:40]}...")
    print(f"  Keys file:   {OUTPUT}")
    print("\nDelete vapid_keys.json and re-run to regenerate.")
else:
    vapid = Vapid()
    vapid.generate_keys()

    # py-vapid ≥1.9 exports via serialize_public_key / private_key
    import base64
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PublicFormat, PrivateFormat, NoEncryption
    )

    pub_bytes  = vapid.public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    priv_bytes = vapid.private_key.private_bytes(Encoding.PEM, PrivateFormat.TraditionalOpenSSL, NoEncryption())

    public_key  = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()
    private_key = priv_bytes.decode()

    keys = {
        "public_key":  public_key,
        "private_key": private_key,
    }
    OUTPUT.write_text(json.dumps(keys, indent=2))

    print("✅ VAPID keys generated!")
    print(f"  Public key:  {public_key[:40]}...")
    print(f"  Keys file:   {OUTPUT}")
    print()
    print("Next steps:")
    print("  1. The public key is served at GET /api/push/vapid-public-key")
    print("  2. Set VAPID_EMAIL env var to your contact email:")
    print("       $env:VAPID_EMAIL = 'you@example.com'")
    print("  3. Start the server: uvicorn main:app --reload --host 0.0.0.0 --port 8000")
