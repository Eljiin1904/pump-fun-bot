# src/core/__init__.py

# Import directly available classes/modules
from .client import SolanaClient
from .wallet import Wallet
from .transactions import TransactionSendResult, get_transaction_fee # Use correct name, add utility
from .instruction_builder import InstructionBuilder
from .curve import BondingCurveManager, BondingCurveState # Remove CurveHelper
from .pubkeys import PumpAddresses, SolanaProgramAddresses # Import key containers

# Note: PriorityFeeManager is in a sub-package, import directly where needed:
# from src.core.priority_fee.manager import PriorityFeeManager

# Note: raydium_amm_v4 is also usually imported directly where needed

# Define __all__ for explicit public interface of this package
__all__ = [
    "SolanaClient",
    "Wallet",
    "TransactionSendResult", # Correct name
    "get_transaction_fee",   # Export utility
    "InstructionBuilder",
    "BondingCurveManager",
    "BondingCurveState",
    "PumpAddresses",         # Export key container
    "SolanaProgramAddresses", # Export key container
]

# If you define custom exceptions in src/core/exceptions.py, uncomment and add here:
# from .exceptions import (
#     PumpFunBotException, ProcessingError, PrefetchError, BuildTransactionError,
#     SendTransactionError, BuyError, SellError, InsufficientFundsError, SlippageError
# )
# __all__.extend([
#     "PumpFunBotException", "ProcessingError", "PrefetchError", "BuildTransactionError",
#     "SendTransactionError", "BuyError", "SellError", "InsufficientFundsError", "SlippageError"
# ])

# If you define enums like TradeDirection in src/core/enums.py, uncomment and add here:
# from .enums import TradeDirection # Add others if they exist
# __all__.extend([
#     "TradeDirection",
# ])