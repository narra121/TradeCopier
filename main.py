import tkinter as tk
import json
import logging
import os
import argparse
import sys

# Add project root to Python path to allow imports from core and gui
project_root = os.path.dirname(os.path.abspath(__file__))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.trade_manager import TradeManager
from core.utils import setup_logging
from gui.main_window import TradeCopierApp

CONFIG_FILE_PATH = os.path.join(project_root, "config", "config.json")
DEFAULT_LOG_FILE = os.path.join(project_root, "logs", "app.log") # Default if not in config

def load_configuration(config_path):
    """Loads the JSON configuration file."""
    try:
        with open(config_path, 'r') as f:
            config = json.load(f)
        logging.info(f"Configuration loaded successfully from {config_path}")
        return config
    except FileNotFoundError:
        logging.error(f"FATAL: Configuration file not found at {config_path}")
        return None
    except json.JSONDecodeError as e:
        logging.error(f"FATAL: Error decoding JSON from {config_path}: {e}")
        return None
    except Exception as e:
        logging.error(f"FATAL: An unexpected error occurred while loading configuration: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="MT5 Trade Copier")
    parser.add_argument("--config", default=os.path.join(project_root, "config", "config_dev.json"), help="Path to the configuration file.")
    args = parser.parse_args()

    # --- 1. Load Configuration ---
    config_path_to_load = args.config
    
    config = load_configuration(config_path_to_load)
    if config is None:
        # If config load fails, setup basic logging to console at least
        logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)-5.5s]  %(message)s")
        logging.error("Application cannot start due to configuration issues.")
        # Attempt to show a simple error dialog if Tkinter is available
        try:
            root = tk.Tk()
            root.withdraw() # Hide the main window
            tk.messagebox.showerror("Configuration Error", f"Failed to load configuration from {config_path_to_load}.\nCheck logs for details.\nApplication will now exit.")
            root.destroy()
        except Exception:
            pass # If Tkinter itself fails, just exit
        return # Exit application

    # --- 2. Setup Logging (using path and level from config) ---
    settings = config.get("settings", {})
    log_file_path = settings.get("log_file", DEFAULT_LOG_FILE)
    log_level_str = settings.get("log_level", "INFO").upper()
    
    # Convert string log level to logging constant
    log_level = getattr(logging, log_level_str, logging.INFO)
    
    # Log rolling settings from config (with safe defaults)
    max_size_mb = settings.get("log_max_size_mb", 10)
    max_files = settings.get("log_max_files", 20)
    try:
        max_size_mb = float(max_size_mb)
    except Exception:
        max_size_mb = 10
    try:
        max_files = int(max_files)
    except Exception:
        max_files = 20
    if max_size_mb <= 0:
        max_size_mb = 10
    if max_files < 1:
        max_files = 1
    backup_count = max(0, max_files - 1)

    setup_logging(log_file_path, level=log_level, max_bytes=int(max_size_mb * 1024 * 1024), backup_count=backup_count)
    
    logging.info(f"Logging initialized at level {log_level_str}")
    
    # If the main log level is DEBUG, ensure MetaTrader5 logger is also set to DEBUG
    if log_level == logging.DEBUG:
        logging.getLogger("MetaTrader5").setLevel(logging.DEBUG)
    else:
        # Otherwise, set it to a higher level to avoid excessive noise
        logging.getLogger("MetaTrader5").setLevel(logging.WARNING)

    logging.info("Application starting...")


    # --- 3. Initialize GUI (Tkinter main window) ---
    # The GUI must run in the main thread.
    root = tk.Tk()
    
    # --- 4. Initialize TradeManager ---
    # TradeManager needs a queue to send updates to the GUI.
    # The GUI needs a queue to send actions to the TradeManager.
    gui_app = TradeCopierApp(root, action_queue_to_manager=None) # Action queue will be set after TM init

    trade_manager = TradeManager(config, gui_queue=gui_app.gui_update_queue)
    gui_app.action_queue_to_manager = trade_manager.action_queue # Now link TM's action queue to GUI

    # --- 5. Start TradeManager (in a separate thread) ---
    if not trade_manager.start():
        logging.error("Failed to start TradeManager. Application may not function correctly.")
        gui_app.log_message("System", "ERROR: TradeManager failed to start. Check logs.")
        # Optionally, decide if GUI should still run or exit
        # For now, let the GUI run so user can see the error.
    else:
        logging.info("TradeManager initialization successful and started.")


    # --- 6. Set up graceful shutdown for the GUI ---
    # This function will be called when the user tries to close the window.
    def on_app_closing():
        gui_app.on_closing(trade_manager.stop) # Pass the stop method of trade_manager

    root.protocol("WM_DELETE_WINDOW", on_app_closing)


    # --- 7. Start Tkinter Main Event Loop ---
    logging.info("Starting GUI main loop...")
    try:
        root.mainloop()
    except KeyboardInterrupt:
        logging.info("Application interrupted by user (Ctrl+C).")
        # Perform cleanup similar to on_app_closing
        if trade_manager and trade_manager.running:
            gui_app.log_message("System", "Shutdown initiated by KeyboardInterrupt...")
            trade_manager.stop() # Attempt to stop manager cleanly
    finally:
        logging.info("Application has shut down.")
        # Ensure any MT5 connections are explicitly shut down if TM didn't fully stop
        # This is more of a failsafe; trade_manager.stop() should handle it.
        if trade_manager and trade_manager.provider_connector and trade_manager.provider_connector.is_connected:
            trade_manager.provider_connector.disconnect()
        if trade_manager and trade_manager.receiver_connectors:
            for rc in trade_manager.receiver_connectors:
                if rc.is_connected:
                    rc.disconnect()

if __name__ == "__main__":
    # Create necessary directories if they don't exist, based on typical config values
    os.makedirs(os.path.join(project_root, "data"), exist_ok=True)
    os.makedirs(os.path.join(project_root, "logs"), exist_ok=True)
    
    main()