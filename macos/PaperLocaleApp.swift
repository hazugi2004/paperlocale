import SwiftUI
import AppKit
import UniformTypeIdentifiers
import PDFKit

// 轻量原生前端：唯一翻译实现仍是已安装的 PaperLocale CLI。
// 不内置登录材料、不代理订阅、不自动更换模型；参数以数组传入，路径不会成为 shell 代码。
@MainActor final class TranslationJob: ObservableObject {
    @Published var pdf = ""
    @Published var workspaceRoot = UserDefaults.standard.string(forKey: "workspaceRoot") ?? WorkspacePaths.defaultRoot
    @Published var existingRun = ""
    @Published var fontFile = ""
    @Published var maxCharacters = 30000
    @Published var repairFeedback = ""
    @Published var repairDrafts: [String: String] = [:]
    @Published var repairPage = 1
    var editingErrorID = ""
    private var receivedReport: RepairReport?
    private var importCacheFrom = ""
    @Published var cli = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".local/bin/paperlocale").path
    @Published var provider = "codex-local"
    let codexModels = CodexModelChoice.load()
    @Published var selectedModel = "gpt-6-sol"
    @Published var baseURL = ""
    @Published var apiKey = ""
    @Published var outputPDF = ""
    @Published var model = "gpt-6-sol"
    @Published var effort = "medium"
    @Published var runQA = true
    @Published var running = false
    @Published var log = "选择论文 PDF，确认翻译服务与模型后开始。\n需要本机已安装 PaperLocale 0.8.2 的 layout 依赖；Codex 服务需要已登录的 Codex CLI，API 服务需要对应密钥。"
    @Published var dropTarget = false
    @Published var fraction = 0.0
    @Published var stage = "等待开始"
    @Published var repairReport: RepairReport?
    private var progressPending = ""
    private var process: Process?
    private var pipe: Pipe?
    // 保存尾部不完整的 UTF-8 序列，防止管道恰好在汉字中间分块而显示乱码。
    private var pending = Data()

    var runDirectory: URL {
        existingRun.isEmpty ? WorkspacePaths.runDirectory(pdf: pdf, root: workspaceRoot) : URL(fileURLWithPath: existingRun)
    }

    func selectWorkspace() {
        let panel = NSOpenPanel()
        panel.title = "选择统一工作区根目录"
        panel.canChooseDirectories = true; panel.canChooseFiles = false; panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            workspaceRoot = url.path; existingRun = ""; importCacheFrom = ""
            UserDefaults.standard.set(workspaceRoot, forKey: "workspaceRoot")
        }
    }

    func openRun() {
        let panel = NSOpenPanel()
        panel.title = "打开已有运行目录（包含 run_manifest.json）"
        panel.canChooseDirectories = true; panel.canChooseFiles = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        loadRun(url)
    }

    // 文件选择与测试共用加载入口；程序赋值不触发用户切换服务的重置逻辑。
    func loadRun(_ url: URL) {
        guard !running else { return }
        do {
            let data = try Data(contentsOf: url.appendingPathComponent("run_manifest.json"))
            guard let manifest = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let source = manifest["source_pdf"] as? String else { throw CocoaError(.fileReadCorruptFile) }
            acceptPDF(URL(fileURLWithPath: source))
            guard pdf == URL(fileURLWithPath: source).standardizedFileURL.path else { return }
            existingRun = url.path
            outputPDF = manifest["output_pdf"] as? String ?? ""
            if let recorded = manifest["translation_provider"] as? [String: Any] {
                provider = recorded["provider"] as? String ?? provider
                model = recorded["model"] as? String ?? model
                selectedModel = modelChoices.contains(model) ? model : "自定义"
                effort = recorded["reasoning_effort"] as? String ?? effort
                baseURL = recorded["base_url"] as? String ?? baseURL
            }
            showCurrentError()
        } catch { log = "无法打开运行目录：\(error.localizedDescription)" }
    }

    func showCurrentError() {
        if let data = try? Data(contentsOf: runDirectory.appendingPathComponent("error_report.json")) {
            repairReport = try? JSONDecoder().decode(RepairReport.self, from: data)
        } else { repairReport = receivedReport }
    }

    func reanalyze(importCache: Bool) {
        guard !running else { return }
        let old = runDirectory
        importCacheFrom = importCache ? old.path : ""
        existingRun = old.deletingLastPathComponent().appendingPathComponent(
            old.lastPathComponent + "-" + String(UUID().uuidString.prefix(8))).path
        repairReport = nil
        start()
    }

    func selectFont() {
        let panel = NSOpenPanel(); panel.title = "选择支持中文的字体（TTF / OTF）"
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { fontFile = url.path }
    }

    func submitEdits(report: RepairReport, edits: [String: String], checkOnly: Bool) {
        guard !running else { return }
        do {
            let draft = runDirectory.appendingPathComponent("repair_draft.json")
            try JSONSerialization.data(withJSONObject: edits, options: [.prettyPrinted, .sortedKeys]).write(to: draft, options: .atomic)
            let task = Process(); let output = Pipe()
            task.executableURL = URL(fileURLWithPath: cli)
            task.arguments = ["repair-choice", "--run-dir", runDirectory.path, "--error-id", report.error_id,
                              "--choice", "e", "--edits-file", draft.path] + (checkOnly ? ["--check-only"] : [])
            task.standardOutput = output; task.standardError = output
            // 后台读管道，长段落错误不会因管道写满卡住；结果留在当前修复窗口。
            repairFeedback = "正在校验…"; running = true
            try task.run(); process = task
            DispatchQueue.global(qos: .userInitiated).async { [weak self] in
                let data = output.fileHandleForReading.readDataToEndOfFile()
                task.waitUntilExit()
                let text = String(decoding: data, as: UTF8.self)
                DispatchQueue.main.async {
                    guard let self else { return }
                    self.running = false; self.process = nil
                    if checkOnly, let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let items = result["items"] as? [[String: Any]] {
                        self.repairFeedback = items.flatMap { row -> [String] in
                            let id = String((row["id"] as? String ?? "").prefix(12))
                            return (row["errors"] as? [String] ?? []).map { id + "：" + $0 }
                        }.joined(separator: "\n")
                        if self.repairFeedback.isEmpty { self.repairFeedback = "内容校验通过。保存后仍会检查排版与 PDF 完整性。" }
                    } else if task.terminationStatus == 0 && !checkOnly {
                        self.repairReport = nil; self.start()
                    } else { self.repairFeedback = text }
                }
            }
        } catch { running = false; repairFeedback = "修订未保存：\(error.localizedDescription)" }
    }

    var resultDirectory: URL {
        // 续跑时窗口可以不重复填写保存位置，打开目录仍须跟随断点中的选择。
        var path = outputPDF
        if path.isEmpty, let data = try? Data(contentsOf: runDirectory.appendingPathComponent("run_manifest.json")),
           let manifest = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            path = manifest["output_pdf"] as? String ?? ""
        }
        return URL(fileURLWithPath: path.isEmpty ? pdf : path).deletingLastPathComponent()
    }

    func selectPDF() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        if panel.runModal() == .OK, let url = panel.url {
            acceptPDF(url)
        }
    }

    // 拖放和文件选择走同一入口。异步拖放完成时再核对 running，防止
    // 用户在文件解析期间开始任务、随后被迟到回调换掉源文件。
    func acceptPDF(_ url: URL) {
        guard !running else { return }
        guard url.isFileURL, url.pathExtension.lowercased() == "pdf",
              FileManager.default.isReadableFile(atPath: url.path) else {
            log = "请选择可读取的本地 PDF 文件。"; return
        }
        pdf = url.standardizedFileURL.path
        existingRun = ""; importCacheFrom = ""; receivedReport = nil
        outputPDF = ""
        repairReport = nil
        fraction = 0
        stage = "等待开始"
    }

    func receiveDrop(_ providers: [NSItemProvider]) -> Bool {
        guard !running, providers.count == 1, let provider = providers.first,
              provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) else { return false }
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { [weak self] item, _ in
            let url: URL?
            if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
            else { url = item as? URL }
            if let url { DispatchQueue.main.async { self?.acceptPDF(url) } }
        }
        return true
    }

    func selectOutput() {
        let panel = NSSavePanel()
        panel.title = "保存翻译 PDF"
        panel.allowedContentTypes = [.pdf]
        panel.nameFieldStringValue = URL(fileURLWithPath: pdf).deletingPathExtension()
            .lastPathComponent + "_translated_by_paperlocale.pdf"
        panel.directoryURL = URL(fileURLWithPath: outputPDF.isEmpty ? pdf : outputPDF)
            .deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { outputPDF = url.path }
    }

    var modelChoices: [String] {
        if provider == "codex-local" { return codexModels.map { $0.name } + ["自定义"] }
        if provider == "qwen-mt" { return ["qwen-mt-plus", "qwen-mt-flash", "自定义"] }
        return ["自定义"]
    }

    var effortChoices: [String] {
        codexModels.first { $0.name == selectedModel }?.efforts ?? ["low", "medium", "high", "xhigh", "max"]
    }

    func changeModel() {
        model = selectedModel == "自定义" ? "" : selectedModel
        if !effortChoices.contains(effort) {
            effort = effortChoices.contains("medium") ? "medium" : (effortChoices.first ?? "medium")
        }
    }

    func changeProvider() {
        // 用户明确切换服务后重置模型示例；旧断点的身份仍由 CLI 校验，不能混用缓存。
        selectedModel = provider == "codex-local" && modelChoices.contains("gpt-6-sol") ? "gpt-6-sol" : modelChoices[0]
        changeModel()
        baseURL = provider == "qwen-mt" ? "https://dashscope.aliyuncs.com/compatible-mode/v1" : ""
        apiKey = ""
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
                progressPending += text
                // stdout 分块可截断 JSON 或汉字；只解析完整换行记录。
                while let end = progressPending.firstIndex(of: "\n") {
                    let line = String(progressPending[..<end])
                    progressPending.removeSubrange(...end)
                    if line.hasPrefix("PAPERLOCALE_ERROR "),
                       let data = String(line.dropFirst("PAPERLOCALE_ERROR ".count)).data(using: .utf8) {
                        receivedReport = try? JSONDecoder().decode(RepairReport.self, from: data)
                    }
                    if line.hasPrefix("PAPERLOCALE_PROGRESS "),
                       let data = String(line.dropFirst("PAPERLOCALE_PROGRESS ".count)).data(using: .utf8),
                       let progress = try? JSONDecoder().decode(JobProgress.self, from: data) {
                        fraction = min(1, max(0, progress.fraction))
                        stage = progress.stage + (progress.total > 0 ? " · \(progress.completed)/\(progress.total) 段" : "")
                    }
                }
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

    func start(repair: RepairAction? = nil, report: RepairReport? = nil) {
        guard !running else { return }
        guard FileManager.default.isExecutableFile(atPath: cli) else {
            log = "找不到可执行的 PaperLocale：\(cli)\n请按本地构建说明安装 PaperLocale 0.8.2，或选择已安装的命令。"
            return
        }
        guard FileManager.default.fileExists(atPath: pdf), !model.trimmingCharacters(in: .whitespaces).isEmpty else {
            log = "请选择存在的 PDF，并填写模型。"; return
        }
        if provider != "codex-local" && baseURL.trimmingCharacters(in: .whitespaces).isEmpty {
            log = "请填写所选翻译服务的 API 地址（不含 /chat/completions）。"; return
        }
        guard maxCharacters > 0 else { log = "请求字符上限必须为正整数。"; return }
        let task = Process()
        task.executableURL = URL(fileURLWithPath: cli)
        // 不使用等待循环：错误退出后让用户查看原因，再点击同一按钮恢复原断点。
        // CLI 的身份校验负责拒绝在旧断点上静默改变模型或源文件。
        var translationArguments = TranslationOptions(pdf: pdf, runDirectory: runDirectory.path,
            provider: provider, model: model, effort: effort, baseURL: baseURL,
            outputPDF: outputPDF, runQA: runQA).arguments
        translationArguments += ["--max-characters", String(maxCharacters)]
        if !fontFile.isEmpty { translationArguments += ["--font-file", fontFile] }
        if !importCacheFrom.isEmpty { translationArguments += ["--import-cache-from", importCacheFrom] }
        if let repair, let report {
            task.arguments = ["repair-choice", "--run-dir", runDirectory.path,
                              "--error-id", report.error_id, "--choice", repair.key]
        } else { task.arguments = translationArguments }
        let reportURL = runDirectory.appendingPathComponent("error_report.json")
        let previousReport = (try? Data(contentsOf: reportURL)).flatMap { try? JSONDecoder().decode(RepairReport.self, from: $0) }
        repairReport = nil
        if repair == nil { fraction = 0; stage = "启动并读取断点" }
        progressPending = ""; receivedReport = nil
        var env = ProcessInfo.processInfo.environment
        // Finder 启动的 app 没有终端 PATH；显式补入正常用户安装目录和 Homebrew。
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        env["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
        env["PYTHONUNBUFFERED"] = "1"
        // 密钥只传给本次子进程环境，不进入命令行、日志、配置文件或断点。
        // 留空时沿用启动 app 的环境变量；Finder 通常没有该变量，需在窗口填写。
        if provider != "codex-local" && !apiKey.isEmpty { env["PAPERLOCALE_API_KEY"] = apiKey }
        task.environment = env
        let output = Pipe()
        task.standardOutput = output
        task.standardError = output
        log = "服务：\(provider) · 模型：\(model)" +
            (provider == "codex-local" ? " · 推理强度：\(effort)" : "") +
            "\n运行目录：\(runDirectory.path)\n"
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
                if let repair {
                    if process.terminationStatus == 0 && repair.key != "b" {
                        self.start()
                    } else if process.terminationStatus == 0 {
                        self.stage = "已回退到修复前断点"
                        self.fraction = 0
                    } else {
                        self.stage = "修复操作失败，断点见日志"
                        self.repairReport = report
                    }
                } else if process.terminationStatus != 0 {
                    self.stage = "已暂停：需要处理错误"
                    if let data = try? Data(contentsOf: reportURL),
                       let report = try? JSONDecoder().decode(RepairReport.self, from: data),
                       report.error_id != previousReport?.error_id {
                        self.repairReport = report
                    } else if let streamed = self.receivedReport {
                        self.repairReport = streamed
                    }
                }
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
    @ObservedObject var job: TranslationJob
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                MascotImage(name: "AppIcon").frame(width: 52, height: 52)
                VStack(alignment: .leading) {
                    Text("PaperLocale").font(.title.bold())
                    Text("0.8.2 · macOS 测试版 · 英文学术 PDF → 中文").foregroundStyle(.secondary)
                }
                Spacer()
            }
            HStack {
                Text(job.pdf.isEmpty ? "拖入一个 PDF，或点击选择论文" : job.pdf).lineLimit(2).textSelection(.enabled)
                Spacer()
                Button("选择 PDF…", action: job.selectPDF).disabled(job.running)
            }
            .padding(12)
            .background(job.dropTarget ? Color.orange.opacity(0.16) : Color.secondary.opacity(0.06))
            .cornerRadius(10)
            .onDrop(of: [UTType.fileURL.identifier], isTargeted: $job.dropTarget, perform: job.receiveDrop)
            HStack {
                Text("工作区：" + (job.existingRun.isEmpty ? job.workspaceRoot : job.existingRun)).lineLimit(2).textSelection(.enabled)
                Spacer()
                Button("选择工作区…", action: job.selectWorkspace)
                Button("打开已有运行…", action: job.openRun)
            }.disabled(job.running)
            HStack {
                Picker("大模型", selection: Binding(get: { job.provider }, set: { job.provider = $0; job.changeProvider() })) {
                    Text("GPT（Codex）").tag("codex-local")
                    Text("其他（兼容 API）").tag("openai-compatible")
                    Text("Qwen（翻译模型）").tag("qwen-mt")
                }
                Picker("具体模型", selection: Binding(get: { job.selectedModel }, set: { job.selectedModel = $0; job.changeModel() })) {
                    ForEach(job.modelChoices, id: \.self) { Text($0) }
                }
                if job.selectedModel == "自定义" {
                    TextField("模型名称", text: $job.model).frame(minWidth: 140)
                }
                if job.provider == "codex-local" {
                    Picker("推理强度", selection: $job.effort) {
                        ForEach(job.effortChoices, id: \.self) { Text($0) }
                    }.frame(width: 190)
                }
            }.disabled(job.running)
            if job.provider != "codex-local" {
                HStack {
                    TextField("API 地址（含版本前缀，不含 /chat/completions）", text: $job.baseURL)
                    SecureField("API 密钥（仅本次使用）", text: $job.apiKey)
                }.disabled(job.running)
            }
            HStack {
                Text(job.outputPDF.isEmpty ? "保存位置：源 PDF 所在目录（续跑沿用原选择）" : job.outputPDF)
                    .lineLimit(2).textSelection(.enabled)
                Spacer()
                Button("保存到…", action: job.selectOutput).disabled(job.running || job.pdf.isEmpty)
            }
            HStack {
                Toggle("生成后运行机器 QA", isOn: $job.runQA).disabled(job.running)
                Spacer()
                if job.running { ProgressView().controlSize(.small) }
                Button(job.running ? "翻译中…" : "开始 / 继续") { job.start() }
                    .buttonStyle(.borderedProminent).disabled(job.running || job.pdf.isEmpty)
            }
            RollingProgress(fraction: job.fraction, stage: job.stage)
            DisclosureGroup("本地安装与运行目录") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack {
                        TextField("PaperLocale 路径", text: $job.cli)
                        Button("选择…", action: job.selectCLI)
                    }.disabled(job.running)
                    Text("此应用调用本机 CLI；需要 PaperLocale 0.8.2、layout 依赖、Poppler，以及所选服务的登录或密钥。兼容 API 的模型须支持聊天接口与结构化翻译；模型是否可用由你的账户决定。")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Text(job.fontFile.isEmpty ? "中文字体：自动获取" : job.fontFile).lineLimit(1)
                        Button("选择字体…", action: job.selectFont).disabled(job.running)
                    }
                    HStack {
                        Text("每次模型请求的字符上限")
                        TextField("字符数", value: $job.maxCharacters, format: .number).frame(width: 100)
                        Text("响应截断时可降低；单个长段落仍保持完整。").font(.caption).foregroundStyle(.secondary)
                    }.disabled(job.running)
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
                Button("查看本次诊断", action: job.showCurrentError).disabled(job.running || job.pdf.isEmpty)
                Button("打开结果目录") {
                    NSWorkspace.shared.open(job.resultDirectory)
                }.disabled(job.pdf.isEmpty)
            }
        }.padding(24).frame(minWidth: 740, minHeight: 680)
            .onDrop(of: [UTType.fileURL.identifier], isTargeted: nil, perform: job.receiveDrop)
            .sheet(item: $job.repairReport) { report in
                RepairWorkbench(job: job, report: report)
            }
            .onAppear {
                AppDelegate.job = job
                if !job.modelChoices.contains(job.selectedModel) { job.changeProvider() }
            }
    }
}

