"""SysPet 挂饰猫：趴在主窗"屋顶"的活体系统指示（docs 设计定位见对话）。
- 弹簧跟随主窗（拖走/甩动松手自动 Q 弹归位）
- 点击：随机表情反应（开心/惊讶/眨眼）+ 跳一下
- 拖拽中：爱心眼
- 空闲：呼吸 + 随机眨眼 + 摆尾
- CPU 持续高负载：热（流汗）状态
"""
import random
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QWidget

from pet_assets import PET_B64
import syspet

SZ = 52


class PetCat(QWidget):
    def __init__(self, main):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)  # 不抢焦点
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
        # 连续体感量（每 tick 30ms 积分）：热度升≈3s、散≈5s；汗：出≈1.5s、蒸发≈10s
        self.heat, self.sweat = 0.0, 0.0
        # 流动汗滴：(x0, y0, 速度prog/s, 相位) 各滴错开——渗出淡入→淌下→滑落淡出
        self._drops = ((11.0, 14.0, 0.28, 0.0), (42.5, 18.0, 0.20, 0.45),
                       (7.5, 19.0, 0.34, 0.75))
        # 弹簧位置（初值=屋顶）
        self.tx, self.ty = self._roof()
        self.move(round(self.tx), round(self.ty))
        self.show()

        self._pm = self._frame()  # 首帧立即可见
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # —— 屋顶锚点：主窗顶部中央偏左，猫下半身探入窗内 ——
    def _roof(self):
        return (self.main.x() + 30, self.main.y() - SZ + 8)  # 爪底搭窗沿

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
        key = f'{s}_{self.tail_flip}'  # 尾巴左右帧；单帧状态（blink/wow/love）回退基础名
        return self.frames.get(key) or self.frames.get(s)

    def snap_to_roof(self):
        """瞬移归位（贴边滑回完成后调用，避免弹簧追赶的横飘卡顿）"""
        self.tx, self.ty = self._roof()
        self.move(round(self.tx), round(self.ty))

    def _tick(self):
        if not self.isVisible():  # 贴边隐藏期暂停，回归走 snap
            return
        # 弹簧逼近目标（拖拽时目标=鼠标；平时=屋顶 + 呼吸浮动）
        now = time.time()
        if not self.dragging:
            self.tx, self.ty = self._roof()
            self.ty += (2 if int(now * 2) % 2 == 0 else 0)  # 呼吸 2px
            self.tail_t += 0.03
            if self.tail_t > 0.55:  # 摆尾节奏（开心时加速由 state_until 内自然覆盖）
                self.tail_t = 0.0
                self.tail_flip = 'b' if self.tail_flip == 'a' else 'a'
        # 体感积分：热得慢、散得更慢；汗在热度下来后要慢慢干（≈10s）
        self.heat = min(1.0, max(0.0,
            self.heat + (0.012 if self._cpu_hot else -0.006)))
        if self.heat > 0.55:
            self.sweat = min(1.0, self.sweat + 0.02)
        else:
            self.sweat = max(0.0, self.sweat - 0.003)
        if int(now) != getattr(self, '_raised_s', -1):  # 每秒抬一次层序
            self._raised_s = int(now)
            self.raise_()
        k = 0.22
        x, y = self.x(), self.y()
        self.move(round(x + (self.tx - x) * k), round(y + (self.ty - y) * k))
        pm = self._frame()
        if pm:
            self.setPixmap_pm(pm)

    def setPixmap_pm(self, pm):
        # 无 QLabel：直接把 pixmap 画在 widget 上
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
            self.dragging = True
            self.press_pos = (e.globalPosition().x(), e.globalPosition().y())
            self.drag_off = (e.globalPosition().x() - self.x(),
                             e.globalPosition().y() - self.y())
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
        if moved < 6:  # 视为点击：随机表情 + 跳一下
            self.state = random.choice(('happy', 'wow', 'blink', 'happy'))
            self.state_until = time.time() + 1.4
            self.ty = self._roof()[1] - 14  # 上跳，弹簧自动回落
            syspet.log.info('挂饰猫: %s', self.state)
        # 其余情况弹簧自动弹回屋顶（甩动 Q 弹感）

    def set_cpu_hot(self, hot):
        self._cpu_hot = hot
