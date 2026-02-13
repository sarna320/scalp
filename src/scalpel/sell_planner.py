from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, getcontext
import math
from typing import Optional

import bittensor as bt
from bittensor.core.chain_data.dynamic_info import DynamicInfo

getcontext().prec = 50

RAO_PER_TAO = 10**9

# 0.05% = 500 ppm (parts per million)
PPM_DEN = 1_000_000
ALPHA_FEE_PPM = 500


@dataclass(frozen=True, slots=True)
class SellPlan:
    """Computed plan for a profitable (targeted) sell."""

    netuid: int

    activation_price: bt.Balance  # TAO/Alpha (price threshold to submit)
    limit_price: bt.Balance  # TAO/Alpha (worst acceptable fill price)

    amount_alpha_to_sell_rao: int  # gross alpha to put into extrinsic
    amount_alpha_into_pool_rao: int  # net alpha that reaches pool (after alpha-fee)

    expected_tao_out_rao: int  # AMM estimate (does NOT include flat extrinsic fee)
    expected_tao_out_after_flat_fee_rao: int

    # Extra debug fields (optional but useful)
    expected_fill_pct_of_position: float
    assumed_cost_basis_rao: int
    required_proceeds_rao: int


def ceil_div(a: int, b: int) -> int:
    """Ceiling division for integers."""
    if b <= 0:
        raise ValueError("b must be > 0")
    return (a + b - 1) // b


def alpha_fee_rao(alpha_gross_rao: int, fee_ppm: int = ALPHA_FEE_PPM) -> int:
    """Alpha fee in rao (rounded up, conservative)."""
    if alpha_gross_rao <= 0:
        return 0
    return ceil_div(alpha_gross_rao * fee_ppm, PPM_DEN)


def net_alpha_into_pool_rao(alpha_gross_rao: int) -> int:
    """Net alpha that reaches the pool after alpha-fee."""
    return max(0, alpha_gross_rao - alpha_fee_rao(alpha_gross_rao))


def max_gross_alpha_for_net_limit(net_limit_rao: int) -> int:
    """
    Max gross alpha such that net_alpha_into_pool_rao(gross) <= net_limit_rao.
    Uses a tight integer conversion to handle fee rounding.
    """
    if net_limit_rao <= 0:
        return 0

    denom = PPM_DEN - ALPHA_FEE_PPM

    # Approximate start (floor)
    gross = (net_limit_rao * PPM_DEN) // denom

    # Nudge upward while still valid
    while True:
        cand = gross + 1
        if net_alpha_into_pool_rao(cand) <= net_limit_rao:
            gross = cand
            continue
        break

    while gross > 0 and net_alpha_into_pool_rao(gross) > net_limit_rao:
        gross -= 1

    return gross


def spot_price_rao_from_reserves(alpha_in_rao: int, tao_in_rao: int) -> int:
    """
    Spot price in (TAO/Alpha) scaled to rao:
      price_tao = tao_in / alpha_in (token units)
      price_rao = price_tao * 1e9 = (tao_in_rao * 1e9) / alpha_in_rao
    """
    if alpha_in_rao <= 0:
        return 0
    return (tao_in_rao * RAO_PER_TAO) // alpha_in_rao


