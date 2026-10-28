import os
from pathlib import Path
import syspet
from syspet import (handle_batch, ev_text, size, rr, split_ext, clip_path,
                    push_event, enc, dec)

syspet._only, syspet._excludes = (), syspet.NOISE  # 隔离本机 syspet.json，固定过滤规则

assert len(rr(0, 0, 92, 10, 4)) == 24 and rr(0, 0, 92, 10, 4)[0] == 4
assert size(None) == '—' and size(2048) == '2 KB'
assert split_ext('C:\\a\\b\\报告.docx') == ('C:\\a\\b\\报告', '.docx')
assert split_ext('C:\\a\\2026.10.28 备份') == ('C:\\a\\2026.10.28 备份', '')

# DPAPI 密钥加密：可逆、密文非明文、旧明文配置兼容、空值不动
_e = enc('sk-abc密钥123')
assert _e != 'sk-abc密钥123' and _e.startswith('dpapi:') and dec(_e) == 'sk-abc密钥123'
assert dec('sk-旧明文兼容') == 'sk-旧明文兼容'
assert enc('') == '' and dec('') == ''

# 系统噪音过滤：Windows/ProgramData/Temp 等被排除，普通文件放行
_ev = []
push_event(_ev, '12:00:00', '修改', 'C:\\Windows\\System32\\x.dll', 1, 2, 'C:\\Windows\\System32\\x.dll')
push_event(_ev, '12:00:00', '修改', 'C:\\Users\\u\\AppData\\Local\\Temp\\t.tmp',
           1, 2, 'C:\\Users\\u\\AppData\\Local\\Temp\\t.tmp')
assert _ev == [], _ev
push_event(_ev, '12:00:00', '修改', 'E:\\桌面\\报告.docx', 1, 2, 'E:\\桌面\\报告.docx')
assert len(_ev) == 1

# 回归：白名单模式内黑名单仍须生效——syspet.log 在白名单目录内曾致
# "写日志→触发监控事件→再写日志"的自激励循环，事件列表与日志双膨胀
syspet._only = ('e:\\桌面\\',)
_ev2 = []
push_event(_ev2, '12:00:00', '修改', 'syspet.log', 1, 2, 'E:\\桌面\\syspet\\syspet.log')
push_event(_ev2, '12:00:00', '修改', 'a.txt', 1, 2, 'E:\\桌面\\a.txt')
assert _ev2 and _ev2[0][2] == 'a.txt', _ev2
syspet._only = ()
long_p = 'C:\\' + 'x' * 80 + '\\file.txt'
assert clip_path(long_p).endswith('file.txt') and clip_path(long_p).startswith('C:\\')
assert clip_path('C:\\short\\a.txt') == 'C:\\short\\a.txt'

d = Path('test_dir')
d.mkdir(exist_ok=True)
drive = str(d.resolve()) + os.sep  # 绝对路径：白名单模式下相对路径会被误拦
ev, cache, pending = [], {}, {}

(d / 'a.txt').write_text('1')
handle_batch([(1, 'a.txt'), (3, 'a.txt')], drive, ev, cache, pending)   # 新增 + 紧随写入
(d / 'a.txt').write_text('22')
handle_batch([(3, 'a.txt')], drive, ev, cache, pending)                  # 修改（相邻合并）
(d / 'a.txt').rename(d / 'b.txt')
handle_batch([(4, 'a.txt'), (3, '其他.log'), (5, 'b.txt')],              # 改名中间被隔开
             drive, ev, cache, pending)
(d / 'b.txt').rename(d / 'c.txt')
handle_batch([(4, 'b.txt')], drive, ev, cache, pending)                  # 改名旧名先到
handle_batch([(5, 'c.txt')], drive, ev, cache, pending)                  # 新名下一批才到
(d / 'c.txt').unlink()
handle_batch([(2, 'c.txt')], drive, ev, cache, pending)                  # 删除
(d / '孤.txt').write_text('9')
handle_batch([(5, '孤.txt')], drive, ev, cache, pending)                 # 无旧名的新名→新增

for e in ev:
    print(ev_text(e))
tags = [e[1] for e in ev]
assert tags == ['新增', '删除', '改名', '改名', '修改', '修改', '新增'], tags
assert ev[3][2] == 'a.txt → b.txt' and ev[2][2] == 'b.txt → c.txt'
assert ev[3][5] == drive + 'b.txt' and ev[2][5] == drive + 'c.txt'  # 改名点击打开新路径
for f in ('孤.txt',):
    (d / f).unlink()
d.rmdir()
print('ok')
