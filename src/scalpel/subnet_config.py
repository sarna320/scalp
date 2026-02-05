from dataclasses import dataclass
import json
import bittensor as bt
from bittensor.core.extrinsics.pallets.base import Call


@dataclass
class SubnetConfig:
    # Required fields (no defaults)
    netuid: int
    limit_price_buy: bt.Balance
    activation_price_buy: bt.Balance

    # Profit multiplier (e.g., 1.05 = 5% profit target)
    # activation_price = avg_entry_price * pct_profit
    # Must be > 1.0
    pct_profit: float

    # Slippage tolerance for limit orders (e.g., 0.05 = 5% slippage)
    # limit_price = activation_price * (1 - slippage_sell_pct)
    # Must be between 0 and 1
    #
    # WARNING: Net profit = pct_profit * (1 - slippage_sell_pct)
    # Example GOOD: pct_profit=1.10, slippage=0.05 → net=1.045 (+4.5% profit)
    # Example BAD:  pct_profit=1.05, slippage=0.05 → net=0.9975 (-0.25% LOSS!)
    slippage_sell_pct: float

    # Optional fields (with defaults)
    validator_hotkey: str | None = None
    amount_tao_to_stake_buy: bt.Balance | None = None
    call_buy: Call | None = None
    call_sell: Call | None = None

    # Future sell functionality (currently unused)
    # activation_price_sell: bt.Balance | None = None
    # limit_price_sell: bt.Balance | None = None
    # amount_alpha_to_unstake: bt.Balance | None = None

    def __post_init__(self) -> None:
        self.netuid = int(self.netuid)

        self.limit_price_buy = bt.Balance.from_tao(
            float(self.limit_price_buy), netuid=0
        )
        self.activation_price_buy = bt.Balance.from_tao(
            float(self.activation_price_buy), netuid=0
        )

        self.validator_hotkey = (
            self.validator_hotkey
            if self.validator_hotkey is not None
            else "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"
        )
        self.amount_tao_to_stake_buy = bt.Balance.from_tao(
            float(
                self.amount_tao_to_stake_buy
                if self.amount_tao_to_stake_buy is not None
                else "1.0"
            ),
            netuid=0,
        )
        self.pct_profit = float(self.pct_profit)
        self.slippage_sell_pct = float(self.slippage_sell_pct)

        # Validate configuration to prevent losses
        self._validate_config()

    def _validate_config(self) -> None:
        """
        Validates subnet configuration to prevent losses from bad parameters.

        Raises:
            ValueError: If configuration would result in guaranteed losses or invalid values.
        """
        # Validate buy prices
        if self.limit_price_buy <= self.activation_price_buy:
            raise ValueError(
                f"Subnet {self.netuid}: limit_price_buy ({self.limit_price_buy.tao}) "
                f"must be > activation_price_buy ({self.activation_price_buy.tao}). "
                f"Limit price is the max you'll pay."
            )

        # Validate profit percentage
        if self.pct_profit <= 1.0:
            raise ValueError(
                f"Subnet {self.netuid}: pct_profit ({self.pct_profit}) must be > 1.0. "
                f"Example: 1.05 means 5% profit."
            )

        # Validate slippage percentage
        if not (0 < self.slippage_sell_pct < 1.0):
            raise ValueError(
                f"Subnet {self.netuid}: slippage_sell_pct ({self.slippage_sell_pct}) "
                f"must be between 0 and 1. Example: 0.05 means 5% slippage."
            )

        # Critical: Check if pct_profit and slippage would result in a loss
        # Net multiplier after profit and slippage
        net_multiplier = self.pct_profit * (1 - self.slippage_sell_pct)

        if net_multiplier <= 1.0:
            min_required_profit = 1.0 / (1 - self.slippage_sell_pct)
            raise ValueError(
                f"Subnet {self.netuid}: Configuration would result in LOSS!\n"
                f"  pct_profit: {self.pct_profit:.4f}\n"
                f"  slippage_sell_pct: {self.slippage_sell_pct:.4f}\n"
                f"  Net multiplier: {net_multiplier:.4f} (≤ 1.0 means loss)\n"
                f"  \n"
                f"  Example with entry price 1.0:\n"
                f"    - Buy at: 1.0\n"
                f"    - Activation: {1.0 * self.pct_profit:.4f}\n"
                f"    - Limit sell: {net_multiplier:.4f}\n"
                f"    - Result: {(net_multiplier - 1.0) * 100:.2f}% loss\n"
                f"  \n"
                f"  Fix: Set pct_profit >= {min_required_profit:.4f} "
                f"(or reduce slippage_sell_pct < {1.0 - (1.0 / self.pct_profit):.4f})"
            )

        # Warn if profit margin is too small (less than 1%)
        net_profit_pct = (net_multiplier - 1.0) * 100
        if net_profit_pct < 1.0:
            bt.logging.warning(
                f"Subnet {self.netuid}: Low profit margin! "
                f"Net profit after slippage: {net_profit_pct:.2f}%. "
                f"Consider increasing pct_profit or reducing slippage_sell_pct."
            )


def get_subnet_configs() -> list[SubnetConfig]:
    configs = []
    with open("subnets_config.json", "r") as file:
        subnets_config = json.load(file)
        for subnet_config in subnets_config:
            config = SubnetConfig(**subnet_config)
            bt.logging.debug(f"{config}")
            configs.append(config)
    return configs


if __name__ == "__main__":
    get_subnet_configs()
