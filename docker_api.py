"""Small Docker Engine HTTP client, run as a bounded asynchronous subprocess."""
import http.client
import json
from pathlib import Path
import re
import socket
import sys


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('localhost', timeout=90)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def request(endpoint, method, path, payload=None):
    if not endpoint.startswith('unix://'):
        raise ValueError('容器自动重建当前要求本机 unix:// Docker socket；远程 context 不自动操作')
    conn = UnixConnection(endpoint[7:])
    try:
        conn.request(method, path, body=json.dumps(payload).encode() if payload is not None else None,
                     headers={'Content-Type': 'application/json'})
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            # Avoid leaking environment variables/registry secrets through API errors.
            raise RuntimeError(f'Docker API {method} {path.split("?")[0]} 返回 HTTP {response.status}')
        return json.loads(body) if body else {}
    finally:
        conn.close()


if __name__ == '__main__':
    endpoint, method, path, *files = sys.argv[1:]
    try:
        version = request(endpoint, 'GET', '/version').get('ApiVersion', '')
        if not re.fullmatch(r'1\.\d+', version):
            raise ValueError('无法协商 Docker API 版本')
        payload = json.loads(Path(files[0]).read_text()) if files else None
        print(json.dumps(request(endpoint, method, '/v' + version + path, payload)))
    except (TimeoutError, ConnectionError, http.client.HTTPException) as exc:
        print('Docker API 连接中断，操作结果不确定', file=sys.stderr)
        sys.exit(124)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
