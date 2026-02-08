from bittensor.core.extrinsics.pallets import SubtensorModule
from bittensor.core.extrinsics.pallets.base import Call
from async_substrate_interface.async_substrate import AsyncExtrinsicReceipt
import bittensor as bt
import asyncio
from pathlib import Path

from scalpel.subnet_config import get_subnet_configs, SubnetConfig
from scalpel.models import StakeAddedEvent, StakeRemovedEvent, Position
from scalpel.positions_persistence import load_positions, save_positions

EXTRINSIC_FEE_TAO_ADD_STAKE = bt.Balance.from_tao(0.000136963)
EXTRINSIC_FEE_TAO_REMOVE_STAKE = bt.Balance.from_tao(0.000135688)
ALPHA_FEE_PCT = 0.0005  # 0.05%


class ScalpRunner:
    def __init__(
        self,
        subtensor: bt.AsyncSubtensor,
        wallet_name: str = "trader",
        positions_path: str = "positions.json",
    ):
        self.subtensor = subtensor
        self.prices: dict[int, bt.Balance]
        self.subnets_config: list[SubnetConfig]
        self.current_block: int
        self.wallet = bt.Wallet(wallet_name)
        bt.logging.info(
            f"Using wallet {self.wallet.coldkey.ss58_address}: {self.wallet}"
        )
        self.positions: dict[int, Position] = {}
        self._persist_lock = asyncio.Lock()
        self.positions_path = Path(positions_path)

    async def run(self):
        await load_positions(self)
        current_block = await self.subtensor.substrate.get_block()
        current_block_hash = current_block.get("header", {}).get("hash")
        self.subnets_config = get_subnet_configs()
        await self.create_calls_buy()  # this calls will always remain the same
        await self.subtensor.substrate.get_block_handler(
            current_block_hash,
            header_only=True,
            subscription_handler=self.handler,
        )

    async def handler(self, block_data: dict):
        self.current_block = block_data["header"]["number"]
        bt.logging.info(f"Current block: [blue]{self.current_block}[/blue]")
        await self.refresh_prices()
        subnets_to_stake = await self.get_subnets_to_stake()
        responses_for_stake = await self.process_subnets(
            subnets_to_stake, call_is_buy=True
        )
        await asyncio.gather(
            *[self.process_response_stake(response) for response in responses_for_stake]
        )

        return True

    async def process_subnets(
        self, subnets: list[SubnetConfig], call_is_buy: bool
    ) -> list[tuple[int, AsyncExtrinsicReceipt] | tuple[None, None]]:
        if len(subnets) == 0:
            return [(None, None)]
        receipts = await asyncio.gather(
            *[
                self.sign_and_send_extrinsic(
                    subnet.call_buy if call_is_buy else subnet.call_sell
                )
                for subnet in subnets
            ]
        )
        return [(subnet.netuid, receipt) for subnet, receipt in zip(subnets, receipts)]

    async def process_response_stake(
        self, response: tuple[int, AsyncExtrinsicReceipt] | tuple[None, None]
    ):
        response_netuid, receipt = response
        if response_netuid is None or receipt is None:
            return

        current_position = self.positions.get(response_netuid)
        if current_position is None:
            self.positions[response_netuid] = Position(response_netuid)
            current_position = self.positions.get(response_netuid)

        # Account for weight-based fee once per extrinsic receipt (whether ok or not)
        current_position.total_tao_spent_rao += EXTRINSIC_FEE_TAO_ADD_STAKE.rao

        ok = await receipt.is_success
        if not ok:
            bt.logging.warning(
                f"Extrinsic failed, adding fee to position: {current_position}"
            )
            return

        bt.logging.debug(f"Processing response for stake: {response}")
        events = await self.subtensor.substrate.get_events(receipt.block_hash)
        bt.logging.debug("EVENTS:")
        for event in events:
            # bt.logging.debug(event)
            stake_event = StakeAddedEvent.from_substrate_event(event)
            if stake_event is None:
                continue
            if stake_event.coldkey_ss58 != self.wallet.coldkey.ss58_address:
                continue
            if stake_event.netuid != response_netuid:
                continue
            bt.logging.debug(stake_event)
            bt.logging.debug(f"Positons before: {current_position}")
            current_position.total_alpha_rao += stake_event.alpha_received_rao
            current_position.total_tao_spent_rao += stake_event.staking_amount_rao
            await save_positions(self)
            bt.logging.debug(f"Positons after: {current_position}")

    async def sign_and_send_extrinsic(self, call: Call) -> AsyncExtrinsicReceipt | None:
        try:
            extrinsic_data = {"call": call, "keypair": self.wallet.coldkey}

            extrinsic_data["era"] = {"period": 4, "current": self.current_block - 2}
            extrinsic = await self.subtensor.substrate.create_signed_extrinsic(
                **extrinsic_data
            )
            bt.logging.debug(f"Prepared extrinsic: {extrinsic}")
            response = await self.subtensor.substrate.submit_extrinsic(
                extrinsic=extrinsic,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )
            ok = await response.is_success
            bt.logging.info(
                f"Response: {response} | succes: {ok} | error: {await response.error_message if not ok else None}"
            )
            return response
        except Exception as e:
            bt.logging.error(f"Error during sending extrinsic: {e}")
            return None

    async def create_calls_buy(self):
        for subnet_config in self.subnets_config:
            subnet_config.call_buy = await SubtensorModule(
                self.subtensor
            ).add_stake_limit(
                hotkey=subnet_config.validator_hotkey,
                netuid=subnet_config.netuid,
                amount_staked=subnet_config.amount_tao_to_stake_buy.rao,
                limit_price=subnet_config.limit_price_buy.rao,
                allow_partial=True,
            )
            bt.logging.info(f"Subnets config with calls: {subnet_config}")

    async def get_subnets_to_stake(self) -> list[SubnetConfig]:
        subnets_to_stake = []
        current_balance = await self.subtensor.get_balance(
            self.wallet.coldkey.ss58_address
        )
        if current_balance.tao <= 0.01:
            bt.logging.warning(f"Not eneough stake: {current_balance}")
            return subnets_to_stake
        for subnet_config in self.subnets_config:
            current_price_on_subnet = self.prices.get(subnet_config.netuid)
            if current_price_on_subnet is None:
                continue
            if current_price_on_subnet.tao <= subnet_config.activation_price_buy.tao:
                subnets_to_stake.append(subnet_config)
            else:
                bt.logging.debug(
                    f"current_price_on_subnet: {current_price_on_subnet} > activation_price_buy: {subnet_config.activation_price_buy}"
                )
        if subnets_to_stake:
            bt.logging.debug(
                f"Achieved actiavation price for subnets to stake: [blue]{subnets_to_stake}[/blue]"
            )
        return subnets_to_stake

    async def refresh_prices(self):
        try:
            self.prices = await self.subtensor.get_subnet_prices()
            # bt.logging.debug(f"Refreshed prices: {self.prices}")
        except Exception as e:
            bt.logging.error(f"Error refreshing prices: {e}")
