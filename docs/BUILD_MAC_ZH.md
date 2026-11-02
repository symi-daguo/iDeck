# 在 Mac 上构建 iDeck（中文）

这份文档是「克隆下来 → 打开 Xcode 前」的全部步骤。目标是让一台干净的 Mac 用两条命令产出可侧载的 IPA。

> **先说三件事，避免白折腾**
>
> 1. 公开源码是**纯源码快照**：编译好的 Wine/FEX/DXMT 运行时、20 个静态库、StikJIT、微软 VC++ 运行库都不在仓库里（`.gitignore` 第 173-199 行）。少了它们，Xcode 一定报错。
> 2. JIT 需要 `get-task-allow`，只有**开发描述文件**带这个权限。TestFlight 和 App Store 永远不会有它（`app/Madeira/Madeira.entitlements:10`、`docs/JIT.md:1-6`）。
> 3. 必须用 **Debug** 配置。Release 会让 guest 崩溃（`docs/BUILDING.md:68`）。

---

## 1. 机器要求

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| 系统 | macOS（Apple Silicon 或 Intel 均可） | `uname -m` |
| Xcode | 27 或更新，iOS SDK ≥ 26.0 | `xcrun --sdk iphoneos --show-sdk-version` |
| Python | 3.9+（系统自带即可） | `python3 -V` |
| 磁盘 | **≥ 15 GB**（IPA 659 MB + 静态库 112 MB + FEX 55 MB + DerivedData 3-6 GB） | `df -h .` |
| 网络 | 需要能访问 GitHub；国内建议走镜像（见下） | — |

Xcode 命令行工具没装的话：`xcode-select --install`，并在 Xcode 里登录你的开发者账号（`Xcode → Settings → Accounts`）。

---

## 2. 选哪个分支

仓库里有两个可构建的分支，差别是**能不能自动拿到 Steam 客户端**：

| 分支 | 内容 | Steam 客户端 | 适合 |
| --- | --- | --- | --- |
| `main` | Build 135/136 的 CEF64（AMD64 Steam）进展 | **要自己准备**，32 位 Steam 已于 2026-01-01 停服，官方已无法获取 | 想跟进最新代码 |
| `feature/automatic-steam-client-install` | Build 99 发布版，发布说明是「Steam, one download away」 | **App 内自动下载安装** | 想真正玩起来 |

`main` 上**没有** `SteamClientInstaller.swift`，也没有 `SteamClientPackages.json`。这是今晚核对出来的关键差异：`git diff main...feature/automatic-steam-client-install` 显示该分支新增这两个文件，且比 main 落后 26 个提交。

**建议**：先按 `feature/automatic-steam-client-install` 构建，把链路玩通；之后想跟进 CEF64 再切 `main`。

```sh
git clone https://github.com/symi-daguo/iDeck.git
cd iDeck
git checkout feature/automatic-steam-client-install
```

---

## 3. 一条命令补齐所有输入

```sh
python3 tools/ideck-bootstrap.py --mirror https://ghfast.top/
```

它会按顺序做七件事，每步都可重复执行（已存在的会跳过）：

| 步骤 | 做什么 | 体积 |
| --- | --- | --- |
| 1 | 拉 FEX 源码 + 3 个头文件子模块（fmt / range-v3 / unordered_dense），固定在 `f59cab95` | ~55 MB |
| 2 | 下载并校验 IPA（`v0.1.3-alpha.4-build99`），把 14 个运行时目录铺到 `app/Madeira/` | 159 MB → 659 MB |
| 3 | 铺 StikJIT.framework（JIT 助手链接需要） | 8 MB |
| 4 | 解包 relink 包，把 20 个静态库放到工程要求的 FEX / app 路径 | 48 MB → 112 MB |
| 5 | 下载 llvm-mingw（构建期要编译 `ideck-control.exe`） | 116 MB |
| 6 | 补微软 VC++ 运行库（见下） | ~10 MB |
| 7 | 生成许可证副本 + 跑预检 | — |

`--mirror` 可以换成任何能代理 `github.com` 的前缀，例如 `https://gh-proxy.com/`。它同时作用于两处：HTTP 下载（IPA / relink / llvm-mingw）和 FEX 的 git fetch（通过 `url.<mirror>.insteadOf` 重写）。不走镜像就省略这个参数。

### 关于微软 VC++ 运行库

`app/Madeira/x86_64-vcruntime/` 需要 14 个 DLL。微软只允许**原样**再分发，所以仓库不存、脚本也不替你下载。

- **在 Windows 上**：脚本会自动从 `C:\Windows\System32` 复制（合法来源，见 `tools/fetch-vcruntime.md`）。
- **在 Mac 上**：先装 7-Zip 再抽：

