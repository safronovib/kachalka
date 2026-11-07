// Качалка — окно со списком загрузок (этап 4).
// Говорит с фоновой программой по тому же Unix-сокету, что и CLI: строка JSON туда, строка JSON обратно.
// Сборка: app/build.sh → build/Kachalka.app

import AppKit
import SwiftUI
import UniformTypeIdentifiers

// MARK: - Связь с фоновой программой

enum DaemonError: LocalizedError {
    case notRunning, message(String), bad
    var errorDescription: String? {
        switch self {
        case .notRunning: return "Фоновая программа не запущена"
        case .message(let m): return m
        case .bad: return "Непонятный ответ фоновой программы"
        }
    }
}

enum Daemon {
    static var home: String {
        ProcessInfo.processInfo.environment["KACHALKA_HOME"]
            ?? NSHomeDirectory() + "/Library/Application Support/Kachalka"
    }
    static var socketPath: String { home + "/kachalka.sock" }

    /// Синхронный вызов; звать не из главного потока.
    static func call(_ req: [String: Any], timeout: Int = 15) throws -> Data {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw DaemonError.notRunning }
        defer { close(fd) }
        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(socketPath.utf8CString)
        guard bytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else { throw DaemonError.notRunning }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (i, b) in bytes.enumerated() { buf[i] = UInt8(bitPattern: b) }
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard ok == 0 else { throw DaemonError.notRunning }
        var tv = timeval(tv_sec: timeout, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var out = try JSONSerialization.data(withJSONObject: req)
        out.append(10)
        try out.withUnsafeBytes { (p: UnsafeRawBufferPointer) in
            var sent = 0
            while sent < p.count {
                let n = write(fd, p.baseAddress! + sent, p.count - sent)
                if n <= 0 { throw DaemonError.bad }
                sent += n
            }
        }
        var buf = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while !buf.ends(with: 10) {
            let n = read(fd, &chunk, chunk.count)
            if n <= 0 { break }
            buf.append(chunk, count: n)
        }
        guard let obj = try JSONSerialization.jsonObject(with: buf) as? [String: Any] else { throw DaemonError.bad }
        if obj["ok"] as? Bool != true { throw DaemonError.message(obj["error"] as? String ?? "ошибка") }
        return buf
    }

    static func decode<T: Decodable>(_ type: T.Type, _ req: [String: Any]) throws -> T {
        try JSONDecoder().decode(T.self, from: call(req))
    }

    static func kickstart() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["kickstart", "gui/\(getuid())/com.kachalka.daemon"]
        try? p.run()
        p.waitUntilExit()
    }
}

private extension Data {
    func ends(with byte: UInt8) -> Bool { last == byte }
}

// MARK: - Данные

struct Torrent: Decodable, Identifiable, Equatable {
    let id: String
    let hash: String
    let name: String
    let state: String
    let paused: Bool
    let progress: Double
    let size: Int64
    let done: Int64
    let down_rate: Int64
    let up_rate: Int64
    let eta_seconds: Int?
    let peers: Int
    let seeds: Int
    let ratio: Double
    let save_path: String
    let error: String
    let follow: Bool?
    let selected_files: Int?

    var isDownloading: Bool { ["качается", "ищу метаданные", "проверка", "выделяю место", "в очереди"].contains(state) }
    var isSeeding: Bool { state == "раздаётся" || state == "скачано" }
}

struct ListResp: Decodable { let torrents: [Torrent] }

struct FileItem: Decodable, Identifiable {
    let index: Int
    let path: String
    let size: Int64
    let progress: Double
    let selected: Bool
    let season: Int?
    let episode: Int?
    var id: Int { index }
    var name: String { (path as NSString).lastPathComponent }
}

struct FilesResp: Decodable {
    let id: String
    let name: String
    let save_path: String?
    let files: [FileItem]
    let note: String?
}

struct WatchFile: Decodable {
    let index: Int
    let state: String
    let watched_share: Int
    let trashed: Bool
}

struct Current: Decodable {
    let season: Int?
    let episode: Int?
    let done: Bool?
}

struct FollowInfo: Decodable {
    let ahead: Int
    let keep_hours: Double
    let cleanup: Int
}

struct WatchTorrent: Decodable {
    let id: String
    let files: [WatchFile]
    let follow: FollowInfo?
    let current: Current?
}

struct WatchResp: Decodable { let torrents: [WatchTorrent] }

