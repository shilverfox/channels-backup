import SwiftUI
import AppKit
import UserNotifications
import ServiceManagement

/// worker 输出的一行事件。
struct Event: Decodable {
    let type: String
    let id: String?
    let title: String?
    let nickname: String?
    let path: String?
    let bytes: Int?
    let validation: String?
    let error: String?
    let percent: Double?
    let done: Double?
    let total: Double?
    let age: Int?
}

/// 一条下载记录，用于列表展示。
struct RecentItem: Identifiable {
    let id: String
    let title: String
    let size: Int
    let date: Date
    let path: String
}

@MainActor
final class WatchModel: ObservableObject {
    @Published var running = false
    @Published var statusLine = "初始化…"
    @Published var currentTitle: String?
    @Published var progress: Double = 0
    @Published var progressText = ""
    @Published var recent: [RecentItem] = []
    @Published var lastActivity: Date?
    @Published var launchAtLogin = SMAppService.mainApp.status == .enabled
    @Published var triggerFav = UserDefaults.standard.object(forKey: "triggerFav") as? Bool ?? true
    @Published var triggerForward = UserDefaults.standard.object(forKey: "triggerForward") as? Bool ?? true

    private var process: Process?
    private var buffer = Data()
    private var intentionalStop = false
    private var restartAttempts = 0
    private var startedAt: Date?
    private var restartTask: Task<Void, Never>?
    private var heartbeatTimer: Timer?

    private var heartbeatURL: URL {
        URL(fileURLWithPath: "\(NSHomeDirectory())/.channels-watch/worker.heartbeat")
    }

    /// worker 定位：优先用 app 内嵌的 PyInstaller 冻结二进制（分发版，零外部
    /// 依赖）；找不到再回退 uv 脚本（源码开发机）。
    private var workerPath: String {
        if let explicit = ProcessInfo.processInfo.environment["CHANNELS_WATCH_WORKER"] {
            return explicit
        }
        if let resource = Bundle.main.resourceURL {
            let frozen = resource.appendingPathComponent("bin/wcdl-worker").path
            if FileManager.default.isExecutableFile(atPath: frozen) { return frozen }
        }
        // 开发模式（从源码目录直接运行、未打包内嵌 worker）：从 app 位置向上找脚本。
        var dir = Bundle.main.bundleURL.deletingLastPathComponent()
        for _ in 0 ..< 6 {
            let candidate = dir.appendingPathComponent("sharewatch.py").path
            if FileManager.default.fileExists(atPath: candidate) { return candidate }
            dir = dir.deletingLastPathComponent()
        }
        return "sharewatch.py"
    }
    private var isFrozenWorker: Bool {
        !workerPath.hasSuffix(".py")
    }
    /// 冻结二进制直接启动；.py 脚本才需要 uv 包装。
    private var launchCommand: (path: String, prefix: [String]) {
        if isFrozenWorker { return (workerPath, []) }
        let uv = uvLaunch
        return (uv.path, uv.prefix + ["run", workerPath])
    }
    /// uv 的位置不能写死用户名：先看环境变量，再按常见安装路径探测。
    private var uvLaunch: (path: String, prefix: [String]) {
        if let explicit = ProcessInfo.processInfo.environment["CHANNELS_WATCH_UV"] {
            return (explicit, [])
        }
        let candidates = [
            "\(NSHomeDirectory())/.local/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            return (path, [])
        }
        return ("/usr/bin/env", ["uv"])
    }
    private var statePath: String {
        ProcessInfo.processInfo.environment["CHANNELS_WATCH_STATE"]
            ?? "\(NSHomeDirectory())/.channels-watch/state.json"
    }
    var outputDir: URL {
        let raw = ProcessInfo.processInfo.environment["CHANNELS_WATCH_OUT"]
            ?? "\(NSHomeDirectory())/Movies/wechat-channels"
        return URL(fileURLWithPath: raw)
    }

    /// 状态行的触发源文案，跟着 chip 开关走，避免 UI 选项与文案各说各话。
    var triggersText: String {
        var parts: [String] = []
        if triggerFav { parts.append("收藏/点赞") }
        if triggerForward { parts.append("转发") }
        return parts.isEmpty ? "未启用任何触发源" : parts.joined(separator: "/") + "即下载"
    }

