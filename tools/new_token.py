"""Creates a token for a gateway and adds its hash to gateway-tokens.json.

Usage:  python3 tools/new_token.py gw-gh1-01 [gateway-tokens.json]

The token is printed only once: load it into the device. The file keeps only
its SHA-256 hash, so it never contains a usable token.
After changing the file, run deploy.sh again (or add a new secret version).
"""
import hashlib
import json
import secrets
import sys
from pathlib import Path

if len(sys.argv) < 2:
    sys.exit(__doc__)

gateway_id = sys.argv[1]
path = Path(sys.argv[2] if len(sys.argv) > 2 else "gateway-tokens.json")

tokens = json.loads(path.read_text()) if path.exists() else {}
token = secrets.token_urlsafe(32)
tokens[hashlib.sha256(token.encode()).hexdigest()] = gateway_id
path.write_text(json.dumps(tokens, indent=2) + "\n")

print(f"gatewayId: {gateway_id}")
print(f"token:     {token}")
print(f"Saved hash in {path}. Configure the device with: Authorization: Bearer <token>")
