"""Read-only release check: python scripts/release_smoke.py [API origin]."""

import json
import sys
from urllib.error import HTTPError
from urllib.request import urlopen

origin = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")
for path in ("/api/v1/health", "/api/v1/ready", "/api/v1/products", "/openapi.json"):
    with urlopen(f"{origin}{path}", timeout=10) as response:
        assert response.status == 200, path
        assert response.headers.get("X-Content-Type-Options") == "nosniff", path
        assert "no-store" in response.headers.get("Cache-Control", ""), path
        result = json.load(response)
        if path == "/openapi.json":
            assert result["info"]["version"] == "1.0.0", result["info"]

try:
    urlopen(f"{origin}/api/v1/admin/offline/shops", timeout=10)
except HTTPError as error:
    assert error.code in (401, 403), error.code
else:
    raise AssertionError("Owner endpoint allowed an anonymous request")
print("Release smoke passed: readiness, catalog read, version and owner access.")
