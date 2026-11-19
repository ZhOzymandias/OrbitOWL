"""Native llama-server transport for one bounded, schema-constrained section."""

import json
import time
from pathlib import Path

import httpx

from .contracts import ServiceError, canonical, sha256
from .section_schema import GRAMMAR, decode_section

REVISION = 'structured-sections-1.2'
EXTRACTION_REVISION = 'structured-sections-1.1'
PROMPT_PATH = Path(__file__).with_name('section_prompt.txt')
SECTION_OUTPUT_TOKENS = 4096
MAX_SECTION_BYTES = 65536


class NativeRuntime:
    def __init__(self, url, context=65536, model_digest='', transport=None, *, model='qwen3.8:27b'):
        self.url, self.context, self.model_digest = url.rstrip('/'), context, model_digest
        self.transport = transport
        self.model = model
        self.prompt = PROMPT_PATH.read_text(encoding='utf-8').strip()
        self.signature = sha256(canonical([
            EXTRACTION_REVISION, self.prompt, GRAMMAR, model, model_digest, context,
        ]))

    def generate_section(self, section, directory, progress):
        # The tokenizer parses ChatML markers only from the template.
        user = json.dumps(section, ensure_ascii=False).replace('<', r'\u003c')
        prompt = (f'<|im_start|>system\n{self.prompt}<|im_end|>\n'
                  f'<|im_start|>user\n{user}<|im_end|>\n'
                  '<|im_start|>assistant\n<think>\n\n</think>\n\n')
        with httpx.Client(base_url=self.url, timeout=90, transport=self.transport) as client:
            response = client.post('/tokenize', json={
                'content': prompt, 'add_special': False, 'parse_special': True})
            response.raise_for_status()
            tokens = response.json()['tokens']
            if not tokens or len(tokens) + SECTION_OUTPUT_TOKENS > self.context:
                raise ServiceError('section_context_capacity_exceeded')
            body = {
                'prompt': tokens, 'n_predict': SECTION_OUTPUT_TOKENS, 'stream': True,
                'cache_prompt': True, 'ignore_eos': False, 'stop': [], 'return_tokens': True,
                'grammar': GRAMMAR, 'temperature': 0, 'top_p': 1, 'top_k': 1,
            }
            final, size = None, 0
            last = time.monotonic()
            progress({'section_input_tokens': len(tokens), 'section_output_bytes': 0})
            with (directory/'response.json.part').open('w', encoding='utf-8') as raw, \
                    client.stream('POST', '/completion', json=body) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                        continue
                    event = json.loads(line[5:])
                    if event.get('error'):
                        raise ServiceError('section_model_runtime_error')
                    content = event.get('content', '')
                    size += len(content.encode())
                    if size > MAX_SECTION_BYTES:
                        raise ServiceError('section_output_limit')
                    raw.write(content)
                    raw.flush()
                    if time.monotonic() - last >= 1:
                        progress({'section_output_bytes': size})
                        last = time.monotonic()
                    if event.get('stop'):
                        final = event
                        break
            if final is None:
                raise ServiceError('section_stream_incomplete')
            (directory/'native-final.json').write_bytes(canonical({
                key: final.get(key) for key in
                ('stop_type', 'truncated', 'tokens_evaluated', 'tokens_predicted', 'timings')
            }))
            if final.get('truncated') or final.get('stop_type') == 'limit':
                raise ServiceError('section_output_limit')
            settings = final.get('generation_settings', {})
            if settings.get('grammar') != GRAMMAR or settings.get('grammar_lazy'):
                raise ServiceError('section_grammar_not_confirmed')
            progress({'section_output_bytes': size})
            return decode_section((directory/'response.json.part').read_text(encoding='utf-8'))