struct Info: Decodable {
    let down_rate: Int64
    let up_rate: Int64
    let dht_nodes: Int
    let active_interface: String?
    let bind_interface: String?
    let download_dir: String
}

struct Candidate: Decodable, Identifiable {
    let id: String
    let name: String
    let size: Int64
    let reason: String
}

struct CleanupResp: Decodable { let candidates: [Candidate]? }

// MARK: - Форматирование (как в CLI)

enum Fmt {
    static func size(_ b: Int64) -> String {
        let v = Double(b)
        for (unit, k) in [("ГБ", 1e9), ("МБ", 1e6), ("КБ", 1e3)] where v >= k {
            let x = v / k
            return (x < 10 ? String(format: "%.2f", x) : String(format: "%.1f", x)).replacingOccurrences(of: ".", with: ",") + " " + unit
        }
        return "\(b) Б"
    }
    static func speed(_ b: Int64) -> String { b > 0 ? size(b) + "/с" : "0" }
    static func eta(_ s: Int?) -> String {
        guard let s else { return "—" }
        let m = (s + 59) / 60
        if m <= 1 { return "меньше минуты" }
        if m < 60 { return "\(m) мин" }
        if m < 60 * 48 { return "\(m / 60) ч \(m % 60) мин" }
        return "\(m / 1440) дн"
    }
    static func finishAt(_ s: Int?) -> String {
        guard let s else { return "" }
        let f = DateFormatter()
        f.timeZone = TimeZone(identifier: "Europe/Moscow")
        f.dateFormat = s < 20 * 3600 ? "HH:mm" : "d.MM HH:mm"
        return "к " + f.string(from: Date().addingTimeInterval(TimeInterval(s))) + " МСК"
    }
    static func episode(_ f: FileItem) -> String? {
        guard let e = f.episode else { return nil }
        if let s = f.season { return String(format: "S%02dE%02d", s, e) }
        return "серия \(e)"
    }
}

// MARK: - Модель окна

final class Model: ObservableObject {
    @Published var torrents: [Torrent] = []
    @Published var info: Info?
    @Published var candidates: [Candidate] = []
    @Published var files: FilesResp?
    @Published var watch: WatchTorrent?
    @Published var selection: String?
    @Published var connected = true
    @Published var errorText: String?
    @Published var busy = false

    private var timer: Timer?
    private var ticks = 0
    private let queue = DispatchQueue(label: "kachalka.daemon")

    init() {
        refresh(full: true)
        timer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.ticks += 1
            self.refresh(full: self.ticks % 5 == 0)
        }
    }

    func refresh(full: Bool) {
        let sel = selection
        queue.async { [weak self] in
            guard let self else { return }
            do {
                let list = try Daemon.decode(ListResp.self, ["cmd": "list"])
                var info: Info? = nil
                var cands: [Candidate]? = nil
                if full {
                    info = try? Daemon.decode(Info.self, ["cmd": "info"])
                    cands = (try? Daemon.decode(CleanupResp.self, ["cmd": "cleanup", "max_age": 300]))?.candidates
                }
                var files: FilesResp? = nil
                var watch: WatchTorrent? = nil
                if let sel, list.torrents.contains(where: { $0.id == sel }) {
                    files = try? Daemon.decode(FilesResp.self, ["cmd": "files", "ref": sel])
                    watch = (try? Daemon.decode(WatchResp.self, ["cmd": "watching", "ref": sel]))?.torrents.first
                }
                DispatchQueue.main.async {
                    self.connected = true
                    if self.torrents != list.torrents { self.torrents = list.torrents }
                    if let info { self.info = info }
                    if let cands { self.candidates = cands }
                    if sel == self.selection {
                        self.files = files
                        self.watch = watch
                    }
                }
            } catch DaemonError.notRunning {
                DispatchQueue.main.async { self.connected = false }
                Daemon.kickstart()
            } catch {
                DispatchQueue.main.async { self.errorText = error.localizedDescription }
            }
        }
    }

    /// Команда фоновой программе; ошибки показываем в окне.
    func run(_ req: [String: Any], then: (() -> Void)? = nil) {
        busy = true
        queue.async { [weak self] in
            do {
                _ = try Daemon.call(req, timeout: 60)
                DispatchQueue.main.async {
                    self?.busy = false
                    then?()
                    self?.refresh(full: true)
                }
            } catch {
                DispatchQueue.main.async {
                    self?.busy = false
                    self?.errorText = error.localizedDescription
                }
            }
        }
    }

    func add(_ source: String, episodes: String = "", follow: Bool = false, ahead: Int = 2) {
        var req: [String: Any] = ["cmd": "add", "source": source]
        if !episodes.trimmingCharacters(in: .whitespaces).isEmpty { req["episodes"] = episodes }
        if follow { req["follow"] = ["ahead": ahead] }
        run(req)
    }

    func select(_ t: String, file: Int, on: Bool) {
        run(["cmd": "select", "ref": t, "mode": on ? "add" : "remove", "files": [file]])
    }

    func reveal(_ path: String) {
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(url.deletingLastPathComponent())
        }
    }
}

