// Funkuino.app — a thin native shell around the existing Studio web app.
//
// The design point: Studio stays a Python aiohttp server. This app starts it as
// a child process on a free localhost port, waits for it to answer, and shows it
// in a WKWebView. Nothing of the app's logic lives here, so the shell can be
// replaced or dropped without touching the tool.
//
// Skeleton stage: the server is the one from the checkout this was built from
// (baked in as FUNKUINO_CODE_ROOT). A release build will carry its own Python
// runtime inside the bundle instead.

import SwiftUI
import WebKit

// MARK: - Server child process

/// Owns the Studio server process for the lifetime of the app.
final class StudioServer: ObservableObject {
    enum State: Equatable {
        case starting
        case running(URL)
        case failed(String)
    }

    @Published private(set) var state: State = .starting

    private var process: Process?
    private let codeRoot: URL
    /// The child's own output. Without it a failing server is a silent 30 s
    /// wait ending in "no answer", while the reason sat on its stderr.
    private var output: [String] = []

    init() {
        // The shipped code lives in the bundle, next to its own Python runtime.
        // FUNKUINO_CODE_ROOT overrides it for development against a checkout;
        // the baked path is the last resort for an unpackaged debug build.
        let bundled = Bundle.main.resourceURL?.appendingPathComponent("code")
        let baked = Bundle.main.object(forInfoDictionaryKey: "FunkuinoCodeRoot") as? String
        if let override = ProcessInfo.processInfo.environment["FUNKUINO_CODE_ROOT"] {
            codeRoot = URL(fileURLWithPath: override)
        } else if let bundled,
                  FileManager.default.fileExists(atPath: bundled.path) {
            codeRoot = bundled
        } else {
            codeRoot = URL(fileURLWithPath: baked ?? "")
        }
    }

    func start(config: AppConfig, configDir: URL) {
        guard process == nil else { return }
        let port: UInt16
        do {
            port = try Self.freePort()
        } catch {
            state = .failed("Kein freier Port: \(error.localizedDescription)")
            return
        }

        let dispatcher = codeRoot.appendingPathComponent("bin/funkuino")
        if let problem = Self.checkPrerequisites(codeRoot: codeRoot, config: config) {
            state = .failed(problem)
            return
        }

        let proc = Process()
        proc.executableURL = dispatcher
        // No --host: Studio reads the device from config.json, and a start
        // parameter would pin it for the whole run, so a change made in
        // Studio's settings dialog could not take effect.
        proc.arguments = ["studio", "--port", String(port), "--no-browser"]
        // A GUI app inherits a minimal PATH. The bundle's own ffmpeg comes
        // first — a Homebrew one may be any build with any feature set — then
        // the usual locations, so a machine without the bundled copy still works.
        var env = ProcessInfo.processInfo.environment
        let bundledTools = Bundle.main.resourceURL?.appendingPathComponent("ffmpeg").path
        env["PATH"] = [bundledTools, "/opt/homebrew/bin", "/usr/local/bin",
                       env["PATH"] ?? "/usr/bin:/bin"]
            .compactMap { $0 }.joined(separator: ":")
        // Belt and braces against an orphaned server: stop() covers a normal
        // quit, this covers a crash, a force-quit or a signal that never reaches
        // applicationWillTerminate.
        env["FUNKUINO_EXIT_WITH_PARENT"] = "1"
        // Resolved here so the child (and everything it spawns) agrees with the
        // window the user just clicked through, whatever the defaults would say.
        env["FUNKUINO_DATA_DIR"] = config.dataDir
        env["FUNKUINO_CONFIG_DIR"] = configDir.path
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty, let text = String(data: chunk, encoding: .utf8) else { return }
            Task { @MainActor in
                self?.append(text)
            }
        }

        do {
            try proc.run()
        } catch {
            state = .failed("Serverstart fehlgeschlagen: \(error.localizedDescription)")
            return
        }
        process = proc

