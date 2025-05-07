# src/core/exceptions.py

class PumpFunBotException(Exception):
    """Base class for custom exceptions in this application."""
    pass

class ProcessingError(PumpFunBotException):
    """For general processing issues."""
    pass

class PrefetchError(PumpFunBotException):
    """For errors during data prefetching or pre-calculation."""
    pass

class BuildTransactionError(PumpFunBotException):
    """For errors during transaction construction."""
    pass

class SendTransactionError(PumpFunBotException):
    """For errors during sending or confirming a transaction."""
    pass

class BuyError(PumpFunBotException):
    """Specific error related to buying tokens."""
    pass

class SellError(PumpFunBotException):
    """Specific error related to selling tokens."""
    pass

class InsufficientFundsError(PumpFunBotException):
    """For insufficient funds errors."""
    pass

class SlippageError(PumpFunBotException):
    """For errors due to slippage."""
    pass

# Add any other specific exceptions your bot might need