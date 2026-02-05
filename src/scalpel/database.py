from datetime import datetime
from pathlib import Path
import aiosqlite
import bittensor as bt

from scalpel.models import StakeAddedEvent, StakeRemovedEvent, Position, Transaction


class PositionDatabase:
    def __init__(self, db_path: str | Path = "./data/positions.db"):
        self.db_path = Path(db_path)
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
        bt.logging.info(f"Connected to database: {self.db_path}")

    async def close(self) -> None:
        if self._connection:
            await self._connection.close()
            self._connection = None

    async def _create_tables(self) -> None:
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                netuid INTEGER PRIMARY KEY,
                total_alpha_rao INTEGER NOT NULL DEFAULT 0,
                total_tao_spent_rao INTEGER NOT NULL DEFAULT 0,
                total_fee_paid_rao INTEGER NOT NULL DEFAULT 0,
                realized_profit_rao INTEGER NOT NULL DEFAULT 0,
                num_transactions INTEGER NOT NULL DEFAULT 0,
                last_updated TEXT NOT NULL
            )
        """
        )
        await self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                netuid INTEGER NOT NULL,
                coldkey_ss58 TEXT NOT NULL,
                validator_ss58 TEXT NOT NULL,
                transaction_type TEXT NOT NULL,
                tao_spent_rao INTEGER NOT NULL,
                alpha_received_rao INTEGER NOT NULL,
                fee_paid_rao INTEGER NOT NULL,
                price REAL NOT NULL,
                profit_rao INTEGER,
                extrinsic_hash TEXT NOT NULL,
                block_hash TEXT NOT NULL,
                block_number INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY (netuid) REFERENCES positions(netuid)
            )
        """
        )
        await self._connection.commit()

    async def update_position(
        self,
        event: StakeAddedEvent,
        extrinsic_hash: str,
        block_hash: str,
        block_number: int,
    ) -> Position:
        """Update position after a successful stake and record the transaction."""
        now = datetime.utcnow().isoformat()
        price = (
            event.staking_amount_rao / event.alpha_received_rao
            if event.alpha_received_rao > 0
            else 0
        )

        # Insert transaction record
        await self._connection.execute(
            """
            INSERT INTO transactions (netuid, coldkey_ss58, validator_ss58, transaction_type, tao_spent_rao, alpha_received_rao, fee_paid_rao, price, profit_rao, extrinsic_hash, block_hash, block_number, created_at)
            VALUES (?, ?, ?, 'BUY', ?, ?, ?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                event.netuid,
                event.coldkey_ss58,
                event.validator_ss58,
                event.staking_amount_rao,
                event.alpha_received_rao,
                event.paid_fee_rao,
                price,
                extrinsic_hash,
                block_hash,
                block_number,
                now,
            ),
        )

        # Upsert position
        await self._connection.execute(
            """
            INSERT INTO positions (netuid, total_alpha_rao, total_tao_spent_rao, total_fee_paid_rao, num_transactions, last_updated)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(netuid) DO UPDATE SET
                total_alpha_rao = total_alpha_rao + excluded.total_alpha_rao,
                total_tao_spent_rao = total_tao_spent_rao + excluded.total_tao_spent_rao,
                total_fee_paid_rao = total_fee_paid_rao + excluded.total_fee_paid_rao,
                num_transactions = num_transactions + 1,
                last_updated = excluded.last_updated
            """,
            (
                event.netuid,
                event.alpha_received_rao,
                event.staking_amount_rao,
                event.paid_fee_rao,
                now,
            ),
        )
        await self._connection.commit()

        return await self.get_position(event.netuid)

    async def update_position_unstake(
        self,
        event: StakeRemovedEvent,
        extrinsic_hash: str,
        block_hash: str,
        block_number: int,
    ) -> Position:
        """Update position after a successful unstake and record the transaction with profit."""
        now = datetime.utcnow().isoformat()

        # Get current position to calculate profit
        current_position = await self.get_position(event.netuid)
        if current_position is None:
            bt.logging.warning(
                f"No position found for netuid {event.netuid} during unstake"
            )
            # Create empty position if doesn't exist
            current_position = Position(
                netuid=event.netuid,
                total_alpha_rao=0,
                total_tao_spent_rao=0,
                total_fee_paid_rao=0,
                num_transactions=0,
                last_updated=datetime.utcnow(),
            )

        # Calculate price and profit
        price = (
            event.tao_recived_rao / event.alpha_unstaked_rao
            if event.alpha_unstaked_rao > 0
            else 0
        )

        # Profit = TAO_received - (alpha_sold * avg_entry_price) - fee
        cost_basis = event.alpha_unstaked_rao * current_position.avg_entry_price
        profit_rao = event.tao_recived_rao - int(cost_basis) - event.paid_fee_rao

        # Insert transaction record with SELL type
        await self._connection.execute(
            """
            INSERT INTO transactions (netuid, coldkey_ss58, validator_ss58, transaction_type, tao_spent_rao, alpha_received_rao, fee_paid_rao, price, profit_rao, extrinsic_hash, block_hash, block_number, created_at)
            VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.netuid,
                event.coldkey_ss58,
                event.validator_ss58,
                -event.tao_recived_rao,  # negative because we're receiving TAO
                -event.alpha_unstaked_rao,  # negative because we're selling alpha
                event.paid_fee_rao,
                price,
                profit_rao,
                extrinsic_hash,
                block_hash,
                block_number,
                now,
            ),
        )

        # Update position - subtract alpha, add realized profit
        await self._connection.execute(
            """
            UPDATE positions SET
                total_alpha_rao = total_alpha_rao - ?,
                realized_profit_rao = realized_profit_rao + ?,
                total_fee_paid_rao = total_fee_paid_rao + ?,
                num_transactions = num_transactions + 1,
                last_updated = ?
            WHERE netuid = ?
            """,
            (
                event.alpha_unstaked_rao,
                profit_rao,
                event.paid_fee_rao,
                now,
                event.netuid,
            ),
        )
        await self._connection.commit()

        return await self.get_position(event.netuid)

    async def get_position(self, netuid: int) -> Position | None:
        """Get position for a specific subnet."""
        cursor = await self._connection.execute(
            "SELECT * FROM positions WHERE netuid = ?", (netuid,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return Position(
            netuid=row["netuid"],
            total_alpha_rao=row["total_alpha_rao"],
            total_tao_spent_rao=row["total_tao_spent_rao"],
            total_fee_paid_rao=row["total_fee_paid_rao"],
            realized_profit_rao=row["realized_profit_rao"] or 0,
            num_transactions=row["num_transactions"],
            last_updated=datetime.fromisoformat(row["last_updated"]),
        )

    async def get_all_positions(self) -> dict[int, Position]:
        """Get all positions."""
        cursor = await self._connection.execute("SELECT * FROM positions")
        rows = await cursor.fetchall()
        positions = {}
        for row in rows:
            positions[row["netuid"]] = Position(
                netuid=row["netuid"],
                total_alpha_rao=row["total_alpha_rao"],
                total_tao_spent_rao=row["total_tao_spent_rao"],
                total_fee_paid_rao=row["total_fee_paid_rao"],
                realized_profit_rao=row["realized_profit_rao"] or 0,
                num_transactions=row["num_transactions"],
                last_updated=datetime.fromisoformat(row["last_updated"]),
            )
        return positions

    async def get_transactions(self, netuid: int | None = None) -> list[Transaction]:
        """Get transactions, optionally filtered by netuid."""
        if netuid is not None:
            cursor = await self._connection.execute(
                "SELECT * FROM transactions WHERE netuid = ? ORDER BY created_at DESC",
                (netuid,),
            )
        else:
            cursor = await self._connection.execute(
                "SELECT * FROM transactions ORDER BY created_at DESC"
            )
        rows = await cursor.fetchall()
        return [
            Transaction(
                id=row["id"],
                netuid=row["netuid"],
                coldkey_ss58=row["coldkey_ss58"],
                validator_ss58=row["validator_ss58"],
                tao_spent_rao=row["tao_spent_rao"],
                alpha_received_rao=row["alpha_received_rao"],
                fee_paid_rao=row["fee_paid_rao"],
                price=row["price"],
                extrinsic_hash=row["extrinsic_hash"],
                block_hash=row["block_hash"],
                block_number=row["block_number"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]
