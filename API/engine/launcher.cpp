#include "launcher.hpp"
#include "mt5_path.hpp"

#include <chrono>
#include <filesystem>
#include <sstream>
#include <thread>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#else
#include <signal.h>
#include <unistd.h>
#endif

namespace fs = std::filesystem;

TerminalLauncher::TerminalLauncher(std::string data_root)
    : data_root_(std::move(data_root)) {
    default_mt5_path_ = DiscoverMt5ExecutablePath();
    try {
        fs::create_directories(data_root_);
    } catch (...) {
    }
}

void TerminalLauncher::set_default_mt5_path(std::string path) {
    std::lock_guard<std::mutex> lock(mu_);
    default_mt5_path_ = std::move(path);
}

std::string TerminalLauncher::default_mt5_path() const {
    std::lock_guard<std::mutex> lock(mu_);
    return default_mt5_path_;
}

bool TerminalLauncher::ensure_portable_copy(const std::string& source_exe, const std::string& dest_dir) {
    try {
        fs::create_directories(dest_dir);
        fs::path src(source_exe);
        fs::path src_dir = src.parent_path();
        fs::path dest(dest_dir);
        fs::path dest_exe = dest / src.filename();
        if (fs::exists(dest_exe) && FileLooksLikeMt5Exe(dest_exe.string())) {
            return true;
        }
        // Copy core binaries from install folder (needed so /portable data stays isolated).
        for (auto& entry : fs::directory_iterator(src_dir)) {
            if (!entry.is_regular_file()) continue;
            auto name = entry.path().filename().string();
            // Keep footprint smaller: exe + dll + essential folders later if needed
            const bool take =
                name == "terminal64.exe" || name == "terminal.exe" ||
                (name.size() >= 4 && (
                    name.substr(name.size() - 4) == ".dll" ||
                    name.substr(name.size() - 4) == ".DLL"
                ));
            if (!take) continue;
            fs::copy_file(entry.path(), dest / entry.path().filename(),
                          fs::copy_options::overwrite_existing);
        }
        return fs::exists(dest_exe);
    } catch (...) {
        return false;
    }
}

std::string TerminalLauncher::prepare_portable_dir(const std::string& account_id, const std::string& source_exe) {
    fs::path dir = fs::path(data_root_) / account_id;
    if (!ensure_portable_copy(source_exe, dir.string())) {
        return {};
    }
    fs::path src(source_exe);
    return (dir / src.filename()).string();
}

LaunchResult TerminalLauncher::start_account(
    const std::string& account_id,
    const std::string& mt5_path_override,
    const std::string& login,
    const std::string& password,
    const std::string& server
) {
    LaunchResult r;
    if (account_id.empty()) {
        r.message = "account_id required";
        return r;
    }

    std::string source = mt5_path_override.empty() ? default_mt5_path() : mt5_path_override;
    if (source.empty() || !FileLooksLikeMt5Exe(source)) {
        source = DiscoverMt5ExecutablePath();
        if (!source.empty()) {
            set_default_mt5_path(source);
        }
    }
    if (source.empty() || !FileLooksLikeMt5Exe(source)) {
        r.message = "MT5 path not found — set path in UI (Locate terminal64.exe) or install MetaTrader 5";
        return r;
    }

    {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = pids_.find(account_id);
        if (it != pids_.end() && it->second != 0) {
#ifdef _WIN32
            HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, it->second);
            if (h) {
                DWORD code = 0;
                if (GetExitCodeProcess(h, &code) && code == STILL_ACTIVE) {
                    CloseHandle(h);
                    r.ok = true;
                    r.pid = it->second;
                    r.message = "already running";
                    // Client resolves portable path via LOCALAPPDATA\\Hammer\\API\\terminals\\<id>
                    return r;
                }
                CloseHandle(h);
            }
#endif
            pids_.erase(it);
        }
    }

    const std::string portable_exe = prepare_portable_dir(account_id, source);
    if (portable_exe.empty()) {
        r.message = "failed to prepare portable terminal folder (copy from install)";
        return r;
    }

