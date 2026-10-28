# SysPet — 桌宠式系统监控悬浮窗

Windows 桌面常驻小工具：无边框置顶半透明悬浮窗，实时显示系统指标、全盘文件变化、AI 平台用量。

## 功能

- **系统仪表**：CPU / 内存 / C盘 / GPU / 显存（圆角进度条，随负载绿→黄→红变色）
- **网速**：实时上/下行速率（标题带内）
- **全盘文件监控**：Windows 内核通知（`ReadDirectoryChangesW`），所有本地盘 + 热插拔 U 盘实时捕获新增/修改/删除/改名，含修改前后大小快照；滚轮翻看最近 200 条，**单击打开文件、右键选择打开方式/资源管理器定位**
- **AI 平台用量**（右键 → 配置 AI 供应商，保存后 5 秒内自动生效）：

| 平台 | 配置 | 显示 |
|---|---|---|
| 智谱 Coding Plan（bigmodel.cn / z.ai） | API key | 已用 % · 重置时间 · 套餐 |
| new-api / one-api 系中转站 | API key | 余 $ · 累计 $ |
| 同上（需精确账号余额） | + 控制台令牌 + UID | 账号余额 $ |
| 阿里百炼官方（dashscope） | AccessKeyId + Secret（需 BSS 只读权限） | 余 ¥ · 本月 ¥ |

- **开机自启动**：右键菜单开关（注册表 HKCU Run，免管理员）
- **密钥加密**：供应商 key/令牌经 Windows DPAPI 加密落盘（绑定当前用户，Chrome 保存密码同款机制），`syspet.json` 中不再存明文；旧明文配置仍兼容，下次保存时自动升级为密文
- **运行日志**：`syspet.log`，滚动保留 1MB × 2 备份，含全量文件事件审计

## 启动

- **普通用户**：从 [Releases](https://github.com/HuKernel/syspet/releases) 下载 `syspet.exe`，双击即用（单文件绿色版，无需安装 Python；配置 `syspet.json` 与日志自动生成在 exe 同目录）
- **开发者**：`python syspet.py` 或双击 `启动.bat`（需 pythonw，依赖：`pip install -r requirements.txt`）
- 重新打包 exe：`pyinstaller --onefile --noconsole --name syspet syspet.py`

## 交互

- 按住窗口任意处拖动；右键 → 配置 AI 供应商 / 开机自启动 / 退出
- 事件列表：滚轮翻看、单击打开文件、右键打开方式 / 文件夹定位

## 文件

```
syspet.py        主程序（单文件）
test_syspet.py   自检测试：python test_syspet.py
启动.bat         启动器（pythonw 无控制台）
syspet.json      AI 供应商配置（key 为 DPAPI 密文，勿提交/分享）
syspet.log       运行日志
```

## 噪音排除

未配置 `only` 时默认只监控**用户文件区**（真实桌面 / 文档 / 下载目录，自动识别重定向），后台应用目录的变化全部静音。内置排除系统位置：`C:\Windows\`、`C:\ProgramData\`、`C:\Program Files*\`、`AppData\Local\Temp`、回收站、pagefile、NTFS 元数据等；**所有 `.log` 文件**（应用日志高频刷写，含 SysPet 自身日志，防止监控自反馈）；目录自身的"修改"事件（条目变化，无信息量）也静音。

应用后台噪音可在 `syspet.json` 追加（小写子串匹配，改后 5 秒内自动生效）：

```json
{
  "providers": ["..."],
  "exclude": ["sogoupy.users", "netease", "zcode_data"]
}
```

## 注意

- `syspet.json`（密钥配置）与 `syspet.log*`（含文件路径与账户用量）均已列入 `.gitignore`，不会进入仓库
- 密文仅当前 Windows 用户本机可解：换机器/用户后需重新填写 key（属预期安全行为）
- 阿里云 AccessKey 建议使用仅授权 `AliyunBSSReadOnlyAccess` 的子账号
