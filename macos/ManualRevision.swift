import SwiftUI
import AppKit

private typealias RevisionViewState<Value> = SwiftUI.State<Value>

enum RevisionOrigin: Hashable {
    case diagnostic(String)
    case candidate(String)

    var arguments: [String] {
        switch self {
        case .diagnostic(let id): return ["--error-id", id]
        case .candidate(let hash): return ["--expected-rendered-sha256", hash]
        }
    }

    var isCandidate: Bool {
        if case .candidate = self { return true }
        return false
    }
}

struct RevisionRequest: Hashable, Identifiable {
    let page: Int
    let origin: RevisionOrigin
    var id: Self { self }

    func arguments(in directory: URL) -> [String] {
        ["manual-revision", "--run-dir", directory.path, "--page", String(page)] + origin.arguments
    }
}

struct RevisionBlock: Decodable, Identifiable {
    struct ProtectedSpan: Decodable {
        let markers: [String]
        let encoded: String
        let display: String
    }

    let id: String
    let segment_id: String
    let source: String
    let validation_source: String?
    let target: String
    let anchors: [String: String]
    let regions: [SourceRegion]
    let rule_details: [RuleDetail]?
    let unresolved_denominator: String?
    let protected_spans: [ProtectedSpan]?
}

struct RevisionBlocks: Decodable { let items: [RevisionBlock] }

struct RevisionEdit: Codable {
    let id: String
    let before: String
    let target: String
    let confirm_denominator: String?
}

struct RevisionResult: Decodable {
    let valid: Bool
    let errors: [String]
}

// 译文展示和实际提交分开。普通文字逐段保留，只有固定标记在预览里显示为 PDF 内容。
struct RevisionText: Equatable {
    struct Lock: Equatable {
        let marker: String
        let display: String
        let encoded: String
        let figureSuffix: String?
    }

    var parts: [String]
    let locks: [Lock]
    var encoded: String { joined(using: \.encoded) }
    var display: String { joined(using: \.display) }

    private func joined(using key: KeyPath<Lock, String>) -> String {
        var text = parts[0]
        for index in locks.indices { text += locks[index][keyPath: key] + parts[index + 1] }
        return text
    }

    func previewDraft(_ text: String) -> String {
        // 待修复的草稿只显示目前的内容；若漏了子图字母，预览也应显示这个差异。
        locks.reduce(text) { draft, lock in
            let label = String(lock.display.dropLast(lock.figureSuffix?.count ?? 0))
            return draft.replacingOccurrences(of: lock.figureSuffix == nil ? lock.encoded : lock.marker, with: label)
        }
    }

    private static func unique(_ value: String, in text: String) -> Range<String.Index>? {
        guard !value.isEmpty, let range = text.range(of: value),
              text[range.upperBound...].range(of: value) == nil else { return nil }
        return range
    }