// 修复台直接展示源 PDF，不要求用户退出应用查找 jsonl 或修改源代码。
struct SourcePDFPreview: NSViewRepresentable {
    let path: String
    let page: Int
    let regions: [SourceRegion]
    func makeNSView(context: Context) -> PDFView {
        let view = PDFView(); view.autoScales = true; view.displayMode = .singlePageContinuous
        return view
    }
    func updateNSView(_ view: PDFView, context: Context) {
        let url = URL(fileURLWithPath: path)
        if view.document?.documentURL != url { view.document = PDFDocument(url: url) }
        if let document = view.document, let selected = document.page(at: max(0, page - 1)) {
            if view.currentPage != selected { view.go(to: selected) }
            // 后端坐标原点在页左上，PDFKit 在左下。旋转页仅定位页码，避免错误高亮。
            let bounds = selected.bounds(for: .cropBox)
            view.highlightedSelections = selected.rotation == 0 ? regions.filter { $0.page == page && $0.rect.count == 4 }.compactMap { region in
                let r = region.rect
                let box = CGRect(x: bounds.minX + r[0], y: bounds.maxY - r[3], width: r[2] - r[0], height: r[3] - r[1])
                let selection = selected.selection(for: box)
                selection?.color = NSColor.systemOrange.withAlphaComponent(0.35)
                return selection
            } : []
        }
    }
}

