import SwiftUI
import AppKit
import UniformTypeIdentifiers
import PDFKit

enum OpenAIModelState {
    case idle, loading, loaded([String]), failed(String)

    var models: [String] {
        if case .loaded(let models) = self { return models }
        return []
    }

    var isLoading: Bool {
        if case .loading = self { return true }
        return false
    }
}

// Swift 只管理阅读与交互。翻译、修复、质量检查和最终写入仍走同一份 PaperLocale CLI。
@MainActor final class TranslationJob: ObservableObject {
    @Published private(set) var pdf = ""
    @Published private(set) var sourceDocument: PDFDocument?
    @Published private(set) var candidateDocument: PDFDocument?
    @Published private(set) var review: RunReview?
    @Published private(set) var runProblem: String?
    @Published private(set) var notice: String?
    @Published private(set) var diagnostic: RepairReport?
    @Published private(set) var loadingPDF = false
    @Published private(set) var checkingRun = false
    @Published var workspaceRoot = UserDefaults.standard.string(forKey: "workspaceRoot") ?? WorkspacePaths.defaultRoot
    @Published private(set) var existingRun = ""
    @Published var fontFile = ""
    @Published var maxCharacters = 30000
    @Published var repairFeedback = ""
    @Published private(set) var inspectingRepair = false
    @Published var repairDrafts: [String: String] = [:]
    @Published var repairPage = 1
    var editingErrorID = ""
    @Published var cli = LocalCLIInstallation.path
    @Published var provider = "codex-local"
    let codexModels = CodexModelChoice.load()
    @Published var selectedModel = "gpt-6-sol"
    @Published var baseURL = ""
    @Published var apiKey = "" {
        didSet { if provider == "openai-api", oldValue != apiKey { clearOpenAIModels() } }
    }
    @Published private(set) var openAIModelState = OpenAIModelState.idle
    @Published var outputPDF = ""
    @Published var model = "gpt-6-sol"
    @Published var effort = "medium"
    @Published var runQA = true
    @Published var running = false
    @Published private(set) var log = ""
    @Published var dropTarget = false
    @Published private(set) var fraction: Double?
    @Published private(set) var stage = ""
    @Published var repairReport: RepairReport?
    @Published var candidateRevision: RevisionRequest?
    @Published private(set) var revisionBlocks: [RevisionBlock] = []
    @Published var revisionFeedback = ""

    private var sourceHash = ""
    private var candidateHash: String?
    private var selection = UUID()
    private var inspection = UUID()
    private var receivedReport: RepairReport?
    private var importCacheFrom = ""
    private var openAIModelTask: Task<Void, Never>?
    private var selectedOpenAIModel = ""
    private var pendingConfiguration: (layout: String, domain: String, referencePolicy: String)?
    // 保留被管道切开的 UTF-8 字符和 JSON 行，后台按读取顺序提交，结束后再加载最终清单。
    private var pending = Data()
    private var progressPending = ""

    var runDirectory: URL {
        existingRun.isEmpty ? WorkspacePaths.runDirectory(pdf: pdf, root: workspaceRoot) : URL(fileURLWithPath: existingRun)
    }

    var isBusy: Bool { running || loadingPDF || checkingRun }
    var hasRecordedProvider: Bool { review?.manifest.translation_provider?.provider != nil }
    var isConfigured: Bool { FileManager.default.isExecutableFile(atPath: cli) }
    var hasTranslationCache: Bool {
        hasManualRevision && review?.manifest.translation_provider != nil &&
        FileManager.default.isReadableFile(atPath: runDirectory.appendingPathComponent("translations.jsonl").path)
    }
    var canExportCandidate: Bool {
        candidateHash != nil && (review?.qaState == .passed || review?.qaState == .accepted)
    }
    var hasManualRevision: Bool {
        ["paragraph", "preserved"].contains(review?.manifest.recordedLayout ?? "")
    }

    init() {
        if !modelChoices.contains(selectedModel) { selectedModel = "自定义" }
    }

