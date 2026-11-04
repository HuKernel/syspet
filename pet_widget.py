"""SysPet 挂饰猫 v2：趴在主窗内部（子控件），拖拽可抓出全屏甩动，松手 Q 弹回嵌。
- 常态：主窗 child，趴窗内顶部（挡一点内容是桌宠的特权）
- 拖拽：reparent 成独立透明窗，全屏范围甩
- 松手：弹簧追窗内锚点，到位自动回嵌（不再有 Z 序/贴边同步/屏顶截断问题）
- 表情/呼吸/尾巴/流动汗滴（热度-汗湿度体感模型）同前
"""
import random
import time

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from pet_assets import PET_B64
import syspet

SZ = 52
THRESH = 12  # 拖拽脱嵌阈值 px（人手点击自然抖动约 5-10px，须高于其上限）


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
        self._popped = False
        self._cpu_hot = False
        self.heat, self.sweat = 0.0, 0.0
        self._drops = ((11.0, 14.0, 0.28, 0.0), (42.5, 18.0, 0.20, 0.45),
                       (7.5, 19.0, 0.34, 0.75))
        self._raised_s = -1
        self._custom_anchor = None  # None=默认窗底；放置后记忆落点
        self._dock_in()
        self.show()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(30)

    # —— 窗内锚点（父内坐标）：默认锚随主窗位置自适应（v3.1.5 逻辑的窗内版）——
    # 主窗贴屏幕底部（压任务栏/贴边）→ 猫爬到窗口顶部；常规 → 趴窗底。
    # 每次动态计算，主窗拖动时猫实时换边；用户在窗内放手后固化为自定义锚 ——
    def _dock_pos(self):
        if self._custom_anchor is not None:
            return self._custom_anchor
        x = int(self.main.width() * 0.40)
        try:
            avail = self.screen().availableGeometry()
            at_bottom = self.main.geometry().bottom() >= avail.bottom() - 4
        except Exception:
            at_bottom = False
        return (x, 2) if at_bottom else (x, self.main.height() - SZ - 2)

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
        if self.dragging:  # 轮询驱动：脱嵌会丢鼠标抓取，必须读全局鼠标状态
            gp = QCursor.pos()
            if not self._popped:
                moved0 = abs(gp.x() - self.press_pos[0]) + abs(gp.y() - self.press_pos[1])
                if moved0 > THRESH:  # 超过点击抖动上限才脱嵌（原位，坐标映射在 child 态完成）
                    top_left = self.mapToGlobal(QPoint(0, 0))
                    self.drag_off = (gp.x() - top_left.x(), gp.y() - top_left.y())
                    self._pop_out(top_left)
                    self._popped = True
                elif not QApplication.mouseButtons() & Qt.LeftButton:
                    self._end_drag(gp)  # 未拖即松 = 点击，child 态处理
            else:
                self.tx = gp.x() - self.drag_off[0]
                self.ty = gp.y() - self.drag_off[1]
                self.move(round(self.tx), round(self.ty))
                if not QApplication.mouseButtons() & Qt.LeftButton:
                    self._end_drag(gp)
        # 体感积分：热得慢、散得更慢；汗在热度下来后要慢慢干（≈10s）
        self.heat = min(1.0, max(0.0,
            self.heat + (0.012 if self._cpu_hot else -0.006)))
        if self.heat > 0.55:
            self.sweat = min(1.0, self.sweat + 0.02)
        else:
            self.sweat = max(0.0, self.sweat - 0.003)
        if not self.dragging:
            if self._docked():
                # 窗内：呼吸浮动 + 摆尾 + 点击表情期上跳
                b = self._dock_pos()
                self.tx = b[0]
                hop = 10 if (self.state in ('happy', 'wow')
                             and self.state_until > now) else 0
                self.ty = b[1] - hop + (1 if int(now * 2) % 2 else 0)
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
    def _begin_drag(self, gp):
        """按下：仅记录（不脱嵌）。真正移动 >4px 才 _pop_out——
        点击场景零窗口重建，彻底规避 reparent 坐标/抓取陷阱"""
        self.dragging = True
        self._popped = False
        self.press_pos = (gp.x(), gp.y())

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._begin_drag(e.globalPosition().toPoint())
            e.accept()

    def mouseMoveEvent(self, e):
        # 拖动跟随由 _tick 轮询负责（脱嵌后事件可能中断）
        pass

    def _end_drag(self, gp):
        """松手（事件入口与轮询入口共用，幂等）"""
        if not self.dragging:
            return
        self.dragging = False
        # 落点规则：猫中心在窗内 → 趴在放手处；甩出窗外 → 弹回上次趴的位置
        cx, cy = self.x() + SZ // 2, self.y() + SZ // 2
        m = self.main.geometry()
        if m.contains(cx, cy):
            lx = max(0, min(cx - SZ // 2 - m.x(), self.main.width() - SZ))
            ly = max(0, min(cy - SZ // 2 - m.y(), self.main.height() - SZ))
            self._custom_anchor = (lx, ly)
        moved = abs(gp.x() - self.press_pos[0]) + abs(gp.y() - self.press_pos[1])
        if moved <= THRESH and self._popped:
            self._dock_in()  # 抖动级位移却已脱嵌：立即回嵌原锚点，净漂移归零
        if moved <= THRESH:  # 点击：表情 + 原地跳（child 态不脱嵌直接跳；独立态飞回锚点上方）
            self.state = random.choice(('happy', 'wow', 'blink', 'happy'))
            self.state_until = time.time() + 1.4
            if self._popped:
                g = self.main.mapToGlobal(QPoint(*self._dock_pos()))
                self.tx, self.ty = g.x(), g.y() - 14  # tx 一并覆盖，杜绝残留脏值
            syspet.log.info('挂饰猫: %s', self.state)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._end_drag(e.globalPosition().toPoint())

    def set_cpu_hot(self, hot):
        self._cpu_hot = hot
