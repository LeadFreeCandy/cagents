// Run macOS clipboard QA while preserving every original pasteboard format.
// Usage: swift preserve_clipboard.swift /absolute/path/to/python -m pytest ...
import AppKit
import Foundation
import Darwin

func runQA() -> Int32 {
    guard CommandLine.arguments.count > 1 else { return 2 }
    let board = NSPasteboard.general
    let saved = (board.pasteboardItems ?? []).map { item in
        item.types.compactMap { type -> (NSPasteboard.PasteboardType, Data)? in
            guard let data = item.data(forType: type) else { return nil }
            return (type, data)
        }
    }
    defer {
        board.clearContents()
        let restored = saved.map { entry -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for (type, data) in entry { item.setData(data, forType: type) }
            return item
        }
        if !restored.isEmpty { board.writeObjects(restored) }
        print("Restored \(saved.count) original clipboard item(s), including all formats.")
    }
    let process = Process()
    process.executableURL = URL(fileURLWithPath: CommandLine.arguments[1])
    process.arguments = Array(CommandLine.arguments.dropFirst(2))
    do {
        try process.run()
        process.waitUntilExit()
        return process.terminationStatus
    } catch {
        fputs("Could not run clipboard QA: \(error)\n", stderr)
        return 1
    }
}

exit(runQA())
