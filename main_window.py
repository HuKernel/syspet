"""SysPet 玻璃拟态主窗口（PySide6）——仅 UI 层，业务逻辑复用 syspet 模块。
按 docs/UI-Redesign-Handoff.md 规格：模拟玻璃（深底 + 弥散光斑 + 1px 高光描边），
逻辑层经 tick() 轮询共享状态，后台线程不直接触碰控件。"""
import math
import threading
import time
from collections import deque

from PySide6.QtCore import (Qt, QTimer, QVariantAnimation, QPropertyAnimation, QSize,
                            QPointF, QObject, QRectF, Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QFont, QLinearGradient, QPainter,
                           QPainterPath, QPen, QPolygonF, QRadialGradient, QTextCursor,
                           QTextFormat)
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QPlainTextEdit, QPushButton,
                               QTabWidget, QTextEdit, QToolTip, QVBoxLayout,
                               QWidget)

import syspet
from syspet import HINT, KEEP, TS_FMT, clip_path, size, speed, split_ext

# 设计 Token（§3）
WIN_BG = QColor(13, 15, 20, 235)  # 窗口深色玻璃底（§3.1 背景 #0d0f14 半透明）
FG, DIM = '#e8e8ec', '#9a9aa2'
ACC1, ACC2 = '#0a84ff', '#7c5cff'
EVENT_COLORS = {'新增': '#30d158', '删除': '#ff453a', '修改': '#ff9f0a',
                '改名': '#5e5ce6', '通知': '#bf5af2', '告警': '#ff453a'}
F_TEXT, F_MONO = 'Microsoft YaHei UI', 'Consolas'


def _rgba(hex_color, a):
    return (f'rgba({int(hex_color[1:3], 16)}, {int(hex_color[3:5], 16)}, '
            f'{int(hex_color[5:7], 16)}, {a})')


QSS = f"""
QWidget {{ color: {FG}; font-family: '{F_TEXT}'; font-size: 9pt; }}
QPushButton {{ background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.16);
  border-radius: 8px; padding: 4px 14px; }}
QPushButton:hover {{ background: rgba(255,255,255,0.16); }}
QPushButton:disabled {{ color: {DIM}; }}
QPushButton#primary {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
  stop:0 {ACC1}, stop:1 {ACC2}); border: none; color: white; font-weight: bold; padding: 6px 20px; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
  stop:0 #1191ff, stop:1 #8b6dff); }}
QLineEdit {{ background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.16);
  border-radius: 8px; padding: 4px 8px; selection-background-color: {ACC1}; }}
QLineEdit:focus {{ border: 1px solid {ACC1}; }}
QListWidget {{ background: transparent; border: none; }}
QTabWidget::pane {{ background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.10);
  border-radius: 12px; }}
QTabBar::tab {{ background: transparent; color: {DIM}; padding: 6px 18px; }}
QTabBar::tab:selected {{ color: {ACC1}; background: rgba(255,255,255,0.07); border-radius: 8px; }}
QMenu {{ background: #14161d; border: 1px solid rgba(255,255,255,0.14); border-radius: 10px; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background: rgba(10,132,255,0.40); }}
QMenu::separator {{ height: 1px; background: rgba(255,255,255,0.12); margin: 4px 6px; }}
QScrollBar:vertical {{ background: transparent; width: 6px; }}
QScrollBar::handle:vertical {{ background: rgba(255,255,255,0.18); border-radius: 3px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
"""


class Bridge(QObject):
    """后台线程 → 主线程回调载体：emit 一个可调用对象，主线程执行（§5.5）"""
    sig = Signal(object)

    def __init__(self):
        super().__init__()
        # 必须连成员方法：连裸 lambda 无 QObject receiver，后台线程 emit 会退化
        # DirectConnection，slot 在后台线程执行（跨线程操作 widget 会偶发崩溃）
        self.sig.connect(self._run)

    def _run(self, fn):
        fn()

    def call(self, fn):
        self.sig.emit(fn)


def _mono(size=9, bold=False):
    f = QFont(F_MONO, size)
    f.setBold(bold)
    return f


def _text(size=9, bold=False):
    f = QFont(F_TEXT, size)
    f.setBold(bold)
    return f


