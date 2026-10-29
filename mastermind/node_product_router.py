"""
mastermind/node_product_router.py — Pinteresto V4 Phase 1

LangGraph node that runs the Product Router for each account's strategy,
right after board selection and before the agent executor publishes.

Graph position:
  ... → node_cmo_mastermind → node_board_selector → node_product_router
      → node_agent_executor

Reads : a1_cmo_strategy, a2_cmo_strategy
Writes: into each strategy dict —
          destination_url, product_id, product_confidence,
          product_method, product_reason, product_cta
        (empty destination_url = pin posts clean, by design)

agent.py publish_next_pin() reads strategy["destination_url"] and passes it
to tools/make_webhook.py, where it takes priority over the blog URL. If this
node is ever skipped, publish_next_pin() runs the router itself as a
fallback, so a pin can never lose its product link to a wiring gap.

Routing itself lives in mastermind/product_router.py (rules + LLM judge;
products come from the `Products` Google Sheet tab). Decisions are logged
to the `Product_Router_Log` sheet tab (best-effort).
"""

import asyncio
import logging

from mastermind.product_router import log_router_decision, route_product
from mastermind.state import MastermindState

logger = logging.getLogger(__name__)


def _route_for_account(strategy: dict, account_key: str) -> dict:
    """Run the router for one account and inject the decision. Never raises."""
    if not strategy:
        return strategy
    try:
        decision = route_product(strategy, account_key=account_key)
        log_router_decision(strategy, decision, account_key=account_key)
        logger.info(
            f"🧭 [ProductRouter] {account_key} → "
            f"{decision['product_id'] or 'NONE (clean pin)'} "
            f"@ {decision['product_confidence']} via {decision['product_method']} "
            f"— {decision['product_reason']}"
        )
        return {**strategy, **decision}
    except Exception as e:  # route_product already never raises; belt & braces
        logger.error(f"❌ [ProductRouter] {account_key} failed (pin posts clean): {e}")
        return {**strategy, "destination_url": "", "product_id": ""}


async def node_product_router(state: MastermindState) -> dict:
    """
    Node — Product Router (V4 Phase 1).

    Account activity follows cycle_trigger exactly like the board selector.
    The router is a sync (possibly LLM-calling) step, so it runs in a thread
    to keep the event loop free.
    """
    trigger = state.get("cycle_trigger", "")
    only_a1 = "account1" in trigger and "account2" not in trigger
    only_a2 = "account2" in trigger and "account1" not in trigger
    run_a1 = not only_a2
    run_a2 = not only_a1

    logger.info(f"🧭 [ProductRouter Node] trigger={trigger} | run_a1={run_a1} run_a2={run_a2}")

    a1_strategy = dict(state.get("a1_cmo_strategy") or {})
    a2_strategy = dict(state.get("a2_cmo_strategy") or {})

    if run_a1 and a1_strategy:
        a1_strategy = await asyncio.to_thread(_route_for_account, a1_strategy, "account_1")
    if run_a2 and a2_strategy:
        a2_strategy = await asyncio.to_thread(_route_for_account, a2_strategy, "account_2")

    return {
        "a1_cmo_strategy": a1_strategy,
        "a2_cmo_strategy": a2_strategy,
    }
