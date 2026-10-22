"""프로젝트 서버만 소유·종료하는 실제 모델 실행기."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


def wait_for_backend(child, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and child.poll() is None:
        try:
            with urlopen('http://127.0.0.1:8100/health', timeout=1) as response:
                if response.status == 200:
                    return True
        except (URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', choices=['openai', 'ollama', 'fake'])
    parser.add_argument('--backend-only', action='store_true')
    parser.add_argument('--frontend-only', action='store_true')
    args = parser.parse_args()
    if args.backend_only and args.frontend_only:
        parser.error('--backend-only와 --frontend-only는 함께 사용할 수 없습니다.')
    if not (ROOT / '.env').exists() and args.backend != 'fake' and not args.frontend_only:
        print('실행 설정 .env가 없습니다. .env.example을 복사해 설정해주세요.')
        return 1
    load_dotenv(ROOT / '.env')
    env = dict(os.environ)
    env['LLM_BACKEND'] = args.backend or env.get('LLM_BACKEND', 'openai')
    env['SUPERVISOR_VERSION'] = 'v2'
    ports = [8600] if args.frontend_only else [8100] if args.backend_only else [8100, 8600]
    for port in ports:
        with socket.socket() as sock:
            try:
                sock.bind(('127.0.0.1', port))
            except OSError:
                print(f'포트 {port}를 사용할 수 없습니다. 실행 중인 프로젝트 서버를 확인해주세요.')
                return 1
    children = []
    try:
        if args.frontend_only:
            try:
                with urlopen('http://127.0.0.1:8100/health', timeout=2) as response:
                    if response.status != 200:
                        raise URLError('backend unavailable')
            except (URLError, OSError):
                print('백엔드가 응답하지 않습니다. 먼저 run_agent_openai.sh 또는 run_agent_ollma.sh를 실행해주세요.')
                return 1
        else:
            children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'server:app',
                '--host', '127.0.0.1', '--port', '8100'], cwd=ROOT, env=env))
            if not wait_for_backend(children[0]):
                print('백엔드가 준비되지 않았습니다. 위 실행 오류와 .env 설정을 확인해주세요.', flush=True)
                return 1
        if not args.backend_only:
            children.append(subprocess.Popen([sys.executable, '-m', 'uvicorn', 'web_demo_server:app',
                '--host', '127.0.0.1', '--port', '8600'], cwd=ROOT, env=env))
        manual_mode = 'RAG 검색' if env.get('RAG_ENABLED', 'false').lower() == 'true' else '내부 매뉴얼 직접 조회'
        print(f"기본 모델: {env['LLM_BACKEND']}, 매뉴얼: {manual_mode}", flush=True)
        print('화면: http://localhost:8600 · API: http://localhost:8100 · 종료: Ctrl+C', flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == '__main__':
    raise SystemExit(main())