```sh
brew install sevenzip
curl -LO https://aka.ms/vs/17/release/vc_redist.x64.exe
7zz x vc_redist.x64.exe -o/tmp/vcredist
7zz x '/tmp/vcredist/.rsrc/1033/CABINET/*.cab' -o/tmp/vcrt
python3 tools/ideck-bootstrap.py --vcruntime-dir /tmp/vcrt
```

缺这 14 个 DLL **不影响编译**（目录按原样拷进包），只影响 x86_64 客机程序。预检会把它标成 warn 而不是 fail。

---

## 4. 构建

### 4.1 出 IPA（不需要设备、不需要团队）

```sh
scripts/mac/build.sh --bundle-id com.symi.ideck.steamui
```

产物：`build/iDeck-<时间>-<bundle-id>.ipa`，并自动跑一遍包校验。

### 4.2 直接装到插着的设备（需要 Apple 开发者团队）

```sh
scripts/mac/build.sh --team <你的TEAMID> --bundle-id com.symi.ideck.steamui \
  --install --device <UDID>
```

不知道 UDID 就先不加 `--device`：脚本会签名、打包，然后打印 `xcrun devicectl list devices` 的结果，你挑一台再重跑。

`--team` 会打开自动签名，Xcode 会自动为 app 与 `MadeiraJITHelper` 申请带 `get-task-allow` 的开发描述文件，并把 `Increased Memory Limit` 能力加到 App ID 上。打包前脚本会自动跑一次产物校验，有 FAIL 就停下不打包。装完之后手动启动一次，确认没被系统秒杀。

### 4.3 生成的命令行（万一要手敲）

```sh
export LLVM_MINGW="$PWD/toolchains/llvm-mingw-20260421-ucrt-macos-universal"
xcodebuild -project app/Madeira.xcodeproj -scheme Madeira \
  -configuration Debug -destination 'generic/platform=iOS' \
  -derivedDataPath build/DerivedData-ideck \
  CODE_SIGNING_ALLOWED=NO CURRENT_PROJECT_VERSION=200 \
  IPHONEOS_DEPLOYMENT_TARGET=26.0 \
  MADEIRA_BUNDLE_IDENTIFIER=com.symi.ideck.steamui \
  LD_GENERATE_MAP_FILE=YES build
```

---

## 5. 能验证到哪一层（含：模拟器为什么不行）

### 5.1 iOS 模拟器跑不了，这不是配置问题

想把 App 装进模拟器点一下，是不可能成功的，原因写在代码里：

| 阻碍 | 证据 |
| --- | --- |
| 静态库只有设备版 | relink 的链接目标是 `arm64-apple-ios26.0`（`link-args.json` 的 `-target`），没有 simulator 变体，链接阶段直接报 `building for iOS Simulator, but linking in object file built for iOS` |
| JIT 在模拟器里不存在 | `docs/JIT.md:46-48` 原文：内置 StikJIT「is unavailable in the simulator and inside LiveContainer」 |
| 没有 JIT 就没有 Wine | FEX 要在运行时生成可执行内存，模拟器不提供这条路径 |

`tools/ideck-verify-app.py` 会检查 Mach-O 的 `LC_BUILD_VERSION` 平台字段，一旦发现是 Simulator 产物就直接判 FAIL，避免把错包签出去。

### 5.2 macOS 虚拟机可以做什么

| 场景 | 可行性 |
| --- | --- |
| 在 macOS 虚拟机里装 Xcode、`bootstrap`、`xcodebuild`、打包 IPA | **可以**，只要能分配 ≥ 8 GB 内存、≥ 40 GB 磁盘，并装 Xcode 27 |
| 在虚拟机里跑 iOS 模拟器看界面 | 可以启动模拟器，但装不进这个 App（见 5.1） |
| 在虚拟机里做签名并装真机 | 需要把 iPad 直通进虚拟机（USB 直通），否则 `devicectl` 看不到设备 |
| 在虚拟机里验证 JIT / Steam / 游戏 | **不行**，虚拟机没有 A 系列/M 系列真机的 JIT 路径 |

结论：虚拟机只适合验证「能不能编译出完整包」，功能验证必须回到真机。

### 5.3 验证阶梯

| 层 | 在哪做 | 命令 | 能证明什么 |
| --- | --- | --- | --- |
| 1 输入完整性 | 任意系统 | `python3 tools/ideck-preflight.py` | 工程引用的每个输入都在位（35 项） |
| 2 编译链接 | Mac / macOS 虚拟机 | `scripts/mac/build.sh` | 代码能过 Swift 编译、C/ObjC 编译和链接 |
| 3 产物结构 | 任意系统 | `python3 tools/ideck-verify-app.py <.app>` | 是设备版、arm64、扩展与运行时目录齐全、权限正确（19 项） |
| 4 包结构 | 任意系统 | `python3 tools/ideck-verify-ipa.py <.ipa>` | 扩展没被丢、bundle id 正确、get-task-allow 在（15 项） |
| 5 安装 | 真机 | Sideloadly / `devicectl` | 签名与描述文件被设备接受 |
| 6 JIT | 真机 | App 内 Enable JIT | 调试器能附加、可执行内存能分配 |
| 7 功能 | 真机 | 进 Steam、登录、开游戏 | 真正的可用性 |

