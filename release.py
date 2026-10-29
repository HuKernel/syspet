"""一键发布：打 tag → push → 创建 GitHub Release 并上传 dist/syspet.exe。
用法: python release.py [版本号]   （默认读 syspet.py 的 __version__）
凭据复用 git credential manager（与 git push 同一身份，无需额外配置）"""
import json
import os
import subprocess
import sys
import time
import urllib.request

REPO = 'HuKernel/syspet'


def sh(*cmd, inp=None):
    p = subprocess.run(cmd, input=inp, capture_output=True, text=True,
                       encoding='utf-8')
    if p.returncode:
        sys.exit(f'命令失败: {" ".join(cmd)}\n{p.stderr.strip()}')
    return p.stdout.strip()


def api(path, token, data=None, raw=None, method=None):
    url = path if path.startswith('http') else f'https://api.github.com/repos/{REPO}/{path}'
    req = urllib.request.Request(url, method=method or ('POST' if (data or raw) else 'GET'))
    req.add_header('Authorization', f'Bearer {token}')
    req.add_header('Accept', 'application/vnd.github+json')
    if raw is not None:
        req.add_header('Content-Type', 'application/octet-stream')
        body = raw
    elif data is not None:
        req.add_header('Content-Type', 'application/json')
        body = json.dumps(data).encode()
    else:
        body = None
    for i in range(3):  # 上传大文件链路易抖动，重试
        try:
            with urllib.request.urlopen(req, body) as r:
                return json.loads(r.read() or b'{}')
        except urllib.error.HTTPError as e:
            sys.exit(f'GitHub API {e.code}: {e.read().decode()[:200]}')
        except (urllib.error.URLError, ConnectionError) as e:
            if i == 2:
                sys.exit(f'网络失败（已重试3次）: {e}')
            time.sleep(5 * (i + 1))


ver = sys.argv[1] if len(sys.argv) > 1 else sh('python', '-c',
                                               'import syspet; print(syspet.__version__)')
tag = f'v{ver}'
exe = os.path.join('dist', 'syspet.exe')
assert os.path.exists(exe), '先打包: pyinstaller --onefile --noconsole --icon=syspet.ico ...'

token = dict(l.split('=', 1) for l in sh('git', 'credential', 'fill',
              inp='protocol=https\nhost=github.com\n\n').splitlines() if '=' in l)['password']

subprocess.run(['git', 'tag', tag], capture_output=True)  # 已存在则忽略
sh('git', 'push', 'origin', 'main', tag)

notes = sh('git', 'log', '--oneline', f'{tag}^..{tag}').partition(' ')[2]
try:
    rel = api(f'releases/tags/{tag}', token)
except SystemExit:  # 无该 tag 的 release，创建
    rel = api('releases', token, {'tag_name': tag, 'name': f'SysPet {tag}',
                                  'body': notes or tag})
for a in rel.get('assets', []):  # 同名旧附件先删，保证幂等
    api(f'releases/assets/{a["id"]}', token, method='DELETE')
api(f'https://uploads.github.com/repos/{REPO}/releases/{rel["id"]}/assets?name=syspet.exe',
    token, raw=open(exe, 'rb').read())
print(f'发布完成: {tag} → {rel["html_url"]}')
