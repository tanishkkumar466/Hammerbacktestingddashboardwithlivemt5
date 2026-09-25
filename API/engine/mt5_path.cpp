#include "mt5_path.hpp"

#include <algorithm>
#include <cctype>
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#else
#include <sys/stat.h>
#endif

bool FileLooksLikeMt5Exe(const std::string& path) {
    if (path.empty()) return false;
#ifdef _WIN32
    DWORD attrs = GetFileAttributesA(path.c_str());
    if (attrs == INVALID_FILE_ATTRIBUTES || (attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        return false;
    }
#else
    struct stat st {};
    if (stat(path.c_str(), &st) != 0 || !S_ISREG(st.st_mode)) return false;
#endif
    const auto slash = path.find_last_of("\\/");
    const std::string name = (slash == std::string::npos) ? path : path.substr(slash + 1);
    return name == "terminal64.exe" || name == "terminal.exe" ||
           name == "terminal64" || name == "terminal";
}

#ifdef _WIN32
static std::string ToLower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

static bool QueryRegPath(HKEY root, const char* subkey, const char* valueName, std::string& out) {
    HKEY hKey = nullptr;
    if (RegOpenKeyExA(root, subkey, 0, KEY_READ, &hKey) != ERROR_SUCCESS) {
        return false;
    }
    char buf[1024];
    DWORD size = sizeof(buf);
    DWORD type = 0;
    LONG ok = RegQueryValueExA(hKey, valueName, nullptr, &type, reinterpret_cast<LPBYTE>(buf), &size);
    RegCloseKey(hKey);
    if (ok != ERROR_SUCCESS || (type != REG_SZ && type != REG_EXPAND_SZ)) {
        return false;
    }
    out.assign(buf);
    return !out.empty();
}

static std::string JoinExe(const std::string& dir) {
    if (dir.empty()) return {};
    std::string d = dir;
    while (!d.empty() && (d.back() == '\\' || d.back() == '/')) d.pop_back();
    const std::string a = d + "\\terminal64.exe";
    if (FileLooksLikeMt5Exe(a)) return a;
    const std::string b = d + "\\terminal.exe";
    if (FileLooksLikeMt5Exe(b)) return b;
    return {};
}

// Any broker install: folders whose name contains "metatrader" + "5" (IC Markets, Exness, …)
static std::string ScanProgramFilesForAnyBrokerMt5() {
    const char* roots[] = {
        "C:\\Program Files",
        "C:\\Program Files (x86)",
    };
    for (const char* root : roots) {
        WIN32_FIND_DATAA fd{};
        std::string pattern = std::string(root) + "\\*";
        HANDLE h = FindFirstFileA(pattern.c_str(), &fd);
        if (h == INVALID_HANDLE_VALUE) continue;
        do {
            if (!(fd.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY)) continue;
            if (fd.cFileName[0] == '.') continue;
            std::string lower = ToLower(fd.cFileName);
            // Match "*MetaTrader*5*" or "*MT5*" broker branded installs
            const bool looks_mt5 =
                (lower.find("metatrader") != std::string::npos && lower.find('5') != std::string::npos) ||
                lower.find("mt5") != std::string::npos;
            if (!looks_mt5) continue;
            std::string joined = JoinExe(std::string(root) + "\\" + fd.cFileName);
            if (!joined.empty()) {
                FindClose(h);
                return joined;
            }
        } while (FindNextFileA(h, &fd));
        FindClose(h);
    }
    return {};
}

static void EnumerateRegTerminals(HKEY root, const char* subkey, std::vector<std::string>& out) {
    HKEY hKey = nullptr;
    if (RegOpenKeyExA(root, subkey, 0, KEY_READ, &hKey) != ERROR_SUCCESS) return;
    for (DWORD i = 0; i < 64; ++i) {
        char name[256];
        DWORD nameLen = sizeof(name);
        if (RegEnumKeyExA(hKey, i, name, &nameLen, nullptr, nullptr, nullptr, nullptr) != ERROR_SUCCESS) {
            break;
        }
        std::string child = std::string(subkey) + "\\" + name;
        std::string val;
        for (const char* vn : {"Path", "InstallPath", "EXE", "Folder", "DataPath"}) {
            if (QueryRegPath(root, child.c_str(), vn, val)) {
                if (FileLooksLikeMt5Exe(val)) {
                    out.push_back(val);
                } else {
                    auto j = JoinExe(val);
                    if (!j.empty()) out.push_back(j);
                }
            }
        }
    }
    RegCloseKey(hKey);
}
#endif

std::string DiscoverMt5ExecutablePath() {
#ifdef _WIN32
    std::string regVal;
    const char* keys[] = {
        "Software\\MetaQuotes\\Terminal",
        "Software\\MetaQuotes\\MetaTrader 5",
        "Software\\MetaQuotes\\MetaTrader 5 Terminal",
    };
    const char* names[] = {"Path", "InstallPath", "EXE", "Folder", "DataPath"};
    for (const char* key : keys) {
        for (const char* name : names) {
            if (QueryRegPath(HKEY_CURRENT_USER, key, name, regVal)) {
                if (FileLooksLikeMt5Exe(regVal)) return regVal;
                std::string joined = JoinExe(regVal);
                if (!joined.empty()) return joined;
            }
            if (QueryRegPath(HKEY_LOCAL_MACHINE, key, name, regVal)) {
                if (FileLooksLikeMt5Exe(regVal)) return regVal;
                std::string joined = JoinExe(regVal);
                if (!joined.empty()) return joined;
            }
        }
    }

    // Nested terminal IDs under Software\MetaQuotes\Terminal\<hash>
    std::vector<std::string> found;
    EnumerateRegTerminals(HKEY_CURRENT_USER, "Software\\MetaQuotes\\Terminal", found);
    EnumerateRegTerminals(HKEY_LOCAL_MACHINE, "Software\\MetaQuotes\\Terminal", found);
    if (!found.empty()) return found.front();

    // Generic Program Files scan — works for any broker-branded MT5 folder
    auto scanned = ScanProgramFilesForAnyBrokerMt5();
    if (!scanned.empty()) return scanned;

    const char* guesses[] = {
        "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
        "C:\\Program Files\\MetaTrader 5\\terminal.exe",
        "C:\\Program Files (x86)\\MetaTrader 5\\terminal64.exe",
    };
    for (const char* g : guesses) {
        if (FileLooksLikeMt5Exe(g)) return g;
    }
#endif
    return {};
}
