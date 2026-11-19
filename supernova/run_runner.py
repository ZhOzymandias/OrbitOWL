"""Pinned standalone native process on a fixed loopback port, independent of Ollama leases."""
import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path


def cuda_environment(binary):
    """Ollama loads its out-of-tree CUDA .so explicitly, not through LD paths alone."""
    explicit = os.getenv('SUPERNOVA_CUDA_BACKEND') or os.getenv('GGML_BACKEND_PATH')
    candidates = [Path(explicit)] if explicit else sorted(binary.parent.glob('cuda_*/libggml-cuda.so*'))
    seen = set()
    for candidate in candidates:
        backend = candidate.resolve()
        if backend in seen or not backend.is_file() or not backend.name.startswith('libggml-cuda.so'):
            continue
        seen.add(backend)
        env = dict(os.environ)
        env['GGML_BACKEND_PATH'] = str(backend)
        paths = [str(binary.parent), str(backend.parent), env.get('LD_LIBRARY_PATH', '')]
        env['LD_LIBRARY_PATH'] = ':'.join(p for p in paths if p)
        try:
            probe = subprocess.run([str(binary), '--list-devices'], env=env,
                                   capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        device = re.search(r'^\s*(CUDA\d+)\s*:', probe.stdout, re.MULTILINE)
        if probe.returncode == 0 and device:
            return env, device[1], backend
    raise SystemExit('CUDA GPU unavailable: no model loaded; CPU fallback forbidden. '
                     'Check installed CUDA backend and driver.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Probe CUDA without loading a model or generating')
    options = parser.parse_args(argv)
    binary = Path(os.environ['SUPERNOVA_RUNNER_BIN']).resolve()
    if hashlib.sha256(binary.read_bytes()).hexdigest() != os.environ['SUPERNOVA_RUNTIME_SHA256']:
        raise SystemExit('Runtime checksum mismatch')
    model = Path(os.environ['SUPERNOVA_MODEL_PATH']).resolve()
    if not model.is_file():
        raise SystemExit('Model missing')
    env, device, backend = cuda_environment(binary)
    if options.check:
        print(json.dumps({'cuda_device': device, 'cuda_backend': str(backend),
                          'model_loaded': False, 'inference_started': False}))
        return
    args = [str(binary), '--model', str(model), '--host', '127.0.0.1',
            '--port', os.getenv('SUPERNOVA_RUNNER_PORT', '18081'), '--no-webui', '--offline',
            '-c', os.getenv('SUPERNOVA_CONTEXT', '65536'), '-np', '1', '-ngl', '999',
            '--no-jinja', '--chat-template', 'chatml', '--flash-attn', 'on',
            '--device', device]
    draft = int(os.getenv('SUPERNOVA_DRAFT_TOKENS', '4'))
    if draft not in (0, 4):
        raise SystemExit('Unsupported draft configuration')
    if draft:
        args += ['--spec-type', 'draft-mtp', '--spec-draft-n-max', str(draft), '--spec-draft-backend-sampling']
    os.execve(binary, args, env)


if __name__ == '__main__':
    main()