// MARK: - Список

enum Filter: String, CaseIterable, Identifiable {
    case all = "Все", downloading = "Качаются", seeding = "Готово", follow = "Смотрю"
    var id: String { rawValue }
}

struct TorrentRow: View {
    let t: Torrent

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: icon).foregroundStyle(color)
                Text(t.name).font(.headline).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                if t.follow == true {
                    Text("смотрю сериал").font(.caption2).padding(.horizontal, 6).padding(.vertical, 2)
                        .background(Capsule().fill(Color.accentColor.opacity(0.15)))
                }
            }
            ProgressView(value: min(max(t.progress / 100, 0), 1)).tint(color)
            Text(caption).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            if !t.error.isEmpty {
                Text("⚠︎ " + t.error).font(.caption).foregroundStyle(.red).lineLimit(1)
            }
        }
        .padding(.vertical, 4)
    }

    var icon: String {
        if t.paused { return "pause.circle.fill" }
        if t.isDownloading { return "arrow.down.circle.fill" }
        return "checkmark.circle.fill"
    }
    var color: Color { t.paused ? .gray : t.isDownloading ? .accentColor : .green }

    var caption: String {
        var parts: [String] = []
        if t.isDownloading && !t.paused {
            parts.append("\(Fmt.size(t.done)) из \(Fmt.size(t.size))")
            parts.append("↓ " + Fmt.speed(t.down_rate))
            if t.eta_seconds != nil { parts.append("осталось \(Fmt.eta(t.eta_seconds)), \(Fmt.finishAt(t.eta_seconds))") }
            parts.append("пиров \(t.peers)")
        } else {
            parts.append("\(Int(t.progress))% · \(t.state)")
            parts.append(Fmt.size(t.size))
            if t.up_rate > 0 { parts.append("↑ " + Fmt.speed(t.up_rate)) }
            parts.append("рейтинг " + String(format: "%.2f", t.ratio).replacingOccurrences(of: ".", with: ","))
        }
        if let n = t.selected_files { parts.append("выбрано файлов: \(n)") }
        return parts.joined(separator: " · ")
    }
}

// MARK: - Подробности

struct DetailView: View {
    @ObservedObject var m: Model
    let t: Torrent
    @State private var confirmTrash = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(t.name).font(.title3.bold()).textSelection(.enabled).lineLimit(2)
            Text(t.save_path).font(.caption).foregroundStyle(.secondary).textSelection(.enabled).lineLimit(1).truncationMode(.middle)

            HStack(spacing: 8) {
                if t.paused {
                    Button { m.run(["cmd": "resume", "ref": t.id]) } label: { Label("Продолжить", systemImage: "play.fill") }
                } else {
                    Button { m.run(["cmd": "pause", "ref": t.id]) } label: { Label("Пауза", systemImage: "pause.fill") }
                }
                Button { m.reveal(t.save_path + "/" + (m.files?.files.first?.path.split(separator: "/").first.map(String.init) ?? t.name)) } label: {
                    Label("В Finder", systemImage: "folder")
                }
                Menu {
                    Button("Разложить по папкам") { m.run(["cmd": "sort", "ref": t.id]) }
                    Button("Отменить раскладку") { m.run(["cmd": "undo", "ref": t.id]) }
                } label: { Label("Порядок", systemImage: "square.grid.2x2") }
                .fixedSize()
                Menu {
                    Button("Убрать из списка (файлы оставить)") { m.run(["cmd": "remove", "ref": t.id]) { m.selection = nil } }
                    Button("Удалить вместе с файлами…", role: .destructive) { confirmTrash = true }
                } label: { Label("Удалить", systemImage: "trash") }
                .fixedSize()
            }
            .confirmationDialog("Убрать «\(t.name)» и перенести файлы в Корзину?", isPresented: $confirmTrash) {
                Button("В Корзину", role: .destructive) {
                    m.run(["cmd": "remove", "ref": t.id, "trash_files": true]) { m.selection = nil }
                }
            }

