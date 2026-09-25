#include "http_mini.hpp"
#include "launcher.hpp"
#include "mt5_path.hpp"
#include "orders.hpp"

#include <cctype>
#include <cstdlib>
#include <iostream>
#include <string>
#include <vector>

#ifdef _WIN32
#include <windows.h>
#include <shlobj.h>
#endif

static std::string JsonGetString(const std::string& body, const std::string& key) {
    const std::string pat = "\"" + key + "\"";
    auto pos = body.find(pat);
    if (pos == std::string::npos) return {};
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return {};
    pos = body.find('"', pos + 1);
    if (pos == std::string::npos) return {};
    ++pos;
    std::string out;
    while (pos < body.size()) {
        char c = body[pos++];
        if (c == '\\' && pos < body.size()) {
            out.push_back(body[pos++]);
            continue;
        }
        if (c == '"') break;
        out.push_back(c);
    }
    return out;
}

static int JsonGetInt(const std::string& body, const std::string& key, int def_v) {
    const std::string pat = "\"" + key + "\"";
    auto pos = body.find(pat);
    if (pos == std::string::npos) return def_v;
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return def_v;
    ++pos;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t')) ++pos;
    try {
        size_t end = pos;
        while (end < body.size() && (isdigit(static_cast<unsigned char>(body[end])) || body[end] == '-')) {
            ++end;
        }
        if (end == pos) return def_v;
        return std::stoi(body.substr(pos, end - pos));
    } catch (...) {
        return def_v;
    }
}

static double JsonGetDouble(const std::string& body, const std::string& key, double def_v) {
    const std::string pat = "\"" + key + "\"";
    auto pos = body.find(pat);
    if (pos == std::string::npos) return def_v;
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return def_v;
    ++pos;
    while (pos < body.size() && (body[pos] == ' ' || body[pos] == '\t')) ++pos;
    try {
        size_t idx = 0;
        double v = std::stod(body.substr(pos), &idx);
        (void)idx;
        return v;
    } catch (...) {
        return def_v;
    }
}

static bool JsonGetBool(const std::string& body, const std::string& key, bool def_v) {
    const std::string pat = "\"" + key + "\"";
    auto pos = body.find(pat);
    if (pos == std::string::npos) return def_v;
    pos = body.find(':', pos + pat.size());
    if (pos == std::string::npos) return def_v;
    auto slice = body.substr(pos, 16);
    if (slice.find("true") != std::string::npos) return true;
    if (slice.find("false") != std::string::npos) return false;
    return def_v;
}

static std::string EscapeJson(const std::string& s) {
    std::string o;
    for (char c : s) {
        if (c == '\\') o += "\\\\";
        else if (c == '"') o += "\\\"";
        else if (c == '\n') o += "\\n";
        else o += c;
    }
    return o;
}

// Split top-level JSON objects inside an array value for key (e.g. "accounts").
static std::vector<std::string> JsonArrayObjects(const std::string& body, const std::string& key) {
    std::vector<std::string> out;
    const std::string pat = "\"" + key + "\"";
    auto pos = body.find(pat);
    if (pos == std::string::npos) return out;
    pos = body.find('[', pos + pat.size());
    if (pos == std::string::npos) return out;
    ++pos;
    int depth = 0;
    size_t start = std::string::npos;
    for (; pos < body.size(); ++pos) {
        char c = body[pos];
        if (c == '{') {
            if (depth == 0) start = pos;
            ++depth;
        } else if (c == '}') {
            --depth;
            if (depth == 0 && start != std::string::npos) {
                out.push_back(body.substr(start, pos - start + 1));
                start = std::string::npos;
            }
        } else if (c == ']' && depth == 0) {
            break;
        }
    }
    return out;
}

static std::string DefaultDataRoot() {
#ifdef _WIN32
    char path[MAX_PATH];
    if (SUCCEEDED(SHGetFolderPathA(nullptr, CSIDL_LOCAL_APPDATA, nullptr, 0, path))) {
        return std::string(path) + "\\Hammer\\API\\terminals";
    }
#endif
    return "API/terminals";
}

