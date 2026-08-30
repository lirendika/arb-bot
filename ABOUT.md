# uniswap-v3 range-edge bot

Passive monitor for a Uniswap V3 pool whose liquidity sits in a narrow range.
When quote-token liquidity appears above the position's lower tick, it sells the
exact base-token amount needed to drain it back to the tick, then stops.

All targets (chain, RPC, pool, token, router, tick anchor) are supplied at runtime
via environment variables. Nothing is hardcoded.

    POOL_ADDR TOKEN_ADDR QUOTE_ADDR ROUTER_ADDR
    CHAIN_ID TICK_FLOOR EXPECTED_LG ARC_RPC PRIVATE_KEY

Safety: halts if the pool's liquidity structure changes, backs off after
consecutive failed races, caps fills per day, and sizes every swap with
amountOutMinimum so a lost race reverts instead of filling badly.
