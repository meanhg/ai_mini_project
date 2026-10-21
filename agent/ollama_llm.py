"""ollama 비동기 실제 모델 어댑터."""
from .http_llm import HTTPModel
from .llm_common import FastModelMixin, QualityModelMixin


class OllamaLLM(HTTPModel):
    provider = 'ollama'


class OllamaFastLLM(FastModelMixin, OllamaLLM):
    pass


class OllamaQualityLLM(QualityModelMixin, OllamaLLM):
    pass