    var todayCount: Int {
        recent.filter { Calendar.current.isDateInToday($0.date) }.count
    }
    var weekBytes: Int {
        let cutoff = Date().addingTimeInterval(-7 * 86400)
        return recent.filter { $0.date > cutoff }.reduce(0) { $0 + $1.size }
    }
    /// 自适应单位：一周只下几十 MB 时用 GB 显示会永远是 0.0。
    var weekSizeText: String {
        let bytes = weekBytes
        if bytes >= 1_000_000_000 { return String(format: "%.2f GB", Double(bytes) / 1e9) }
        if bytes >= 1_000_000 { return String(format: "%.0f MB", Double(bytes) / 1e6) }
        if bytes >= 1000 { return String(format: "%.0f KB", Double(bytes) / 1000) }
        return "0 MB"
    }
    var lastActivityText: String {
        guard let last = lastActivity else { return "最近无活动" }
        let seconds = Int(Date().timeIntervalSince(last))
        if seconds < 60 { return "最近活动 刚刚" }
        if seconds < 3600 { return "最近活动 \(seconds / 60) 分钟前" }
        return "最近活动 \(seconds / 3600) 小时前"
    }

    init() {
        loadHistory()
        // 授权只请求一次；放在每次下载里请求是重复动作。
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        // 退出（Cmd+Q / 注销）时把 worker 和它的 python 子进程一起收掉，避免留孤儿。
        NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.stop() }
        }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 600_000_000)
            start()
        }
    }

    private func loadHistory() {
        guard let data = FileManager.default.contents(atPath: statePath),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let seen = root["seen"] as? [String: Any] else { return }
        var items: [RecentItem] = []
        for (id, raw) in seen {
            guard let entry = raw as? [String: Any],
                  let path = entry["path"] as? String,
                  let at = entry["at"] as? Double else { continue }
            let bytes = (entry["bytes"] as? NSNumber)?.intValue ?? 0
            let name = (path as NSString).lastPathComponent
            items.append(RecentItem(id: id, title: name, size: bytes,
                                    date: Date(timeIntervalSince1970: at), path: path))
        }
        recent = items.sorted { $0.date > $1.date }
        lastActivity = recent.first?.date
    }

    func start() {
        guard process == nil else { return }
        intentionalStop = false
        let launch = launchCommand
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launch.path)
        var args = launch.prefix + ["--events", "--out", outputDir.path,
                                    "--state", statePath, "--watch-parent"]
        if let fmt = ProcessInfo.processInfo.environment["CHANNELS_WATCH_FORMAT"] {
            args += ["--format", fmt]
        }
        if let age = ProcessInfo.processInfo.environment["CHANNELS_WATCH_MAX_AGE"] {
            args += ["--max-age", age]
        }
        var triggers: [String] = []
        if triggerFav { triggers.append("fav") }
        if triggerForward { triggers.append("forward") }
        args += ["--triggers", triggers.isEmpty ? "none" : triggers.joined(separator: ",")]
        p.arguments = args
        p.environment = ProcessInfo.processInfo.environment
        let outPipe = Pipe()
        p.standardOutput = outPipe
        let logURL = URL(fileURLWithPath: "\(NSHomeDirectory())/Library/Logs/ChannelsWatch.log")
        // createFile 在文件已存在时会清空内容，别把每次重启的日志都抹掉。
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        if let handle = try? FileHandle(forWritingTo: logURL) {
            handle.seekToEndOfFile()
            p.standardError = handle
        } else {
            p.standardError = FileHandle.nullDevice
        }
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            if chunk.isEmpty {
                // EOF 时不清掉 handler 会被反复唤醒，白烧 CPU。
                handle.readabilityHandler = nil
                return
            }
            Task { @MainActor in self?.ingest(chunk) }
        }
        p.terminationHandler = { [weak self] proc in
            Task { @MainActor in self?.workerExited(status: proc.terminationStatus) }
        }
        do {
            try p.run()
            process = p
            running = true
            startedAt = Date()
            startHeartbeat()
            statusLine = "监视中 · \(triggersText)"
        } catch {
            statusLine = "启动失败：\(error.localizedDescription)"
            scheduleRestart()
        }
    }

    /// 心跳里写自己的 pid：worker 靠它判断宿主是否还活着（不能只靠信号，
    /// app 被强杀时 uv 和 python 不会被连带回收）。
    private func startHeartbeat() {
        stopHeartbeat()
        writeHeartbeat()
        let timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.writeHeartbeat() }
        }
        heartbeatTimer = timer
    }

    private func writeHeartbeat() {
        try? FileManager.default.createDirectory(
            at: heartbeatURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? Data("\(ProcessInfo.processInfo.processIdentifier)".utf8).write(to: heartbeatURL)
    }

    private func stopHeartbeat() {
        heartbeatTimer?.invalidate()
        heartbeatTimer = nil
        // 故意不删心跳文件：app 退出时 worker 要靠它读到「宿主 pid 已消失」才肯自杀。
    }

    private func workerExited(status: Int32) {
        process = nil
        running = false
        // 稳定运行超过一分钟才算「重启成功」，否则计数继续累加。
        if let s = startedAt, Date().timeIntervalSince(s) > 60 { restartAttempts = 0 }
        startedAt = nil
        if intentionalStop {
            statusLine = "已暂停"
            return
        }
        // 75 = EX_TEMPFAIL：账本锁被另一个实例持有。这不是崩溃，重启只会
        // 造成风暴；显示状态等用户手动接管（或另一实例退出后手动点开始）。
        if status == 75 {
            statusLine = "另一实例正在监视，本实例待命"
            return
        }
        statusLine = "worker 退出（\(status)），准备重启"
        scheduleRestart()
    }

    /// 守护进程崩了要能自己回来；连续失败则指数退避。退避用尽后不再永久熔断，
    /// 改为每 2 分钟慢速重试——否则一次环境抖动（如换包后的锁冲突）会让
    /// 监视永久停摆，而 UI 又看起来像在正常运行。
    private func scheduleRestart() {
        restartAttempts += 1
        guard restartAttempts <= 5 else {
            statusLine = "连续异常退出，每 2 分钟自动重试"
            slowRetry()
            return
        }
        let delay = min(pow(2.0, Double(restartAttempts - 1)), 30.0)
        restartTask?.cancel()
        restartTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled, !intentionalStop else { return }
            start()
        }
    }

    private func slowRetry() {
        restartTask?.cancel()
        restartTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 120_000_000_000)
            guard !Task.isCancelled, !intentionalStop else { return }
            start()
        }
    }

    func stop() {
        intentionalStop = true
        restartTask?.cancel()
        restartTask = nil
        stopHeartbeat()
        if let p = process {
            killChildProcesses(of: p.processIdentifier)
            p.terminate()
        }
        process = nil
        running = false
        statusLine = "已暂停"
    }

    /// `uv run` 会 fork 出 python，只给 uv 发信号会留下孤儿 worker 继续写账本。
    private func killChildProcesses(of pid: pid_t) {
        let killer = Process()
        killer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killer.arguments = ["-TERM", "-P", "\(pid)"]
        killer.standardOutput = FileHandle.nullDevice
        killer.standardError = FileHandle.nullDevice
        do {
            try killer.run()
            killer.waitUntilExit()
        } catch {
            // pkill 不可用时退回只终止 uv
        }
    }

    func toggle() {
        if running {
            stop()
        } else {
            restartAttempts = 0   // 用户手动重开，重新给满次数
            start()
        }
    }

    /// 注册/取消登录项；失败时把原因写到状态行。
    /// 切换触发源：持久化后优先走 HTTP 热更新（零空窗），
    /// worker 不可达时回退为重启 worker。
    func setTrigger(fav: Bool, forward: Bool) {
        triggerFav = fav
        triggerForward = forward
        UserDefaults.standard.set(fav, forKey: "triggerFav")
        UserDefaults.standard.set(forward, forKey: "triggerForward")
        applyTriggers()
    }

    /// 向常驻 worker 推送新的触发源集合；只有 HTTP 不可达（worker 刚死、
    /// 未启动等）才回退到重启。热更新让切换在毫秒级生效，不产生监视空窗。
    private func applyTriggers() {
        guard running, process != nil else { return }
        statusLine = "监视中 · \(triggersText)"
        var triggers: [String] = []
        if triggerFav { triggers.append("fav") }
        if triggerForward { triggers.append("forward") }
        var request = URLRequest(url: URL(string: "http://127.0.0.1:47821/triggers")!)
        request.httpMethod = "POST"
        request.timeoutInterval = 3
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["triggers": triggers])
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            let ok = error == nil && (response as? HTTPURLResponse)?.statusCode == 200
            guard !ok else { return }
            Task { @MainActor [weak self] in
                // 回退重启前再核对状态，避免与用户此刻的暂停操作打架
                guard let self, self.running, self.process != nil else { return }
                self.stop()
                self.start()
            }
        }.resume()
    }

    func toggleLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
        } catch {
            statusLine = "登录项设置失败：\(error.localizedDescription)"
        }
        launchAtLogin = SMAppService.mainApp.status == .enabled
    }

    private func ingest(_ chunk: Data) {
        buffer.append(chunk)
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[..<nl]
            buffer = buffer[(nl + 1)...]
            guard !line.isEmpty,
                  let event = try? JSONDecoder().decode(Event.self, from: Data(line)) else { continue }
            apply(event)
        }
    }

    private func apply(_ event: Event) {
        switch event.type {
        case "detected":
            statusLine = "发现新作品，准备下载"
            currentTitle = event.title
            progress = 0
            progressText = ""
        case "downloading":
            statusLine = "下载中"
            currentTitle = event.title
            progress = 0
        case "progress":
            if let pct = event.percent { progress = min(max(pct / 100, 0), 1) }
            if let done = event.done, let total = event.total {
                progressText = String(format: "%.1f / %.1f MB", done, total)
            }
            statusLine = "下载中"
        case "done":
            let size = event.bytes ?? 0
            let item = RecentItem(id: event.id ?? UUID().uuidString,
                                  title: event.title ?? "未命名", size: size,
                                  date: Date(), path: event.path ?? "")
            recent.removeAll { $0.id == item.id }
            recent.insert(item, at: 0)
            // 上限放宽到 500：统计口径要覆盖够长的窗口，卡 20 条会让「今日/近 7 天」突然回退。
            if recent.count > 500 { recent = Array(recent.prefix(500)) }
            lastActivity = Date()
            statusLine = String(format: "已完成 %.1f MB", Double(size) / 1e6)
            progress = 1
            currentTitle = nil
            notify(title: "视频号已下载",
                   body: "\(event.nickname ?? "") · \(String(format: "%.1f MB", Double(size) / 1e6))")
        case "failed":
            statusLine = "失败：\(event.error?.prefix(24) ?? "")"
            notify(title: "视频号下载失败", body: event.error ?? "")
        default:
            break
        }
    }

    private func notify(title: String, body: String) {
        // 授权已在 init 里请求过；这里直接投递，避免每次下载都重走一遍授权回调。
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }
}

