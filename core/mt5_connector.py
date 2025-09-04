import MetaTrader5 as mt5
import logging
import os
import time
import threading
from .utils import normalize_price, normalize_volume

# A global re-entrant lock for all MT5 operations to ensure thread safety.
# The MetaTrader5 library is not thread-safe. A re-entrant lock (RLock) is
# crucial here because a single thread may need to acquire the lock multiple times
# in a nested fashion (e.g., get_positions -> ensure_connection -> connect).
# A standard Lock() would cause a deadlock in this scenario.
MT5_LOCK = threading.RLock()

# Constants for position confirmation retry logic
MAX_CONFIRM_RETRIES = 5
CONFIRM_RETRY_INTERVAL = 0.1 # seconds

class MT5Connector:
    def __init__(self, account_config, name="MT5Terminal"):
        self.config = account_config
        self.name = name
        self.is_connected = False
        self.terminal_info = None
        self.account_info = None
        # Per-connector lock for operations on this specific instance's data
        self.instance_lock = threading.Lock()
        logging.info(f"[{self.name}] Connector initialized for account {self.config.get('account')}")

    def connect(self):
        with MT5_LOCK:
            logging.info(f"[{self.name}] Attempting to connect to account {self.config.get('account')}...")
            current_terminal = mt5.terminal_info()
            desired_full_path = (self.config.get("terminal_path") or "").rstrip("/\\")
            desired_dir = os.path.dirname(desired_full_path)

            if current_terminal:
                current_path = current_terminal.path.rstrip("/\\")
                path_matches = current_path == desired_full_path or current_path == desired_dir
                acct_info = mt5.account_info() if path_matches else None
                if path_matches and acct_info and acct_info.login == self.config.get("account"):
                    # Already in correct terminal/account; mark connected and return
                    logging.debug(f"[{self.name}] Existing MT5 context already matches desired terminal and account. Skipping re-initialize.")
                    with self.instance_lock:
                        self.is_connected = True
                        self.terminal_info = current_terminal
                        self.account_info = acct_info
                    return True
                # Need to switch ONLY if path differs genuinely (not just exe vs folder path)
                if not path_matches:
                    logging.info(f"[{self.name}] Switching terminal context from {current_terminal.path} to {desired_full_path}. Shutting down first.")
                    mt5.shutdown()
            
            # Securely get password
            password = self.config.get("password")
            if not password:
                password_env_var = f"MT5_PASSWORD_{self.config.get('account')}"
                password = os.getenv(password_env_var)

            # Now, initialize. This is safe even if already initialized for the same path.
            if not mt5.initialize(
                path=self.config.get("terminal_path"),
                login=self.config.get("account"),
                password=password,
                server=self.config.get("server"),
                timeout=10000
            ):
                logging.error(f"[{self.name}] initialize() failed, error code = {mt5.last_error()}")
                with self.instance_lock:
                    self.is_connected = False
                return False

            terminal_info = mt5.terminal_info()
            if not terminal_info:
                logging.error(f"[{self.name}] terminal_info() failed, error code = {mt5.last_error()}")
                mt5.shutdown()
                with self.instance_lock:
                    self.is_connected = False
                return False
            
            account_info = mt5.account_info()
            if not account_info:
                logging.error(f"[{self.name}] account_info() failed, error code = {mt5.last_error()}")
                mt5.shutdown()
                with self.instance_lock:
                    self.is_connected = False
                return False

            logging.info(f"[{self.name}] Successfully connected to {terminal_info.name}, Account: {account_info.login}, Balance: {account_info.balance:.2f}")
            with self.instance_lock:
                self.is_connected = True
                self.terminal_info = terminal_info
                self.account_info = account_info
            return True

    def disconnect(self):
        with self.instance_lock:
            is_connected = self.is_connected
        
        if is_connected:
            with MT5_LOCK:
                # Check if the current context belongs to this instance before shutting down
                current_terminal = mt5.terminal_info()
                if current_terminal and current_terminal.path == self.config.get("terminal_path"):
                    logging.info(f"[{self.name}] Disconnecting from {self.config.get('account')}")
                    mt5.shutdown()
                else:
                    logging.warning(f"[{self.name}] Wanted to disconnect, but global MT5 context was for a different terminal ({current_terminal.path if current_terminal else 'None'}). Not shutting down.")
            with self.instance_lock:
                self.is_connected = False
    
    def ensure_connection(self):
        """
        Ensures the global mt5 object is pointing to THIS terminal instance.
        This is the most critical method for thread safety. It must be called
        before any MT5 operation that depends on a specific account context.
        
        IMPORTANT: This method assumes MT5_LOCK is ALREADY HELD by the caller.
        """
        with self.instance_lock:
            is_connected = self.is_connected
            
        # Fast path: if we think we are connected, check the actual MT5 context
        # This check must also be under MT5_LOCK, which is assumed to be held by the caller.
        if is_connected:
            current_terminal = mt5.terminal_info()
            current_account = mt5.account_info()
            desired_full_path = (self.config.get("terminal_path") or "").rstrip("/\\")
            desired_dir = os.path.dirname(desired_full_path)
            if current_terminal and current_account:
                current_path = current_terminal.path.rstrip("/\\")
                path_matches = current_path == desired_full_path or current_path == desired_dir
                if path_matches and current_account.login == self.config.get("account"):
                    return True # Connection is correct, no need to reconnect

        # Slow path: connection is down or context is wrong, must reconnect
        logging.warning(f"[{self.name}] Re-establishing connection or ensuring context for account {self.config.get('account')}")
        # connect() itself acquires MT5_LOCK, so this is safe.
        return self.connect()


    def get_positions(self, symbol=None, magic=None):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for get_positions.")
                return []
            try:
                if symbol:
                    positions = mt5.positions_get(symbol=symbol)
                else:
                    positions = mt5.positions_get()
                
                if positions is None:
                    logging.warning(f"[{self.name}] No positions found or error: {mt5.last_error()}")
                    return []

                # Filter by magic if specified
                if magic is not None:
                    positions = [p for p in positions if p.magic == magic]
                return list(positions)
            except Exception as e:
                logging.error(f"[{self.name}] Error getting positions: {e}")
                return []

    def debug_positions_report(self):
        """Collects diagnostic info about current MT5 positions state for this connector."""
        with MT5_LOCK:
            report = {"name": self.name}
            try:
                ensured = self.ensure_connection()
                report["ensured_connection"] = ensured
                term_info = mt5.terminal_info() if ensured else None
                report["terminal_path"] = getattr(term_info, 'path', None)
                acct = mt5.account_info() if ensured else None
                report["account"] = getattr(acct, 'login', None)
                report["equity"] = getattr(acct, 'equity', None)
                raw_positions = mt5.positions_get()
                report["positions_total_api"] = len(raw_positions) if raw_positions else 0
                report["positions_total_func"] = mt5.positions_total()
                report["positions_sample"] = [
                    {"ticket": p.ticket, "symbol": p.symbol, "type": p.type, "volume": p.volume}
                    for p in list(raw_positions or [])[:5]
                ]
                # Pending orders (might indicate user opened an order not yet filled)
                raw_orders = mt5.orders_get()
                report["orders_total_api"] = len(raw_orders) if raw_orders else 0
                report["orders_sample"] = [
                    {"ticket": o.ticket, "symbol": o.symbol, "type": o.type, "volume_current": getattr(o, 'volume_current', None)}
                    for o in list(raw_orders or [])[:5]
                ]
                # Recent deals (last 5 minutes) to see if trades executed
                from_time = time.time() - 300
                to_time = time.time()
                recent_deals = mt5.history_deals_get(from_time, to_time)
                report["recent_deals_count"] = len(recent_deals) if recent_deals else 0
                if recent_deals:
                    report["recent_deals_sample"] = [
                        {"deal": d.ticket, "position_id": d.position_id, "symbol": d.symbol, "entry": d.entry, "volume": d.volume}
                        for d in list(recent_deals)[:5]
                    ]
                report["last_error"] = mt5.last_error()
            except Exception as e:
                report["exception"] = str(e)
            logging.warning(f"[{self.name}] DEBUG_POS_REPORT: {report}")
            return report

    def get_symbol_info(self, symbol_name):
        with MT5_LOCK:
            if not self.ensure_connection():
                logging.error(f"[{self.name}] Failed to ensure connection for get_symbol_info.")
                return None
            
            # First, ensure the symbol is visible in Market Watch
            if not mt5.symbol_select(symbol_name, True):
                logging.warning(f"[{self.name}] mt5.symbol_select({symbol_name}, True) failed. The symbol may not be available on the broker. Error: {mt5.last_error()}")
                # Even if select fails, we can still try to get info. Some brokers might not need it.

            info = mt5.symbol_info(symbol_name)
            if not info:
                logging.warning(f"[{self.name}] Could not get symbol info for {symbol_name}: {mt5.last_error()}")
            return info

    def get_tick_info(self, symbol_name):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for get_tick_info.")
                return None
            tick = mt5.symbol_info_tick(symbol_name)
            if not tick:
                logging.warning(f"[{self.name}] Could not get tick info for {symbol_name}: {mt5.last_error()}")
            return tick

    def open_trade(self, symbol, lot_size, order_type, sl_price, tp_price, deviation_points, magic_number, comment=""):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for open_trade.")
                return None
            
            # These calls are now safe because MT5_LOCK is already held by open_trade
            symbol_info = self.get_symbol_info(symbol)
            if not symbol_info:
                logging.error(f"[{self.name}] Cannot open trade, symbol info not found for {symbol}")
                return None

            tick_info = self.get_tick_info(symbol)
            if tick_info is None:
                logging.error(f"[{self.name}] Could not get tick info for {symbol}. Cannot open trade.")
                return None

            price = 0.0
            if order_type == mt5.ORDER_TYPE_BUY:
                price = tick_info.ask
            elif order_type == mt5.ORDER_TYPE_SELL:
                price = tick_info.bid
            else:
                logging.error(f"[{self.name}] Invalid order type: {order_type}")
                return None

            if price == 0.0:
                logging.error(f"[{self.name}] Market price is zero for {symbol}. Cannot open trade.")
                return None
                
            final_lot_size = normalize_volume(symbol_info, lot_size)
            if final_lot_size <= 0:
                logging.error(f"[{self.name}] Invalid lot size {lot_size} (normalized to {final_lot_size}) for {symbol}. Min lot: {symbol_info.volume_min}")
                return None

            # Check margin (Important!)
            margin_required = mt5.order_calc_margin(order_type, symbol, final_lot_size, price)
            if margin_required is None:
                logging.warning(f"[{self.name}] Could not calculate margin for {symbol} lot {final_lot_size}. Error: {mt5.last_error()}")
            elif self.account_info and self.account_info.margin_free < margin_required:
                logging.error(f"[{self.name}] Insufficient free margin for {symbol} lot {final_lot_size}. Required: {margin_required:.2f}, Free: {self.account_info.margin_free:.2f}")
                return None


            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": final_lot_size,
                "type": order_type,
                "price": price,
                "sl": normalize_price(symbol_info, sl_price),
                "tp": normalize_price(symbol_info, tp_price),
                "deviation": deviation_points,
                "magic": magic_number,
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC, # Or FOK, check broker
            }
            
            logging.info(f"[{self.name}] Sending trade request: {request}")
            result = mt5.order_send(request)

            if result is None:
                logging.error(f"[{self.name}] order_send failed, error code = {mt5.last_error()}")
                return None
            
            if result.retcode not in (mt5.TRADE_RETCODE_DONE, getattr(mt5, 'TRADE_RETCODE_PLACED', -999)):
                broker_comment = getattr(result, 'comment_broker', None)
                logging.error(
                    f"[{self.name}] order_send failed. Retcode: {result.retcode} - {result.comment} "
                    f"(Broker: {broker_comment if broker_comment is not None else 'N/A'}) Request: {result.request}"
                )
                failure_data = {
                    "retcode": result.retcode,
                    "comment": result.comment,
                    "broker_comment": broker_comment,
                    "request": result.request._asdict() if hasattr(result.request, '_asdict') else str(result.request),
                    "position_ticket": 0
                }
                try:
                    result_dict = {k: getattr(result, k) for k in dir(result) if not k.startswith('_') and not callable(getattr(result, k))}
                    failure_data["raw_result"] = result_dict
                except Exception:
                    pass
                return failure_data
            
            logging.info(f"[{self.name}] Trade executed/placed successfully. Order: {result.order}, Deal: {result.deal}, Position: {result.position if hasattr(result, 'position') else 'N/A'}")
            
            # Create a dictionary to return, as OrderSendResult objects are immutable
            # and do not allow adding new attributes like 'position_ticket'.
            response_data = {
                "retcode": result.retcode,
                "deal": result.deal,
                "order": result.order,
                "volume": result.volume,
                "price": result.price,
                "bid": result.bid,
                "ask": result.ask,
                "comment": result.comment,
                "request": result.request,
                "position_ticket": 0 # Default to 0, will be updated if found
            }

            if hasattr(result, 'position') and result.position > 0:
                response_data["position_ticket"] = result.position
            elif hasattr(result, 'deal') and result.deal > 0:
                # Try to get the position ticket from the deal
                deals = mt5.history_deals_get(ticket=result.deal)
                if deals and len(deals) > 0:
                    response_data["position_ticket"] = deals[0].position_id
            
            # Confirm the position is visible in get_positions()
            if response_data["position_ticket"] > 0:
                confirmed_position = None
                for i in range(MAX_CONFIRM_RETRIES):
                    time.sleep(CONFIRM_RETRY_INTERVAL)
                    positions = mt5.positions_get(ticket=response_data["position_ticket"])
                    if positions and len(positions) > 0:
                        confirmed_position = positions[0]
                        logging.info(f"[{self.name}] Position {response_data['position_ticket']} confirmed after {i+1} retries.")
                        break
                if not confirmed_position:
                    logging.warning(f"[{self.name}] Position {response_data['position_ticket']} not confirmed via get_positions after {MAX_CONFIRM_RETRIES} retries.")
                    # Decide if this is a critical failure or just a warning.
                    # For now, we return the ticket even if not confirmed, but log the warning.
            
            return response_data

    def close_trade(self, position_ticket, volume_to_close, deviation_points, comment=""):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for close_trade.")
                return None

            position = mt5.positions_get(ticket=position_ticket)
            if not position or len(position) == 0:
                logging.error(f"[{self.name}] Position ticket {position_ticket} not found for closing.")
                return None
            
            pos_data = position[0]
            # These calls are now safe because MT5_LOCK is already held by close_trade
            symbol_info = self.get_symbol_info(pos_data.symbol)
            if not symbol_info: return None

            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "position": position_ticket,
                "symbol": pos_data.symbol,
                "volume": normalize_volume(symbol_info, volume_to_close),
                "type": mt5.ORDER_TYPE_SELL if pos_data.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY,
                "deviation": deviation_points,
                "magic": pos_data.magic, # Use original magic for close often
                "comment": comment,
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            # Price for closing market orders is determined by broker
            
            logging.info(f"[{self.name}] Sending close request: {request}")
            result = mt5.order_send(request)

            if result is None:
                logging.error(f"[{self.name}] close_trade failed (order_send is None), error code = {mt5.last_error()}")
                return None
            
            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logging.error(f"[{self.name}] close_trade failed. Retcode: {result.retcode} - {result.comment} (Broker: {result.comment_broker})")
                return None
            
            logging.info(f"[{self.name}] Trade closed successfully. Order: {result.order}, Deal: {result.deal}")
            return result

    def modify_position_sltp(self, position_ticket, new_sl, new_tp):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for modify_position_sltp.")
                return None

            position = mt5.positions_get(ticket=position_ticket)
            if not position or len(position) == 0:
                logging.error(f"[{self.name}] Position ticket {position_ticket} not found for SL/TP modification.")
                return None
            pos_data = position[0]
            # These calls are now safe because MT5_LOCK is already held by modify_position_sltp
            symbol_info = self.get_symbol_info(pos_data.symbol)
            if not symbol_info: return None

            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "position": position_ticket,
                "sl": normalize_price(symbol_info, new_sl),
                "tp": normalize_price(symbol_info, new_tp),
            }
            logging.info(f"[{self.name}] Sending SL/TP modification request: {request}")
            result = mt5.order_send(request)

            if result is None:
                logging.error(f"[{self.name}] modify_position_sltp failed (order_send is None), error code = {mt5.last_error()}")
                return None

            if result.retcode != mt5.TRADE_RETCODE_DONE:
                logging.error(f"[{self.name}] modify_position_sltp failed. Retcode: {result.retcode} - {result.comment}")
                return None
            
            logging.info(f"[{self.name}] Position SL/TP modified successfully. Order: {result.order}")
            return result

    def get_deals_for_position(self, position_id):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for get_deals_for_position.")
                return []
            deals = mt5.history_deals_get(position=position_id) # This gets deals related to a position ticket
            if deals is None:
                logging.warning(f"[{self.name}] Could not get deals for position {position_id}: {mt5.last_error()}")
                return []
            return list(deals)

    def get_current_account_info(self):
        with MT5_LOCK:
            if not self.ensure_connection(): 
                logging.error(f"[{self.name}] Failed to ensure connection for get_current_account_info.")
                return None
            self.account_info = mt5.account_info()
            return self.account_info

    # --- Diagnostic / Environment Helpers ---
    def startup_diagnostics(self):
        """Log extended diagnostics at startup to help trace why positions might be missing."""
        with MT5_LOCK:
            info = {"name": self.name}
            try:
                ensured = self.ensure_connection()
                info["ensured_connection"] = ensured
                term = mt5.terminal_info() if ensured else None
                acct = mt5.account_info() if ensured else None
                info.update({
                    "terminal_path": getattr(term, 'path', None),
                    "terminal_company": getattr(term, 'company', None) if term else None,
                    "account_login": getattr(acct, 'login', None),
                    "account_name": getattr(acct, 'name', None),
                    "account_server": getattr(acct, 'server', None),
                    "trade_allowed": getattr(acct, 'trade_allowed', None),
                    "trade_mode": getattr(acct, 'trade_mode', None),
                    "margin_mode": getattr(acct, 'margin_mode', None),
                    "positions_total_initial": mt5.positions_total() if ensured else None,
                    "orders_total_initial": len(mt5.orders_get() or []) if ensured else None,
                })
                # Sample of recent deals (last 24h)
                now = time.time()
                deals = mt5.history_deals_get(now - 86400, now) if ensured else None
                info["recent_deals_24h"] = len(deals) if deals else 0
                if deals:
                    latest = sorted(deals, key=lambda d: d.time, reverse=True)[:3]
                    info["recent_deals_sample"] = [
                        {"deal": d.ticket, "position_id": d.position_id, "symbol": d.symbol, "entry": d.entry, "volume": d.volume, "time": d.time}
                        for d in latest
                    ]
                # Market watch sample (first 10 symbols)
                symbols = mt5.symbols_get() if ensured else []
                if symbols:
                    info["market_watch_sample"] = [s.name for s in symbols[:10]]
            except Exception as e:
                info["exception"] = str(e)
            logging.warning(f"[{self.name}] STARTUP_DIAGNOSTICS: {info}")
            return info
