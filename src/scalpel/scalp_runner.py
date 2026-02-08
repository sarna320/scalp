from bittensor.core.extrinsics.pallets import SubtensorModule
from bittensor.core.extrinsics.pallets.base import Call
from async_substrate_interface.async_substrate import AsyncExtrinsicReceipt
import bittensor as bt
import asyncio
import math

from scalpel.database import PositionDatabase
from scalpel.subnet_config import get_subnet_configs, SubnetConfig
from scalpel.models import StakeAddedEvent, StakeRemovedEvent

EXTRINSIC_FEE_TAO_ADD_STAKE = 0.000136963
EXTRINSIC_FEE_TAO_REMOVE_STAKE = 0.000135688
ALPHA_FEE_PCT = 0.0005  # 0.05%


class ScalpRunner:
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
            await self.create_calls_buy()  # this calls will always remain the same
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
        # subnets_to_unstake = await self.get_subnets_to_unstake()
        # responses_for_stake, responses_for_unstake = await asyncio.gather(
        #     *[
        #         self.process_subnets(subnets_to_stake, call_is_buy=True),
        #         self.process_subnets(subnets_to_unstake, call_is_buy=False),
        #     ]
        # )
        # await asyncio.gather(
        #     *[self.process_response_stake(response) for response in responses_for_stake]
        #     + [
        #         self.process_response_unstake(response)
        #         for response in responses_for_unstake
        #     ]
        # )

        responses_for_stake = await self.process_subnets(
            subnets_to_stake, call_is_buy=True
        )

        await asyncio.gather(
            *[self.process_response_stake(response) for response in responses_for_stake]
        )

        return None

    def _safe_unstake_amount_rao(
        sefl, stake_rao: int, fee_pct: float = ALPHA_FEE_PCT, buffer_rao: int = 0
    ) -> int:
        """
        Returns an amount slightly below the current stake to avoid NotEnoughStakeToWithdraw caused by fees/rounding.
        buffer_rao is in alpha-RAO.
        """
        # Conservative: assume fee is taken "on top" in stake-equivalent terms.
        fee_rao = math.ceil(stake_rao * fee_pct)
        return max(0, stake_rao - fee_rao - buffer_rao)

    async def get_subnets_to_unstake(self) -> list[SubnetConfig]:
        subnets_to_unstake = []
        all_position = await self.db.get_all_positions()
        if not all_position:
            return subnets_to_unstake
        for subnet_config in self.subnets_config:
            current_price_on_subnet = self.prices.get(subnet_config.netuid)
            current_postion = all_position.get(subnet_config.netuid)
            if current_postion is None or current_postion.total_alpha == 0:
                continue

            base_required_price = (
                current_postion.avg_entry_price * subnet_config.pct_profit
            )

            extrinsic_fee_per_alpha = (
                EXTRINSIC_FEE_TAO_REMOVE_STAKE / current_postion.total_alpha_rao / 1e9
            )

            required_price_with_fees = (
                base_required_price + extrinsic_fee_per_alpha
            ) / (1 - ALPHA_FEE_PCT)

            if current_price_on_subnet.tao >= required_price_with_fees:
                bt.logging.debug(f"Current postion: {current_postion}")
                limit_price = bt.Balance.from_tao(
                    required_price_with_fees * (1 - subnet_config.slippage_sell_pct)
                )
                total_alpha_rao_to_unstake_from_position = bt.Balance.from_rao(
                    current_postion.total_alpha_rao, subnet_config.netuid
                )  # this for some reason gives error even if this exactly the same value from chain
                total_alpha_rao_to_unstake_from_chain = await self.subtensor.get_stake(
                    self.wallet.coldkey.ss58_address,
                    subnet_config.validator_hotkey,
                    subnet_config.netuid,
                )
                bt.logging.debug(
                    f"Stake from chain: {total_alpha_rao_to_unstake_from_chain.rao} stake from postion {total_alpha_rao_to_unstake_from_position.rao}"
                )

                amount_unstaked_rao = self._safe_unstake_amount_rao(
                    total_alpha_rao_to_unstake_from_chain.rao
                )

                subnet_config.call_sell = await SubtensorModule(
                    self.subtensor
                ).remove_stake_limit(
                    netuid=subnet_config.netuid,
                    hotkey=subnet_config.validator_hotkey,
                    amount_unstaked=amount_unstaked_rao,
                    limit_price=limit_price.rao,
                    allow_partial=True,
                )

                subnets_to_unstake.append(subnet_config)
                bt.logging.info(
                    f"Unstake triggered for netuid {subnet_config.netuid}: "
                    f"current_price={current_price_on_subnet.tao:.6f}, "
                    f"base_required={base_required_price:.6f}, "
                    f"required_with_fees={required_price_with_fees:.6f}"
                )
            else:
                bt.logging.debug(
                    f"Current price: {current_price_on_subnet.tao} < required price with fees: {required_price_with_fees} for current postion: {current_postion}"
                )

        if subnets_to_unstake:
            bt.logging.debug(f"Subnets to unstake: [blue]{subnets_to_unstake}[/blue]")
        return subnets_to_unstake

    async def process_subnets(
        self, subnets: list[SubnetConfig], call_is_buy: bool
    ) -> list[AsyncExtrinsicReceipt | None]:
        if len(subnets) == 0:
            return [None]
        responses = await asyncio.gather(
            *[
                self.sign_and_send_extrinsic(
                    subnet.call_buy if call_is_buy else subnet.call_sell
                )
                for subnet in subnets
            ]
        )
        return responses

    async def process_response_stake(self, response: None | AsyncExtrinsicReceipt):
        if response is None:
            return
        bt.logging.debug(f"Processing response for stake: {response}")
        events = await self.subtensor.substrate.get_events(response.block_hash)
        bt.logging.debug("EVENTS:")
        for event in events:
            # bt.logging.debug(event)
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
                f"Position STAKE for netuid {position.netuid}: "
                f"alpha={position.total_alpha}, "
                f"tao_spent={position.total_tao_spent}, "
                f"avg_price={position.avg_entry_price:.6f}, "
                f"realized_profit={position.realized_profit}, "
                f"fee_paid={position.total_fee_paid}, "
                f"txs={position.num_transactions}"
            )

    async def process_response_unstake(self, response: None | AsyncExtrinsicReceipt):
        if response is None:
            return
        bt.logging.debug(f"Processing response unstake: {response}")
        events = await self.subtensor.substrate.get_events(response.block_hash)
        bt.logging.debug("EVENTS:")
        for event in events:
            # bt.logging.debug(event)
            unstake_event = StakeRemovedEvent.from_substrate_event(event)
            if unstake_event is None:
                continue
            if unstake_event.coldkey_ss58 != self.wallet.coldkey.ss58_address:
                continue
            bt.logging.debug(unstake_event)
            position = await self.db.update_position_unstake(
                event=unstake_event,
                extrinsic_hash=response.extrinsic_hash,
                block_hash=response.block_hash,
                block_number=response.block_number,
            )
            bt.logging.info(
                f"Position UNSTAKE for netuid {position.netuid}: "
                f"alpha={position.total_alpha}, "
                f"avg_price={position.avg_entry_price:.6f}, "
                f"realized_profit={position.realized_profit}, "
                f"fee_paid={position.total_fee_paid}, "
                f"txs={position.num_transactions}"
            )

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

    def get_subnets_to_stake(self) -> list[SubnetConfig]:
        subnets_to_stake = []
        for subnet_config in self.subnets_config:
            current_price_on_subnet = self.prices.get(subnet_config.netuid)
            if current_price_on_subnet.tao <= subnet_config.activation_price_buy.tao:
                subnets_to_stake.append(subnet_config)
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
