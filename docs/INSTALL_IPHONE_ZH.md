# 装到 iPhone / iPad 并开 JIT（中文）

前提：设备已升到 **iPadOS / iOS 26 或更新**（`IPHONEOS_DEPLOYMENT_TARGET = 26.0`，低于此版本直接拒绝安装）。Apple ID 用你的**港区付费开发者账号**，因为 LocalDevVPN 不在中国区上架。

**本文以 iPad Pro 2025（M4）为主目标**，iPhone 14 Pro Max 作为对照。原因见下面 F 节：iPad 内存更宽裕，但有一条已知的地址空间失败路径，需要先调参数。

两条路，先走 A 验证链路，再走 B 做开发。

---

## A. 最快验证：侧载现成 IPA（不需要 Mac）

这一步只回答一个问题：**你的设备到底跑不跑得起来**。答案是否定的话，后面所有开发工作都没有意义。

1. 下载 `iDeck-0.1.3-alpha.4-build99.ipa` 和 `SHA256SUMS`，核对哈希：
   - alpha.4 / Build 99 的 SHA-256：`c09687a26e6695a329348a869a74bacf004217713d4fd7c223b5263f818552a1`
2. Windows 上装 **Sideloadly**（先装 Apple Devices 驱动）。
3. 用 Apple ID 登录（付费账号建议用 App 专用密码），拖入 IPA，勾选**保留 App 扩展**。
4. 安装完成后，iPhone 上「设置 → 通用 → VPN与设备管理」信任该开发者证书。
5. 「设置 → 隐私与安全性 → 开发者模式」打开，重启设备。

> IPA 是 ad-hoc 签名、不含描述文件，**必须**用你自己的证书重签一次才能装。谁的账号签的，就只有谁能装。

---

## B. 自己构建并签名

```sh
scripts/mac/build.sh --team <TEAMID> --bundle-id com.symi.ideck.steamui
xcrun devicectl device install app --device <UDID> \
  build/DerivedData-ideck/Build/Products/Debug-iphoneos/Madeira.app
```

`--team` 会自动申请带 `get-task-allow` 的开发描述文件；这一步同时也是 JIT 的前置条件。

---

## C. 开 JIT（三条路径，按系统版本选）

JIT 是这台设备上跑 Windows 代码的前提：Wine + FEX 需要在运行时生成可执行内存（`docs/JIT.md:1-6`）。**每次冷启动都要重新开一次**，App 被杀掉就失效（`docs/IDECK.md:29-34`）。

| 路径 | 需要 | 适合 |
| --- | --- | --- |
| **In-App 配对**（推荐） | iOS 27+，Wi-Fi，LocalDevVPN | 不用电脑，手机自己完成配对 |
| 配对文件 + 内置 StikJIT | 电脑生成 pairing 文件 | iOS 26 |
| StikDebug | 装 StikDebug + LocalDevVPN | 任何版本，助手扩展被侧载工具丢掉时用 |

### C1. In-App 配对（iOS 27+）

1. 连 Wi-Fi，打开 iDeck → 首次设置里选 **In-app** → **Start pairing**，允许「本地网络」。
2. 进「设置 → 隐私与安全性 → 开发者模式」，拉到底点 **Pair with Madeira**。
3. 输入 iDeck 显示的配对码（通知里也有一份）。
4. 回到 iDeck → **Check setup** → **Enable JIT**。

配对期间 iDeck 靠后台续期任务保活；如果侧载工具改过 bundle ID 导致被系统拒绝，你只有约 30 秒，动作要快。

### C2. 任意版本的通用流程

1. **LocalDevVPN**（App Store，港区可下）安装并连接。JIT 只走它的环回 `10.7.0.1:62078`，**蜂窝数据下不通**——必须 Wi-Fi 或无网络（`docs/JIT.md:131-137`）。
2. iDeck → **Settings → JIT → JIT setup** → 导入 pairing 文件或选择 StikDebug。
3. **Check setup**（会检查环回、下载并挂载 Developer Disk Image）。
4. **Enable JIT**。

### C3. 注意

- 侧载工具把 `MadeiraJITHelper.appex` 丢掉时，iDeck 会提示助手缺失 → 改走 StikDebug。
- 系统更新后如果提示 Developer Disk Image 过期：**Reset Developer Disk Image** → 重新 Check setup。
- iOS 26 没有 Shortcuts 的 Store Content，JIT 快捷指令那条路走不通，直接用 LocalDevVPN 手动连。

---

## D. 六项检查

打开 iDeck → **Check all prerequisites**，逐项点亮：

| 步骤 | 要做什么 |
| --- | --- |
| 1 本地运行时 | **Prepare runtime**，初始化 Windows 前缀（已有文件不会被覆盖） |
| 2 Steam 与浏览器文件 | 走 `feature/automatic-steam-client-install` 时这里会自动下载；`main` 分支要你自己准备完整客户端 |
| 3 开发签名 | 必须是带调试权限的开发签名，否则重签 |
| 4 JIT 配置 | 见上面 C |
| 5 设备连接 | 提示时连 LocalDevVPN，回来重新检查 |
| 6 本次会话 JIT | **Enable JIT**，再回到 iDeck |

六项变绿后才会解锁 **Launch Steam Big Picture**。它只代表「前提条件齐了」，不代表 Steam 一定登录成功、某个游戏一定跑得起来。

---

## E. 首次进 Steam

