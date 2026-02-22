# Hummingbot v2.12.0 Strategy Loading Guide

## Overview

This guide explains exactly how Hummingbot v2.12.0 loads and runs script strategies in Docker containers, focusing on auto-starting strategies without the interactive CLI.

## Key Components

### 1. Quickstart Script (`bin/hummingbot_quickstart.py`)

The quickstart script is the main entry point for automated strategy loading. It supports both regular strategies (YAML-based) and script strategies (Python-based).

**Key Command Line Arguments:**
- `--config-file-name (-f)`: Strategy config file name (from `conf/` directory)
- `--script-conf (-c)`: Script config file (from `conf/scripts/` directory)  
- `--config-password (-p)`: Password to decrypt encrypted files
- `--headless`: Run without CLI interface

**Environment Variables (Docker-friendly):**
- `CONFIG_FILE_NAME`: Strategy file to load
- `SCRIPT_CONFIG`: Script configuration file
- `CONFIG_PASSWORD`: Decryption password
- `HEADLESS_MODE`: Set to "true" for headless mode

### 2. Script Strategy Base Class

Script strategies inherit from `ScriptStrategyBase` which extends `StrategyPyBase`.

**Required Structure:**
```python
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class YourStrategy(ScriptStrategyBase):
    # Define required markets
    markets = {
        "exchange_name": {"TRADING-PAIR"}
    }
    
    def on_tick(self):
        # Your strategy logic here
        pass
```

### 3. Strategy Loading Process

The loading process follows this flow:

1. **Strategy Detection**: `trading_core.detect_strategy_type()` checks if it's a script strategy by looking for `{strategy_name}.py` in the `scripts/` directory
2. **Module Loading**: `load_script_class()` imports the module and searches for ScriptStrategyBase subclasses
3. **Market Initialization**: Markets defined in the strategy's `markets` class attribute are initialized
4. **Strategy Instantiation**: The strategy class is instantiated with connectors and optional config

## Configuration Options

### For Script Strategies (.py files)

**Basic Auto-Start (No Config File):**
```bash
# Environment variables
CONFIG_FILE_NAME=my_strategy.py
HEADLESS_MODE=true

# Command line
python bin/hummingbot_quickstart.py -f my_strategy.py --headless
```

**With Script Configuration:**
```bash
# Environment variables  
CONFIG_FILE_NAME=my_strategy.py
SCRIPT_CONFIG=my_strategy_config.yml
HEADLESS_MODE=true

# Command line
python bin/hummingbot_quickstart.py -f my_strategy.py -c my_strategy_config.yml --headless
```

### Docker Configuration

The Dockerfile CMD is:
```dockerfile
CMD conda activate hummingbot && ./bin/hummingbot_quickstart.py
```

For auto-start, override with environment variables:
```bash
docker run -e CONFIG_FILE_NAME=my_strategy.py -e HEADLESS_MODE=true hummingbot
```

## Key Differences: ScriptStrategyBase vs BaseClientModel

### ScriptStrategyBase
- **Purpose**: Base class for trading strategies
- **Location**: `hummingbot/strategy/script_strategy_base.py`
- **Usage**: Inherit from this for actual trading strategies
- **Loading**: Searched for in script modules by `load_script_class()`

### BaseClientModel  
- **Purpose**: Base class for configuration models (Pydantic-based)
- **Location**: `hummingbot/client/config/config_data_types.py`
- **Usage**: Used for configuration validation and serialization
- **Loading**: Not directly loaded as strategies

**The Error Explained:**
The error "The module ict_btc_perps_v6_native does not contain any subclass of BaseClientModel" occurs when the loading system incorrectly searches for BaseClientModel instead of ScriptStrategyBase. This happens when there's a mismatch in the loading path.

## Strategy Auto-Start Requirements

### 1. Strategy File Location
- Script must be in `/scripts/` directory
- Named `{strategy_name}.py`

### 2. Class Structure
```python
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase

class MyStrategy(ScriptStrategyBase):
    # REQUIRED: Define markets
    markets = {
        "binance": {"BTC-USDT"},  # format: exchange_name: {trading_pairs}
    }
    
    def on_tick(self):
        # Strategy logic executed every tick
        pass
```