def compute_activation_and_limit_for_fill(
    *,
    position_total_alpha_rao: int,
    position_total_tao_spent_rao: int,
    gross_alpha_fill_rao: int,
    pct_profit: float,
    slippage_sell_pct: float,
    flat_fee_sell_rao: int,
) -> tuple[int, int, int, int]:
    """
    Compute activation & limit price (rao) for a given assumed gross fill amount.
    Returns:
      (activation_price_rao, limit_price_rao, assumed_cost_basis_rao, required_proceeds_rao)

    Guarantee:
      If gross_alpha_fill_rao fills at >= limit_price_rao in this extrinsic,
      then net outcome >= pct_profit after alpha-fee and flat fee.
    """
    if position_total_alpha_rao <= 0:
        raise ValueError("Position has no alpha.")
    if gross_alpha_fill_rao <= 0:
        raise ValueError("gross_alpha_fill_rao must be > 0")
    if gross_alpha_fill_rao > position_total_alpha_rao:
        gross_alpha_fill_rao = position_total_alpha_rao

    if pct_profit <= 1.0:
        raise ValueError("pct_profit must be > 1.0")
    if not (0.0 <= slippage_sell_pct < 1.0):
        raise ValueError("slippage_sell_pct must be in [0, 1)")

    # Alpha fee is paid in alpha; conservative rounding up.
    fee_alpha = alpha_fee_rao(gross_alpha_fill_rao)
    effective_alpha = gross_alpha_fill_rao - fee_alpha
    if effective_alpha <= 0:
        raise ValueError("Effective alpha after fee is <= 0")

    # Avg-cost cost basis for the sold part (conservative rounding up).
    # cost_basis = ceil(total_cost * sold_alpha / total_alpha)
    assumed_cost_basis = ceil_div(
        position_total_tao_spent_rao * gross_alpha_fill_rao,
        position_total_alpha_rao,
    )

    # Required proceeds to meet profit + cover flat fee
    required_proceeds = int(
        (Decimal(assumed_cost_basis) * Decimal(str(pct_profit))).to_integral_value(
            rounding=ROUND_CEILING
        )
    ) + int(flat_fee_sell_rao)

    # Minimal limit price so that:
    # floor(limit_price * effective_alpha / 1e9) >= required_proceeds
    limit_price_rao = ceil_div(required_proceeds * RAO_PER_TAO, effective_alpha)

    # Activation to tolerate slippage down to limit
    sl_mult = Decimal("1") - Decimal(str(slippage_sell_pct))
    if sl_mult <= 0:
        raise ValueError("Invalid slippage multiplier")
    activation_price_rao = int(
        (Decimal(limit_price_rao) / sl_mult).to_integral_value(rounding=ROUND_CEILING)
    )

    return activation_price_rao, limit_price_rao, assumed_cost_basis, required_proceeds


def estimate_max_fill_under_limit(
    *,
    dynamic: DynamicInfo,
    limit_price_rao: int,
    max_gross_sell_rao: int,
) -> tuple[int, int, int]:
    """
    Estimate max gross alpha fill such that final spot price stays >= limit_price.
    Returns:
      (gross_fill_rao, net_alpha_into_pool_rao, expected_tao_out_rao)

    Notes:
      - Uses constant-product pool: k = alpha_in * tao_in (in rao units).
      - Assumes trade continues until final spot reaches the limit.
      - Does NOT include flat extrinsic fee.
    """
    A = int(dynamic.alpha_in.rao)
    T = int(dynamic.tao_in.rao)
    if A <= 0 or T <= 0 or limit_price_rao <= 0:
        return 0, 0, 0

    k = A * T

    # final_spot_price_rao = (k * 1e9) / newA^2  >= limit_price_rao
    rhs = (k * RAO_PER_TAO) // limit_price_rao
    if rhs <= 0:
        return 0, 0, 0

    max_newA = math.isqrt(rhs)
    net_alpha_limit = max(0, max_newA - A)
    if net_alpha_limit <= 0:
        return 0, 0, 0

    gross_fill_cap = max_gross_alpha_for_net_limit(net_alpha_limit)
    gross_fill = min(max_gross_sell_rao, gross_fill_cap)
    net_alpha = net_alpha_into_pool_rao(gross_fill)
    if net_alpha <= 0:
        return 0, 0, 0

    newA = A + net_alpha
    newT = k // newA
    tao_out = max(0, T - newT)

    return gross_fill, net_alpha, tao_out


