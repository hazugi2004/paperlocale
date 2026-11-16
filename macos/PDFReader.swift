import SwiftUI
import PDFKit
import AppKit

@MainActor func configurePaperPDFView(_ view: PDFView) {
    view.displayMode = .singlePageContinuous
    view.displayDirection = .vertical
    view.displayBox = .cropBox
    view.pageBreakMargins = NSEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
    view.backgroundColor = .underPageBackgroundColor
    view.autoScales = true
}

enum ReadingSide: String, CaseIterable {
    case original = "原文"
    case translation = "译文"
}

enum ReadingArrangement {
    case original, comparison, translation

    func contains(_ side: ReadingSide) -> Bool {
        self == .comparison || (self == .original ? side == .original : side == .translation)
    }
}

// 保版引擎将旋转归入版面坐标，报告矩形以可见 CropBox 左上角为原点。
// PDFKit 跳转用的是原始 PDF 页坐标。先裁到可见区，再反转旋转和平移，
// 避免把非零 CropBox 原点或 90/270 度页误当成普通纵页。
func sourceIssuePageRect(_ region: SourceRegion, crop: CGRect, rotation angle: Int) -> CGRect? {
    let rect = region.rect
    guard rect.count == 4, rect.allSatisfy(\.isFinite), rect[2] > rect[0], rect[3] > rect[1],
          !crop.isNull, !crop.isInfinite, !crop.isEmpty else { return nil }
    let rotation = ((angle % 360) + 360) % 360
    let quarterTurn = rotation == 90 || rotation == 270
    let width = quarterTurn ? crop.height : crop.width
    let height = quarterTurn ? crop.width : crop.height
    let clipped = CGRect(x: rect[0], y: rect[1], width: rect[2] - rect[0], height: rect[3] - rect[1])
        .intersection(CGRect(x: 0, y: 0, width: width, height: height))
    guard !clipped.isNull, !clipped.isEmpty else { return nil }
    let x = clipped.minX, y = clipped.minY, w = clipped.width, h = clipped.height
    let pageRect: CGRect
    switch rotation {
    case 0: pageRect = CGRect(x: x, y: crop.height - y - h, width: w, height: h)
    case 90: pageRect = CGRect(x: y, y: x, width: h, height: w)
    case 180: pageRect = CGRect(x: crop.width - x - w, y: y, width: w, height: h)
    case 270: pageRect = CGRect(x: crop.width - y - h, y: crop.height - x - w, width: h, height: w)
    default: return nil
    }
    return pageRect.offsetBy(dx: crop.minX, dy: crop.minY)
}

@MainActor private func issueRects(_ regions: [SourceRegion], on page: PDFPage) -> [CGRect] {
    // CGPDFPage 返回未旋转的物理页面框；有些 PDF 的裁切框比纸页还大。
    let crop = page.pageRef.map { $0.getBoxRect(.cropBox).intersection($0.getBoxRect(.mediaBox)) }
        ?? page.bounds(for: .cropBox)
    return regions.compactMap { sourceIssuePageRect($0, crop: crop, rotation: page.rotation) }
}

@MainActor private final class SourceIssueOverlay: NSView {
    weak var pdfView: PDFView?
    weak var page: PDFPage?
    var rects: [CGRect] = [] { didSet { needsDisplay = true } }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }

    override func draw(_ dirtyRect: NSRect) {
        guard let pdfView, let page else { return }
        NSColor(srgbRed: 1, green: 0.94, blue: 0, alpha: 0.41).setFill()
        for rect in rects {
            let visible = convert(pdfView.convert(rect, from: page), from: pdfView).intersection(dirtyRect)
            if !visible.isNull && !visible.isEmpty { NSBezierPath(rect: visible).fill() }
        }
    }
}

// PDFKit 为可见页管理覆盖层。高亮与搜索选区独立，既不添加 PDF 注释，也不拦截点击或选字。
@MainActor private final class SourceIssueOverlays: NSObject, PDFPageOverlayViewProvider {
    private var regions: [SourceRegion] = []
    private var byPage: [Int: [SourceRegion]] = [:]
    private var overlays: [PDFPage: SourceIssueOverlay] = [:]

    func setRegions(_ newRegions: [SourceRegion], in view: PDFView) {
        guard regions != newRegions else { return }
        regions = newRegions
        byPage = Dictionary(grouping: newRegions, by: \.page)
        for (page, overlay) in overlays { refresh(overlay, for: page, in: view) }
    }

