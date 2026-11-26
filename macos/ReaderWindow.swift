import SwiftUI
import PDFKit
import AppKit
import UniformTypeIdentifiers

// 直接使用 SwiftUI 的属性包装器。新版命令行 SDK 也声明了同名宏，但未附带该宏的插件。
private typealias ViewState<Value> = SwiftUI.State<Value>

@MainActor struct ReaderWindow: View {
    @ObservedObject var job: TranslationJob
    @StateObject private var reader = PDFReader()
    @ViewState private var showSettings = false
    @ViewState private var showReview = false
    @ViewState private var showDiagnostics = false
    @ViewState private var showSearch = false
    @ViewState private var searchText = ""
    @ViewState private var searchSubmitted = false
    @ViewState private var pageText = "1"
    @ViewState private var preferredArrangement = ReadingArrangement.comparison
    @ViewState private var narrowArrangement: ReadingArrangement?
    @ViewState private var acceptance: AcceptanceRequest?
    @ViewState private var afterRepair: RepairDestination?
    @FocusState private var searchFocused: Bool
    @FocusState private var pageFocused: Bool

    private var hasCandidate: Bool { job.candidateDocument != nil }
    private var hasSource: Bool { job.sourceDocument != nil }
    private var diagnosticRegions: [SourceRegion] { job.diagnostic?.items.flatMap { $0.regions ?? [] } ?? [] }
    private var documentTitle: String {
        if !hasSource { return "PaperLocale" }
        return URL(fileURLWithPath: job.pdf).deletingPathExtension().lastPathComponent
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if hasSource { readingControls }
            if let notice = job.runProblem ?? job.notice ?? job.diagnostic?.message { noticeBar(notice) }
            HStack(spacing: 0) {
                if hasSource {
                    GeometryReader { geometry in
                        let wide = geometry.size.width >= 870
                        readerCanvas(arrangement(wide: wide), wide: wide)
                            .onChange(of: wide) { if $0 { narrowArrangement = nil } }
                    }
                } else {
                    startScreen
                }
                if showReview && hasSource {
                    Divider()
                    ReviewPanel(job: job, page: reader.page, jump: { reader.goToPage($0, alignBoth: true) },
                                locateSource: locateSource)
                        .frame(width: 276)
                }
            }
            if showDiagnostics && hasSource { diagnostics }
            Divider()
            footer
        }
        .frame(minWidth: 740, minHeight: 490)
        .background(Color(nsColor: .windowBackgroundColor))
        .onDrop(of: [UTType.fileURL.identifier], isTargeted: $job.dropTarget, perform: job.receiveDrop)
        .overlay {
            if job.dropTarget && !job.running {
                RoundedRectangle(cornerRadius: 13).strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 2, dash: [7, 5]))
                    .padding(5).allowsHitTesting(false).accessibilityHidden(true)
            }
        }
        .sheet(isPresented: $showSettings) { TranslationSettings(job: job) }
        .sheet(item: $job.repairReport, onDismiss: finishRepair) { report in
            RepairWorkbench(job: job, initialReport: report, openSettings: { leaveRepair(for: .settings) },
                            openAcceptance: { leaveRepair(for: .acceptance) })
        }
        .sheet(item: $job.candidateRevision) { request in
            ManualRevisionWorkbench(job: job, request: request, pages: [request.page])
        }
        .sheet(item: $acceptance) { request in AcceptanceSheet(job: job, request: request) }
        .onAppear {
            AppDelegate.job = job
            reader.load(source: job.sourceDocument, candidate: job.candidateDocument)
            reader.setSourceIssues(diagnosticRegions)
            reader.startWatchingInput()
        }
        .onDisappear { reader.stopWatchingInput() }
        .onChange(of: job.sourceDocument) { _ in
            preferredArrangement = .comparison
            narrowArrangement = nil
            reader.load(source: job.sourceDocument, candidate: job.candidateDocument)
            reader.setSourceIssues(diagnosticRegions)
            pageText = String(reader.page)
            searchText = ""
            searchSubmitted = false
            showSearch = false
        }
        .onChange(of: job.candidateDocument) { _ in
            reader.load(source: job.sourceDocument, candidate: job.candidateDocument)
            if !hasCandidate {
                preferredArrangement = .comparison
                narrowArrangement = nil
            }
        }
        .onChange(of: diagnosticRegions) { reader.setSourceIssues($0) }
        .onChange(of: reader.page) { newPage in
            if !pageFocused { pageText = String(newPage) }
        }
        .onChange(of: pageFocused) { focused in
            if !focused { pageText = String(reader.page) }
        }
        .onChange(of: reader.active) { _ in searchSubmitted = false }
        .onChange(of: showSearch) { visible in
            searchFocused = visible
            if !visible {
                reader.cancelSearch()
                searchSubmitted = false
            }
        }
    }

    private func locateSource(_ page: Int, _ regions: [SourceRegion]) {
        // 单栏正在看译文时，定位明确切回原文。并排时保留两栏以及联动选择。
        if preferredArrangement == .translation { preferredArrangement = .original }
        if narrowArrangement == .translation { narrowArrangement = .original }
        reader.locateSourceIssue(page: page, regions: regions)
    }

    private func leaveRepair(for destination: RepairDestination) {
        afterRepair = destination
        job.repairReport = nil
    }

    private func finishRepair() {
        let next = afterRepair
        afterRepair = nil
        switch next {
        case .settings: showSettings = true
        case .acceptance:
            if job.review?.qaState == .passed, let hash = job.review?.manifest.rendered_sha256 {
                acceptance = AcceptanceRequest(hash: hash)
            }
        case nil: break
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            Menu {
                Button("打开 PDF…", action: job.selectPDF)
                Button("打开已有运行…", action: job.openRun)
            } label: {
                Label("打开", systemImage: "folder")
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .disabled(job.running)
            .accessibilityLabel("打开论文或已有运行")

            Divider().frame(height: 23)
            VStack(alignment: .leading, spacing: 2) {
                Text(documentTitle).font(.headline).lineLimit(1).truncationMode(.middle).help(hasSource ? job.pdf : "PaperLocale")
                if hasSource {
                    Text(job.review?.manifest.translation_provider?.model ?? job.model)
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            Button { showSettings = true } label: {
                Label("翻译设置", systemImage: "gearshape")
            }.help("服务、模型、保存位置与本地安装")
            Button { showReview.toggle() } label: {
                Label("审校", systemImage: showReview ? "sidebar.trailing" : "checklist")
            }.disabled(!hasSource).help(showReview ? "隐藏审校面板" : "查看机器检查与保留原文")
            primaryAction
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.bar)
    }

    @ViewBuilder private var primaryAction: some View {
        if job.loadingPDF {
            ProgressView().controlSize(.small).accessibilityLabel("正在打开 PDF")
        } else if job.running {
            Label("任务运行中", systemImage: "hourglass").font(.callout).foregroundStyle(.secondary)
        } else if job.checkingRun && !hasCandidate {
            ProgressView().controlSize(.small).accessibilityLabel("正在核对已有运行")
        } else if hasSource {
            if let report = job.diagnostic, TranslationJob.hasOutputFailure(report), job.canExportCandidate {
                Button("解决保存问题", action: job.showCurrentError).buttonStyle(.borderedProminent)
            } else if TranslationJob.hasReviewFailure(job.diagnostic) {
                Button("解决审校问题", action: job.showCurrentError).buttonStyle(.borderedProminent)
            } else if job.runProblem != nil {
                Button("为原文新建运行") { job.newRun() }.buttonStyle(.borderedProminent)
            } else if hasCandidate, let review = job.review {
                switch review.qaState {
                case .passed:
                    Button("记录人工验收") {
                        if let hash = review.manifest.rendered_sha256 {
                            acceptance = AcceptanceRequest(hash: hash)
                        }
                    }.buttonStyle(.borderedProminent).disabled(job.isBusy)
                case .accepted:
                    if let output = review.exported {
                        Button("显示已验收译文") { NSWorkspace.shared.activateFileViewerSelecting([output]) }
                    }
                case .failed where job.diagnostic != nil:
                    Button("查看失败原因", action: job.showCurrentError).buttonStyle(.borderedProminent)
                case .notRun, .failed, .unavailable:
                    if review.manifest.status != .accepted {
                        Button("运行机器检查", action: job.checkCandidate).buttonStyle(.borderedProminent).disabled(job.isBusy)
                    } else {
                        Button("查看验收记录") { showReview = true }
                    }
                }
            } else if job.diagnostic != nil {
                Button("修复与继续", action: job.showCurrentError).buttonStyle(.borderedProminent)
            } else {
                Button(job.review == nil ? "开始翻译" : "继续翻译") { job.start() }
                    .buttonStyle(.borderedProminent).disabled(job.isBusy)
            }
        }
    }

    private var readingControls: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Button { reader.goToPage(reader.page - 1) } label: { Image(systemName: "chevron.left") }
                    .disabled(reader.page <= 1).accessibilityLabel("上一页").help("上一页")
                TextField("页码", text: $pageText)
                    .textFieldStyle(.roundedBorder).multilineTextAlignment(.center)
                    .monospacedDigit()
                    .frame(width: 43).focused($pageFocused).accessibilityLabel("跳到页码")
                    .onSubmit {
                        if let requested = Int(pageText) { reader.goToPage(requested) }
                        pageText = String(reader.page)
                        pageFocused = false
                    }
                Text("/ \(reader.totalPages)").monospacedDigit().foregroundStyle(.secondary)
                Button { reader.goToPage(reader.page + 1) } label: { Image(systemName: "chevron.right") }
                    .disabled(reader.page >= reader.totalPages).accessibilityLabel("下一页").help("下一页")
                Divider().frame(height: 19)
                Button { reader.zoom(by: 1 / 1.25) } label: { Image(systemName: "minus.magnifyingglass") }
                    .accessibilityLabel("缩小").help("缩小").keyboardShortcut("-")
                Button { reader.fitWidth() } label: {
                    Text("\(reader.zoomPercent)%").monospacedDigit().frame(minWidth: 38)
                }.help("适合页宽").accessibilityLabel("适合页宽").keyboardShortcut("0")
                Button { reader.zoom(by: 1.25) } label: { Image(systemName: "plus.magnifyingglass") }
                    .accessibilityLabel("放大").help("放大").keyboardShortcut("=")
                Spacer(minLength: 6)
                if hasCandidate {
                    Button {
                        reader.linked.toggle()
                    } label: {
                        if reader.canLink {
                            Label(reader.linked ? "联动开启" : "联动关闭", systemImage: "link")
                        } else {
                            Label("无法联动", systemImage: "link")
                        }
                    }
                    .help(reader.canLink ? "联动滚动、页码及缩放" : "原译页数不同，无法联动")
                    .disabled(!reader.canLink)
                }
                Button { showSearch.toggle() } label: {
                    Label("搜索", systemImage: "magnifyingglass")
                }.keyboardShortcut("f").help("搜索当前栏（⌘F）")
            }
            .controlSize(.small)
            .buttonStyle(.borderless)
            .padding(.horizontal, 16)
            .padding(.vertical, 7)
            if showSearch { searchBar }
            Divider()
        }
    }

    private var searchBar: some View {
        HStack(spacing: 9) {
            TextField("搜索\(reader.active.rawValue)", text: $searchText)
                .textFieldStyle(.roundedBorder)
                .focused($searchFocused)
                .accessibilityLabel("搜索\(reader.active.rawValue)")
                .onSubmit { submitSearch() }
            if reader.finding {
                ProgressView().controlSize(.small).accessibilityLabel("正在搜索")
            } else if searchSubmitted {
                Text(reader.searchCount == 0 ? "无匹配" : "\(reader.searchIndex) / \(reader.searchCount)")
                    .monospacedDigit().font(.caption).foregroundStyle(.secondary)
            }
            Button("查找", action: submitSearch).disabled(searchText.trimmingCharacters(in: .whitespaces).isEmpty)
            Button { reader.navigateMatch(-1) } label: { Image(systemName: "chevron.up") }
                .disabled(reader.searchCount == 0).accessibilityLabel("上一处匹配")
            Button { reader.navigateMatch(1) } label: { Image(systemName: "chevron.down") }
                .disabled(reader.searchCount == 0).accessibilityLabel("下一处匹配")
            Button { showSearch = false } label: { Image(systemName: "xmark") }
                .accessibilityLabel("关闭搜索")
        }
        .controlSize(.small).padding(.horizontal, 16).padding(.bottom, 7)
    }

    private func submitSearch() {
        searchSubmitted = true
        reader.search(searchText)
    }

    private func arrangement(wide: Bool) -> ReadingArrangement {
        guard hasCandidate else { return .original }
        if wide { return preferredArrangement }
        if let narrowArrangement { return narrowArrangement }
        if preferredArrangement != .comparison { return preferredArrangement }
        return reader.active == .original ? .original : .translation
    }

    private func readerCanvas(_ arrangement: ReadingArrangement, wide: Bool) -> some View {
        VStack(spacing: 0) {
            if hasCandidate {
                // 窄窗口只改当前显示的栏。窗口再次变宽时恢复用户上次选定的宽屏阅读方式。
                Picker("阅读方式", selection: Binding(get: { arrangement }, set: {
                    if wide { preferredArrangement = $0 }
                    else { narrowArrangement = $0 }
                })) {
                    Text("原文").tag(ReadingArrangement.original)
                    if wide { Text("双栏对照").tag(ReadingArrangement.comparison) }
                    Text("译文").tag(ReadingArrangement.translation)
                }
                .pickerStyle(.segmented).labelsHidden()
                .frame(width: wide ? 245 : 152, height: 32)
                .frame(maxWidth: .infinity)
                Divider()
            }
            HStack(spacing: 0) {
                if arrangement.contains(.original) { pane(.original, comparing: arrangement == .comparison) }
                if arrangement == .comparison { Divider() }
                if arrangement.contains(.translation) { pane(.translation, comparing: arrangement == .comparison) }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .onAppear { reader.present(arrangement) }
        .onChange(of: arrangement) { reader.present($0) }
    }

    private func pane(_ side: ReadingSide, comparing: Bool) -> some View {
        let active = comparing && reader.active == side
        return VStack(spacing: 0) {
            HStack {
                Label(side.rawValue, systemImage: side == .original ? "doc" : "character.book.closed")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(active ? Color.accentColor : Color.secondary)
                Spacer()
            }
            .padding(.horizontal, 12).padding(.vertical, 5)
            .background(active ? Color.accentColor.opacity(0.08) : Color(nsColor: .controlBackgroundColor))
            .accessibilityAddTraits(.isHeader)
            ReaderPDFPane(view: reader.view(side))
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityLabel("\(side.rawValue) PDF")
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var startScreen: some View {
        VStack(spacing: 14) {
            MascotImage(name: "AppIcon").frame(width: 98, height: 98).accessibilityHidden(true)
            Text("打开论文").font(.system(.largeTitle, design: .default).weight(.semibold))
            Text("拖入一份 PDF，或从本机选择文件。").foregroundStyle(.secondary)
            HStack(spacing: 10) {
                Button("打开 PDF…", action: job.selectPDF).buttonStyle(.borderedProminent)
                Button("打开已有运行…", action: job.openRun)
            }.padding(.top, 7)
        }
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .underPageBackgroundColor).opacity(0.31))
    }

    private func noticeBar(_ text: String) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle").foregroundStyle(.orange)
            Text(text).font(.callout).lineLimit(2).textSelection(.enabled).help(text)
            Spacer()
            if job.diagnostic != nil {
                if let report = job.diagnostic,
                   let page = report.items.first?.regions?.first?.page ?? report.items.first?.pages?.first
                       ?? report.page_context?.first?.page {
                    Button("在原文定位") { locateSource(page, diagnosticRegions) }.buttonStyle(.borderless)
                }
                Button("详情", action: job.showCurrentError).buttonStyle(.borderless)
            } else if hasSource {
                Button("诊断") { showDiagnostics = true }.buttonStyle(.borderless)
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 7)
        .background(Color.orange.opacity(0.08))
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack {
                Text("诊断").font(.subheadline.weight(.semibold))
                Spacer()
                Button("打开运行目录") { NSWorkspace.shared.open(job.runDirectory) }
                    .disabled(job.review == nil && job.diagnostic == nil)
                Button { showDiagnostics = false } label: { Image(systemName: "xmark") }
                    .accessibilityLabel("收起诊断")
            }.padding(.horizontal, 14)
            ScrollView {
                Text(job.log.isEmpty ? (job.diagnostic?.message ?? "当前没有任务日志。") : job.log)
                    .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 14)
            }.frame(height: 114)
        }.padding(.bottom, 7)
    }

    private var footer: some View {
        HStack(spacing: 9) {
            if job.running {
                if let step = job.fraction {
                    ProgressView(value: step).frame(width: 92).accessibilityLabel("任务阶段进度")
                } else {
                    ProgressView().controlSize(.small).accessibilityLabel("任务进行中")
                }
                Text(job.stage).lineLimit(1)
            } else {
                Label(statusText, systemImage: statusIcon).foregroundStyle(statusColor).lineLimit(1)
            }
            Spacer()
            if job.diagnostic != nil {
                Button("查看诊断详情", action: job.showCurrentError).buttonStyle(.borderless)
            }
            if let output = job.review?.exported {
                Button {
                    NSWorkspace.shared.activateFileViewerSelecting([output])
                } label: {
                    Label("已保存", systemImage: "arrow.up.right.square")
                }.help(output.path).buttonStyle(.borderless)
            }
            if hasSource {
                Button { showDiagnostics.toggle() } label: {
                    Label("诊断", systemImage: showDiagnostics ? "chevron.down" : "chevron.up")
                }.buttonStyle(.borderless)
            }
        }
        .font(.caption)
        .padding(.horizontal, 16).padding(.vertical, 7)
        .frame(minHeight: 31)
    }

    private var statusText: String {
        if job.loadingPDF { return "正在打开 PDF" }
        if job.checkingRun { return "正在核对运行记录" }
        if job.runProblem != nil { return "运行记录需要处理" }
        if TranslationJob.hasOutputFailure(job.diagnostic) && job.canExportCandidate {
            return "候选可阅读 · 保存位置需要处理"
        }
        if job.diagnostic != nil && !hasCandidate { return "等待处理 · 已保存断点" }
        if hasCandidate, let review = job.review {
            let prefix = review.coverage?.isPartial == true ? "部分翻译 · " : ""
            switch review.qaState {
            case .notRun: return prefix + "候选译文 · 未运行机器检查"
            case .failed: return prefix + "机器检查未通过"
            case .passed: return prefix + "机器检查通过 · 待人工验收"
            case .accepted: return prefix + "已人工验收"
            case .unavailable: return prefix + "机器检查或验收记录不可用"
            }
        }
        if job.review != nil { return "可从原断点继续" }
        return hasSource ? "原文已打开 · 等待开始翻译" : "英语学术 PDF → 中文"
    }

    private var statusIcon: String {
        if job.checkingRun || job.loadingPDF { return "hourglass" }
        if job.runProblem != nil || job.diagnostic != nil || job.review?.qaState == .failed { return "exclamationmark.circle" }
        if job.review?.qaState == .accepted { return "checkmark.seal.fill" }
        if hasCandidate { return "doc.on.doc" }
        return "doc"
    }

    private var statusColor: Color {
        if job.runProblem != nil || job.diagnostic != nil || job.review?.qaState == .failed { return .orange }
        if job.review?.qaState == .accepted { return .green }
        return .secondary
    }
}

