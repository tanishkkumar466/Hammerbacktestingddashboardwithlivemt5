#pragma once

#include <cstdint>
#include <string>
#include <vector>

struct OrderIntent {
    std::string account_id;
    std::string symbol;
    std::string side;       // buy | sell
    std::string volume;
    std::string order_type; // market | limit
    double price = 0;
    double sl = 0;
    double tp = 0;
    int magic = 0;
    std::string comment;
};

struct OrderResult {
    std::string account_id;
    bool ok = false;
    std::string message;
};

struct BatchOrderResult {
    bool ok = false;
    std::string message;
    std::vector<OrderResult> results;
};

// Fire all orders via thread pool (std::async) so N accounts are not sequential.
// parallel=true → concurrent; parallel=false → sequential (debug only).
BatchOrderResult DispatchOrdersParallel(const std::vector<OrderIntent>& orders, bool parallel);
