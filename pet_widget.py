"""SysPet 挂饰猫 v3（简化重构）：窗内子控件，拖拽=Qt 原生抓取（无脱嵌）。
- 默认趴窗底（主窗贴屏幕底时趴窗顶）；拖到哪趴到哪（夹紧在窗内）
- 点击=随机表情；CPU ≥85% 热脸+流动汗滴（湿度 ~10s 蒸干）
- 精灵帧 base64 内嵌（pet_assets.py）"""
import random
import time

from PySide6.QtCore import Qt, QTimer, QRectF, QPointF
from PySide6.QtGui import QColor, QPainter, QPolygonF, QPixmap
from PySide6.QtWidgets import QWidget

from pet_assets import PET_B64
import syspet

SZ = 52


class PetCat(QWidget):
    def __init__(self, main):
        super().__init__(main)  # 常态：主窗子控件（窗内趴着）
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
        self.dragging, self.drag_off = False, None
        self._cpu_hot = False
        self.heat, self.sweat = 0.0, 0.0
        self._drops = ((11.0, 14.0, 0.28, 0.0), (42.5, 18.0, 0.20, 0.45),
                       (7.5, 19.0, 0.34, 0.75))
        self._custom_anchor = None  # 拖放后记忆位置；None=默认锚（自适应）
        self._pm = self.frames['idle_a']
        self._move_to_dock()
        self.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # —— 位置 ——
    def _dock_pos(self):
        """默认锚：窗底中央；主窗贴屏幕底（压任务栏）时翻到窗顶。
        拖放过后记忆位置（_custom_anchor）优先。均为父内坐标。"""
        if self._custom_anchor is not None:
            return self._custom_anchor
        x = int(self.main.width() * 0.40)
        try:
            avail = self.screen().availableGeometry()
            at_bottom = self.main.geometry().bottom() >= avail.bottom() - 4
        except Exception:
            at_bottom = False
        return (x, 2) if at_bottom else (x, self.main.height() - SZ - 2)

    def _clamp_in(self, x, y):
        """夹紧在主窗内（全身可见）"""
        x = max(0, min(x, self.main.width() - SZ))
        y = max(0, min(y, self.main.height() - SZ))
        return x, y

    def _move_to_dock(self):
        x, y = self._dock_pos()
        self._custom_anchor = None
        self.move(x, y)

    # —— 帧选择 ——
    def _frame(self):
        now = time.time()
        if self.state_until and now > self.state_until:
            self.state, self.state_until = 'idle', 0.0
        s = self.state
        if s == 'idle' and self.heat > 0.55:
            s = 'flush'
        elif s == 'idle' and now >= self.blink_at:
            self.blink_at = now + random.uniform(2.5, 6)
            self.state, self.state_until = 'blink', now + 0.18
            s = 'blink'
        key = f'{s}_{self.tail_flip}'
        return self.frames.get(key) or self.frames.get(s)

    # —— 主循环（30ms）：体感积分 + 呼吸/摆尾 ——
    def _tick(self):
        now = time.time()
        self.heat = min(1.0, max(0.0,
            self.heat + (0.012 if self._cpu_hot else -0.006)))
        if self.heat > 0.55:
            self.sweat = min(1.0, self.sweat + 0.02)
        else:
            self.sweat = max(0.0, self.sweat - 0.003)
        if not self.dragging:
            b = self._dock_pos()
            hop = 10 if (self.state in ('happy', 'wow')
                         and self.state_until > now) else 0
            self.ty = b[1] - hop + (1 if int(now * 2) % 2 else 0)  # 跳 + 呼吸
            self.tail_t += 0.03
            if self.tail_t > 0.55:
                self.tail_t = 0.0
                self.tail_flip = 'b' if self.tail_flip == 'a' else 'a'
            k = 0.3
            y = self.y()
            self.move(self.x(), round(y + (self.ty - y) * k))
        pm = self._frame()
        if pm:
            self._pm = pm
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        if getattr(self, '_pm', None):
            p.drawPixmap(0, 0, self._pm)
            if self.sweat > 0.04:  # 流动汗滴：渗出淡入→淌下→滑落淡出
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

    # —— 拖拽（子控件原生抓取：按下后 move/release 由 Qt 保证送达，无脱嵌）——
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self.dragging = True
            self.press_gp = e.globalPosition().toPoint()
            self.drag_off = self.press_gp - self.pos()  # 全局按下点-位置（含窗偏移）
            e.accept()

    def mouseMoveEvent(self, e):
        if self.dragging:
            gp = e.globalPosition().toPoint()
            # drag_off = 全局按下点 - 父内位置，故 gp - drag_off 已是父内坐标
            x, y = self._clamp_in(gp.x() - self.drag_off.x(),
                                  gp.y() - self.drag_off.y())
            self.move(x, y)
            e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        gp = e.globalPosition().toPoint()
        moved = (gp - self.press_gp).manhattanLength()
        self.dragging = False
        # 放置处成为新趴位（夹紧窗内）
        self._custom_anchor = self._clamp_in(self.x(), self.y())
        if moved < 6:  # 点击：随机表情 + 跳一下（弹簧负责起落）
            self.state = random.choice(('happy', 'wow', 'blink', 'happy'))
            self.state_until = time.time() + 1.4
            syspet.log.info('挂饰猫: %s', self.state)
        e.accept()

    def set_cpu_hot(self, hot):
        self._cpu_hot = hot
