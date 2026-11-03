# SysPet UI 重设计交接文档（PySide6 + 玻璃拟态）

> 目标：把现有 tkinter 悬浮窗 UI 整体迁移到 **PySide6**，视觉风格定为**玻璃拟态（Glassmorphism）**，业务逻辑（文件监控 / 通知捕获 / AI 用量 / DPAPI 密钥）**全部保留不动**，只重写 UI 层。
>
> 本文档供开发者自行实现使用，给出规格、组件映射、关键实现要点、风险与里程碑，不包含完整成品代码。

---

## 1. 现状盘点（迁移对象）

技术栈：纯 Python + `tkinter`/`ttk`/`Canvas`，单文件 `syspet.py`（1717 行），UI 代码约 500~600 行与业务逻辑混在同一文件。

| 组件 | 位置（`syspet.py`） | 说明 |
|---|---|---|
| `Gauge` 圆角进度条 | 116–145 | 名称 + Canvas 圆角进度条 + 数值，双列布局，共 6 个 |
| `Pet` 主窗口 | 1109–1694 | 无边框、置顶、半透明悬浮窗 |
| `AICfg` 设置窗口 | 880–1107 | `ttk.Notebook` 双 tab：AI 供应商 / 通用设置 |
| Top 进程弹窗 | 1348–1412 | 无边框 Toplevel + Canvas 曲线 + Top5 列表 |
| 事件流 `tk.Text` | 1209–1225 | 两行制：摘要行 + 路径行，标签配色 |

现有配色（浅色苹果风）：

```
BG '#f7f7f9'  FG '#1d1d1f'  DIM '#86868b'  TRACK '#e3e3e8'  ACCENT '#007aff'
事件色：新增 '#34c759' 删除 '#ff3b30' 修改 '#c9860a' 改名 '#5856d6' 通知 '#af52de' 告警 '#ff3b30'
负载 mood：<60 绿 / <90 橙 / else 红
设置窗：WBG '#f7f7f9'  CARD '#ffffff'  LINE '#d8d8dc'
```

---

## 2. 技术迁移总览

### 2.1 依赖变更

- **移除**：无（tkinter 是标准库，不打包也不删，逻辑层不依赖它即可）。
- **新增**：`PySide6>=6.6`（只用 `QtWidgets` / `QtCore` / `QtGui`，不引 `QtQml`/`QtQuick` 可减小体积）。
- **打包**：PyInstaller 加 `--collect-all PySide6` 或使用 `pyside6` 官方 hook；成品体积预计 **50~60MB**（onefile，自解压后更大）。`syspet.spec` 的 `hiddenimports` 需追加 Qt 平台插件相关项（通常 `--windowed` + PySide6 hook 自动处理）。

### 2.2 API 对照（迁移时逐条替换）

| tkinter | PySide6 | 备注 |
|---|---|---|
| `tk.Tk()` | `QApplication` + `QWidget` | 需先建 `QApplication` |
| `overrideredirect(True)` | `Qt.FramelessWindowHint` | 配合 `WA_TranslucentBackground` 做圆角 |
| `attributes('-topmost', True)` | `Qt.WindowStaysOnTopHint` | |
| `attributes('-alpha', x)` | `setWindowOpacity(x)` | 悬停聚焦用 |
| `tk.Canvas` + `create_polygon` | `QPainter.drawRoundedRect` | 抗锯齿、渐变、发光 |
| `tk.Label` | `QLabel` | |
| `tk.Text`（事件流） | `QListWidget` + 自绘 item / `QTextEdit` | 见 §5.5 |
| `tk.Menu` | `QMenu` | 右键菜单 |
| `tk.Toplevel` | `QDialog` 或 `QWidget(Qt.Popup)` | 弹窗 |
| `ttk.Notebook` | `QTabWidget` | 设置窗口 |
| `tk.Entry` | `QLineEdit` | `setEchoMode(QLineEdit.Password)` 遮罩 |
| `tk.Checkbutton` | `QCheckBox` | |
| `tk.Button` | `QPushButton` | |
| `tk.Listbox` | `QListWidget` | |
| `after(ms, fn)` | `QTimer.singleShot` / `QTimer` | `tick()` 主循环 |
| `bind('<Button-1>')` | `mousePressEvent` / `eventFilter` | |
| `winfo_x/y` | `self.x()` / `self.y()` | |
| `geometry(f'+x+y')` | `self.move(x, y)` | |
| `grid` / `pack` | `QGridLayout` / `QVBoxLayout` / `QHBoxLayout` | 布局重写 |

---

## 3. 玻璃拟态设计系统（Design Tokens）

