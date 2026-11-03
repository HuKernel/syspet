"""SysPet 挂饰猫 v2：趴在主窗内部（子控件），拖拽可抓出全屏甩动，松手 Q 弹回嵌。
- 常态：主窗 child，趴窗内顶部（挡一点内容是桌宠的特权）
- 拖拽：reparent 成独立透明窗，全屏范围甩
- 松手：弹簧追窗内锚点，到位自动回嵌（不再有 Z 序/贴边同步/屏顶截断问题）
- 表情/呼吸/尾巴/流动汗滴（热度-汗湿度体感模型）同前
"""
import random
import time

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget

from pet_assets import PET_B64
import syspet

SZ = 52


class PetCat(QWidget):
    def __init__(self, main):
        super().__init__(main)  # 常态：主窗子控件（趴窗内）
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.main = main
        import base64
        self.frames = {}
        for k, b64 in PET_B64.items():
            pm = QPixmap()
            pm.loadFromData(base64.b64decode(b64), 'PNG')
            self.frames[k] = pm.scaled(SZ, SZ, Qt.KeepAspectRatio,
                                       Qt.SmoothTransformation)
        self.setFixedSize(SZ, SZ)
        self.state, self.state_until = 'idle', 0.0
        self.tail_flip, self.tail_t = 'a', 0.0
        self.blink_at = time.time() + random.uniform(2, 5)
        self.drag_off, self.dragging, self.press_pos = None, False, None
        self._cpu_hot = False
        self.heat, self.sweat = 0.0, 0.0
        self._drops = ((11.0, 14.0, 0.28, 0.0), (42.5, 18.0, 0.20, 0.45),
                       (7.5, 19.0, 0.34, 0.75))
        self._raised_s = -1
        self._dock_in()
        self.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # —— 窗内锚点（父内坐标）：顶部中央偏左 ——
    def _dock_pos(self):
        return (int(self.main.width() * 0.40), 2)

    def _docked(self):
        return self.parent() is self.main

    def _dock_in(self):
        """回嵌主窗（瞬移到锚点）"""
        self.setWindowFlags(Qt.Widget)  # 清除独立窗口标志
        self.setParent(self.main)
        self.tx, self.ty = self._dock_pos()
        self.move(round(self.tx), round(self.ty))
        self.raise_()
        self.show()

    def _pop_out(self, gp):
        """抓出窗外：变独立透明窗（全屏甩动）"""
        self.setParent(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.move(gp)
        self.show()
        self.raise_()

    # —— 帧选择 ——
    def _frame(self):
        now = time.time()
        if self.state_until and now > self.state_until:
            self.state, self.state_until = 'idle', 0.0
        s = self.state
        if self.dragging:
            s = 'love'
        elif s == 'idle' and self.heat > 0.55:
            s = 'flush'
        elif s == 'idle' and now >= self.blink_at:
            self.blink_at = now + random.uniform(2.5, 6)
            self.state, self.state_until = 'blink', now + 0.18
            s = 'blink'
        key = f'{s}_{self.tail_flip}'
        return self.frames.get(key) or self.frames.get(s)

    def _tick(self):
        now = time.time()
        # 体感积分：热得慢、散得更慢；汗在热度下来后要慢慢干（≈10s）
        self.heat = min(1.0, max(0.0,
            self.heat + (0.012 if self._cpu_hot else -0.006)))
        if self.heat > 0.55:
            self.sweat = min(1.0, self.sweat + 0.02)
        else:
            self.sweat = max(0.0, self.sweat - 0.003)
        if not self.dragging:
            if self._docked():
                # 窗内：呼吸浮动 + 摆尾
                b = self._dock_pos()
                self.tx = b[0]
                self.ty = b[1] + (1 if int(now * 2) % 2 else 0)
                self.tail_t += 0.03
                if self.tail_t > 0.55:
                    self.tail_t = 0.0
                    self.tail_flip = 'b' if self.tail_flip == 'a' else 'a'
                k = 0.3
            else:
                # 独立态：弹簧追窗内锚点的全局位置，到位回嵌
                g = self.main.mapToGlobal(QPoint(*self._dock_pos()))
                self.tx, self.ty = g.x(), g.y()
                if int(now) != self._raised_s:
                    self._raised_s = int(now)
                    self.raise_()
                k = 0.22
            x, y = self.x(), self.y()
            self.move(round(x + (self.tx - x) * k), round(y + (self.ty - y) * k))
            if not self._docked() and abs(x - self.tx) < 6 and abs(y - self.ty) < 6:
                self._dock_in()
        pm = self._frame()
        if pm:
            self._pm = pm
            self.update()

    def paintEvent(self, _):
        from PySide6.QtGui import QPainter
        if getattr(self, '_pm', None):
            p = QPainter(self)
            p.drawPixmap(0, 0, self._pm)
            if self.sweat > 0.04:  # 流动汗滴：渗出淡入→沿脸侧淌下→滑落淡出
                from PySide6.QtGui import QColor, QPolygonF
                from PySide6.QtCore import QPointF
                now = time.time()
                for x0, y0, spd, ph in self._drops:
                    prog = (now * spd + ph) % 1.0
                    y = y0 + prog * 12.0
                    fade = min(prog / 0.18, 1.0) * min((1.0 - prog) / 0.25, 1.0)
                    a = int(235 * self.sweat * fade)
                    if a < 8:
                        continue
                    c = QColor(130, 195, 255, a)
                    p.setPen(c)
                    p.setBrush(c)
                    p.drawPolygon(QPolygonF([QPointF(x0, y), QPointF(x0 - 2, y + 3),
                                             QPointF(x0 + 2, y + 3)]))
                    p.drawEllipse(QPointF(x0, y + 4.5), 2.6, 3.0)

    # —— 交互 ——
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            gp = e.globalPosition().toPoint()
            self.dragging = True
            self.press_pos = (gp.x(), gp.y())
            self.drag_off = (gp.x() - self.x(), gp.y() - self.y())
            self._pop_out(gp - QPoint(*self.drag_off))  # 原位抓出，不跳变
            e.accept()

    def mouseMoveEvent(self, e):
        if self.dragging:
            self.tx = e.globalPosition().x() - self.drag_off[0]
            self.ty = e.globalPosition().y() - self.drag_off[1]

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        moved = abs(e.globalPosition().x() - self.press_pos[0]) + \
                abs(e.globalPosition().y() - self.press_pos[1])
        self.dragging = False
        if moved < 6:  # 点击：表情 + 跳一下（独立态弹回逻辑接管）
            self.state = random.choice(('happy', 'wow', 'blink', 'happy'))
            self.state_until = time.time() + 1.4
            g = self.main.mapToGlobal(QPoint(*self._dock_pos()))
            self.ty = g.y() - 14
            syspet.log.info('挂饰猫: %s', self.state)

    def set_cpu_hot(self, hot):
        self._cpu_hot = hot
