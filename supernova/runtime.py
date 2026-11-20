"""Native llama-server transport for one bounded, schema-constrained section."""

import json
import time
from pathlib import Path

import httpx

from .contracts import ServiceError, canonical, sha256
from .section_schema import (
    GRAMMAR, decode_section, entity_grammar, fact_grammar, source_passages, validate_section,
)

REVISION = 'structured-sections-1.6.2'
PREVIOUS_REVISIONS = ('structured-sections-1.2', 'structured-sections-1.3', 'structured-sections-1.4', 'structured-sections-1.6.1')
PROMPT_PATH = Path(__file__).with_name('section_prompt.txt')
SECTION_OUTPUT_TOKENS = 4096
MAX_SECTION_BYTES = 65536


class NativeRuntime:
    def __init__(self, url, context=65536, model_digest='', transport=None, *, model='qwen3.8:27b'):
        self.url, self.context, self.model_digest = url.rstrip('/'), context, model_digest
        self.transport = transport
        self.model = model
        self.entity_prompt = PROMPT_PATH.with_name('section_entities_prompt.txt').read_text(encoding='utf-8').strip()
        self.fact_prompt = PROMPT_PATH.with_name('section_facts_prompt.txt').read_text(encoding='utf-8').strip()
        example_entities = [{'id': 'c', 'kind': 'class'}, {'id': 'i', 'kind': 'individual'},
                            {'id': 'p', 'kind': 'procedure'}]
        self.signature = sha256(canonical([
            REVISION, self.entity_prompt, self.fact_prompt, entity_grammar(),
            fact_grammar(example_entities, ['p1']), model, model_digest, context,
        ]))
        # 1.3 changed only the first line of the grammar from 1.2.
        old_grammar = PROMPT_PATH.with_name('section_grammar_1_1.gbnf').read_text(encoding='utf-8').strip()
        previous = (
            ('structured-sections-1.1', 'section_prompt_1_1.txt', old_grammar),
            ('structured-sections-1.3', 'section_prompt_1_3.txt',
             'root ::= ws complete ws\n' + old_grammar.partition('\n')[2]),
            ('structured-sections-1.4', 'section_prompt.txt', GRAMMAR),
        )
        self.previous_signatures = {
            pipeline_revision: sha256(canonical([
                extraction_revision, PROMPT_PATH.with_name(prompt).read_text(encoding='utf-8').strip(),
                grammar, model, model_digest, context,
            ]))
            for pipeline_revision, (extraction_revision, prompt, grammar) in zip(PREVIOUS_REVISIONS[:3], previous, strict=True)
        }

        old_prompt = PROMPT_PATH.with_name('section_facts_prompt_1_6_1.txt').read_text(encoding='utf-8').strip()
        self.previous_signatures['structured-sections-1.6.1'] = sha256(canonical([
            'structured-sections-1.6.1', self.entity_prompt, old_prompt, entity_grammar(),
            fact_grammar(example_entities), model, model_digest, context,
        ]))

    def generate_section(self, section, directory, progress):
        checkpoint = directory / 'entities.json'
        if checkpoint.is_file():
            entities = decode_section(checkpoint.read_text(encoding='utf-8'))
        else:
            progress({'section_step': 'entities'})
            entities = self.complete(self.entity_prompt, section, entity_grammar(), directory, progress)
        if not isinstance(entities, dict) or set(entities) != {'entities'}:
            raise ServiceError('section_schema_invalid')
        data = {'status': 'complete', **entities, 'relations': [], 'attributes': []}
        validate_section(data, section['text'])
        if not checkpoint.is_file():
            temporary = checkpoint.with_suffix('.tmp')
            temporary.write_bytes(canonical(entities))
            temporary.replace(checkpoint)

        passages = source_passages(section['text'])
        progress({'section_step': 'facts'})
        facts = self.complete(
            self.fact_prompt, {'passages': passages, **entities},
            fact_grammar(entities['entities'], passages),
            directory, progress,
        )
        if not isinstance(facts, dict) or set(facts) != {'relations', 'attributes', 'unresolved'}:
            raise ServiceError('section_schema_invalid')
        unresolved = facts.pop('unresolved')
        if not isinstance(unresolved, list) or len(unresolved) > 12 or any(
            not isinstance(ref, str) or ref not in passages for ref in unresolved
        ):
            raise ServiceError('section_schema_invalid')
        for rows in facts.values():
            if not isinstance(rows, list):
                raise ServiceError('section_schema_invalid')
            for row in rows:
                ref = row.get('quote') if isinstance(row, dict) else None
                if not isinstance(ref, str) or ref not in passages:
                    raise ServiceError('section_passage_reference_invalid')
                row['quote'] = passages[ref]
        data.update(facts)
        validate_section(data, section['text'])
        if unresolved:
            raise ServiceError('section_facts_unresolved', details={'facts_unresolved': len(unresolved)})
        return data

    def complete(self, system, payload, grammar, directory, progress):
        # The tokenizer parses ChatML markers only from the template.
        user = json.dumps(payload, ensure_ascii=False).replace('<', r'\u003c')
        prompt = (f'<|im_start|>system\n{system}<|im_end|>\n'
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
                'grammar': grammar, 'temperature': 0, 'top_p': 1, 'top_k': 1,
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
            if settings.get('grammar') != grammar or settings.get('grammar_lazy'):
                raise ServiceError('section_grammar_not_confirmed')
            progress({'section_output_bytes': size})
            return decode_section((directory/'response.json.part').read_text(encoding='utf-8'))
