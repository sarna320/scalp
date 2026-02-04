# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Scalpel is a Bittensor subnet staking bot that automatically stakes TAO tokens when subnet prices reach configured thresholds. It uses `add_stake_limit` extrinsics with limit orders.

## Commands

```bash
uv sync                        # Install dependencies
uv run python -m scalpel.main  # Run the bot
```

## Architecture

```
src/scalpel/
├── main.py          # Entrypoint - creates AsyncSubtensor and ScalpBuyer
├── scalp_buyer.py   # ScalpBuyer class - main bot logic
├── subnet_config.py # SubnetConfig dataclass, loads subnets_config.json
├── database.py      # PositionDatabase - SQLite tracking
├── models.py        # StakeAddedEvent, Position, Transaction dataclasses
└── logger.py        # configure_logging() - Bittensor logging setup
```

### ScalpBuyer (`scalp_buyer.py`)

Main bot class:
- `run()` - Connects DB, loads config, creates calls, subscribes to blocks
- `handler()` - Called each block: refresh prices → check thresholds → submit stakes
- `create_calls()` - Pre-builds `add_stake_limit` calls via `SubtensorModule`
- `get_subnets_to_stake()` - Returns subnets where `current_price <= activation_price`
- `sign_and_send_extrinsic()` - Signs with mortal era (4 blocks) and submits
- `process_response()` - Parses `StakeAdded` events, updates database

### SubnetConfig (`subnet_config.py`)

Dataclass with fields: `netuid`, `limit_price`, `activation_price`, `validator_hotkey`, `amount_tao_to_stake`, `call`. Defaults: validator `5E2LP6EnZ54m3wS8s1yPvD5c3xo71kQroBw7aUVK32TKeZ5u`, amount `1.0` TAO.

### PositionDatabase (`database.py`)

SQLite at `./data/positions.db`:
- **positions** - Aggregated per netuid: total_alpha_rao, total_tao_spent_rao, total_fee_paid_rao, num_transactions
- **transactions** - Individual stakes: amounts, price, extrinsic_hash, block_hash, block_number

### Models (`models.py`)

- `StakeAddedEvent` - Parses substrate `StakeAdded` events (frozen dataclass)
- `Position` - Aggregated position with `avg_entry_price` property
- `Transaction` - Individual transaction record

## Configuration

`subnets_config.json` - Array of subnet targets:

```json
[
  {
    "netuid": 64,
    "limit_price": 0.0874,
    "activation_price": 0.0884,
    "amount_tao_to_stake": 0.005,
    "validator_hotkey": "5F..."
  }
]
```

| Field | Description |
|-------|-------------|
| `netuid` | Subnet identifier |
| `activation_price` | Price that triggers the stake order |
| `limit_price` | Max price for limit order (slippage protection, set below activation) |
| `amount_tao_to_stake` | TAO per transaction (default: 1.0) |
| `validator_hotkey` | Target validator (optional, has default) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `TRACE` | TRACE, DEBUG, INFO, or WARNING |
| `BT_LOGGING_RECORD_LOG` | `1` | Enable file logging |
| `BT_LOGGING_LOGGING_DIR` | `./logs` | Log directory |

## Wallet

Loads wallet by name from Bittensor keystore. Default: `auto_staker` (production), `trader_test` (test mode). Toggle `TEST_MODE` in `main.py` to switch networks.
