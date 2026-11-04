"""SysPet — 桌宠式系统监控悬浮窗：CPU/GPU/内存/磁盘/网速 + 全盘文件变化 + AI 平台用量。
依赖: psutil, pywin32（见 requirements.txt）"""
import base64, ctypes, datetime, hashlib, hmac, json, logging, os, shutil, subprocess
import sys, threading, time, uuid, webbrowser, winreg
from collections import deque
import urllib.error, urllib.parse, urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

import psutil
import pywintypes
import win32con
import win32crypt
import win32file

try:  # 通知捕获依赖；缺失时仅关闭该功能，不影响其余监控
    import comtypes.client
    comtypes.client.GetModule('UIAutomationCore.dll')
    from comtypes.gen.UIAutomationClient import CUIAutomation, IUIAutomation
except Exception:
    CUIAutomation = IUIAutomation = None

log = logging.getLogger('syspet')
__version__ = '3.2.12'
# PyInstaller exe 运行时配置/日志放 exe 旁（绿色软件惯例）；脚本运行时放脚本旁
BASE = Path(sys.executable if getattr(sys, 'frozen', False) else __file__)
RUN_KEY = (winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run')


def autostart_cmd():
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    pyw = Path(sys.executable).with_name('pythonw.exe')
    return f'"{pyw}" "{Path(sys.argv[0]).resolve()}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(*RUN_KEY) as k:
            return winreg.QueryValueEx(k, 'SysPet')[0] == autostart_cmd()
    except OSError:
        return False


def set_autostart(on):
    try:
        if on:
            with winreg.CreateKeyEx(*RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, 'SysPet', 0, winreg.REG_SZ, autostart_cmd())
        else:
            with winreg.OpenKey(*RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, 'SysPet')
        log.info('开机自启 %s', '开' if on else '关')
        return True
    except OSError as e:
        log.warning('自启设置失败: %s', e)
        return False


def setup_log():
    """滚动文件日志：单文件 1MB × 保留 2 个备份，总量恒定约 3MB（幂等）"""
    if log.handlers:
        return
    h = RotatingFileHandler(BASE.with_suffix('.log'), maxBytes=1_000_000,
                            backupCount=2, encoding='utf-8')
    h.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(threadName)s %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    threading.excepthook = lambda a: log.error(
        '线程异常', exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

KEEP = 200
MERGE_SEC = 120  # 相邻同文件修改合并的时间窗：窗口内连续保存合并防刷屏，跨会话各自成条
TS_FMT = '%y-%m-%d %H:%M'  # 事件时间戳：年月日时分（隔天可分辨）
HINT = '点事件打开 · 拖到屏幕边缘隐藏 · 右键'
TAG_CN = {1: '新增', 2: '删除', 3: '修改', 4: '改名'}
NOTIFY_MASK = (win32con.FILE_NOTIFY_CHANGE_FILE_NAME
               | win32con.FILE_NOTIFY_CHANGE_LAST_WRITE
               | win32con.FILE_NOTIFY_CHANGE_SIZE
               | 0x40)  # FILE_NOTIFY_CHANGE_CREATION，本版 win32con 未收录
# 系统与软件后台噪音（小写子串匹配）；用户可在 syspet.json 的 "exclude" 数组追加
NOISE = ('c:\\windows\\', 'c:\\programdata\\', 'c:\\program files\\',
         'c:\\program files (x86)\\', '\\appdata\\local\\temp\\',
         '$recycle.bin', 'system volume information', 'pagefile.sys', 'swapfile.sys',
         'hiberfil.sys', 'usnjrnl', '$logfile', '$mft', '$extend', 'ntuser.dat',
         'syspet.log', 'syspet.json')


def rr(x1, y1, x2, y2, r):
    """圆角矩形顶点序列（Tk smooth polygon 用）"""
    return (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)


def size(n):
    """字节量 → 可读串：≥100 用整数、否则 1 位小数；按显示精度舍入达 1024 即进位"""
    if n is None:
        return '—'
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if round(n) < 1024:  # 按将要显示的精度判断，杜绝 '1024 KB'
            return f'{n:.0f} {u}' if u == 'B' or n >= 99.95 else f'{n:.1f} {u}'
        n /= 1024
    return f'{n:.0f} PB'


def speed(b):
    return size(b) + '/s'


def mood(pct):
    return '#34c759' if pct < 60 else '#ff9500' if pct < 90 else '#ff3b30'




def gpu_probe():
    """nvidia-smi → (利用率%, 显存占用%, 温度℃)；无 N 卡或失败返回 (None,)*3"""
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW  # pythonw 下防控制台黑窗弹闪抢焦点
            ).stdout
        lines = out.strip().splitlines()  # nvidia-smi 在但无 GPU（WSL/虚拟机）时 stdout 为空
        if not lines:
            return None, None, None
        util, used, total, temp = (int(x) for x in lines[0].split(','))
        return util, round(100 * used / total), temp
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError):
        return None, None, None


CFG_AI = BASE.with_suffix('.json')
_excludes, _only = NOISE, ()


def _user_dirs():
    """注册表读真实桌面/文档/下载路径（含重定向，如桌面挪到 E:\桌面）"""
    dirs = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders') as k:
            for n in ('Desktop', 'Personal', '{374DE290-123F-4565-9164-39C4925E467B}'):
                try:
                    dirs.append(os.path.expandvars(winreg.QueryValueEx(k, n)[0]))
                except OSError:
                    pass
    except OSError:
        pass
    return tuple(d.lower().replace('/', '\\').rstrip('\\') + '\\' for d in dirs)


