# Hummingbot Codebase Research Findings

## 1. ScriptStrategyBase Order Placement

### Method Signatures

**ScriptStrategyBase buy() method:**
```python
def buy(self,
        connector_name: str,
        trading_pair: str,
        amount: Decimal,
        order_type: OrderType,
        price=s_decimal_nan,
        position_action=PositionAction.OPEN) -> str
```

**ScriptStrategyBase sell() method:**
```python
def sell(self,
         connector_name: str,
         trading_pair: str,
         amount: Decimal,
         order_type: OrderType,
         price=s_decimal_nan,
         position_action=PositionAction.OPEN) -> str
```

### Parent Class Methods (StrategyBase)

Both methods call the parent class methods:

**buy_with_specific_market():**
```python
def buy_with_specific_market(self, market_trading_pair_tuple, amount,
                             order_type=OrderType.MARKET,
                             price=s_decimal_nan,
                             expiration_seconds=NaN,
                             position_action=PositionAction.OPEN)
```

**sell_with_specific_market():**
```python
def sell_with_specific_market(self, market_trading_pair_tuple, amount,
                              order_type=OrderType.MARKET,
                              price=s_decimal_nan,
                              expiration_seconds=NaN,
                              position_action=PositionAction.OPEN)
```

### Market Close Orders

To place a **market close order**:
1. Set `order_type=OrderType.MARKET`
2. Set `position_action=PositionAction.CLOSE`
3. For **closing LONG positions**: Use `sell()` method
4. For **closing SHORT positions**: Use `buy()` method

**Example usage for closing positions:**
```python
# Close LONG position
self.sell(
    connector_name="hyperliquid_perpetual",
    trading_pair="BTC-USD",
    amount=abs(position.amount),  # Use absolute amount
    order_type=OrderType.MARKET,
    position_action=PositionAction.CLOSE
)

# Close SHORT position  
self.buy(
    connector_name="hyperliquid_perpetual",
    trading_pair="BTC-USD",
    amount=abs(position.amount),  # Use absolute amount
    order_type=OrderType.MARKET,
    position_action=PositionAction.CLOSE
)
```

## 2. Hyperliquid Position Management

### account_positions Property

**Location:** `hummingbot/connector/perpetual_derivative_py_base.py`

```python
@property
def account_positions(self) -> Dict[str, Position]:
    """Returns a dictionary of current active open positions."""
    return self._perpetual_trading.account_positions
```

### Object Type Returned

**Returns:** `Dict[str, Position]` where:
- **Key:** Position key (string)
- **Value:** `Position` object from `hummingbot/connector/derivative/position.py`

### Position Object Structure

```python
class Position:
    def __init__(self, trading_pair: str, position_side: PositionSide, 
                 unrealized_pnl: Decimal, entry_price: Decimal, 
                 amount: Decimal, leverage: Decimal)
    
    # Properties:
    @property
    def trading_pair(self) -> str
    @property  
    def position_side(self) -> PositionSide  # LONG, SHORT, BOTH
    @property
    def unrealized_pnl(self) -> Decimal
    @property
    def entry_price(self) -> Decimal
    @property
    def amount(self) -> Decimal
    @property
    def leverage(self) -> Decimal
```

### Position Tracking Updates

Position tracking is managed by the `PerpetualTrading` class:

```python
# Update position
def set_position(self, pos_key: str, position: Position):
    self._account_positions[pos_key] = position

# Remove position
def remove_position(self, pos_key: str) -> Optional[Position]:
    return self._account_positions.pop(pos_key, None)
```

**Position updates occur:**
1. During WebSocket position updates
2. After order fills
3. During periodic account synchronization
4. When positions are manually closed

## 3. OrderType Enum Values

**Location:** `hummingbot/core/data_type/common.py`

```python
class OrderType(Enum):
    MARKET = 1
    LIMIT = 2
    LIMIT_MAKER = 3
    AMM_SWAP = 4

    def is_limit_type(self):
        return self in (OrderType.LIMIT, OrderType.LIMIT_MAKER)
```

### For Position Closing

Use `OrderType.MARKET` for immediate market close orders:
```python
OrderType.MARKET  # Value: 1
```

## 4. CandlesFactory / CandlesBase Interface

### Factory Usage

**Location:** `hummingbot/data_feed/candles_feed/candles_factory.py`

```python
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.data_feed.candles_feed.data_types import CandlesConfig

# Create candles object
config = CandlesConfig(
    connector="hyperliquid_perpetual",
    trading_pair="BTC-USD", 
    interval="1m",
    max_records=150
)
candles = CandlesFactory.get_candle(config)
```

### CandlesBase Interface Methods

