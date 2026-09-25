#include "http_mini.hpp"

#include <cstring>
#include <sstream>
#include <vector>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
using socket_t = SOCKET;
static constexpr socket_t kInvalid = INVALID_SOCKET;
static void close_sock(socket_t s) { closesocket(s); }
#else
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>
using socket_t = int;
static constexpr socket_t kInvalid = -1;
static void close_sock(socket_t s) { ::close(s); }
#endif

static bool read_request(socket_t client, HttpRequest& req) {
    std::string data;
    char buf[4096];
    while (true) {
#ifdef _WIN32
        int n = recv(client, buf, sizeof(buf), 0);
#else
        ssize_t n = recv(client, buf, sizeof(buf), 0);
#endif
        if (n <= 0) break;
        data.append(buf, buf + n);
        if (data.find("\r\n\r\n") != std::string::npos) break;
        if (data.size() > 1024 * 1024) break;
    }
    if (data.empty()) return false;
    std::istringstream ss(data);
    ss >> req.method >> req.path;
    auto pos = data.find("\r\n\r\n");
    if (pos != std::string::npos) {
        req.body = data.substr(pos + 4);
    }
    // If Content-Length remains, try one more read
    auto cl = data.find("Content-Length:");
    if (cl != std::string::npos) {
        size_t len = static_cast<size_t>(atoi(data.c_str() + cl + 15));
        while (req.body.size() < len) {
#ifdef _WIN32
            int n = recv(client, buf, sizeof(buf), 0);
#else
            ssize_t n = recv(client, buf, sizeof(buf), 0);
#endif
            if (n <= 0) break;
            req.body.append(buf, buf + n);
        }
        if (req.body.size() > len) req.body.resize(len);
    }
    return !req.method.empty();
}

static void send_response(socket_t client, const HttpResponse& resp) {
    std::ostringstream oss;
    oss << "HTTP/1.1 " << resp.status << " OK\r\n"
        << "Content-Type: " << resp.content_type << "\r\n"
        << "Content-Length: " << resp.body.size() << "\r\n"
        << "Connection: close\r\n\r\n"
        << resp.body;
    const std::string out = oss.str();
    send(client, out.data(), static_cast<int>(out.size()), 0);
}

bool RunHttpServer(const std::string& host, int port, const HttpHandler& handler, std::string& err) {
#ifdef _WIN32
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        err = "WSAStartup failed";
        return false;
    }
#endif
    socket_t server = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (server == kInvalid) {
        err = "socket() failed";
        return false;
    }
    int yes = 1;
    setsockopt(server, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&yes), sizeof(yes));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<uint16_t>(port));
#ifdef _WIN32
    inet_pton(AF_INET, host.c_str(), &addr.sin_addr);
#else
    inet_pton(AF_INET, host.c_str(), &addr.sin_addr);
#endif
    if (bind(server, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        err = "bind() failed — is port " + std::to_string(port) + " in use?";
        close_sock(server);
        return false;
    }
    if (listen(server, 16) != 0) {
        err = "listen() failed";
        close_sock(server);
        return false;
    }

    for (;;) {
        sockaddr_in peer{};
#ifdef _WIN32
        int plen = sizeof(peer);
#else
        socklen_t plen = sizeof(peer);
#endif
        socket_t client = accept(server, reinterpret_cast<sockaddr*>(&peer), &plen);
        if (client == kInvalid) continue;
        HttpRequest req;
        if (read_request(client, req)) {
            HttpResponse resp = handler(req);
            send_response(client, resp);
        }
        close_sock(client);
    }
}
