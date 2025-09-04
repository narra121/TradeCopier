import logging
import time
import json
import os
import threading
from queue import Queue, Empty

from core.mt5_connector import MT5Connector
from .utils import generate_universal_trade_id, get_datetime_from_timestamp, normalize_volume
import MetaTrader5 as mt5

DUPLICATE_COMMENT_PREFIX = "DUPLICATE_OF:"

RETRY_INTERVAL_FAILED_COPY = 30 # seconds

class TradeManager:
    def __init__(self, config, gui_queue):
        self.config = config
        self.gui_queue = gui_queue  # For sending updates to GUI
        self.action_queue = Queue() # For receiving actions from GUI (e.g., close trade)

        self.provider_connector = MT5Connector(config['provider'], name=f"Provider-{config['provider']['account']}")
        self.receiver_connectors = []
        for rec_config in config['receivers']:
            if rec_config.get("enabled", True):
                # Normalize symbol mapping keys (handle mis-typed 'RecieverSymbol')
                for m in rec_config.get("SymbolMapping", []):
                    if 'ReceiverSymbol' not in m:
                        # Common misspelling variants
                        for alt in ('RecieverSymbol', 'receiverSymbol', 'recieverSymbol'):
                            if alt in m:
                                m['ReceiverSymbol'] = m[alt]
                                break
                self.receiver_connectors.append(
                    MT5Connector(rec_config, name=f"Receiver-{rec_config['account']}")
                )

        self.state_file = config['settings'].get('state_file', 'data/trade_copier_state.json')
        self.trade_state = self.load_trade_state() # {universal_id: {provider_ticket: X, receivers: {rec_name: {ticket: Y, status: "copied"|"attempted", last_attempt: Z}}, manually_closed: False}}

        self.running = False
        self.thread = None
        self.lock = threading.RLock() # Use RLock to allow nested lock acquisition by the same thread
        # Feature flag: duplicate provider trades (open an automatic duplicate on provider when a new manual trade is detected)
        self.duplicate_provider_trades = self.config.get('settings', {}).get('duplicate_provider_trades', False)
        self.duplicate_retry_interval = self.config.get('settings', {}).get('duplicate_retry_interval_seconds', RETRY_INTERVAL_FAILED_COPY)
        # Logging mode: if True, only log actionable trade events (open/close/modify/duplicate/errors)
        self.log_actions_only = self.config.get('settings', {}).get('log_actions_only', False)
        # Whether to close underlying MT5 terminal processes on shutdown
        self.auto_close_terminals = self.config.get('settings', {}).get('auto_close_terminals', True)
        if self.log_actions_only:
            logging.info("Actions-only logging mode ENABLED: routine cycle and skip logs suppressed.")
        else:
            logging.debug("Actions-only logging mode disabled (full verbose logging).")
        
    def load_trade_state(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                    logging.info(f"Loaded trade state from {self.state_file}")
                    # Ensure 'manually_closed' and 'receivers' keys exist for old states
                    for uid, data in state.items():
                        if 'manually_closed' not in data:
                            data['manually_closed'] = False
                        
                        # Migrate old 'receivers' format if necessary
                        if 'receivers' not in data or not isinstance(data['receivers'], dict):
                            data['receivers'] = {}
                        else:
                            for rec_name, rec_ticket_or_data in list(data['receivers'].items()):
                                if isinstance(rec_ticket_or_data, (int, float)): # Old format: {rec_name: ticket}
                                    data['receivers'][rec_name] = {"ticket": rec_ticket_or_data, "status": "copied"}
                                elif not isinstance(rec_ticket_or_data, dict) or "status" not in rec_ticket_or_data:
                                    # Handle cases where it's a dict but missing status (e.g., partially migrated)
                                    data['receivers'][rec_name] = {"ticket": rec_ticket_or_data.get("ticket"), "status": rec_ticket_or_data.get("status", "copied")}
                                    if "last_attempt" not in data['receivers'][rec_name] and data['receivers'][rec_name]["status"] == "attempted":
                                        data['receivers'][rec_name]["last_attempt"] = time.time() # Set current time if missing for attempted
                    return state
        except Exception as e:
            logging.error(f"Error loading trade state: {e}")
        return {}

    def save_trade_state(self):
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
             # Ensure thread-safe write
            with open(self.state_file, 'w') as f:
                json.dump(self.trade_state, f, indent=4)
            if not self.log_actions_only:
                logging.debug(f"Saved trade state to {self.state_file}")
        except Exception as e:
            logging.error(f"Error saving trade state: {e}")

    def _get_universal_id_by_provider_ticket(self, provider_ticket):

        for uid, data in self.trade_state.items():
            if data.get("provider_ticket") == provider_ticket:
                return uid
        return None
    
    def _get_universal_id_by_receiver_ticket(self, receiver_name, receiver_ticket):
       
        for uid, data in self.trade_state.items():
            rec_data = data.get("receivers", {}).get(receiver_name)
            if rec_data and rec_data.get("ticket") == receiver_ticket:
                return uid
        return None

    def start(self):
        self.running = True
        # Connect to all terminals first
        if not self.provider_connector.connect():
            self.gui_queue.put({"type": "status", "account": self.provider_connector.name, "message": "Provider connection failed. Aborting."})
            logging.error("Provider connection failed. Trade manager cannot start.")
            self.running = False
            return False
        else:
            # Log extended diagnostics once at startup for provider
            try:
                self.provider_connector.startup_diagnostics()
            except Exception as e:
                logging.error(f"Provider startup diagnostics failed: {e}")

        all_receivers_connected = True
        for rec_conn in self.receiver_connectors:
            if not rec_conn.connect():
                self.gui_queue.put({"type": "status", "account": rec_conn.name, "message": f"{rec_conn.name} connection failed."})
                logging.warning(f"{rec_conn.name} connection failed.")
                # We can choose to continue or abort if a receiver fails. For now, continue.
                # all_receivers_connected = False
            else:
                 self.gui_queue.put({"type": "status", "account": rec_conn.name, "message": f"{rec_conn.name} connected."})
                 try:
                     rec_conn.startup_diagnostics()
                 except Exception as e:
                     logging.error(f"{rec_conn.name} startup diagnostics failed: {e}")


        self.thread = threading.Thread(target=self._run_loop, name="TradeManagerThread")
        self.thread.daemon = True # Allows main program to exit even if this thread is running
        self.thread.start()
        logging.info("TradeManager started.")
        self.gui_queue.put({"type": "status", "account": "System", "message": "Trade Manager Started."})
        return True


    def stop(self):
        self.running = False
        if self.thread and self.thread.is_alive():
            logging.info("Stopping TradeManager...")
            self.thread.join(timeout=10) # Wait for the thread to finish
        
        self.provider_connector.disconnect()
        if self.auto_close_terminals:
            try:
                self.provider_connector.terminate_terminal()
            except Exception as e:
                logging.error(f"Failed to terminate provider terminal: {e}")
        for rec_conn in self.receiver_connectors:
            rec_conn.disconnect()
            if self.auto_close_terminals:
                try:
                    rec_conn.terminate_terminal()
                except Exception as e:
                    logging.error(f"Failed to terminate receiver terminal {rec_conn.name}: {e}")
        
        self.save_trade_state() # Final save
        logging.info("TradeManager stopped.")
        self.gui_queue.put({"type": "status", "account": "System", "message": "Trade Manager Stopped."})

    def _run_loop(self):
        min_interval_sec = self.config['settings'].get('default_processing_interval_ms', 1000) / 1000.0
        
        while self.running:
            start_time = time.time()
            try:
                # Handle actions from GUI. This method now returns messages to be sent.
                gui_action_messages = self._process_gui_actions()

                # Core logic: check provider, sync receivers. Returns messages.
                sync_messages = self._synchronize_trades()
                
                # Update GUI with current open trades. Returns one message.
                gui_update_message = self._send_open_trades_to_gui()

                # Now, send all collected messages after all locks have been released.
                for msg in gui_action_messages:
                    self.gui_queue.put(msg)
                for msg in sync_messages:
                    self.gui_queue.put(msg)
                if gui_update_message:
                    if not self.log_actions_only:
                        logging.debug(f"Sending to GUI: {gui_update_message}")
                    self.gui_queue.put(gui_update_message)

            except Exception as e:
                logging.exception("Error in TradeManager run loop:")
                # This put is safe as it's in an exception handler where locks are likely released
                self.gui_queue.put({"type": "error", "message": f"TradeManager loop error: {e}"})

            # Ensure consistent polling interval
            elapsed_time = time.time() - start_time
            sleep_duration = max(0, min_interval_sec - elapsed_time)
            time.sleep(sleep_duration)
        
        logging.info("TradeManager run loop finished.")

    def _process_gui_actions(self):
        """Processes actions from the GUI queue and returns a list of messages to be sent."""
        messages_to_send = []
        try:
            while not self.action_queue.empty():
                action = self.action_queue.get_nowait()
                logging.info(f"Processing GUI action: {action}")
                action_type = action.get("type")

                if action_type == "close_universal_trade":
                    uid = action.get("universal_id")
                    messages_to_send.extend(self._handle_close_universal_trade(uid, manual_close=True))
                elif action_type == "close_all_trades":
                    messages_to_send.extend(self._handle_close_all_trades())
                
                self.action_queue.task_done()
        except Empty:
            pass # No actions from GUI
        except Exception as e:
            logging.error(f"Error processing GUI action: {e}")
            messages_to_send.append({"type": "error", "message": f"GUI action processing error: {e}"})
        return messages_to_send

    def _handle_close_universal_trade(self, universal_id, manual_close=False):
        """
        Handles closing a universal trade group. This method is thread-safe
        and returns a list of GUI messages.
        """
        gui_messages = []
        with self.lock:
            if universal_id not in self.trade_state:
                logging.warning(f"Attempted to close non-existent universal trade ID: {universal_id}")
                return []

            trade_data = self.trade_state[universal_id]
            provider_ticket = trade_data.get("provider_ticket")
                
            if provider_ticket:
                self.provider_connector.ensure_connection()
                prov_pos = self.provider_connector.get_positions(symbol=None)
                actual_prov_pos = next((p for p in prov_pos if p.ticket == provider_ticket), None)

                if actual_prov_pos:
                    logging.info(f"Closing provider trade {provider_ticket} for universal ID {universal_id}")
                    gui_messages.append({"type": "status", "account": self.provider_connector.name, "message": f"Closing trade {provider_ticket} (UID {universal_id[:8]}) initiated."})
                    self.provider_connector.close_trade(actual_prov_pos.ticket, actual_prov_pos.volume, 100, f"Close UID {universal_id[:8]}")
                else:
                    logging.info(f"Provider trade {provider_ticket} for UID {universal_id} already closed or not found.")
                    gui_messages.append({"type": "status", "account": self.provider_connector.name, "message": f"Trade {provider_ticket} (UID {universal_id[:8]}) already closed."})
            
            for rec_conn in self.receiver_connectors:
                rec_name = rec_conn.name
                receiver_ticket = trade_data.get("receivers", {}).get(rec_name)
                if receiver_ticket:
                    rec_conn.ensure_connection()
                    rec_positions = rec_conn.get_positions(magic=rec_conn.config.get("magic_number"))
                    actual_rec_pos = next((p for p in rec_positions if p.ticket == receiver_ticket), None)
                    
                    if actual_rec_pos:
                        logging.info(f"Closing receiver trade {receiver_ticket} on {rec_name} for UID {universal_id}")
                        gui_messages.append({"type": "status", "account": rec_name, "message": f"Closing trade {receiver_ticket} for UID {universal_id[:8]}"})
                        rec_conn.close_trade(actual_rec_pos.ticket, actual_rec_pos.volume, rec_conn.config.get("price_deviation_points", 50), f"Close UID {universal_id[:8]}")
                    else:
                         logging.info(f"Receiver trade {receiver_ticket} for UID {universal_id} on {rec_name} already closed or not found.")

            if manual_close:
                if universal_id in self.trade_state:
                    self.trade_state[universal_id]["manually_closed"] = True
                    logging.info(f"Universal trade ID {universal_id} marked as manually closed.")
            
            self._cleanup_closed_universal_trade(universal_id)
            self.save_trade_state()
        return gui_messages

    def _handle_close_all_trades(self):
        """
        Handles closing all active trades. This method is thread-safe and returns
        a list of GUI messages.
        """
        gui_messages = []
        with self.lock:
            logging.info("Processing 'Close All Trades' request.")
            uids_to_close = [uid for uid, data in self.trade_state.items() if not data.get("manually_closed", False)]
            
            if not uids_to_close:
                logging.info("No active universal trades found to close.")
                gui_messages.append({"type": "status", "account": "System", "message": "No active trades to close."})
                return gui_messages

            logging.info(f"Found {len(uids_to_close)} universal trades to close: {uids_to_close}")
            for uid in uids_to_close:
                # This nested call is safe due to RLock. The returned messages are extended.
                gui_messages.extend(self._handle_close_universal_trade(uid, manual_close=True))
            
            gui_messages.append({"type": "status", "account": "System", "message": f"Attempted to close {len(uids_to_close)} trade groups."})
        return gui_messages

    def _cleanup_closed_universal_trade(self, universal_id):
        """
        Checks if a trade group is fully closed on all terminals and removes it from the state.
        Assumes the lock is already held.
        """
        if universal_id not in self.trade_state:
            return

        trade_data = self.trade_state[universal_id]
        provider_ticket = trade_data.get("provider_ticket")

        if provider_ticket:
            self.provider_connector.ensure_connection()
            if any(p.ticket == provider_ticket for p in self.provider_connector.get_positions()):
                return 

        for rec_conn in self.receiver_connectors:
            rec_name = rec_conn.name
            rec_data = trade_data.get("receivers", {}).get(rec_name)
            if rec_data and rec_data.get("ticket"):
                rec_conn.ensure_connection()
                if any(p.ticket == rec_data["ticket"] for p in rec_conn.get_positions(magic=rec_conn.config.get("magic_number"))):
                    return 
        
        logging.info(f"All trades for universal ID {universal_id} are confirmed closed. Removing from state.")
        del self.trade_state[universal_id]


    def _synchronize_trades(self):
        """
        Analyzes provider and receiver positions, determines required actions,
        and executes them, returning a list of GUI messages.
        """
        gui_messages = []
        with self.lock:
            if not self.log_actions_only:
                logging.debug("TradeManager: Starting synchronization cycle.")

            self.provider_connector.ensure_connection()
            provider_positions = self.provider_connector.get_positions()
            try:
                # Extra diagnostic: summarize MT5 core state when zero positions
                if not provider_positions and not self.log_actions_only:
                    term = mt5.terminal_info()
                    acct = mt5.account_info()
                    pos_total = mt5.positions_total()
                    last_err = mt5.last_error()
                    logging.info(f"Provider diagnostic: positions_total()={pos_total} term_path={getattr(term,'path',None)} acct_login={getattr(acct,'login',None)} last_error={last_err}")
            except Exception as diag_e:
                logging.error(f"Provider diagnostic error: {diag_e}")
            if not provider_positions:
                if not self.log_actions_only:
                    logging.info("Provider has 0 open positions detected this cycle.")
                    # Emit a deeper diagnostic snapshot every 5 cycles to reduce log spam
                    cycle_counter = getattr(self, '_empty_provider_cycle_counter', 0) + 1
                    self._empty_provider_cycle_counter = cycle_counter
                    if cycle_counter % 5 == 1:  # 1,6,11,...
                        try:
                            self.provider_connector.debug_positions_report()
                        except Exception as _e:
                            logging.error(f"Failed provider debug report: {_e}")
            else:
                if not self.log_actions_only:
                    logging.debug(f"Detected {len(provider_positions)} provider positions.")
            provider_open_tickets = {pos.ticket for pos in provider_positions}
            provider_positions_dict = {pos.ticket: pos for pos in provider_positions}
            
            actions_by_receiver = {rec.name: {'open': [], 'close': [], 'modify': []} for rec in self.receiver_connectors}
            uids_to_remove_from_state = []

            for uid, trade_entry in list(self.trade_state.items()):
                provider_ticket = trade_entry.get("provider_ticket")

                if provider_ticket and provider_ticket not in provider_open_tickets:
                    logging.info(f"Provider trade {provider_ticket} (UID: {uid}) closed. Scheduling closure on receivers.")
                    for rec_name, rec_data in trade_entry.get("receivers", {}).items():
                        if rec_name in actions_by_receiver and rec_data.get("status") == "copied":
                            actions_by_receiver[rec_name]['close'].append({'ticket': rec_data.get("ticket"), 'uid': uid})
                    if not trade_entry.get("manually_closed"):
                        uids_to_remove_from_state.append(uid)
                    continue

                if provider_ticket and provider_ticket in provider_open_tickets:
                    prov_pos_data = provider_positions_dict[provider_ticket]
                    for rec_name, rec_data in trade_entry.get("receivers", {}).items():
                        if rec_name in actions_by_receiver and rec_data.get("status") == "copied":
                            actions_by_receiver[rec_name]['modify'].append({'rec_ticket': rec_data.get("ticket"), 'prov_sl': prov_pos_data.sl, 'prov_tp': prov_pos_data.tp, 'uid': uid})

            for prov_pos in provider_positions:
                uid = self._get_universal_id_by_provider_ticket(prov_pos.ticket)
                if not uid:
                    uid = generate_universal_trade_id()
                    provider_comment = getattr(prov_pos, 'comment', '')
                    is_duplicate = isinstance(provider_comment, str) and provider_comment.startswith(DUPLICATE_COMMENT_PREFIX)
                    self.trade_state[uid] = {
                        "provider_ticket": prov_pos.ticket,
                        "provider_symbol": prov_pos.symbol,
                        "provider_type": prov_pos.type,
                        "provider_volume": prov_pos.volume,
                        "provider_sl": prov_pos.sl,
                        "provider_tp": prov_pos.tp,
                        "provider_open_time": prov_pos.time,
                        "provider_comment": provider_comment,
                        "is_duplicate": is_duplicate,
                        "duplicate_opened": False,
                        "duplicate_attempt_time": 0,
                        "receivers": {},
                        "manually_closed": False
                    }
                    logging.info(f"New provider trade detected: Ticket {prov_pos.ticket}, assigned UID {uid} (duplicate? {is_duplicate})")
                    gui_messages.append({"type": "status", "account": self.provider_connector.name, "message": f"New trade: {prov_pos.symbol} {prov_pos.volume} lot {'BUY' if prov_pos.type == 0 else 'SELL'} (UID: {uid[:8]}){' [DUPLICATE]' if is_duplicate else ''}"})

                # Attempt provider duplication (only for original trades, not already duplicates)
                if self.duplicate_provider_trades:
                    trade_entry = self.trade_state.get(uid, {})
                    if (not trade_entry.get('is_duplicate', False) and
                        not trade_entry.get('duplicate_opened', False) and
                        not trade_entry.get('duplicate_permanent_failure', False)):
                        last_attempt = trade_entry.get('duplicate_attempt_time', 0)
                        if (time.time() - last_attempt) >= self.duplicate_retry_interval:
                            # Prepare duplicate order
                            deviation_points = self.config.get('provider', {}).get('price_deviation_points', 50)
                            magic_number = self.config.get('provider', {}).get('duplicate_magic_number', self.config.get('provider', {}).get('magic_number', 0))
                            duplicate_comment = f"{DUPLICATE_COMMENT_PREFIX}{prov_pos.ticket}"
                            logging.info(f"Opening duplicate provider trade for ticket {prov_pos.ticket} (UID {uid})")
                            gui_messages.append({"type": "status", "account": self.provider_connector.name, "message": f"Duplicating trade {prov_pos.ticket} (UID:{uid[:8]})"})
                            # Record attempt time BEFORE sending so exceptions still rate-limit retries
                            trade_entry['duplicate_attempt_time'] = time.time()
                            try:
                                open_result = self.provider_connector.open_trade(
                                symbol=prov_pos.symbol,
                                lot_size=prov_pos.volume,
                                order_type=prov_pos.type,
                                sl_price=prov_pos.sl,
                                tp_price=prov_pos.tp,
                                deviation_points=deviation_points,
                                magic_number=magic_number,
                                comment=duplicate_comment
                                )
                            except Exception as dup_exc:
                                logging.exception(f"Duplicate open_trade raised exception for provider ticket {prov_pos.ticket}: {dup_exc}")
                                open_result = None
                            # Handle permanent failure retcodes (e.g., AutoTrading disabled)
                            permanent_failure_codes = {10027, 10028}  # AUTOTRADING disabled by client/server
                            if open_result and open_result.get('retcode') in permanent_failure_codes and not open_result.get('position_ticket'):
                                trade_entry['duplicate_permanent_failure'] = True
                                logging.error(f"Duplicate for provider ticket {prov_pos.ticket} aborted permanently (retcode {open_result.get('retcode')}). Enable Algo Trading in terminal to allow duplication.")
                                gui_messages.append({"type": "error", "account": self.provider_connector.name, "message": f"Duplicate aborted (enable Algo Trading) for {prov_pos.ticket}"})
                            if open_result and open_result.get('position_ticket'):
                                trade_entry['duplicate_opened'] = True
                                logging.info(f"Duplicate provider trade opened: original {prov_pos.ticket} duplicate {open_result['position_ticket']}")
                                gui_messages.append({"type": "status", "account": self.provider_connector.name, "message": f"Duplicate opened as {open_result['position_ticket']} for {prov_pos.ticket}"})
                            else:
                                if not trade_entry.get('duplicate_permanent_failure'):
                                    logging.error(f"Failed to open duplicate for provider trade {prov_pos.ticket}. Will retry after {self.duplicate_retry_interval}s.")
                                    gui_messages.append({"type": "error", "account": self.provider_connector.name, "message": f"Duplicate failed for {prov_pos.ticket}"})

                if self.trade_state[uid].get("manually_closed"): continue

                for rec_conn in self.receiver_connectors:
                    rec_name = rec_conn.name
                    rec_trade_data = self.trade_state[uid].get("receivers", {}).get(rec_name)
                    
                    should_attempt_copy = False
                    if not rec_trade_data: # Never attempted for this receiver
                        should_attempt_copy = True
                    elif rec_trade_data.get("status") == "attempted":
                        last_attempt_time = rec_trade_data.get("last_attempt", 0)
                        if (time.time() - last_attempt_time) > RETRY_INTERVAL_FAILED_COPY:
                            logging.info(f"Retrying failed copy for UID {uid} on {rec_name}. Last attempt: {time.time() - last_attempt_time:.2f}s ago.")
                            should_attempt_copy = True
                        else:
                            if not self.log_actions_only:
                                logging.debug(f"Skipping retry for UID {uid} on {rec_name}. Last attempt too recent.")
                    # If status is "copied", we don't need to do anything here.

                    if should_attempt_copy:
                        actions_by_receiver[rec_name]['open'].append({'provider_pos': prov_pos, 'uid': uid})

            for rec_conn in self.receiver_connectors:
                rec_name, rec_config, actions = rec_conn.name, rec_conn.config, actions_by_receiver[rec_conn.name]
                if not any(actions.values()):
                    continue

                if not self.log_actions_only:
                    logging.debug(f"Executing actions for receiver {rec_name}: {len(actions['open'])} open, {len(actions['close'])} close, {len(actions['modify'])} modify.")
                rec_conn.ensure_connection()
                receiver_positions_dict = {p.ticket: p for p in rec_conn.get_positions(magic=rec_config.get("magic_number"))}

                for close_action in actions['close']:
                    rec_ticket, uid = close_action['ticket'], close_action['uid']
                    if rec_ticket in receiver_positions_dict:
                        pos_to_close = receiver_positions_dict[rec_ticket]
                        logging.info(f"Closing receiver trade {rec_ticket} on {rec_name} for UID {uid}")
                        gui_messages.append({"type": "status", "account": rec_name, "message": f"Closing trade {rec_ticket} (UID {uid[:8]})"})
                        rec_conn.close_trade(rec_ticket, pos_to_close.volume, rec_config.get("price_deviation_points", 50), f"ProvClose UID {uid[:8]}")
                    if uid in self.trade_state and rec_name in self.trade_state[uid]['receivers']:
                        del self.trade_state[uid]['receivers'][rec_name]

                for modify_action in actions['modify']:
                    rec_ticket, uid = modify_action['rec_ticket'], modify_action['uid']
                    rec_pos_data = receiver_positions_dict.get(rec_ticket)
                    if rec_pos_data:
                        prov_sl, prov_tp = modify_action['prov_sl'], modify_action['prov_tp']
                        if (abs(prov_sl - rec_pos_data.sl) > 1e-5 and prov_sl != 0.0) or (abs(prov_tp - rec_pos_data.tp) > 1e-5 and prov_tp != 0.0) or (prov_sl == 0.0 and rec_pos_data.sl != 0.0) or (prov_tp == 0.0 and rec_pos_data.tp != 0.0) or (prov_sl != 0.0 and rec_pos_data.sl == 0.0) or (prov_tp != 0.0 and rec_pos_data.tp == 0.0):
                            logging.info(f"Modifying SL/TP for UID {uid} on {rec_name} (R:{rec_ticket}). New SL:{prov_sl}, TP:{prov_tp}")
                            rec_conn.modify_position_sltp(rec_ticket, prov_sl, prov_tp)
                            gui_messages.append({"type": "status", "account": rec_name, "message": f"Modified SL/TP for {rec_ticket} (UID {uid[:8]})"})

                for open_action in actions['open']:
                    prov_pos, uid = open_action['provider_pos'], open_action['uid']
                    
                    # Re-check if already copied or attempted recently (double-check after action queue processing)
                    current_rec_trade_data = self.trade_state[uid].get("receivers", {}).get(rec_name)
                    if current_rec_trade_data and current_rec_trade_data.get("status") == "copied":
                        if not self.log_actions_only:
                            logging.debug(f"Skipping copy for UID {uid} on {rec_name}: already copied.")
                        continue
                    if current_rec_trade_data and current_rec_trade_data.get("status") == "attempted" and \
                       (time.time() - current_rec_trade_data.get("last_attempt", 0)) < RETRY_INTERVAL_FAILED_COPY:
                        if not self.log_actions_only:
                            logging.debug(f"Skipping copy for UID {uid} on {rec_name}: attempted recently.")
                        continue

                    if prov_pos.time < (time.time() - (rec_config.get("exclude_trades_older_than_minutes", 5) * 60)):
                        if not self.log_actions_only:
                            logging.info(f"Skipping copy for UID {uid} on {rec_name}: trade too old.")
                        continue
                    if (prov_pos.type == mt5.ORDER_TYPE_BUY and not rec_config.get("copy_buy_trades", True)) or \
                       (prov_pos.type == mt5.ORDER_TYPE_SELL and not rec_config.get("copy_sell_trades", True)):
                        if not self.log_actions_only:
                            logging.info(f"Skipping copy for UID {uid} on {rec_name}: trade type not allowed by config.")
                        continue
                    
                    # Find mapped receiver symbol (robust to mis-spelled key)
                    receiver_symbol = prov_pos.symbol
                    for m in rec_config.get("SymbolMapping", []):
                        if m.get("ProviderSymbol") == prov_pos.symbol:
                            if 'ReceiverSymbol' in m:
                                receiver_symbol = m.get('ReceiverSymbol') or receiver_symbol
                            elif 'RecieverSymbol' in m:  # fallback misspelling
                                receiver_symbol = m.get('RecieverSymbol') or receiver_symbol
                            break
                    if receiver_symbol != prov_pos.symbol and not self.log_actions_only:
                        logging.debug(f"Mapping provider symbol {prov_pos.symbol} -> receiver symbol {receiver_symbol} for {rec_name}")
                    rec_symbol_info = rec_conn.get_symbol_info(receiver_symbol)
                    if not rec_symbol_info:
                        logging.error(f"Cannot copy UID {uid} to {rec_name}: Symbol info for {receiver_symbol} not found."); continue
                    
                    final_receiver_volume = normalize_volume(rec_symbol_info, prov_pos.volume * rec_config.get("provider_lot_size_multiplied_by", 1.0))
                    if final_receiver_volume <= 0:
                        logging.warning(f"Skipping copy for UID {uid} to {rec_name}: calculated volume {final_receiver_volume} is zero or less.")
                        continue

                    # Mark as 'attempted' before sending the order
                    self.trade_state[uid]["receivers"][rec_name] = {"ticket": None, "status": "attempted", "last_attempt": time.time()}
                    logging.info(f"Attempting to copy UID {uid} (P:{prov_pos.ticket}) to {rec_name} with volume {final_receiver_volume}")
                    gui_messages.append({"type": "status", "account": rec_name, "message": f"Copying P:{prov_pos.ticket} (UID:{uid[:8]}) Vol:{final_receiver_volume}"})
                    
                    open_result = rec_conn.open_trade(symbol=receiver_symbol, lot_size=final_receiver_volume, order_type=prov_pos.type, sl_price=prov_pos.sl, tp_price=prov_pos.tp, deviation_points=rec_config.get("price_deviation_points", 50), magic_number=rec_config.get("magic_number"), comment=f"{prov_pos.ticket}")

                    position_ticket = None
                    if open_result and open_result.get('position_ticket') and open_result['position_ticket'] > 0:
                        position_ticket = open_result['position_ticket']
                    
                    if position_ticket:
                        self.trade_state[uid]["receivers"][rec_name] = {"ticket": position_ticket, "status": "copied"}
                        logging.info(f"Successfully copied UID {uid} (P:{prov_pos.ticket}) to {rec_name} as R:{position_ticket}")
                        gui_messages.append({"type": "status", "account": rec_name, "message": f"Copied P:{prov_pos.ticket} to R:{position_ticket}"})
                    else:
                        error_details = f"Result: {open_result}" if open_result else "Result was None."
                        broker_comment = open_result.get('comment', 'Unknown reason') if open_result else 'Unknown reason'
                        logging.error(f"Failed to copy UID {uid} (P:{prov_pos.ticket}) to {rec_name}. {error_details}")
                        gui_messages.append({"type": "error", "account": rec_name, "message": f"Failed to copy P:{prov_pos.ticket} (UID:{uid[:8]}). Reason: {broker_comment}"})
                        # Keep status as "attempted" to allow retry after interval

            for uid in uids_to_remove_from_state:
                if uid in self.trade_state and not self.trade_state[uid].get("receivers"):
                    logging.info(f"Cleaning up fully closed UID {uid} from state.")
                    del self.trade_state[uid]

            # Cleanup any other trades that might be fully closed
            for uid in list(self.trade_state.keys()):
                self._cleanup_closed_universal_trade(uid)

            self.save_trade_state()
            logging.debug("Synchronization cycle finished.")
        return gui_messages

    def _send_open_trades_to_gui(self):
        """Prepares a snapshot of all tracked trades and returns it as a single GUI message."""
        gui_trades_data = []
        with self.lock:
            self.provider_connector.ensure_connection()
            all_provider_positions = self.provider_connector.get_positions()
            provider_positions_dict = {p.ticket: p for p in all_provider_positions}

            for uid, data in self.trade_state.items():
                if data.get("manually_closed", False): continue

                provider_ticket = data.get("provider_ticket")
                prov_pos_details = provider_positions_dict.get(provider_ticket)
                
                if not prov_pos_details:
                    if not self.log_actions_only:
                        logging.debug(f"Provider position {provider_ticket} for UID {uid} not found live, using state fallback.")
                    prov_pos_details_from_state = {"ticket": provider_ticket, "symbol": data.get("provider_symbol"), "type": data.get("provider_type"), "volume": data.get("provider_volume"), "price_open": data.get("provider_price_open",0), "sl": data.get("provider_sl"), "tp": data.get("provider_tp"), "profit": "N/A", "time": data.get("provider_open_time")}
                    if not provider_ticket: continue
                    prov_pos_details = type('obj', (object,), prov_pos_details_from_state)()

                if prov_pos_details:
                    trade_group = {"universal_id": uid, "manually_closed": data.get("manually_closed", False), "provider": {"name": self.provider_connector.name, "ticket": prov_pos_details.ticket, "symbol": prov_pos_details.symbol, "type": "BUY" if prov_pos_details.type == 0 else "SELL", "volume": prov_pos_details.volume, "open_price": prov_pos_details.price_open, "sl": prov_pos_details.sl, "tp": prov_pos_details.tp, "profit": getattr(prov_pos_details, 'profit', 'N/A'), "open_time": get_datetime_from_timestamp(getattr(prov_pos_details,'time', 0))}, "receivers_data": []}

                    for rec_conn in self.receiver_connectors:
                        rec_name = rec_conn.name
                        rec_pos_detail = None
                        rec_data_from_state = data.get("receivers", {}).get(rec_name)

                        if rec_data_from_state and rec_data_from_state.get("ticket"):
                            rec_conn.ensure_connection()
                            rec_positions = rec_conn.get_positions(magic=rec_conn.config.get("magic_number"))
                            rec_pos_detail = next((p for p in rec_positions if p.ticket == rec_data_from_state["ticket"]), None)
                            
                            # If not found live, but was marked as copied, try a small delay and re-check
                            if not rec_pos_detail and rec_data_from_state.get("status") == "copied":
                                if not self.log_actions_only:
                                    logging.warning(f"[{rec_name}] Position {rec_data_from_state['ticket']} for UID {uid} not found live in GUI update, but marked 'copied'. Retrying after short delay.")
                                time.sleep(0.2) # Small delay to allow MT5 to update
                                rec_positions = rec_conn.get_positions(magic=rec_conn.config.get("magic_number"))
                                rec_pos_detail = next((p for p in rec_positions if p.ticket == rec_data_from_state["ticket"]), None)
                                if rec_pos_detail and not self.log_actions_only:
                                    logging.info(f"[{rec_name}] Position {rec_data_from_state['ticket']} for UID {uid} found after retry.")

                        if rec_pos_detail:
                            trade_group["receivers_data"].append({"name": rec_name, "ticket": rec_pos_detail.ticket, "symbol": rec_pos_detail.symbol, "type": "BUY" if rec_pos_detail.type == 0 else "SELL", "volume": rec_pos_detail.volume, "open_price": rec_pos_detail.price_open, "sl": rec_pos_detail.sl, "tp": rec_pos_detail.tp, "profit": rec_pos_detail.profit, "open_time": get_datetime_from_timestamp(rec_pos_detail.time)})
                        elif rec_data_from_state and rec_data_from_state.get("status") == "attempted":
                            trade_group["receivers_data"].append({"name": rec_name, "ticket": rec_data_from_state["ticket"], "status": "Attempting Copy", "symbol": "-", "type": "-", "volume": "-", "open_price": "-", "sl": "-", "tp": "-", "profit": "-", "open_time": "-"})
                        else:
                            trade_group["receivers_data"].append({"name": rec_name, "ticket": rec_data_from_state.get("ticket") if rec_data_from_state else "-", "status": "Not Found Live", "symbol": "-", "type": "-", "volume": "-", "open_price": "-", "sl": "-", "tp": "-", "profit": "-", "open_time": "-"})
                    gui_trades_data.append(trade_group)
        
        return {"type": "update_trades", "data": gui_trades_data}
