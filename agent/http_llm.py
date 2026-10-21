"""실제 HTTP 모델 연결. 비밀 키나 HTTP 응답 원문을 오류에 노출하지 않는다."""
import asyncio
import weakref
import json
import os

import httpx


_LIMITS = weakref.WeakKeyDictionary()


class TransientModelError(RuntimeError):
    pass


class HTTPModel:
    provider = ''

    def __init__(self, model, usd_per_1k_tokens=None):
        self.model = model
        self.name = f'{self.provider}:{model}'
        # 이전 실습의 단일 가상 단가 인자는 호환성 때문에 받지만 사용하지 않는다.

    async def request(self, system, user, schema=None, stream=False, on_token=None, token_limit=900):
        loop = asyncio.get_running_loop()
        limits = _LIMITS.setdefault(loop, {})
        limit = limits.setdefault(self.provider, asyncio.Semaphore(1 if self.provider == 'ollama' else 4))
        emitted = False
        def emit(piece):
            nonlocal emitted
            emitted = True
            if on_token:
                on_token(piece)
        try:
            async with asyncio.timeout(float(os.getenv('MODEL_DEADLINE_SECONDS', '300'))):
                async with limit:
                    for attempt in range(3):
                        try:
                            return await self._request(system, user, schema, stream, emit, token_limit)
                        except TransientModelError:
                            if emitted or attempt == 2:
                                raise RuntimeError(f'{self.provider} 일시적 연결 장애가 지속됩니다.') from None
                            await asyncio.sleep(0.5 * 2 ** attempt)
        except TimeoutError:
            raise RuntimeError(f'{self.provider} 호출의 전체 제한 시간을 초과했습니다.') from None

    async def _request(self, system, user, schema=None, stream=False, on_token=None, token_limit=900):
        remote = self.provider == 'openai'
        headers = {}
        payload = {'model': self.model, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': user}], 'stream': stream}
        if remote:
            key = os.getenv('OPENAI_API_KEY', '')
            if not key or key == 'sk-...':
                raise RuntimeError('OpenAI API 키가 .env에 설정되지 않았습니다.')
            headers['Authorization'] = f'Bearer {key}'
            url = 'https://api.openai.com/v1/chat/completions'
            payload.update(temperature=0, max_completion_tokens=token_limit)
            if schema:
                payload['response_format'] = {'type': 'json_schema', 'json_schema': {
                    'name': 'agent_result', 'strict': True, 'schema': schema}}
            if stream:
                payload['stream_options'] = {'include_usage': True}
        else:
            url = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/') + '/api/chat'
            payload.update(think=False, keep_alive='10m', options={
                'temperature': 0, 'num_predict': token_limit, 'num_ctx': 8192})
            if schema:
                payload['format'] = schema
        parts, usage = [], {}
        try:
            async with httpx.AsyncClient(timeout=240) as client:
                async with client.stream('POST', url, json=payload, headers=headers) as response:
                    if response.status_code in (429, 502, 503, 504):
                        raise TransientModelError('temporary failure')
                    if response.status_code >= 400:
                        raise RuntimeError(f'{self.provider} 호출 실패(HTTP {response.status_code}). '
                                           '키·모델 접근 권한·서버 상태를 확인하세요.')
                    if stream:
                        async for line in response.aiter_lines():
                            if not line or (remote and (not line.startswith('data: ')
                                                       or line == 'data: [DONE]')):
                                continue
                            item = json.loads(line[6:] if remote else line)
                            if item.get('error'):
                                raise RuntimeError(f'{self.provider} 추론 중 오류가 발생했습니다.')
                            if remote:
                                usage = item.get('usage') or usage
                                choices = item.get('choices') or []
                                piece = choices[0].get('delta', {}).get('content') if choices else None
                            else:
                                usage = item
                                piece = item.get('message', {}).get('content')
                            if piece:
                                parts.append(piece)
                                if on_token:
                                    on_token(piece)
                    else:
                        item = json.loads(await response.aread())
                        if remote:
                            parts.append(item['choices'][0]['message'].get('content') or '')
                            usage = item.get('usage') or {}
                        else:
                            parts.append(item.get('message', {}).get('content') or '')
                            usage = item
        except httpx.HTTPError:
            raise TransientModelError('network failure') from None
        text = ''.join(parts).strip()
        if not text:
            raise RuntimeError(f'{self.provider}가 빈 응답을 반환했습니다.')
        inp = usage.get('prompt_tokens' if remote else 'prompt_eval_count', 0)
        out = usage.get('completion_tokens' if remote else 'eval_count', 0)
        # 입력/출력 단가를 모델별로 명시한 경우에만 추정액을 계산한다.
        # 캐시 할인·세금·임베딩 비용은 포함하지 않으므로 청구 금액으로 표시하지 않는다.
        usd, configured = (0.0 if not remote else None), False
        if remote:
            try:
                prices = json.loads(os.getenv('MODEL_PRICES_JSON', '{}')).get(self.model)
                if prices:
                    usd = (inp * float(prices['input_per_million']) + out * float(prices['output_per_million'])) / 1000000
                    configured = True
            except (ValueError, TypeError, KeyError):
                pass
        return text, {'model': self.name, 'tokens': inp + out, 'input_tokens': inp,
                      'output_tokens': out, 'usd': round(usd, 8) if usd is not None else None,
                      'pricing': 'configured_estimate' if configured else 'local_no_api_fee' if not remote else 'unconfigured',
                      'cost_is_estimate': True, 'prompt': f'[SYSTEM]\n{system}\n\n[USER]\n{user}'}