struct PanelView: View {
    @ObservedObject var model: WatchModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            statusCard
            triggerRow
            statsRow
            if !model.recent.isEmpty { recentList }
            Divider().opacity(0.25)
            footer
        }
        .padding(16)
        .frame(width: 360)
        .background(
            LinearGradient(colors: [Color(red: 0.09, green: 0.10, blue: 0.13),
                                    Color(red: 0.06, green: 0.07, blue: 0.09)],
                           startPoint: .top, endPoint: .bottom)
        )
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Label("视频号自动下载", systemImage: "arrow.down.circle.fill")
                .font(.system(size: 14, weight: .semibold))
            Spacer()
            Text(model.lastActivityText)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
        }
    }

    private var statusCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Circle()
                    .fill(model.running ? Color.green : Color.orange)
                    .frame(width: 6, height: 6)
                Text(model.statusLine)
                    .font(.system(size: 12, weight: .medium))
                Spacer()
            }
            if let title = model.currentTitle {
                Text(title)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                ProgressView(value: model.progress)
                    .progressViewStyle(.linear)
                    .tint(Color.accentColor)
                HStack {
                    Spacer()
                    Text(model.progressText)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(12)
        .background(Color.white.opacity(0.05))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var triggerRow: some View {
        HStack(spacing: 6) {
            Text("触发")
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            chip("收藏/点赞", on: model.triggerFav) {
                model.setTrigger(fav: !model.triggerFav, forward: model.triggerForward)
            }
            chip("转发", on: model.triggerForward) {
                model.setTrigger(fav: model.triggerFav, forward: !model.triggerForward)
            }
            Spacer()
        }
    }

    private func chip(_ title: String, on: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            HStack(spacing: 4) {
                Image(systemName: on ? "checkmark.circle.fill" : "circle")
                    .font(.system(size: 9))
                Text(title).font(.system(size: 10))
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(Color.white.opacity(on ? 0.12 : 0.04))
            .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private var statsRow: some View {
        HStack(spacing: 10) {
            stat(title: "今日", value: "\(model.todayCount) 条")
            stat(title: "近 7 天", value: model.weekSizeText)
            stat(title: "累计", value: "\(model.recent.count) 条")
        }
    }

    private func stat(title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.system(size: 10)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 13, weight: .semibold, design: .rounded))
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(10)
        .background(Color.white.opacity(0.04))
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    private var recentList: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("最近下载").font(.system(size: 10)).foregroundStyle(.secondary)
            ForEach(model.recent.prefix(3)) { item in
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: item.path)])
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "film")
                            .font(.system(size: 10))
                            .foregroundStyle(.secondary)
                        Text(item.title)
                            .font(.system(size: 11))
                            .lineLimit(1)
                        Spacer()
                        Text(String(format: "%.1f MB", Double(item.size) / 1e6))
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
    }

    private var footer: some View {
        HStack(spacing: 8) {
            Button("打开目录") { NSWorkspace.shared.open(model.outputDir) }
            Button(model.running ? "暂停" : "开始") { model.toggle() }
            Button(model.launchAtLogin ? "取消登录启动" : "登录启动") { model.toggleLogin() }
            Spacer()
            Button("退出") { NSApp.terminate(nil) }
        }
        .font(.system(size: 11))
        .buttonStyle(.bordered)
    }
}