def load_excludes():
    """加载排除/白名单规则：填了 only 只显示名单内目录；
    未填则默认只看用户文件区（应用后台目录无边界，黑名单枚举不完）"""
    global _excludes, _only
    try:
        cfg = json.loads(CFG_AI.read_text('utf-8'))
    except (OSError, ValueError):
        cfg = {}
    _excludes = NOISE + tuple(x.lower() for x in cfg.get('exclude', []))
    only_cfg = [x for x in cfg.get('only', []) if x]
    _only = (tuple(x.lower().replace('/', '\\').rstrip('\\') + '\\' for x in only_cfg)
             if only_cfg else _user_dirs())


load_excludes()


def enc(s):
    """DPAPI 加密（绑定当前 Windows 用户+本程序熵）→ 'dpapi:'+base64；空值原样返回"""
    if not s:
        return s
    blob = win32crypt.CryptProtectData(s.encode(), 'syspet', b'syspet', None, None, 0)
    return 'dpapi:' + base64.b64encode(blob).decode()


def dec(s):
    """解密 enc() 结果；旧明文原样返回（兼容）；跨机器/用户解不开返回 ''（需重填）"""
    if not s or not s.startswith('dpapi:'):
        return s
    try:
        return win32crypt.CryptUnprotectData(
            base64.b64decode(s[6:]), b'syspet', None, None, 0)[1].decode()
    except Exception:
        log.warning('密钥解密失败（换机器/用户后需重新填写）')
        return ''


def provs_plain(provs):
    """配置里的 key/token 解密为明文（供网络查询与表单显示）"""
    for p in provs:
        for k in ('key', 'token'):
            if p.get(k):
                p[k] = dec(p[k])
    return provs


def upgrade_keys():
    """旧版明文 key/令牌启动时自动升级为 DPAPI 密文，明文不再留存"""
    try:
        cfg = json.loads(CFG_AI.read_text('utf-8'))
    except (OSError, ValueError):
        return
    changed = False
    for p in cfg.get('providers', []):
        for k in ('key', 'token'):
            if p.get(k) and not p[k].startswith('dpapi:'):
                p[k] = enc(p[k])
                changed = True
    if changed:
        cfg_write(cfg)
        log.info('检测到明文密钥，已自动升级为 DPAPI 密文')


upgrade_keys()


def test_provider(url, key, token=''):
    """测试连接 → 状态文本（按协议自动识别）"""
    p = {'url': url, 'key': key, 'token': token}
    if 'dashscope.aliyuncs.com' in url:
        if token:
            return ali_usage(p) or '失败：无法连接阿里云 BSS'
        return '百炼官方查余额需 RAM AccessKey（Key=AccessKeyId，令牌=Secret，需 BSS 只读权限）'
    if 'bigmodel.cn' in url or 'z.ai' in url:
        r = zhipu_usage(p)
        return r or '失败：无法获取智谱用量（无 Coding Plan 订阅或 Key 无效）'
    if 'deepseek.com' in url:
        return deepseek_usage(p) or '失败：无法获取 DeepSeek 余额'
    try:
        base = url.rstrip('/')
        sub = http_json(base + '/v1/dashboard/billing/subscription', key, timeout=6)
        lim = sub.get('hard_limit_usd')
        if lim is None:
            return '已连通，但未返回额度（该平台可能不支持余额查询）'
        if lim == 100000000:
            return '连接正常 · 无限额度令牌'
        return f'连接正常 · 总额度 ${lim:.2f}'
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return '失败 HTTP 401（Key 无效）'
        if e.code == 404:
            return '失败 HTTP 404（该平台无余额接口）'
        return f'失败 HTTP {e.code}'
    except Exception as e:
        return f'失败 {type(e).__name__}: {e}'


def zhipu_usage(p):
    """智谱 Coding Plan/团队版：额度百分比模型（显示用量，无余额概念）"""
    try:
        u = urlsplit(p['url'])
        req = urllib.request.Request(
            f'{u.scheme}://{u.netloc}/api/monitor/usage/quota/limit',
            headers={'Authorization': p.get('key', ''), 'Accept-Language': 'en-US,en'})
        with urllib.request.urlopen(req, timeout=6) as r:
            resp = json.loads(r.read())
        lim = next(x for x in resp['data']['limits'] if x['type'] == 'TOKENS_LIMIT')
        pct = lim.get('percentage') or 0
        reset = ''
        if lim.get('nextResetTime'):  # 接口版本差异：时间戳或 ISO 字符串
            try:
                t = lim['nextResetTime']
                dt = (datetime.datetime.fromtimestamp(t / 1000 if t > 1e12 else t)
                      if isinstance(t, (int, float)) else
                      datetime.datetime.fromisoformat(str(t).replace('Z', '+00:00')))
                same_day = dt.astimezone().date() == datetime.date.today()
                reset = ' · ' + dt.astimezone().strftime(
                    '%H:%M' if same_day else '%m-%d') + ' 重置'
            except (ValueError, OSError, OverflowError):
                pass
        lv = (resp['data'].get('level') or '').upper()
        return (f'已用 {pct:.0f}% · 剩 {100 - pct:.0f}%{reset}'
                + (f' · {lv}' if lv else ''))
    except (urllib.error.URLError, json.JSONDecodeError, OSError,
            ValueError, KeyError, StopIteration, TypeError):
        return None