#ifdef _WIN32
    // Note: MetaQuotes documents /portable officially. CLI /login /password /server
    // are used by some bridges; if ignored, account is still attached via saved profile
    // or later MetaTrader5/MtApi bind to this portable instance path.
    std::ostringstream args;
    args << "\"" << portable_exe << "\"";
    args << " /portable";
    if (!login.empty()) args << " /login:" << login;
    if (!password.empty()) args << " /password:" << password;
    if (!server.empty()) args << " /server:" << server;

    std::string cmdline = args.str();
    std::vector<char> mutable_cmd(cmdline.begin(), cmdline.end());
    mutable_cmd.push_back('\0');

    STARTUPINFOA si{};
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESHOWWINDOW;
    si.wShowWindow = SW_HIDE;
    PROCESS_INFORMATION pi{};

    fs::path work = fs::path(portable_exe).parent_path();
    BOOL ok = CreateProcessA(
        nullptr,
        mutable_cmd.data(),
        nullptr,
        nullptr,
        FALSE,
        CREATE_NO_WINDOW | DETACHED_PROCESS,
        nullptr,
        work.string().c_str(),
        &si,
        &pi
    );
    if (!ok) {
        r.message = "CreateProcess failed (error " + std::to_string(GetLastError()) + ")";
        return r;
    }
    const uint32_t pid = static_cast<uint32_t>(pi.dwProcessId);
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);

    {
        std::lock_guard<std::mutex> lock(mu_);
        pids_[account_id] = pid;
    }
    r.ok = true;
    r.pid = pid;
    r.portable_path = portable_exe;
    r.message = "headless portable terminal started pid=" + std::to_string(pid);
    return r;
#else
    (void)login;
    (void)password;
    (void)server;
    r.message = "headless launcher is Windows-only (CREATE_NO_WINDOW)";
    return r;
#endif
}

FleetLaunchResult TerminalLauncher::start_accounts_staggered(
    const std::vector<AccountConnectSpec>& specs,
    int stagger_ms
) {
    FleetLaunchResult fleet;
    if (specs.empty()) {
        fleet.ok = true;
        fleet.message = "no accounts";
        return fleet;
    }
    if (stagger_ms < 0) stagger_ms = 0;
    // Cap absurd values; 100ms default from Python / UI
    if (stagger_ms > 5000) stagger_ms = 5000;

    for (size_t i = 0; i < specs.size(); ++i) {
        if (i > 0 && stagger_ms > 0) {
#ifdef _WIN32
            Sleep(static_cast<DWORD>(stagger_ms));
#else
            std::this_thread::sleep_for(std::chrono::milliseconds(stagger_ms));
#endif
        }
        const auto& s = specs[i];
        auto lr = start_account(s.account_id, s.mt5_path, s.login, s.password, s.server);
        fleet.results.push_back(lr);
    }

    size_t ok_n = 0;
    for (const auto& r : fleet.results) {
        if (r.ok) ++ok_n;
    }
    fleet.ok = ok_n == fleet.results.size();
    fleet.message = "fleet started " + std::to_string(ok_n) + "/" +
                    std::to_string(fleet.results.size()) +
                    " stagger_ms=" + std::to_string(stagger_ms);
    return fleet;
}

LaunchResult TerminalLauncher::stop_account(const std::string& account_id) {
    LaunchResult r;
    uint32_t pid = 0;
    {
        std::lock_guard<std::mutex> lock(mu_);
        auto it = pids_.find(account_id);
        if (it == pids_.end()) {
            r.ok = true;
            r.message = "not running";
            return r;
        }
        pid = it->second;
        pids_.erase(it);
    }
#ifdef _WIN32
    HANDLE h = OpenProcess(PROCESS_TERMINATE, FALSE, pid);
    if (!h) {
        r.message = "OpenProcess failed";
        return r;
    }
    BOOL ok = TerminateProcess(h, 1);
    CloseHandle(h);
    r.ok = ok == TRUE;
    r.pid = pid;
    r.message = ok ? "terminated" : "TerminateProcess failed";
    return r;
#else
    if (pid) kill(static_cast<pid_t>(pid), SIGTERM);
    r.ok = true;
    r.pid = pid;
    r.message = "signal sent";
    return r;
#endif
}

LaunchResult TerminalLauncher::stop_all() {
    std::unordered_map<std::string, uint32_t> copy;
    {
        std::lock_guard<std::mutex> lock(mu_);
        copy = pids_;
    }
    for (auto& kv : copy) {
        stop_account(kv.first);
    }
    LaunchResult r;
    r.ok = true;
    r.message = "all stopped";
    return r;
}

std::string TerminalLauncher::status_json() const {
    std::ostringstream oss;
    oss << "{\"default_mt5_path\":\"";
    {
        std::lock_guard<std::mutex> lock(mu_);
        // Escape minimal
        for (char c : default_mt5_path_) {
            if (c == '\\') oss << "\\\\";
            else if (c == '"') oss << "\\\"";
            else oss << c;
        }
        oss << "\",\"accounts\":[";
        bool first = true;
        for (const auto& kv : pids_) {
            if (!first) oss << ",";
            first = false;
            oss << "{\"id\":\"" << kv.first << "\",\"pid\":" << kv.second << "}";
        }
    }
    oss << "]}";
    return oss.str();
}