- 用**真正的 Steam 界面**登录，Steam Guard 自己过。iDeck 不碰你的密码、令牌和登录缓存。
- 自动安装客户端的分支会从 Valve CDN 拉兼容客户端；进度在启动器里。
- 登录成功后先进库、再点游戏，第一次运行前先回桌面确认设备没被系统杀掉（6 GB 机型尤其要看）。

---

## F. iPad Pro 2025（M4）专项

### F1. 为什么主推 iPad

| 维度 | iPad Pro 2025 (M4) | iPhone 14 Pro Max |
| --- | --- | --- |
| 内存 | 8 / 16 GB（按配置） | 6 GB |
| 屏幕 | 更适合 Big Picture 与手柄 | 偏小，控件遮挡多 |
| 仓库验证状态 | **未验证**，且有公开失败案例 | 未验证（作者验证机是 8 GB 的 15 Pro Max） |
| 已知风险 | 32 位子进程的虚拟地址空间不足 | 内存不足被 jetsam 杀掉 |

两边都不是「官方支持」，但 iPad 的内存更像能扛住 CEF。

### F2. 已知的地址空间失败，先认清日志

`docs/IDECK_IPAD_ADDRESS_SPACE.md:6-20` 记录过一台 iPad（iPadOS 27.0.1 + Build67 IPA）在子进程启动阶段直接失败：

```
[wow-window] refused: map too small (639 MB free VA; a window needs 4096 MB
plus MADEIRA_WOW_MIN_FREE_GB=8 left over)
```

这段话的含义：每个 32 位 Windows 进程都要独占一块 **4 GiB** 的宿主虚拟地址窗口，而且外面还要留 `MADEIRA_WOW_MIN_FREE_GB`（默认 8 GiB）余量。检查发生在 `build/ntdll-unix/virtual_ios.c:10466` 的 `ios_wow_map_allows_window()`。

两个可调门槛（写进 `Documents/madeira.cfg`）：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `env.MADEIRA_WOW_MIN_MAP_GB` | 48 | 整个地址空间小于它就直接拒绝 |
| `env.MADEIRA_WOW_MIN_FREE_GB` | 8 | 留多少 GiB 余量才允许开 4 GiB 窗口 |

判断方法看日志里的数字：

- 剩余 VA **≥ 4 GiB 但不到 12 GiB** → 是「余量门槛」挡的，可以降 `MADEIRA_WOW_MIN_FREE_GB` 试；
- 剩余 VA **< 4 GiB**（上面那台是 639 MB）→ 调门槛没有意义，窗口本身就放不下，这条路在 iPad 上走不通。

降门槛是有代价的：它是为了保护 64 位一侧不被挤爆，调小之后可能换成另一种崩溃。

### F3. 建议的初始 madeira.cfg

App 内的设置在 `Documents/madeira.cfg`，一行一个键（`docs/madeira.cfg.example`）。iPad 首次启动后可以先放这份：

```ini
# 把 guest 内存放到文件-backed，iOS 不把它算进 jetsam 占用（ml1077）
swap-mb = 3072
env.MADEIRA_SWAP_COVERAGE = broad
# 显存与对象池按 iPad 抬高
vram-mb = 1536
pool = 640
totalphys = 4096
# 只有日志显示是「余量」挡住时，才动这一行
env.MADEIRA_WOW_MIN_FREE_GB = 1
```

改完杀掉 App 重开才生效。

### F4. iPad 上的操作差异

- **横屏**：Big Picture 按 16:9 设计，iPad 竖屏会有大黑边，进 Steam 前先转横屏。
- **文件**：`UIFileSharingEnabled` 为 true，Steam 客户端/游戏可以用「文件」App 拷进 iDeck 的 `Documents`。
- **外接手柄**：iPad 直接连蓝牙手柄即可，比 iPhone 好按。
- **分屏/台前调度**：先关掉。窗口尺寸变化会触发重新布局，第一次验证不要引入这个变量。

## G. 排错对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 装不上，报 `0xE8008016` | 描述文件与 entitlement 不匹配（常见于缺 `Increased Memory Limit`） | 后台给 App ID 勾上该能力后重签 |
| 打开就闪退 | 用了 distribution 签名 | 用开发签名重装 |
| `Enable JIT` 一直失败 | LocalDevVPN 没连、走了蜂窝、或另一个全局 VPN 抢了路由 | 关蜂窝、断开其它 VPN、只留 LocalDevVPN |
| 提示助手缺失 | 侧载工具删了 `.appex` | 重装并保留扩展，或改用 StikDebug |
| 六项卡在第 2 项 | 客户端文件不完整（分散目录不算数） | 同一目录必须是完整树：`Steam32Lab` 或 `Steam32BundleLab` |
| 进 Steam 后卡在加载画面 | 已知问题（`README.md:143-147`） | 收集第一失败阶段 + 诊断日志再排查 |
| 玩一会儿被系统杀掉 | 6 GB 内存不足 | 关掉后台 App、降低分辨率/画质，或换 iPad 试 |

**永远不要**把密码、令牌、pairing 文件、描述文件发到公开 issue 里。

---

## H. 数据与卸载

- 登录态、游戏、存档都在 App 的 `Documents` 里（Wine 前缀内）。
- **删除 App 会连它们一起删**（`README.md:51-53`）。升级前先备份。
- 升级安装请保持同一个 bundle ID 和同一个签名团队，否则系统会当成两个 App，数据不互通。