def http_json(url, key, timeout=5):
    req = urllib.request.Request(url, headers={'Authorization': f'Bearer {key}'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def per_unit_of(base, key):
    """new-api 实例的 1美元=多少quota，读公开 /api/status；失败按默认 500000"""
    try:
        return http_json(f'{base}/api/status', key, timeout=4) \
            .get('data', {}).get('quota_per_unit', 500000) or 500000
    except Exception:
        return 500000


def ali_rpc(action, aid, secret, extra=None):
    """阿里云 RPC API 自签名调用（BSS 费用中心，无 SDK 依赖）"""
    p = {'Action': action, 'Format': 'JSON', 'Version': '2017-12-14',
         'AccessKeyId': aid, 'SignatureMethod': 'HMAC-SHA1',
         'SignatureNonce': uuid.uuid4().hex, 'SignatureVersion': '1.0',
         'Timestamp': datetime.datetime.now(datetime.timezone.utc)
                      .strftime('%Y-%m-%dT%H:%M:%SZ')}
    p.update(extra or {})
    pe = lambda s: urllib.parse.quote(str(s), safe='-_.~')
    q = '&'.join(f'{pe(k)}={pe(v)}' for k, v in sorted(p.items()))
    sig = base64.b64encode(hmac.new((secret + '&').encode(),
                                    ('GET&%2F&' + pe(q)).encode(),
                                    hashlib.sha1).digest()).decode()
    req = urllib.request.Request(f'https://business.aliyuncs.com/?{q}&Signature={pe(sig)}')
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def ali_usage(p):
    """阿里云百炼官方：BSS 查账户余额+本月消费（Key=AccessKeyId，令牌=AccessKeySecret）"""
    try:
        bal = float(ali_rpc('QueryAccountBalance', p.get('key', ''), p.get('token', ''))
                    ['Data'].get('AvailableAmount') or 0)
        d = ali_rpc('QueryAccountBill', p.get('key', ''), p.get('token', ''),
                    {'BillingCycle': datetime.date.today().strftime('%Y-%m'),
                     'Granularity': 'MONTHLY'}).get('Data', {})
        m = d.get('SummaryTotal') or (d.get('AccountBillList') or {}).get('SummaryTotal')
        if m is None:
            items = (d.get('AccountBillList') or {}).get('Items') or d.get('Items') or []
            m = sum(float(i.get('PretaxAmount') or 0) for i in items)
        return f'余 ¥{bal:.2f} · 本月 ¥{float(m):.2f}'
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read()).get('Code', '')
        except Exception:
            code = ''
        hint = {'InvalidAccessKeyId.NotFound': 'AccessKeyId 不存在',
                'SignatureDoesNotMatch': 'Secret 不匹配',
                'NoPermission': 'AccessKey 缺少 BSS 只读权限'}.get(code, code or f'HTTP {e.code}')
        return f'失败：{hint}'
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError, KeyError):
        return None


def newapi_account(p):
    """new-api 账号视角：/api/user/self + 控制台访问令牌（cc-switch 同款）"""
    try:
        base = (p.get('url') or '').rstrip('/')
        req = urllib.request.Request(
            base + '/api/user/self',
            headers={'Authorization': 'Bearer ' + p['token'],
                     'New-Api-User': str(p['uid'])})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read()).get('data', {})
        per = per_unit_of(base, p['token'])
        rem, used = d.get('quota', 0) / per, d.get('used_quota', 0) / per
        return f'余 ${rem:.2f} · 已用 ${used:.2f}'
    except (urllib.error.URLError, json.JSONDecodeError, OSError, ValueError):
        return None


def deepseek_usage(p):
    """DeepSeek 官方：/user/balance → CNY 余额"""
    try:
        d = http_json(p['url'].rstrip('/') + '/user/balance', p.get('key', ''))
        bal = sum(float(x.get('total_balance') or 0)
                  for x in d.get('balance_infos', []) if x.get('currency') == 'CNY')
        return f'余 ¥{bal:.2f}'
    except Exception:
        return None


def fetch_one(p):
    """查询单个供应商 → 展示文本；按地址自动识别协议（智谱 / new-api 系）
    配置了访问令牌+UID 的走账号视角，否则走 sk key 的令牌视角"""
    url = p.get('url', '')
    if 'bigmodel.cn' in url or 'z.ai' in url:
        return zhipu_usage(p)
    if 'deepseek.com' in url:  # DeepSeek 官方：余额接口
        return deepseek_usage(p) or '填写官方地址与 Key 后可查余额'
    if 'dashscope.aliyuncs.com' in url:  # 阿里百炼官方：BSS 协议
        if p.get('key') and p.get('token'):
            return ali_usage(p)
        return '填 AccessKeyId+Secret 后可查（见配置窗口说明）'
    if p.get('token') and p.get('uid'):
        r = newapi_account(p)
        if r:
            return r
        # 令牌失效则回退令牌视角
    try:
        base, key = url.rstrip('/'), p.get('key', '')
        sub = http_json(f'{base}/v1/dashboard/billing/subscription', key)
        # 官方语义：usage 为累计消费（美分）；subscription 已换算为美元
        used = http_json(f'{base}/v1/dashboard/billing/usage', key) \
            .get('total_usage', 0) / 100
        lim = sub.get('hard_limit_usd') or 0
        if lim == 100000000:  # new-api 无限额度令牌的哨兵值（源码写死）
            return f'无限额度 · 累计 ${used:.2f}'
        bal = lim - used
        return (f'余 ${bal:.2f} · 累计 ${used:.2f}' if bal >= 0
                else f'额度 ${lim:.2f} · 累计 ${used:.2f}')
    except (urllib.error.URLError, json.JSONDecodeError, OSError,
            ValueError, KeyError):
        return None


def parse_used_pct(text):
    """'已用 84% · …' → 84；无百分比语义的文本返回 None"""
    i = text.find('已用 ')
    if i < 0:
        return None
    d = text[i + 3:text.find('%', i)]
    return int(d) if d.isdigit() else None


def cfg_write(cfg):
    """原子写 syspet.json：先写 .tmp 再替换，防写入中断留下半截 JSON 丢配置。
    replace 带重试：自身监控线程做内容快照的共享读锁会让 rename 短暂失败"""
    tmp = CFG_AI.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
    for _ in range(5):
        try:
            os.replace(tmp, CFG_AI)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, CFG_AI)  # 仍失败则如实抛出


