#pragma once

#include <functional>
#include <string>

struct HttpRequest {
    std::string method;
    std::string path;
    std::string body;
};

struct HttpResponse {
    int status = 200;
    std::string content_type = "application/json";
    std::string body;
};

using HttpHandler = std::function<HttpResponse(const HttpRequest&)>;

// Blocking HTTP/1.1 loop on 127.0.0.1:port (Windows WinSock / POSIX sockets).
bool RunHttpServer(const std::string& host, int port, const HttpHandler& handler, std::string& err);