第 2 步是唯一必须 macOS 的一步，其余都能在任意系统先跑一遍。

## 6. 这一步之前该看到什么

`tools/ideck-bootstrap.py` 结尾会跑 `tools/ideck-preflight.py`，正常输出：

```
35 checks: 34 ok, 1 warn, 0 fail
READY with warnings. Warnings above do not stop xcodebuild.
```

那 1 个 warn 通常是 llvm-mingw 或 vcruntime，按提示处理即可。任何 `[FAIL]` 都意味着 `xcodebuild` 会在同一个地方失败，只是报错更晚、更难读。

预检读的是 `project.pbxproj` 本身，不是硬编码清单。它检查：

- Frameworks 阶段引用的 **20 个静态库**是否都在（含 FEX 4 个子路径）
- Resources 阶段的 **8 个文件夹**是否存在且内容够数（`arm64ec-windows` 264、`i386-windows` 729、`aarch64-windows` 135）
- `StikJIT.framework` 二进制、modulemap、Swift 头
- 许可证副本是否与 `COPYING` / `LICENSE-EXCEPTION.md` **逐字节一致**（不一致构建期直接 fail）
- `Info.plist`：`IDeckEnabled`、`CFBundleURLSchemes`、`UIFileSharingEnabled`
- entitlements：`get-task-allow` 必须为 true
- FEX 头文件 5 个 include 根

---

## 7. 常见失败对照

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `'FEXCore/Core/Context.h' file not found` | FEX 源码没拉，或子模块没进 | `python3 tools/ideck-bootstrap.py` |
| `ld: library not found for -lFEXCore` | 静态库落在 `app/Madeira/`，但工程从 `FEX/build-ios/...` 找 | 重新跑第 4 步（会自动铺到 FEX 子路径） |
| `error: bundled LICENSE-MADEIRA-GPL-3.0.txt is missing or stale` | 许可证副本没生成 | `bash build/stage-licenses.sh` |
| `iDeck control helper requires LLVM_MINGW` | 没装 llvm-mingw 或没导出 | `export LLVM_MINGW=$PWD/toolchains/llvm-mingw-*/` |
| `x86_64-vcruntime: No such file or directory` | 空目录被清理掉了 | `mkdir -p app/Madeira/x86_64-vcruntime` 后重跑 bootstrap |
| 签名报 `Entitlements not permitted` | 团队没开 `Increased Memory Limit` | 开发者后台给 App ID 勾上该能力，或改用付费团队 |
| 装完点开闪退 | 用了 distribution 描述文件，没有 `get-task-allow` | 重新用开发签名 |
| 构建卡在 `Bundle Wine Mono` | `build/wine-mono/` 没有 Mono 包（可选） | 正常现象，会以「无 Mono」继续；`.NET` 游戏才需要 |
| Xcode 报 SDK 太旧 | Xcode 26 或更早 | 装 Xcode 27（iOS 26 SDK 起） |

---

## 8. 已经验证过什么，没验证什么

| 项目 | 状态 | 证据 |
| --- | --- | --- |
| 引导脚本全流程（含 FEX 拉取、静态库落位、vcruntime） | **已验证**，在 Windows 上真实执行 | 预检 `35 ok / 0 fail` |
| 预检本身与发布的 IPA 对齐 | **已验证** | 对 Build 99 IPA 跑 `ideck-verify-ipa.py`：15/15 通过 |
| 产物校验器 | **已验证** | 对 Build 99 解包出的 `.app` 跑 `ideck-verify-app.py`：19/19；畸形包负向测试：6 项 FAIL、退出码 1 |
| 两个分支的 `project.pbxproj` 都能解析 | **已验证** | main 152 refs / feature 154 refs，均 20 库 8 目录 |
| `xcodebuild` 编译与链接 | **未验证** | 只能在 Mac 上做，Windows 无 iOS SDK |
| 签名、安装、JIT、进 Steam、跑游戏 | **未验证** | 必须真机 |

也就是说：**结构完整性已经证明，编译与运行还没有**。第一条命令跑完如果预检全绿，剩下的风险就集中在 Xcode 版本、签名团队能力和真机内存。

---

## 9. 相关文档

- `docs/INSTALL_IPHONE_ZH.md` — 装到 iPhone、开 JIT、第一次进 Steam
- `docs/BUILDING.md` — 上游的完整构建记录（含从源码重编 FEX/Wine/DXMT，那是以「小时」为单位的事）
- `docs/JIT.md` — JIT 三种路径的原始说明
- `docs/IDECK_IPAD_ADDRESS_SPACE.md` — iPad 上已知的地址空间失败案例