    func reset(in view: PDFView) {
        setRegions([], in: view)
        // 重新载入 PDF 时只保留新页。旧文档即使尚未来得及发出隐藏回调也可释放。
        overlays.removeAll()
    }

    private func refresh(_ overlay: SourceIssueOverlay, for page: PDFPage, in view: PDFView) {
        guard page.document === view.document, let index = page.document?.index(for: page), index != NSNotFound else {
            overlay.rects = []
            return
        }
        overlay.rects = issueRects(byPage[index + 1] ?? [], on: page)
    }

    nonisolated func pdfView(_ view: PDFView, overlayViewFor page: PDFPage) -> NSView? {
        // 协议来自 ObjC，签名未声明主线程。PDFKit 在布局可见的 NSView 时发出回调。
        MainActor.assumeIsolated {
            let overlay = SourceIssueOverlay(frame: .zero)
            overlay.pdfView = view
            overlay.page = page
            overlay.setAccessibilityElement(false)
            overlays[page] = overlay
            refresh(overlay, for: page, in: view)
            return overlay
        }
    }

    nonisolated func pdfView(_ view: PDFView, willEndDisplayingOverlayView overlayView: NSView, for page: PDFPage) {
        MainActor.assumeIsolated {
            if overlays[page] === overlayView { overlays.removeValue(forKey: page) }
        }
    }
}

final class ReadingPDFView: PDFView {
    private let issueOverlays = SourceIssueOverlays()
    var didLayout: (() -> Void)?

    func enableSourceIssueMarks() { pageOverlayViewProvider = issueOverlays }

    func setSourceIssues(_ regions: [SourceRegion]) {
        issueOverlays.setRegions(regions, in: self)
    }

    func resetSourceIssues() { issueOverlays.reset(in: self) }

    func locateSourceIssue(page number: Int, regions: [SourceRegion]) {
        guard number >= 1, let document, number <= document.pageCount,
              let page = document.page(at: number - 1) else { return }
        if currentPage !== page { go(to: page) }
        if let rect = issueRects(regions.filter { $0.page == number }, on: page).first { go(to: rect, on: page) }
    }

    override func layout() {
        super.layout()
        didLayout?()
    }
}

struct SourcePDFPreview: NSViewRepresentable {
    let path: String
    let page: Int
    let regions: [SourceRegion]
    var focus = 0

    final class Coordinator {
        var page: Int?
        var regions: [SourceRegion] = []
        var focus: Int?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> ReadingPDFView {
        let view = ReadingPDFView()
        configurePaperPDFView(view)
        view.enableSourceIssueMarks()
        return view
    }

    func updateNSView(_ view: ReadingPDFView, context: Context) {
        let url = URL(fileURLWithPath: path)
        let changedDocument = view.document?.documentURL != url
        if changedDocument {
            view.resetSourceIssues()
            view.document = PDFDocument(url: url)
        }
        view.setSourceIssues(regions)
        let previous = context.coordinator
        if changedDocument || previous.page != page || previous.regions != regions || previous.focus != focus {
            previous.page = page
            previous.regions = regions
            previous.focus = focus
            view.locateSourceIssue(page: page, regions: regions)
        }
    }
}

struct ReaderPDFPane: NSViewRepresentable {
    let view: ReadingPDFView

    func makeNSView(context: Context) -> ReadingPDFView { view }
    func updateNSView(_ nsView: ReadingPDFView, context: Context) {}
}

@MainActor final class PDFReader: ObservableObject {
    let original = ReadingPDFView()
    let translation = ReadingPDFView()

    @Published private(set) var active = ReadingSide.original
    @Published private(set) var page = 1
    @Published private(set) var totalPages = 0
    @Published private(set) var zoomPercent = 100
    @Published private(set) var canLink = false
    @Published var linked = true {
        didSet { if linked { syncFromActive() } }
    }
    @Published private(set) var searchCount = 0
    @Published private(set) var searchIndex = 0
    @Published private(set) var finding = false

    private var arrangement = ReadingArrangement.original
    private var scrollObservers: [ReadingSide: NSObjectProtocol] = [:]
    private var clips: [ReadingSide: NSClipView] = [:]
    private var layoutSizes: [ReadingSide: CGSize] = [:]
    private var scaleObservers: [NSObjectProtocol] = []
    private var searchObservers: [NSObjectProtocol] = []
    private var mouseMonitor: Any?
    private var changingViews = false
    private var relativeZoom: [ReadingSide: CGFloat] = [.original: 1, .translation: 1]
    private var matches: [PDFSelection] = []
    private var searchDocument: PDFDocument?
    private var searchRequest = UUID()