struct RepairWorkbench: View {
    @ObservedObject var job: TranslationJob
    let report: RepairReport
    private var canEdit: Bool { report.actions.contains { $0.key == "e" } }
    private var submitted: [String: String] {
        var result: [String: String] = [:]
        for item in report.items {
            if let id = item.id, let target = item.target { result[id] = job.repairDrafts[id] ?? target }
        }
        return result
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("修复与继续").font(.title2.bold())
                if let kind = report.classification { Text(kind.title).font(.headline) }
                Spacer()
                Button("返回设置（保留断点）") { job.repairReport = nil }.disabled(job.running)
            }
            HSplitView {
                VStack(alignment: .leading) {
                    Stepper("源 PDF 第 \(job.repairPage) 页", value: $job.repairPage, in: 1...10000)
                    SourcePDFPreview(path: report.source_pdf ?? job.pdf, page: job.repairPage, regions: report.items.flatMap { $0.regions ?? [] })
                }.frame(minWidth: 350, idealWidth: 420)
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(report.message).font(.headline).textSelection(.enabled)
                        Text(report.classification?.guidance ?? report.solution).foregroundStyle(.secondary)
                        ForEach(Array(report.items.enumerated()), id: \.offset) { _, item in
                            HStack {
                                Text(item.pageLabel).font(.headline)
                                if let first = item.pages?.first { Button("定位原页") { job.repairPage = first } }
                            }
                            if let id = item.id { Text("片段：\(id)").font(.caption).textSelection(.enabled) }
                            if let details = item.rule_details, !details.isEmpty {
                                ForEach(Array(details.enumerated()), id: \.offset) { _, detail in
                                    Text(detail.title + "：" + detail.message).foregroundStyle(.red).textSelection(.enabled)
                                    Text(detail.guidance).font(.callout).foregroundStyle(.secondary)
                                }
                            } else {
                                ForEach(Array((item.errors ?? []).enumerated()), id: \.offset) { _, reason in
                                    Text("违反规则与差异：" + reason).foregroundStyle(.red).textSelection(.enabled)
                                }
                            }
                            Text("原文").font(.subheadline.bold())
                            Text(item.source).textSelection(.enabled)
                            if let source = item.validation_source {
                                Text("校验原文（保留全部 {vN} 固定公式／引用标记）").font(.subheadline.bold())
                                Text(source).textSelection(.enabled)
                            }
                            if let anchors = item.anchors, !anchors.isEmpty {
                                Text("固定标记对应原文（只读）").font(.subheadline.bold())
                                ForEach(anchors.keys.sorted { $0.localizedStandardCompare($1) == .orderedAscending }, id: \.self) { key in
                                    Text(key + " → " + (anchors[key] ?? "")).font(.callout).textSelection(.enabled)
                                }
                            }
                            if let target = item.target {
                                Text(canEdit ? "修订译文（不删减科学信息）" : "本次失败译文").font(.subheadline.bold())
                                if canEdit, let id = item.id {
                                    TextEditor(text: Binding(get: { job.repairDrafts[id] ?? target }, set: { job.repairDrafts[id] = $0 }))
                                        .font(.body).frame(minHeight: 150).border(Color.secondary.opacity(0.4))
                                        .disabled(job.running)
                                } else { Text(target).textSelection(.enabled) }
                            }
                            Divider()
                        }
                        ForEach(Array((report.page_context ?? []).enumerated()), id: \.offset) { _, context in
                            Button("查看第 \(context.page) 页上下文") { job.repairPage = context.page }
                            Text(context.source).textSelection(.enabled)
                        }
                        if !job.repairFeedback.isEmpty { Text(job.repairFeedback).textSelection(.enabled).padding(8).background(Color.orange.opacity(0.1)) }
                    }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                }.frame(minWidth: 500)
            }
            if canEdit {
                HStack {
                    Button("校验内容（不保存）") { job.submitEdits(report: report, edits: submitted, checkOnly: true) }
                    Button("保存修订并继续") { job.submitEdits(report: report, edits: submitted, checkOnly: false) }
                        .buttonStyle(.borderedProminent)
                    Text("保存前再次校验；失败不改缓存。可回退。").font(.caption)
                }.disabled(job.running)
            }
            HStack {
                Button("重新分析版面并复用合格缓存") { job.reanalyze(importCache: true) }
                Button("新建运行，不导入缓存（可能重新计费）") { job.reanalyze(importCache: false) }
            }.disabled(job.running)
            ScrollView(.horizontal) {
                HStack {
                    ForEach(report.actions.filter { $0.key != "e" }) { action in
                        Button(action.label) {
                            if action.key == "q" { job.repairReport = nil }
                            else { job.start(repair: action, report: report) }
                        }
                    }
                }
            }.disabled(job.running)
            Text("重试可能调用模型并计费；保留原文属于部分翻译，仍需检查最终 PDF。外部服务或未知结构问题不能靠忽略校验安全解决。")
                .font(.caption).foregroundStyle(.secondary)
        }.padding(20).frame(minWidth: 1040, idealWidth: 1140, minHeight: 680, idealHeight: 780)
            .onAppear { job.repairPage = report.items.first?.pages?.first ?? report.page_context?.first?.page ?? 1; job.repairFeedback = ""
                if job.editingErrorID != report.error_id { job.repairDrafts = [:]; job.editingErrorID = report.error_id }
            }
    }
}

