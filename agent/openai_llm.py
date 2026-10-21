"""openai 비동기 실제 모델 어댑터."""
from .http_llm import HTTPModel
from .llm_common import FastModelMixin, QualityModelMixin


class OpenAILLM(HTTPModel):
    provider = 'openai'


class OpenAIFastLLM(FastModelMixin, OpenAILLM):
    pass


class OpenAIQualityLLM(QualityModelMixin, OpenAILLM):
    pass
