// vision-ocr.swift - optional Apple Vision text recognition for Un-DRM.
//
// Peekaboo's `see --ocr` runs Vision in fast mode. This tool runs the accurate
// recognizer on saved page images so undrm_assemble.py --reocr (or
// undrm_capture.py --ocr-engine vision) can get the best text Vision offers.
//
// Build once:   swiftc -O -o vision-ocr tools/vision-ocr.swift
// Run:          ./vision-ocr [--fast] [--languages en-US,de-DE] page.png [more.png ...]
//
// Prints one JSON object per image, one per line:
//   {"image": "...", "width": px, "height": px, "level": "accurate",
//    "observations": [{"text": "...", "confidence": 0.98,
//                      "bbox": {"x": 0.1, "y": 0.2, "width": 0.5, "height": 0.02}}]}
// bbox values are normalized to the image with a top-left origin.

import Foundation
import ImageIO
import Vision

struct Box: Codable {
    let x: Double
    let y: Double
    let width: Double
    let height: Double
}

struct Observation: Codable {
    let text: String
    let confidence: Double
    let bbox: Box
}

struct Output: Codable {
    let image: String
    let width: Int
    let height: Int
    let level: String
    let observations: [Observation]
}

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

var fast = false
var languages: [String] = []
var paths: [String] = []
let arguments = Array(CommandLine.arguments.dropFirst())
var index = 0
while index < arguments.count {
    let argument = arguments[index]
    if argument == "--fast" {
        fast = true
    } else if argument == "--languages" {
        index += 1
        guard index < arguments.count else { fail("--languages needs a value") }
        languages = arguments[index].split(separator: ",").map { String($0) }
    } else if argument == "--help" || argument == "-h" {
        print("usage: vision-ocr [--fast] [--languages a,b] image.png [...]")
        exit(0)
    } else {
        paths.append(argument)
    }
    index += 1
}
if paths.isEmpty {
    fail("usage: vision-ocr [--fast] [--languages a,b] image.png [...]", code: 2)
}

let encoder = JSONEncoder()
for path in paths {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        fail("cannot decode image: \(path)")
    }
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.usesLanguageCorrection = true
    if !languages.isEmpty {
        request.recognitionLanguages = languages
    }
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    do {
        try handler.perform([request])
    } catch {
        fail("Vision failed on \(path): \(error.localizedDescription)")
    }
    let results = request.results ?? []
    let observations: [Observation] = results.compactMap { observation in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let text = candidate.string.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let box = observation.boundingBox  // normalized, bottom-left origin
        return Observation(
            text: text,
            confidence: Double(candidate.confidence),
            bbox: Box(x: box.minX, y: 1.0 - box.maxY, width: box.width, height: box.height))
    }
    let output = Output(
        image: path,
        width: image.width,
        height: image.height,
        level: fast ? "fast" : "accurate",
        observations: observations)
    guard let data = try? encoder.encode(output), let line = String(data: data, encoding: .utf8) else {
        fail("cannot encode result for \(path)")
    }
    print(line)
}
