"""Build a source-only archive for GitHub publishing; never include live data."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
TOP_LEVEL = ['README.md', 'autoupdate_bot.py', 'core.py', 'admin.py', 'deploy.sh', 'install.sh', 'uninstall.sh',
             'requirements.txt', 'requirements.lock', 'config.example.json', 'build_release.py', '.gitignore']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(ROOT.parent / 'check-docker-v2.0.0.zip'))
    args = parser.parse_args()
    paths = [ROOT / name for name in TOP_LEVEL]
    paths += sorted((ROOT / 'docs').glob('*.md'))
    paths += sorted((ROOT / 'tests').glob('*.py'))
    paths += sorted((ROOT / '.github/workflows').glob('*.yml'))
    output = Path(args.output).resolve()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            z.write(p, 'check-docker-v2.0.0/' + p.relative_to(ROOT).as_posix())
    print(output)


if __name__ == '__main__':
    main()
