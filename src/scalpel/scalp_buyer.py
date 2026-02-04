from bittensor.core.extrinsics.pallets import SubtensorModule
from bittensor.core.extrinsics.pallets.base import Call
from async_substrate_interface.async_substrate import AsyncExtrinsicReceipt
import bittensor as bt
import asyncio

from scalpel.database import PositionDatabase
from scalpel.subnet_config import get_subnet_configs, SubnetConfig
from scalpel.models import StakeAddedEvent


class ScalpBuyer:
    def __init__(
        self,
        subtensor: bt.AsyncSubtensor,
        wallet_name: str = "trader",
        db_path: str = "./data/positions.db",
    ):
        self.subtensor = subtensor
        self.prices: dict[int, bt.Balance]
        self.subnets_config: list[SubnetConfig]
        self.current_block: int
        self.wallet = bt.Wallet(wallet_name)
        self.db = PositionDatabase(db_path)
        bt.logging.info(
            f"Using wallet {self.wallet.coldkey.ss58_address}: {self.wallet}"
        )

    async def run(self):
        try:
            await self.db.connect()
            current_block = await self.subtensor.substrate.get_block()
            current_block_hash = current_block.get("header", {}).get("hash")
            self.subnets_config = get_subnet_configs()
            await self.create_calls()
            await self.subtensor.substrate.get_block_handler(
                current_block_hash,
                header_only=True,
                subscription_handler=self.handler,
            )
        finally:
            await self.db.close()

    async def handler(self, block_data: dict):
        self.current_block = block_data["header"]["number"]
        bt.logging.info(f"Current block: [blue]{self.current_block}[/blue]")
        await self.refresh_prices()
        subnets_to_stake = self.get_subnets_to_stake()
        responses = await self.process_subnets_to_stake(subnets_to_stake)
        for response in responses:
            await self.process_response(response)
        return True

    async def process_subnets_to_stake(
        self, subnets_to_stake: list[SubnetConfig]
    ) -> list[AsyncExtrinsicReceipt | None]:
        if len(subnets_to_stake) == 0:
            return [None]
        responses = await asyncio.gather(
            *[self.sign_and_send_extrinsic(subnet.call) for subnet in subnets_to_stake]
        )
        return responses

    async def process_response(self, response: None | AsyncExtrinsicReceipt):
        if response is None:
            return
        bt.logging.debug(f"Processing response: {response}")
        events = await self.subtensor.substrate.get_events(response.block_hash)
        bt.logging.debug("EVENTS:")
        for event in events:
            stake_event = StakeAddedEvent.from_substrate_event(event)
            if stake_event is None:
                continue
            if stake_event.coldkey_ss58 != self.wallet.coldkey.ss58_address:
                continue
            bt.logging.debug(stake_event)
            position = await self.db.update_position(
                event=stake_event,
                extrinsic_hash=response.extrinsic_hash,
                block_hash=response.block_hash,
                block_number=response.block_number,
            )
            bt.logging.info(
                f"Position updated for netuid {position.netuid}: "
                f"alpha={position.total_alpha}, "
                f"tao_spent={position.total_tao_spent}, "
                f"fee_paid={position.total_fee_paid}, "
                f"avg_price={position.avg_entry_price:.6f}, "
                f"txs={position.num_transactions}"
            )
            break

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
            bt.logging.info(f"Response: {response}")
            return response
        except Exception as e:
            bt.logging.error(f"Error during sednig extrinsic: {e}")
            if "ancient birth block" in e:
                bt.logging.info(f"Retrying with diffrent block")
                self.current_block += 1
                await self.sign_and_send_extrinsic(call)
            return None

    async def create_calls(self):
        for subnet_config in self.subnets_config:
            subnet_config.call = await SubtensorModule(self.subtensor).add_stake_limit(
                hotkey=subnet_config.validator_hotkey,
                netuid=subnet_config.netuid,
                amount_staked=subnet_config.amount_tao_to_stake.rao,
                limit_price=subnet_config.limit_price.rao,
                allow_partial=True,
            )
            bt.logging.info(f"Subnets config with calls: {subnet_config}")

    def get_subnets_to_stake(self) -> list[SubnetConfig]:
        subnets_to_stake = []
        for subnet_config in self.subnets_config:
            current_price_on_subnet = self.prices.get(subnet_config.netuid)
            if current_price_on_subnet.tao <= subnet_config.activation_price.tao:
                subnets_to_stake.append(subnet_config)
        bt.logging.debug(
            f"Achieved actiavation price for subnets: [blue]{subnets_to_stake}[/blue]"
        )
        return subnets_to_stake

    async def refresh_prices(self):
        try:
            self.prices = await self.subtensor.get_subnet_prices()
            bt.logging.debug(f"Refreshed prices: {self.prices}")
        except Exception as e:
            bt.logging.error(f"Error refreshing prices: {e}")