    private static func suffix(after marker: String, in text: String) -> String? {
        guard let markerRange = unique(marker, in: text) else { return nil }
        let rest = text[markerRange.upperBound...]
        guard let suffixRange = rest.range(of: #"^[a-z](?:[–—-][a-z])?[)）]"#, options: [.regularExpression, .caseInsensitive])
        else { return nil }
        return String(rest[suffixRange])
    }

    private static func normalizedClose(_ text: String) -> String {
        text.replacingOccurrences(of: "）", with: ")")
    }

    static func template(for block: RevisionBlock) -> Self? {
        let source = block.validation_source ?? block.target
        guard let pattern = try? NSRegularExpression(pattern: #"\{v[0-9]+\}"#) else { return nil }
        let ids = pattern.matches(in: source, range: NSRange(source.startIndex..., in: source)).compactMap { match in
            Range(match.range, in: source).map { String(source[$0]) }
        }
        guard Set(ids).count == ids.count, Set(ids) == Set(block.anchors.keys) else { return nil }
        // 后端为旧断点提供可合并的固定片段。界面显示一个实体，仍提交原占位符和空白。
        let groups = block.protected_spans ?? []
        let continuations = Set(groups.flatMap { $0.markers.dropFirst() })
        let locks = ids.filter { !continuations.contains($0) }.compactMap { id -> Lock? in
            if let group = groups.first(where: { $0.markers.first == id }) {
                return Lock(marker: id, display: group.display, encoded: group.encoded, figureSuffix: nil)
            }
            guard let label = block.anchors[id], !label.isEmpty else { return nil }
            // Fig. 3 的底层锚点可能只覆盖左括号和编号。原句和可读 PDF 文本均证明
            // 连写的是同一个子图范围时，界面再将尾缀一起锁定，避免把 d 当成天。
            if label.range(of: #"^[（(]Fig(?:ure)?\.?\s*[0-9]+$"#, options: [.regularExpression, .caseInsensitive]) != nil,
               let sourceSuffix = suffix(after: id, in: source),
               unique(normalizedClose(label + sourceSuffix), in: normalizedClose(block.source)) != nil {
                let candidateSuffix = suffix(after: id, in: block.target)
                let tail = candidateSuffix.flatMap {
                    normalizedClose($0) == normalizedClose(sourceSuffix) ? $0 : nil
                } ?? sourceSuffix
                return Lock(marker: id, display: label + tail, encoded: id + tail, figureSuffix: tail)
            }
            return Lock(marker: id, display: label, encoded: id, figureSuffix: nil)
        }
        guard locks.count == ids.count - continuations.count else { return nil }
        return Self(parts: Array(repeating: "", count: locks.count + 1), locks: locks)
    }

    func editing(_ text: String) -> Self? {
        var remaining = text
        var pieces: [String] = []
        for lock in locks {
            guard let range = Self.unique(lock.encoded, in: remaining) else { return nil }
            pieces.append(String(remaining[..<range.lowerBound]))
            remaining = String(remaining[range.upperBound...])
        }
        pieces.append(remaining)
        guard !pieces.contains(where: { $0.range(of: #"\{v[0-9]+\}"#, options: .regularExpression) != nil })
        else { return nil }
        return Self(parts: pieces, locks: locks)
    }

    func restoringFigure(in draft: String) -> Self? {
        var target = draft
        for lock in locks where lock.figureSuffix != nil {
            if Self.unique(lock.encoded, in: target) != nil { continue }
            // 只替换可唯一确认的引用槽位。普通中文、数字和两侧标点不参与恢复，
            // 确认之前将该建议与当前草稿分开，避免自动写回用户的失败草稿。
            if let expanded = Self.unique(lock.display, in: target) {
                target.replaceSubrange(expanded, with: lock.encoded)
                continue
            }
            guard let marker = Self.unique(lock.marker, in: target) else { continue }
            let originalTail = target[marker.upperBound...]
            guard let tail = originalTail.range(of: #"^(?:[a-z]|[a-z]?[–—-][a-z])?[)）]"#,
                                                options: [.regularExpression, .caseInsensitive]) else { continue }
            let start = target[..<marker.lowerBound]
                .range(of: #"[（(][ \t]*$"#, options: .regularExpression)?.lowerBound ?? marker.lowerBound
            target.replaceSubrange(start..<tail.upperBound, with: lock.encoded)
        }
        guard target != draft else { return nil }
        return editing(target)
    }
}

@MainActor struct ManualRevisionWorkbench: View {
    @ObservedObject var job: TranslationJob
    let origin: RevisionOrigin
    let pages: [Int]
    let report: RepairReport?
    var showRepairOptions: (() -> Void)?
    @RevisionViewState private var page: Int
    @RevisionViewState private var selectedID: String?
    @RevisionViewState private var focus = 0

    init(job: TranslationJob, request: RevisionRequest, pages: [Int], report: RepairReport? = nil,
         showRepairOptions: (() -> Void)? = nil) {
        self.job = job
        origin = request.origin
        self.pages = pages
        self.report = report
        self.showRepairOptions = showRepairOptions
        _page = RevisionViewState(initialValue: request.page)
    }

    private var request: RevisionRequest { RevisionRequest(page: page, origin: origin) }
    private var block: RevisionBlock? { job.revisionBlocks.first { $0.id == selectedID } }
    private var sourceRegions: [SourceRegion] { block?.regions ?? report?.items.flatMap { $0.regions ?? [] } ?? [] }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(origin.isCandidate ? "修订译文" : "修复问题").font(.title2.bold())
                if pages.count > 1 {
                    Picker("页码", selection: $page) {
                        ForEach(pages, id: \.self) { Text("第 \($0) 页").tag($0) }
                    }.fixedSize().disabled(job.isBusy)
                } else { Text("第 \(page) 页").foregroundStyle(.secondary) }
                Spacer()
                if let showRepairOptions {
                    Button("其他处理方式", action: showRepairOptions).disabled(job.isBusy)
                }
                Button("关闭") {
                    if origin.isCandidate { job.candidateRevision = nil }
                    else { job.repairReport = nil }
                }.disabled(job.isBusy)
            }
            HSplitView {
                VStack(alignment: .leading, spacing: 5) {
                    HStack {
                        Text("原 PDF").font(.subheadline.weight(.semibold))
                        if sourceRegions.contains(where: { $0.page == page }) {
                            Button("定位标记区域") { focus += 1 }.buttonStyle(.link)
                        }
                    }
                    SourcePDFPreview(path: job.pdf, page: page, regions: sourceRegions, focus: focus)
                }.frame(minWidth: 350, idealWidth: 495)
                VStack(alignment: .leading, spacing: 10) {
                    if let classification = report?.classification, classification.code == "layout.capacity" {
                        Text(classification.title).font(.subheadline.weight(.semibold))
                        Text(classification.guidance).font(.callout).foregroundStyle(.secondary)
                        if report?.actions.contains(where: { $0.key == "f" }) == true {
                            Text("「其他处理方式」中也可以降低失败段落的字号下限后重试。")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if job.revisionBlocks.count > 1 {
                        Picker("译文区块", selection: $selectedID) {
                            ForEach(Array(job.revisionBlocks.enumerated()), id: \.element.id) { index, item in
                                Text("\(index + 1). " + String(item.source.prefix(62))).tag(Optional(item.id))
                            }
                        }
                        .accessibilityLabel("选择译文区块")
                        .disabled(job.isBusy)
                    }
                    if let block {
                        ManualRevisionEditor(job: job, request: request, block: block) { focus += 1 }
                            .id(block.id)
                    } else if job.running {
                        ProgressView("正在读取本页区块…")
                    } else {
                        Text(job.revisionFeedback.isEmpty ? "本页没有可修订的译文区块。" : job.revisionFeedback)
                            .foregroundStyle(.secondary).textSelection(.enabled)
                    }
                    Spacer(minLength: 0)
                }.padding(.leading, 12).frame(minWidth: 440, idealWidth: 540)
            }
        }
        .padding(20).frame(minWidth: 865, idealWidth: 1070, minHeight: 550, idealHeight: 715)
        .onAppear { job.loadRevisionBlocks(request) }
        .onChange(of: page) { _ in job.loadRevisionBlocks(request) }
        .onChange(of: job.revisionBlocks.map(\.id)) { ids in selectedID = ids.first }
        .onChange(of: selectedID) { id in
            if id != nil { job.revisionFeedback = ""; focus += 1 }
        }
    }
}

@MainActor private struct ManualRevisionEditor: View {
    @ObservedObject var job: TranslationJob
    let request: RevisionRequest
    let block: RevisionBlock
    let locate: () -> Void
    let originalDraft: String
    let recovery: RevisionText?
    let template: RevisionText?
    @RevisionViewState private var editor: RevisionText?
    @RevisionViewState private var confirmed = false

    init(job: TranslationJob, request: RevisionRequest, block: RevisionBlock, locate: @escaping () -> Void) {
        self.job = job
        self.request = request
        self.block = block
        self.locate = locate
        let draft = job.revisionDraft(for: block, request: request) ?? block.target
        originalDraft = draft
        let template = RevisionText.template(for: block)
        self.template = template
        recovery = template?.restoringFigure(in: draft)
        _editor = RevisionViewState(initialValue: template?.editing(draft))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ScrollView {
                VStack(alignment: .leading, spacing: 11) {
                    if let details = block.rule_details, !details.isEmpty {
                        DisclosureGroup("本段检查记录") {
                            ForEach(Array(details.enumerated()), id: \.offset) { _, detail in
                                Text(detail.message).textSelection(.enabled).font(.caption)
                            }
                        }
                    }
                    Text("原文").font(.subheadline.weight(.semibold))
                    Text(block.source).font(.callout).textSelection(.enabled)
                    if let editor {
                        Text("修订译文").font(.subheadline.weight(.semibold))
                        ForEach(editor.parts.indices, id: \.self) { index in
                            if index > 0 {
                                Button(action: locate) {
                                    Label(editor.locks[index - 1].display, systemImage: "lock")
                                }.buttonStyle(.bordered).help("定位原 PDF 中的固定内容")
                            }
                            TextEditor(text: Binding(get: { self.editor?.parts[index] ?? "" }, set: { text in
                                self.editor?.parts[index] = text
                            }))
                            .font(.body).frame(height: editor.parts[index].count > 150 ? 108 : 65)
                            .border(Color.secondary.opacity(0.35)).disabled(job.isBusy)
                            .accessibilityLabel("修订译文 \(index + 1)")
                        }
                        DisclosureGroup("预览整段译文") {
                            Text(editor.display).textSelection(.enabled).padding(.top, 4)
                        }
                    } else {
                        Text("草稿中的固定内容需要核对").font(.subheadline.weight(.semibold))
                        Text(template?.previewDraft(originalDraft) ?? originalDraft).font(.callout).textSelection(.enabled)
                        if let recovery {
                            Text("建议恢复源文的图号。确认后可继续编辑草稿。").font(.callout)
                            Text(recovery.display).textSelection(.enabled)
                            Button("恢复图号") { editor = recovery }.buttonStyle(.borderedProminent)
                        } else if let template {
                            Button("按原文恢复固定内容") { editor = template }
                                .help("以原页固定内容为基础，可从上方旧草稿复制中文到相应位置。")
                        }
                    }
                }
            }
            if let denominator = block.unresolved_denominator, editor != nil {
                Toggle("我已核对原文「\(denominator)」的统计分母", isOn: $confirmed)
                    .disabled(job.isBusy)
            }
            if !job.revisionFeedback.isEmpty {
                Text(job.revisionFeedback).font(.callout).textSelection(.enabled)
            }
            if let editor {
                HStack {
                    Button("检查修订") { submit(editor, checkOnly: true) }
                    Spacer()
                    Button("采用我的修订") { submit(editor, checkOnly: false) }
                        .buttonStyle(.borderedProminent)
                }
                .disabled(job.isBusy || editor.encoded.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .onChange(of: editor) { updated in
            guard let updated else { return }
            confirmed = false
            job.revisionFeedback = ""
            job.saveRevisionDraft(updated.encoded, for: block, request: request)
        }
    }

    private func submit(_ text: RevisionText, checkOnly: Bool) {
        job.submitRevision(RevisionEdit(id: block.id, before: block.target, target: text.encoded,
                           confirm_denominator: confirmed ? block.unresolved_denominator : nil),
                           request: request, checkOnly: checkOnly)
    }
}
