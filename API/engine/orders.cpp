#include "orders.hpp"

#include <chrono>
#include <future>
#include <sstream>
#include <thread>
#include <vector>

static OrderResult ExecuteOneOrder(const OrderIntent& o) {
    OrderResult r;
    r.account_id = o.account_id;
    if (o.account_id.empty() || o.symbol.empty()) {
        r.message = "account_id and symbol required";
        return r;
    }
    if (o.side != "buy" && o.side != "sell") {
        r.message = "side must be buy or sell";
        return r;
    }
    // Structural fan-out is ready. Real broker fill binds later via MtApi /
    // terminal bridge per portable account path. For now we acknowledge the
    // parallel dispatch contract so the Python desk can wire strategies.
    std::ostringstream msg;
    msg << "queued " << o.side << " " << o.volume << " " << o.symbol
        << " magic=" << o.magic << " [" << o.order_type << "]";
    r.ok = true;
    r.message = msg.str();
    return r;
}

BatchOrderResult DispatchOrdersParallel(const std::vector<OrderIntent>& orders, bool parallel) {
    BatchOrderResult out;
    if (orders.empty()) {
        out.ok = true;
        out.message = "no orders";
        return out;
    }

    if (!parallel || orders.size() == 1) {
        for (const auto& o : orders) {
            out.results.push_back(ExecuteOneOrder(o));
        }
    } else {
        // Same-millisecond window: launch all workers then join.
        std::vector<std::future<OrderResult>> futs;
        futs.reserve(orders.size());
        for (const auto& o : orders) {
            futs.push_back(std::async(std::launch::async, ExecuteOneOrder, o));
        }
        for (auto& f : futs) {
            out.results.push_back(f.get());
        }
    }

    size_t ok_n = 0;
    for (const auto& r : out.results) {
        if (r.ok) ++ok_n;
    }
    out.ok = ok_n == out.results.size();
    std::ostringstream msg;
    msg << "dispatched " << out.results.size() << " orders ("
        << (parallel ? "parallel" : "sequential") << ") ok=" << ok_n;
    out.message = msg.str();
    return out;
}
