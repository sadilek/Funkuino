// First run: choose where the library lives and which ESPuino to talk to.
//
// The app cannot pick a data folder for the user — a media library is theirs to
// place, possibly on an external disk — so nothing can start until it is set.
// The answer is written to the same config.json that espuino.py reads, so the
// terminal tools and the app operate on one installation instead of diverging.

import SwiftUI

// MARK: - AppConfiguration

struct AppConfig: Codable, Equatable {
    /// Written as `data_dir` — espuino.py's key, this file has two readers.
    var dataDir: String
    var host: String

    enum CodingKeys: String, CodingKey {
        case dataDir = "data_dir"
        case host
    }

    init(dataDir: String, host: String) {
        self.dataDir = dataDir
        self.host = host
    }

    /// Studio's settings dialog drops `host` from the file when it is set
    /// back to the default, so a missing one means exactly that.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        dataDir = try container.decode(String.self, forKey: .dataDir)
        host = try container.decodeIfPresent(String.self, forKey: .host) ?? "espuino.local"
    }
}

@MainActor
final class AppConfiguration: ObservableObject {
    @Published private(set) var config: AppConfig?
    /// Set while the user re-runs setup on an already configured installation.
    @Published var forceSetup = false

    let directory: URL

    var needsSetup: Bool { config == nil || forceSetup }
    var file: URL { directory.appendingPathComponent("config.json") }

    init() {
        directory = Self.resolveDirectory()
        load()
    }

    /// `--config-dir DIR` > FUNKUINO_CONFIG_DIR > Application Support. The
    /// argument form is what makes the first-run flow testable repeatedly:
    /// `open -a Funkuino --args --config-dir /tmp/probe`.
    private static func resolveDirectory() -> URL {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--config-dir"), i + 1 < args.count {
            return URL(fileURLWithPath: (args[i + 1] as NSString).expandingTildeInPath)
        }
        if let env = ProcessInfo.processInfo.environment["FUNKUINO_CONFIG_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.urls(for: .applicationSupportDirectory,
                                        in: .userDomainMask)[0]
            .appendingPathComponent("Funkuino")
    }

    func load() {
        guard let data = try? Data(contentsOf: file),
              let decoded = try? JSONDecoder().decode(AppConfig.self, from: data)
        else { config = nil; return }
        // A configured folder that has since vanished (external disk, moved,
        // deleted) must not silently become an empty library — ask again.
        var isDir: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: decoded.dataDir, isDirectory: &isDir)
        config = exists && isDir.boolValue ? decoded : nil
    }

    /// Merges into the existing file rather than replacing it: Studio keeps
    /// its own settings there too (features, library sort, …), which a
    /// rewrite from AppConfig alone would silently drop.
    func save(_ new: AppConfig) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var merged: [String: Any] = [:]
        if let data = try? Data(contentsOf: file),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            merged = existing
        }
        merged["data_dir"] = new.dataDir
        merged["host"] = new.host
        let data = try JSONSerialization.data(
            withJSONObject: merged,
            options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        try data.write(to: file, options: .atomic)
        config = new
        forceSetup = false
    }
}

// MARK: - Setup view

struct SetupView: View {
    @EnvironmentObject var configuration: AppConfiguration

    @State private var folder: String = ""
    @State private var host: String = "espuino.local"
    @State private var probe: String?
    @State private var probing = false
    @State private var error: String?

