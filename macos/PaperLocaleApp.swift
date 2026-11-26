import SwiftUI
import AppKit
import PDFKit

enum LocalCLIInstallation {
    // 构建脚本把 Python 发布版本写入 Info.plist，安装命令用同一个版本创建独立环境。
    static let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "development"
    static let bundled = Bundle.main.object(forInfoDictionaryKey: "PaperLocaleBundledCLI") as? Bool == true
    // 发行版仅记住用户为当前版本明确选择的 CLI。仅从源码构建的窗口仍可沿用原有设置。
    static let preferenceKey = bundled ? "bundledCLIPath.\(version)" : "cliPath"
    static var path: String {
        if let configured = UserDefaults.standard.string(forKey: preferenceKey) { return configured }
        let home = FileManager.default.homeDirectoryForCurrentUser
        if !bundled { return home.appendingPathComponent(".local/bin/paperlocale").path }
        return home.appendingPathComponent("Library/Application Support/PaperLocale/cli")
            .appendingPathComponent(version)
            .appendingPathComponent("bin/paperlocale").path
    }
    static let installationHelp = bundled
        ? "首次使用请运行下载包里的 Install PaperLocale CLI.command。"
        : "请按源码构建说明安装同版本 CLI 及其排版依赖。"
}

// 原文阅读窗口关闭后，App 继续持有后台任务。等待子进程自然结束后再退出。
final class AppDelegate: NSObject, NSApplicationDelegate {
    static weak var job: TranslationJob?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if Self.job?.running == true {
            let alert = NSAlert()
            alert.messageText = "任务仍在运行"
            alert.informativeText = "请等待本次任务结束后退出。"
            alert.runModal()
            return .terminateCancel
        }
        return .terminateNow
    }
}

// 修复动作来自诊断与已核对的候选状态。源文、按钮和确认后果在同一窗口可见。
private typealias RepairViewState<Value> = SwiftUI.State<Value>

@MainActor struct RepairWorkbench: View {
    @ObservedObject var job: TranslationJob
    let initialReport: RepairReport
    let openSettings: () -> Void
    let openAcceptance: () -> Void
    @RepairViewState private var index = 0
    @RepairViewState private var focus = 0
    @RepairViewState private var showOptions = false
    @RepairViewState private var confirmPartial = false

    private var report: RepairReport { job.repairReport ?? initialReport }
    private var code: String? { report.classification?.code }
    private var item: SourceIssue? { report.items.indices.contains(index) ? report.items[index] : nil }
    private var pageCount: Int { max(1, job.sourceDocument?.pageCount ?? 1) }
    private var canEdit: Bool { action("e") != nil }
    private var activeRevisionPages: [Int] {
        Set(report.items.filter { $0.id != nil && $0.target != nil }.flatMap { $0.pages ?? [] }).sorted()
    }
    private var canReviseBlock: Bool { canEdit && job.hasManualRevision && !activeRevisionPages.isEmpty }
    private var canChangeOutput: Bool { TranslationJob.hasOutputFailure(report) && job.canExportCandidate }
    private var structuralIssue: Bool { code?.hasPrefix("source.") == true }
    private var settingsIssue: Bool {
        guard let code else { return false }
        return ["service.authentication", "service.connection", "environment.missing", "file.permission"].contains(code)
    }
    private var canRestartAnalysis: Bool { (job.candidateDocument == nil && structuralIssue) || code == "candidate.review" }

    private func action(_ key: String) -> RepairAction? { report.actions.first { $0.key == key } }

    var body: some View {
        Group {
            if canReviseBlock && !showOptions && !job.inspectingRepair, let page = activeRevisionPages.first {
                ManualRevisionWorkbench(job: job,
                    request: RevisionRequest(page: page, origin: .diagnostic(report.error_id)),
                    pages: activeRevisionPages, report: report, showRepairOptions: { showOptions = true })
            } else {
                diagnosticDetails
            }
        }
        .onAppear { setLocation(item?.pages?.first ?? item?.regions?.first?.page ?? report.page_context?.first?.page) }
        .onChange(of: index) { _ in setLocation(item?.pages?.first ?? item?.regions?.first?.page) }
    }

    private func setLocation(_ page: Int?) {
        if let page { job.repairPage = min(pageCount, max(1, page)) }
        focus += 1
    }