**Key Properties and Methods:**
```python
# Main data access
@property
def candles_df(self) -> pd.DataFrame:
    """Returns candles as Pandas DataFrame with columns:
    ['timestamp', 'open', 'high', 'low', 'close', 'volume', 
     'quote_asset_volume', 'n_trades', 'taker_buy_base_volume', 'taker_buy_quote_volume']
    """

# Check if data is ready
@property 
def ready(self) -> bool:
    """Returns True when _candles deque has reached maxlen (has enough historical data)"""

# Other useful properties
@property
def interval_in_seconds(self) -> int:
    """Get interval in seconds"""

@property
def name(self) -> str:
    """Exchange name"""

# Methods
async def start_network(self):
    """Start fetching candle data"""
    
async def stop_network(self):
    """Stop fetching candle data"""

def load_candles_from_csv(self, data_path: str):
    """Load historical candles from CSV"""
```

### Usage Example

```python
# Check if candles are ready
if candles.ready:
    df = candles.candles_df
    current_price = df.iloc[-1]['close']  # Latest close price
    
# Access specific candle data
latest_candle = candles.candles_df.iloc[-1]
```

## 5. Position Sync Patterns on Startup

### Pattern 1: Direct Position Closing (Directional Strategy)

**File:** `hummingbot/strategy/directional_strategy_base.py`

```python
def close_open_positions(self):
    """Close all open positions when bot stops/starts"""
    for connector_name, connector in self.connectors.items():
        for trading_pair, position in connector.account_positions.items():
            if position.position_side == PositionSide.LONG:
                self.sell(connector_name=connector_name,
                          trading_pair=position.trading_pair,
                          amount=abs(position.amount),
                          order_type=OrderType.MARKET,
                          price=connector.get_mid_price(position.trading_pair),
                          position_action=PositionAction.CLOSE)
            elif position.position_side == PositionSide.SHORT:
                self.buy(connector_name=connector_name,
                         trading_pair=position.trading_pair,
                         amount=abs(position.amount),
                         order_type=OrderType.MARKET,
                         price=connector.get_mid_price(position.trading_pair),
                         position_action=PositionAction.CLOSE)
```

### Pattern 2: Position Detection and Management (LP Management Script)

**File:** `scripts/lp_manage_position.py`

```python
async def check_and_use_existing_position(self):
    """Check for existing positions on startup"""
    await asyncio.sleep(3)  # Wait for connector to initialize

    if await self.check_existing_positions():
        self.position_opened = True
        self.logger().info("Using existing position for monitoring")

async def check_existing_positions(self):
    """Check if user has existing positions"""
    try:
        connector = self.connectors[self.exchange]
        
        # For perpetuals: check account_positions
        if hasattr(connector, 'account_positions'):
            positions = connector.account_positions
            for position_key, position in positions.items():
                if position.trading_pair == self.config.trading_pair:
                    if abs(position.amount) > 0:  # Non-zero position
                        self.position_info = position
                        return True
        return False
    except Exception as e:
        self.logger().debug(f"No existing positions found: {str(e)}")
        return False
```

### Pattern 3: Position Filtering (Spot-Perpetual Arbitrage)

**File:** `hummingbot/strategy/spot_perpetual_arbitrage/spot_perpetual_arbitrage.py`

```python
@property
def perp_positions(self) -> List[Position]:
    """Get active positions for specific trading pair"""
    return [position for position in self._perp_market_info.market.account_positions.values() 
            if position.trading_pair == self._perp_market_info.trading_pair 
            and position.amount != Decimal("0")]
```

### Recommended Pattern for Hyperliquid Startup Sync

```python
def sync_positions_on_startup(self):
    """Detect and close orphaned positions on strategy startup"""
    try:
        connector = self.connectors["hyperliquid_perpetual"]
        
        # Wait for connector to be ready
        if not connector.ready:
            self.logger().warning("Connector not ready, skipping position sync")
            return
            
        positions = connector.account_positions
        
        for position_key, position in positions.items():
            if abs(position.amount) > Decimal("0"):  # Active position
                self.logger().info(f"Found orphaned position: {position}")
                
                # Close the position
                if position.position_side == PositionSide.LONG:
                    self.sell(
                        connector_name="hyperliquid_perpetual",
                        trading_pair=position.trading_pair,
                        amount=abs(position.amount),
                        order_type=OrderType.MARKET,
                        position_action=PositionAction.CLOSE
                    )
                elif position.position_side == PositionSide.SHORT:
                    self.buy(
                        connector_name="hyperliquid_perpetual", 
                        trading_pair=position.trading_pair,
                        amount=abs(position.amount),
                        order_type=OrderType.MARKET,
                        position_action=PositionAction.CLOSE
                    )
                    
        self.logger().info("Position sync completed")
        
    except Exception as e:
        self.logger().error(f"Error during position sync: {str(e)}")
```

### Implementation Notes

1. **Timing:** Always wait for connector initialization (3-5 seconds) before checking positions
2. **Error Handling:** Wrap in try-catch blocks as connectors may not be ready
3. **Position Detection:** Check `abs(position.amount) > 0` for active positions
4. **Closing Logic:** Use opposite direction - sell for LONG, buy for SHORT
5. **Position Action:** Always use `PositionAction.CLOSE` for closing orders
6. **Order Type:** Use `OrderType.MARKET` for immediate execution

---

*Research completed on 2026-02-19 by reviewing actual Hummingbot source code files.*