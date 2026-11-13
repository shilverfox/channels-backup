# 微信视频号下载（macOS / WeChat 4.x）

把微信视频号里某个视频存到本地。两条路子都已在本机验证过链路，按侵入程度排序：

| 方案 | 权限要求 | 原理 | 可靠性 |
|---|---|---|---|
| A. `watch` 落盘抓取 | 无 | 播放时微信把媒体写进容器目录，监视新文件并导出 | 取决于微信是否落盘；零风险，先试 |
| B. `proxy` 抓包 | 管理员密码（装根证书 + 改系统代理） | 本地 mitmproxy 截 `finder.video.qq.com` 的媒体响应，边下边写盘 | 通用；若微信校验证书会失败 |
| C. 录屏 | — | — | 不可行，见下 |

抓包窗口内（约 30 秒）系统代理指向 mitmproxy，mitmproxy 默认**直连出网**：国内站点本来就不该绕 VPN，
抓完 `disable.sh` 会按备份还原成原来的 `127.0.0.1:6514`。想连 VPN 一起保留就用 `--upstream auto`。

## 本机实测结论（WeChat 4.1.5 / macOS 26.6.2 / arm64）

- 微信主窗口 `kCGWindowSharingState = 0`（`NSWindowSharingNone`），**截图和录屏都拍不到内容**，`screencapture -l` 直接报
  `could not create image from window`。这是 App 侧防截屏，跟系统权限无关。
- 开了「辅助功能」权限也没用：微信主界面是自绘的，AX 树里只有窗口标题和三个红绿灯按钮，
  没有文本、没有 WebArea，`AXDocument` / `AXProxy` 都返回 `-25212`（不支持）。
- 聊天/朋友圈视频是**明文 MP4** 落盘：
  `~/Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/<账号>/msg/video/<YYYY-MM>/<md5>.mp4`，
  同目录的 `<md5>_raw.mp4` 是更大的一份（原始码率）。
- 视频号页面跑在 **XWeb（Chromium）** 的 `multitab_<hash>` profile 里：播放时
  `…/radium/web/profiles/multitab_<hash>/Cache/Cache_Data/` 会写入封面图，响应头带
  `Content-Type: image/jpg`、`X-ServerIp`，URL 是 `finder.video.qq.com/…/stodownload?…&picformat=200`。
  说明媒体走 Chromium 网络栈，**系统代理 + 系统钥匙串信任就能截**。
- 但视频本体**不落盘也不进 HTTP 缓存**：一次完整播放后，容器内 12 分钟内没有任何 >300KB 的新文件，
  web 缓存也没有 >800KB 的新条目。`watch` 方案对纯流式播放无效，必须走 `proxy`。
- 视频号直链形如
  `https://finder.video.qq.com/251/2030x/stodownload?encfilekey=…&token=…&bizid=1023`，
  `token` 有时效，过期后需要重新播放再抓一次。

## 用法

```sh
# 0) 看现有缓存里有什么（按时间倒序）
python3 wcdl.py scan --hours 0

# A) 零权限：先跑起来，再去微信里点开视频播放
python3 wcdl.py watch --out ~/Downloads/wechat-videos --timeout 300

# B) 一条命令抓包（自动还原，见下节）
bash scripts/grab.sh

# B') 手动抓包（两步 + 播放）
python3 wcdl.py proxy                       # 起 8899，默认直连出网（--upstream auto 才串现有代理）
sudo bash scripts/enable.sh                 # 装根证书 + 切系统代理（会先备份原设置）
#   ...回微信播放视频 10 秒...
sudo bash scripts/disable.sh                # 恢复原代理设置
python3 wcdl.py fetch --latest 1            # token 还没过期时补下载
```

### 一条命令的抓包

```sh
bash scripts/grab.sh                 # 默认等 150 秒，抓到就收工
bash scripts/grab.sh --timeout 300   # 多等一会儿
bash scripts/grab.sh --dry-run       # 只看它会做什么
```

它按顺序做：起 mitmproxy → 装根证书 → 切系统代理 → 轮询 `~/Downloads/wechat-videos` →
抓到 `finder-*.mp4` 就停。**还原写在 `trap` 里**，正常结束、报错、Ctrl-C 都会把代理恢复成原先的设置。
输出里直接给 ffprobe 的时长和分辨率，确认下对了没有。

`watch` 会把新落盘的媒体复制成 `wechat-<时间>-<哈希>.mp4`，自动优先取 `_raw.mp4`。
`proxy` 的产物在 `--out` 下：`finder-*.mp4`（边下边写）+ `captures.jsonl` + `urls.jsonl`（含请求头，可复现）。

## 参数

- `--min-size` 最小文件 KB，默认 200，防止把封面图当视频。
- `--web-cache` `scan`/`watch` 时连 XWeb 网页缓存一起扫（更慢，一般不需要）。
- `watch --interval` 轮询间隔秒；`--stable-rounds` 连续多少次大小不变算写完。
- `proxy --hosts` 逗号分隔的域名后缀，默认 `finder.video.qq.com,video.qq.com,qpic.cn`。

## 已知限制

- `watch` 依赖微信真的把视频写到磁盘。纯流式播放（解码在内存/管道）时抓不到，工具会打印
  `? 正在播放（未落盘完成）` 提示，这种就走 `proxy`。
- `proxy` 需要微信信任系统根证书。若它内置证书固定（pinning），握手会失败，日志里会看到 TLS 错误，
  那就只能回到 `watch` 或放弃自动化。
- 微信升级后目录结构可能变；改 `ROOT_GLOBS` 即可。

## 合规

只用于自己有权保存的内容（自己的作品、已授权素材、个人离线备份）。下载后再传播他人视频涉及著作权，
平台条款也另有约束，后果自负。