        let url = URL(string: "http://127.0.0.1:\(port)/")!
        Task { await waitUntilAnswering(url) }
    }

    /// Poll until the server answers — it needs a moment to scan the library.
    private func waitUntilAnswering(_ url: URL) async {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if process?.isRunning == false {
                await MainActor.run {
                    state = .failed(describe("Der Server hat sich beendet."))
                }
                return
            }
            var request = URLRequest(url: url)
            request.timeoutInterval = 2
            if let (_, response) = try? await URLSession.shared.data(for: request),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                await MainActor.run { state = .running(url) }
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
        await MainActor.run {
            state = .failed(describe("Der Server antwortet nicht."))
        }
    }

    private func append(_ text: String) {
        output.append(contentsOf: text.split(separator: "\n").map(String.init))
        if output.count > 40 { output.removeFirst(output.count - 40) }
    }

    /// A headline plus what the server last said — the useful half.
    private func describe(_ headline: String) -> String {
        let tail = output.suffix(8).joined(separator: "\n")
        return tail.isEmpty ? headline : headline + "\n\n" + tail
    }

    /// Everything that must be in place before starting is worth its own
    /// message: "no answer" after 30 seconds is not a diagnosis.
    static func checkPrerequisites(codeRoot: URL, config: AppConfig) -> String? {
        let fm = FileManager.default
        let dispatcher = codeRoot.appendingPathComponent("bin/funkuino")
        guard fm.isExecutableFile(atPath: dispatcher.path) else {
            return "Das Programm ist unvollständig: bin/funkuino fehlt unter "
                 + codeRoot.path + "."
        }
        let interpreter = codeRoot.appendingPathComponent("runtime/bin/python3")
        let venv = codeRoot.appendingPathComponent(".venv/bin/python")
        guard fm.isExecutableFile(atPath: interpreter.path)
                || fm.isExecutableFile(atPath: venv.path) else {
            return "Das Programm ist unvollständig: die Python-Laufzeit fehlt im "
                 + "Programmpaket."
        }
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: config.dataDir, isDirectory: &isDir), isDir.boolValue else {
            return "Der Datenordner \(config.dataDir) ist nicht erreichbar. Liegt "
                 + "er auf einer Platte, die gerade nicht angeschlossen ist?"
        }
        guard fm.isWritableFile(atPath: config.dataDir) else {
            return "Keine Schreibrechte im Datenordner \(config.dataDir)."
        }
        return nil
    }

    /// ffmpeg is only needed for downloading and merging, so its absence is a
    /// warning rather than a blocked start — library, sync and printing work.
    static func findFFmpeg() -> String? {
        if let bundled = Bundle.main.resourceURL?
            .appendingPathComponent("ffmpeg/ffmpeg").path,
           FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        for directory in ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"] {
            let candidate = directory + "/ffmpeg"
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// Must run on quit: an orphaned server would keep the port and the device
    /// websocket open.
    func stop() {
        process?.terminate()
        process = nil
        state = .starting
    }

    /// Ask the kernel for an unused port by binding to 0 and reading it back.
    private static func freePort() throws -> UInt16 {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { throw POSIXError(.EADDRNOTAVAIL) }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        let size = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, size) }
        }
        guard bound == 0 else { throw POSIXError(.EADDRINUSE) }
        var out = sockaddr_in()
        var outSize = size
        _ = withUnsafeMutablePointer(to: &out) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(fd, $0, &outSize) }
        }
        return UInt16(bigEndian: out.sin_port)
    }
}

// MARK: - Web view

/// Lets the page act as the window's drag region.
///
/// With a full-size content view the WKWebView covers the whole window, the
/// transparent title bar included, and swallows the clicks that would otherwise
/// move the window. So the page reports a drag started in its header (see
/// initShell in app.js) and we hand that to AppKit.
final class DragHandler: NSObject, WKScriptMessageHandler {
    weak var webView: WKWebView?

    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard let window = webView?.window, let event = NSApp.currentEvent else { return }
        window.performDrag(with: event)
    }
}

/// Hands the live web view to the menu commands (there is no other way to reach
/// it from the App's scene, and without it Cmd-R would just beep).
final class WebViewBox: ObservableObject {
    weak var view: WKWebView?
}

struct StudioWebView: NSViewRepresentable {
    let url: URL
    let box: WebViewBox

    func makeCoordinator() -> DragHandler { DragHandler() }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.userContentController.add(context.coordinator, name: "shellDrag")
        let view = WKWebView(frame: .zero, configuration: config)
        context.coordinator.webView = view
        box.view = view
        view.load(URLRequest(url: url))
        // .windowStyle(.hiddenTitleBar) alone only hides the title: the window
        // still reserves the title bar's height, which showed as a white strip
        // above the page header. Configure the window itself so the content
        // runs to the top edge and the traffic lights float over the header.
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.styleMask.insert(.fullSizeContentView)
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            // Without a title bar to grab, the window would be hard to move.
            window.isMovableByWindowBackground = true
        }
        return view
    }

    func updateNSView(_ view: WKWebView, context: Context) {}
}

