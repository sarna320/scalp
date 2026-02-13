from bittensor.core.extrinsics.pallets import SubtensorModule
from bittensor.core.extrinsics.pallets.base import Call
from async_substrate_interface.async_substrate import AsyncExtrinsicReceipt
import bittensor as bt
import asyncio
from pathlib import Path
from bittensor.core.chain_data import DynamicInfo

from scalpel.subnet_config import get_subnet_configs, SubnetConfig
from scalpel.models import StakeAddedEvent, StakeRemovedEvent, Position
from scalpel.positions_persistence import load_positions, save_positions
from scalpel.sell_planner import build_sell_plan

EXTRINSIC_FEE_TAO_ADD_STAKE = bt.Balance.from_tao(0.000136963)
EXTRINSIC_FEE_TAO_REMOVE_STAKE = bt.Balance.from_tao(0.000135688)


class ScalpRunner:
    def __init__(
        self,
        subtensor: bt.AsyncSubtensor,
        wallet_name: str = "trader",
        positions_path: str = "positions.json",
    ):
        self.subtensor = subtensor
        self.prices: dict[int, bt.Balance]
        self.dynamics: dict[int, bt.DynamicInfo]
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
        subnets_to_stake, subnets_to_unstake = await asyncio.gather(
            *[
                self.get_subnets_to_stake(),
                self.get_subnets_to_unstake(),
            ]
        )
        responses_for_stake, responses_for_unstake = await asyncio.gather(
            *[
                self.process_subnets(subnets_to_stake, call_is_buy=True),
                self.process_subnets(subnets_to_unstake, call_is_buy=False),
            ]
        )
        await asyncio.gather(
            *[
                self.process_response_stake(response)
                for response in responses_for_stake
            ],
            *[
                self.process_response_unstake(response)
                for response in responses_for_unstake
            ],
        )
        return None

    async def process_response_unstake(
        self, response: tuple[int, AsyncExtrinsicReceipt] | tuple[None, None]
    ) -> None:
        response_netuid, receipt = response
        if response_netuid is None or receipt is None:
            return

        pos = self.positions.get(response_netuid)
        if pos is None:
            # Nothing to sell-account against; still may pay fee, but this likely means logic error.
            pos = Position(response_netuid)
            self.positions[response_netuid] = pos

        # Account flat (weight-based) extrinsic fee once per sell attempt, regardless of success.
        # We store it in realized PnL as a trading cost.
        pos.realized_profit_rao -= EXTRINSIC_FEE_TAO_REMOVE_STAKE.rao

        ok = await receipt.is_success
        if not ok:
            bt.logging.warning(
                f"Unstake extrinsic failed; fee accounted. Position: {pos}"
            )
            await save_positions(self)
            return

        events = await self.subtensor.substrate.get_events(receipt.block_hash)

        any_event_applied = False

        for event in events:
            removed = StakeRemovedEvent.from_substrate_event(event)
            if removed is None:
                continue

            # Filter to our wallet + correct subnet
            if removed.coldkey_ss58 != self.wallet.coldkey.ss58_address:
                continue
            if removed.netuid != response_netuid:
                continue

            if pos.total_alpha_rao <= 0:
                bt.logging.warning(
                    f"Received StakeRemoved but position has no alpha. Event: {removed}"
                )
                continue

            # Sell quantity cannot exceed current position size
            sell_qty_rao = int(removed.alpha_unstaked_rao)
            if sell_qty_rao > pos.total_alpha_rao:
                sell_qty_rao = pos.total_alpha_rao

            # AVG-cost cost basis for the sold portion (integer math, no floats)
            # Note: using floor keeps invariants stable; we zero out remainder when position is fully closed.
            cost_basis_sold_rao = (
                pos.total_tao_spent_rao * sell_qty_rao
            ) // pos.total_alpha_rao

            proceeds_rao = int(removed.tao_recived_rao)

            # Realized PnL from this fill (flat fee already accounted above)
            realized_pnl_rao = proceeds_rao - cost_basis_sold_rao

            bt.logging.debug(f"Positions before SELL: {pos}")
            pos.realized_profit_rao += realized_pnl_rao
            pos.total_alpha_rao -= sell_qty_rao
            pos.total_tao_spent_rao -= cost_basis_sold_rao

            # Clean up rounding leftovers when fully closed
            # This is also needed since when we sell we substract 1 from postions
            if pos.total_alpha_rao <= 1:
                pos.total_tao_spent_rao = 0

            bt.logging.debug(f"Applied StakeRemoved: {removed}")
            bt.logging.debug(f"Positions after SELL: {pos}")

            any_event_applied = True

        if any_event_applied:
            await save_positions(self)
        else:
            # Still persist fee change
            await save_positions(self)

    async def get_subnets_to_unstake(self) -> list[SubnetConfig]:
        subnets: list[SubnetConfig] = []

        for cfg in self.subnets_config:
            pos = self.positions.get(cfg.netuid)
            if pos is None or pos.total_alpha_rao <= 0:
                # bt.logging.debug(f"Positions netuid: {cfg.netuid} is None or 0")
                continue

            stake_from_chain: bt.Balance = await self.subtensor.get_stake(
                coldkey_ss58=self.wallet.coldkey.ss58_address,
                hotkey_ss58=cfg.validator_hotkey,
                netuid=cfg.netuid,
            )
            onchain_alpha_rao = int(stake_from_chain.rao)
            # Sync local position to on-chain stake (source of truth)
            if onchain_alpha_rao > pos.total_alpha_rao:
                # Rewards accrued
                pos.total_alpha_rao = onchain_alpha_rao
                await save_positions(self)
            elif onchain_alpha_rao < pos.total_alpha_rao:
                # Local state is ahead -> clamp to on-chain to avoid oversell
                pos.total_alpha_rao = onchain_alpha_rao
                await save_positions(self)

            dyn = self.dynamics.get(cfg.netuid)
            if dyn is None:
                bt.logging.debug(f"Dynamic netuid: {cfg.netuid} is None")
                continue

            # Compute desired sell amount based on sell_pct and min_sell_alpha
            desired_sell_rao = int(pos.total_alpha_rao * cfg.sell_pct)
            if desired_sell_rao < cfg.min_sell_alpha_rao:
                desired_sell_rao = min(cfg.min_sell_alpha_rao, pos.total_alpha_rao)

            plan = build_sell_plan(
                netuid=cfg.netuid,
                dynamic=dyn,
                position_total_alpha_rao=pos.total_alpha_rao,
                position_total_tao_spent_rao=pos.total_tao_spent_rao,
                pct_profit=cfg.pct_profit,
                slippage_sell_pct=cfg.slippage_sell_pct,
                flat_fee_sell_rao=EXTRINSIC_FEE_TAO_REMOVE_STAKE.rao,
                min_gross_fill_rao=0,
                max_sell_alpha_rao=desired_sell_rao,
            )
            if plan is None:
                bt.logging.debug(f"Plan netuid: {cfg.netuid} is None")
                continue

            spot_price = self.prices.get(cfg.netuid)
            if spot_price is None:
                bt.logging.debug(f"spot_price netuid: {cfg.netuid} is None")
                continue

            if spot_price.tao >= plan.activation_price.tao:
                cfg.call_sell = await SubtensorModule(
                    self.subtensor
                ).remove_stake_limit(
                    hotkey=cfg.validator_hotkey,
                    netuid=cfg.netuid,
                    amount_unstaked=plan.amount_alpha_to_sell_rao
                    - 1,  # Substract 1 to avoid error with NotEnoughStakeToWithdraw, even if you getting data fresh from chain
                    limit_price=plan.limit_price.rao,
                    allow_partial=True,
                )
                subnets.append(cfg)
            else:
                bt.logging.debug(
                    f"Postions netuid: {pos.netuid} activation_price: {plan.activation_price} > spot_price: {spot_price}"
                )
        return subnets

    async def process_subnets(
        self, subnets: list[SubnetConfig], call_is_buy: bool
    ) -> list[tuple[int, AsyncExtrinsicReceipt] | tuple[None, None]]:
        if not subnets:
            return [(None, None)]

        tasks = [
            asyncio.wait_for(
                self.sign_and_send_extrinsic(
                    subnet.call_buy if call_is_buy else subnet.call_sell
                ),
                timeout=48.0,  # seconds
            )
            for subnet in subnets
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        out: list[tuple[int, AsyncExtrinsicReceipt] | tuple[None, None]] = []
        for subnet, res in zip(subnets, results):
            if isinstance(res, Exception):
                bt.logging.warning(
                    f"Tx timed out/failed for netuid={subnet.netuid}: {res}"
                )
                out.append((subnet.netuid, None))
            else:
                out.append((subnet.netuid, res))

        return out

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
            await save_positions(self)
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
            bt.logging.warning(f"Not eneough balance: {current_balance}")
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
            infos = await self.subtensor.all_subnets()
            self.dynamics = {info.netuid: info for info in infos}
            self.prices = {info.netuid: info.price for info in infos}
            # bt.logging.debug(f"Refreshed prices: {self.prices}")
        except Exception as e:
            bt.logging.error(f"Error refreshing prices: {e}")