// 图像是应用资源；减少动态效果开启时仍显示同样的真实进度，但不旋转。
struct MascotImage: View {
    let name: String
    var body: some View {
        if let path = Bundle.main.path(forResource: name, ofType: "png"),
           let image = NSImage(contentsOfFile: path) {
            Image(nsImage: image).resizable().scaledToFit()
        }
    }
}
struct RollingProgress: View {
    let fraction: Double
    let stage: String
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(stage).font(.caption)
                Spacer()
                Text("阶段进度 \(Int(fraction * 100))% · 非耗时预测").font(.caption).foregroundStyle(.secondary)
            }
            GeometryReader { geometry in
                let travel = max(0, geometry.size.width - 42)
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.secondary.opacity(0.15)).frame(height: 8)
                    Capsule().fill(Color.orange).frame(width: 21 + travel * fraction, height: 8)
                    MascotImage(name: "ProgressEgg").frame(width: 42, height: 42)
                        .rotationEffect(.degrees(reduceMotion ? 0 : travel * fraction / 21 * 180 / .pi))
                        .offset(x: travel * fraction)
                }.frame(height: 44)
                    .animation(reduceMotion ? nil : .easeInOut(duration: 0.7), value: fraction)
            }.frame(height: 44)
        }.accessibilityElement(children: .ignore)
            .accessibilityLabel("\(stage)，阶段进度 \(Int(fraction * 100))%")
    }
}

@main struct PaperLocaleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    // 任务由 app 持有，关闭/重开窗口不能丢失子进程、日志或运行中状态。
    @StateObject private var job = TranslationJob()
    init() {
        if CommandLine.arguments.contains("--version") {
            print("PaperLocale macOS 0.8.2")
            exit(0)
        }
    }
    var body: some Scene {
        WindowGroup { ContentView(job: job) }
            .commands { CommandGroup(replacing: .newItem) {} }
    }
}