    func selectWorkspace(startNewRun: Bool = false) {
        guard !running else { return }
        let panel = NSOpenPanel()
        panel.title = "选择统一工作区根目录"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        if panel.runModal() == .OK, let url = panel.url {
            workspaceRoot = url.path
            UserDefaults.standard.set(workspaceRoot, forKey: "workspaceRoot")
            if startNewRun {
                newRun(in: url)
                start()
                return
            }
            // 已绑定的运行仍可审阅。新根目录影响下一份 PDF 的默认断点位置。
            if !pdf.isEmpty && existingRun.isEmpty {
                clearReview()
                refreshReview(restoreSettings: true, loadDiagnostic: true)
            }
        }
    }

    func openRun() {
        guard !running else { return }
        let panel = NSOpenPanel()
        panel.title = "打开已有运行目录（包含 run_manifest.json）"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        if panel.runModal() == .OK, let url = panel.url { loadRun(url) }
    }

    func loadRun(_ url: URL) {
        guard !running else { return }
        do {
            let root = RunReview.canonical(url)
            let manifest = try RunManifest.read(in: root)
            openPDF(URL(fileURLWithPath: manifest.source_pdf), in: root)
        } catch {
            notice = "无法打开运行目录。请检查其中的 run_manifest.json。"
        }
    }

