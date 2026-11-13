# ChannelsWatch — 视频号自动备份（macOS）

菜单栏常驻小工具：当你在微信里**收藏/点赞**（或**转发**，可选）一条视频号视频时，
它自动把该视频下载到本地目录，做你自己的内容备份。

- ✅ 全自动：微信里点收藏，几秒后视频落进 `~/Movies/wechat-channels`
- ✅ 零依赖：dmg 拖拽安装，内嵌全部运行时（Python、ffmpeg）
- ✅ 本地运行：无账号、无上传、无遥测，数据不出你的电脑

> **用途声明**：本工具仅用于备份你在微信视频号中主动收藏/转发的内容，
> 请尊重内容创作者的著作权，勿用于二次分发。

## 安装

1. 从 [Releases](../../releases) 下载 `ChannelsWatch.dmg`
2. 打开 dmg，把 **ChannelsWatch** 拖进 **Applications**
3. 首次打开若提示「无法验证开发者」：右键点击 app → **打开**（或在终端执行 `xattr -cr /Applications/ChannelsWatch.app`）
4. 首次启动会请求 **「完全磁盘访问」** 授权——工具需要读取微信的本地缓存才能工作，全程只在你的电脑上完成

> 未公证说明：本工具使用个人开发者证书签名，未加入 Apple 公证计划。
> 这是免费开源工具的常见状态，源码全部公开，可自行审查或从源码构建。

## 使用

1. 菜单栏出现 ▶︎↓ 图标，点开面板
2. 打开微信 → 视频号 → 找到想备份的视频 → **收藏**（或长按转发）
3. 几秒后面板显示下载进度，完成后系统通知，视频在「打开目录」里

**触发方式可在面板里切换**：`收藏/点赞` 与 `转发` 两个开关独立控制，即时生效。

## 工作原理（透明化说明）

微信播放视频号视频时，会把作品的播放地址、标题等元数据写入本机微信容器的
本地数据库；收藏/转发动作会让该记录可被识别。本工具只做两件事：

1. 监视微信容器内的本地缓存数据库（只读，不修改微信任何数据）
2. 用缓存里的播放地址把视频流下载到本地并校验完整性（ffmpeg full-decode）

它**不能**：绕过付费内容、读取你的聊天记录、上传任何数据。

## 已知限制

- 仅支持 macOS + 微信 4.x（Apple Silicon；Intel 未验证）
- 视频号 tab 内直接播放的视频不走该缓存，请通过收藏/转发触发
- 微信大版本更新可能改变缓存格式，届时需要等适配更新

## 从源码构建

```sh
# 依赖：Swift 5.9+（Xcode 命令行工具）、Python 3.12、curl
# 1) 建构建环境（PyInstaller 用来冻结 worker）
python3.12 -m venv .build-venv
.build-venv/bin/pip install --no-deps python-snappy==0.7.3
.build-venv/bin/pip install zstd cramjam==2.11.0 pyinstaller
.build-venv/bin/pip install --no-deps dfindexeddb==20260210
# 说明：dfindexeddb 锁死 python-snappy==0.6.1，而 0.6.1 没有 Apple Silicon 预编译
# wheel，所以必须分两步、用 --no-deps 装 0.7.3。

# 2) 放入 ffmpeg/ffprobe 静态构建（大文件不入 git，需自行下载）
#    来源：https://www.osxexperts.net （GPL 构建，源码见 FFmpeg 官方仓库）
mkdir -p vendor && cp <下载的 ffmpeg> <下载的 ffprobe> vendor/

# 3) 一键构建 + 安装 + 产出 dmg
./app/install.sh
```

`install.sh` 会依次：冻结 worker（PyInstaller）→ 构建 app → 内嵌 worker 与 ffmpeg →
签名（自动挑钥匙串里的 Apple Development 身份，可用 `CODESIGN_IDENTITY` 覆盖）→
安装到 `~/Applications` → 产出 `ChannelsWatch.dmg`。

> PyInstaller 的构建目录会被脚本放到 `$TMPDIR`：在工作区内直接重写可执行文件会被
> macOS 安全策略拒绝（lipo 失败）。从源码运行时若未打包内嵌 worker，app 会向上查找
> 仓库里的 `sharewatch.py` 并用 `uv run` 启动，也可用 `CHANNELS_WATCH_WORKER` 指定。

## License

MIT — 见 [LICENSE](LICENSE)。内嵌的 ffmpeg/ffprobe 为 GPL 构建，源码见
[FFmpeg](https://github.com/FFmpeg/FFmpeg)。