def cfg_update(**patch):
    """合并写 syspet.json 顶层键（保留 providers/exclude/only 等其余配置）"""
    try:
        cfg = json.loads(CFG_AI.read_text('utf-8'))
    except (OSError, ValueError):
        cfg = {}
    cfg.update(patch)
    try:
        cfg_write(cfg)
    except OSError:
        pass


def traffic_load():
    """流量记录 {'days': [最近 ≤7 天]}；尾项为今日，同日重启续算不清零"""
    today = time.strftime('%Y-%m-%d')
    days = []
    try:
        t = json.loads(CFG_AI.read_text('utf-8')).get('traffic') or {}
        days = t.get('days') or ([t] if t.get('date') else [])
    except (OSError, ValueError, AttributeError):
        pass
    if days and days[-1].get('date') == today:
        return {'days': days[-7:]}
    return {'days': [d for d in days if d.get('date') != today][-6:]
            + [{'date': today, 'down': 0.0, 'up': 0.0}]}


def traffic_delta(io_last, io_now):
    """相邻两次 net_io_counters 的 (Δdown, Δup)；首次无基线与
    负跳变（网卡计数重置/重连）按 0 计"""
    if io_last is None:
        return 0, 0
    return (max(0, io_now.bytes_recv - io_last.bytes_recv),
            max(0, io_now.bytes_sent - io_last.bytes_sent))


def pos_load():
    """上次窗口位置 'x,y' → (x, y)；无记录返回 None（默认右下角）"""
    try:
        s = json.loads(CFG_AI.read_text('utf-8')).get('pos', '')
        x, y = s.split(',')
        return int(x), int(y)
    except (OSError, ValueError, AttributeError):
        return None


def top_procs(kind):
    """Top5 进程 [(名, 值)]；CPU 用两段采样（各 ~0.4s）；磁盘为两段采样的 MB/s"""
    procs = list(psutil.process_iter())
    if kind == 'CPU':
        for p in procs:
            try:
                p.cpu_percent(None)  # 预热基准
            except psutil.Error:
                pass
        time.sleep(0.4)
    elif kind == '磁盘':
        def _io_sum(p):
            try:
                io = p.io_counters()
                return io.read_bytes + io.write_bytes
            except (psutil.Error, AttributeError):
                return None
        first = {p.pid: _io_sum(p) for p in procs}
        time.sleep(0.4)
        rows = []
        for p in procs:
            base, now = first.get(p.pid), _io_sum(p)
            if base is not None and now is not None and now > base:
                try:
                    rows.append((p.name(), (now - base) / 0.4 / 1048576))
                except psutil.Error:
                    pass
        rows.sort(key=lambda r: r[1], reverse=True)
        return rows[:5]
    rows = []
    for p in procs:
        try:
            v = p.cpu_percent(None) if kind == 'CPU' else p.memory_percent()
            rows.append((p.name(), v))
        except psutil.Error:
            pass
    rows.sort(key=lambda r: r[1], reverse=True)
    return rows[:5]


# ———————— 扩展功能逻辑层（仪表/网络/预测/硬件） ————————

def disk_io_speed(last, now, dt):
    """相邻两次 disk_io_counters 的 (读 B/s, 写 B/s)；不可用/首采返回 None"""
    if last is None or now is None or dt <= 0:
        return None
    return (max(0, (now.read_bytes - last.read_bytes) / dt),
            max(0, (now.write_bytes - last.write_bytes) / dt))


def month_traffic(days):
    """流量天数组中本月的 (↓, ↑) 字节汇总"""
    ym = time.strftime('%Y-%m')
    return (sum(d['down'] for d in days if d['date'][:7] == ym),
            sum(d['up'] for d in days if d['date'][:7] == ym))


_PRIVATE = ('10.', '192.168.', '127.', '169.254.', '0.0.0.0', '::1', 'fe80:')


def _is_private(ip):
    """内网/回环地址（含 172.16-31 段）"""
    if ip.startswith('172.'):
        try:
            return 16 <= int(ip.split('.')[1]) <= 31
        except (IndexError, ValueError):
            return True
    return any(ip.startswith(p) for p in _PRIVATE)


def net_procs():
    """外网 TCP 连接 [(进程, 远程 ip:port)]，最多 15 行（按进程排序）"""
    rows = []
    for c in psutil.net_connections(kind='inet'):
        if c.status != psutil.CONN_ESTABLISHED or not c.raddr:
            continue
        if _is_private(c.raddr.ip):
            continue
        try:
            name = psutil.Process(c.pid).name()
        except (psutil.Error, TypeError):
            name = '未知'
        rows.append((name, f'{c.raddr.ip}:{c.raddr.port}'))
    rows.sort()
    return rows[:15]


def disk_hist_note():
    """每日记录各盘已用量到配置（幂等：同日同盘覆盖），供满盘预测"""
    try:
        cfg = json.loads(CFG_AI.read_text('utf-8'))
    except (OSError, ValueError):
        cfg = {}
    hist = cfg.get('disk_hist', {})
    today = time.strftime('%Y-%m-%d')
    for d in drives():
        try:
            hist.setdefault(d[0], {})[today] = shutil.disk_usage(d).used
        except OSError:
            pass
    cfg_update(disk_hist=hist)
    return hist


def disk_forecast():
    """[(盘, 占用%, 预测文本)]：≥2 天占用史且在增时给出'约 N 天后满'（>365 天视为充裕不提）"""
    try:
        hist = json.loads(CFG_AI.read_text('utf-8')).get('disk_hist', {})
    except (OSError, ValueError):
        hist = {}
    out = []
    for d in drives():
        try:
            du = shutil.disk_usage(d)
            pct = 100 * du.used / du.total
        except OSError:
            continue
        pred = ''
        pts = sorted(hist.get(d[0], {}).items())
        if len(pts) >= 2:
            (d0, u0), (d1, u1) = pts[0], pts[-1]
            span = max(1, (datetime.datetime.strptime(d1, '%Y-%m-%d')
                           - datetime.datetime.strptime(d0, '%Y-%m-%d')).days)
            grow = (u1 - u0) / span  # B/天
            if grow > 0:
                left = (du.total - du.used) / grow
                if left <= 365:
                    pred = f' · 约 {left:.0f} 天后满'
        out.append((f'{d[0]}盘', pct, pred))
    return out


