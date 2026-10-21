"""실행 데이터·비밀 설정을 제외하고 원자적으로 배포 ZIP을 만든다."""
import argparse
import fnmatch
import os
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = ('.venv', '__pycache__', '.git', '.DS_Store', '*.log',
            'session.db*', '*.checkpoints.db*', '*.sqlite3*', '*.zip')


def distributable(path):
    return all(not (part.startswith('.env') and part != '.env.example')
               and not any(fnmatch.fnmatch(part, pattern) for pattern in EXCLUDED)
               for part in path.parts)


def build_archive(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.project-package-', suffix='.zip', dir=output.parent)
    os.close(descriptor)
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                relative = path.relative_to(root)
                if path.is_file() and not path.is_symlink() and distributable(relative):
                    archive.write(path, Path(root.name) / relative)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=ROOT.parent / (ROOT.name + '.zip'))
    args = parser.parse_args()
    print('생성됨:', build_archive(ROOT, args.output))