玻璃拟态在**深色底**上表现最好（浅色玻璃对比度低、炫酷感弱），故整体从浅色转为深色玻璃。以下 Token 建议集中定义在一个常量模块（或沿用现有全局常量风格）中统一引用。

### 3.1 颜色

```
背景        #0d0f14         窗口底色（半透明，见 §6.2）
玻璃面板    rgba(255,255,255,0.055)  卡片/仪表底
描边        rgba(255,255,255,0.12)   1px 高光边（玻璃边缘）
主文字      #e8e8ec
次文字      #9a9aa2
强调 accent  #0a84ff → #7c5cff  蓝紫渐变（主色 + 高亮）
成功/正常    #34c759
警告        #ff9500
危险        #ff3b30
弥散光斑 A   rgba(76,194,255,0.35)   青，左上
弥散光斑 B   rgba(124,92,255,0.30)   紫，右下
```

事件类别色沿用现有语义（用户已熟悉），只需提亮适配深底：

```
新增 #30d158  删除 #ff453a  修改 #ff9f0a  改名 #5e5ce6  通知 #bf5af2  告警 #ff453a
```

### 3.2 形状

```
窗口圆角   16px
卡片圆角   12px
仪表圆角   6px（进度条）/ 环形仪表见 §5.2
按钮圆角   8px
```

### 3.3 字体

```
正文  'Microsoft YaHei UI'  9pt
数值  'JetBrains Mono' / 'Consolas'（Qt 中用 QFontDatabase 注册，或回退 Consolas）
标题  'Microsoft YaHei UI'  10pt bold
```

### 3.4 动效（统一规格）

| 动效 | 规格 | 实现 |
|---|---|---|
| 数值过渡 | 仪表/网速/用量数值 200ms 缓动到新值 | `QVariantAnimation` 或 `QPropertyAnimation` |
| 呼吸光斑 | 弥散光斑 3s 缩放/透明度循环，缓入缓出 | `QPropertyAnimation` + 循环 |
| 进度流光 | 进度条高亮末端 1.5s 左右扫光（可选） | `QTimer` 驱动重绘 offset |
| 悬停聚焦 | 窗口 opacity 0.72→1.0，150ms | `QPropertyAnimation` on `windowOpacity` |
| 边缘滑出 | 指数逼近 150ms（沿用现逻辑） | `QTimer` + `move()` |
| 卡片入场 | 事件卡片淡入 120ms（可选） | `QGraphicsOpacityEffect` |

---

## 4. 组件级迁移规格

### 4.1 主窗口 `Pet`

- **形态**：无边框 + 圆角 + 半透明 + 置顶，保持紧凑竖条，窗口尺寸由内容撑开（对应现有 `pack` 自适应）。
- **结构**（自上而下，全部用 `QVBoxLayout`）：
  1. **头部**：标题「SysPet」+ 右侧「网速 · 今日流量 Σ · 时钟」。头部做玻璃高光面板。
  2. **仪表区**：6 个仪表，2 列网格（`QGridLayout`），顺序与现有一致：CPU / GPU、内存 / 显存、C盘 / 温度。
  3. **AI 用量行**：每个供应商一行（名称 + 余额/用量），数据版本变化时重建。
  4. **分隔线**：1px 玻璃描边。
  5. **事件流**：见 §4.5。
  6. **底部提示**：`HINT` 文案 + 临时 `_flash` 消息。
- **右键菜单**：版本(disabled) / 设置… / 开机自启动(check) / 退出，映射到 `QMenu`。
- **交互保留**：悬停聚焦、边缘滑出/滑回、拖动移动 + 位置记忆（`pos` 存 `syspet.json`）。

### 4.2 仪表 `Gauge`（重点改造）

**推荐方案 A：渐变发光进度条**（改动小、保持紧凑）
- `QPainter` 绘制：圆角轨道（半透明白）→ 圆角填充（`mood` 色 → 同色系高光，`QLinearGradient` 从左到右）→ 末端 2px 高光点。
- 数值用 `QLabel`，等宽字体；负载色沿用 `mood(pct)` 三档。

**可选方案 B：环形仪表（ring gauge）**（更炫酷，窗口变高）
- `QPainter.drawArc` 画 270° 环形，`QConicalGradient` 随负载绿→黄→红；中心显示数值。适合想「一眼炫」的场景，代价是 6 个环会明显拉高窗口。

> 建议先做 A 保底，B 作为增强项自行选择。

### 4.3 设置窗口 `AICfg`

