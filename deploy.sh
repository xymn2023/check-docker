#!/usr/bin/env bash
# Public entry point: also works when BASH_SOURCE is /dev/fd/63.
set -Eeuo pipefail
umask 077
REPOSITORY="${CHECK_DOCKER_REPOSITORY:-xymn2023/check-docker}"
REF="${CHECK_DOCKER_REF:-main}"
DOWNLOAD_ONLY=''
if [[ "${1:-}" == '--download-only' ]]; then
  [[ $# == 2 ]] || { echo '用法：deploy.sh --download-only 目标目录'; exit 1; }
  DOWNLOAD_ONLY="$2"
  shift 2
elif [[ "${1:-}" == '--local' ]]; then
  shift
  LOCAL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
  [[ -f "$LOCAL_DIR/install.sh" ]] || { echo '--local 需要完整的本地项目目录'; exit 1; }
  exec bash "$LOCAL_DIR/install.sh" "$@"
fi
[[ "$REPOSITORY" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo '仓库名称无效'; exit 1; }
[[ "$REF" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo '分支/标签应为不含斜杠的名称，或完整 Commit SHA'; exit 1; }
if [[ -z "$DOWNLOAD_ONLY" && $EUID -ne 0 ]]; then
  echo '请先切换到 root，再执行一键命令；或下载 deploy.sh 后 sudo bash deploy.sh'
  exit 1
fi
# curl is already required by the outer one-line command.
command -v curl >/dev/null || { echo '请先安装 curl'; exit 1; }
if [[ -z "$DOWNLOAD_ONLY" ]]; then
  if ! python3 -c 'import sys, venv; assert sys.version_info >= (3,10)' >/dev/null 2>&1; then
    if command -v apt-get >/dev/null; then
      apt-get update
      apt-get install -y python3 python3-venv python3-pip ca-certificates
    elif command -v dnf >/dev/null; then
      dnf install -y python3 python3-pip ca-certificates
    else
      echo '请安装 Python 3.10+、venv、pip 后重试'; exit 1
    fi
  fi
  # Debian may provide import venv while ensurepip support is absent.
  if command -v apt-get >/dev/null && ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    apt-get install -y python3-venv
  fi
fi
python3 -c 'import sys; assert sys.version_info >= (3,10), "需要 Python 3.10+"'
WORK_DIR="$(mktemp -d)"
trap 'rm -rf -- "$WORK_DIR"' EXIT
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 60 \
  "https://api.github.com/repos/$REPOSITORY/commits/$REF" -o "$WORK_DIR/commit.json"
COMMIT="$(python3 - "$WORK_DIR/commit.json" <<'PY'
import json,re,sys
sha=json.load(open(sys.argv[1])).get('sha','')
if not re.fullmatch(r'[0-9a-f]{40}',sha):
    raise SystemExit('GitHub 未返回有效 Commit；请检查仓库、分支或 API 限流')
print(sha)
PY
)"
echo "正在获取 $REPOSITORY @ $COMMIT"
curl --fail --silent --show-error --location --retry 3 --connect-timeout 15 --max-time 300 \
  "https://codeload.github.com/$REPOSITORY/tar.gz/$COMMIT" -o "$WORK_DIR/source.tar.gz"
python3 - "$WORK_DIR/source.tar.gz" "$WORK_DIR/source" "$COMMIT" <<'PY'
import ast,pathlib,sys,tarfile
archive,destination,commit=sys.argv[1:]
root=pathlib.Path(destination)
root.mkdir(mode=0o700)
with tarfile.open(archive,'r:gz') as tar:
    members=tar.getmembers()
    if not members or sum(m.size for m in members)>100*1024*1024:
        raise SystemExit('源码包为空或超过100MiB限制')
    prefix=pathlib.PurePosixPath(members[0].name).parts[0]
    for m in members:
        parts=pathlib.PurePosixPath(m.name).parts
        if not parts or parts[0]!=prefix or '..' in parts or m.name.startswith('/'):
            raise SystemExit('源码包路径无效')
        if m.isdir():
            continue
        if not m.isfile():
            raise SystemExit('源码包不允许符号链接或特殊文件')
        out=root.joinpath(*parts[1:])
        if out==root:
            raise SystemExit('源码包结构无效')
        out.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        with tar.extractfile(m) as f:
            out.write_bytes(f.read())
for name in ('install.sh','uninstall.sh','core.py','autoupdate_bot.py','admin.py','requirements.txt','requirements.lock'):
    if not (root/name).is_file():
        raise SystemExit('仓库缺少 '+name+'；请上传新版项目根目录全部文件后重试')
for p in root.glob('*.py'):
    ast.parse(p.read_text(encoding='utf-8'),filename=p.name)
(root/'.source-commit').write_text(commit+'\n')
PY
bash -n "$WORK_DIR/source/install.sh" "$WORK_DIR/source/uninstall.sh"
if [[ -n "$DOWNLOAD_ONLY" ]]; then
  [[ ! -e "$DOWNLOAD_ONLY" ]] || { echo '下载目标已存在，请使用新目录'; exit 1; }
  mkdir -p -- "$(dirname -- "$DOWNLOAD_ONLY")"
  mv -- "$WORK_DIR/source" "$DOWNLOAD_ONLY"
  echo "源码已获取：$DOWNLOAD_ONLY（未安装、未修改服务）"
else
  bash "$WORK_DIR/source/install.sh" "$@"
fi
