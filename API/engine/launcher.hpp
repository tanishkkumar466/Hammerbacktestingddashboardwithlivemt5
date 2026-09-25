#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

struct LaunchResult {
    bool ok = false;
    std::string message;
    uint32_t pid = 0;
    std::string portable_path;  // terminal64.exe under isolated data folder
};

struct AccountConnectSpec {
    std::string account_id;
    std::string mt5_path;
    std::string login;
    std::string password;
    std::string server;
};

struct FleetLaunchResult {
    bool ok = false;
    std::string message;
    std::vector<LaunchResult> results;
};

class TerminalLauncher {
public:
    // data_root = %LOCALAPPDATA%/Hammer/API/terminals
    explicit TerminalLauncher(std::string data_root);

    void set_default_mt5_path(std::string path);
    std::string default_mt5_path() const;

    LaunchResult start_account(
        const std::string& account_id,
        const std::string& mt5_path_override,
        const std::string& login,
        const std::string& password,
        const std::string& server
    );

    // Stagger launches (default 100ms) so brokers do not see N identical logins
    // at the same microsecond.
    FleetLaunchResult start_accounts_staggered(
        const std::vector<AccountConnectSpec>& specs,
        int stagger_ms
    );

    LaunchResult stop_account(const std::string& account_id);
    LaunchResult stop_all();

    std::string status_json() const;

private:
    std::string prepare_portable_dir(const std::string& account_id, const std::string& source_exe);
    bool ensure_portable_copy(const std::string& source_exe, const std::string& dest_dir);

    std::string data_root_;
    std::string default_mt5_path_;
    mutable std::mutex mu_;
    std::unordered_map<std::string, uint32_t> pids_;
};