private enum RepairDestination { case settings, acceptance }

private struct TranslationSettings: View {
    @ObservedObject var job: TranslationJob
    @Environment(\.dismiss) private var dismiss
    @ViewState private var initialCLI = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("翻译设置").font(.title2.weight(.semibold)).padding(18)
            Divider()
            Form {
                Section("服务与模型") {
                    if job.hasRecordedProvider {
                        LabeledContent("当前断点", value: "\(job.provider) · \(job.model)")
                        if job.provider == "codex-local" { LabeledContent("推理强度", value: job.effort) }
                    } else {
                        Picker("翻译服务", selection: Binding(get: { job.provider }, set: { job.provider = $0; job.changeProvider() })) {
                            Text("GPT（Codex）").tag("codex-local")
                            Text("OpenAI API（独立计费）").tag("openai-api")
                            Text("其他（兼容 API）").tag("openai-compatible")
                            Text("Qwen（翻译模型）").tag("qwen-mt")
                        }
                        if job.provider == "openai-api" {
                            openAIModelPicker
                        } else {
                            Picker("具体模型", selection: Binding(get: { job.selectedModel }, set: { job.selectedModel = $0; job.changeModel() })) {
                                ForEach(job.modelChoices, id: \.self) { Text($0) }
                            }
                            if job.selectedModel == "自定义" { TextField("模型名称", text: $job.model) }
                        }
                        if job.provider == "codex-local" {
                            Picker("推理强度", selection: $job.effort) {
                                ForEach(job.effortChoices, id: \.self) { Text($0) }
                            }
                        }
                    }
                    if job.provider != "codex-local" {
                        if job.provider == "openai-api" {
                            LabeledContent("API 地址", value: "api.openai.com")
                            Text("请选择支持 Responses API 结构化输出的模型。开始翻译后，待译文本会发送到 OpenAI，并按 API 用量计费。")
                                .font(.caption).foregroundStyle(.secondary)
                        } else {
                            TextField("API 地址", text: $job.baseURL).disabled(job.hasRecordedProvider)
                        }
                        SecureField("API 密钥（仅本次使用）", text: $job.apiKey)
                    }
                }
                Section("输出与检查") {
                    HStack {
                        Text(job.outputPDF.isEmpty ? "保存到原 PDF 所在目录" : job.outputPDF)
                            .lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                        Spacer()
                        Button("保存到…") { job.selectOutput() }.disabled(job.pdf.isEmpty)
                    }
                    Toggle("生成后运行机器检查", isOn: $job.runQA)
                        .disabled(job.review?.manifest.recordedLayout == "legacy" || job.candidateDocument != nil)
                }
                Section("本地安装与工作区") {
                    HStack {
                        TextField("PaperLocale CLI 路径", text: $job.cli)
                            .accessibilityLabel("PaperLocale CLI 路径")
                        Button("选择 CLI…", action: job.selectCLI)
                    }
                    Text("\(LocalCLIInstallation.installationHelp) 翻译和机器检查还需要 Poppler。阅读 PDF 可直接使用。")
                        .font(.caption).foregroundStyle(.secondary)
                    HStack {
                        Text(job.workspaceRoot).lineLimit(2).truncationMode(.middle).textSelection(.enabled)
                        Spacer()
                        Button("选择工作区…") { job.selectWorkspace() }
                    }
                    DisclosureGroup("字体与请求大小") {
                        HStack {
                            Text(job.fontFile.isEmpty ? "中文字体自动获取" : job.fontFile).lineLimit(1)
                            Spacer()
                            Button("选择字体…") { job.selectFont() }
                        }
                        TextField("每次请求的字符上限", value: $job.maxCharacters, format: .number)
                    }
                }
            }
            .formStyle(.grouped)
            .disabled(job.running)
            Divider()
            HStack {
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.defaultAction)
            }.padding(14)
        }
        .frame(width: 550, height: 555)
        .onAppear {
            initialCLI = job.cli
            job.loadOpenAIModels(automatically: true)
        }
        .onDisappear { if initialCLI != job.cli { job.saveCLIPath() } }
    }

    @ViewBuilder private var openAIModelPicker: some View {
        let models = job.openAIModelState.models
        HStack {
            Picker("API 模型", selection: Binding(get: { job.model }, set: job.selectOpenAIModel)) {
                if job.model.isEmpty {
                    Text("请选择模型").tag("")
                } else if !models.contains(job.model) {
                    Text(job.model).tag(job.model)
                }
                ForEach(models, id: \.self) { Text($0).tag($0) }
            }
            .disabled(models.isEmpty)
            Button(models.isEmpty ? "获取模型" : "刷新模型") { job.loadOpenAIModels() }
                .disabled(job.openAIModelState.isLoading)
        }
        switch job.openAIModelState {
        case .idle:
            Text("输入 API 密钥后获取模型列表，也可使用 App 启动环境中的 OPENAI_API_KEY。")
                .font(.caption).foregroundStyle(.secondary)
        case .loading:
            ProgressView("正在获取项目模型…").controlSize(.small)
        case .loaded(let models) where models.isEmpty:
            Text("此 API 密钥没有返回适合文本任务的模型。")
                .font(.caption).foregroundStyle(.secondary)
        case .loaded(let models) where !job.model.isEmpty && !models.contains(job.model):
            Text("当前所选模型未在本次列表中返回。可从列表选择其他模型。")
                .font(.caption).foregroundStyle(.secondary)
        case .failed(let reason):
            Text(reason).font(.caption).foregroundStyle(.orange)
        default:
            EmptyView()
        }
    }
}

