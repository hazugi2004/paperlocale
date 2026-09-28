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
        print("drop, progress, UTF-8, report decoding passed")
    }
}
''')
            binary=work/'interaction-test'
            subprocess.run(['xcrun','swiftc','-swift-version','5','-parse-as-library',str(app),
                            str(root/'macos/TranslationOptions.swift'),str(main),'-o',str(binary)],check=True,capture_output=True)
            output=subprocess.run([str(binary)],check=True,capture_output=True,text=True,timeout=20)
            self.assertIn('passed',output.stdout)
