"""编译实际 App 模型，验证真实拖放提供者、运行中保护和跨字节进度解析。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

@unittest.skipUnless(shutil.which('xcrun'), '需要 macOS Swift 编译器')
class MacOSInteractionTests(unittest.TestCase):
    def test_drop_progress_and_report_decoding(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp)
            # 只替换可执行入口，TranslationJob/SwiftUI 内容与发行源码完全相同。
            app=work/'App.swift'
            app.write_text((root/'macos/PaperLocaleApp.swift').read_text().split('@main struct PaperLocaleApp')[0])
            main=work/'Main.swift'
            main.write_text(r'''
import Foundation
import AppKit
@main struct TestMain {
    @MainActor static func main() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let pdf = directory.appendingPathComponent("中文 source.pdf")
        try Data("%PDF-1.4\n".utf8).write(to: pdf)
        let job = TranslationJob()
        let provider = NSItemProvider(contentsOf: pdf)!
        precondition(job.receiveDrop([provider]))
        for _ in 0..<100 {
            if job.pdf == pdf.path { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        precondition(job.pdf == pdf.path, "真实 NSItemProvider 拖放回调应设置 PDF")
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

        // 打开自定义服务的旧断点必须保留精确模型，不能被 picker 的默认选择重置。
        let run = directory.appendingPathComponent("旧运行")
        try FileManager.default.createDirectory(at: run, withIntermediateDirectories: true)
        let manifest: [String: Any] = ["source_pdf": pdf.path, "translation_provider":
            ["provider": "openai-compatible", "model": "research-custom", "base_url": "https://example.invalid/v1"]]
        try JSONSerialization.data(withJSONObject: manifest).write(to: run.appendingPathComponent("run_manifest.json"))
        job.loadRun(run)
        precondition(job.model == "research-custom" && job.selectedModel == "自定义")
        precondition(job.provider == "openai-compatible" && job.baseURL == "https://example.invalid/v1")
        precondition(job.runDirectory.path == run.path)
        job.acceptPDF(pdf)
        precondition(job.existingRun.isEmpty, "选择另一份 PDF 必须解除旧运行绑定")
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
            binary=work/'interaction-test'
            subprocess.run(['xcrun','swiftc','-swift-version','5','-parse-as-library',str(app),
                            str(root/'macos/TranslationOptions.swift'),str(main),'-o',str(binary)],check=True,capture_output=True)
            output=subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=20)
            self.assertIn('passed',output.stdout)
            from paperlocale.workspaces import run_directory
            self.assertIn('workspace=' + str(run_directory(Path('/Users/Shared/论文 source.pdf'), Path('/Users/Shared/paperlocale'))), output.stdout)