private struct ReviewPanel: View {
    @ObservedObject var job: TranslationJob
    let page: Int
    let jump: (Int) -> Void
    let locateSource: (Int, [SourceRegion]) -> Void

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 13) {
                Text("审校").font(.headline).accessibilityAddTraits(.isHeader)
                if let review = job.review, job.candidateDocument != nil {
                    status(review)
                    if job.hasManualRevision {
                        Button { job.openCandidateRevision(page: page) } label: {
                            Label("修订第 \(page) 页译文", systemImage: "pencil")
                        }
                        .disabled(job.isBusy)
                    }
                    if let report = review.report {
                        let errors = report.errors
                        let warnings = report.warnings ?? []
                        if !errors.isEmpty { issues("错误", errors) }
                        if !warnings.isEmpty { issues("警告", warnings) }
                    }
                    if let coverage = review.coverage { coverageSection(coverage) }
                    else {
                        Text("本次运行没有可读取的翻译覆盖记录。").font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Text("生成候选译文后，可以在这里检查结果。")
                        .font(.callout).foregroundStyle(.secondary)
                }
                if let report = job.diagnostic {
                    Divider()
                    Text(report.classification?.title ?? "运行失败").font(.subheadline.weight(.semibold))
                    Text(report.message).font(.caption).textSelection(.enabled)
                    let regions = report.items.flatMap { $0.regions ?? [] }
                    let pages = Set(report.items.flatMap { $0.pages ?? [] } + regions.map(\.page)
                                    + (report.page_context ?? []).map(\.page))
                    ForEach(pages.sorted(), id: \.self) { page in
                        Button("在原文定位第 \(page) 页") { locateSource(page, regions) }.buttonStyle(.link)
                    }
                    Button("打开修复窗口", action: job.showCurrentError)
                }
            }.padding(14)
        }.background(Color(nsColor: .controlBackgroundColor))
    }

    @ViewBuilder private func status(_ review: RunReview) -> some View {
        let state = review.qaState
        switch state {
        case .notRun: Label("未运行机器检查", systemImage: "clock")
        case .failed: Label("机器检查未通过", systemImage: "exclamationmark.circle").foregroundStyle(.orange)
        case .passed:
            Label("待人工验收", systemImage: "checklist")
            Text("请逐页对照原文和候选译文，核对图表、公式与科学表达。")
                .font(.caption).foregroundStyle(.secondary)
        case .accepted:
            Label("已人工验收", systemImage: "checkmark.seal").foregroundStyle(.green)
            if let reviewer = review.report?.visual_reviewed_by {
                Text("复核者 \(reviewer)").font(.caption).textSelection(.enabled)
            }
            if let date = review.report?.visual_reviewed_at {
                Text(date).font(.caption).foregroundStyle(.secondary)
            }
        case .unavailable:
            Label("检查或验收记录不可用", systemImage: "exclamationmark.circle")
            Text("请核对运行目录中的报告及其对应的候选 PDF。").font(.caption).foregroundStyle(.secondary)
        }
    }

    private func issues(_ heading: String, _ messages: [String]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("\(heading)（\(messages.count)）").font(.subheadline.weight(.semibold))
            ForEach(Array(messages.enumerated()), id: \.offset) { index, text in
                issue(ReviewIssue(id: index, qaText: text))
            }
        }
    }

    private func coverageSection(_ coverage: TranslationCoverage) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            if coverage.isPartial {
                Label("部分翻译", systemImage: "exclamationmark.circle").foregroundStyle(.orange)
                Text("本次跳过 \(coverage.user_skipped_segments.count) 个段落和 \(coverage.user_skipped_blocks.count) 个版面块。")
                    .font(.caption).foregroundStyle(.secondary)
                ForEach(Array(coverage.user_skipped_segments.enumerated()), id: \.offset) { index, segment in
                    issue(ReviewIssue(id: index, text: segment.source ?? "保留原文的段落", page: segment.pages?.first))
                }
                ForEach(coverage.user_skipped_blocks, id: \.self) { block in
                    if let original = coverage.preserved_blocks?.first(where: { $0.id == block }) {
                        Button {
                            locateSource(original.page, original.regions ?? [])
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Label("查看保留原文 · 第 \(original.page) 页", systemImage: "arrow.right.circle")
                                if let text = original.text { Text(text).foregroundStyle(.secondary).lineLimit(4) }
                            }.font(.caption).frame(maxWidth: .infinity, alignment: .leading)
                        }.buttonStyle(.link)
                    } else {
                        Text("版面块 \(block)").font(.caption).textSelection(.enabled)
                    }
                }
            }
            if let blocks = coverage.preserved_blocks, !blocks.isEmpty {
                DisclosureGroup("按版面规则保留（\(blocks.count) 项）") {
                    LazyVStack(alignment: .leading, spacing: 7) {
                        ForEach(blocks, id: \.id) { block in
                            issue(ReviewIssue(id: block.page, text: block.text ?? block.reason ?? "保留原文", page: block.page))
                        }
                    }.padding(.top, 6)
                }.font(.callout)
            }
        }
    }

    @ViewBuilder private func issue(_ item: ReviewIssue) -> some View {
        if let page = item.page {
            Button { jump(page) } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Label("第 \(page) 页", systemImage: "arrow.right.circle").font(.caption.weight(.medium))
                    Text(item.text).font(.caption).foregroundStyle(.secondary).lineLimit(4)
                }.frame(maxWidth: .infinity, alignment: .leading)
            }.buttonStyle(.link).help(item.text)
        } else {
            Text(item.text).font(.caption).lineLimit(4).help(item.text)
        }
    }
}

