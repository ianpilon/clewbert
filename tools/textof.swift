// textof FILE — prints the text in an image (Apple Vision text recognition, on device) or a PDF (its text layer;
// pages with no text layer are rendered and recognized like an image). Used by server.py to index boards.
// Build: swiftc -O tools/textof.swift -o bin/textof
import Foundation
import Vision
import PDFKit
import AppKit

func recognize(_ image: CGImage) -> String {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    req.usesLanguageCorrection = true
    try? VNImageRequestHandler(cgImage: image, options: [:]).perform([req])
    return (req.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
}

func render(_ page: PDFPage) -> CGImage? {
    let box = page.bounds(for: .mediaBox)
    let scale = min(2.0, 2000 / max(box.width, box.height))
    let w = Int(box.width * scale), h = Int(box.height * scale)
    guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8, bytesPerRow: 0,
                              space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return nil }
    ctx.setFillColor(CGColor(gray: 1, alpha: 1))
    ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
    ctx.scaleBy(x: scale, y: scale)
    page.draw(with: .mediaBox, to: ctx)
    return ctx.makeImage()
}

let path = CommandLine.arguments[1]
let url = URL(fileURLWithPath: path)
var out = ""
if path.lowercased().hasSuffix(".pdf"), let doc = PDFDocument(url: url) {
    for i in 0..<min(doc.pageCount, 60) {
        guard let page = doc.page(at: i) else { continue }
        let t = (page.string ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        out += (t.count > 20 ? t : (render(page).map(recognize) ?? "")) + "\n\n"
    }
} else if let img = NSImage(contentsOf: url), let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) {
    out = recognize(cg)
}
print(out.trimmingCharacters(in: .whitespacesAndNewlines))