    private var diagnosticDetails: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                VStack(alignment: .leading, spacing: 3) {
                    Text(report.classification?.title ?? "处理翻译问题").font(.title2.bold())
                    if job.sourceDocument != nil {
                        Text(URL(fileURLWithPath: job.pdf).deletingPathExtension().lastPathComponent)
                            .foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                Spacer()
                if canReviseBlock && showOptions {
                    Button("返回修订译文") { showOptions = false }.disabled(job.isBusy)
                }
                Button("关闭") { job.repairReport = nil }.disabled(job.running && !job.inspectingRepair)
            }
            HSplitView {
                VStack(alignment: .leading, spacing: 7) {
                    HStack {
                        Label("原 PDF", systemImage: "doc").font(.subheadline.weight(.semibold))
                        Spacer()
                        Stepper("第 \(job.repairPage) / \(pageCount) 页", value: $job.repairPage, in: 1...pageCount)
                            .fixedSize()
                    }
                    SourcePDFPreview(path: job.pdf, page: job.repairPage, regions: item?.regions ?? [], focus: focus)
                }.frame(minWidth: 320, idealWidth: 450)
                ScrollView {
                    VStack(alignment: .leading, spacing: 14) {
                        Text(report.message).font(.headline).textSelection(.enabled)
                        Text(report.classification?.guidance ?? report.solution)
                            .foregroundStyle(.secondary).textSelection(.enabled)
                        if report.items.count > 1 {
                            Picker("问题位置", selection: $index) {
                                ForEach(Array(report.items.enumerated()), id: \.offset) { offset, issue in
                                    Text("第 \(offset + 1) 项 · \(issue.pageLabel)").tag(offset)
                                }
                            }
                        }
                        if let item { issueDetails(item) }
                        ForEach(Array((report.page_context ?? []).enumerated()), id: \.offset) { _, context in
                            Button("查看第 \(context.page) 页原文") { setLocation(context.page) }
                        }
                        if !job.repairFeedback.isEmpty {
                            Text(job.repairFeedback).textSelection(.enabled).padding(8)
                                .background(Color.orange.opacity(0.1))
                        }
                    }.padding(12).frame(maxWidth: .infinity, alignment: .leading)
                }.frame(minWidth: 440, idealWidth: 520)
            }
            Divider()
            if job.inspectingRepair {
                ProgressView("正在核对可用的处理方式…").controlSize(.small)
            } else if job.running {
                ProgressView(job.stage).controlSize(.small)
            } else {
                HStack(spacing: 10) {
                    repairActions
                    Spacer()
                }
            }
        }
        .padding(20).frame(minWidth: 830, idealWidth: 1050, minHeight: 525, idealHeight: 675)
        .confirmationDialog("保留原文并继续？", isPresented: $confirmPartial, titleVisibility: .visible) {
            Button("继续部分翻译") {
                if let keep = action("s") { job.start(repair: keep, report: report) }
            }
            Button("取消", role: .cancel) {}
        } message: {
            Text("报错涉及的原文将保留在译文中，其余内容继续翻译。候选会标为「部分翻译」，并重新检查版面。")
        }
    }

    @ViewBuilder private func issueDetails(_ item: SourceIssue) -> some View {
        if let page = item.pages?.first ?? item.regions?.first?.page {
            Button("定位原文第 \(page) 页") { setLocation(page) }.buttonStyle(.link)
        }
        if !item.source.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                Text("涉及的原文").font(.subheadline.weight(.semibold))
                Text(item.source).font(.callout).textSelection(.enabled)
            }
        }
        if let details = item.rule_details, !details.isEmpty {
            ForEach(Array(details.enumerated()), id: \.offset) { _, detail in
                Text(detail.title).font(.subheadline.bold())
                Text(detail.message).font(.callout).textSelection(.enabled)
                Text(detail.guidance).font(.callout).foregroundStyle(.secondary)
            }
        } else {
            ForEach(Array((item.errors ?? []).enumerated()), id: \.offset) { _, reason in
                Text(reason).font(.callout).textSelection(.enabled)
            }
        }
        if canEdit && !canReviseBlock, let id = item.id, let target = item.target {
            if let validation = item.validation_source {
                Text("校验原文（保留固定标记）").font(.subheadline.bold())
                Text(validation).font(.callout).textSelection(.enabled)
            }
            Text("修订译文").font(.subheadline.bold())
            TextEditor(text: Binding(get: { job.repairDrafts[id] ?? target }, set: { job.repairDrafts[id] = $0 }))
                .frame(minHeight: 130).border(Color.secondary.opacity(0.35))
                .disabled(job.isBusy)
        }
    }

    private var legacyEdits: [String: String] {
        var edits: [String: String] = [:]
        for issue in report.items {
            if let id = issue.id, let target = issue.target { edits[id] = job.repairDrafts[id] ?? target }
        }
        return edits
    }

    @ViewBuilder private var repairActions: some View {
        if !job.isConfigured {
            Button("配置命令行组件", action: openSettings).buttonStyle(.borderedProminent)
        } else if canChangeOutput {
            Button("另选位置保存候选…", action: job.exportCandidateAs).buttonStyle(.borderedProminent)
            if job.review?.qaState == .passed {
                Button("记录人工验收…", action: openAcceptance)
            }
        } else if code == "cache.incompatible" && job.candidateDocument == nil {
            Button("跳过旧缓存并继续", action: job.continueWithoutImport).buttonStyle(.borderedProminent)
        } else if code == "candidate.changed" {
            Button("重新读取当前候选", action: job.refreshCandidate).buttonStyle(.borderedProminent)
        } else if code == "candidate.review", job.candidateDocument != nil, job.review?.manifest.status != .accepted {
            Button("重新运行机器检查", action: job.checkCandidate).buttonStyle(.borderedProminent)
        } else if action("s") != nil {
            Button("保留原文并继续…") { confirmPartial = true }.buttonStyle(.borderedProminent)
        } else if code == "layout.font" {
            Button("选择中文字体并继续…") {
                if job.selectFont() { job.retryDiagnostic() }
            }.buttonStyle(.borderedProminent)
        } else if code == "file.permission", job.candidateDocument == nil {
            Button("更换工作区并新建运行…") { job.selectWorkspace(startNewRun: true) }.buttonStyle(.borderedProminent)
        } else if settingsIssue {
            Button("打开翻译设置…", action: openSettings).buttonStyle(.borderedProminent)
            // 设置保存后由用户明确发起重试，避免关闭设置就触发计费请求。
            if code == "service.authentication", action("r") != nil {
                Button("使用当前设置重试", action: job.retryDiagnostic)
            }
        } else if code == "service.response" {
            Button("减小请求并继续", action: job.reduceBatchAndRetry)
                .buttonStyle(.borderedProminent).disabled(job.maxCharacters <= 1000)
        } else if canRestartAnalysis {
            if job.hasTranslationCache {
                Button("重新分析并复用合格译文") { job.reanalyze(importCache: true) }.buttonStyle(.borderedProminent)
            } else {
                Button("重新分析版面") { job.reanalyze(importCache: false) }.buttonStyle(.borderedProminent)
            }
        } else if action("r") != nil {
            Button("重试当前步骤", action: job.retryDiagnostic).buttonStyle(.borderedProminent)
        }
        if canEdit && !canReviseBlock && !legacyEdits.isEmpty {
            Button("检查修订") { job.submitEdits(report: report, edits: legacyEdits, checkOnly: true) }
            Button("采用修订并继续") { job.submitEdits(report: report, edits: legacyEdits, checkOnly: false) }
        }
        moreActions
    }

    private var moreActions: some View {
        Menu("其他方式") {
            if !canChangeOutput, code != "service.authentication", action("r") != nil,
               settingsIssue || canRestartAnalysis || action("s") != nil {
                Button("重试当前步骤", action: job.retryDiagnostic)
            }
            ForEach(report.actions.filter { ["t", "f", "b"].contains($0.key) }) { choice in
                Button(choice.label) { job.start(repair: choice, report: report) }
            }
            if canRestartAnalysis && job.hasTranslationCache && (action("s") != nil || job.candidateDocument != nil) {
                Button("重新分析并复用合格译文") { job.reanalyze(importCache: true) }
            }
            if job.candidateDocument == nil || TranslationJob.hasReviewFailure(report) {
                Button("新建独立运行…") {
                    job.newRun()
                    openSettings()
                }
            }
            if !settingsIssue || code == "file.permission" { Button("翻译设置…", action: openSettings) }
            Button("打开运行目录") { NSWorkspace.shared.open(job.runDirectory) }
        }
        .fixedSize()
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
@main struct PaperLocaleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    // 任务由 app 持有，关闭/重开窗口不能丢失子进程、日志或运行中状态。
    @StateObject private var job = TranslationJob()
    init() {
        if CommandLine.arguments.contains("--version") {
            print("PaperLocale macOS \(LocalCLIInstallation.version)")
            exit(0)
        }
        if CommandLine.arguments.contains("--cli-path") {
            print(LocalCLIInstallation.path)
            exit(0)
        }
    }
    var body: some Scene {
        Window("PaperLocale", id: "reader") { ReaderWindow(job: job) }
            .defaultSize(width: 1260, height: 810)
            .commands {
                CommandGroup(replacing: .newItem) {
                    Button("打开 PDF…", action: job.selectPDF)
                        .keyboardShortcut("o").disabled(job.running)
                    Button("打开已有运行…", action: job.openRun).disabled(job.running)
                }
            }
    }
}
