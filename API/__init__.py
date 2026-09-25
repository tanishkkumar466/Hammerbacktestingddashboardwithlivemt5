"""
API package — headless MT5 account management + algo bindings (any broker).

Python UI owns the table. C++ hammer_mt5_engine on 127.0.0.1:17101 launches
hidden /portable terminals (staggered fleet) and parallel order fan-out.
Live trading (live_accounts / Live dock) is unchanged — this is a separate path.
"""

from .accounts import (
    ApiAccount,
    ApiAccountStore,
    default_accounts_path,
    load_accounts,
    save_accounts,
)
from .algo_desk import start_api_trading_parallel, stop_api_trading
from .bindings import (
    BindingStore,
    StrategyBinding,
    default_bindings_path,
    load_bindings,
    save_bindings,
)
from .fleet import (
    DEFAULT_STAGGER_MS,
    FleetClient,
    FleetStartPlan,
    OrderIntent,
    build_fleet_plan_from_stores,
    build_order_batch_payload,
)
from .paths import portable_terminal_path
from .preset_meta import (
    enabled_timeframes_from_preset,
    read_preset_file,
    stamp_preset_meta,
)
from .runtime import build_api_trade_start, strategy_from_preset
from .service import (
    HeadlessEngineClient,
    DEFAULT_ENGINE_HOST,
    DEFAULT_ENGINE_PORT,
    build_connect_payload,
    discover_mt5_path_windows,
    list_mt5_installs_windows,
)

__all__ = [
    "ApiAccount",
    "ApiAccountStore",
    "BindingStore",
    "StrategyBinding",
    "FleetClient",
    "FleetStartPlan",
    "OrderIntent",
    "HeadlessEngineClient",
    "DEFAULT_ENGINE_HOST",
    "DEFAULT_ENGINE_PORT",
    "DEFAULT_STAGGER_MS",
    "default_accounts_path",
    "default_bindings_path",
    "load_accounts",
    "save_accounts",
    "load_bindings",
    "save_bindings",
    "build_connect_payload",
    "build_fleet_plan_from_stores",
    "build_order_batch_payload",
    "build_api_trade_start",
    "strategy_from_preset",
    "start_api_trading_parallel",
    "stop_api_trading",
    "portable_terminal_path",
    "discover_mt5_path_windows",
    "list_mt5_installs_windows",
    "enabled_timeframes_from_preset",
    "read_preset_file",
    "stamp_preset_meta",
]
