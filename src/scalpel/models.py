from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
import bittensor as bt
from datetime import datetime


@dataclass(frozen=True, slots=True)
class StakeRemovedEvent:
    coldkey_ss58: str
    validator_ss58: str
    tao_recived_rao: int
    alpha_unstaked_rao: int
    netuid: int
    paid_fee_rao: int

    @classmethod
    def from_substrate_event(
        cls, raw: Mapping[str, Any]
    ) -> Optional["StakeRemovedEvent"]:
        event_data = raw.get("event")
        if not isinstance(event_data, Mapping):
            return None

        if event_data.get("event_id") != "StakeRemoved":
            return None

        attributes = event_data.get("attributes")
        if not isinstance(attributes, Sequence) or len(attributes) != 6:
            return None

        (
            coldkey_ss58,
            validator_ss58,
            tao_recived_rao,
            alpha_unstaked_rao,
            netuid,
            paid_fee_rao,
        ) = attributes

        # Ensure numeric fields are ints even if substrate returns strings or other numeric types
        return cls(
            coldkey_ss58=str(coldkey_ss58),
            validator_ss58=str(validator_ss58),
            tao_recived_rao=int(tao_recived_rao),
            alpha_unstaked_rao=int(alpha_unstaked_rao),
            netuid=int(netuid),
            paid_fee_rao=int(paid_fee_rao),
        )


@dataclass(frozen=True, slots=True)
class StakeAddedEvent:
    coldkey_ss58: str
    validator_ss58: str
    staking_amount_rao: int
    alpha_received_rao: int
    netuid: int
    paid_fee_rao: int

    @classmethod
    def from_substrate_event(
        cls, raw: Mapping[str, Any]
    ) -> Optional["StakeAddedEvent"]:
        event_data = raw.get("event")
        if not isinstance(event_data, Mapping):
            return None

        if event_data.get("event_id") != "StakeAdded":
            return None

        attributes = event_data.get("attributes")
        if not isinstance(attributes, Sequence) or len(attributes) != 6:
            return None

        (
            coldkey_ss58,
            validator_ss58,
            staking_amount_rao,
            alpha_received_rao,
            netuid,
            paid_fee_rao,
        ) = attributes

        # Ensure numeric fields are ints even if substrate returns strings or other numeric types
        return cls(
            coldkey_ss58=str(coldkey_ss58),
            validator_ss58=str(validator_ss58),
            staking_amount_rao=int(staking_amount_rao),
            alpha_received_rao=int(alpha_received_rao),
            netuid=int(netuid),
            paid_fee_rao=int(paid_fee_rao),
        )


@dataclass
class Position:
    netuid: int
    total_alpha_rao: int
    total_tao_spent_rao: int
    total_fee_paid_rao: int
    realized_profit_rao: int
    num_transactions: int
    last_updated: datetime

    @property
    def avg_entry_price(self) -> float:
        """Average entry price in TAO per Alpha."""
        if self.total_alpha_rao == 0:
            return 0.0
        return self.total_tao_spent_rao / self.total_alpha_rao

    @property
    def total_alpha(self) -> bt.Balance:
        return bt.Balance.from_rao(self.total_alpha_rao, netuid=self.netuid)

    @property
    def total_tao_spent(self) -> bt.Balance:
        return bt.Balance.from_rao(self.total_tao_spent_rao, netuid=0)

    @property
    def total_fee_paid(self) -> bt.Balance:
        return bt.Balance.from_rao(self.total_fee_paid_rao, netuid=0)

    @property
    def realized_profit(self) -> bt.Balance:
        """Total realized profit from closed positions."""
        return bt.Balance.from_rao(self.realized_profit_rao, netuid=0)

    @property
    def unrealized_pnl_rao(self) -> int | None:
        """Unrealized P&L in rao (requires current price to calculate)."""
        # This would need current price to calculate: (current_price - avg_entry_price) * total_alpha_rao
        return None


@dataclass
class Transaction:
    id: int
    netuid: int
    coldkey_ss58: str
    validator_ss58: str
    tao_spent_rao: int
    alpha_received_rao: int
    fee_paid_rao: int
    price: float
    extrinsic_hash: str
    block_hash: str
    block_number: int | None
    created_at: datetime