def build_sell_plan(
    *,
    netuid: int,
    dynamic: DynamicInfo,
    position_total_alpha_rao: int,
    position_total_tao_spent_rao: int,
    pct_profit: float,
    slippage_sell_pct: float,
    flat_fee_sell_rao: int,
    min_gross_fill_rao: int = 0,
    max_sell_alpha_rao: int | None = None,
    max_iters: int = 20,
) -> Optional[SellPlan]:
    """
    Builds a self-consistent sell plan:
      - chooses amount_alpha_to_sell_rao as the estimated max fill under the computed limit,
      - computes limit/activation so that pct_profit holds for that amount,
      - iterates until (assumed_fill == estimated_fill) or converges to 0.

    Returns None if no profitable / meaningful fill exists under current pool state.
    """
    if position_total_alpha_rao <= 0:
        return None

    # Cap sell amount if max_sell_alpha_rao is set
    sell_cap = position_total_alpha_rao
    if max_sell_alpha_rao is not None:
        sell_cap = min(max_sell_alpha_rao, position_total_alpha_rao)

    # Start from sell cap (full position or partial)
    assumed_gross_fill = sell_cap

    for _ in range(max_iters):
        activation_rao, limit_rao, cost_basis_rao, required_proceeds_rao = (
            compute_activation_and_limit_for_fill(
                position_total_alpha_rao=position_total_alpha_rao,
                position_total_tao_spent_rao=position_total_tao_spent_rao,
                gross_alpha_fill_rao=assumed_gross_fill,
                pct_profit=pct_profit,
                slippage_sell_pct=slippage_sell_pct,
                flat_fee_sell_rao=flat_fee_sell_rao,
            )
        )

        est_gross_fill, est_net_alpha, est_tao_out = estimate_max_fill_under_limit(
            dynamic=dynamic,
            limit_price_rao=limit_rao,
            max_gross_sell_rao=sell_cap,
        )

        if est_gross_fill <= 0:
            return None

        if min_gross_fill_rao and est_gross_fill < min_gross_fill_rao:
            return None

        # Fixed point reached
        if est_gross_fill == assumed_gross_fill:
            expected_after_fee = max(0, est_tao_out - flat_fee_sell_rao)
            fill_pct = 100.0 * est_gross_fill / position_total_alpha_rao

            return SellPlan(
                netuid=netuid,
                activation_price=bt.Balance.from_rao(activation_rao, netuid=0),
                limit_price=bt.Balance.from_rao(limit_rao, netuid=0),
                amount_alpha_to_sell_rao=est_gross_fill,
                amount_alpha_into_pool_rao=est_net_alpha,
                expected_tao_out_rao=est_tao_out,
                expected_tao_out_after_flat_fee_rao=expected_after_fee,
                expected_fill_pct_of_position=fill_pct,
                assumed_cost_basis_rao=cost_basis_rao,
                required_proceeds_rao=required_proceeds_rao,
            )

        # Monotone decrease; update and continue
        if est_gross_fill < assumed_gross_fill:
            assumed_gross_fill = est_gross_fill
            continue

        # Safety: if somehow increased (should be rare), accept it and stop.
        assumed_gross_fill = est_gross_fill
        break

    # If we exit loop without exact equality, do one final plan build for the last assumed fill
    activation_rao, limit_rao, cost_basis_rao, required_proceeds_rao = (
        compute_activation_and_limit_for_fill(
            position_total_alpha_rao=position_total_alpha_rao,
            position_total_tao_spent_rao=position_total_tao_spent_rao,
            gross_alpha_fill_rao=assumed_gross_fill,
            pct_profit=pct_profit,
            slippage_sell_pct=slippage_sell_pct,
            flat_fee_sell_rao=flat_fee_sell_rao,
        )
    )
    est_gross_fill, est_net_alpha, est_tao_out = estimate_max_fill_under_limit(
        dynamic=dynamic,
        limit_price_rao=limit_rao,
        max_gross_sell_rao=sell_cap,
    )
    if est_gross_fill <= 0:
        return None

    expected_after_fee = max(0, est_tao_out - flat_fee_sell_rao)
    fill_pct = 100.0 * est_gross_fill / position_total_alpha_rao

    return SellPlan(
        netuid=netuid,
        activation_price=bt.Balance.from_rao(activation_rao, netuid=0),
        limit_price=bt.Balance.from_rao(limit_rao, netuid=0),
        amount_alpha_to_sell_rao=est_gross_fill,
        amount_alpha_into_pool_rao=est_net_alpha,
        expected_tao_out_rao=est_tao_out,
        expected_tao_out_after_flat_fee_rao=expected_after_fee,
        expected_fill_pct_of_position=fill_pct,
        assumed_cost_basis_rao=cost_basis_rao,
        required_proceeds_rao=required_proceeds_rao,
    )
