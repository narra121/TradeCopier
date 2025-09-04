import logging
from logging.handlers import RotatingFileHandler
import uuid
import os
from datetime import datetime
import sys
import math

def rotate_log_file(log_file_path):
    """Legacy no-op rotation retained for backward compatibility (runtime rotation handled by RotatingFileHandler)."""
    return

def setup_logging(log_file_path="logs/app.log", level=logging.INFO, max_bytes=10*1024*1024, backup_count=19):
    abs_log_path = os.path.abspath(log_file_path)
    print(f"--- Attempting to log to: {abs_log_path} ---")
    log_dir = os.path.dirname(abs_log_path)
    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
            print(f"--- Created log directory: {log_dir} ---")
        except Exception as e:
            print(f"--- FAILED to create log directory {log_dir}: {e} ---")
            return # Cannot proceed with file logging

    # Legacy manual rotation removed; RotatingFileHandler enforces size/retention.

    try:
        # BasicConfig should only be called once.
        # If this is not the first call, it might not reconfigure.
        # Forcing it can help during debugging, but ensure it's structured to be called once.
        rotating_handler = RotatingFileHandler(abs_log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
        rotating_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s"))
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5.5s]  %(message)s"))
        logging.basicConfig(level=level, handlers=[rotating_handler, stream_handler], force=True)
        logging.info("--- TEST: Logging initialized by basicConfig ---")
        print("--- FileHandler should be active. Check the log file. ---")
    except Exception as e:
        print(f"--- FAILED to initialize FileHandler for {abs_log_path}: {e} ---")
        # Fallback to console only
        logging.basicConfig(level=level, format="%(asctime)s [%(levelname)-5.5s]  %(message)s", handlers=[logging.StreamHandler(sys.stdout)], force=True)
        logging.info("--- TEST: Logging to CONSOLE ONLY due to FileHandler error ---")

def generate_universal_trade_id():
    """Generates a unique trade ID."""
    return str(uuid.uuid4())

def normalize_symbol(symbol_info, symbol_name):
    """Placeholder for complex symbol normalization if needed later"""
    # For now, just return as is, but this could handle suffixes/prefixes
    return symbol_name

def normalize_price(symbol_info, price):
    """Normalizes price to the correct number of digits for the symbol."""
    if symbol_info and price != 0:
        return round(price, symbol_info.digits)
    return price

def normalize_volume(symbol_info, volume):
    """Normalizes volume to lot step and min/max lots."""
    if not symbol_info or volume <= 0:
        return 0.0

    lot_step = symbol_info.volume_step
    min_lot = symbol_info.volume_min
    max_lot = symbol_info.volume_max

    # Correctly align to lot step by flooring, not rounding.
    # e.g., if volume is 0.019 and step is 0.01, it should be 0.01, not 0.02.
    if lot_step > 0:
        normalized_volume = math.floor(volume / lot_step) * lot_step
    else:
        normalized_volume = volume # Should not happen, but as a fallback

    # Clamp to min/max
    normalized_volume = max(min_lot, min(normalized_volume, max_lot))
    
    # Ensure it's not below min_lot due to rounding small numbers
    if normalized_volume < min_lot and volume > 0: # only if original volume was > 0
        return min_lot
        
    return round(normalized_volume, 8) # MT5 volumes can have many decimals

def get_datetime_from_timestamp(ts):
    if ts > 0:
        return datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
    return ""