int main(int argc, char** argv) {
    const char* host = "127.0.0.1";
    int port = 17101;
    std::string data_root = DefaultDataRoot();
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if ((a == "--port" || a == "-p") && i + 1 < argc) {
            port = std::atoi(argv[++i]);
        } else if ((a == "--data" || a == "-d") && i + 1 < argc) {
            data_root = argv[++i];
        }
    }

    TerminalLauncher launcher(data_root);
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if ((a == "--mt5" || a == "-m") && i + 1 < argc) {
            launcher.set_default_mt5_path(argv[++i]);
        }
    }

    std::cout << "[hammer_mt5_engine] data_root=" << data_root << "\n";
    std::cout << "[hammer_mt5_engine] mt5=" << launcher.default_mt5_path() << "\n";
    std::cout << "[hammer_mt5_engine] listening http://" << host << ":" << port << "\n";

    auto handler = [&](const HttpRequest& req) -> HttpResponse {
        HttpResponse resp;
        if (req.method == "GET" && (req.path == "/health" || req.path == "/")) {
            resp.body = std::string("{\"ok\":true,\"engine\":\"hammer_mt5_engine\",\"mt5\":\"") +
                        EscapeJson(launcher.default_mt5_path()) + "\"}";
            return resp;
        }
        if (req.method == "GET" && req.path == "/discover") {
            auto p = DiscoverMt5ExecutablePath();
            if (!p.empty()) launcher.set_default_mt5_path(p);
            resp.body = std::string("{\"path\":\"") + EscapeJson(p) + "\"}";
            return resp;
        }
        if (req.method == "GET" && req.path == "/status") {
            resp.body = launcher.status_json();
            return resp;
        }
        if (req.method == "POST" && req.path == "/engine/set_path") {
            auto p = JsonGetString(req.body, "path");
            if (!FileLooksLikeMt5Exe(p)) {
                resp.status = 400;
                resp.body = "{\"ok\":false,\"message\":\"invalid terminal path\"}";
                return resp;
            }
            launcher.set_default_mt5_path(p);
            resp.body = "{\"ok\":true,\"path\":\"" + EscapeJson(p) + "\"}";
            return resp;
        }
        if (req.method == "POST" && req.path == "/account/connect") {
            auto id = JsonGetString(req.body, "account_id");
            if (id.empty()) id = JsonGetString(req.body, "id");
            if (id.empty()) id = JsonGetString(req.body, "name");
            auto path = JsonGetString(req.body, "terminal_path");
            if (path.empty()) path = JsonGetString(req.body, "mt5_path");
            auto login = JsonGetString(req.body, "login");
            auto password = JsonGetString(req.body, "password");
            auto server = JsonGetString(req.body, "server");
            auto lr = launcher.start_account(id, path, login, password, server);
            resp.status = lr.ok ? 200 : 500;
            resp.body = std::string("{\"ok\":") + (lr.ok ? "true" : "false") +
                        ",\"pid\":" + std::to_string(lr.pid) +
                        ",\"portable_path\":\"" + EscapeJson(lr.portable_path) +
                        "\",\"message\":\"" + EscapeJson(lr.message) + "\"}";
            return resp;
        }
        if (req.method == "POST" && req.path == "/account/disconnect") {
            auto id = JsonGetString(req.body, "account_id");
            if (id.empty()) id = JsonGetString(req.body, "id");
            auto lr = launcher.stop_account(id);
            resp.status = lr.ok ? 200 : 500;
            resp.body = std::string("{\"ok\":") + (lr.ok ? "true" : "false") +
                        ",\"message\":\"" + EscapeJson(lr.message) + "\"}";
            return resp;
        }
        // Multi-account staggered connect (broker firewall safeguard).
        if (req.method == "POST" && req.path == "/fleet/connect") {
            int stagger = JsonGetInt(req.body, "stagger_ms", 100);
            auto objs = JsonArrayObjects(req.body, "accounts");
            std::vector<AccountConnectSpec> specs;
            specs.reserve(objs.size());
            for (const auto& obj : objs) {
                AccountConnectSpec s;
                s.account_id = JsonGetString(obj, "account_id");
                if (s.account_id.empty()) s.account_id = JsonGetString(obj, "id");
                s.mt5_path = JsonGetString(obj, "terminal_path");
                if (s.mt5_path.empty()) s.mt5_path = JsonGetString(obj, "mt5_path");
                s.login = JsonGetString(obj, "login");
                s.password = JsonGetString(obj, "password");
                s.server = JsonGetString(obj, "server");
                if (!s.account_id.empty()) specs.push_back(std::move(s));
            }
            auto fr = launcher.start_accounts_staggered(specs, stagger);
            std::string results = "[";
            for (size_t i = 0; i < fr.results.size(); ++i) {
                if (i) results += ",";
                const auto& r = fr.results[i];
                results += "{\"ok\":" + std::string(r.ok ? "true" : "false") +
                           ",\"pid\":" + std::to_string(r.pid) +
                           ",\"portable_path\":\"" + EscapeJson(r.portable_path) +
                           "\",\"message\":\"" + EscapeJson(r.message) + "\"}";
            }
            results += "]";
            resp.status = fr.ok ? 200 : 500;
            resp.body = std::string("{\"ok\":") + (fr.ok ? "true" : "false") +
                        ",\"message\":\"" + EscapeJson(fr.message) +
                        "\",\"results\":" + results + "}";
            return resp;
        }
        // Parallel order fan-out (std::async) — not a sequential for-loop.
        if (req.method == "POST" && req.path == "/orders/batch") {
            bool parallel = JsonGetBool(req.body, "parallel", true);
            auto objs = JsonArrayObjects(req.body, "orders");
            std::vector<OrderIntent> orders;
            orders.reserve(objs.size());
            for (const auto& obj : objs) {
                OrderIntent o;
                o.account_id = JsonGetString(obj, "account_id");
                o.symbol = JsonGetString(obj, "symbol");
                o.side = JsonGetString(obj, "side");
                o.volume = JsonGetString(obj, "volume");
                o.order_type = JsonGetString(obj, "order_type");
                if (o.order_type.empty()) o.order_type = "market";
                o.price = JsonGetDouble(obj, "price", 0);
                o.sl = JsonGetDouble(obj, "sl", 0);
                o.tp = JsonGetDouble(obj, "tp", 0);
                o.magic = JsonGetInt(obj, "magic", 0);
                o.comment = JsonGetString(obj, "comment");
                orders.push_back(std::move(o));
            }
            auto br = DispatchOrdersParallel(orders, parallel);
            std::string results = "[";
            for (size_t i = 0; i < br.results.size(); ++i) {
                if (i) results += ",";
                const auto& r = br.results[i];
                results += "{\"account_id\":\"" + EscapeJson(r.account_id) +
                           "\",\"ok\":" + std::string(r.ok ? "true" : "false") +
                           ",\"message\":\"" + EscapeJson(r.message) + "\"}";
            }
            results += "]";
            resp.status = br.ok ? 200 : 500;
            resp.body = std::string("{\"ok\":") + (br.ok ? "true" : "false") +
                        ",\"message\":\"" + EscapeJson(br.message) +
                        "\",\"results\":" + results + "}";
            return resp;
        }
        if (req.method == "POST" && req.path == "/engine/stop_all") {
            auto lr = launcher.stop_all();
            resp.body = std::string("{\"ok\":true,\"message\":\"") + EscapeJson(lr.message) + "\"}";
            return resp;
        }
        resp.status = 404;
        resp.body = "{\"ok\":false,\"message\":\"not found\"}";
        return resp;
    };

    std::string err;
    if (!RunHttpServer(host, port, handler, err)) {
        std::cerr << "[hammer_mt5_engine] FATAL " << err << "\n";
        return 1;
    }
    return 0;
}