- 用 `QDialog` + `QTabWidget` 替代 `Toplevel` + `Notebook`。
- **Tab1 AI 供应商**：`QListWidget`（供应商列表，行文本 `name  url  key[:5]***`）+「新增/删除」按钮 + 字段区（名称/地址/Key/访问令牌/UID，`QLineEdit`，Key 与令牌用 `Password` 遮罩）+「测试连接」。
- **Tab2 通用设置**：三个短输入框（告警阈值 / 久坐间隔 / 待机透明度）+ 5 个 `QCheckBox`（toast 分类开关）。
- **校验逻辑**原样保留（`_save` 里的完整度、token/uid 成对、数值范围校验）。
- 底部：状态文案 +「保存并关闭」主按钮（accent 渐变背景）。

### 4.4 Top 进程弹窗

- `QWidget(Qt.Popup | Qt.FramelessWindowHint)`。
- 标题 + 历史曲线（`QPainter.drawPolyline` 抗锯齿折线，330×80，带网格线）+ Top5 列表（`QLabel` 逐行）。
- 8 秒 `QTimer.singleShot` 自动关 + 点击关闭，位置逻辑（下放不下弹上、x 夹屏内）原样保留。

### 4.5 事件流（重点改造）

从 `tk.Text` 两行制 → **`QListWidget` + 自绘卡片**，是最能体现「炫酷」的一处：

- 每个事件一个 item，卡片布局：左侧 3px 类别色条 + 首行「时间 + 类别标签」+ 次行「路径（后缀高亮）/ 大小变化」。
- 类别标签用圆角胶囊（`QPainter` 或带 `border-radius` 的 QLabel），颜色对应事件类别。
- 通知/告警项：无大小行，显示文案；点击行为保留（文件 `os.startfile`、通知打开通知中心、告警无动作）。
- 右键菜单「打开 / 打开方式… / 在文件夹中显示」保留。
- 增量更新沿用现有 `_insert_ev` 的「顶插 + KEEP 截断 + 首行合并替换」逻辑，只是渲染目标换成 `QListWidget`（`insertItem(0, ...)`）。

---

## 5. 关键实现要点（PySide6）

### 5.1 无边框圆角透明窗口

```python
self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
self.setAttribute(Qt.WA_TranslucentBackground)   # 圆角外透明
```

圆角与阴影：`WA_TranslucentBackground` 下，在 `paintEvent` 里先画一个 16px 圆角矩形作底板，再叠加玻璃面板；阴影用 `QGraphicsDropShadowEffect`（对透明背景窗口，阴影需包一层子 widget，否则无效）。

### 5.2 真·毛玻璃的取舍（重要）

Windows 桌面没有 Qt 原生的 backdrop-blur。三条路径：

1. **Acrylic（推荐，Win10 1803+ / Win11）**：通过 `ctypes` 调 `SetWindowCompositionAttribute`，`ACCENT_ENABLE_ACRYLICBLURBEHIND` + 可选 tint 色。注意：**acrylic 与 `WA_TranslucentBackground` 组合时，圆角边缘会露系统直角**，需接受「整窗直角」或用 2px 内边距规避；且 acrylic 会让 QSS 半透明面板也整体变糊，层次感下降。
2. **纯 QSS 模拟玻璃（最稳，跨版本一致）**：半透明深色底板 + 弥散光斑（`QRadialGradient` 画在 `paintEvent`）+ 1px 白色高光描边。**没有真模糊**，但「玻璃质感」主要靠光斑 + 高光 + 描边营造，视觉上已接近，且 Win10/11 全兼容、无系统 API 坑。
3. **QGraphicsBlurEffect 局部模糊**：性能差，不推荐。

> **结论**：先做方案 2（模拟玻璃）保证全版本稳定；若目标用户主要在 Win11，可加方案 1（acrylic）作为增强，需自行验证圆角与层次。

### 5.3 弥散光斑（玻璃拟态的灵魂）

在窗口 `paintEvent` 里画 2~3 个低透明度 `QRadialGradient` 圆斑（青、紫、可选粉），叠加呼吸动画（§3.4）。光斑让「玻璃」有通透的层次感，比纯半透明高级得多。

### 5.4 主循环 `tick()` 迁移

现有 `tick()` 每秒更新逻辑**全部保留**（CPU/内存/磁盘/网速/时钟/流量/AI/事件流/告警/久坐），只把「写控件」部分换成 PySide6 调用，末尾 `self.root.after(1000, self.tick)` → `QTimer` 每秒 `timeout` 信号。

### 5.5 后台线程 → UI 回传

现有大量 `threading.Thread` + `root.after(0, ...)` 回传 UI。PySide6 下**严禁在非主线程直接操作 widget**，改用信号槽：

```python
class Bridge(QObject):
    sig = Signal(object)          # 通用数据回传
# 子线程里 bridge.sig.emit(data)；主线程 bridge.sig.connect(handler)
```