            followBox

            Divider()
            if let note = m.files?.note { Text(note).foregroundStyle(.secondary) }
            filesList
        }
        .padding()
    }

    @ViewBuilder var followBox: some View {
        let hasEpisodes = m.files?.files.contains { $0.episode != nil } ?? false
        if hasEpisodes {
            GroupBox {
                if let f = m.watch?.follow {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Label("Смотрю сериал", systemImage: "tv").font(.headline)
                            Spacer()
                            Button("Выключить") { m.run(["cmd": "unfollow", "ref": t.id]) }
                        }
                        if let c = m.watch?.current {
                            Text(c.done == true ? "Всё досмотрено" :
                                 "Сейчас: " + (c.episode.map { e in c.season.map { String(format: "S%02dE%02d", $0, e) } ?? "серия \(e)" } ?? "—"))
                        }
                        Stepper("Держать наготове серий: \(f.ahead)", value: Binding(
                            get: { f.ahead },
                            set: { m.run(["cmd": "follow", "ref": t.id, "ahead": $0, "keep_hours": f.keep_hours, "cleanup": f.cleanup == 1]) }
                        ), in: 0...10)
                        Text(f.cleanup == 1 ? "Просмотренные серии уходят в Корзину через \(Int(f.keep_hours)) ч."
                                            : "Просмотренные серии не удаляются.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    HStack {
                        Text("Качать серии по мере просмотра и убирать просмотренные").font(.callout)
                        Spacer()
                        Button("Смотрю сериал") { m.run(["cmd": "follow", "ref": t.id]) }
                    }
                }
            }
        }
    }

    var filesList: some View {
        let states = Dictionary(uniqueKeysWithValues: (m.watch?.files ?? []).map { ($0.index, $0) })
        return List(m.files?.files ?? []) { f in
            HStack(spacing: 8) {
                Toggle("", isOn: Binding(get: { f.selected }, set: { m.select(t.id, file: f.index, on: $0) }))
                    .toggleStyle(.checkbox).labelsHidden()
                    .help("Качать этот файл")
                if let w = states[f.index] {
                    Image(systemName: w.state == "watched" ? "checkmark.circle.fill" : w.state == "watching" ? "play.circle.fill" : "circle")
                        .foregroundStyle(w.state == "watched" ? .green : w.state == "watching" ? .orange : .secondary)
                        .help(w.state == "watched" ? "Посмотрено" : w.state == "watching" ? "Смотрю, \(w.watched_share)%" : "Не смотрел")
                }
                if let ep = Fmt.episode(f) { Text(ep).font(.body.monospacedDigit()).foregroundStyle(.secondary) }
                Text(f.name).lineLimit(1).truncationMode(.middle)
                Spacer()
                if states[f.index]?.trashed == true {
                    Text("в Корзине").font(.caption).foregroundStyle(.secondary)
                } else {
                    Text(f.progress >= 100 ? Fmt.size(f.size) : "\(Int(f.progress))% из \(Fmt.size(f.size))")
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
            }
            .contextMenu {
                if states[f.index] != nil {
                    Button("Отметить посмотренным") { m.run(["cmd": "watched", "ref": t.id, "files": [f.index], "watched": true]) }
                    Button("Отметить непросмотренным") { m.run(["cmd": "watched", "ref": t.id, "files": [f.index], "watched": false]) }
                }
                Button("Показать в Finder") { m.reveal(t.save_path + "/" + f.path) }
            }
        }
        .listStyle(.inset)
    }
}

// MARK: - Добавление

