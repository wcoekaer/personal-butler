// JarvisContext: snapshots the user's calendar, reminders, Mail unread count and watched-sender messages
// to ~/Library/Caches/jarvis/context.json for the Jarvis daemon.
//
// It runs as a small app bundle in the user's login session (LaunchAgent,
// every few minutes) because macOS only grants Calendar/Reminders access to
// bundled apps that can show a permission prompt; the boot-time daemon can't.

import AppKit
import EventKit
import Foundation

let store = EKEventStore()
let iso = ISO8601DateFormatter()
iso.formatOptions = [.withInternetDateTime]
iso.timeZone = TimeZone.current

func requestAccess(_ type: EKEntityType) -> Bool {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    let done: (Bool, Error?) -> Void = { ok, _ in granted = ok; sem.signal() }
    if type == .event {
        store.requestFullAccessToEvents(completion: done)
    } else {
        store.requestFullAccessToReminders(completion: done)
    }
    // Pump the run loop so a first-run permission prompt can be shown.
    while sem.wait(timeout: .now() + 0.1) == .timedOut {
        RunLoop.current.run(until: Date().addingTimeInterval(0.1))
    }
    return granted
}

func events() -> [[String: Any]]? {
    guard requestAccess(.event) else { return nil }
    let cal = Calendar.current
    let now = Date()
    let end = cal.date(byAdding: .day, value: 2, to: cal.startOfDay(for: now))!  // through tomorrow
    let pred = store.predicateForEvents(withStart: cal.startOfDay(for: now), end: end, calendars: nil)
    return store.events(matching: pred)
        .filter { ev in
            // Skip invitations the user declined.
            let me = ev.attendees?.first(where: { $0.isCurrentUser })
            return me?.participantStatus != .declined
        }
        .sorted { $0.startDate < $1.startDate }
        .map { ev in
            var d: [String: Any] = [
                "title": ev.title ?? "(untitled)",
                "start": iso.string(from: ev.startDate),
                "end": iso.string(from: ev.endDate),
                "all_day": ev.isAllDay,
                "calendar": ev.calendar.title,
            ]
            if let loc = ev.location, !loc.isEmpty { d["location"] = loc }
            return d
        }
}

func reminders() -> [[String: Any]]? {
    guard requestAccess(.reminder) else { return nil }
    let cal = Calendar.current
    let endOfToday = cal.date(byAdding: .day, value: 1, to: cal.startOfDay(for: Date()))!
    let pred = store.predicateForIncompleteReminders(withDueDateStarting: nil, ending: endOfToday, calendars: nil)
    let sem = DispatchSemaphore(value: 0)
    var out: [[String: Any]] = []
    store.fetchReminders(matching: pred) { items in
        for r in items ?? [] {
            var d: [String: Any] = ["title": r.title ?? "(untitled)", "list": r.calendar.title]
            if let comps = r.dueDateComponents, let due = cal.date(from: comps) {
                d["due"] = iso.string(from: due)
                d["overdue"] = due < cal.startOfDay(for: Date())
            }
            out.append(d)
        }
        sem.signal()
    }
    sem.wait()
    return out
}

func mailUnread() -> Int? {
    let running = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.apple.mail" }
    guard running else { return nil }
    var err: NSDictionary?
    let script = NSAppleScript(source: "tell application \"Mail\" to return unread count of inbox")
    let result = script?.executeAndReturnError(&err)
    if err != nil { return nil }
    return result.map { Int($0.int32Value) }
}

let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Caches/jarvis")

// Messages (inbox and Trash by default) from the senders Jarvis watches (mail-watch.json, written by the
// daemon from feeds.mail_watch): id, subject, sender, received time and a short
// excerpt. Nothing else from Mail is read.
func mailWatch() -> [[String: Any]]? {
    guard let raw = try? Data(contentsOf: dir.appendingPathComponent("mail-watch.json")),
          let cfg = (try? JSONSerialization.jsonObject(with: raw)) as? [String: Any],
          let senders = cfg["senders"] as? [String], !senders.isEmpty else { return nil }
    let running = NSWorkspace.shared.runningApplications.contains { $0.bundleIdentifier == "com.apple.mail" }
    guard running else { return nil }
    let days = (cfg["days"] as? Int) ?? 3
    let chars = (cfg["excerpt_chars"] as? Int) ?? 1500
    let quoted = senders.map { "\"" + $0.replacingOccurrences(of: "\\", with: "").replacingOccurrences(of: "\"", with: "") + "\"" }
    // which mailboxes to look in: inbox and Trash by default (alerts are often read and deleted quickly)
    let names: [String: String] = ["inbox": "inbox", "trash": "trash mailbox", "junk": "junk mailbox"]
    let boxes = ((cfg["mailboxes"] as? [String]) ?? ["inbox", "trash"]).compactMap { names[$0.lowercased()] }
    let source = """
    tell application "Mail"
        set out to {}
        set cutoff to (current date) - (\(days) * days)
        repeat with b in {\((boxes.isEmpty ? ["inbox"] : boxes).joined(separator: ", "))}
            repeat with addr in {\(quoted.joined(separator: ", "))}
                set ms to (messages of b whose sender contains (addr as text) and date received > cutoff)
                repeat with m in ms
                    set c to ""
                    try
                        set c to content of m
                        if (length of c) > \(chars) then set c to text 1 thru \(chars) of c
                    end try
                    set end of out to {message id of m, subject of m, sender of m, ((date received of m) as «class isot» as string), c}
                end repeat
            end repeat
        end repeat
        return out
    end tell
    """
    var err: NSDictionary?
    guard let result = NSAppleScript(source: source)?.executeAndReturnError(&err), err == nil else {
        FileHandle.standardError.write(("mail watch: AppleScript error \(err ?? [:])\n").data(using: .utf8)!)
        return nil
    }
    var out: [[String: Any]] = []
    let n = result.numberOfItems
    if n > 0 {
        for i in 1...n {
            guard let item = result.atIndex(i), item.numberOfItems >= 5 else { continue }
            out.append(["id": item.atIndex(1)?.stringValue ?? "", "subject": item.atIndex(2)?.stringValue ?? "",
                        "sender": item.atIndex(3)?.stringValue ?? "", "received": item.atIndex(4)?.stringValue ?? "",
                        "excerpt": item.atIndex(5)?.stringValue ?? ""])
        }
    }
    return out
}

var snapshot: [String: Any] = ["generated": iso.string(from: Date())]
if let e = events() { snapshot["events"] = e } else { snapshot["calendar_access"] = "denied" }
if let r = reminders() { snapshot["reminders"] = r } else { snapshot["reminders_access"] = "denied" }
if let m = mailUnread() { snapshot["mail_unread_inbox"] = m }
if let w = mailWatch() { snapshot["mail_watch"] = w }

try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
let data = try JSONSerialization.data(withJSONObject: snapshot, options: [.prettyPrinted, .sortedKeys])
try data.write(to: dir.appendingPathComponent("context.json"), options: .atomic)
print(String(data: data, encoding: .utf8)!)