private struct AcceptanceRequest: Identifiable {
    let hash: String
    var id: String { hash }
}

private struct AcceptanceSheet: View {
    @ObservedObject var job: TranslationJob
    let request: AcceptanceRequest
    @Environment(\.dismiss) private var dismiss
    @ViewState private var reviewer = ""
    @ViewState private var confirmed = false

    var body: some View {
        VStack(alignment: .leading, spacing: 15) {
            Text("记录人工验收").font(.title2.weight(.semibold))
            Text(URL(fileURLWithPath: job.pdf).lastPathComponent).font(.callout).foregroundStyle(.secondary)
                .lineLimit(2).truncationMode(.middle)
            if job.review?.coverage?.isPartial == true {
                Label("此候选含保留原文的部分。验收后仍会标为部分翻译。", systemImage: "exclamationmark.circle")
                    .foregroundStyle(.orange)
            }
            if let warnings = job.review?.report?.warnings, !warnings.isEmpty {
                Text("机器检查报告有 \(warnings.count) 条警告。").foregroundStyle(.secondary)
            }
            TextField("复核者", text: $reviewer).textFieldStyle(.roundedBorder)
                .accessibilityLabel("复核者")
            Toggle("我已逐页检查这份候选译文", isOn: $confirmed)
            HStack {
                Text(job.outputPDF.isEmpty ? "译文保存到原 PDF 所在目录" : job.outputPDF)
                    .foregroundStyle(.secondary).font(.caption).lineLimit(2).truncationMode(.middle)
                Spacer()
                Button("保存到…") { job.selectOutput() }
            }
            Divider()
            HStack {
                Spacer()
                Button("取消") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("确认并记录") {
                    job.recordAcceptance(reviewer: reviewer, expectedHash: request.hash)
                    dismiss()
                }
                .buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction)
                .disabled(!confirmed || reviewer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }.padding(20).frame(width: 445)
    }
}
