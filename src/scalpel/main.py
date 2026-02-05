import asyncio
import bittensor as bt

from scalpel.logger import configure_logging
from scalpel.scalp_runner import ScalpRunner


async def main():
    TEST_MODE = False
    configure_logging()
    async with bt.AsyncSubtensor(
        network=(
            "ws://205.172.59.24:9944"
            if not TEST_MODE
            else "wss://test.finney.opentensor.ai:443"
        ),
        log_verbose=True,
        fallback_endpoints=(
            ["wss://entrypoint-finney.opentensor.ai:443"] if not TEST_MODE else None
        ),
        archive_endpoints=(
            ["wss://archive.chain.opentensor.ai:443"] if not TEST_MODE else None
        ),
        websocket_shutdown_timer=20,
    ) as subtensor:
        scalp_buyer = ScalpRunner(
            subtensor=subtensor,
            wallet_name="auto_staker" if not TEST_MODE else "trader_test",
        )
        await scalp_buyer.run()


if __name__ == "__main__":

    asyncio.run(main())