    private var defaultFolder: String {
        FileManager.default.urls(for: .musicDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Funkuino").path
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 10) {
                    WaveMark(height: 22)
                    Text("Willkommen bei Funkuino").font(Theme.display(28))
                }
                Text("Zwei Angaben, dann kann es losgehen.")
                    .foregroundStyle(Theme.inkSoft)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Datenordner").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink)
                Text("Hier liegen deine Hörspiele und Lieder, die Titelbilder für "
                     + "die Karten, die Druckbögen und der Abgleichstand. Du kannst "
                     + "ihn auf eine externe Platte legen.")
                    .font(.system(size: 12.5)).foregroundStyle(Theme.inkSoft).fixedSize(horizontal: false, vertical: true)
                HStack {
                    TextField("", text: $folder).textFieldStyle(FunkuinoFieldStyle())
                    Button("Wählen…") { choose() }.buttonStyle(FunkuinoButtonStyle())
                }
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("ESPuino").font(.system(size: 13, weight: .semibold)).foregroundStyle(Theme.ink)
                Text("Der Netzwerkname oder die IP-Adresse deines Geräts. "
                     + "Lässt sich später ändern.")
                    .font(.system(size: 12.5)).foregroundStyle(Theme.inkSoft).fixedSize(horizontal: false, vertical: true)
                HStack {
                    TextField("espuino.local", text: $host).textFieldStyle(FunkuinoFieldStyle())
                    Button(probing ? "Prüfe…" : "Verbindung testen") { test() }
                        .buttonStyle(FunkuinoButtonStyle())
                        .disabled(probing || host.isEmpty)
                }
                if let probe {
                    Text(probe).font(.system(size: 12.5, design: .monospaced))
                        .foregroundStyle(Theme.inkFaint)
                }
            }

            if StudioServer.findFFmpeg() == nil {
                // Not a blocker: only downloading and merging need it.
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "exclamationmark.triangle")
                    Text("ffmpeg wurde nicht gefunden. Mediathek, Abgleich und "
                         + "Kartendruck funktionieren, zum Herunterladen und "
                         + "Zusammenschneiden von Hörspielen fehlt es aber. "
                         + "Installieren mit: brew install ffmpeg")
                        .fixedSize(horizontal: false, vertical: true)
                }
                .font(.system(size: 12.5)).foregroundStyle(Theme.warn)
            }

            if let error {
                Text(error).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Spacer()
                Button("Fertig") { finish() }
                    .buttonStyle(FunkuinoButtonStyle(prominent: true))
                    .keyboardShortcut(.defaultAction)
                    .disabled(folder.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(38)
        .frame(minWidth: 560, maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(PaperBackground())
        .onAppear {
            if folder.isEmpty {
                folder = configuration.config?.dataDir ?? defaultFolder
            }
            if let configured = configuration.config?.host { host = configured }
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = "Auswählen"
        panel.directoryURL = URL(fileURLWithPath: folder).deletingLastPathComponent()
        if panel.runModal() == .OK, let url = panel.url { folder = url.path }
    }

    /// Purely informational — an ESPuino that is switched off right now is no
    /// reason to block setup.
    private func test() {
        probing = true
        probe = nil
        let target = host.trimmingCharacters(in: .whitespaces)
        Task {
            defer { probing = false }
            guard let url = URL(string: "http://\(target)/info") else {
                probe = "Ungültiger Name."
                return
            }
            var request = URLRequest(url: url)
            request.timeoutInterval = 6
            do {
                let (_, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                probe = code == 200 ? "Gerät antwortet." : "Antwort mit Status \(code)."
            } catch {
                probe = "Keine Antwort — das Gerät kann auch einfach aus sein."
            }
        }
    }

    private func finish() {
        let path = (folder.trimmingCharacters(in: .whitespaces) as NSString).expandingTildeInPath
        let url = URL(fileURLWithPath: path)
        do {
            // Create the layout the tools expect, so the first sync or download
            // does not fail on a missing directory.
            for sub in ["files", "card-covers", "print-sheets", "status"] {
                try FileManager.default.createDirectory(
                    at: url.appendingPathComponent(sub), withIntermediateDirectories: true)
            }
            guard FileManager.default.isWritableFile(atPath: url.path) else {
                error = "Kein Schreibrecht in \(url.path)."
                return
            }
            let trimmed = host.trimmingCharacters(in: .whitespaces)
            try configuration.save(AppConfig(dataDir: url.path,
                                             host: trimmed.isEmpty ? "espuino.local" : trimmed))
        } catch {
            self.error = "Ordner konnte nicht angelegt werden: \(error.localizedDescription)"
        }
    }
}