    init() {
        original.enableSourceIssueMarks()
        for side in ReadingSide.allCases {
            let pdf = view(side)
            configurePaperPDFView(pdf)
            pdf.didLayout = { [weak self] in self?.viewDidLayout(side) }
            let token = NotificationCenter.default.addObserver(forName: .PDFViewScaleChanged,
                    object: pdf, queue: nil) { [weak self] _ in
                // PDFView 的缩放与 NSClipView 的位置通知来自同一个主线程。
                MainActor.assumeIsolated { self?.scaleChanged(side) }
            }
            scaleObservers.append(token)
        }
    }

    deinit {
        for token in scaleObservers + Array(scrollObservers.values) + searchObservers {
            NotificationCenter.default.removeObserver(token)
        }
        if let mouseMonitor { NSEvent.removeMonitor(mouseMonitor) }
    }

    func view(_ side: ReadingSide) -> ReadingPDFView { side == .original ? original : translation }

    func setSourceIssues(_ regions: [SourceRegion]) { original.setSourceIssues(regions) }

    func locateSourceIssue(page: Int, regions: [SourceRegion]) {
        activate(.original)
        withViewChanges {
            original.locateSourceIssue(page: page, regions: regions)
            if arrangement == .comparison && linked && canLink { go(to: page, in: .translation) }
        }
        updateSummary()
        syncFromActive()
    }

    func load(source: PDFDocument?, candidate: PDFDocument?) {
        let sourceChanged = original.document !== source
        let candidateChanged = translation.document !== candidate
        guard sourceChanged || candidateChanged else { return }
        let oldPage = page
        cancelSearch()
        changingViews = true
        if sourceChanged {
            original.resetSourceIssues()
            original.document = source
            relativeZoom[.original] = 1
            original.autoScales = true
        }
        if candidateChanged {
            translation.document = candidate
            relativeZoom[.translation] = 1
            translation.autoScales = true
        }
        changingViews = false
        let matchedPages = source != nil && candidate != nil && source?.pageCount == candidate?.pageCount
        if canLink != matchedPages { canLink = matchedPages }
        if sourceChanged { active = .original }
        if candidate == nil && active == .translation { active = .original }
        let destination = sourceChanged ? 1 : oldPage
        if sourceChanged || active == .original { updateSummary() }
        if candidateChanged, candidate != nil {
            withViewChanges { go(to: destination, in: .translation) }
        }
        if sourceChanged { goToPage(destination, alignBoth: true) }
        syncFromActive()
    }