涉及点：`ai_net_loop` / `gpu_loop` / `drive_manager` / `check_update` / 通知子进程回读（`_read_notify`）/ Top5 采样（`_top_fill`）。

---

## 6. 必须保留的原生能力（勿动，只改调用层的 UI 出口）

以下模块与 UI 无关，**原样保留**，仅在 UI 层接它们的数据：

- 密钥：`enc` / `dec` / `upgrade_keys`（DPAPI）
- 文件监控：`listen_drive` / `handle_batch` / `push_event` / `drive_manager`（ReadDirectoryChangesW）
- 通知捕获：`notify_child` / `scan_toasts` / `_read_notify`（UIA + 子进程管道）
- AI 用量：`fetch_one` / `zhipu_usage` / `newapi_account` / `deepseek_usage` / `ali_usage` / `test_provider`
- 系统：`gpu_probe`（nvidia-smi）、`cpu_utility_*`（PDH）、`net_connected` / `net_ifaces`（NLM）、`top_procs`
- 其他：`autostart_*`（注册表）、`toast`（Win 原生通知）、`check_update`、`traffic_load` / `cfg_*`（配置读写）、`setup_log`

单实例锁、孤儿通知子进程清理（`__main__` 段）也保留，仅把 `tk.mainloop()` 换成 `app.exec()`。

---

## 7. 迁移步骤与里程碑

| 里程碑 | 内容 | 验证标准 |
|---|---|---|
| M1 骨架 | 建 `app.py` / `main_window.py`，无边框圆角透明窗口 + 深色玻璃底板 + 光斑 + 阴影 | 空窗能显示、圆角透明、置顶、可拖动 |
| M2 仪表 | `Gauge` 迁移 + 渐变发光 + `tick` 数据接入 | 6 仪表实时刷新、负载变色、点击弹 Top/切盘 |
| M3 头部 + AI 行 | 标题/网速/流量/时钟 + AI 用量行 | 网速时钟每秒刷新、AI 数据自动回传刷新 |
| M4 事件流 | `QListWidget` 卡片 + 类别色条 + 交互 | 新增/删除/通知等正确显示、点击/右键可用 |
| M5 设置窗口 | `QDialog` 双 tab + 校验 + DPAPI 保存 | 增删改供应商、热生效、透明度即时生效 |
| M6 弹窗 | Top 进程弹窗 + 曲线 + 历史流量 toast | 曲线抗锯齿、8s 自动关 |
| M7 交互收尾 | 悬停聚焦、边缘滑出、位置记忆、动效打磨 | 与旧版交互一致 + 动效流畅 |
| M8 打包 | PySide6 hook + `syspet.spec` 更新 + 全量回归 | exe 单文件运行、首启慢 1~2s 属正常 |

建议：保留旧 `syspet.py` 为 `syspet_tk_backup.py` 或在新分支开发，逻辑层逐步 `import` 复用而非复制，避免两份逻辑漂移。

---

## 8. 风险与坑

1. **体积暴增**：PySide6 onefile 约 50~60MB，启动自解压慢 1~2 秒、内存占用上升。用户若介意，可评估 `PySide6-Essentials`（去掉 Qt 多余模块）或改 `onedir` 打包。
2. **真毛玻璃跨版本差异**：acrylic 在 Win10 早期版本不支持 / 与圆角透明窗口冲突（§5.2）。务必以「模拟玻璃」为基线。
3. **线程安全**：所有后台线程回调 UI 必须走信号槽，否则偶发崩溃（比 tkinter 的 `after` 更严格）。
4. **无边框窗口拖拽**：PySide6 无边框窗需手动实现 `mousePressEvent`/`mouseMoveEvent` 拖动，且要区分「点击仪表/事件」与「拖动窗口」（现有 `break` 抑制逻辑要用 `event.accept()` 等效替代）。
5. **HiDPI**：现有 `SetProcessDpiAwareness(1)` 需保留，否则 Qt 高分屏下坐标错位；QPainter 绘制会自带缩放，注意圆角/字号在 125%/150% 下的观感。
6. **字体**：`JetBrains Mono` 非系统自带，打包/分发需回退 `Consolas`，或随包携带字体（增加体积）。
7. **通知子进程**：`--notify` 子进程与 UI 解耦（纯管道），迁移时**不要**让它依赖 Qt，否则子进程也要打包 Qt。

---

## 9. 交付边界

- 本文档交付：设计 Token、组件映射、关键实现要点、里程碑、风险。
- 不交付：完整 PySide6 成品代码（由开发者按本规格实现）。
- 逻辑层（§6 列表）不修改，仅 UI 层重写，接口以现有 `self.events` / `self.ai_state` / `self.gpu_state` / `_hist` / `_tr` 等字段为准。
