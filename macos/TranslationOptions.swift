import Foundation

// 只生成 CLI 参数，不包含密钥。服务能力来自 PaperLocale 当前的 Provider 合同：
// Codex 支持推理档位；兼容 API 与 Qwen-MT 不接收 reasoning-effort。
struct TranslationOptions {
    var pdf: String
    var runDirectory: String
    var provider: String
    var model: String
    var effort: String
    var baseURL: String
    var outputPDF: String
    var runQA: Bool

    var arguments: [String] {
        var args = ["run", pdf, "--run-dir", runDirectory, "--provider", provider,
                    "--model", model, "--layout-mode", "paragraph",
                    "--target-language", "zh-CN", "--domain", "atmospheric-science",
                    "--unattended", "--no-wait-on-error"]
        if provider == "codex-local" {
            args += ["--reasoning-effort", effort]
        } else {
            args += ["--base-url", baseURL, "--api-key-env", "PAPERLOCALE_API_KEY"]
        }
        if !outputPDF.isEmpty { args += ["--output-pdf", outputPDF] }
        if !runQA { args += ["--no-qa"] }
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
    let source: String
    let pages: [Int]?
    var pageLabel: String { "PDF 第 " + (pages ?? []).map { String($0) }.joined(separator: ", ") + " 页" }
}
struct SourcePageContext: Decodable {
    let page: Int
    let source: String
}
struct RepairReport: Decodable, Identifiable {
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
