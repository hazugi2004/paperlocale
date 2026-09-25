import SwiftUI
import AppKit
import UniformTypeIdentifiers

// 轻量原生前端：唯一翻译实现仍是已安装的 PaperLocale CLI。
// 不内置登录材料、不代理订阅、不自动更换模型；参数以数组传入，路径不会成为 shell 代码。
@MainActor final class TranslationJob: ObservableObject {
    @Published var pdf = ""
    @Published var cli = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/bin/paperlocale").path
    @Published var model = "gpt-6-sol"
    @Published var effort = "medium"
    @Published var runQA = true
    @Published var running = false
    @Published var log = "选择论文 PDF，确认模型和推理强度后开始。\n需要本机已安装 PaperLocale 0.7.7 的 layout 依赖与已登录的 Codex CLI。"
    private var process: Process?
    private var pipe: Pipe?
    // 保存尾部不完整的 UTF-8 序列，防止管道恰好在汉字中间分块而显示乱码。
    private var pending = Data()

    var runDirectory: URL {
        URL(fileURLWithPath: pdf).deletingPathExtension()
            .appendingPathExtension("paperlocale")
    }

    func selectPDF() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url { pdf = url.path }
    }

    func selectCLI() {
        let panel = NSOpenPanel()
        panel.title = "选择 PaperLocale 可执行文件"
        panel.showsHiddenFiles = true
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { cli = url.path }
    }

    func append(_ data: Data, final: Bool = false) {
        pending.append(data)
        // 最多保留 UTF-8 字符的 3 个尾字节；非法输出只在最终刷新时替换。
        for tail in 0...min(3, pending.count) {
            let prefix = pending.prefix(pending.count - tail)
            if let text = String(data: prefix, encoding: .utf8) {
                log += text
                pending.removeFirst(prefix.count)
                break
            }
        }
        if final, !pending.isEmpty {
            log += String(decoding: pending, as: UTF8.self)
            pending.removeAll()
        }
        // 窗口只保留最近日志，完整断点与诊断由 CLI 写入运行目录。
        if log.count > 150_000 { log = String(log.suffix(120_000)) }
    }

    func start() {
        guard !running else { return }
        guard FileManager.default.isExecutableFile(atPath: cli) else {
            log = "找不到可执行的 PaperLocale：\(cli)\n请按发行说明安装 paperlocale[layout]==0.7.7，或选择已安装的命令。"
            return
        }
        guard FileManager.default.fileExists(atPath: pdf), !model.trimmingCharacters(in: .whitespaces).isEmpty else {
            log = "请选择存在的 PDF，并填写模型。"; return
        }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: cli)
        // 不使用等待循环：错误退出后让用户查看原因，再点击同一按钮恢复原断点。
        // CLI 的身份校验负责拒绝在旧断点上静默改变模型或源文件。
        task.arguments = ["run", pdf, "--run-dir", runDirectory.path,
                          "--provider", "codex-local", "--model", model,
                          "--reasoning-effort", effort, "--layout-mode", "paragraph",
                          "--target-language", "zh-CN", "--domain", "atmospheric-science",
                          "--unattended", "--no-wait-on-error"] + (runQA ? [] : ["--no-qa"])
        var env = ProcessInfo.processInfo.environment
        // Finder 启动的 app 没有终端 PATH；显式补入正常用户安装目录和 Homebrew。
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        task.environment = env
        let output = Pipe()
        task.standardOutput = output
        task.standardError = output
        log = "模型：\(model) · 推理强度：\(effort)\n运行目录：\(runDirectory.path)\n"
        pending.removeAll()
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if !data.isEmpty { DispatchQueue.main.async { self?.append(data) } }
        }
        task.terminationHandler = { [weak self] process in
            output.fileHandleForReading.readabilityHandler = nil
            let rest = output.fileHandleForReading.readDataToEndOfFile()
            DispatchQueue.main.async {
                guard let self else { return }
                self.append(rest, final: true)
                self.log += "\n进程退出：\(process.terminationStatus)\n" +
                    (process.terminationStatus == 0 ? "完成状态与 PDF 位置见上方日志。" : "请修正上方错误，再点击开始 / 继续；已有断点保留。")
                self.running = false
                self.process = nil
                self.pipe = nil
            }
        }
        do {
            try task.run()
            process = task; pipe = output; running = true
        } catch {
            output.fileHandleForReading.readabilityHandler = nil
            log += "\n无法启动：\(error.localizedDescription)"
        }
    }
}

// 关闭窗口不会中断工作；运行期间退出 app 必须先明确停止任务。
// 此首版不提供不可靠的整棵子进程取消，避免遗留 Codex 子任务仍在消耗额度。
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var job: TranslationJob?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if Self.job?.running == true {
            let alert = NSAlert()
            alert.messageText = "翻译仍在运行"
            alert.informativeText = "请等待本次任务结束后退出。关闭窗口不会停止翻译。"
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
}

struct ContentView: View {
    @StateObject private var job = TranslationJob()
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Image(systemName: "doc.text").font(.largeTitle).foregroundStyle(.blue)
                VStack(alignment: .leading) {
                    Text("PaperLocale").font(.title.bold())
                    Text("0.7.7 · macOS 测试版 · 英文学术 PDF → 中文").foregroundStyle(.secondary)
                }
                Spacer()
            }
            HStack {
                Text(job.pdf.isEmpty ? "尚未选择论文" : job.pdf).lineLimit(2).textSelection(.enabled)
                Spacer()
                Button("选择 PDF…", action: job.selectPDF).disabled(job.running)
            }
            HStack {
                Text("模型")
                TextField("模型名称", text: $job.model).frame(minWidth: 170)
                Picker("推理强度", selection: $job.effort) {
                    ForEach(["none", "low", "medium", "high", "xhigh", "max"], id: \.self) { Text($0) }
                }.frame(width: 210)
            }.disabled(job.running)
            HStack {
                Toggle("生成后运行机器 QA", isOn: $job.runQA).disabled(job.running)
                Spacer()
                if job.running { ProgressView().controlSize(.small) }
                Button(job.running ? "翻译中…" : "开始 / 继续", action: job.start)
                    .buttonStyle(.borderedProminent).disabled(job.running || job.pdf.isEmpty)
            }
            DisclosureGroup("本地安装与运行目录") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        TextField("PaperLocale 路径", text: $job.cli)
                        Button("选择…", action: job.selectCLI)
                    }.disabled(job.running)
                    Text("此应用调用本机 CLI；需要 PaperLocale 0.7.7、layout 依赖、Poppler，以及已登录的 Codex。模型是否可用由你的账户决定。")
                        .font(.caption).foregroundStyle(.secondary)
                    if !job.pdf.isEmpty {
                        Text(job.runDirectory.path).font(.caption).textSelection(.enabled)
                    }
                }.padding(.top, 8)
            }
            ScrollView {
                Text(job.log).font(.system(.caption, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled).padding(10)
            }.background(Color(nsColor: .textBackgroundColor)).cornerRadius(8)
            HStack {
                Text("错误会保留断点；机器 QA 完成不代表人工验收。")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("打开结果目录") {
                    NSWorkspace.shared.open(URL(fileURLWithPath: job.pdf).deletingLastPathComponent())
                }.disabled(job.pdf.isEmpty)
            }
        }.padding(24).frame(minWidth: 740, minHeight: 560)
            .onAppear { AppDelegate.job = job }
    }
}

@main struct PaperLocaleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    init() {
        if CommandLine.arguments.contains("--version") {
            print("PaperLocale macOS 0.7.7")
            exit(0)
        }
    }
    var body: some Scene {
        WindowGroup { ContentView() }
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}
