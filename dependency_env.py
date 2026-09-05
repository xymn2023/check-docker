"""Select an exact reusable venv, or build one when dependencies are missing."""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time


CHECK_SCRIPT = r'''
import importlib.metadata as metadata
from pathlib import Path
import re, sys
lock = Path(sys.argv[1]).read_text(encoding='utf-8').splitlines()
wanted = {}
for raw in lock:
    line = raw.strip()
    if not line or line.startswith('#'):
        continue
    match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^;\s]+)', line)
    if not match:
        raise SystemExit(2)
    wanted[match.group(1).lower().replace('_', '-')] = match.group(2)
installed = {d.metadata['Name'].lower().replace('_', '-'): d.version
             for d in metadata.distributions() if d.metadata.get('Name')}
missing = [f'{name}=={version}' for name, version in wanted.items()
           if installed.get(name) != version]
if missing:
    print('\n'.join(missing))
    raise SystemExit(1)
'''


def fingerprint(lock: Path) -> str:
    identity = f'{sys.implementation.name}-{sys.version_info.major}.{sys.version_info.minor}\0'.encode()
    return hashlib.sha256(identity + lock.read_bytes()).hexdigest()[:20]


def valid(venv: Path, lock: Path) -> bool:
    python = venv / 'bin' / 'python'
    if not python.is_file():
        return False
    result = subprocess.run([str(python), '-c', CHECK_SCRIPT, str(lock)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0


def link(target: Path, source: Path):
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.symlink_to(source.resolve(), target_is_directory=True)


def prepare(lock: Path, target: Path, cache_root: Path, candidates: list[Path]) -> str:
    for candidate in candidates:
        if candidate and valid(candidate, lock):
            link(target, candidate)
            return f'依赖版本完全匹配，复用现有环境：{candidate.resolve()}'

    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache_key = fingerprint(lock)
    cached = cache_root / cache_key
    for reusable in [cached, *sorted(cache_root.glob(cache_key + '-*'))]:
        if valid(reusable, lock):
            link(target, reusable)
            return f'依赖版本完全匹配，复用缓存环境：{reusable}'

    temporary = Path(tempfile.mkdtemp(prefix='.building-', dir=cache_root))
    try:
        subprocess.run([sys.executable, '-m', 'venv', str(temporary)], check=True)
        subprocess.run([str(temporary / 'bin' / 'python'), '-m', 'pip',
                        '--disable-pip-version-check', 'install', '-r', str(lock)], check=True)
        if not valid(temporary, lock):
            raise RuntimeError('依赖安装完成后版本核对失败')
        destination = cached if not cached.exists() else cache_root / f'{cache_key}-{time.time_ns()}'
        try:
            os.rename(temporary, destination)
        except FileExistsError:
            if valid(cached, lock):
                destination = cached
            else:
                raise RuntimeError('依赖缓存并发创建冲突，请重新运行安装')
        link(target, destination)
        return f'未找到完整依赖环境，已按清单安装并缓存：{destination}'
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--target', required=True, type=Path)
    parser.add_argument('--cache-root', required=True, type=Path)
    parser.add_argument('--candidate', action='append', default=[], type=Path)
    args = parser.parse_args()
    print(prepare(args.lock.resolve(), args.target, args.cache_root,
                  [p for p in args.candidate if p.exists()]))


if __name__ == '__main__':
    main()
