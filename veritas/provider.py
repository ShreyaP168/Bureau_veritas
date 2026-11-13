"""Load only provider settings from UC; never copy its environment file."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

KEYS = {'AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY', 'AZURE_OPENAI_API_VERSION',
        'AZURE_AI_EVALUATION_DEPLOYMENT', 'OPENAI_API_KEY', 'OPENAI_MODEL'}


def load_uc_settings():
    path = Path(os.getenv('VERITAS_UC_ENV_FILE', r'D:\python\UC\config\environment\.env.cdp'))
    if path.is_file():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.removeprefix('export ').split('=', 1)
            key, value = key.strip(), value.strip()
            if key in KEYS:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if value:
                    os.environ.setdefault(key, value)


def azure_configured():
    return all(os.getenv(k) for k in ('AZURE_OPENAI_ENDPOINT', 'AZURE_OPENAI_API_KEY', 'AZURE_AI_EVALUATION_DEPLOYMENT'))


def provider_name():
    value = os.getenv('VERITAS_PROVIDER', 'azure').lower()
    if value not in {'azure', 'openai'}:
        raise ValueError('VERITAS_PROVIDER must be azure or openai.')
    return value


def default_model():
    return os.getenv('AZURE_AI_EVALUATION_DEPLOYMENT', '') if provider_name() == 'azure' else os.getenv('OPENAI_MODEL', '')


def configured():
    return azure_configured() if provider_name() == 'azure' else bool(os.getenv('OPENAI_API_KEY') and os.getenv('OPENAI_MODEL'))


class AzureAdapter:
    def __init__(self):
        from openai import AzureOpenAI
        self.client = AzureOpenAI(azure_endpoint=os.environ['AZURE_OPENAI_ENDPOINT'],
            api_key=os.environ['AZURE_OPENAI_API_KEY'],
            api_version=os.getenv('AZURE_OPENAI_API_VERSION', '2024-10-21'),
            timeout=120, max_retries=2)
        self.responses = self

    def parse(self, *, model, store, text_format, instructions, input):
        # Match UC's Azure Chat Completions JSON-mode deployment contract.
        # Pydantic validates the returned JSON before any fields are accepted.
        parts = []
        for item in input[0]['content']:
            if item['type'] == 'input_text':
                parts.append({'type': 'text', 'text': item['text']})
            else:
                parts.append({'type': 'image_url', 'image_url': {'url': item['image_url'], 'detail': 'high'}})
        schema = json.dumps(text_format.model_json_schema())
        response = self.client.chat.completions.create(model=model, temperature=0,
            max_tokens=1200, response_format={'type': 'json_object'},
            messages=[{'role': 'system', 'content': instructions+' Return only JSON matching this schema: '+schema},
                      {'role': 'user', 'content': parts}])
        choice = response.choices[0]
        if choice.finish_reason != 'stop' or not choice.message.content:
            raise RuntimeError('Azure returned incomplete or refused output.')
        usage = getattr(response, 'usage', None)
        return SimpleNamespace(output_parsed=text_format.model_validate_json(choice.message.content),
            usage=usage.model_dump() if usage is not None else None,
            provider='azure', model=getattr(response, 'model', model),
            id=getattr(response, 'id', None))


def create_client():
    if provider_name() == 'azure':
        return AzureAdapter()
    from openai import OpenAI
    return OpenAI(timeout=120, max_retries=2)
