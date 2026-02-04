from dataclasses import dataclass
import json
import bittensor as bt
from bittensor.core.extrinsics.pallets.base import Call


@dataclass
class SubnetConfig:
    netuid: int
    limit_price: bt.Balance
    activation_price: bt.Balance
    validator_hotkey: str | None = None
    amount_tao_to_stake: bt.Balance | None = None
    call: Call | None = None

    def __post_init__(self) -> None:
        self.netuid = int(self.netuid)
        self.limit_price = bt.Balance.from_tao(float(self.limit_price), netuid=0)
        self.activation_price = bt.Balance.from_tao(
            float(self.activation_price), netuid=0
        )
        self.validator_hotkey = (
            self.validator_hotkey
            if self.validator_hotkey is not None
            else "5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u"
        )
        self.amount_tao_to_stake = bt.Balance.from_tao(
            float(
                self.amount_tao_to_stake
                if self.amount_tao_to_stake is not None
                else "1.0"
            ),
            netuid=0,
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
