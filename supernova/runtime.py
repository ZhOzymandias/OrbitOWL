"""Stable native llama-server transport: no /proc discovery or Ollama child ports."""
import json
import time
from pathlib import Path

import httpx

from .artifacts import MAX_ARTIFACT_BYTES
from .contracts import ServiceError, canonical
from .documents import source_text
from .owl_grammar import owl_grammar

REVISION = 'direct-original-files-3.4'
PROMPT_PATH = Path(__file__).with_name('ontology_prompt.txt')


def messages(sources, iri):
    system = PROMPT_PATH.read_text(encoding='utf-8').replace('{iri}', iri).strip()
    return [{'role': 'system', 'content': system},
            {'role': 'user', 'content': 'DOCUMENTI COMPLETI:\n'+source_text(sources)}]


def render(history):
    # Explicit medium: thinking is open, no xhigh instruction, matching Qwen3.8 ChatML.
    return ''.join('<|im_start|>'+m['role']+'\n'+m['content']+'<|im_end|>\n'
                   for m in history)+'<|im_start|>assistant\n<think>\n'


class NativeRuntime:
    def __init__(self, url, context=65536, model_digest='', transport=None, *, model='qwen3.8:27b'):
        self.url, self.context, self.model_digest = url.rstrip('/'), context, model_digest
        self.transport = transport
        self.model = model

    def generate(self, history, directory, progress, *, iri):
        prompt = render(history)
        with httpx.Client(base_url=self.url, timeout=15, transport=self.transport) as client:
            response = client.post('/tokenize', json={'content': prompt, 'add_special': False, 'parse_special': True})
            response.raise_for_status()
            tokens = response.json()['tokens']
            if not tokens or len(tokens)+4096 > self.context:
                raise ServiceError('context_capacity_exceeded')
            body = {'prompt': tokens, 'n_predict': -1, 'stream': True, 'cache_prompt': True,
                    'ignore_eos': False, 'stop': [], 'return_tokens': True, 'grammar': owl_grammar(iri),
                    'temperature': 0.6, 'top_p': 0.95, 'top_k': 20, 'min_p': 0,
                    'repeat_penalty': 1, 'presence_penalty': 0}
            (directory/'request.json').write_bytes(canonical({'revision': REVISION, 'messages': history,
                'options': {k: v for k, v in body.items() if k != 'prompt'}}))
            final, total, last = None, 0, time.monotonic()
            started = last
            with (directory/'raw-response.txt').open('w', encoding='utf-8') as raw, \
                    (directory/'native-stream.jsonl').open('w', encoding='utf-8') as log, \
                    client.stream('POST', '/completion', json=body, timeout=None) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                        continue
                    event = json.loads(line[5:])
                    log.write(json.dumps(event, ensure_ascii=False)+'\n')
                    log.flush()
                    content = event.get('content', '')
                    total += len(content.encode())
                    if total > MAX_ARTIFACT_BYTES:
                        raise ServiceError('artifact_byte_limit')
                    raw.write(content)
                    raw.flush()
                    if event.get('error'):
                        raise ServiceError('model_runtime_error')
                    if time.monotonic()-last >= 1:
                        progress({'output_bytes': total, 'input_tokens': len(tokens)})
                        last = time.monotonic()
                    if event.get('stop'):
                        final = event
                        break
            if not final:
                raise ServiceError('stream_ended_without_final')
            (directory/'native-final.json').write_bytes(canonical(final))
            text = (directory/'raw-response.txt').read_bytes().decode('utf-8')
            thinking, separator, xml = text.partition('</think>')
            if not separator or final.get('truncated') or not xml.rstrip().endswith('</rdf:RDF>'):
                raise ServiceError('ontology_generation_incomplete')
            (directory/'thinking.txt').write_text(thinking, encoding='utf-8')
            xml = xml.lstrip()
            (directory/'original.owl').write_bytes(xml.encode('utf-8'))
            settings = final.get('generation_settings', {})
            if settings.get('grammar') != body['grammar'] or settings.get('grammar_lazy'):
                raise ServiceError('ontology_grammar_not_confirmed')
            return xml, {'model': self.model, 'model_digest': self.model_digest,
                'thinking': 'medium', 'context': self.context, 'num_predict': -1,
                'grammar': 'rdfxml', 'input_tokens': len(tokens),
                'stop_type': final.get('stop_type'), 'timings': final.get('timings', {}),
                'wall_seconds': time.monotonic()-started,
                'prompt_revision': REVISION}