// MARK: - App

struct RootView: View {
    @EnvironmentObject var server: StudioServer
    @EnvironmentObject var web: WebViewBox
    @EnvironmentObject var configuration: AppConfiguration

    var body: some View {
        if configuration.needsSetup {
            // No data folder yet (or the user asked to redo setup): the server
            // has nothing to serve from, so it is not started at all.
            SetupView()
        } else {
            serverView.onAppear(perform: startServer)
        }
    }

    private func startServer() {
        guard let config = configuration.config else { return }
        server.start(config: config, configDir: configuration.directory)
    }

    @ViewBuilder
    private var serverView: some View {
        switch server.state {
        case .starting:
            StartingView()
        case .running(let url):
            // ?shell=mac tells the page it is inside this window, so its header
            // can take over the role of the (hidden) title bar. ignoresSafeArea
            // is what actually lets it reach the top edge — SwiftUI insets the
            // content by the title bar height otherwise, hidden or not.
            StudioWebView(url: URL(string: "?shell=mac", relativeTo: url) ?? url, box: web)
                .ignoresSafeArea()
        case .failed(let message):
            FailureView(message: message) {
                server.stop()
                configuration.forceSetup = true
            }
        }
    }
}

// Own types, not inline cases: this is what `--snapshot` renders (Snapshot.swift),
// which is the only way to look at these screens without sitting at the Mac.

struct StartingView: View {
    var body: some View {
        VStack(spacing: 14) {
            WaveMark(height: 26)
            Text("Studio startet…")
                .font(Theme.display(17)).foregroundStyle(Theme.inkSoft)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PaperBackground())
    }
}

struct FailureView: View {
    let message: String
    let onSetup: () -> Void

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 30)).foregroundStyle(Theme.warn)
            Text("Studio konnte nicht starten")
                .font(Theme.display(20)).foregroundStyle(Theme.ink)
            // Selectable: the tail of the server's own output is in here, and
            // that is what gets pasted into a bug report.
            Text(message)
                .multilineTextAlignment(.leading)
                .textSelection(.enabled)
                .font(.system(size: 12, design: .monospaced))
                .foregroundStyle(Theme.inkSoft)
                .padding(14)
                .background(RoundedRectangle(cornerRadius: Theme.radius).fill(Theme.surface))
                .overlay(RoundedRectangle(cornerRadius: Theme.radius)
                    .stroke(Theme.line, lineWidth: 1))
            Button("Einrichtung ändern…", action: onSetup)
                .buttonStyle(FunkuinoButtonStyle())
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PaperBackground())
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var server: StudioServer?
    private var signalSources: [DispatchSourceSignal] = []

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Before anything else: render a screen to a file and quit.
        if let request = Snapshot.requested() {
            Snapshot.run(view: request.view, to: request.path)
        }

        // A GUI app does not run applicationWillTerminate on SIGTERM, so a
        // `kill` would leave the server behind. Catch the signals we can.
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { [weak self] in
                self?.server?.stop()
                exit(0)
            }
            source.resume()
            signalSources.append(source)
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        server?.stop()
    }
}

@main
struct FunkuinoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var server = StudioServer()
    @StateObject private var web = WebViewBox()
    @StateObject private var configuration = AppConfiguration()

    var body: some Scene {
        WindowGroup("Funkuino Studio") {
            RootView()
                .environmentObject(server)
                .environmentObject(web)
                .environmentObject(configuration)
                .frame(minWidth: 900, minHeight: 620)
                .onAppear { delegate.server = server }
        }
        // No native title bar: it would show "Funkuino Studio" directly above the
        // page's own wordmark. The window's content now runs to the top edge and
        // the page header is the title bar; the traffic lights float over it.
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1180, height: 800)
        .commands {
            // Without a browser's chrome there is no reload; Cmd-R would beep.
            CommandGroup(after: .toolbar) {
                Button("Neu laden") { web.view?.reload() }
                    .keyboardShortcut("r", modifiers: .command)
            }
            CommandGroup(after: .appSettings) {
                Button("Einrichtung ändern…") {
                    // Stop first: the running server holds the old data folder,
                    // and the setup screen is where a new one is chosen.
                    server.stop()
                    configuration.forceSetup = true
                }
            }
        }
    }
}