    func startWatchingInput() {
        guard mouseMonitor == nil else { return }
        mouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .scrollWheel, .magnify]) {
            [weak self] event in
            MainActor.assumeIsolated { self?.selectInputPane(event) }
            return event
        }
    }

    func stopWatchingInput() {
        if let mouseMonitor { NSEvent.removeMonitor(mouseMonitor) }
        mouseMonitor = nil
    }

    private func selectInputPane(_ event: NSEvent) {
        for side in ReadingSide.allCases where arrangement.contains(side) {
            let pdf = view(side)
            guard let window = pdf.window, window === event.window else { continue }
            if pdf.bounds.contains(pdf.convert(event.locationInWindow, from: nil)) {
                activate(side)
                return
            }
        }
    }

    private func activate(_ side: ReadingSide) {
        guard active != side else { return }
        cancelSearch()
        active = side
        updateSummary()
    }

    func present(_ newArrangement: ReadingArrangement) {
        guard arrangement != newArrangement else { return }
        let retainedPage = page
        let oldSide = active
        arrangement = newArrangement
        if newArrangement == .original { activate(.original) }
        if newArrangement == .translation { activate(.translation) }
        if oldSide != active {
            // 单栏切换保留物理页码，即使两个阅读栏先前解除联动。
            DispatchQueue.main.async { [weak self] in
                guard let self, self.arrangement == newArrangement else { return }
                self.goToPage(retainedPage)
            }
        } else { syncFromActive() }
    }

    func goToPage(_ requested: Int, alignBoth: Bool = false) {
        withViewChanges {
            go(to: requested, in: active)
            if alignBoth || (arrangement == .comparison && linked && canLink) {
                go(to: requested, in: active == .original ? .translation : .original)
            }
        }
        updateSummary()
        if !alignBoth { syncFromActive() }
    }

    private func go(to requested: Int, in side: ReadingSide) {
        let pdf = view(side)
        guard let doc = pdf.document, doc.pageCount > 0,
              let page = doc.page(at: min(max(requested, 1), doc.pageCount) - 1) else { return }
        pdf.go(to: page)
    }

    func zoom(by factor: CGFloat) {
        let next = (relativeZoom[active] ?? 1) * factor
        setZoom(next)
    }

    func fitWidth() { setZoom(1) }

    private func setZoom(_ ratio: CGFloat) {
        let zoom = min(max(ratio, 0.4), 5)
        relativeZoom[active] = zoom
        withViewChanges { applyZoom(zoom, in: active) }
        updateSummary()
        syncFromActive()
    }

    private func applyZoom(_ ratio: CGFloat, in side: ReadingSide) {
        let pdf = view(side)
        guard pdf.document != nil else { return }
        if abs(ratio - 1) < 0.002 {
            if !pdf.autoScales { pdf.autoScales = true }
            return
        }
        let fitted = pdf.scaleFactorForSizeToFit
        guard fitted.isFinite, fitted > 0.01 else { return }
        let scale = min(max(ratio * fitted, 0.05), 12)
        if abs(pdf.scaleFactor - scale) > 0.002 {
            if pdf.autoScales {
                pdf.minScaleFactor = 0.05
                pdf.maxScaleFactor = 12
            }
            pdf.scaleFactor = scale
        }
    }

    private func scaleChanged(_ side: ReadingSide) {
        guard !changingViews, side == active, arrangement.contains(side) else { return }
        let pdf = view(side)
        let fitted = pdf.scaleFactorForSizeToFit
        guard fitted.isFinite, fitted > 0.01 else { return }
        relativeZoom[side] = pdf.autoScales ? 1 : pdf.scaleFactor / fitted
        updateSummary()
        syncFromActive()
    }

    private func updateSummary() {
        let pdf = view(active)
        let pages = pdf.document?.pageCount ?? 0
        let index = visiblePage(in: active).flatMap { pdf.document?.index(for: $0) }
        let shown = index.flatMap { (0..<pages).contains($0) ? $0 + 1 : nil } ?? 1
        let zoom = relativeZoom[active] ?? 1
        let percent = zoom.isFinite ? Int((zoom * 100).rounded()) : 100
        if shown != page { page = shown }
        if totalPages != pages { totalPages = pages }
        if zoomPercent != percent { zoomPercent = percent }
    }

    private func viewportCenter(in side: ReadingSide) -> CGPoint? {
        guard let clip = clips[side], !clip.bounds.isEmpty else { return nil }
        let center = CGPoint(x: clip.bounds.midX, y: clip.bounds.midY)
        return view(side).convert(center, from: clip)
    }

    private func visiblePage(in side: ReadingSide) -> PDFPage? {
        let pdf = view(side)
        // 滚动事件到达时，PDFKit 的 currentPage 可能仍停留在上一次跳页。
        // 用实际视口中心所在页同步，即使一次触控板滚动跨过多个物理页也能找到正确目标。
        return viewportCenter(in: side).flatMap { pdf.page(for: $0, nearest: true) } ?? pdf.currentPage
    }

    private func viewDidLayout(_ side: ReadingSide) {
        let pdf = view(side)
        if let clip = pdf.documentView?.enclosingScrollView?.contentView, clips[side] !== clip {
            if let old = scrollObservers[side] { NotificationCenter.default.removeObserver(old) }
            clips[side] = clip
            clip.postsBoundsChangedNotifications = true
            scrollObservers[side] = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                        object: clip, queue: nil) { [weak self] _ in
                MainActor.assumeIsolated { self?.scrollChanged(side) }
            }
        }
        let size = pdf.bounds.size
        guard size != layoutSizes[side], size.width > 1, size.height > 1 else { return }
        layoutSizes[side] = size
        // 等 PDFKit 用新的视口宽度完成排版后再恢复相对缩放，避开 SwiftUI 布局期间的状态写入。
        DispatchQueue.main.async { [weak self] in
            guard let self, self.arrangement.contains(side) else { return }
            self.withViewChanges { self.applyZoom(self.relativeZoom[side] ?? 1, in: side) }
            self.updateSummary()
            self.syncFromActive()
        }
    }

    private func scrollChanged(_ side: ReadingSide) {
        guard !changingViews, side == active, arrangement.contains(side) else { return }
        updateSummary()
        syncFromActive()
    }

    private func withViewChanges(_ changes: () -> Void) {
        let previous = changingViews
        changingViews = true
        changes()
        changingViews = previous
    }

    private func syncFromActive() {
        guard !changingViews, arrangement == .comparison, linked, canLink else { return }
        let other: ReadingSide = active == .original ? .translation : .original
        let reader = view(active)
        let follower = view(other)
        guard let left = reader.document, let right = follower.document, let from = visiblePage(in: active) else { return }
        let index = left.index(for: from)
        guard (0..<right.pageCount).contains(index), let to = right.page(at: index) else { return }

        withViewChanges {
            let fromBox = from.bounds(for: .cropBox)
            let toBox = to.bounds(for: .cropBox)
            // 正常页以视口中心占裁切框的比例联动。旋转、不同页框或 PDFKit 未提供滚动视口时只对齐页码。
            guard from.rotation == 0, to.rotation == 0, fromBox.width > 0, fromBox.height > 0,
                  abs(fromBox.width - toBox.width) < 0.25, abs(fromBox.height - toBox.height) < 0.25,
                  abs(fromBox.minX - toBox.minX) < 0.25, abs(fromBox.minY - toBox.minY) < 0.25,
                  let center = viewportCenter(in: active), let toClip = clips[other] else {
                if visiblePage(in: other) !== to { follower.go(to: to) }
                return
            }
            let ratio = relativeZoom[active] ?? 1
            relativeZoom[other] = ratio
            applyZoom(ratio, in: other)
            let point = reader.convert(center, to: from)
            let x = min(max((point.x - fromBox.minX) / fromBox.width, 0), 1)
            let y = min(max((point.y - fromBox.minY) / fromBox.height, 0), 1)
            let projected = CGPoint(x: toBox.minX + x * toBox.width, y: toBox.minY + y * toBox.height)
            let next = toClip.convert(follower.convert(projected, from: to), from: follower)
            let targetBounds = toClip.bounds
            guard next.x.isFinite, next.y.isFinite,
                  abs(next.x - targetBounds.midX) > 0.75 || abs(next.y - targetBounds.midY) > 0.75 else { return }
            var bounds = targetBounds
            bounds.origin.x += next.x - targetBounds.midX
            bounds.origin.y += next.y - targetBounds.midY
            toClip.scroll(to: toClip.constrainBoundsRect(bounds).origin)
            toClip.enclosingScrollView?.reflectScrolledClipView(toClip)
        }
    }

    func search(_ query: String) {
        cancelSearch()
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let document = view(active).document else { return }
        let request = searchRequest
        searchDocument = document
        finding = true
        let center = NotificationCenter.default
        searchObservers = [
            center.addObserver(forName: .PDFDocumentDidFindMatch, object: document, queue: .main) {
                [weak self] event in
                MainActor.assumeIsolated {
                    guard let self, self.searchRequest == request,
                          let match = event.userInfo?["PDFDocumentFoundSelection"] as? PDFSelection else { return }
                    self.matches.append(match)
                }
            },
            center.addObserver(forName: .PDFDocumentDidEndFind, object: document, queue: .main) {
                [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self, self.searchRequest == request else { return }
                    self.finding = false
                    self.searchCount = self.matches.count
                    if !self.matches.isEmpty { self.showMatch(at: 0) }
                }
            }
        ]
        document.beginFindString(text, withOptions: [.caseInsensitive])
    }

    func navigateMatch(_ direction: Int) {
        guard !matches.isEmpty else { return }
        showMatch(at: (searchIndex - 1 + direction + matches.count) % matches.count)
    }

    private func showMatch(at index: Int) {
        let pdf = view(active)
        let selection = matches[index]
        searchIndex = index + 1
        withViewChanges {
            pdf.setCurrentSelection(selection, animate: false)
            pdf.go(to: selection)
        }
        updateSummary()
        syncFromActive()
    }

    func cancelSearch() {
        searchRequest = UUID()
        for token in searchObservers { NotificationCenter.default.removeObserver(token) }
        searchObservers.removeAll()
        searchDocument?.cancelFindString()
        searchDocument = nil
        matches.removeAll()
        if finding { finding = false }
        if searchIndex != 0 { searchIndex = 0 }
        if searchCount != 0 { searchCount = 0 }
    }
}
