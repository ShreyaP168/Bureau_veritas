import json
from types import SimpleNamespace

from veritas.provider import KEYS, AzureAdapter, load_uc_settings


def test_uc_loader_reads_only_provider_keys_and_respects_environment(tmp_path, monkeypatch):
    path = tmp_path/'uc.env'
    path.write_text('AZURE_OPENAI_API_KEY="test-only"\nOPENAI_MODEL=from-file\nUW_OUTPUT_DIR=unwanted\n', encoding='utf-8')
    for key in KEYS: monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv('UW_OUTPUT_DIR', raising=False)
    monkeypatch.setenv('VERITAS_UC_ENV_FILE', str(path))
    monkeypatch.setenv('OPENAI_MODEL', 'existing')
    load_uc_settings()
    import os
    assert os.environ['AZURE_OPENAI_API_KEY'] == 'test-only'
    assert os.environ['OPENAI_MODEL'] == 'existing'
    assert 'UW_OUTPUT_DIR' not in os.environ


def test_azure_adapter_converts_images_and_validates_json():
    from pydantic import BaseModel
    class Schema(BaseModel):
        value: str
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps({'value': 'ok'})))],
            usage=SimpleNamespace(model_dump=lambda: {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}))
    adapter = AzureAdapter.__new__(AzureAdapter)
    adapter.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = adapter.parse(model='test-deployment', store=False, text_format=Schema, instructions='Extract facts.',
        input=[{'role': 'user', 'content': [{'type': 'input_text', 'text': 'test'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,test'}]}])
    assert result.output_parsed.value == 'ok'
    assert result.usage['total_tokens'] == 120
    assert calls[0]['model'] == 'test-deployment'
    assert calls[0]['response_format']['type'] == 'json_object'
    assert calls[0]['messages'][1]['content'][1]['image_url']['detail'] == 'high'
