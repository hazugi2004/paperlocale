import Foundation
import CryptoKit

// 只生成 CLI 参数，不包含密钥。服务能力来自 PaperLocale 当前的 Provider 合同：
// 只有 Codex 接收推理档位。官方 OpenAI API 使用固定端点。
struct TranslationOptions {
    var pdf: String
    var runDirectory: String
    var provider: String
    var model: String
    var effort: String
    var baseURL: String
    var outputPDF: String
    var runQA: Bool
    var layoutMode = "paragraph"
    var domain = "atmospheric-science"
    var referencePolicy = "preserve"

    // 每个翻译进程只接收当前服务的密钥。检查和导出无需 API 凭据。
    static func keyEnvironment(_ inherited: [String: String], provider: String,
                               key: String, includeKey: Bool) -> [String: String] {
        var environment = inherited
        environment.removeValue(forKey: "OPENAI_API_KEY")
        environment.removeValue(forKey: "PAPERLOCALE_API_KEY")
        if includeKey && provider != "codex-local" {
            let name = provider == "openai-api" ? "OPENAI_API_KEY" : "PAPERLOCALE_API_KEY"
            environment[name] = key.isEmpty ? inherited[name] : key
        }
        return environment
    }

    var arguments: [String] {
        var args = ["run", pdf, "--run-dir", runDirectory, "--provider", provider,
                    "--model", model, "--layout-mode", layoutMode,
                    "--target-language", "zh-CN", "--domain", domain,
                    "--unattended", "--no-wait-on-error"]
        if referencePolicy != "preserve" { args += ["--reference-policy", referencePolicy] }
        if provider == "codex-local" {
            args += ["--reasoning-effort", effort]
        } else if provider == "openai-api" {
            args += ["--api-key-env", "OPENAI_API_KEY"]
        } else {
            args += ["--base-url", baseURL, "--api-key-env", "PAPERLOCALE_API_KEY"]
        }
        if !outputPDF.isEmpty { args += ["--output-pdf", outputPDF] }
        if !runQA && layoutMode != "legacy" { args += ["--no-qa"] }
        return args
    }
}

// Codex 的本机模型目录提供当前账户可见名称与能力；仅取 CLI 支持的档位，
// 不读取登录材料。没有目录时仅提供 CLI 已有默认模型，其他模型可走兼容 API。
struct CodexModelChoice {
    let name: String
    let efforts: [String]

    static func load() -> [CodexModelChoice] {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent(".codex/models_cache.json")
        let allowed = Set(["none", "low", "medium", "high", "xhigh", "max"])
        if let data = try? Data(contentsOf: path),
           let document = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let models = document["models"] as? [[String: Any]] {
            let choices = models.compactMap { item -> CodexModelChoice? in
                guard let name = item["slug"] as? String, name.hasPrefix("gpt-"),
                      let levels = item["supported_reasoning_levels"] as? [[String: Any]] else { return nil }
                let efforts = levels.compactMap { $0["effort"] as? String }.filter { allowed.contains($0) }
                return efforts.isEmpty ? nil : CodexModelChoice(name: name, efforts: efforts)
            }
            if !choices.isEmpty { return choices }
        }
        return [CodexModelChoice(name: "gpt-6-sol", efforts: ["low", "medium", "high", "xhigh", "max"])]
    }
}

// 后端提供可选动作及诊断，前端不按错误字符串猜修复办法。
struct RepairAction: Decodable, Identifiable {
    let key: String
    let label: String
    var id: String { key }
}
struct SourceIssue: Decodable {
    // 旧断点只有原文/页码。新诊断字段可选，确保更新后仍能打开旧错误报告。
    let id: String?
    let target: String?
    let errors: [String]?
    let validation_source: String?
    let rule_details: [RuleDetail]?
    let anchors: [String: String]?
    let regions: [SourceRegion]?
    let source: String
    let pages: [Int]?
    var pageLabel: String {
        let located = pages ?? regions.map { Array(Set($0.map(\.page))).sorted() } ?? []
        return located.isEmpty ? "页码未定位" : "PDF 第 " + located.map(String.init).joined(separator: ", ") + " 页"
    }
}
struct SourceRegion: Decodable, Equatable {
    let page: Int
    let rect: [Double]
}
struct SourcePageContext: Decodable {
    let page: Int
    let source: String
}
struct RuleDetail: Decodable {
    let code: String
    let title: String
    let message: String
    let guidance: String
}
struct ErrorClassification: Decodable {
    let code: String
    let title: String
    let guidance: String
}
struct RepairReport: Decodable, Identifiable {
    let source_pdf: String?
    let source_sha256: String?
    let classification: ErrorClassification?
    let error_id: String
    let message: String
    let solution: String
    let items: [SourceIssue]
    let page_context: [SourcePageContext]?
    let actions: [RepairAction]
    var id: String { error_id }
}
struct JobProgress: Decodable {
    let stage: String
    let fraction: Double
    let completed: Int
    let total: Int
}

// 与 Python workspaces.run_directory 使用完全相同的 UTF-8 路径摘要，避免同名 PDF 共用缓存。
enum WorkspacePaths {
    static var defaultRoot: String {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("paperlocale").path
    }
    static func runDirectory(pdf: String, root: String) -> URL {
        let source = URL(fileURLWithPath: (pdf as NSString).expandingTildeInPath)
            .standardizedFileURL.resolvingSymlinksInPath()
        let digest = SHA256.hash(data: Data(source.path.utf8)).map { String(format: "%02x", $0) }.joined()
        return URL(fileURLWithPath: (root as NSString).expandingTildeInPath)
            .appendingPathComponent(source.deletingPathExtension().lastPathComponent + "-" + digest.prefix(12))
    }
}
