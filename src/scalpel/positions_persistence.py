import bittensor as bt
import json

from scalpel.models import Position
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scalpel.scalp_runner import ScalpRunner


async def load_positions(self: "ScalpRunner") -> None:
    """Load current positions from JSON if available."""
    if not self.positions_path.exists():
        bt.logging.info(
            f"No positions file found: {self.positions_path}. Starting fresh."
        )
        return

    try:
        raw = json.loads(self.positions_path.read_text(encoding="utf-8"))
        positions_obj = raw.get("positions", {})
        if not isinstance(positions_obj, dict):
            bt.logging.warning(
                "Invalid positions.json format (positions is not a dict). Starting fresh."
            )
            return

        loaded: dict[int, Position] = {}
        for netuid_str, pos_dict in positions_obj.items():
            try:
                netuid = int(netuid_str)
                loaded[netuid] = Position(
                    netuid=netuid,
                    total_alpha_rao=int(pos_dict.get("total_alpha_rao", 0)),
                    total_tao_spent_rao=int(pos_dict.get("total_tao_spent_rao", 0)),
                    realized_profit_rao=int(pos_dict.get("realized_profit_rao", 0)),
                )
            except Exception:
                continue

        self.positions = loaded
        bt.logging.info(
            f"Loaded {len(self.positions)} positions from {self.positions_path}"
        )
    except Exception as e:
        bt.logging.error(f"Failed to load positions: {e}")


async def save_positions(self: "ScalpRunner") -> None:
    """Atomically save current positions snapshot to JSON."""
    async with self._persist_lock:
        payload = {
            "positions": {
                str(netuid): {
                    "netuid": pos.netuid,
                    "total_alpha_rao": pos.total_alpha_rao,
                    "total_tao_spent_rao": pos.total_tao_spent_rao,
                    "realized_profit_rao": pos.realized_profit_rao,
                }
                for netuid, pos in self.positions.items()
            },
        }
        tmp_path = self.positions_path.with_suffix(self.positions_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.positions_path)
