"""SysPet — 桌宠式系统监控悬浮窗：CPU/GPU/内存/磁盘/网速 + 全盘文件变化 + AI 平台用量。
依赖: psutil, pywin32（见 requirements.txt）"""
import base64, ctypes, datetime, hashlib, hmac, json, logging, os, shutil, subprocess
import sys, threading, time, tkinter as tk, uuid, winreg
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
__version__ = '1.5.0'
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
    """滚动文件日志：单文件 1MB × 保留 2 个备份，总量恒定约 3MB"""
    h = RotatingFileHandler(BASE.with_suffix('.log'), maxBytes=1_000_000,
                            backupCount=2, encoding='utf-8')
    h.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(threadName)s %(message)s'))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    threading.excepthook = lambda a: log.error(
        '线程异常', exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

KEEP = 200
HINT = '点击记录打开文件 · 滚轮翻看 · 拖动 · 右键'
BG, FG, DIM = '#f6f5f2', '#3d3a37', '#807b76'
TRACK, ACCENT = '#e7e4df', '#e8862e'
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
         'syspet.log')


def rr(x1, y1, x2, y2, r):
    """圆角矩形顶点序列（Tk smooth polygon 用）"""
    return (x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1)


def size(n):
    if n is None:
        return '—'
    for u in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.0f} {u}'
        n /= 1024
    return f'{n:.0f} PB'


def speed(b):
    return size(b) + '/s'


def mood(pct):
    return '#7fbf8e' if pct < 60 else '#eebe55' if pct < 90 else '#e98181'


class Gauge:
    """名称 + 圆角进度条 + 数值 三列仪表，col 为起始列（双列布局）"""
    W, H, R = 62, 9, 3

    def __init__(self, parent, name, row, col, font):
        tk.Label(parent, text=name, bg=BG, fg=DIM, font=font, width=4,
                 anchor='w').grid(row=row, column=col, sticky='w')
        self.cv = tk.Canvas(parent, width=self.W, height=self.H, bg=BG,
                            highlightthickness=0, bd=0)
        self.cv.grid(row=row, column=col + 1, padx=(2, 5))
        self.val = tk.Label(parent, text='—', bg=BG, fg=FG, font=font,
                            width=4, anchor='e')
        self.val.grid(row=row, column=col + 2, sticky='e')

    def set(self, pct, color):
        self.cv.delete('all')
        w, h, r = self.W, self.H, self.R
        self.cv.create_polygon(*rr(0, 0, w, h, r), fill=TRACK, smooth=True)
        if pct is not None:
            fw = max(r + 1, round(w * pct / 100))
            self.cv.create_polygon(*rr(0, 0, fw, h, r), fill=color, smooth=True)
            self.val.config(text=f'{pct:3.0f}%')
        else:
            self.val.config(text='—')


