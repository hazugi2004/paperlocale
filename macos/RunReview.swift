import Foundation
import CryptoKit

struct RunManifest: Decodable {
    enum Stage: String, Decodable {
        case initialized, collected, translated, validated, rendered, qa_generated, accepted

        var hasCandidate: Bool {
            self == .rendered || self == .qa_generated || self == .accepted
        }
    }

    struct Provider: Decodable {
        let provider: String?
        let model: String?
        let reasoning_effort: String?
        let base_url: String?
    }

    struct Domain: Decodable { let id: String }

    let status: Stage
    let source_pdf: String
    let source_sha256: String
    let translation_provider: Provider?
    let layout_mode: String?
    let reference_policy: String?
    let domain_pack: Domain?
    let rendered_pdf: String?
    let rendered_sha256: String?
    let output_pdf: String?
    let exported_pdf: String?
    let exported_sha256: String?
    let qa_report: String?
    let qa_output_dir: String?
    let accepted_by: String?

    static func read(in root: URL) throws -> Self {
        try JSONDecoder().decode(Self.self, from: Data(contentsOf: root.appendingPathComponent("run_manifest.json")))
    }

    var recordedLayout: String { layout_mode ?? (status == .initialized ? "paragraph" : "legacy") }
}

struct ReviewIssue: Identifiable {
    let id: Int
    let text: String
    let page: Int?

    init(id: Int, text: String, page: Int?) {
        self.id = id
        self.text = text
        self.page = page
    }

    // 旧版 QA 将页码写在消息开头，只有明示的物理页码可用来跳页。
    init(id: Int, qaText: String) {
        let range = qaText.range(of: #"^第\s*[1-9][0-9]*\s*页"#, options: .regularExpression)
        self.init(id: id, text: qaText, page: range.flatMap { Int(qaText[$0].filter(\.isNumber)) })
    }
}

struct MachineReview: Decodable {
    let source_pdf: String
    let translated_pdf: String
    let source_sha256: String
    let translated_sha256: String
    let errors: [String]
    let warnings: [String]?
    let visual_accepted: Bool?
    let visual_reviewed_by: String?
    let visual_reviewed_at: String?
}

struct TranslationCoverage: Decodable {
    struct Segment: Decodable {
        let source: String?
        let pages: [Int]?
    }

    struct PreservedBlock: Decodable {
        let id: String
        let page: Int
        let text: String?
        let reason: String?
        let regions: [SourceRegion]?
    }

    let translation_complete: Bool
    let user_skipped_segments: [Segment]
    let user_skipped_blocks: [String]
    let preserved_blocks: [PreservedBlock]?

    var isPartial: Bool {
        !translation_complete || !user_skipped_segments.isEmpty || !user_skipped_blocks.isEmpty
    }
}

struct PDFSnapshot {
    let url: URL
    let data: Data
    let sha256: String

    init(url: URL) throws {
        self.url = url
        // PDFKit 用同一份字节显示。路径后来被替换时，已显示的候选仍对应审阅时的哈希。
        data = try Data(contentsOf: url)
        sha256 = Self.hex(SHA256.hash(data: data))
    }

    static func hashFile(_ url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var digest = SHA256()
        while let chunk = try handle.read(upToCount: 1024 * 1024), !chunk.isEmpty {
            digest.update(data: chunk)
        }
        return hex(digest.finalize())
    }

    private static func hex(_ digest: SHA256.Digest) -> String {
        digest.map { String(format: "%02x", $0) }.joined()
    }
}

struct RunReview {
    enum QAState: Equatable {
        case notRun, failed, passed, accepted, unavailable
    }

    let manifest: RunManifest
    let candidate: URL?
    let report: MachineReview?
    let coverage: TranslationCoverage?
    let exported: URL?
    let problem: String?

    var qaState: QAState {
        if report?.errors.isEmpty == false { return .failed }
        switch manifest.status {
        case .rendered: return .notRun
        case .qa_generated: return report == nil ? .unavailable : .passed
        case .accepted:
            if let report, report.visual_accepted == true,
               let reviewer = report.visual_reviewed_by, !reviewer.isEmpty, reviewer == manifest.accepted_by {
                return .accepted
            }
            return .unavailable
        default: return .notRun
        }
    }