def smart_check():
    """硬盘 SMART 失败预测 [(盘实例, 是否预测失败)]；无数据/不支持返回 []"""
    try:
        out = subprocess.run(
            ['wmic', '/namespace:\\\\root\\wmi', 'path',
             'MSStorageDriver_FailurePredictStatus',
             'get', 'InstanceName,PredictFailure', '/format:csv'],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    heads = [l for l in lines if l.lower().startswith('node,')]
    if not heads:
        return []
    cols = heads[0].split(',')
    try:
        i_inst, i_pf = cols.index('InstanceName'), cols.index('PredictFailure')
    except ValueError:
        return []
    rows = []
    for l in lines[lines.index(heads[0]) + 1:]:
        parts = l.split(',')
        if len(parts) > max(i_inst, i_pf):
            rows.append((parts[i_inst], parts[i_pf].strip().upper() == 'TRUE'))
    return rows


def hw_info():
    """硬件信息（全只读）：CPU/内存/GPU/主板/系统；任一项拿不到就跳过"""
    out = {}
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
            out['CPU'] = winreg.QueryValueEx(k, 'ProcessorNameBuffer')[0].strip()
    except OSError:
        pass
    vm = psutil.virtual_memory()
    out['内存'] = f'{vm.total / 1073741824:.1f} GB（可用 {vm.available / 1073741824:.1f}）'
    try:
        o = subprocess.run(
            ['nvidia-smi', '--query-gpu=name,driver_version', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout.strip()
        if o:
            out['GPU'] = o.splitlines()[0].replace(',', ' ·')
    except (OSError, subprocess.SubprocessError):
        pass
    if 'GPU' not in out:  # 无 N 卡：注册表显示适配器兜底（0000 为主卡）
        base = (r'SYSTEM\CurrentControlSet\Control\Class'
                r'\{4d36e968-e325-11ce-bfc1-08002be10318}')
        for sub in ('0000', '0001'):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    base + '\\' + sub) as k:
                    out['GPU'] = winreg.QueryValueEx(k, 'Device Description')[0]
                    break
            except OSError:
                continue
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r'HARDWARE\DESCRIPTION\System\BIOS') as k:
            mk = winreg.QueryValueEx(k, 'SystemManufacturer')[0].strip()
            md = winreg.QueryValueEx(k, 'SystemProductName')[0].strip()
            out['主板'] = f'{mk} {md}'.strip()
    except OSError:
        pass
    wv = sys.getwindowsversion()
    out['系统'] = f'Windows {wv.major}.{wv.minor}.{wv.build}（{"64" if sys.maxsize > 2**32 else "32"}位）'
    return out


RELEASE_URL = 'https://github.com/HuKernel/syspet/releases/latest'


def _vtuple(v):
    try:
        return tuple(int(x) for x in v.split('.'))
    except ValueError:
        return (0,)


def check_update(events):
    """启动 10s 后查最新 Release；新版入事件流，点击打开下载页"""
    time.sleep(10)
    try:
        with urllib.request.urlopen(
                'https://api.github.com/repos/HuKernel/syspet/releases/latest',
                timeout=8) as r:
            tag = json.loads(r.read()).get('tag_name', '').lstrip('v')
        if tag and _vtuple(tag) > _vtuple(__version__):
            push_event(events, time.strftime(TS_FMT), '通知',
                       f'SysPet {tag} 可更新（点击打开下载页）', None, None, RELEASE_URL)
    except Exception:
        pass  # 离线/接口变更是常态，静默


_toast = {'ai': True, 'sys': True, 'net': True, 'usb': True, 'rest': True}
# 按类别的系统通知开关（事件流照常记录，仅控制弹窗）；键：ai/sys/net/usb/rest


def toast(title, body, cat='sys', manual=False):
    """弹 Windows 原生通知（PowerShell WinRT；后台线程执行防阻塞 UI；
    无控制台父进程的子进程 stdio 必须显式重定向，否则 PowerShell 挂起）。
    manual=True 为用户主动点击触发的报告，不走路由门控（如磁盘报告/流量历史）"""
    if not manual and not _toast.get(cat, True):
        return

    def run():
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                    r'SOFTWARE\Classes\AppUserModelId\SysPet') as k:
                winreg.SetValueEx(k, 'DisplayName', 0, winreg.REG_SZ, 'SysPet')
            esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace(
                '>', '&gt;').replace("'", "''")  # PS 单引号串内单引号需双写，兼防注入
            ps = ("[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,"
                  "ContentType=WindowsRuntime]|Out-Null;"
                  "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                  "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                  "$t=$x.GetElementsByTagName('text');"
                  f"[void]$t.Item(0).AppendChild($x.CreateTextNode('{esc(title)}'));"
                  f"[void]$t.Item(1).AppendChild($x.CreateTextNode('{esc(body)}'));"
                  "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                  "'SysPet').Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            subprocess.run(['powershell', '-NoProfile', '-Command', ps], timeout=4,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception as e:
            log.warning('通知发送失败: %r', e)
    threading.Thread(target=run, daemon=True).start()


def cpu_utility_init():
    """打开 % Processor Utility 计数器（任务管理器同口径，含频率提升因子；
    纯时间口径在高 turbo CPU 上会比任务管理器低近一半）"""
    pdh = ctypes.windll.pdh
    q, c = ctypes.c_void_p(), ctypes.c_void_p()
    if pdh.PdhOpenQueryW(None, 0, ctypes.byref(q)) != 0:
        return None
    if pdh.PdhAddEnglishCounterW(
            q, r'\Processor Information(_Total)\% Processor Utility',
            0, ctypes.byref(c)) != 0:
        pdh.PdhCloseQuery(q)
        return None
    return q, c


def cpu_utility_read(pdh):
    """读一轮 Utility（rate 计数器需两次采集，首轮返回 None）"""
    if not pdh:
        return None
    q, c = pdh

    class CV(ctypes.Structure):
        # PDH_FMT_COUNTERVALUE：DWORD 状态 + 联合体（按 double 读），联合体必须单字段
        _fields_ = [('CStatus', ctypes.c_ulong), ('DoubleValue', ctypes.c_double)]
    val = CV()
    if (ctypes.windll.pdh.PdhCollectQueryData(q) != 0
            or ctypes.windll.pdh.PdhGetFormattedCounterValue(
                c, 0x200, None, ctypes.byref(val)) != 0 or val.CStatus):
        return None
    return max(0.0, min(100.0, val.DoubleValue))


def idle_seconds():
    """系统键鼠空闲秒数（GetLastInputInfo，任一输入都刷新）——离开工位即无输入"""
    class LII(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('dwTime', ctypes.c_uint)]
    lii = LII(ctypes.sizeof(LII))
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        # dwTime 是 32 位 tick，会回绕：须同宽度相减再取无符号（64 位减 32 位回绕后恒为巨值）
        tick = ctypes.windll.kernel32.GetTickCount()
        return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000
    return 0


def screen_locked():
    """锁屏中？（锁屏时输入桌面切到 Winlogon，OpenInputDesktop 打不开）"""
    h = ctypes.windll.user32.OpenInputDesktop(0, False, 0)
    if h:
        ctypes.windll.user32.CloseDesktop(h)
        return False
    return True


def ai_net_loop(state, events):
    """供应商用量定时查询（默认 60s 一轮，配置变化 ≤5s 热生效）；
    数据版本号驱动 UI 重绘。秒级轮询易触发平台限流，勿调太低"""
    mtime, next_poll, alert, last_sig = None, 0.0, 90, None
    while True:
        try:
            m = CFG_AI.stat().st_mtime
        except OSError:
            m = None
        now = time.time()
        if m != mtime or now >= next_poll:
            mtime = m
            try:
                cfg = json.loads(CFG_AI.read_text('utf-8'))
            except (OSError, ValueError):
                cfg = {}
            # 只对比与查询相关的键：pos/traffic 落盘不会误触发全量重查（防限流）
            sig = json.dumps([cfg.get('providers'), cfg.get('exclude'),
                              cfg.get('only'), cfg.get('ai_alert')],
                             ensure_ascii=False)
            if sig != last_sig or now >= next_poll:
                last_sig = sig
                load_excludes()  # 排除规则与供应商配置同文件，一并热加载
                alert = cfg.get('ai_alert', 90) or 0  # 百分比阈值，0=禁用
                try:
                    state['rows'] = [(p.get('name', '?'), fetch_one(p))
                                     for p in provs_plain(cfg.get('providers', []))]
                except Exception:  # 手写配置缺字段等，单轮失败不杀线程
                    log.warning('AI 查询异常', exc_info=True)
                    state['rows'] = [(p.get('name', '?'), None) for p in
                                     (cfg.get('providers') or [])]
                state['ver'] += 1
                log.info('AI 余额: %s', state['rows'])
                for name, text in state['rows']:  # 用量告警：跨阈值报一次，回落重置
                    pct = parse_used_pct(text or '')
                    if not pct or pct < alert:
                        state['alerted'].discard(name)
                    elif pct >= alert and name not in state['alerted']:
                        state['alerted'].add(name)
                        push_event(events, time.strftime(TS_FMT), '告警',
                                   f'{name} 已用 {pct}%（阈值 {alert}%）',
                                   None, None, '')
                        toast('SysPet 告警', f'{name} 已用 {pct}%（阈值 {alert}%）', cat='ai')
                next_poll = time.time() + 60  # 仅真正查过才排下一轮（防拖动无限推迟轮询）
        time.sleep(5)


def gpu_loop(state):
    while True:
        state['gpu'] = gpu_probe()
        time.sleep(2)


NOTIFY_JUNK = ('新通知', '此通知的设置', '将此通知移动到操作中心', '关闭通知')


def scan_toasts(uia, root, cond, seen):
    """单轮 Toast 扫描 → [(app, 正文)]。只能在无 Tk 的进程里跑：
    Tk 进程内任何线程调 UIA FindAll 都会死锁（Win10 实测）"""
    arr = root.FindAll(2, cond)  # TreeScope_Children：桌面顶层窗口
    out = []
    for i in range(arr.Length):
        el = arr.GetElement(i)
        # 先判类名/标题再读句柄：受限系统窗口读句柄会抛 COM 错，须跳过而非毁掉整轮
        if el.CurrentClassName != 'Windows.UI.Core.CoreWindow':
            continue
        if el.CurrentName != '新通知':  # 开始菜单/通知中心等同类名窗口靠标题排除
            continue
        h = el.CurrentNativeWindowHandle
        if h in seen:
            continue
        seen.add(h)
        if len(seen) > 256:  # ponytail: 句柄集合粗暴清空，256 内无复用风险
            seen.clear()
        kids = el.FindAll(7, cond)  # Subtree 全文
        names = [kids.GetElement(j).CurrentName for j in range(kids.Length)]
        if names and names[0] == 'SysPet':
            continue  # 自己发的告警 Toast，事件流已有，防重复捕获
        names = [t for t in dict.fromkeys(names)
                 if t and t not in NOTIFY_JUNK and not t.startswith('来自')]
        if not names:
            continue
        # UIA 首个文本是来源显示名（钉钉/微信），其余是标题+正文；进程名仅兜底
        display, body = names[0], ' · '.join(names[1:])
        out.append((display, body or display))
    return out


def notify_child():
    """独立通知子进程入口：轮询 Toast，每行 'app\\t正文' 写 stdout（管道）"""
    uia = comtypes.client.CreateObject(CUIAutomation, interface=IUIAutomation)
    root, cond = uia.GetRootElement(), uia.CreateTrueCondition()
    seen = set()
    while True:
        try:
            for app, body in scan_toasts(uia, root, cond, seen):
                os.write(1, f'{app}\t{body}\n'.encode('utf-8'))
        except Exception:
            pass  # 单轮失败（锁屏/UIA 忙）跳过
        time.sleep(2)


def fsize(path):
    try:
        return os.stat(path).st_size
    except OSError:
        return None


SNAP_MAX = 32 * 1024   # 内容快照上限：超过的文件不做修改对比（大文件/二进制）
SNAP_FILES = 1000      # 快照池上限：超限整体清空（与大小 cache 同策略）


def snap_text(path, sz):
    """读小文本文件内容做"修改前"快照；超大/二进制/读失败返回 None。
    sz 为 fsize 结果：已保证 ≤SNAP_MAX 的文件读取时不会被截断。
    自身配置排除：读它会与 cfg_write 的原子替换互相卡（共享锁 vs rename）。"""
    if sz is None or sz > SNAP_MAX:
        return None
    if os.path.abspath(path).lower() == os.path.abspath(CFG_AI).lower():
        return None
    try:
        data = open(path, 'rb').read(SNAP_MAX)
    except OSError:
        return None
    if b'\x00' in data:  # 文本不含 NUL；含即二进制，防 gbk 兜底解出乱码 diff
        return None
    for enc in ('utf-8', 'gbk'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def _snap_update(snapshots, path, sz):
    """刷新快照池并返回新内容（不可快照时返回 None，不动旧值）"""
    after = snap_text(path, sz)
    if after is not None:
        snapshots[path] = after
        if len(snapshots) > SNAP_FILES:
            snapshots.clear()
    return after


def drives():
    return [p.mountpoint for p in psutil.disk_partitions()
            if p.fstype not in ('CDFS', 'UDF', '')]


def push_event(events, ts, tag, path, old, new, open_path, diff=None):
    now = time.time()
    if tag in ('通知', '告警'):  # 非文件事件，不走路径过滤（白名单模式下也要显示）
        events.insert(0, (ts, tag, path, old, new, open_path, None, now))
        del events[KEEP:]
        log.info('%s %s', tag, path)
        return
    p = open_path.lower()  # 过滤基于完整路径（改名的显示串是相对名，不可用于匹配）
    if _only and not p.startswith(_only):  # 白名单=范围，黑名单=范围内剔噪音，两者叠加
        return
    # 全部 .log 排除（应用日志高频刷写易刷屏；syspet.log 曾致"写日志→事件→再写日志"死循环）
    if p.endswith('.log') or any(x in p for x in _excludes):
        return
    if (events and events[0][1] == tag and events[0][2] == path
            and now - events[0][7] <= MERGE_SEC):  # 相邻同文件同类型且在时间窗内
        old_diff = events[0][6] if len(events[0]) > 6 else None
        if diff and old_diff:  # 连续修改：保留这串的最初 before，配最新 after
            diff = (old_diff[0] if old_diff[0] is not None else diff[0], diff[1])
        events[0] = (ts, tag, path, events[0][3] or old, new, open_path,
                     diff or old_diff, now)
    else:
        events.insert(0, (ts, tag, path, old, new, open_path, diff, now))
        del events[KEEP:]
    log.info('%s %s  %s → %s', tag, path, size(old), size(new))


def _drop_events(events, *pairs):
    """从事件流移除已被折叠的中间步骤卡片（tag, 路径串）"""
    for tag, p in pairs:
        for i, e in enumerate(events):
            if e[1] == tag and e[2] == p:
                del events[i]
                break


def handle_batch(batch, drive, events, cache, pending, snapshots=None,
                 evicted=None, doomed=None):
    """一批通知 [(action, 盘内相对路径)] → 事件；cache 记录路径上次大小作'前快照'；
    snapshots 跨批持久，记小文本文件上次内容（点击修改事件可看前后对比）；
    pending 跨批暂存改名旧名，等新名到达配对（内核洪峰时 4/5 会被拆开送达）；
    evicted/doomed 服务 Office 替换式保存的折叠（见 a==5 分支）"""
    if snapshots is None:
        snapshots = {}
    if evicted is None:   # {原路径: (让位临时名, 旧大小, 旧快照, 时刻)}
        evicted = {}
    if doomed is None:    # {待清理临时名: 时刻}——折叠后旧文件的删除不再单独报
        doomed = {}
    ts = time.strftime(TS_FMT)
    now = time.time()
    for old in [k for k, t in pending.items() if now - t > 10]:
        del pending[old]  # 等不到新名的孤立旧名，放弃
    for k in [k for k, (_, _, _, t) in evicted.items() if now - t > 10] + \
             [k for k, t in doomed.items() if now - t > 10]:
        evicted.pop(k, None)
        doomed.pop(k, None)
    for a, name in batch:
        if name.startswith('~$'):  # Office 锁文件（开/关文档时增删）：纯噪音
            continue
        path = drive + name
        diff = None
        if a == 4:                     # 改名旧名先到，暂存等配对
            pending[path] = now
            continue
        if a == 5:                     # 新名到达，与最早暂存的旧名配对（FIFO）
            old_path = next(iter(pending), None)
            if old_path is not None:
                del pending[old_path]
                ev = evicted.get(path)
                if ev and now - ev[3] <= 10:
                    # Office 替换式保存：写临时文件 → 原文件改名让位 → 临时文件
                    # 顶上原名 → 删旧。净效果 = 原文件被修改，折叠成一条"修改"
                    # （含前后内容对比），并撤掉已入流的"让位改名/临时新增"卡片
                    tmp, old_sz, old_snap, _ = ev
                    evicted.pop(path, None)
                    new_sz = fsize(path)
                    after = _snap_update(snapshots, path, new_sz)
                    if old_snap is not None or after is not None:
                        diff = (old_snap, after)
                    push_event(events, ts, '修改', path, old_sz, new_sz, path, diff)
                    _drop_events(events,
                                 ('改名', f'{path[len(drive):]} → {tmp[len(drive):]}'),
                                 ('新增', old_path), ('新增', tmp))
                    doomed[tmp] = now  # 随后旧文件的删除是内部清理，不报
                    cache[path] = new_sz
                    continue
                old_sz, new_sz = cache.pop(old_path, None), fsize(path)
                cache[path] = new_sz
                snap = snapshots.pop(old_path, None)  # 快照随改名迁移
                after = _snap_update(snapshots, path, new_sz)
                if after is None and snap is not None:
                    snapshots[path] = snap
                evicted[old_path] = (path, old_sz, snap, now)  # 让位记录，等顶替
                push_event(events, ts, '改名',
                           f'{old_path[len(drive):]} → {name}', old_sz, new_sz, path)
                continue
            a = 1                      # 等不到旧名，按新增降级
        if a == 1:
            old, new = None, fsize(path)
            _snap_update(snapshots, path, new)  # 新增即建基线，首次修改便有 before
        elif a == 3:
            if os.path.isdir(path):  # 目录条目变化无信息量，内容变化会单独报
                continue
            old, new = cache.get(path), fsize(path)
            before = snapshots.get(path)
            after = _snap_update(snapshots, path, new)
            if before is not None or after is not None:
                diff = (before, after)
        elif a == 2:
            if path in doomed:        # 替换保存的旧文件清理：内部步骤不报
                doomed.pop(path, None)
                cache.pop(path, None)
                continue
            old, new = cache.pop(path, None), None
            snapshots.pop(path, None)
        else:
            continue
        cache[path] = new
        if len(cache) > 50000:  # 全盘长跑上限：超限整体清空（丢失的仅前后大小快照）
            cache.clear()
        push_event(events, ts, TAG_CN[a], path, old, new, path, diff)


def listen_drive(drive, events, cache, snapshots):
    """单盘内核通知监听（阻塞式），盘移除或打开失败则退出，由管理线程重启"""
    evicted, doomed = {}, {}
    try:
        h = win32file.CreateFileW(
            drive, 0x1,  # FILE_LIST_DIRECTORY，本版 win32file 未收录
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32file.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    except pywintypes.error as e:
        log.warning('%s 打开失败: %s', drive, e)
        return
    pending = {}
    try:
        while True:
            try:
                batch = win32file.ReadDirectoryChangesW(h, 65536, True, NOTIFY_MASK, None, None)
            except pywintypes.error as e:
                if e.winerror == 514:  # 通知洪峰缓冲溢出，丢弃本批继续
                    log.warning('%s 通知溢出，丢弃一批', drive)
                    continue
                log.warning('%s 监听退出: %s', drive, e)
                return
            handle_batch(batch, drive, events, cache, pending, snapshots,
                         evicted, doomed)
    finally:
        win32file.CloseHandle(h)  # 退出路径释放目录句柄


def net_ifaces():
    """当前 up 的物理网络接口名集合（含 WiFi/以太，排除回环）"""
    return frozenset(n for n, s in psutil.net_if_stats().items()
                     if s.isup and 'Loopback' not in n and n != 'lo')


_nlm = None


def net_connected():
    """NLM（Network List Manager）联网判定——任务栏网络图标同源，
    覆盖断 WiFi/切网场景；COM 失败按有网计，避免误报"""
    global _nlm
    try:
        if _nlm is None:
            import comtypes.client
            _nlm = comtypes.client.CreateObject(
                '{DCB00C01-570F-4A9B-8D69-199FDBA5723B}')
        return bool(_nlm.IsConnectedToInternet)
    except Exception:
        return True


def drive_manager(events, cache):
    watched, last, snapshots = {}, set(), {}
    while True:
        ds = set(drives())
        if last:  # 存储设备插拔（U 盘/新卷挂载），与文件监控同流
            ts = time.strftime(TS_FMT)
            for d in sorted(ds - last):
                push_event(events, ts, '通知', f'U 盘/存储已插入 {d}', None, None, '')
                toast('SysPet', f'U 盘/存储已插入 {d}', cat='usb')
            for d in sorted(last - ds):
                push_event(events, ts, '通知', f'存储已移除 {d}', None, None, '')
                toast('SysPet', f'存储已移除 {d}', cat='usb')
        last = ds
        for d in ds:
            t = watched.get(d)
            if t is None or not t.is_alive():
                t = threading.Thread(target=listen_drive,
                                     args=(d, events, cache, snapshots), daemon=True)
                watched[d] = t
                t.start()
                log.info('开始监听 %s', d)
        time.sleep(10)


def split_ext(path):
    """路径 → (主体, 后缀含点)；无合法后缀（太长/含空格斜杠）时后缀为空"""
    base, dot, ext = path.rpartition('.')
    ok = dot and len(ext) <= 6 and not any(c in ext for c in ' \\/:')
    return (base, '.' + ext) if ok else (path, '')


def clip_path(path, limit=44):
    """超长路径截中段保头尾，后缀与目录线索都可见"""
    return path if len(path) <= limit else path[:22] + ' … ' + path[-18:]


def ev_text(ev):
    ts, tag, path, old, new = ev[:5]
    sz = '' if tag in ('通知', '告警') else f'  {size(old)} → {size(new)}'
    return f'{ts} {tag} {clip_path(path)}{sz}'