struct AddSheet: View {
    @ObservedObject var m: Model
    @Environment(\.dismiss) private var dismiss
    @State var source = ""
    @State private var episodes = ""
    @State private var follow = false
    @State private var ahead = 2

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Добавить загрузку").font(.title2.bold())
            HStack {
                TextField("magnet-ссылка, ссылка на .torrent или путь к файлу", text: $source)
                    .textFieldStyle(.roundedBorder)
                Button("Файл…") {
                    let p = NSOpenPanel()
                    p.allowedContentTypes = [UTType(filenameExtension: "torrent") ?? .data]
                    if p.runModal() == .OK, let u = p.url { source = u.path }
                }
            }
            TextField("Только серии (необязательно): 4,5 · 4-6 · S02E04", text: $episodes)
                .textFieldStyle(.roundedBorder)
            Toggle("Смотрю сериал: качать по мере просмотра, просмотренное убирать", isOn: $follow)
            if follow {
                Stepper("Держать наготове серий: \(ahead)", value: $ahead, in: 0...10)
            }
            HStack {
                Spacer()
                Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("Скачать") {
                    m.add(source.trimmingCharacters(in: .whitespacesAndNewlines), episodes: episodes, follow: follow, ahead: ahead)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(source.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20)
        .frame(width: 520)
        .onAppear {
            if source.isEmpty, let s = NSPasteboard.general.string(forType: .string),
               s.hasPrefix("magnet:") || s.hasSuffix(".torrent") {
                source = s
            }
        }
    }
}

// MARK: - Чистка

struct CleanupSheet: View {
    @ObservedObject var m: Model
    @Environment(\.dismiss) private var dismiss
    @State private var chosen: Set<String> = []

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Можно освободить место").font(.title2.bold())
            Text("Отмеченное уйдёт в Корзину, оттуда его можно вернуть.").foregroundStyle(.secondary)
            List(m.candidates) { c in
                Toggle(isOn: Binding(get: { chosen.contains(c.id) },
                                     set: { if $0 { chosen.insert(c.id) } else { chosen.remove(c.id) } })) {
                    VStack(alignment: .leading) {
                        Text(c.name).lineLimit(1)
                        Text("\(Fmt.size(c.size)) · \(c.reason)").font(.caption).foregroundStyle(.secondary)
                    }
                }
                .toggleStyle(.checkbox)
            }
            .frame(minHeight: 180)
            HStack {
                Spacer()
                Button("Отмена") { dismiss() }.keyboardShortcut(.cancelAction)
                Button("В Корзину (\(chosen.count))") {
                    m.run(["cmd": "cleanup", "apply": true, "ids": Array(chosen)])
                    dismiss()
                }
                .disabled(chosen.isEmpty)
                .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 560, height: 420)
        .onAppear { chosen = Set(m.candidates.map(\.id)) }
    }
}

// MARK: - Главное окно

struct ContentView: View {
    @ObservedObject var m: Model
    @State private var filter: Filter = .all
    @State private var search = ""
    @State private var showAdd = false
    @State private var addSource = ""
    @State private var showCleanup = false

    var shown: [Torrent] {
        m.torrents.filter { t in
            let f: Bool
            switch filter {
            case .all: f = true
            case .downloading: f = t.isDownloading
            case .seeding: f = !t.isDownloading
            case .follow: f = t.follow == true
            }
            return f && (search.isEmpty || t.name.localizedCaseInsensitiveContains(search))
        }
    }

