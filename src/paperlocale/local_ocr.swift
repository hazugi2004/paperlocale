// 只读识别传入的局部 PNG；Vision 在本机运行，不发送论文内容到云端。
// 不启用语言自动纠错，避免专名、变量被系统替换为常见词。
import Foundation
import Vision
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["en-US"]
request.usesLanguageCorrection = false
let handler = VNImageRequestHandler(url: URL(fileURLWithPath: CommandLine.arguments[1]))
try handler.perform([request])
let candidates = (request.results ?? []).compactMap { $0.topCandidates(1).first }
let result: [String: Any] = ["text": candidates.map { $0.string }.joined(separator: " "),
                            "confidence": candidates.map { $0.confidence }.min() ?? 0]
let data = try JSONSerialization.data(withJSONObject: result, options: [.sortedKeys])
print(String(data: data, encoding: .utf8)!)
