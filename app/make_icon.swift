// Рисует значок приложения 1024×1024: тёплый градиент и белая стрелка вниз.
// swift app/make_icon.swift <out.png>
import AppKit

let out = CommandLine.arguments.dropFirst().first ?? "icon.png"
let size = 1024.0
let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size),
                           bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                           colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// подложка со скруглением, как у системных значков macOS
let inset = size * 0.09
let rect = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let shape = NSBezierPath(roundedRect: rect, xRadius: rect.width * 0.225, yRadius: rect.width * 0.225)
NSGradient(colors: [NSColor(calibratedRed: 1.0, green: 0.55, blue: 0.20, alpha: 1),
                    NSColor(calibratedRed: 0.93, green: 0.22, blue: 0.36, alpha: 1)])!
    .draw(in: shape, angle: -90)

// стрелка вниз (SF Symbol)
let cfg = NSImage.SymbolConfiguration(pointSize: size * 0.46, weight: .bold)
if let sym = NSImage(systemSymbolName: "arrow.down", accessibilityDescription: nil)?.withSymbolConfiguration(cfg) {
    let tinted = NSImage(size: sym.size, flipped: false) { r in
        sym.draw(in: r)
        NSColor.white.set()
        r.fill(using: .sourceAtop)
        return true
    }
    let w = tinted.size.width, h = tinted.size.height
    tinted.draw(in: NSRect(x: (size - w) / 2, y: (size - h) / 2 + size * 0.03, width: w, height: h))
}
// полоска «загрузки» под стрелкой
NSColor.white.withAlphaComponent(0.95).setFill()
NSBezierPath(roundedRect: NSRect(x: size * 0.30, y: size * 0.20, width: size * 0.40, height: size * 0.055),
             xRadius: size * 0.0275, yRadius: size * 0.0275).fill()

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