### 3. Configuration Files (Optional)

**Script Config File** (`conf/scripts/{config_name}.yml`):
```yaml
# Optional configuration for script strategies
# Structure depends on your strategy's config class
parameter1: value1
parameter2: value2
```

### 4. Environment Setup for Docker

**Dockerfile Environment Variables:**
```dockerfile
ENV CONFIG_FILE_NAME=my_strategy.py
ENV HEADLESS_MODE=true  
ENV CONFIG_PASSWORD=your_password
# Optional:
ENV SCRIPT_CONFIG=my_strategy_config.yml
```

**Docker Run Command:**
```bash
docker run \
  -e CONFIG_FILE_NAME=my_strategy.py \
  -e HEADLESS_MODE=true \
  -e CONFIG_PASSWORD=your_password \
  your_hummingbot_image
```

## Example Scripts Analysis

### Basic Example (`scripts/basic/simple_order_example.py`)
- Demonstrates minimal ScriptStrategyBase usage
- Defines markets as class attribute
- Uses event handlers for order lifecycle
- Auto-stops after order completion

### Advanced Example (`scripts/v2_with_controllers.py`)
- Shows V2 strategy pattern with configuration
- Uses `StrategyV2ConfigBase` for config validation
- Demonstrates controller-based architecture

## Troubleshooting Common Issues

### 1. "Module does not contain any subclass of BaseClientModel"
**Cause**: Loading system searches for wrong base class
**Solution**: Ensure your strategy inherits from `ScriptStrategyBase`, not `BaseClientModel`

### 2. Strategy Not Found
**Cause**: File not in correct location or naming mismatch
**Solution**: Place `.py` file in `/scripts/` directory with exact name match

### 3. Markets Not Initialized
**Cause**: Missing or incorrectly defined `markets` class attribute
**Solution**: Define `markets = {"exchange": {"PAIR-NAME"}}` as class attribute

### 4. Configuration Not Loading
**Cause**: Config file path issues or format errors
**Solution**: Place config files in `/conf/scripts/` and use correct YAML format

## Complete Working Example

**File**: `scripts/auto_start_example.py`
```python
import logging
from decimal import Decimal
from hummingbot.strategy.script_strategy_base import ScriptStrategyBase
from hummingbot.core.data_type.common import OrderType

class AutoStartExample(ScriptStrategyBase):
    # Required: Define markets for auto-initialization
    markets = {
        "paper_trade": {"BTC-USDT"}
    }
    
    # Strategy parameters
    order_amount = Decimal(0.01)
    order_created = False
    
    def on_tick(self):
        if not self.order_created and self.ready_to_trade:
            # Get current price
            mid_price = self.connectors["paper_trade"].get_mid_price("BTC-USDT")
            
            # Place buy order slightly below mid price
            buy_price = mid_price * Decimal(0.995)
            
            self.buy(
                connector_name="paper_trade",
                trading_pair="BTC-USDT",
                amount=self.order_amount,
                order_type=OrderType.LIMIT,
                price=buy_price
            )
            
            self.order_created = True
            self.logger().info(f"Placed buy order for {self.order_amount} BTC at {buy_price}")
```

**Docker Command**:
```bash
docker run \
  -e CONFIG_FILE_NAME=auto_start_example.py \
  -e HEADLESS_MODE=true \
  -e CONFIG_PASSWORD=your_password \
  hummingbot
```

This setup will automatically start the strategy without any CLI interaction.

## Summary

To make a Hummingbot strategy auto-start in Docker:

1. **Create** a script inheriting from `ScriptStrategyBase` in `/scripts/`
2. **Define** the `markets` class attribute with required exchanges/pairs
3. **Set** environment variables: `CONFIG_FILE_NAME`, `HEADLESS_MODE=true`
4. **Run** with quickstart script in headless mode

The key difference from regular strategies is that script strategies are Python files loaded dynamically, while regular strategies use YAML configuration files with pre-built strategy classes.