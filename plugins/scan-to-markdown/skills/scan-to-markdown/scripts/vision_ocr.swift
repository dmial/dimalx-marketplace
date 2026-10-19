// Apple Vision text recognition for page images: one JSON file per image.
// Usage: vision_ocr <image> <out.json> [<image> <out.json> ...]
// Language correction is off on purpose: we want raw characters, not "fixed" words.
import Foundation
import ImageIO
import Vision

func recognize(imagePath: String, outPath: String) throws {
    let url = URL(fileURLWithPath: imagePath)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "vision_ocr", code: 1, userInfo: [NSLocalizedDescriptionKey: "cannot load \(imagePath)"])
    }

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = ["ru-RU", "en-US"]
    request.minimumTextHeight = 0.003  // small grey print is ~0.6% of page height
    try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])

    let width = Double(image.width)
    let height = Double(image.height)
    var observations: [[String: Any]] = []
    for observation in request.results ?? [] {
        let candidates = observation.topCandidates(3)
        guard let best = candidates.first else { continue }
        let box = observation.boundingBox  // normalized, origin at bottom-left
        observations.append([
            "text": best.string,
            "confidence": (Double(best.confidence) * 1000).rounded() / 1000,
            "alternatives": candidates.dropFirst().map { $0.string },
            "bbox": [
                (box.minX * width).rounded(),
                ((1 - box.maxY) * height).rounded(),
                (box.width * width).rounded(),
                (box.height * height).rounded(),
            ],
        ])
    }

    let payload: [String: Any] = [
        "engine": "apple-vision",
        "revision": request.revision,
        "languages": request.recognitionLanguages,
        "language_correction": request.usesLanguageCorrection,
        "width": image.width,
        "height": image.height,
        "observations": observations,
    ]
    let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
    let out = URL(fileURLWithPath: outPath)
    try FileManager.default.createDirectory(at: out.deletingLastPathComponent(), withIntermediateDirectories: true)
    try data.write(to: out)
}

let args = Array(CommandLine.arguments.dropFirst())
guard !args.isEmpty, args.count % 2 == 0 else {
    FileHandle.standardError.write("usage: vision_ocr <image> <out.json> [<image> <out.json> ...]\n".data(using: .utf8)!)
    exit(2)
}
for i in stride(from: 0, to: args.count, by: 2) {
    do {
        try recognize(imagePath: args[i], outPath: args[i + 1])
    } catch {
        FileHandle.standardError.write("\(args[i]): \(error)\n".data(using: .utf8)!)
        exit(1)
    }
}