/// 菜单栏图标：视频框 + 播放三角 + 右下角下载箭头。
func statusIcon(active: Bool) -> NSImage {
    let size = NSSize(width: 18, height: 18)
    let image = NSImage(size: size, flipped: false) { _ in
        let stroke = active ? NSColor.black : NSColor.secondaryLabelColor
        let body = NSRect(x: 1.5, y: 5.0, width: 15.0, height: 11.5)
        let rounded = NSBezierPath(roundedRect: body, xRadius: 2.5, yRadius: 2.5)
        rounded.lineWidth = 1.3
        stroke.setStroke()
        rounded.stroke()

        let play = NSBezierPath()
        play.move(to: NSPoint(x: 7.0, y: 8.0))
        play.line(to: NSPoint(x: 7.0, y: 13.6))
        play.line(to: NSPoint(x: 11.6, y: 10.8))
        play.close()
        stroke.setFill()
        play.fill()

        let arrow = NSBezierPath()
        arrow.move(to: NSPoint(x: 14.2, y: 7.2))
        arrow.line(to: NSPoint(x: 14.2, y: 2.4))
        arrow.lineWidth = 1.4
        stroke.setStroke()
        arrow.stroke()
        let head = NSBezierPath()
        head.move(to: NSPoint(x: 12.2, y: 4.2))
        head.line(to: NSPoint(x: 14.2, y: 1.8))
        head.line(to: NSPoint(x: 16.2, y: 4.2))
        head.lineWidth = 1.4
        head.stroke()
        return true
    }
    image.isTemplate = true
    return image
}

@main
struct ChannelsWatchApp: App {
    @StateObject private var model = WatchModel()

    init() {
        // MenuBarExtra 的弹出面板不继承 preferredColorScheme：系统切到浅色后，
        // 按钮与标题会拿到浅色模式的配色，在深色卡片上等于黑底黑字。
        NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
    }

    var body: some Scene {
        MenuBarExtra {
            PanelView(model: model)
        } label: {
            Image(nsImage: statusIcon(active: model.running))
        }
        .menuBarExtraStyle(.window)
    }
}