def gpu_probe():
    """nvidia-smi → (利用率%, 显存占用%)；无 N 卡或查询失败返回 (None, None)"""
    try:
        out = subprocess.run(
            ['nvidia-smi', '--query-gpu=utilization.gpu,memory.used,memory.total',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=3,
            creationflags=subprocess.CREATE_NO_WINDOW  # pythonw 下防控制台黑窗弹闪抢焦点
            ).stdout
        util, used, total = (int(x) for x in out.strip().splitlines()[0].split(','))
        return util, round(100 * used / total)
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError):
        return None, None


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
        CFG_AI.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
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
            headers={'Authorization': p['key'], 'Accept-Language': 'en-US,en'})
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
        bal = float(ali_rpc('QueryAccountBalance', p['key'], p['token'])
                    ['Data'].get('AvailableAmount') or 0)
        d = ali_rpc('QueryAccountBill', p['key'], p['token'],
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
        base = p['url'].rstrip('/')
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


def fetch_one(p):
    """查询单个供应商 → 展示文本；按地址自动识别协议（智谱 / new-api 系）
    配置了访问令牌+UID 的走账号视角，否则走 sk key 的令牌视角"""
    url = p.get('url', '')
    if 'bigmodel.cn' in url or 'z.ai' in url:
        return zhipu_usage(p)
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
        base, key = url.rstrip('/'), p['key']
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


def ai_net_loop(state):
    """监测配置文件变化（≤5 秒）即重查全部供应商；数据版本号驱动 UI 重绘"""
    mtime = None
    while True:
        try:
            m = CFG_AI.stat().st_mtime
        except OSError:
            m = None
        if m != mtime:
            mtime = m
            load_excludes()  # 排除规则与供应商配置同文件，一并热加载
            try:
                provs = json.loads(CFG_AI.read_text('utf-8')).get('providers', [])
            except (OSError, ValueError):
                provs = []
            state['rows'] = [(p.get('name', '?'), fetch_one(p))
                             for p in provs_plain(provs)[:3]]
            state['ver'] += 1
            log.info('AI 余额: %s', state['rows'])
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


def drives():
    return [p.mountpoint for p in psutil.disk_partitions()
            if p.fstype not in ('CDFS', 'UDF', '')]


def push_event(events, ts, tag, path, old, new, open_path):
    if tag == '通知':  # 通知非文件事件，不走路径过滤（白名单模式下也要显示）
        events.insert(0, (ts, tag, path, old, new, open_path))
        del events[KEEP:]
        log.info('%s %s', tag, path)
        return
    p = open_path.lower()  # 过滤基于完整路径（改名的显示串是相对名，不可用于匹配）
    if _only and not p.startswith(_only):  # 白名单=范围，黑名单=范围内剔噪音，两者叠加
        return
    # 全部 .log 排除（应用日志高频刷写易刷屏；syspet.log 曾致"写日志→事件→再写日志"死循环）
    if p.endswith('.log') or any(x in p for x in _excludes):
        return
    if events and events[0][1] == tag and events[0][2] == path:  # 相邻同文件同类型合并
        events[0] = (ts, tag, path, events[0][3] or old, new, open_path)
    else:
        events.insert(0, (ts, tag, path, old, new, open_path))
        del events[KEEP:]
    log.info('%s %s  %s → %s', tag, path, size(old), size(new))


def handle_batch(batch, drive, events, cache, pending):
    """一批通知 [(action, 盘内相对路径)] → 事件；cache 记录路径上次大小作'前快照'；
    pending 跨批暂存改名旧名，等新名到达配对（内核洪峰时 4/5 会被拆开送达）"""
    ts = time.strftime('%H:%M:%S')
    now = time.time()
    for old in [k for k, t in pending.items() if now - t > 10]:
        del pending[old]  # 等不到新名的孤立旧名，放弃
    for a, name in batch:
        path = drive + name
        if a == 4:                     # 改名旧名先到，暂存等配对
            pending[path] = now
            continue
        if a == 5:                     # 新名到达，与最早暂存的旧名配对（FIFO）
            old_path = next(iter(pending), None)
            if old_path is not None:
                del pending[old_path]
                old_sz, new_sz = cache.pop(old_path, None), fsize(path)
                cache[path] = new_sz
                push_event(events, ts, '改名',
                           f'{old_path[len(drive):]} → {name}', old_sz, new_sz, path)
                continue
            a = 1                      # 等不到旧名，按新增降级
        if a == 1:
            old, new = None, fsize(path)
        elif a == 3:
            if os.path.isdir(path):  # 目录条目变化无信息量，内容变化会单独报
                continue
            old, new = cache.get(path), fsize(path)
        elif a == 2:
            old, new = cache.pop(path, None), None
        else:
            continue
        cache[path] = new
        # ponytail: cache 不设上限，被改过的文件路径集合有限，长期运行可控
        push_event(events, ts, TAG_CN[a], path, old, new, path)


def listen_drive(drive, events, cache):
    """单盘内核通知监听（阻塞式），盘移除或打开失败则退出，由管理线程重启"""
    try:
        h = win32file.CreateFileW(
            drive, 0x1,  # FILE_LIST_DIRECTORY，本版 win32file 未收录
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
            None, win32file.OPEN_EXISTING, win32con.FILE_FLAG_BACKUP_SEMANTICS, None)
    except pywintypes.error as e:
        log.warning('%s 打开失败: %s', drive, e)
        return
    pending = {}
    while True:
        try:
            batch = win32file.ReadDirectoryChangesW(h, 65536, True, NOTIFY_MASK, None, None)
        except pywintypes.error as e:
            if e.winerror == 514:  # 通知洪峰缓冲溢出，丢弃本批继续
                log.warning('%s 通知溢出，丢弃一批', drive)
                continue
            log.warning('%s 监听退出: %s', drive, e)
            return
        handle_batch(batch, drive, events, cache, pending)


def drive_manager(events, cache):
    watched = {}
    while True:
        for d in drives():
            t = watched.get(d)
            if t is None or not t.is_alive():
                t = threading.Thread(target=listen_drive, args=(d, events, cache), daemon=True)
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
    sz = '' if tag == '通知' else f'  {size(old)} → {size(new)}'
    return f'{ts} {tag} {clip_path(path)}{sz}'


class AICfg(tk.Toplevel):
    """AI 供应商配置窗口：亮色现代风（独立于主悬浮窗的深色桌宠主题），保存写 syspet.json"""
    WBG, WFG, DIM, LINE, ACC = '#f8fafc', '#1e293b', '#64748b', '#e2e8f0', '#2563eb'

    def __init__(self, master):
        super().__init__(master)
        self.title('AI 供应商配置')
        self.configure(bg=self.WBG)
        try:
            self.provs = provs_plain(json.loads(CFG_AI.read_text('utf-8'))
                                     .get('providers', []))
        except (OSError, ValueError):
            self.provs = []
        self.sel = None
        F = 'Microsoft YaHei UI'

        top = tk.Frame(self, bg=self.WBG)
        top.pack(fill='x', padx=20, pady=(16, 10))
        tk.Label(top, text='AI 供应商', bg=self.WBG, fg=self.WFG,
                 font=(F, 13, 'bold')).pack(anchor='w')
        tk.Label(top, text='添加并管理你的 AI 平台密钥，保存后自动生效', bg=self.WBG,
                 fg=self.DIM, font=(F, 9)).pack(anchor='w', pady=(2, 0))

        c1 = self._card()
        self.lb = tk.Listbox(c1, bg='#ffffff', fg=self.WFG, bd=0, width=48, height=4,
                             font=(F, 10), highlightthickness=1,
                             highlightbackground=self.LINE, highlightcolor=self.ACC,
                             selectbackground='#dbeafe', selectforeground='#1e40af',
                             activestyle='none', exportselection=False)
        self.lb.pack(fill='x', padx=12, pady=(12, 6))
        self.lb.bind('<<ListboxSelect>>', self._pick)
        btn2 = dict(bg='#ffffff', fg='#475569', activebackground='#f1f5f9',
                    activeforeground=self.WFG, font=(F, 9), bd=0, padx=14, pady=4,
                    cursor='hand2', highlightthickness=1, highlightbackground='#cbd5e1')
        brow = tk.Frame(c1, bg='#ffffff')
        brow.pack(fill='x', padx=12, pady=(0, 12))
        tk.Button(brow, text='＋ 新增', command=self._add, **btn2).pack(side='left')
        tk.Button(brow, text='删除', command=self._del, **btn2).pack(side='left', padx=(8, 0))

        c2 = self._card()
        self.e_name = self._field(c2, '名称', F)
        self.e_url = self._field(c2, '地址', F)
        self.e_key = self._field(c2, 'Key', F, secret=True)          # 凭据框 ● 遮罩
        self.e_token = self._field(c2, '访问令牌（可选）', F, secret=True)
        self.e_uid = self._field(c2, '用户 UID（可选，需与令牌同时填）', F)
        self.btn_test = tk.Button(c2, text='测试连接', command=self._test, **btn2)
        self.btn_test.pack(anchor='w', padx=12, pady=(10, 12))

        bot = tk.Frame(self, bg=self.WBG)
        bot.pack(fill='x', padx=20, pady=(6, 16))
        self.status = tk.Label(bot, bg=self.WBG, fg=self.DIM, font=(F, 9), anchor='w',
                               text='密钥经 Windows DPAPI 加密存储，仅本机当前用户可解')
        self.status.pack(side='left', fill='x', expand=True)
        tk.Button(bot, text='保存并关闭', command=self._save, bg=self.ACC, fg='#ffffff',
                  activebackground='#1d4ed8', activeforeground='#ffffff',
                  font=(F, 10, 'bold'), bd=0, padx=18, pady=5,
                  cursor='hand2').pack(side='right')
        self._refresh()

    def _card(self):
        c = tk.Frame(self, bg='#ffffff', highlightthickness=1,
                     highlightbackground=self.LINE)
        c.pack(fill='x', padx=20, pady=4)
        return c

    def _field(self, parent, label, f, secret=False):
        tk.Label(parent, text=label, bg='#ffffff', fg=self.DIM, font=(f, 9)
                 ).pack(anchor='w', padx=12, pady=(10, 2))
        e = tk.Entry(parent, bg='#ffffff', fg=self.WFG, bd=0, width=44, font=(f, 10),
                     insertbackground=self.WFG, show='●' if secret else '',
                     highlightthickness=1, highlightbackground=self.LINE,
                     highlightcolor=self.ACC)  # 聚焦时边框变蓝
        e.pack(fill='x', padx=12)
        return e

    def _refresh(self):
        self.lb.delete(0, 'end')
        for p in self.provs:
            k = p.get('key', '')
            self.lb.insert('end', f"{p.get('name', '?')}   {p.get('url', '')}   {k[:5]}***")
        if self.sel is not None and 0 <= self.sel < len(self.provs):
            self.lb.selection_set(self.sel)

    def _pick(self, _e=None):
        self._sync()
        sel = self.lb.curselection()
        self.sel = sel[0] if sel else None
        if self.sel is not None:
            p = self.provs[self.sel]
            for e, v in ((self.e_name, p.get('name', '')),
                         (self.e_url, p.get('url', '')),
                         (self.e_key, p.get('key', '')),
                         (self.e_token, p.get('token', '')),
                         (self.e_uid, p.get('uid', ''))):
                e.delete(0, 'end')
                e.insert(0, v)

    def _sync(self):
        """当前表单值写回编辑中的条目"""
        if self.sel is not None and self.sel < len(self.provs):
            p = {'name': self.e_name.get().strip(),
                 'url': self.e_url.get().strip(),
                 'key': self.e_key.get().strip()}
            for k, e in (('token', self.e_token), ('uid', self.e_uid)):
                if e.get().strip():
                    p[k] = e.get().strip()
            self.provs[self.sel] = p

    def _add(self):
        self._sync()
        p = {'name': '新供应商', 'url': 'https://', 'key': ''}
        self.provs.append(p)
        self.sel = len(self.provs) - 1
        self._refresh()
        self._pick()

    def _del(self):
        if self.sel is not None and self.sel < len(self.provs):
            del self.provs[self.sel]
            self.sel = None
        self._refresh()

    def _test(self):
        self._sync()
        url, key = self.e_url.get().strip(), self.e_key.get().strip()
        tok, uid = self.e_token.get().strip(), self.e_uid.get().strip()
        self.status.config(text='测试中…', fg=DIM)
        self.btn_test.config(state='disabled')

        def run():  # 有账号凭据优先测账号视角，否则测 billing
            if tok and uid:
                r = newapi_account({'url': url, 'token': tok, 'uid': uid}) \
                    or '账号令牌无效（回退按 Key 查询令牌额度）'
            else:
                r = test_provider(url, key, tok)
            self.after(0, lambda: (self.status.config(
                text=r, fg='#16a34a' if r.startswith(('连接', '余')) else '#dc2626'),
                self.btn_test.config(state='normal')))
        threading.Thread(target=run, daemon=True).start()

    def _save(self):
        self._sync()
        for p in self.provs:
            if not (p['name'] and p['url'].startswith('http') and p['key']):
                self.status.config(text='有条目未填全（名称 / 地址需 http 开头 / Key）',
                                   fg='#dc2626')
                return
            if bool(p.get('token')) != bool(p.get('uid')):
                self.status.config(text='「访问令牌」与「用户 UID」需同时填写（或都留空）',
                                   fg='#dc2626')
                return
        try:
            cfg = json.loads(CFG_AI.read_text('utf-8'))
        except (OSError, ValueError):
            cfg = {}
        for p in self.provs:  # key/令牌加密落盘，明文不再留在 syspet.json
            p['key'] = enc(p['key'])
            if p.get('token'):
                p['token'] = enc(p['token'])
        cfg['providers'] = self.provs  # 只更新 providers，保留手写的 exclude/only
        CFG_AI.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
        log.info('AI 配置已保存，共 %d 个供应商', len(self.provs))
        self.destroy()  # 主窗后台线程 ≤5 秒内自动加载新配置


class Pet:
    def __init__(self):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        setup_log()
        log.info('SysPet v%s 启动，盘符: %s', __version__, ', '.join(drives()))
        self.events = []
        self._shown = 0
        self._ctx = 0
        self.ai_state = {'rows': None, 'ver': 0}
        self._ai_rows, self._ai_ver = [], -1
        self._io, self._t = None, time.time()
        self.gpu_state = {'gpu': (None, None)}
        threading.Thread(target=drive_manager, args=(self.events, {}),
                         daemon=True).start()
        threading.Thread(target=gpu_loop, args=(self.gpu_state,),
                         daemon=True).start()
        threading.Thread(target=ai_net_loop, args=(self.ai_state,),
                         daemon=True).start()
        # 通知捕获：独立子进程轮询 UIA（Tk 进程内调 UIA 必死锁），管道行协议回传
        self._nproc = None
        if CUIAutomation:
            cmd = [sys.executable] + ([] if getattr(sys, 'frozen', False)
                                       else [str(Path(__file__).resolve())]) + ['--notify']
            try:
                self._nproc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                threading.Thread(target=self._read_notify, daemon=True).start()
            except OSError as e:
                log.warning('通知子进程启动失败: %s', e)
        # 先按配置渲染占位行，数据到达后仅替换文本，窗口不跳变
        try:
            _p = json.loads(CFG_AI.read_text('utf-8')).get('providers', [])
        except (OSError, ValueError):
            _p = []
        self.ai_state['rows'] = [(x.get('name', '?'), '…') for x in _p[:3]]
        self.ai_state['ver'] += 1

        r = self.root = tk.Tk()
        r.report_callback_exception = lambda *a: log.error('UI 回调异常', exc_info=a)
        r.overrideredirect(True)
        r.attributes('-topmost', True, '-alpha', 0.97)
        r.configure(bg=BG)
        f, e_f = ('Microsoft YaHei UI', 9), ('Microsoft YaHei UI', 9)

        head = tk.Frame(r, bg='#efece6')
        head.pack(fill='x', ipady=4)
        tk.Label(head, text='SysPet', bg='#efece6', fg=ACCENT,
                 font=('Microsoft YaHei UI', 10, 'bold')).pack(side='left', padx=(12, 6))
        hf = ('Microsoft YaHei UI', 9)
        self.c_down = tk.Label(head, bg='#efece6', fg=FG, font=hf, anchor='w')
        self.c_down.pack(side='left')
        self.c_up = tk.Label(head, bg='#efece6', fg=DIM, font=hf, anchor='w')
        self.c_up.pack(side='left', padx=(8, 0))
        self.c_clk = tk.Label(head, bg='#efece6', fg=DIM, font=hf)
        self.c_clk.pack(side='right', padx=(0, 12))

        sysf = tk.Frame(r, bg=BG)
        sysf.pack(fill='x', padx=10, pady=(6, 2))
        self.g_cpu = Gauge(sysf, 'CPU', 0, 0, f)
        self.g_gpu = Gauge(sysf, 'GPU', 0, 3, f)
        self.g_mem = Gauge(sysf, '内存', 1, 0, f)
        self.g_vram = Gauge(sysf, '显存', 1, 3, f)
        self.g_dsk = Gauge(sysf, 'C盘', 2, 0, f)
        self.ai_f = tk.Frame(r, bg=BG)  # AI 用量行独立紧凑布局，不受仪表列宽影响
        self.ai_f.pack(fill='x', padx=10, pady=(2, 0))

        tk.Frame(r, bg=TRACK, height=1).pack(fill='x', padx=10, pady=(2, 4))
        self.txt = tk.Text(r, bg=BG, fg=FG, font=e_f, bd=0, height=4, width=42,
                           highlightthickness=0, wrap='none', spacing1=2, spacing3=2,
                           state='disabled', cursor='arrow')
        self.txt.pack(fill='both', expand=True, padx=10)
        self.txt.tag_config('ts', foreground=DIM)
        self.txt.tag_config('ext', foreground='#c99a5b')
        for t, c in (('新增', '#6aaa7f'), ('删除', '#e07a7a'),
                     ('修改', '#d9a94e'), ('改名', '#7d9ec7'),
                     ('通知', '#a78bb7')):
            self.txt.tag_config(f't-{t}', foreground=c)
        for t in ('ts', 'ev', 'ext', 't-新增', 't-删除', 't-修改', 't-改名', 't-通知'):
            self.txt.tag_bind(t, '<Button-1>', self._open_ev)
            self.txt.tag_bind(t, '<Button-3>', self._ctx_ev)

        self.ev_menu = tk.Menu(r, tearoff=0)
        self.ev_menu.add_command(label='打开', command=lambda: self._launch('open'))
        self.ev_menu.add_command(label='打开方式…', command=lambda: self._launch('as'))
        self.ev_menu.add_command(label='在文件夹中显示', command=lambda: self._launch('show'))

        self.hint = tk.Label(r, bg=BG, fg=DIM, font=('Microsoft YaHei UI', 8),
                             anchor='w', width=44, text=HINT)
        self.hint.pack(fill='x', padx=12, pady=(2, 6))

        menu = tk.Menu(r, tearoff=0)
        menu.add_command(label='配置 AI 供应商…', command=self._cfg_ai)
        self.auto_var = tk.BooleanVar(value=autostart_enabled())
        menu.add_checkbutton(label='开机自启动', variable=self.auto_var,
                             command=self._toggle_autostart)
        menu.add_separator()
        menu.add_command(label='退出', command=r.destroy)
        r.bind('<Button-3>', lambda e: menu.tk_popup(e.x_root, e.y_root))
        r.bind('<Button-1>', self._grab)
        r.bind('<B1-Motion>', self._drag)
        self.tick()  # 先填充文本撑出真实尺寸，再定位；geometry 调用后会冻结自动伸缩
        r.update_idletasks()
        r.geometry(f'+{r.winfo_screenwidth() - r.winfo_width() - 16}'
                   f'+{r.winfo_screenheight() - r.winfo_height() - 56}')

    def _read_notify(self):
        """读通知子进程 stdout：每行 'app\t正文' → 事件流"""
        for raw in self._nproc.stdout:
            line = raw.decode('utf-8').rstrip('\n')
            if not line:
                continue
            app, _, body = line.partition('\t')
            push_event(self.events, time.strftime('%H:%M:%S'), '通知',
                       f'{app} {body}' if app else body, None, None, '')

    def _grab(self, e):
        self._d = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag(self, e):
        self.root.geometry(f'+{e.x_root - self._d[0]}+{e.y_root - self._d[1]}')

    def tick(self):
        cpu = psutil.cpu_percent()
        mem = psutil.virtual_memory().percent
        du = shutil.disk_usage('C:\\')
        dsk = 100 * du.used / du.total
        io, t = psutil.net_io_counters(), time.time()
        dt = t - self._t
        down = (io.bytes_recv - self._io.bytes_recv) / dt if self._io else 0
        up = (io.bytes_sent - self._io.bytes_sent) / dt if self._io else 0
        self._io, self._t = io, t

        self.g_cpu.set(cpu, mood(cpu))
        self.g_mem.set(mem, mood(mem))
        self.g_dsk.set(dsk, mood(dsk))
        gu, gv = self.gpu_state['gpu']
        self.g_gpu.set(gu, mood(gu) if gu is not None else FG)
        self.g_vram.set(gv, mood(gv) if gv is not None else FG)
        self.c_clk.config(text=time.strftime('%H:%M:%S'))
        self.c_down.config(text=f'↓ {speed(down)}')
        self.c_up.config(text=f'↑ {speed(up)}')
        self._render_ai()

        delta = len(self.events) - self._shown
        if delta < 0:  # 列表被 KEEP 截断，全量重建
            self.txt.config(state='normal')
            self.txt.delete('1.0', 'end')
            self.txt.config(state='disabled')
            for ev in reversed(self.events):
                self._insert_ev(ev)
        elif delta > 0:
            for ev in self.events[:delta]:
                self._insert_ev(ev)
        elif self.events and self.events[0] is not self._top:
            # 顶部条目被合并替换（同文件连续修改）：重写首行刷新大小
            self.txt.config(state='normal')
            self.txt.delete('1.0', '2.0')
            self.txt.config(state='disabled')
            self._insert_ev(self.events[0])
        self._top = self.events[0] if self.events else None
        self._shown = len(self.events)
        self.root.after(1000, self.tick)

    def _insert_ev(self, ev):
        """事件插到列表顶部，两行制：摘要行 + 完整路径行（后缀高亮）；每事件 2 行"""
        ts, tag, path, old, new = ev[:5]
        base, ext = split_ext(clip_path(path))
        t = self.txt
        t.config(state='normal')
        t.insert('1.0', '\n')                                  # 本事件结束换行
        t.insert('1.0', ext, ('ext', 'ev'))                    # 路径行: 后缀（多标签须传元组）
        t.insert('1.0', base, 'ev')                            #        主体
        t.insert('1.0', '  ', 'ev')                            #        缩进
        t.insert('1.0', '\n')                                  # 摘要→路径换行
        t.insert('1.0', '' if tag == '通知' else f'  {size(old)} → {size(new)}',
                 'ev')                                         # 摘要行: 大小（通知无大小）
        t.insert('1.0', f'{tag} ', (f't-{tag}', 'ev'))         #        类型
        t.insert('1.0', f'{ts} ', ('ts', 'ev'))                #        时间
        if int(t.index('end-1c').split('.')[0]) > KEEP * 2:
            t.delete(f'{KEEP * 2 + 1}.0', 'end')
        t.config(state='disabled')

    def _line_ev(self, e):
        """屏幕坐标 → 事件索引（每事件两行，整除即得）"""
        line = int(self.txt.index(f'@{e.x},{e.y}').split('.')[0]) - 1
        i = line // 2
        return i if 0 <= i < len(self.events) else None

    def _open_ev(self, e):
        """点击事件行：文件还在就直接打开；通知行打开系统通知中心"""
        i = self._line_ev(e)
        if i is not None:
            if self.events[i][1] == '通知':
                try:
                    os.startfile('ms-actioncenter:')
                except OSError:
                    self._flash('无法打开通知中心')
                return 'break'
            p = self.events[i][5]
            if os.path.exists(p):
                os.startfile(p)
            else:
                self._flash('文件已不存在')
        return 'break'  # 阻止传播到窗口拖拽

    def _ctx_ev(self, e):
        """事件行右键：打开方式菜单"""
        i = self._line_ev(e)
        if i is not None:
            self._ctx = i
            self.ev_menu.tk_popup(e.x_root, e.y_root)
        return 'break'

    def _launch(self, mode):
        p = self.events[self._ctx][5] if self._ctx < len(self.events) else None
        if not p or not os.path.exists(p):
            self._flash('文件已不存在')
            return
        if mode == 'open':
            os.startfile(p)
        elif mode == 'as':  # 系统"打开方式"对话框
            subprocess.Popen(['rundll32.exe', 'shell32.dll,OpenAs_RunDLL', p],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:               # 资源管理器中定位
            subprocess.Popen(['explorer', '/select,', p])

    def _render_ai(self):
        """供应商余额/用量行；数据版本变化或行数变化时重建"""
        s = self.ai_state
        if s['ver'] == self._ai_ver:
            return
        self._ai_ver = s['ver']
        for w in self._ai_rows:
            w.destroy()
        self._ai_rows = []
        rows = s['rows'] if s['rows'] else []
        if not rows:
            return
        for i, (name, val) in enumerate(rows):
            row = tk.Frame(self.ai_f, bg=BG)
            row.pack(fill='x')
            n = tk.Label(row, text=f'{name[:6]}', bg=BG, fg=DIM, width=7,
                         font=('Microsoft YaHei UI', 9), anchor='w')
            n.pack(side='left')
            v = tk.Label(row, text=val or '查询失败', bg=BG, fg=FG, width=36,
                         font=('Microsoft YaHei UI', 9), anchor='w')
            v.pack(side='left', padx=(8, 0))
            self._ai_rows += [row]

    def _cfg_ai(self):
        AICfg(self.root)

    def _toggle_autostart(self):
        on = self.auto_var.get()
        if not set_autostart(on):
            self.auto_var.set(not on)  # 写入失败回弹
            self._flash('自启设置失败（见日志）')
        else:
            self._flash('已' + ('开启' if on else '关闭') + '开机自启动')

    def _flash(self, msg):
        self.hint.config(text=msg, fg='#e07a7a')
        self.root.after(2000, lambda: self.hint.config(text=HINT, fg=DIM))


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--notify':
        notify_child()
    else:
        pet = Pet()
        pet.root.mainloop()
        if pet._nproc:
            pet._nproc.terminate()
