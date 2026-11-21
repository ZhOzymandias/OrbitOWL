"""ASGI launcher for OrbitOWL. Public TLS terminates at the optional proxy/tunnel."""

import os

import uvicorn


def main():
    port = int(os.getenv("ORBITOWL_API_PORT") or os.getenv("SUPERNOVA_API_PORT", "8110"))
    uvicorn.run(
        "orbitowl.api:app",
        host="127.0.0.1",
        port=port,
        workers=1,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
