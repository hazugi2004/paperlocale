"""编译实际 App，检查拖放、断点身份、候选版本和审校状态。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

@unittest.skipUnless(shutil.which('xcrun'), '需要 macOS Swift 编译器')
class MacOSInteractionTests(unittest.TestCase):
    def test_drop_progress_and_report_decoding(self):
        root = Path(__file__).resolve().parents[1]
        sources = sorted((root / "macos").glob("*.swift"))
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            # 只替换可执行入口，其余使用发行 App 的源码和最低系统版本。
            app = work / "App.swift"
            entry = root / "macos/PaperLocaleApp.swift"
            app.write_text(entry.read_text().split("@main struct PaperLocaleApp")[0])
            main = work / "Main.swift"
            main.write_text(r'''
import Foundation
import AppKit
import PDFKit

@main struct TestMain {
    @MainActor static func waitForLoad(_ job: TranslationJob, source: URL) async throws {
        for _ in 0..<300 {
            if !job.isBusy && job.pdf == source.path && job.sourceDocument != nil { return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        preconditionFailure("PDF 预览和运行检查未完成")
    }

    @MainActor static func createPDF(_ url: URL, color: NSColor) {
        let image = NSImage(size: CGSize(width: 300, height: 420), flipped: false) { rect in
            color.setFill()
            NSBezierPath(rect: rect).fill()
            return true
        }
        let pdf = PDFDocument()
        for index in 0..<2 { pdf.insert(PDFPage(image: image)!, at: index) }
        precondition(pdf.write(to: url))
    }

    static func writeJSON(_ object: [String: Any], to url: URL) throws {
        try JSONSerialization.data(withJSONObject: object).write(to: url)
    }

    @MainActor static func verifySourceIssueMarks(_ source: URL) throws {
        // 同一蓝色矩形在一个带偏移裁切框的纸页上，按四个方向旋转后位置不同，
        // 但定位 API 应恢复同一个 PDF 原始页矩形；显示坐标不可二次旋转。
        let crop = CGRect(x: 40, y: 70, width: 370, height: 400)
        let original = CGRect(x: 75, y: 265, width: 70, height: 55)
        let regions: [(Int, [Double])] = [
            (0, [35, 150, 105, 205]), (90, [195, 35, 250, 105]),
            (180, [265, 195, 335, 250]), (270, [150, 265, 205, 335]),
        ]
        for (rotation, region) in regions {
            precondition(sourceIssuePageRect(SourceRegion(page: 2, rect: region), crop: crop, rotation: rotation) == original)
        }
        let invalid = SourceRegion(page: 2, rect: [20, 480, 290, 512])
        precondition(sourceIssuePageRect(invalid, crop: crop, rotation: 0) == nil)
        let boundary = SourceRegion(page: 2, rect: [-5, 390, 20, 410])
        precondition(sourceIssuePageRect(boundary, crop: crop, rotation: 0) == CGRect(x: 40, y: 70, width: 20, height: 10))

        let bytes = try Data(contentsOf: source)
        let document = PDFDocument(url: source)!
        let reader = PDFReader()
        reader.load(source: document, candidate: nil)
        let marked = SourceRegion(page: 2, rect: [40, 55, 90, 66])
        reader.setSourceIssues([marked])
        reader.locateSourceIssue(page: 2, regions: [marked])
        precondition((0..<document.pageCount).allSatisfy { document.page(at: $0)?.annotations.isEmpty == true })
        reader.setSourceIssues([])
        let after = try Data(contentsOf: source)
        precondition(after == bytes)
    }

    @MainActor static func main() async throws {
        let directory = RunReview.canonical(FileManager.default.temporaryDirectory).appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let pdf = directory.appendingPathComponent("中文 source.pdf")
        createPDF(pdf, color: .white)
        try verifySourceIssueMarks(pdf)
        let job = TranslationJob()
        precondition(job.provider == "codex-local")
        job.workspaceRoot = directory.appendingPathComponent("workspace").path
        let provider = NSItemProvider(contentsOf: pdf)!
        precondition(job.receiveDrop([provider]))
        try await waitForLoad(job, source: pdf)
        precondition(job.sourceDocument?.pageCount == 2 && job.review == nil)
        job.running = true
        precondition(!job.receiveDrop([provider]))
        job.acceptPDF(directory.appendingPathComponent("missing.pdf"))
        precondition(job.pdf == pdf.path)
        job.running = false
        precondition(!job.receiveDrop([provider,provider]))
        let record = "PAPERLOCALE_PROGRESS {\"stage\":\"翻译片段\",\"fraction\":0.5,\"completed\":2,\"total\":4}\n"
        for byte in record.utf8 { job.append(Data([byte])) }
        precondition(job.fraction == 0.5)
        precondition(job.stage == "翻译片段 · 2/4 段")
        let raw = #"{"error_id":"current","message":"error","solution":"retry","items":[{"source":"Original sentence.","pages":[1,2]}],"actions":[{"key":"q","label":"退出"}]}"#
        let report = try JSONDecoder().decode(RepairReport.self, from: Data(raw.utf8))
        precondition(report.items[0].pageLabel == "PDF 第 1, 2 页")
        precondition(report.items[0].target == nil && report.items[0].errors == nil)
        let detailed = #"{"error_id":"new","message":"未通过","solution":"retry","items":[{"id":"segment","source":"CDHE intensity.","pages":[2],"validation_source":"CDHE {v0}.","target":"强度{v0}。","errors":["abbreviation 标记缺失：CDHE"]}],"actions":[]}"#
        let current = try JSONDecoder().decode(RepairReport.self, from: Data(detailed.utf8))
        precondition(current.items[0].id == "segment")
        precondition(current.items[0].target == "强度{v0}。")
        precondition(current.items[0].errors == ["abbreviation 标记缺失：CDHE"])
        precondition(current.items[0].validation_source == "CDHE {v0}.")

        // 第 4 页旧草稿多写了左括号，且遗漏了图号的 d。只恢复可核实的图号，
        // 用户在锚点前后输入的中文和空白直接进入待提交的区块文本。
        let revisionJSON = #"{"items":[{"id":"page4:unit","segment_id":"segment","source":"Highest frequency (Fig. 3d–i), elsewhere.","validation_source":"Highest frequency {v0}d–i), elsewhere.","target":"主要出现在热带地区{v0}d–i），其余内容。","anchors":{"{v0}":"(Fig. 3"},"regions":[]}]}"#
        let block = try JSONDecoder().decode(RevisionBlocks.self, from: Data(revisionJSON.utf8)).items[0]
        let locks = RevisionText.template(for: block)!
        let malformed = "主要出现在赤道地区( {v0}–i），其余内容。"
        precondition(locks.editing(malformed) == nil)
        var recovered = locks.restoringFigure(in: malformed)!
        precondition(recovered.display == "主要出现在赤道地区(Fig. 3d–i），其余内容。")
        precondition(recovered.encoded == "主要出现在赤道地区{v0}d–i），其余内容。")
        recovered.parts[0] = "每个发生过事件的夏季平均 10.43 天。 最高频次主要出现在赤道地区"
        precondition(recovered.encoded.utf8.elementsEqual(
            "每个发生过事件的夏季平均 10.43 天。 最高频次主要出现在赤道地区{v0}d–i），其余内容。".utf8))
        let anchorInText = "中文(Fig. 3d–i），其余内容。"
        precondition(locks.restoringFigure(in: anchorInText)?.encoded == "中文{v0}d–i），其余内容。")
        precondition(locks.restoringFigure(in: anchorInText + anchorInText) == nil)
        let request = RevisionRequest(page: 4, origin: .candidate("shown-bytes"))
        precondition(request.arguments(in: directory).suffix(4) ==
            ["--page", "4", "--expected-rendered-sha256", "shown-bytes"])

        // 打开 PDF 自动找到默认工作区中的断点，并沿用旧运行的模型与版面设置。
        let run = job.runDirectory
        try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
        let manifestURL = run.appendingPathComponent("run_manifest.json")
        let sourceHash = try PDFSnapshot.hashFile(pdf)
        var manifest: [String: Any] = ["source_pdf": pdf.path, "source_sha256": sourceHash,
            "status": "collected", "layout_mode": "legacy", "translation_provider":
            ["provider": "openai-compatible", "model": "research-custom", "base_url": "https://example.invalid/v1"]]
        try writeJSON(manifest, to: manifestURL)
        job.acceptPDF(pdf)
        try await waitForLoad(job, source: pdf)
        precondition(job.model == "research-custom" && job.selectedModel == "自定义")
        precondition(job.provider == "openai-compatible" && job.baseURL == "https://example.invalid/v1")
        precondition(job.runDirectory.path == run.path && job.review?.manifest.recordedLayout == "legacy")

        // 打开官方 API 断点时保留已记录的模型，并清除上一个服务的临时密钥。
        job.apiKey = "compatible-test-key"
        manifest["translation_provider"] = ["provider": "openai-api", "model": "official-model",
            "endpoint": "https://api.openai.com/v1/responses"]
        try writeJSON(manifest, to: manifestURL)
        job.loadRun(run)
        try await waitForLoad(job, source: pdf)
        precondition(job.provider == "openai-api" && job.model == "official-model")
        precondition(job.baseURL.isEmpty && job.apiKey.isEmpty)

        // PDF 内容相同但路径不同，也不能取得另一份论文的运行与验收记录。
        let another = directory.appendingPathComponent("another")
        try FileManager.default.createDirectory(at: another, withIntermediateDirectories: true)
        let sameName = another.appendingPathComponent(pdf.lastPathComponent)
        try FileManager.default.copyItem(at: pdf, to: sameName)
        job.acceptPDF(sameName)
        try await waitForLoad(job, source: sameName)
        precondition(job.existingRun.isEmpty && job.runDirectory != run && job.review == nil)

        let candidate = run.appendingPathComponent("candidate.pdf")
        createPDF(candidate, color: .lightGray)
        let candidateHash = try PDFSnapshot.hashFile(candidate)
        manifest["rendered_pdf"] = candidate.path
        manifest["rendered_sha256"] = candidateHash
        manifest["status"] = "rendered"
        try writeJSON(manifest, to: manifestURL)
        try writeJSON(["translation_complete": false, "user_skipped_segments": [["source": "Kept text", "pages": [2]]],
            "user_skipped_blocks": []], to: run.appendingPathComponent("translation_coverage.json"))
        job.loadRun(run)
        try await waitForLoad(job, source: pdf)
        precondition(job.candidateDocument?.pageCount == 2)
        precondition(job.review?.qaState == .notRun && job.review?.coverage?.isPartial == true)
        precondition(job.review?.coverage?.user_skipped_segments.first?.pages == [2])

        // 缺失或旧报告允许阅读候选，只有报告、路径与两个文件哈希一致时才能提示待验收。
        let reportURL = run.appendingPathComponent("qa_report.json")
        manifest["qa_report"] = reportURL.path
        manifest["status"] = "qa_generated"
        try writeJSON(manifest, to: manifestURL)
        let read = { try RunReview.read(in: run, source: pdf, sourceHash: sourceHash, cachedCandidateHash: candidateHash) }
        let missing = try read()
        precondition(missing?.review.qaState == .unavailable && missing?.review.candidate != nil)
        var qa: [String: Any] = ["source_pdf": pdf.path, "source_sha256": sourceHash,
            "translated_pdf": candidate.path, "translated_sha256": "old", "errors": []]
        try writeJSON(qa, to: reportURL)
        let stale = try read()
        precondition(stale?.review.qaState == .unavailable)
        qa["translated_sha256"] = candidateHash
        try writeJSON(qa, to: reportURL)
        let passed = try read()
        precondition(passed?.review.qaState == .passed)
        manifest["status"] = "accepted"
        manifest["accepted_by"] = "试用复核"
        try writeJSON(manifest, to: manifestURL)
        let pending = try read()
        precondition(pending?.review.qaState == .unavailable)
        qa["visual_accepted"] = true
        qa["visual_reviewed_by"] = "试用复核"
        try writeJSON(qa, to: reportURL)
        let accepted = try read()
        precondition(accepted?.review.qaState == .accepted)
        createPDF(candidate, color: .yellow)
        let replaced = try read()
        precondition(replaced?.review.candidate == nil && replaced?.review.problem != nil)

        // 模拟磁盘写入失败时的结构化错误流；无需依赖 error_report.json 存在。
        for byte in ("PAPERLOCALE_ERROR " + detailed + "\n").utf8 { job.append(Data([byte])) }
        job.showCurrentError()
        precondition(job.repairReport?.error_id == "new")
        let cross = WorkspacePaths.runDirectory(pdf: "/Users/Shared/论文 source.pdf", root: "/Users/Shared/paperlocale")
        print("workspace=" + cross.path)
        print("drop, progress, UTF-8, report decoding passed")
    }
}
''')
            binary = work / "interaction-test"
            import platform
            build = subprocess.run([
                "xcrun", "swiftc", "-swift-version", "5", "-parse-as-library", "-target",
                f"{platform.machine()}-apple-macosx13.0", str(app),
                *[str(path) for path in sources if path != entry], str(main), "-o", str(binary),
            ], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            output = subprocess.run([str(binary)], capture_output=True, text=True, timeout=20)
            self.assertEqual(output.returncode, 0, output.stdout + output.stderr)
            self.assertIn('passed',output.stdout)
            from paperlocale.workspaces import run_directory
            self.assertIn('workspace=' + str(run_directory(Path('/Users/Shared/论文 source.pdf'), Path('/Users/Shared/paperlocale'))), output.stdout)
