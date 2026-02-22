#!/usr/bin/env python
"""
Headless launcher for script strategies — bypasses TUI entirely.

Used by Docker to auto-start strategy without interactive CLI.
Set HB_SCRIPT env var to the strategy filename (e.g., strategy_v7.py).
"""
import asyncio
import sys
import os
import logging

sys.path.insert(0, "/home/hummingbot")
sys.path.insert(0, "/home/hummingbot/bin")
import path_util  # noqa

from hummingbot import chdir_to_data_directory, init_logging
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger
from hummingbot.client.config.config_helpers import (
    ClientConfigAdapter,
    create_yml_files_legacy,
    load_client_config_map_from_file,
)
from hummingbot.client.config.security import Security
from hummingbot.client.settings import AllConnectorSettings
from hummingbot.client.hummingbot_application import HummingbotApplication

PASSWORD = os.environ.get("CONFIG_PASSWORD", "EE#!01477410#!ee")
SCRIPT = os.environ.get("HB_SCRIPT", "strategy_v7.py")

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def main():
    logger.info(f"🚀 Starting Hummingbot headless with script: {SCRIPT}")

    chdir_to_data_directory()

    secrets_manager = ETHKeyFileSecretManger(PASSWORD)
    Security.secrets_manager = secrets_manager

    if not Security.login(secrets_manager):
        logger.error("❌ Login failed!")
        return

    await Security.wait_til_decryption_done()
    await create_yml_files_legacy()

    client_config_map = load_client_config_map_from_file()
    init_logging("hummingbot_logs.yml", client_config_map)

    AllConnectorSettings.initialize_paper_trade_settings(
        client_config_map.paper_trade.paper_trade_exchanges
    )

    hb = HummingbotApplication.main_application(
        client_config_map=client_config_map, headless_mode=True
    )

    # Remove .py extension if present (trading_core expects strategy name without extension)
    strategy_name = SCRIPT.replace(".py", "")
    logger.info(f"📊 Loading script strategy: {strategy_name}")

    # Start strategy using trading_core directly (matching quickstart pattern)
    # IMPORTANT: strategy_file_name must match strategy_name when no config file is used
    # Otherwise trading_core will try to load a BaseClientModel config class
    success = await hb.trading_core.start_strategy(
        strategy_name=strategy_name,
        strategy_config=None,
        strategy_file_name=strategy_name  # Must match strategy_name for simple scripts
    )

    if not success:
        logger.error("❌ Failed to start strategy!")
        return

    logger.info("✅ Strategy started. Running in headless mode...")

    # Keep the strategy running (don't call hb.run() as it requires MQTT)
    # The strategy is already running via trading_core.start_strategy()
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
        await hb.trading_core.stop_strategy()
        await hb.trading_core.shutdown()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Shutting down...")
