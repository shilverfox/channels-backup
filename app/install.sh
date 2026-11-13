#!/usr/bin/env bash
# 一键构建分发包：冻结 worker → 构建 app → 签名 → 安装到 ~/Applications → 产出 dmg。
# 需要在本机运行（不要在沙箱里跑）。依赖：uv 或已建好的 .build-venv、Swift 5.9+。
set -euo pipefail
cd "$(dirname "$0")/.."   # 安装脚本在 app/ 子目录，其余产物都在仓库根

echo "== 1/4 冻结 worker（PyInstaller）=="
if [ ! -x .build-venv/bin/pyinstaller ]; then
  echo "缺少构建 venv，先执行（见 README「从源码构建」）："
  echo "  ~/.local/bin/python3.12 -m venv .build-venv && .build-venv/bin/pip install --no-deps python-snappy==0.7.3"
  echo "  .build-venv/bin/pip install zstd cramjam==2.11.0 pyinstaller && .build-venv/bin/pip install --no-deps dfindexeddb==20260210"
  exit 1
fi
# 构建目录放在 $TMPDIR 而不是仓库内：PyInstaller 的 lipo 需要重写产物，
# 在工作区内重写可执行文件会被 macOS 安全策略拒绝（$TMPDIR 不受此限）。
BUILD_ROOT="${TMPDIR:-/tmp}/wcdl-build-$(date +%s)"
BUILD_OUT="$BUILD_ROOT/dist"
BUILD_WORK="$BUILD_ROOT/work"
.build-venv/bin/pyinstaller --onefile --name wcdl-worker \
  --paths "$(pwd)/wcdl_engine" --add-data "$(pwd)/wcdl_engine:wcdl_engine" \
  --distpath "$BUILD_OUT" --workpath "$BUILD_WORK" --specpath "$BUILD_WORK" \
  --noconfirm "$(pwd)/sharewatch.py"
WORKER_BIN="$BUILD_OUT/wcdl-worker"

echo "== 2/4 构建 app =="
swift build -c release --disable-sandbox --scratch-path .build --package-path app

APP="app/ChannelsWatch.app"   # app bundle 由脚本组装，Info.plist 模板在 app/Info.plist
BIN_DIR="$APP/Contents/Resources/bin"
mkdir -p "$APP/Contents/MacOS" "$BIN_DIR"
cp app/Info.plist "$APP/Contents/Info.plist"
cp .build/release/ChannelsWatch "$APP/Contents/MacOS/ChannelsWatch"
cp "$WORKER_BIN" "$BIN_DIR/wcdl-worker"

# ffmpeg/ffprobe 静态构建放 vendor/（大文件不入 git）；缺了给出提示但不阻断
# ——本机装了 brew ffmpeg 时 worker 仍能从 PATH 找到。
if [ -f vendor/ffmpeg ] && [ -f vendor/ffprobe ]; then
  cp vendor/ffmpeg vendor/ffprobe "$BIN_DIR/"
  echo "已内嵌 vendor/ 的 ffmpeg/ffprobe"
else
  echo "⚠️ vendor/ 缺 ffmpeg/ffprobe（dmg 分发前必须补上；本机开发可忽略，走 brew 的 ffmpeg）"
fi

[ -f "$APP/Contents/Info.plist" ] || { echo "缺少 Info.plist"; exit 1; }

echo "== 3/4 签名 =="
# 优先用稳定的签名身份：ad-hoc 每次重建都会换指纹，TCC 授权会反复失效。
# 不写死具体证书（含个人邮箱），从钥匙串里挑第一个 Apple Development 身份；
# 需要指定时用 CODESIGN_IDENTITY 覆盖。
IDENTITY="${CODESIGN_IDENTITY:-}"
if [ -z "$IDENTITY" ]; then
  IDENTITY="$(security find-identity -v -p codesigning 2>/dev/null \
    | sed -n 's/.*"\(Apple Development: [^"]*\)".*/\1/p' | head -1)"
fi
if [ -n "$IDENTITY" ]; then
  echo "签名身份：$IDENTITY"
  codesign --force --deep --sign "$IDENTITY" "$APP"
else
  echo "警告：找不到稳定签名身份，退回 ad-hoc（TCC 授权可能每次重建都失效）"
  codesign --force --deep --sign - "$APP"
fi

echo "== 4/4 安装 + 产出 dmg =="
APP_NAME="$(basename "$APP")"
DEST="$HOME/Applications/$APP_NAME"
mkdir -p "$HOME/Applications"
rm -rf "$DEST"
cp -R "$APP" "$DEST"
echo "已安装到 $DEST"

DMG_SRC="dmg-src"
rm -rf "$DMG_SRC"
mkdir -p "$DMG_SRC"
cp -R "$APP" "$DMG_SRC/$APP_NAME"
ln -sfh /Applications "$DMG_SRC/Applications"
hdiutil create -volname ChannelsWatch -srcfolder "$DMG_SRC" \
  -ov -format UDZO "ChannelsWatch.dmg" | tail -1
echo "dmg 产物：$(pwd)/ChannelsWatch.dmg"
# 清掉构建中间物，仓库里不留垃圾（dmg 与组装好的 app bundle 保留）
rm -rf "$BUILD_ROOT" "$DMG_SRC"
open "$DEST"
