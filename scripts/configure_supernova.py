#!/usr/bin/env python3
"""Prepare private config from the installed model; no generation, download or service changes."""
import argparse
import hashlib
import json
import os
import pwd
import re
import secrets
import shutil
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domain', help='Public DNS already pointing to Supernova')
    parser.add_argument('--email', help='ACME certificate contact')
    parser.add_argument('--runner', type=Path, default=Path('/usr/local/lib/ollama/llama-server'))
    parser.add_argument('--draft-tokens', type=int, choices=[0, 4], default=4)
    parser.add_argument('--model', default='qwen3.8:27b', choices=['qwen3.8:27b'])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    directory = root/'.supernova'
    directory.mkdir(mode=0o700, exist_ok=True)
    with urlopen(Request('http://127.0.0.1:11434/api/show', data=json.dumps({'model': args.model}).encode(),
                         headers={'Content-Type': 'application/json'}), timeout=15) as response:
        info = json.load(response)
    paths = re.findall(r'^FROM (.+)$', info['modelfile'], re.MULTILINE)
    if not paths:
        raise SystemExit('Model path unavailable')
    model = Path(paths[0].strip()).resolve()
    if not model.is_file() or not args.runner.is_file():
        raise SystemExit('Installed model or native runtime missing')
    match = re.fullmatch('sha256-([0-9a-f]{64})', model.name)
    if not match:
        raise SystemExit('Expected the installed content-addressed model blob')
    if args.domain and (not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', args.domain)
                        or not args.email or any(c in args.email for c in '\r\n')):
        raise SystemExit('Valid domain and ACME email required')
    token = directory/'api-token'
    if not token.exists():
        fd = os.open(token, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as file:
            file.write(secrets.token_urlsafe(48))
    (directory/'data').mkdir(mode=0o700, exist_ok=True)
    (directory/'acme').mkdir(mode=0o700, exist_ok=True)
    values = {'SUPERNOVA_DATA': str(directory/'data'), 'SUPERNOVA_TOKEN_FILE': str(token),
              'SUPERNOVA_MODEL_PATH': str(model), 'SUPERNOVA_MODEL_SHA256': match[1],
              'SUPERNOVA_RUNNER_BIN': str(args.runner.resolve()),
              'SUPERNOVA_RUNTIME_SHA256': hashlib.sha256(args.runner.read_bytes()).hexdigest(),
              'SUPERNOVA_RUNNER_PORT': '18081', 'SUPERNOVA_RUNNER_URL': 'http://127.0.0.1:18081',
              'SUPERNOVA_CONTEXT': '65536', 'SUPERNOVA_API_PORT': '8090',
              'SUPERNOVA_DRAFT_TOKENS': str(args.draft_tokens),
              'SUPERNOVA_CONFIG_DIR': str(directory), 'SUPERNOVA_UID': str(os.getuid()),
              'SUPERNOVA_GID': str(os.getgid())}
    env = directory/'service.env'
    env.write_text(''.join(k+'='+json.dumps(v)+'\n' for k, v in values.items()))
    env.chmod(0o600)
    unit = f'''[Unit]
Description=Sinapsia Supernova native GPU runtime
After=network-online.target

[Service]
Type=simple
User={pwd.getpwuid(os.getuid()).pw_name}
WorkingDirectory={root}
EnvironmentFile={env}
ExecStart={shutil.which('python3')} -m supernova.run_runner
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
'''
    (directory/'supernova-runner.service').write_text(unit)
    if args.domain:
        static = {'entryPoints': {'web': {'address': ':80', 'http': {'redirections': {'entryPoint': {'to': 'websecure', 'scheme': 'https'}}}},
                                  'websecure': {'address': ':443'}},
            'providers': {'file': {'filename': '/etc/traefik/dynamic.yaml'}},
            'certificatesResolvers': {'le': {'acme': {'email': args.email, 'storage': '/acme/acme.json',
                                                    'httpChallenge': {'entryPoint': 'web'}}}}}
        dynamic = {'http': {'routers': {'supernova': {'rule': 'Host(`'+args.domain+'`)',
            'entryPoints': ['websecure'], 'tls': {'certResolver': 'le'}, 'service': 'supernova'}},
            'services': {'supernova': {'loadBalancer': {'servers': [{'url': 'http://127.0.0.1:8090'}]}}}}}
        (directory/'traefik.yaml').write_text(json.dumps(static, indent=2))
        (directory/'dynamic.yaml').write_text(json.dumps(dynamic, indent=2))
    print('Prepared:', env)
    print('Credential file (never paste its content):', token)
    print('Configuration only: no GPU inference or service restart performed.')


if __name__ == '__main__':
    main()
