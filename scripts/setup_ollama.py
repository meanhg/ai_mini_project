"""현재 .env와 동일한 모델을 준비하고 기존 Ollama 서버는 재사용한다."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def tags(base):
    with urlopen(base + '/api/tags', timeout=2) as response:
        return {m['name'] for m in json.load(response)['models']}


def main():
    load_dotenv(ROOT / '.env')
    if not shutil.which('ollama'):
        raise RuntimeError('ollama가 설치되어 있지 않습니다.')
    base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    models = list(dict.fromkeys([os.getenv('OLLAMA_FAST_MODEL', 'qwen3.5:2b'),
        os.getenv('OLLAMA_QUALITY_MODEL', 'qwen3:8b'),
        os.getenv('OLLAMA_EMBEDDING_MODEL', 'qwen3-embedding:0.6b')]))
    env = {**os.environ, 'OLLAMA_HOST': base}
    child = None
    try:
        try:
            installed = tags(base)
            print('실행 중인 Ollama 서버를 재사용합니다.', flush=True)
        except (URLError, OSError):
            if urlparse(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
                raise RuntimeError('지정한 Ollama 서버에 연결할 수 없습니다.') from None
            with tempfile.NamedTemporaryFile(prefix='mini-project-ollama-', suffix='.log', delete=False) as log:
                child = subprocess.Popen(['ollama', 'serve'], env=env, stdout=log,
                                         stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(60):
                try:
                    installed = tags(base)
                    break
                except (URLError, OSError):
                    if child.poll() is not None:
                        raise RuntimeError('Ollama 시작 실패. 로그: ' + log.name) from None
                    time.sleep(.5)
            else:
                raise RuntimeError('Ollama 준비 시간이 초과되었습니다. 로그: ' + log.name)
        for model in models:
            name = model if ':' in model else model + ':latest'
            if os.getenv('OLLAMA_FORCE_REFRESH') == '1' and name in installed:
                subprocess.run(['ollama', 'rm', model], env=env, check=True)
                installed.discard(name)
            if name not in installed:
                subprocess.run(['ollama', 'pull', model], env=env, check=True)
            else:
                print('이미 준비된 모델:', model, flush=True)
        print('준비 완료. Ollama 서버를 유지합니다. bash sh/run_live.sh로 실행하세요.')
        return 0
    except BaseException:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(str(error))
        raise SystemExit(1)
