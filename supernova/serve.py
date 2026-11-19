"""ASGI launcher. Public TLS terminates at the optional Traefik service."""
import os

import uvicorn


def main():
    uvicorn.run('supernova.api:app', host='127.0.0.1',
                port=int(os.getenv('SUPERNOVA_API_PORT', '8090')), workers=1,
                access_log=False, proxy_headers=False)


if __name__ == '__main__':
    main()