class GaugeBar(QWidget):
    """渐变发光进度条（方案 A）：圆角轨道 → mood 渐变填充 → 末端高光点"""

    def __init__(self):
        super().__init__()
        self.setFixedHeight(9)
        self._pct = None
        self._color = QColor(255, 255, 255, 40)

    def set_value(self, pct, color):
        self._pct = pct
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        h = self.height() - 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawRoundedRect(0, 1, self.width(), h, 4, 4)
        if self._pct is None:
            return
        w = max(8, round(self.width() * min(self._pct, 100) / 100))
        g = QLinearGradient(0, 0, w, 0)
        g.setColorAt(0, self._color)
        g.setColorAt(1, self._color.lighter(150))
        p.setBrush(g)
        p.drawRoundedRect(0, 1, w, h, 4, 4)
        p.setBrush(QColor(255, 255, 255, 130))
        p.drawEllipse(w - 3, 1 + h // 2 - 2, 4, 4)


class Gauge(QWidget):
    """名称 + 发光进度条 + 数值；点击发 kind 信号"""

    clicked = Signal(str)

    def __init__(self, name, kind):
        super().__init__()
        self.kind = kind
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._name = QLabel(name)
        self._name.setFont(_text(9))
        self._name.setStyleSheet(f'color: {DIM};')
        self._name.setFixedWidth(32)
        self.bar = GaugeBar()
        self.bar.setFixedWidth(92)
        self.val = QLabel('—')
        self.val.setFont(_mono(9, True))
        self.val.setFixedWidth(44)
        self.val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay.addWidget(self._name)
        lay.addWidget(self.bar, stretch=1)
        lay.addWidget(self.val)

    def mousePressEvent(self, e):
        self.clicked.emit(self.kind)
        e.accept()

    def set_value(self, pct, color, unit='%', text=None):
        self.bar.set_value(pct, color)
        self.val.setText('—' if pct is None else (text or f'{pct:3.0f}{unit}'))

    def set_name(self, name):
        self._name.setText(name)


class GlassCheckBox(QCheckBox):
    """玻璃风复选框：选中 = 蓝底 + 白对勾（QSS 纯填充画不出对勾）"""

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = 15
        y = (self.height() - box) // 2
        rect = QRectF(0.5, y + 0.5, box - 1, box - 1)
        p.setPen(QPen(QColor(255, 255, 255, 95 if self.underMouse() else 60), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(rect, 4, 4)
        if self.isChecked():
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(ACC1))
            p.drawRoundedRect(rect, 4, 4)
            pen = QPen(QColor('#ffffff'), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(4, y + 8), QPointF(6.5, y + 10.5))
            p.drawLine(QPointF(6.5, y + 10.5), QPointF(11, y + 5))
        p.setPen(QColor(FG))
        p.setFont(self.font())
        p.drawText(QRectF(box + 7, 0, self.width() - box - 7, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self.text())


class ClickableLabel(QLabel):
    """可点击 QLabel（Σ 流量历史入口）"""
    clicked = Signal()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)


class EventCard(QWidget):
    """事件卡片：左侧类别色条 + 时间/胶囊标签 + 路径(后缀高亮) + 大小"""

    def __init__(self, ev):
        super().__init__()
        self.ev = ev
        ts, tag, path, old, new = ev[:5]
        c = EVENT_COLORS.get(tag, DIM)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 8, 4)
        lay.setSpacing(8)
        strip = QFrame()
        strip.setFixedWidth(3)
        strip.setStyleSheet(f'background: {c}; border-radius: 1px;')
        lay.addWidget(strip)
        col = QVBoxLayout()
        col.setSpacing(1)
        row1 = QHBoxLayout()
        row1.setSpacing(6)
        t = QLabel(ts)
        t.setFont(_mono(8))
        t.setStyleSheet(f'color: {DIM};')
        pill = QLabel(tag)
        pill.setStyleSheet(f'color: {c}; background: {_rgba(c, 0.16)};'
                           f'border-radius: 7px; padding: 0px 8px;')
        pill.setFont(_text(8))
        pill.setFixedHeight(15)
        row1.addWidget(t)
        row1.addWidget(pill)
        row1.addStretch(1)
        col.addLayout(row1)
        row2 = QHBoxLayout()
        row2.setSpacing(2)
        if tag in ('通知', '告警'):
            row2.addWidget(QLabel(clip_path(path)))
        else:
            base, ext = split_ext(clip_path(path))
            b = QLabel(base)
            b.setStyleSheet(f'color: {FG};')
            row2.addWidget(b)
            if ext:
                x = QLabel(ext)
                x.setStyleSheet('color: #5ac8fa;')
                row2.addWidget(x)
            sz = QLabel(f'  {size(old)} → {size(new)}')
            sz.setFont(_mono(8))
            sz.setStyleSheet(f'color: {DIM};')
            row2.addStretch(1)
            row2.addWidget(sz)
        col.addLayout(row2)
        lay.addLayout(col)
        # 悬浮显示完整路径：卡片空间有限，clip_path 截断会吃掉后缀/中段。
        # 本窗为 Qt.Tool 透明窗，系统 ToolTip 触发链不可靠（实测不产生事件），
        # 故 enterEvent 里主动 QToolTip.showText
        self._tip = path if tag in ('通知', '告警') \
            else f'{ev[5]}\n{size(old)} → {size(new)}'
        self.setToolTip(self._tip)
        self._tip_timer = QTimer(self, singleShot=True)
        self._tip_timer.timeout.connect(
            lambda: QToolTip.showText(QCursor.pos(), self._tip, self))
        self.setStyleSheet(f'background: {_rgba("#ffffff", 0.055)};'
                           f'border: 1px solid {_rgba("#ffffff", 0.10)};'
                           f'border-radius: 10px;')

    def enterEvent(self, _):
        self._tip_timer.start(500)

    def leaveEvent(self, _):
        self._tip_timer.stop()
        QToolTip.hideText()


class HistCanvas(QWidget):
    """1 小时历史折线（抗锯齿 + 网格 + 峰/均标注）"""

    def __init__(self, data, height=80):
        super().__init__()
        self.data = data
        self.setFixedSize(330, height)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        for g in (0.25, 0.5, 0.75):
            p.setPen(QPen(QColor(255, 255, 255, 18), 1))
            p.drawLine(0, round(h * g), w, round(h * g))
        data = self.data
        if len(data) < 2:
            p.setPen(QColor('#b0b0b5'))
            p.setFont(_text(9))
            p.drawText(self.rect(), Qt.AlignCenter, '历史采集中…')
            return
        pts = []
        for i, v in enumerate(data):
            pts += [i * w / 3600, h - 4 - v / 100 * (h - 8)]
        p.setPen(QPen(QColor(ACC1), 2))
        p.drawPolyline(QPolygonF([QPointF(x, y) for x, y in zip(pts[::2], pts[1::2])]))
        p.setPen(QColor(DIM))
        p.setFont(_text(8))
        p.drawText(w - 6, 16, f'峰 {max(data):.0f}% · 均 {sum(data) / len(data):.0f}%',
                   )


class TopPopup(QWidget):
    """Top 进程弹窗：曲线 + Top5（曲线即时显示，采样异步回填）"""

    def __init__(self, main, kind):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # 不抢焦点，8 秒定时关仍在
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        title = QLabel(f'{kind} · 最近 1 小时 + Top 5（点击空白处关闭）')
        title.setFont(_text(9, True))
        title.setStyleSheet(f'color: {ACC1};')
        lay.addWidget(title)
        lay.addWidget(HistCanvas(main._hist[kind]))
        self.placeholder = QLabel('Top 5 采样中…')
        self.placeholder.setStyleSheet(f'color: #b0b0b5;')
        lay.addWidget(self.placeholder)
        self.setStyleSheet(f'background: #14161d; border: 1px solid {_rgba("#ffffff", 0.14)};'
                           f'border-radius: 12px;')
        QTimer.singleShot(8000, self.close)

    def fill(self, rows, unit='%'):
        """Top5 采样结果回填（弹窗可能已自动关闭）；unit 兼容磁盘 MB/s"""
        if not self.isVisible() or not rows:
            return
        self.placeholder.hide()
        for name, v in rows:
            r = QLabel(f'{name[:24]:24s} {v:5.1f}{unit}')
            r.setFont(_mono(9))
            lay = self.layout()
            lay.insertWidget(lay.count() - 1, r)

    def mousePressEvent(self, _):
        self.close()  # Qt.Tool 不再自动关：点击弹窗任意处关闭（与旧版一致）

    def place_near(self, main):
        self.adjustSize()
        y2 = main.y() + main.height() + 6
        if y2 + self.height() > main.screen().geometry().height():
            y2 = main.y() - self.height() - 6
        x2 = min(main.x(), main.screen().geometry().width() - self.width())
        self.move(max(0, x2), max(0, y2))


class InfoPopup(QWidget):
    """置顶信息弹窗（谁在联网/硬件信息等）：行列表回填，点击关闭"""

    def __init__(self, main, title, loading='加载中…'):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(4)
        head = QLabel(title)
        head.setFont(_text(9, True))
        head.setStyleSheet(f'color: {ACC1};')
        lay.addWidget(head)
        self.placeholder = QLabel(loading)
        self.placeholder.setStyleSheet(f'color: #b0b0b5;')
        lay.addWidget(self.placeholder)
        lay.addStretch(1)
        self.setStyleSheet(f'background: #14161d; border: 1px solid {_rgba("#ffffff", 0.14)};'
                           f'border-radius: 12px;')
        QTimer.singleShot(15000, self.close)

    def fill(self, lines, mono=True):
        if not self.isVisible():
            return
        self.placeholder.hide()
        for line in lines:
            r = QLabel(line)
            r.setFont(_mono(9) if mono else _text(9))
            self.layout().insertWidget(self.layout().count() - 1, r)

    def mousePressEvent(self, _):
        self.close()

    def place_near(self, main):
        TopPopup.place_near(self, main)


class SettingsDialog(QDialog):
    """设置窗口：QTabWidget 双页（AI 供应商 / 通用设置）"""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle('SysPet 设置')
        self.setMinimumWidth(520)
        self.setStyleSheet('QDialog { background-color: #14161d; } '
                           'QListWidget#plist { background: #1c1e26; '
                           'border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; } '
                           'QListWidget#plist::item { padding: 3px; }')  # 深色底：白字可读
        try:
            cfg = syspet.json.loads(syspet.CFG_AI.read_text('utf-8'))
        except (OSError, ValueError):
            cfg = {}
        self.cfg = cfg
        self.provs = syspet.provs_plain(cfg.get('providers', []))
        self.sel = None
        self.bridge = Bridge()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(6)
        head = QLabel('设置')
        head.setFont(_text(13, True))
        sub = QLabel('保存后自动生效；密钥经 Windows DPAPI 加密存储')
        sub.setStyleSheet(f'color: {DIM};')
        lay.addWidget(head)
        lay.addWidget(sub)

        tabs = QTabWidget()
        tab_ai = QWidget()
        tab_gen = QWidget()
        tabs.addTab(tab_ai, 'AI 供应商')
        tabs.addTab(tab_gen, '通用设置')
        lay.addWidget(tabs, stretch=1)

        # Tab1：列表 + 按钮 + 字段
        ai_lay = QVBoxLayout(tab_ai)
        ai_lay.setContentsMargins(10, 10, 10, 10)
        self.lb = QListWidget()
        self.lb.setObjectName('plist')
        self.lb.setFixedHeight(84)
        self.lb.itemClicked.connect(lambda _i: self._pick())
        ai_lay.addWidget(self.lb)
        brow = QHBoxLayout()
        for text, fn in (('＋ 新增', self._add), ('删除', self._del)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            brow.addWidget(b)
        brow.addStretch(1)
        ai_lay.addLayout(brow)
        self.e_name = self._field(ai_lay, '名称')
        self.e_url = self._field(ai_lay, '地址')
        self.e_key = self._field(ai_lay, 'Key', secret=True)
        self.e_token = self._field(ai_lay, '访问令牌（可选）', secret=True)
        self.e_uid = self._field(ai_lay, '用户 UID（可选，需与令牌同时填）')
        self.btn_test = QPushButton('测试连接')
        self.btn_test.clicked.connect(self._test)
        ai_lay.addWidget(self.btn_test, alignment=Qt.AlignLeft)

        # Tab2：通用
        gen_lay = QVBoxLayout(tab_gen)
        gen_lay.setContentsMargins(10, 10, 10, 10)
        self.e_alert = self._short(gen_lay, '用量告警阈值 %（0=禁用，默认 90）', cfg.get('ai_alert', 90))
        self.e_rest = self._short(gen_lay, '久坐提醒间隔分钟（0=禁用，默认 45）', cfg.get('rest', 45))
        self.e_alpha = self._short(gen_lay, '待机透明度 %（5-95，默认 72；悬停恒清晰）', cfg.get('alpha', 72))
        gen_lay.addSpacing(6)
        tip = QLabel('弹 Windows 系统通知（关闭项仅记录到事件流）')
        tip.setStyleSheet(f'color: {DIM};')
        gen_lay.addWidget(tip)
        self.toast_vars = {}
        grid = QGridLayout()
        grid.setHorizontalSpacing(18)
        for i, (k, label) in enumerate((('ai', 'AI 用量告警'), ('sys', 'CPU/磁盘告警'),
                                        ('net', '网络变化'), ('usb', 'U 盘插拔'),
                                        ('rest', '久坐提醒'))):
            cb = GlassCheckBox(label)
            cb.setChecked(syspet._toast.get(k, True))
            self.toast_vars[k] = cb
            grid.addWidget(cb, i // 3, i % 3)
        gen_lay.addLayout(grid)
        gen_lay.addStretch(1)

        bot = QHBoxLayout()
        self.status = QLabel('密钥经 Windows DPAPI 加密存储，仅本机当前用户可解')
        self.status.setStyleSheet(f'color: {DIM};')
        bot.addWidget(self.status, stretch=1)
        save = QPushButton('保存并关闭')
        save.setObjectName('primary')
        save.clicked.connect(self._save)
        bot.addWidget(save)
        lay.addLayout(bot)
        self._refresh()

    def _field(self, parent_lay, label, secret=False):
        lb = QLabel(label)
        lb.setStyleSheet(f'color: {DIM};')
        parent_lay.addWidget(lb)
        e = QLineEdit()
        if secret:
            e.setEchoMode(QLineEdit.EchoMode.Password)
        parent_lay.addWidget(e)
        return e

    def _short(self, parent_lay, label, value):
        lb = QLabel(label)
        lb.setStyleSheet(f'color: {DIM};')
        parent_lay.addWidget(lb)
        e = QLineEdit(str(value))
        e.setFixedWidth(90)
        e.setAlignment(Qt.AlignLeft)
        parent_lay.addWidget(e)
        return e

    def _refresh(self):
        self.lb.clear()
        for pv in self.provs:
            k = pv.get('key', '')
            self.lb.addItem(f"{pv.get('name', '?')}   {pv.get('url', '')}   {k[:5]}***")
        if self.sel is not None and 0 <= self.sel < len(self.provs):
            self.lb.setCurrentRow(self.sel)
            self._load_fields()

    def _pick(self):
        self._sync()
        self.sel = self.lb.currentRow()
        if 0 <= self.sel < len(self.provs):
            self._load_fields()

    def _load_fields(self):
        pv = self.provs[self.sel]
        for e, k in ((self.e_name, 'name'), (self.e_url, 'url'), (self.e_key, 'key'),
                     (self.e_token, 'token'), (self.e_uid, 'uid')):
            e.setText(pv.get(k, ''))

    def _sync(self):
        """当前表单值写回编辑中的条目"""
        if self.sel is None or self.sel >= len(self.provs):
            return
        pv = {'name': self.e_name.text().strip(),
              'url': self.e_url.text().strip(),
              'key': self.e_key.text().strip()}
        if self.e_token.text().strip():
            pv['token'] = self.e_token.text().strip()
        if self.e_uid.text().strip():
            pv['uid'] = self.e_uid.text().strip()
        self.provs[self.sel] = pv

    def _add(self):
        self._sync()
        self.provs.append({'name': '新供应商', 'url': 'https://', 'key': ''})
        self.sel = len(self.provs) - 1
        self._refresh()

    def _del(self):
        self._sync()
        if self.sel is not None and 0 <= self.sel < len(self.provs):
            del self.provs[self.sel]
            self.sel = None
            self._refresh()

    def _test(self):
        self._sync()
        url, key = self.e_url.text().strip(), self.e_key.text().strip()
        tok, uid = self.e_token.text().strip(), self.e_uid.text().strip()
        self.status.setText('测试中…')
        self.btn_test.setEnabled(False)

        def run():
            if tok and uid:
                r = syspet.newapi_account({'url': url, 'token': tok, 'uid': uid}) \
                    or '账号令牌无效（回退按 Key 查询令牌额度）'
            else:
                r = syspet.test_provider(url, key, tok)
            ok = r.startswith(('连接', '余', '无限'))
            self.bridge.call(lambda: (self.status.setText(r),
                                      self.status.setStyleSheet(f'color: {"#34c759" if ok else "#ff3b30"};'),
                                      self.btn_test.setEnabled(True)))
        import threading
        threading.Thread(target=run, daemon=True).start()

    def _save(self):
        self._sync()
        for pv in self.provs:
            if not (pv.get('name') and (pv.get('url') or '').startswith('http')
                    and pv.get('key')):
                self._err('有条目未填全（名称 / 地址需 http 开头 / Key）')
                return
            if bool(pv.get('token')) != bool(pv.get('uid')):
                self._err('「访问令牌」与「用户 UID」需同时填写（或都留空）')
                return
        alert = self.e_alert.text().strip()
        if not (alert.isdigit() and int(alert) <= 100):
            self._err('告警阈值需为 0-100 的整数')
            return
        rest = self.e_rest.text().strip()
        if not (rest.isdigit() and int(rest) <= 480):
            self._err('久坐间隔需为 0-480 的整数')
            return
        alpha = self.e_alpha.text().strip()
        if not (alpha.isdigit() and 5 <= int(alpha) <= 95):
            self._err('待机透明度需为 5-95 的整数')
            return
        for pv in self.provs:
            pv['key'] = syspet.enc(pv['key'])
            if pv.get('token'):
                pv['token'] = syspet.enc(pv['token'])
        cfg = self.cfg
        cfg['providers'] = self.provs
        cfg['ai_alert'] = int(alert)
        cfg['rest'] = int(rest)
        cfg['alpha'] = int(alpha)
        cfg['toast'] = {k: v.isChecked() for k, v in self.toast_vars.items()}
        syspet._toast.update(cfg['toast'])
        syspet.cfg_write(cfg)
        self.main.set_alpha(int(alpha) / 100)  # 透明度立即生效
        syspet.log.info('AI 配置已保存，共 %d 个供应商', len(self.provs))
        self.accept()

    def _err(self, msg):
        self.status.setText(msg)
        self.status.setStyleSheet('color: #ff3b30;')


class DiffDialog(QDialog):
    """修改对比：左"修改前"右"修改后"只读视图，可选中复制或一键复制整侧；
    变更行高亮（前=红 后=绿，git diff 风格）"""
    DIFF_MAX_LINES = 8000  # 行级 diff 上限：超过跳过高亮防卡顿（32KB 快照的极端行长）

    def __init__(self, main, ts, path, before, after):
        super().__init__(main)
        import os
        self.setWindowTitle(f'修改对比 — {os.path.basename(path)}')
        self.resize(880, 540)
        self.setStyleSheet('QDialog { background-color: #14161d; } '
                           'QPlainTextEdit { background-color: #1c1e26; color: #e8e8ec; '
                           'border: 1px solid rgba(255,255,255,0.12); border-radius: 8px; '
                           'font-family: Consolas, "Microsoft YaHei UI"; }')
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        info = QLabel(f'{path}    {ts}')
        info.setStyleSheet(f'color: {DIM};')
        info.setWordWrap(True)
        lay.addWidget(info)
        body = QHBoxLayout()
        self._panes = {}
        for title, text in (('修改前', before), ('修改后', after)):
            col = QVBoxLayout()
            col.addWidget(QLabel(title))
            te = QPlainTextEdit()
            te.setPlainText(text if text is not None
                            else '（无快照：文件超过 32KB / 二进制 / 修改前无记录）')
            te.setReadOnly(True)  # 只读仍可选中 Ctrl+C
            col.addWidget(te, 1)
            body.addLayout(col, 1)
            self._panes[title] = (te, text)
        lay.addLayout(body, 1)
        self._highlight_diff()
        row = QHBoxLayout()
        for title in ('修改前', '修改后'):
            b = QPushButton(f'复制{title}')
            b.clicked.connect(lambda _, t=title: self._copy(t))
            row.addWidget(b)
        row.addStretch(1)
        b = QPushButton('关闭')
        b.clicked.connect(self.close)
        row.addWidget(b)
        lay.addLayout(row)

    def _highlight_diff(self):
        """行级差异高亮：删除/替换行标前栏红，新增/替换行标后栏绿"""
        import difflib
        before, after = (self._panes[t][1] for t in ('修改前', '修改后'))
        if before is None or after is None:  # 单侧无快照：占位提示已足够
            return
        a, b = before.splitlines(), after.splitlines()
        if min(len(a), len(b)) > self.DIFF_MAX_LINES:
            return
        pre, post = [], []
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
            if op in ('delete', 'replace'):
                pre.extend(range(i1, i2))
            if op in ('insert', 'replace'):
                post.extend(range(j1, j2))
        self._hl_lines(self._panes['修改前'][0], pre, QColor(255, 69, 58, 90))
        self._hl_lines(self._panes['修改后'][0], post, QColor(48, 209, 88, 90))

    @staticmethod
    def _hl_lines(te, lines, color):
        sels = []
        for ln in lines:
            if ln >= te.document().blockCount():
                continue
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(color)
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            c = QTextCursor(te.document().findBlockByNumber(ln))
            c.select(QTextCursor.BlockUnderCursor)
            s.cursor = c
            sels.append(s)
        te.setExtraSelections(sels)

    def _copy(self, which):
        text = self._panes[which][1]
        if text is not None:
            QApplication.clipboard().setText(text)


class MainWindow(QWidget):
    """玻璃拟态主悬浮窗：深底 + 弥散光斑 + 1px 高光描边"""

    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        syspet.setup_log()
        syspet.log.info('SysPet v%s (Qt) 启动', syspet.__version__)
        try:
            cfg = syspet.json.loads(syspet.CFG_AI.read_text('utf-8'))
        except (OSError, ValueError):
            cfg = {}
        self._rest_min = int(cfg.get('rest', 45) or 0)
        self._rest_n = 0
        self._rest_reminded, self._last_rem_n = False, 0
        self._alpha = max(0.05, int(cfg.get('alpha', 72) or 72) / 100)
        t = cfg.get('toast')
        if isinstance(t, dict):
            syspet._toast.update({k: v for k, v in t.items() if k in syspet._toast})
        elif t is False:
            syspet._toast.update({k: False for k in syspet._toast})

        # —— 状态（与 tkinter 版同构，逻辑层直接读写）——
        self.events = []
        self._shown = 0
        self._top = None
        self.ai_state = {'rows': None, 'ver': 0, 'alerted': set()}
        self._ai_ver = -1
        self._ai_rows = []
        self.gpu_state = {'gpu': (None, None, None)}
        self._hist = {'CPU': deque(maxlen=3600), '内存': deque(maxlen=3600),
                      '磁盘': deque(maxlen=3600)}
        self._io, self._t = None, time.time()
        self._dio = None  # disk_io_counters 上次采样（读写速度用）
        self._pomo_end = 0  # 番茄钟截止时刻（0=未开）；休息期复用并配 _pomo_rest
        self._pomo_rest = False
        self._day_mark = ''  # 每日维护（磁盘占用记录+SMART）标记
        self._pdh = syspet.cpu_utility_init()
        self._armed = set()
        self._net_last, self._net_ck = None, 0
        self._tr = syspet.traffic_load()
        self._tr_tick = 0
        self._dsk_i = 0
        self._edge, self._hidden, self._edge_timer = None, False, None
        self._edge_pos = (0, 0)
        self._popup = None  # Top 弹窗引用（防 GC 销毁）
        self._nproc = None
        self._breath = 0.0
        self.bridge = Bridge()
        self._slide_timer = None

        # —— 后台线程（只读写状态/事件，不触碰控件）——
        threading.Thread(target=syspet.drive_manager, args=(self.events, {}),
                         daemon=True).start()
        threading.Thread(target=syspet.gpu_loop, args=(self.gpu_state,),
                         daemon=True).start()
        threading.Thread(target=syspet.ai_net_loop, args=(self.ai_state, self.events),
                         daemon=True).start()
        threading.Thread(target=syspet.check_update, args=(self.events,),
                         daemon=True).start()
        if syspet.CUIAutomation and self._notify_spawn():
            threading.Thread(target=self._read_notify, daemon=True).start()

        self.setStyleSheet(QSS)
        self._build_ui()
        self._apply_alpha(self._alpha, animate=False)
        self._restore_pos()
        self._show_empty()

        breath = QVariantAnimation(self)
        breath.setStartValue(0.0)
        breath.setEndValue(1.0)
        breath.setDuration(3000)
        breath.setLoopCount(-1)
        breath.valueChanged.connect(self._on_breath)
        breath.start()
        from pet_widget import PetCat
        self.pet_cat = PetCat(self)  # 挂饰猫：趴屋顶，主窗持有引用

        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(1000)
        self.tick()

    def _on_breath(self, v):
        self._breath = v
        self.update()

    # ———————— UI 构建 ————————
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 8, 12, 8)
        root.setSpacing(6)

        header = QWidget()
        header.setObjectName('header')
        header.setStyleSheet(f'background: rgba(255,255,255,0.07);'
                             f'border-top-left-radius: 14px; border-top-right-radius: 14px;'
                             f'border-bottom: 1px solid rgba(255,255,255,0.08);')
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 5, 10, 5)
        hl.setSpacing(6)
        title = QLabel('SysPet')
        title.setFont(_text(10, True))
        hl.addWidget(title)
        hl.addStretch(1)
        self.c_net = QLabel('—')
        self.c_net.setFont(_mono(8))
        self.c_net.setFixedWidth(23 * 7)  # Consolas 8pt ≈ 7px/字符
        self.c_pomo = QLabel('')  # 番茄钟倒计时（未开时隐藏）
        self.c_pomo.setFont(_mono(8))
        self.c_pomo.setStyleSheet(f'color: {ACC1};')
        self.c_pomo.setFixedWidth(8 * 7)
        self.c_pomo.hide()
        self.c_day = ClickableLabel('Σ —')
        self.c_day.setFont(_mono(8))
        self.c_day.setStyleSheet(f'color: {DIM};')
        self.c_day.setFixedWidth(8 * 7)
        self.c_clk = QLabel(time.strftime('%H:%M:%S'))
        self.c_clk.setFont(_mono(8))
        self.c_clk.setStyleSheet(f'color: {DIM};')
        self.c_clk.setFixedWidth(8 * 7)
        for w in (self.c_clk, self._dot(), self.c_pomo, self.c_day,
                  self._dot(), self.c_net):
            hl.addWidget(w, alignment=Qt.AlignRight)
        self.c_day.clicked.connect(self._show_traffic_hist)
        root.addWidget(header)

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 6, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(7)
        self.g_cpu = Gauge('CPU', 'CPU')
        self.g_gpu = Gauge('GPU', 'GPU')
        self.g_mem = Gauge('内存', '内存')
        self.g_vram = Gauge('显存', '显存')
        self.g_dsk = Gauge('C盘', 'C盘')
        self.g_gput = Gauge('温度', '温度')
        self.g_dio = Gauge('读写', '磁盘')
        for i, g in enumerate((self.g_cpu, self.g_gpu, self.g_mem,
                               self.g_vram, self.g_dsk, self.g_gput, self.g_dio)):
            grid.addWidget(g, i // 2, i % 2)
        self.g_cpu.clicked.connect(self._on_gauge)
        self.g_mem.clicked.connect(self._on_gauge)
        self.g_dsk.clicked.connect(self._on_gauge)
        self.g_dio.clicked.connect(self._on_gauge)
        root.addWidget(grid_host)

        self.ai_host = QWidget()
        self.ai_lay = QVBoxLayout(self.ai_host)
        self.ai_lay.setContentsMargins(0, 4, 0, 0)
        self.ai_lay.setSpacing(3)
        root.addWidget(self.ai_host)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet('background: rgba(255,255,255,0.10);')
        root.addWidget(sep)

        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFixedHeight(172)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.itemClicked.connect(self._ev_click)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._ev_menu)
        root.addWidget(self.list, stretch=1)

        self.hint = QLabel(HINT)
        self.hint.setFont(_text(8))
        self.hint.setStyleSheet(f'color: {DIM};')
        root.addWidget(self.hint)

    def _dot(self):
        d = QLabel('·')
        d.setFont(_mono(8))
        d.setStyleSheet(f'color: {DIM};')
        return d

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        # 全部绘制裁进圆角内：弥散光斑的巨椭圆渐变到角落 alpha 不为 0，
        # 不裁剪会在圆角外的透明区染出淡色"直角"残影
        clip = QPainterPath()
        clip.addRoundedRect(QRectF(r), 16, 16)
        p.setClipPath(clip)
        p.setPen(Qt.NoPen)
        p.setBrush(WIN_BG)
        p.drawRoundedRect(r, 16, 16)
        breath = 0.5 + 0.5 * math.sin(getattr(self, '_breath', 0.25) * 2 * math.pi)
        w, h = self.width(), self.height()
        for cx, cy, rad, col, base in ((w * 0.16, h * 0.10, min(w, h) * 1.1,
                                        QColor(76, 194, 255), 34),
                                       (w * 0.86, h * 0.92, min(w, h) * 1.2,
                                        QColor(124, 92, 255), 30)):
            g = QRadialGradient(cx, cy, rad)
            top = QColor(col)
            top.setAlpha(int(base * (0.7 + 0.3 * breath)))
            g.setColorAt(0, top)
            fade = QColor(col)
            fade.setAlpha(0)
            g.setColorAt(1, fade)
            p.setBrush(QBrush(g))
            p.drawEllipse(QPointF(cx, cy), rad, rad)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 31), 1))
        p.drawRoundedRect(r, 16, 16)

    # ———————— 交互：悬停 / 拖动 / 贴边 ————————
    def set_alpha(self, a):
        self._alpha = a
        self.alpha_idle = a
        self.setWindowOpacity(a)

    def _apply_alpha(self, a, animate=True):
        self.alpha_idle = a
        if animate:
            self._anim_alpha(a)
        else:
            self.setWindowOpacity(a)

    def _anim_alpha(self, target, ms=150):
        anim = QPropertyAnimation(self, b'windowOpacity', self)
        anim.setDuration(ms)
        anim.setStartValue(self.windowOpacity())
        anim.setEndValue(target)
        anim.finished.connect(anim.deleteLater)
        anim.start()

    def enterEvent(self, _):
        if getattr(self, '_sliding', False):
            return  # 滑动动画期窗口扫过鼠标产生的 enter：不算交互
        if self._edge and self._hidden:
            self._hidden = False
            if self._edge_timer:
                self._edge_timer.stop()
            self._edge_back()
        self._anim_alpha(1.0)

    def leaveEvent(self, _):
        if getattr(self, '_sliding', False):
            return  # 滑动动画期（含猫划过鼠标的转发）：不触发收缩
        gp = self.mapFromGlobal(QCursor.pos())  # cursor() 是光标形状对象，须用 QCursor.pos()
        if 0 <= gp.x() < self.width() and 0 <= gp.y() < self.height():
            return  # 只是移入子控件
        self._anim_alpha(self.alpha_idle)
        if self._edge and not self._hidden:
            self._hidden = True
            self._edge_timer = QTimer(self, singleShot=True)
            self._edge_timer.timeout.connect(self._edge_out)
            self._edge_timer.start(800)  # 可被 enterEvent 的 stop() 取消

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_off = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
        elif e.button() == Qt.RightButton:
            self._ctx_menu()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton and hasattr(self, '_drag_off'):
            self.move(e.globalPosition().toPoint() - self._drag_off)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and hasattr(self, '_drag_off'):
            self._drop()

    def _drop(self):
        syspet.cfg_update(pos=f'{self.x()},{self.y()}')
        edge = self._snap_edge()
        if edge:
            self._edge, self._hidden = edge, True
            self._edge_pos = (self.x(), self.y())
            self._edge_out()
        else:
            self._edge = None

    def _snap_edge(self):
        W, H = self.width(), self.height()
        SW, SH = self.screen().geometry().width(), self.screen().geometry().height()
        cand = {'left': self.x(), 'top': self.y(),
                'right': SW - W - self.x(), 'bottom': SH - H - self.y()}
        edge, dist = min(cand.items(), key=lambda kv: kv[1])
        return edge if dist <= 6 else None

    def _edge_out(self):
        self._slide_to(*self._edge_xy())
        # 猫跟窗口一起收进去：否则它挂在屏缘（任务栏上）会被鼠标
        # 反复碰到，enter 转发把窗口唤回——收缩/弹回死循环（页面闪缩）
        self.pet_cat.hide()

    def _edge_xy(self, out=True):
        W, H = self.width(), self.height()
        SW, SH = self.screen().geometry().width(), self.screen().geometry().height()
        x, y = self.x(), self.y()
        return {'left': (2 - W if out else self._edge_pos[0], y),
                'right': (SW - 2 if out else self._edge_pos[0], y),
                'top': (x, 2 - H if out else self._edge_pos[1]),
                'bottom': (x, SH - 2 if out else self._edge_pos[1])}[self._edge]

    def _edge_back(self):
        self._slide_to(*self._edge_xy(out=False))
        self.pet_cat.show()  # 窗口滑回，猫从屏外弹簧追回屋顶

    def _slide_to(self, tx, ty):
        def step():
            dx, dy = (tx - self.x()) / 3, (ty - self.y()) / 3
            if abs(dx) < 2 and abs(dy) < 2:
                self.move(tx, ty)
                self._sliding = False
                return
            self.move(round(self.x() + dx), round(self.y() + dy))
            QTimer.singleShot(15, step)
        self._sliding = True  # 动画期窗口/猫会扫过鼠标，挂起 enter/leave 防收缩唤回震荡
        step()

    def _ctx_menu(self):
        m = QMenu(self)
        ver = m.addAction(f'SysPet v{syspet.__version__}')
        ver.setEnabled(False)
        m.addSeparator()
        m.addAction('设置…', self.open_settings)
        act = m.addAction('开机自启动')
        act.setCheckable(True)
        act.setChecked(syspet.autostart_enabled())
        act.triggered.connect(lambda: self._toggle_autostart(act))
        m.addSeparator()
        if self._pomo_end > time.time():
            rem = int(self._pomo_end - time.time())
            m.addAction(f'🍅 番茄钟 · {rem // 60:02d}:{rem % 60:02d}（点击取消）',
                        self._pomo_cancel)
        else:
            m.addAction('🍅 番茄钟 · 专注 25 分钟', self._pomo_start)
        m.addAction('谁在联网…', self._show_net)
        m.addAction('磁盘空间报告', self._show_disk_report)
        m.addAction('硬件信息', self._show_hw)
        m.addSeparator()
        m.addAction('退出', self._quit)
        m.exec(self.cursor().pos())

    # ———————— 扩展功能：番茄钟 / 网络 / 磁盘报告 / 硬件 ————————

    def _pomo_start(self):
        self._pomo_end, self._pomo_rest = time.time() + 25 * 60, False
        self._flash('番茄钟已开始：专注 25 分钟')

    def _pomo_cancel(self):
        self._pomo_end, self._pomo_rest = 0, False
        self.c_pomo.hide()
        self._flash('番茄钟已取消')

    def _pomo_tick(self):
        """番茄钟推进（tick 每秒调）：到点通知并自动接 5 分钟休息"""
        if not self._pomo_end:
            return
        rem = self._pomo_end - time.time()
        if rem <= 0:
            if self._pomo_rest:
                msg, self._pomo_end, self._pomo_rest = '休息结束，继续冲刺！', 0, False
                self.c_pomo.hide()
            else:
                msg = '番茄钟完成！起来活动 5 分钟'
                self._pomo_end, self._pomo_rest = time.time() + 5 * 60, True
            syspet.push_event(self.events, time.strftime(TS_FMT), '通知',
                              msg, None, None, '')
            syspet.toast('SysPet 番茄钟', msg, cat='rest')
            return
        tag = '☕' if self._pomo_rest else '🍅'
        self.c_pomo.setText(f'{tag}{int(rem) // 60:02d}:{int(rem) % 60:02d}')
        self.c_pomo.show()

    def _show_net(self):
        popup = self._open_popup(InfoPopup(self, '谁在联网 · 外网 TCP 连接（点击关闭）',
                                           '扫描中…'))

        def run():
            rows = syspet.net_procs()
            lines = [f'{name[:20]:20s} {remote}' for name, remote in rows]
            if not lines:
                lines = ['当前无外网连接']
            self.bridge.call(lambda: popup.fill(lines))
        threading.Thread(target=run, daemon=True).start()

    def _show_disk_report(self):
        rows = syspet.disk_forecast()
        if not rows:
            self._flash('无法读取磁盘信息')
            return
        syspet.toast('磁盘空间报告',
                     '\n'.join(f'{n} {p:.0f}%{pred}' for n, p, pred in rows),
                     manual=True)  # 手动触发不门控（用户关了 sys 类通知也照弹）

    def _show_hw(self):
        popup = self._open_popup(InfoPopup(self, '硬件信息（点击关闭）', '读取中…'))

        def run():
            info = syspet.hw_info()
            lines = [f'{k:　<3s} {v}' for k, v in info.items()]
            self.bridge.call(lambda: popup.fill(lines, mono=False))
        threading.Thread(target=run, daemon=True).start()

    def _daily_maint(self):
        """每日一次（含启动）：后台记录磁盘占用 + SMART 失败预警"""
        def run():
            try:
                syspet.disk_hist_note()
            except Exception:
                pass  # 配置读写失败不影响主流程
            try:
                for inst, bad in syspet.smart_check():
                    if bad:
                        msg = f'硬盘 SMART 预测失败：{inst[:40]}'
                        syspet.push_event(self.events, time.strftime(TS_FMT),
                                          '告警', msg, None, None, '')
                        syspet.toast('SysPet 磁盘告警', msg, cat='sys')
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def _toggle_autostart(self, act):
        if not syspet.set_autostart(act.isChecked()):
            act.setChecked(not act.isChecked())
            self._flash('自启设置失败（见日志）')
        else:
            self._flash('已' + ('开启' if act.isChecked() else '关闭') + '开机自启动')

    def _quit(self):
        syspet.cfg_update(traffic=self._tr)
        if self._nproc:
            self._nproc.terminate()
        self.close()
        QApplication.quit()

    def open_settings(self):
        pet = self.pet_cat
        pet.hide()  # 猫是置顶窗，模态设置页期间会遮挡对话框
        SettingsDialog(self).exec()
        if not (self._edge and self._hidden):  # 窗口未处于贴边收缩态才放猫
            pet.show()

    def _flash(self, msg):
        self.hint.setText(msg)
        self.hint.setStyleSheet(f'color: #ff453a;')
        QTimer.singleShot(2000, lambda: (self.hint.setText(HINT),
                                         self.hint.setStyleSheet(f'color: {DIM};')))

    # ———————— 仪表交互 ————————
    def _on_gauge(self, kind):
        if kind in ('CPU', '内存', '磁盘'):
            self.show_top(kind)
        elif kind == 'C盘':
            self._next_disk()

    def _open_popup(self, popup):
        """换弹窗：先关旧的，防连续点击叠加出一摞"""
        if self._popup:
            self._popup.close()
        self._popup = popup
        popup.place_near(self)
        popup.show()
        return popup

    def show_top(self, kind):
        # 无父顶层窗由 Python 引用拥有：局部变量随函数返回被 GC，窗口即销毁（闪没根因）
        popup = self._open_popup(TopPopup(self, kind))

        def run():
            rows = syspet.top_procs(kind)
            unit = 'MB/s' if kind == '磁盘' else '%'
            self.bridge.call(lambda: popup.fill(rows, unit))
        threading.Thread(target=run, daemon=True).start()

    def _next_disk(self):
        self._dsk_i += 1
        dsk = self._refresh_disk()
        self.g_dsk.set_value(dsk, syspet.mood(dsk))

    def _refresh_disk(self):
        disk = self._cur_disk()
        du = syspet.shutil.disk_usage(disk)
        dsk = 100 * du.used / du.total
        self.g_dsk.set_name(f'{disk[0]}盘')
        self._alert_sys(f'{disk[0]}盘', dsk, 95)
        return dsk

    def _cur_disk(self):
        ds = syspet.drives()
        return ds[self._dsk_i % len(ds)] if ds else 'C:\\'

    def _alert_sys(self, what, pct, th):
        if pct >= th and what not in self._armed:
            self._armed.add(what)
            msg = f'{what} {pct:.0f}%（阈值 {th}%）'
            syspet.push_event(self.events, time.strftime(TS_FMT), '告警', msg, None, None, '')
            syspet.toast('SysPet 告警', msg, cat='sys')
        elif pct < th - 5:
            self._armed.discard(what)

    # ———————— 事件流 ————————
    def _show_traffic_hist(self):
        """点击 Σ：toast 弹最近 3 天 + 本月累计"""
        ds = self._tr['days'][-4:-1]
        year = time.strftime('%Y')
        lines = [f"{(d['date'] + ' ') if d['date'][:4] != year else d['date'][5:] + ' '}"
                 f"↓{size(d['down'])} ↑{size(d['up'])}"
                 for d in reversed(ds)]
        md, mu = syspet.month_traffic(self._tr['days'])
        lines.append(f"本月　↓{size(md)} ↑{size(mu)}")
        syspet.toast('流量历史（最近 3 天）', '\n'.join(lines), manual=True)  # 手动触发不门控

    def _show_empty(self):
        it = QListWidgetItem()
        it.setSizeHint(QSize(370, 44))
        self.list.addItem(it)
        lb = QLabel('等待文件变化、系统通知与告警…')
        lb.setStyleSheet(f'color: #b0b0b5;')
        lb.setAlignment(Qt.AlignCenter)
        self.list.setItemWidget(it, lb)

    def _insert_card(self, ev):
        it = QListWidgetItem()
        it.setSizeHint(QSize(370, 44))
        self.list.insertItem(0, it)
        card = EventCard(ev)
        self.list.setItemWidget(it, card)
        it.setToolTip(card._tip)  # 卡片间隙区域的兜底
        if self.list.count() > KEEP:
            row = self.list.count() - 1
            w = self.list.itemWidget(self.list.item(row))  # 先取引用
            self.list.takeItem(row)
            if w:  # takeItem 只移除 item，不释放 setItemWidget 绑定的卡片
                w.deleteLater()

    def _ev_click(self, item):
        row = self.list.row(item)
        if not (0 <= row < len(self.events)):
            return
        ev = self.events[row]
        if ev[1] == '告警':
            return
        p = ev[5]
        if ev[1] == '通知':
            if p.startswith('http'):
                import webbrowser
                webbrowser.open(p)
            else:
                import os
                try:
                    os.startfile('ms-actioncenter:')
                except OSError:
                    self._flash('无法打开通知中心')
            return
        import os
        if ev[1] == '修改' and len(ev) > 6 and ev[6]:
            # 点击修改事件：前后对比（有快照才弹，否则回退为直接打开）
            DiffDialog(self, ev[0], p, ev[6][0], ev[6][1]).exec()
            return
        if os.path.exists(p):
            os.startfile(p)
        else:
            self._flash('文件已不存在')

    def _ev_menu(self, pos):
        item = self.list.itemAt(pos)
        if item is None:
            return
        row = self.list.row(item)
        ev = self.events[row] if row < len(self.events) else None
        if ev is None or ev[1] in ('通知', '告警'):
            return
        m = QMenu(self)
        m.addAction('打开', lambda: self._launch(ev[5], 'open'))
        m.addAction('打开方式…', lambda: self._launch(ev[5], 'as'))
        m.addAction('在文件夹中显示', lambda: self._launch(ev[5], 'show'))
        m.exec(self.list.mapToGlobal(pos))

    def _launch(self, p, mode):
        import os, subprocess
        if not os.path.exists(p):
            self._flash('文件已不存在')
            return
        if mode == 'open':
            os.startfile(p)
        elif mode == 'as':
            subprocess.Popen(['rundll32.exe', 'shell32.dll,OpenAs_RunDLL', p],
                             creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            subprocess.Popen(['explorer', '/select,', p])

    def _ev_replace_first(self):
        it = self.list.item(0)
        if it is None:
            return
        it.setSizeHint(QSize(370, 44))
        self.list.setItemWidget(it, EventCard(self.events[0]))

    # ———————— 通知子进程 ————————
    def _notify_spawn(self):
        import subprocess
        cmd = [syspet.sys.executable] + ([] if getattr(syspet.sys, 'frozen', False)
                                         else [str(syspet.Path(syspet.__file__).resolve())]) \
            + ['--notify']
        try:
            self._nproc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW)
            return True
        except OSError as e:
            syspet.log.warning('通知子进程启动失败: %s', e)
            return False

    def _read_notify(self):
        import time as _t
        while True:
            for raw in self._nproc.stdout:
                line = raw.decode('utf-8').rstrip('\n')
                if not line:
                    continue
                app, _, body = line.partition('\t')
                syspet.push_event(self.events, _t.strftime(TS_FMT), '通知',
                                  f'{app} {body}' if app else body, None, None, '')
            if self._nproc.poll() is None:
                _t.sleep(5)
                continue
            syspet.log.warning('通知子进程退出（码 %s），5 秒后重启', self._nproc.returncode)
            _t.sleep(5)
            if not self._notify_spawn():
                return

    # ———————— 主循环 ————————
    def tick(self):
        cpu = syspet.cpu_utility_read(self._pdh)
        if cpu is None:
            cpu = syspet.psutil.cpu_percent()
        mem = syspet.psutil.virtual_memory().percent
        dsk = self._refresh_disk()
        io, t = syspet.psutil.net_io_counters(), time.time()
        dt = t - self._t
        down = (io.bytes_recv - self._io.bytes_recv) / dt if self._io else 0
        up = (io.bytes_sent - self._io.bytes_sent) / dt if self._io else 0
        today = time.strftime('%Y-%m-%d')
        if self._tr['days'][-1]['date'] != today:
            self._tr = syspet.traffic_load()
        if self._day_mark != today:  # 每日维护：磁盘占用记录 + SMART 预警
            self._day_mark = today
            self._daily_maint()
        cur = self._tr['days'][-1]
        d, u = syspet.traffic_delta(self._io, io)  # 累计用精确字节增量
        cur['down'] += d
        cur['up'] += u
        self._io, self._t = io, t  # 必须在累计之后更新，否则差值恒 0（v2.1.0 引入的 bug）
        self._tr_tick += 1
        if self._tr_tick >= 300:
            self._tr_tick = 0
            syspet.cfg_update(traffic=self._tr)

        self.g_cpu.set_value(cpu, syspet.mood(cpu))
        self.g_mem.set_value(mem, syspet.mood(mem))
        self.g_dsk.set_value(dsk, syspet.mood(dsk))
        try:
            dio = syspet.psutil.disk_io_counters()
        except OSError:
            dio = None
        sp = syspet.disk_io_speed(self._dio, dio, dt)
        self._dio = dio
        if sp is None:
            self.g_dio.set_value(None, FG)
            self._hist['磁盘'].append(0)
        else:  # 1GB/s 满量程画条（NVMe 顺序读写不打满），文本直读速度
            mbps = (sp[0] + sp[1]) / 1048576
            pct = min(100.0, mbps / 10)
            self.g_dio.set_value(pct, syspet.mood(pct), text=f'{mbps:3.0f}M')
            self._hist['磁盘'].append(pct)
        self._pomo_tick()
        gu, gv, gt = self.gpu_state['gpu']
        self.g_gpu.set_value(gu, syspet.mood(gu) if gu is not None else FG)
        self.g_vram.set_value(gv, syspet.mood(gv) if gv is not None else FG)
        gt_c = ('#34c759' if (gt or 0) < 60 else
                '#ff9500' if gt is not None and gt < 80 else
                '#ff3b30' if gt is not None else DIM)
        self.g_gput.set_value(gt, gt_c, unit='°')
        self._alert_sys('CPU', cpu, 90)
        self.pet_cat.set_cpu_hot(cpu >= 85)
        if self._net_ck % 10 == 0:
            conn, ifs = syspet.net_connected(), syspet.net_ifaces()
            if self._net_last is not None:
                last_conn, last_ifs = self._net_last
                ts = time.strftime(TS_FMT)
                if conn != last_conn:
                    msg = '网络已恢复' if conn else '网络已断开'
                    syspet.push_event(self.events, ts, '通知' if conn else '告警',
                                      msg, None, None, '')
                    syspet.toast('SysPet 网络', msg, cat='net')
                elif ifs != last_ifs:
                    parts = [f'{"、".join(sorted(ifs - last_ifs))} 接入'] if ifs - last_ifs else []
                    if last_ifs - ifs:
                        parts.append(f'{"、".join(sorted(last_ifs - ifs))} 断开')
                    msg = '网络环境变化: ' + '，'.join(parts)
                    syspet.push_event(self.events, ts, '通知', msg, None, None, '')
                    syspet.toast('SysPet 网络', msg, cat='net')
            self._net_last = (conn, ifs)
        self._net_ck += 1
        self._hist['CPU'].append(cpu)
        self._hist['内存'].append(mem)
        self._rest_n += 1
        if syspet.screen_locked() or syspet.idle_seconds() >= 900:
            self._rest_n = 0
            self._rest_reminded = False
        if self._rest_min and self._rest_n >= self._rest_min * 60:
            if not self._rest_reminded or self._rest_n - self._last_rem_n >= 300:
                first = not self._rest_reminded
                self._rest_reminded, self._last_rem_n = True, self._rest_n
                mins = self._rest_n // 60
                msg = (f'久坐提醒 · 已连续工作 {mins} 分钟，起来活动一下' if first
                       else f'仍未休息 · 已连续久坐 {mins} 分钟')
                syspet.push_event(self.events, time.strftime(TS_FMT), '通知',
                                  msg, None, None, '')
                syspet.toast('SysPet 久坐提醒', msg, cat='rest')
        self.c_clk.setText(time.strftime('%H:%M:%S'))
        self.c_day.setText(f'Σ {size(cur["down"] + cur["up"])}')
        self.c_net.setText(f'↓ {speed(down)} ↑ {speed(up)}')
        self._render_ai()
        self._render_events()

    def _render_ai(self):
        s = self.ai_state
        if s['ver'] == self._ai_ver:
            return
        self._ai_ver = s['ver']
        for w in self._ai_rows:
            w.setParent(None)
            w.deleteLater()
        self._ai_rows = []
        for name, val in (s['rows'] or []):
            row = QWidget()
            lay = QHBoxLayout(row)
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(8)
            n = QLabel(name[:6])
            n.setFont(_text(9))
            n.setStyleSheet(f'color: {DIM};')
            n.setFixedWidth(64)
            v = QLabel(val or '查询失败')
            v.setFont(_text(9))
            lay.addWidget(n)
            lay.addWidget(v, stretch=1)
            self.ai_lay.addWidget(row)
            self._ai_rows.append(row)

    def _render_events(self):
        n = len(self.events)
        shown = self._shown
        if n < shown:  # 列表被截断，全量重建
            self.list.clear()
            for ev in reversed(self.events):
                self._insert_card(ev)
        elif n > shown:
            if shown == 0:
                self.list.clear()
            # 新事件是最前面的 n-shown 条（events[0] 最新）；旧→新逐条顶插使最新置顶，
            # 保证 row i 与 events[i] 一致（点击按行号映射文件）
            for ev in reversed(self.events[:n - shown]):
                self._insert_card(ev)
        elif self.events and self.events[0] is not self._top:
            self._ev_replace_first()
        self._top = self.events[0] if self.events else None
        self._shown = n
        if not self.events and self.list.count() == 0:
            self._show_empty()

    def _restore_pos(self):
        pos = syspet.pos_load()
        SW, SH = self.screen().geometry().width(), self.screen().geometry().height()
        if pos:
            x = min(max(0, pos[0]), SW - self.width())
            y = min(max(0, pos[1]), SH - self.height())
            self.move(x, y)
        else:
            self.move(SW - self.width() - 16, SH - self.height() - 56)

    def closeEvent(self, e):
        if self._nproc:
            self._nproc.terminate()
        e.accept()