    var body: some View {
        NavigationSplitView {
            List(shown, selection: $m.selection) { t in
                TorrentRow(t: t).tag(t.id)
                    .contextMenu {
                        Button(t.paused ? "Продолжить" : "Пауза") { m.run(["cmd": t.paused ? "resume" : "pause", "ref": t.id]) }
                        Button("Разложить по папкам") { m.run(["cmd": "sort", "ref": t.id]) }
                        Button("Убрать из списка") { m.run(["cmd": "remove", "ref": t.id]) }
                    }
            }
            .navigationSplitViewColumnWidth(min: 340, ideal: 420)
            .safeAreaInset(edge: .top) {
                Picker("Показать", selection: $filter) {
                    ForEach(Filter.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
            }
            .overlay {
                if m.torrents.isEmpty {
                    VStack(spacing: 8) {
                        Image(systemName: "arrow.down.circle").font(.system(size: 44)).foregroundStyle(.secondary)
                        Text(m.connected ? "Загрузок нет" : "Запускаю фоновую программу…").font(.headline)
                        Text("Нажми «+», перетащи сюда .torrent или открой magnet-ссылку.")
                            .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    }
                    .padding()
                }
            }
        } detail: {
            if let sel = m.selection, let t = shown.first(where: { $0.id == sel }) {
                DetailView(m: m, t: t)
            } else {
                Text("Выбери загрузку слева").foregroundStyle(.secondary)
            }
        }
        .searchable(text: $search, placement: .sidebar, prompt: "Поиск")
        .onChange(of: m.selection) {
            m.files = nil
            m.watch = nil
            m.refresh(full: false)
        }
        .toolbar {
            ToolbarItemGroup {
                Button { m.run(["cmd": "pause", "ref": "all"]) } label: { Label("Пауза всех", systemImage: "pause") }
                Button { m.run(["cmd": "resume", "ref": "all"]) } label: { Label("Продолжить все", systemImage: "play") }
                Button {
                    NSWorkspace.shared.open(URL(fileURLWithPath: m.info?.download_dir ?? NSHomeDirectory() + "/Movies"))
                } label: { Label("Папка загрузок", systemImage: "folder") }
                Button { addSource = ""; showAdd = true } label: { Label("Добавить", systemImage: "plus") }
                    .keyboardShortcut("n")
            }
        }
        .safeAreaInset(edge: .bottom) { statusBar }
        .sheet(isPresented: $showAdd) { AddSheet(m: m, source: addSource) }
        .sheet(isPresented: $showCleanup) { CleanupSheet(m: m) }
        .onDrop(of: [.fileURL], isTargeted: nil) { providers in
            for p in providers {
                _ = p.loadObject(ofClass: URL.self) { url, _ in
                    if let url, url.pathExtension.lowercased() == "torrent" {
                        DispatchQueue.main.async { m.add(url.path) }
                    }
                }
            }
            return true
        }
        .alert("Качалка", isPresented: Binding(get: { m.errorText != nil }, set: { if !$0 { m.errorText = nil } })) {
            Button("OK") { m.errorText = nil }
        } message: { Text(m.errorText ?? "") }
        .onReceive(NotificationCenter.default.publisher(for: .kachalkaOpen)) { note in
            if let s = note.object as? String { addSource = s; showAdd = true }
        }
    }

    var statusBar: some View {
        HStack(spacing: 14) {
            if let i = m.info {
                Label(Fmt.speed(i.down_rate), systemImage: "arrow.down").help("Скачивание")
                Label(Fmt.speed(i.up_rate), systemImage: "arrow.up").help("Раздача")
                let iface = i.active_interface ?? ""
                Label(iface.hasPrefix("utun") ? "через VPN" : iface.isEmpty ? "сеть системы" : "мимо VPN (\(iface))",
                      systemImage: iface.hasPrefix("utun") ? "lock.shield" : "network")
                    .help("Переключить: kachalka vpn on|off")
                Text("DHT \(i.dht_nodes)").foregroundStyle(.secondary)
            } else if !m.connected {
                Label("фоновая программа не отвечает", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
            Spacer()
            if !m.candidates.isEmpty {
                Button {
                    showCleanup = true
                } label: {
                    Label("Можно освободить \(Fmt.size(m.candidates.reduce(0) { $0 + $1.size }))", systemImage: "sparkles")
                }
                .buttonStyle(.link)
            }
            if m.busy { ProgressView().controlSize(.small) }
        }
        .font(.caption)
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .background(.bar)
    }
}

// MARK: - Приложение

extension Notification.Name {
    static let kachalkaOpen = Notification.Name("kachalkaOpen")
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var model: Model?

    func application(_ application: NSApplication, open urls: [URL]) {
        for url in urls {
            // magnet-ссылки и .torrent из Finder/браузера сразу добавляем; окно с выбором серий — по «+»
            if url.scheme == "magnet" {
                model?.add(url.absoluteString)
            } else if url.isFileURL, url.pathExtension.lowercased() == "torrent" {
                model?.add(url.path)
            }
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct KachalkaApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = Model()

    var body: some Scene {
        WindowGroup("Качалка") {
            ContentView(m: model)
                .frame(minWidth: 900, minHeight: 500)
                .onAppear { delegate.model = model }
        }
        .defaultSize(width: 1200, height: 720)
        .commands {
            CommandGroup(after: .appSettings) {
                Button("Открывать magnet-ссылки и .torrent в Качалке") { makeDefault() }
            }
        }
    }

    /// Владелец сам нажимает пункт меню: делаем Качалку приложением по умолчанию для magnet и .torrent.
    func makeDefault() {
        guard let id = Bundle.main.bundleIdentifier else { return }
        LSSetDefaultHandlerForURLScheme("magnet" as CFString, id as CFString)
        if let t = UTType(filenameExtension: "torrent") {
            LSSetDefaultRoleHandlerForContentType(t.identifier as CFString, .all, id as CFString)
        }
        model.errorText = "Готово: magnet-ссылки и .torrent-файлы теперь открываются в Качалке."
    }
}