    func selectPDF() {
        guard !running else { return }
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.pdf]
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { acceptPDF(url) }
    }

    func acceptPDF(_ url: URL) { openPDF(url, in: nil) }

    private func openPDF(_ url: URL, in root: URL?) {
        guard !running else { return }
        guard url.isFileURL, url.pathExtension.lowercased() == "pdf",
              FileManager.default.isReadableFile(atPath: url.path) else {
            notice = "请选择可读取的本地 PDF 文件。"
            return
        }
        let source = RunReview.canonical(url)
        let request = UUID()
        selection = request
        loadingPDF = true
        notice = nil
        Task {
            let snapshot = await Task.detached(priority: .userInitiated) { try? PDFSnapshot(url: source) }.value
            guard selection == request else { return }
            loadingPDF = false
            guard let snapshot, let document = PDFDocument(data: snapshot.data), document.pageCount > 0,
                  !document.isLocked else {
                notice = "无法阅读这个 PDF。文件可能已加密、损坏或没有页面。"
                return
            }
            clearReview()
            pdf = source.path
            sourceHash = snapshot.sha256
            sourceDocument = document
            outputPDF = ""
            existingRun = root?.path ?? ""
            log = ""
            refreshReview(restoreSettings: true, loadDiagnostic: true)
        }
    }

    func receiveDrop(_ providers: [NSItemProvider]) -> Bool {
        guard !running, providers.count == 1, let provider = providers.first,
              provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) else { return false }
        let request = selection
        provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { [weak self] item, _ in
            let url: URL?
            if let data = item as? Data { url = URL(dataRepresentation: data, relativeTo: nil) }
            else { url = item as? URL }
            DispatchQueue.main.async {
                guard let self, self.selection == request, let url else { return }
                self.acceptPDF(url)
            }
        }
        return true
    }

    private func clearReview() {
        inspection = UUID()
        checkingRun = false
        review = nil
        candidateDocument = nil
        candidateHash = nil
        repairReport = nil
        inspectingRepair = false
        candidateRevision = nil
        revisionBlocks = []
        revisionFeedback = ""
        diagnostic = nil
        receivedReport = nil
        runProblem = nil
        importCacheFrom = ""
        pendingConfiguration = nil
        fraction = nil
        stage = ""
    }

    // 快照校验和大 PDF 的磁盘读取在后台完成。打开另一份论文后，旧回调不能更新新窗口。
    func refreshReview(restoreSettings: Bool = false, loadDiagnostic: Bool = false) {
        guard !pdf.isEmpty, !running else { return }
        let root = runDirectory
        let source = URL(fileURLWithPath: pdf)
        let hash = sourceHash
        let cachedHash = candidateHash
        let request = UUID()
        inspection = request
        checkingRun = true
        Task {
            let result = await Task.detached(priority: .userInitiated) {
                Result { try RunReview.read(in: root, source: source, sourceHash: hash, cachedCandidateHash: cachedHash) }
            }.value
            guard inspection == request else { return }
            checkingRun = false
            switch result {
            case .success(let loaded):
                runProblem = loaded?.review.problem
                if let candidate = loaded?.review.candidate {
                    if let bytes = loaded?.candidateBytes {
                        candidateDocument = PDFDocument(data: bytes)
                        candidateHash = loaded?.review.manifest.rendered_sha256
                    }
                    if candidateDocument?.isLocked != false || (candidateDocument?.pageCount ?? 0) == 0 {
                        candidateDocument = nil
                        candidateHash = nil
                        runProblem = "当前候选 PDF 无法阅读。请检查 \(candidate.lastPathComponent)。"
                    }
                } else {
                    candidateDocument = nil
                    candidateHash = nil
                }
                review = loaded?.review
                if let manifest = review?.manifest {
                    // 同一已识别的运行保持绑定，即使用户后来更改默认工作区。
                    existingRun = root.path
                    if restoreSettings { applyRecordedSettings(manifest) }
                }
                if loadDiagnostic {
                    let saved = readDiagnostic(in: root)
                    // 机器通过后仍可能因为目标冲突而没保存。已通过的候选应继续显示
                    // 这个问题，但旧的翻译失败不能重新覆盖本次有效的审校状态。
                    let reviewed = review?.qaState == .accepted || review?.qaState == .passed
                    diagnostic = !reviewed || Self.hasOutputFailure(saved) || Self.hasReviewFailure(saved) ? saved : nil
                }
            case .failure(let error):
                review = nil
                candidateDocument = nil
                candidateHash = nil
                diagnostic = nil
                runProblem = (error as? RunReadError)?.errorDescription ?? "无法读取运行清单。可打开正确的运行目录，或为当前 PDF 新建运行。"
            }
        }
    }

    private func applyRecordedSettings(_ manifest: RunManifest) {
        outputPDF = manifest.output_pdf ?? ""
        guard let recorded = manifest.translation_provider else { return }
        let previousService = (provider, baseURL)
        provider = recorded.provider ?? provider
        model = recorded.model ?? model
        selectedModel = modelChoices.contains(model) ? model : "自定义"
        effort = recorded.reasoning_effort ?? effort
        baseURL = recorded.base_url ?? ""
        if provider == "openai-api" { selectedOpenAIModel = model }
        // 仅在服务或地址变化时清空内存密钥，避免把一个服务的密钥发给另一个地址。
        if previousService != (provider, baseURL) {
            clearOpenAIModels()
            apiKey = ""
        }
    }

    func newRun(importCache: Bool = false, in workspace: URL? = nil) {
        guard !isBusy, !pdf.isEmpty else { return }
        let old = runDirectory
        let previous = review?.manifest
        clearReview()
        let base = workspace.map { WorkspacePaths.runDirectory(pdf: pdf, root: $0.path) } ?? old
        existingRun = base.deletingLastPathComponent().appendingPathComponent(
            base.lastPathComponent + "-" + String(UUID().uuidString.prefix(8))).path
        importCacheFrom = importCache ? old.path : ""
        // 重建使用已有运行的科学术语和参考文献设置；服务仍可在新运行单独选择。
        if let previous {
            pendingConfiguration = (previous.recordedLayout, previous.domain_pack?.id ?? "atmospheric-science",
                                    previous.reference_policy ?? "preserve")
        }
        // 新运行有独立的输出选择，旧运行的导出文件可继续独立追溯。
        outputPDF = ""
        notice = nil
    }

    func reanalyze(importCache: Bool) {
        guard !isBusy else { return }
        newRun(importCache: importCache)
        start()
    }

    @discardableResult func selectFont() -> Bool {
        let panel = NSOpenPanel()
        panel.title = "选择支持中文的字体（TTF / OTF）"
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return false }
        fontFile = url.path
        return true
    }

    @discardableResult func selectOutput(chooseUnoccupiedName: Bool = false) -> Bool {
        let panel = NSSavePanel()
        panel.title = "保存翻译 PDF"
        panel.allowedContentTypes = [.pdf]
        let proposed = URL(fileURLWithPath: outputPDF.isEmpty ? pdf : outputPDF)
        let directory = proposed.deletingLastPathComponent()
        let name = outputPDF.isEmpty
            ? proposed.deletingPathExtension().lastPathComponent + "_translated_by_paperlocale.pdf"
            : proposed.lastPathComponent
        var suggestion = name
        if chooseUnoccupiedName, FileManager.default.fileExists(atPath: directory.appendingPathComponent(name).path) {
            let stem = (name as NSString).deletingPathExtension
            var suffix = 2
            repeat {
                suggestion = "\(stem)-\(suffix).pdf"
                suffix += 1
            } while FileManager.default.fileExists(atPath: directory.appendingPathComponent(suggestion).path)
        }
        panel.nameFieldStringValue = suggestion
        panel.directoryURL = directory
        // 保留 CLI 对已存在目标的保护。选择位置本身不写文件。
        guard panel.runModal() == .OK, let url = panel.url else { return false }
        outputPDF = url.path
        return true
    }

    var modelChoices: [String] {
        if provider == "codex-local" { return codexModels.map(\.name) + ["自定义"] }
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
        clearOpenAIModels()
        selectedModel = provider == "codex-local" && modelChoices.contains("gpt-6-sol") ? "gpt-6-sol" : modelChoices[0]
        changeModel()
        baseURL = provider == "qwen-mt" ? "https://dashscope.aliyuncs.com/compatible-mode/v1" : ""
        apiKey = ""
        if provider == "openai-api" { model = selectedOpenAIModel }
        loadOpenAIModels(automatically: true)
    }

    private func clearOpenAIModels() {
        openAIModelTask?.cancel()
        openAIModelTask = nil
        openAIModelState = .idle
    }

    private var openAIAPIKey: String {
        TranslationOptions.keyEnvironment(ProcessInfo.processInfo.environment,
            provider: "openai-api", key: apiKey, includeKey: true)["OPENAI_API_KEY"] ?? ""
    }

    var hasOpenAIAPIKey: Bool { !openAIAPIKey.isEmpty }

    func loadOpenAIModels(automatically: Bool = false) {
        guard provider == "openai-api", !running, !hasRecordedProvider, !openAIModelState.isLoading else { return }
        let key = openAIAPIKey
        if automatically {
            guard case .idle = openAIModelState, !key.isEmpty else { return }
        }
        openAIModelState = .loading
        // 输入密钥只清空旧列表并取消在途请求。用户完成输入后手动获取，
        // 旧密钥的异步响应不能覆盖新密钥对应的状态或所选模型。
        openAIModelTask = Task { [weak self] in
            let result: OpenAIModelState
            do {
                result = .loaded(try await OpenAIProjectModels.load(apiKey: key))
            } catch let error as OpenAIProjectModels.RequestError {
                result = .failed(error.localizedDescription)
            } catch {
                result = .failed("无法连接 OpenAI 获取模型列表。请检查网络后重试。")
            }
            guard !Task.isCancelled, let self else { return }
            self.openAIModelState = result
            self.openAIModelTask = nil
        }
    }

    func selectOpenAIModel(_ id: String) {
        guard provider == "openai-api", !hasRecordedProvider, openAIModelState.models.contains(id) else { return }
        model = id
        selectedOpenAIModel = id
    }

    func selectCLI() {
        let panel = NSOpenPanel()
        panel.title = "选择 PaperLocale 可执行文件"
        panel.showsHiddenFiles = true
        panel.canChooseDirectories = false
        if panel.runModal() == .OK, let url = panel.url { cli = url.path }
    }

    func saveCLIPath() { UserDefaults.standard.set(cli, forKey: LocalCLIInstallation.preferenceKey) }

    private func readDiagnostic(in root: URL) -> RepairReport? {
        let path = root.appendingPathComponent("error_report.json")
        guard let data = try? Data(contentsOf: path),
              let report = try? JSONDecoder().decode(RepairReport.self, from: data),
              report.source_sha256 == nil || report.source_sha256 == sourceHash else { return nil }
        return report
    }

    static func hasOutputFailure(_ report: RepairReport?) -> Bool {
        guard let code = report?.classification?.code else { return false }
        return code == "file.output_conflict" || code == "file.permission"
    }

    static func hasReviewFailure(_ report: RepairReport?) -> Bool {
        guard let code = report?.classification?.code else { return false }
        return code == "candidate.review" || code == "candidate.changed"
    }

    func showCurrentError() {
        guard !isBusy, let report = diagnostic ?? receivedReport ?? readDiagnostic(in: runDirectory) else { return }
        let root = runDirectory
        repairFeedback = ""
        if editingErrorID != report.error_id {
            repairDrafts = [:]
            editingErrorID = report.error_id
        }
        // 老断点报告可以由当前引擎只读补充已验证的区块和新处理动作。初始报告仍
        // 可阅读，补充失败也无需丢弃它；旧的修复回调不能改到另一个运行上。
        inspectingRepair = isConfigured
        repairReport = report
        guard inspectingRepair else { return }
        launch(["repair-inspect", "--run-dir", root.path, "--error-id", report.error_id],
               title: "核对可用的修复方式", captureOutput: true) { [weak self] status, data in
            guard let self else { return }
            self.inspectingRepair = false
            guard self.runDirectory == root, self.repairReport?.error_id == report.error_id else { return }
            if status == 0, let current = try? JSONDecoder().decode(RepairReport.self, from: data),
               current.error_id == report.error_id {
                self.repairReport = current
                self.diagnostic = current
            }
        }
        if !running { inspectingRepair = false }
    }

    func exportCandidateAs() {
        guard !isBusy, canExportCandidate, let hash = candidateHash,
              selectOutput(chooseUnoccupiedName: true) else { return }
        // 仅导出这份已检查的候选。文件发生变化时由后端拒绝，保存 PDF 不写入人工验收。
        runOperation(["export", "--run-dir", runDirectory.path, "--expected-rendered-sha256", hash,
                      "--output-pdf", outputPDF], title: "正在另存候选译文")
    }

    func continueWithoutImport() {
        guard !isBusy, candidateDocument == nil else { return }
        // 导入发生于模型调用前；保留当前运行的布局分析与同一运行已有译文。
        importCacheFrom = ""
        repairReport = nil
        start()
    }

    func retryDiagnostic() {
        guard !isBusy else { return }
        repairReport = nil
        if candidateDocument == nil { start() }
        else if review?.qaState != .accepted && review?.qaState != .passed { checkCandidate() }
        else { refreshReview(loadDiagnostic: true) }
    }

    func refreshCandidate() {
        guard !isBusy else { return }
        repairReport = nil
        // 清掉缓存身份以重新读取磁盘上的实际候选，保留原文和当前运行选择。
        candidateHash = nil
        diagnostic = nil
        refreshReview()
    }

    func reduceBatchAndRetry() {
        maxCharacters = max(1000, maxCharacters / 2)
        retryDiagnostic()
    }

    func append(_ data: Data, final: Bool = false) {
        pending.append(data)
        // 按完整的行解析，支持中文被拆到相邻块。普通日志仍可在诊断栏查看。
        while let end = pending.firstIndex(of: 0x0A) {
            let line = String(decoding: pending[..<end], as: UTF8.self)
            pending.removeSubrange(...end)
            progressPending += line + "\n"
            if line.hasPrefix("PAPERLOCALE_ERROR ") {
                let json = Data(line.dropFirst("PAPERLOCALE_ERROR ".count).utf8)
                receivedReport = try? JSONDecoder().decode(RepairReport.self, from: json)
            } else if line.hasPrefix("PAPERLOCALE_PROGRESS ") {
                let json = Data(line.dropFirst("PAPERLOCALE_PROGRESS ".count).utf8)
                if let progress = try? JSONDecoder().decode(JobProgress.self, from: json) {
                    fraction = min(1, max(0, progress.fraction))
                    stage = progress.stage + (progress.total > 0 ? " · \(progress.completed)/\(progress.total) 段" : "")
                }
            }
        }
        if final && !pending.isEmpty {
            progressPending += String(decoding: pending, as: UTF8.self)
            pending.removeAll()
        }
        if !progressPending.isEmpty {
            log += progressPending
            progressPending = ""
            if log.count > 150_000 { log = String(log.suffix(120_000)) }
        }
    }

    func start(repair: RepairAction? = nil, report: RepairReport? = nil,
               afterManualRevision: Bool = false, checkRevisedCandidate: Bool = false) {
        guard !isBusy else { return }
        if let repair, let report {
            applyRepair(report: report, key: repair.key)
            return
        }
        guard runProblem == nil, candidateDocument == nil else { return }
        guard !pdf.isEmpty, FileManager.default.isReadableFile(atPath: pdf) else {
            notice = "源 PDF 无法读取。请重新打开源文件。"
            return
        }
        guard !model.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            notice = provider == "openai-api" ? "请在「翻译设置」获取并选择 OpenAI API 模型。"
                : "请在「翻译设置」选择或填写模型。"
            return
        }
        // 新任务在版面分析前检查凭据。已有断点仍走原恢复流程，包括缓存渲染。
        if provider == "openai-api", !hasRecordedProvider, !hasOpenAIAPIKey {
            notice = "请在「翻译设置」输入 OpenAI API 密钥。"
            return
        }
        if provider != "codex-local" && provider != "openai-api"
            && baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            notice = "请填写所选翻译服务的 API 地址。"
            return
        }
        guard maxCharacters > 0 else {
            notice = "请求字符上限必须为正整数。"
            return
        }
        // 旧断点的模式、术语包与参考文献策略来自清单。CLI 继续校验原文、领域和服务身份。
        let recorded = review?.manifest
        var arguments = TranslationOptions(pdf: pdf, runDirectory: runDirectory.path, provider: provider,
            model: model, effort: effort, baseURL: baseURL, outputPDF: outputPDF, runQA: runQA || checkRevisedCandidate,
            layoutMode: recorded?.recordedLayout ?? pendingConfiguration?.layout ?? "paragraph",
            domain: recorded?.domain_pack?.id ?? pendingConfiguration?.domain ?? "atmospheric-science",
            referencePolicy: recorded?.reference_policy ?? pendingConfiguration?.referencePolicy ?? "preserve").arguments
        arguments += ["--max-characters", String(maxCharacters)]
        if !fontFile.isEmpty { arguments += ["--font-file", fontFile] }
        if !importCacheFrom.isEmpty { arguments += ["--import-cache-from", importCacheFrom] }
        // 人工文字先进入本次候选。导出仍由审校结果控制，旧导出在此时不被覆盖。
        if afterManualRevision { arguments.append("--no-export") }
        runOperation(arguments, title: "读取断点与资源", includeKey: true)
    }

    func checkCandidate() {
        guard candidateDocument != nil, let review, review.manifest.status != .accepted else { return }
        runOperation(["qa", "--run-dir", runDirectory.path], title: "正在运行机器检查")
    }

    func recordAcceptance(reviewer: String, expectedHash: String) {
        let name = reviewer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !name.isEmpty, !isBusy, review?.qaState == .passed, candidateDocument != nil,
              candidateHash == expectedHash else {
            notice = "请重新核对当前候选及机器检查，并填写复核者。"
            return
        }
        var arguments = ["accept", "--run-dir", runDirectory.path, "--reviewed-by", name,
                         "--expected-rendered-sha256", expectedHash]
        if !outputPDF.isEmpty { arguments += ["--output-pdf", outputPDF] }
        runOperation(arguments, title: "正在记录人工验收")
    }

    private func runOperation(_ arguments: [String], title: String, includeKey: Bool = false) {
        guard !isBusy else { return }
        let root = runDirectory
        let previousDiagnostic = diagnostic
        let previousID = readDiagnostic(in: root)?.error_id
        diagnostic = nil
        repairReport = nil
        launch(arguments, title: title, includeKey: includeKey) { [weak self] status, _ in
            guard let self else { return }
            if status != 0 {
                if let report = self.readDiagnostic(in: root), report.error_id != previousID {
                    self.diagnostic = report
                } else {
                    self.diagnostic = self.receivedReport
                }
                if self.diagnostic == nil {
                    self.notice = "操作未完成（退出状态 \(status)）。请查看诊断。"
                }
            }
            self.refreshReview()
        }
        if !running { diagnostic = previousDiagnostic }
    }

    func openCandidateRevision(page: Int) {
        guard !isBusy, hasManualRevision, let candidateHash else { return }
        candidateRevision = RevisionRequest(page: page, origin: .candidate(candidateHash))
    }

    func loadRevisionBlocks(_ request: RevisionRequest) {
        guard !isBusy else { return }
        revisionBlocks = []
        revisionFeedback = ""
        guard isConfigured else {
            revisionFeedback = "\(LocalCLIInstallation.installationHelp) 也可在翻译设置中选择同版本 CLI。"
            return
        }
        launch(request.arguments(in: runDirectory), title: "读取可修订区块", captureOutput: true) { [weak self] status, data in
            guard let self else { return }
            if status == 0, let response = try? JSONDecoder().decode(RevisionBlocks.self, from: data) {
                self.revisionBlocks = response.items
            } else {
                self.revisionFeedback = self.revisionMessage(data, fallback: "无法读取本页的译文区块。")
            }
        }
    }

    private struct StoredRevision: Codable {
        let version: [String]
        let before: String
        let target: String
    }

    private var revisionDraftPath: URL { runDirectory.appendingPathComponent("manual_revision_drafts.json") }

    private func storedRevisions() -> [String: StoredRevision] {
        guard let data = try? Data(contentsOf: revisionDraftPath) else { return [:] }
        return (try? JSONDecoder().decode([String: StoredRevision].self, from: data)) ?? [:]
    }

    func revisionDraft(for block: RevisionBlock, request: RevisionRequest) -> String? {
        if let draft = storedRevisions()[block.id], draft.version == request.origin.arguments,
           draft.before.utf8.elementsEqual(block.target.utf8) {
            return draft.target
        }
        // 早期失败工作台以逻辑片段保存草稿。只在当前诊断中恢复供预览，
        // 提交仍使用用户选中的唯一版面区块和后端提供的当前译文。
        guard !request.origin.isCandidate,
              let data = try? Data(contentsOf: runDirectory.appendingPathComponent("repair_draft.json")),
              let old = try? JSONDecoder().decode([String: String].self, from: data) else { return nil }
        return old[block.segment_id]
    }

    func saveRevisionDraft(_ target: String, for block: RevisionBlock, request: RevisionRequest) {
        do {
            var drafts = storedRevisions()
            drafts[block.id] = StoredRevision(version: request.origin.arguments, before: block.target, target: target)
            try JSONEncoder().encode(drafts).write(to: revisionDraftPath, options: .atomic)
        } catch {
            revisionFeedback = "草稿暂时无法保存。\(error.localizedDescription)"
        }
    }

    private func revisionMessage(_ data: Data, fallback: String) -> String {
        let message = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        return message.isEmpty ? (notice ?? fallback) : message
    }

    func submitRevision(_ edit: RevisionEdit, request: RevisionRequest, checkOnly: Bool) {
        guard !isBusy else { return }
        let file = FileManager.default.temporaryDirectory
            .appendingPathComponent("paperlocale-revision-\(UUID())").appendingPathExtension("json")
        do {
            try JSONEncoder().encode(edit).write(to: file, options: .atomic)
        } catch {
            revisionFeedback = "无法提交修订。\(error.localizedDescription)"
            return
        }
        var arguments = request.arguments(in: runDirectory) + ["--edit-file", file.path]
        if checkOnly { arguments.append("--check-only") }
        revisionFeedback = checkOnly ? "正在检查选中的译文…" : "正在采用你的修订…"
        launch(arguments, title: "检查人工修订", captureOutput: true) { [weak self] status, data in
            defer { try? FileManager.default.removeItem(at: file) }
            guard let self else { return }
            let response = try? JSONDecoder().decode(RevisionResult.self, from: data)
            guard status == 0, response?.valid == true else {
                if let errors = response?.errors, !errors.isEmpty {
                    self.revisionFeedback = errors.joined(separator: "\n")
                } else {
                    self.revisionFeedback = self.revisionMessage(data, fallback: "当前修订尚未采用。")
                }
                return
            }
            if checkOnly {
                self.revisionFeedback = "内容检查通过。采用修订后会重新排版，检查译文能否放入原段落框。"
                return
            }
            self.revisionFeedback = ""
            self.candidateRevision = nil
            self.repairReport = nil
            self.diagnostic = nil
            self.candidateDocument = nil
            self.candidateHash = nil
            self.start(afterManualRevision: true, checkRevisedCandidate: request.origin.isCandidate)
        }
        if !running {
            try? FileManager.default.removeItem(at: file)
            revisionFeedback = notice ?? "未能启动修订检查。"
        }
    }

    func submitEdits(report: RepairReport, edits: [String: String], checkOnly: Bool) {
        guard !isBusy else { return }
        do {
            let draft = runDirectory.appendingPathComponent("repair_draft.json")
            let data = try JSONSerialization.data(withJSONObject: edits, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: draft, options: .atomic)
            applyRepair(report: report, key: "e", extra: ["--edits-file", draft.path], checkOnly: checkOnly)
        } catch {
            repairFeedback = "无法保存修订草稿。\(error.localizedDescription)"
        }
    }

    private func applyRepair(report: RepairReport, key: String, extra: [String] = [], checkOnly: Bool = false) {
        let arguments = ["repair-choice", "--run-dir", runDirectory.path, "--error-id", report.error_id, "--choice", key]
            + extra + (checkOnly ? ["--check-only"] : [])
        repairFeedback = checkOnly ? "正在校验内容…" : "正在保存所选修复…"
        launch(arguments, title: "处理修复", captureOutput: true) { [weak self] status, data in
            guard let self else { return }
            if checkOnly {
                if let result = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
                   let items = result["items"] as? [[String: Any]] {
                    let errors = items.flatMap { ($0["errors"] as? [String]) ?? [] }
                    self.repairFeedback = errors.isEmpty && status == 0
                        ? "内容校验通过。保存后仍会检查排版与 PDF 完整性。" : errors.joined(separator: "\n")
                } else { self.repairFeedback = String(decoding: data, as: UTF8.self) }
                return
            }
            if status != 0 {
                self.repairFeedback = self.revisionMessage(data, fallback: "未能应用这项修复。请重新打开错误后检查可用的处理方式。")
                return
            }
            self.repairReport = nil
            self.diagnostic = nil
            if key == "b" {
                self.notice = "已回到修复前的断点。"
                self.refreshReview(loadDiagnostic: true)
            } else {
                // 正在修复的旧候选可能已被后端撤下。保留已选论文和阅读位置，以新运行报告刷新译文。
                self.candidateDocument = nil
                self.candidateHash = nil
                self.start()
            }
        }
    }

    private func launch(_ arguments: [String], title: String, includeKey: Bool = false,
                        captureOutput: Bool = false, completion: @escaping (Int32, Data) -> Void) {
        guard !running else { return }
        guard isConfigured else {
            notice = "找不到 PaperLocale CLI。\(LocalCLIInstallation.installationHelp) 也可在翻译设置中选择同版本 CLI。"
            if repairReport != nil { repairFeedback = notice ?? "" }
            return
        }
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: cli)
        task.arguments = arguments
        task.standardInput = FileHandle.nullDevice
        task.standardOutput = pipe
        task.standardError = pipe
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var environment = TranslationOptions.keyEnvironment(ProcessInfo.processInfo.environment,
            provider: provider, key: apiKey, includeKey: includeKey)
        // Finder 不使用终端的 PATH。可执行文件路径与参数分开传入，API 密钥只放在翻译子进程环境中。
        environment["PATH"] = "\(home)/.local/bin:/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin")
        environment["PYTHONUNBUFFERED"] = "1"
        task.environment = environment
        stage = title
        notice = nil
        log = title + "\n运行目录：\(runDirectory.path)\n"
        receivedReport = nil
        fraction = nil
        pending.removeAll()
        progressPending = ""
        do {
            try task.run()
            running = true
        } catch {
            notice = "无法启动 PaperLocale CLI。\(error.localizedDescription)"
            if repairReport != nil { repairFeedback = notice ?? "" }
            return
        }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var result = Data()
            let reader = pipe.fileHandleForReading
            // 阶段事件通常只有一行。管道有数据时即交给主线程，不积攒到固定字节数或任务退出。
            var chunk = reader.availableData
            while !chunk.isEmpty {
                if captureOutput {
                    result.append(chunk)
                } else {
                    let update = chunk
                    DispatchQueue.main.async { self?.append(update) }
                }
                chunk = reader.availableData
            }
            task.waitUntilExit()
            let output = result
            DispatchQueue.main.async {
                guard let self else { return }
                self.append(Data(), final: true)
                self.log += "\n进程退出：\(task.terminationStatus)\n"
                self.running = false
                completion(task.terminationStatus, output)
            }
        }
    }
}
