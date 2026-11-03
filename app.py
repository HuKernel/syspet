"""SysPet 玻璃拟态版入口（PySide6 UI + syspet 逻辑层，见 docs/UI-Redesign-Handoff.md）。
--notify 参数进入通知捕获子进程（无 Qt 依赖）。"""
from PySide6.QtCore import QTimer
import ctypes
import sys
import time

import psutil

import syspet


def main():
    syspet.setup_log()
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)  # 默认 windll 不保留 LastError
    kernel32.CreateMutexW(None, False, 'SysPetSingleInstance')
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        syspet.toast('SysPet', '已在运行，本次启动自动退出')
        time.sleep(1.2)  # 等 toast 线程发出再退
        return
    for p in psutil.process_iter(['name', 'cmdline']):  # 清理上次强退的孤儿通知子进程
        try:
            cmd = [str(c) for c in (p.info['cmdline'] or [])]
            if '--notify' in cmd and any('syspet' in c.lower() for c in cmd):
                p.kill()
                syspet.log.info('清理孤儿通知子进程 %s', p.pid)
        except psutil.Error:
            pass
    # HiDPI：Qt6 默认启用 per-monitor v2 感知，无需也不应再设 awareness(1)（会冲突告警）
    from PySide6.QtWidgets import QApplication
    import main_window
    from main_window import MainWindow
    app = QApplication(sys.argv)
    app.setStyleSheet(main_window.QSS)
    w = MainWindow()
    w.show()
    if '--shot' in sys.argv:  # 自渲染截图（验收用，无需平台屏幕抓取）
        path = sys.argv[sys.argv.index('--shot') + 1]

        def snap():
            w.grab().save(path)
            app.quit()
        QTimer.singleShot(3500, snap)
    syspet.log.info('盘符: %s', ', '.join(syspet.drives()))
    sys.exit(app.exec())


if __name__ == '__main__':
    if '--notify' in sys.argv:
        syspet.notify_child()
    else:
        main()