    static func canonical(_ url: URL) -> URL {
        url.standardizedFileURL.resolvingSymlinksInPath()
    }

    static func read(in root: URL, source: URL, sourceHash: String, cachedCandidateHash: String?) throws
        -> (review: RunReview, candidateBytes: Data?)? {
        let manifestPath = root.appendingPathComponent("run_manifest.json")
        guard FileManager.default.fileExists(atPath: manifestPath.path) else { return nil }
        let manifest = try RunManifest.read(in: root)
        guard canonical(URL(fileURLWithPath: manifest.source_pdf)) == source,
              manifest.source_sha256 == sourceHash else {
            throw RunReadError.sourceChanged
        }
        guard manifest.status.hasCandidate else {
            return (RunReview(manifest: manifest, candidate: nil, report: nil, coverage: nil,
                              exported: nil, problem: nil), nil)
        }

        guard let path = manifest.rendered_pdf, let candidateHash = manifest.rendered_sha256,
              !candidateHash.isEmpty else {
            return (unavailable(manifest), nil)
        }
        let candidateURL = canonical(URL(fileURLWithPath: path))
        let candidateBytes: Data?
        if candidateHash == cachedCandidateHash {
            // 验收等步骤可能只更新报告。保留已打开的 PDFKit 文档，同时核对磁盘仍是同一候选。
            guard (try? PDFSnapshot.hashFile(candidateURL)) == candidateHash else { return (unavailable(manifest), nil) }
            candidateBytes = nil
        } else {
            guard let pdf = try? PDFSnapshot(url: candidateURL), pdf.sha256 == candidateHash else {
                return (unavailable(manifest), nil)
            }
            candidateBytes = pdf.data
        }

        let boundReport = manifest.qa_report.map { URL(fileURLWithPath: $0) }
        let defaultReport = (manifest.qa_output_dir.map { URL(fileURLWithPath: $0) }
                             ?? root.appendingPathComponent("qa")).appendingPathComponent("qa_report.json")
        // QA 失败时清单仍是 rendered，报告可留在 QA 目录。只有路径和双哈希完全闭合的报告属于当前候选。
        let reportURL = manifest.status == .rendered ? defaultReport : boundReport
        let parsed = reportURL.flatMap { try? JSONDecoder().decode(MachineReview.self, from: Data(contentsOf: $0)) }
        let report = parsed.flatMap { report -> MachineReview? in
            guard canonical(URL(fileURLWithPath: report.source_pdf)) == source,
                  canonical(URL(fileURLWithPath: report.translated_pdf)) == candidateURL,
                  report.source_sha256 == sourceHash, report.translated_sha256 == candidateHash else { return nil }
            return manifest.status != .rendered || !report.errors.isEmpty ? report : nil
        }

        let coveragePath = root.appendingPathComponent("translation_coverage.json")
        let coverage = try? JSONDecoder().decode(TranslationCoverage.self, from: Data(contentsOf: coveragePath))
        var exported: URL?
        if let output = manifest.exported_pdf, manifest.exported_sha256 == candidateHash {
            let url = URL(fileURLWithPath: output)
            if (try? PDFSnapshot.hashFile(url)) == candidateHash { exported = url }
        }
        return (RunReview(manifest: manifest, candidate: candidateURL, report: report, coverage: coverage,
                          exported: exported, problem: nil), candidateBytes)
    }

    private static func unavailable(_ manifest: RunManifest) -> RunReview {
        RunReview(manifest: manifest, candidate: nil, report: nil, coverage: nil, exported: nil,
                  problem: "运行目录中的候选 PDF 缺失或已变化。请恢复已绑定的文件，或为原文新建运行。")
    }
}

enum RunReadError: LocalizedError {
    case sourceChanged

    var errorDescription: String? {
        "此断点绑定的原文与当前打开的 PDF 不一致。可为当前文件新建运行，或重新打开原文件。"
    }
}
