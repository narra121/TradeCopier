import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import logging
from queue import Queue, Full
import threading
import time # For timestamp in log
from .theme import *

# Assuming utils.get_datetime_from_timestamp is available
# from core.utils import get_datetime_from_timestamp
# If not directly importable, define a simple version here or pass it around
def get_datetime_from_timestamp_gui(ts):
    if ts is None:
        return "N/A"
    try:
        # Attempt to convert to float, assuming it's a valid timestamp
        timestamp = float(ts)
        if timestamp > 0:
            return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(timestamp))
        else:
            return "0" # Or "N/A" or "" if you prefer for 0 timestamp
    except (ValueError, TypeError):
        # If conversion fails, it's not a valid number or timestamp
        return str(ts) # Return the original value as a string

class TradeGroupRow:
    def __init__(self, parent, trade_group_data, app_instance):
        self.parent = parent
        self.app = app_instance
        self.uid = trade_group_data['universal_id']
        self.widgets = {}
        self.receiver_rows = {}
        self._create_widgets(trade_group_data)

    def _create_widgets(self, trade_group_data):
        self.frame = ttk.Frame(self.parent, relief=tk.RIDGE, borderwidth=1, style="Trade.TFrame", padding=5)
        self.frame.columnconfigure(1, weight=1)
        self.frame.columnconfigure(2, weight=1)

        self.widgets['uid_label'] = ttk.Label(self.frame, text="", wraplength=100, style="Trade.TLabel")
        self.widgets['uid_label'].grid(row=0, column=0, padx=5, pady=2, sticky="nw")

        provider_frame, provider_widgets = self._create_trade_details_widgets(self.frame, trade_group_data['provider'], "Provider")
        provider_frame.grid(row=0, column=1, padx=5, pady=2, sticky="ew")
        self.widgets['provider_widgets'] = provider_widgets

        receivers_outer_frame = ttk.Frame(self.frame, style="Trade.TFrame")
        receivers_outer_frame.grid(row=0, column=2, padx=5, pady=2, sticky="nsew")
        self.widgets['receivers_frame'] = receivers_outer_frame

        actions_frame = ttk.Frame(self.frame, style="Trade.TFrame")
        actions_frame.grid(row=0, column=3, padx=5, pady=2, sticky="ne")
        self.widgets['close_button'] = ttk.Button(actions_frame, text="Close Group", command=lambda: self.app._on_close_universal_trade(self.uid), style="Close.TButton")
        self.widgets['close_button'].pack(pady=2)

        self.update(trade_group_data)

    def update(self, trade_group_data):
        # Update UID/Status
        uid_status_text = self.app._format_uid_status_text(self.uid, trade_group_data)
        self.widgets['uid_label'].config(text=uid_status_text)

        # Update Provider
        self._update_trade_details_widgets(self.widgets['provider_widgets'], trade_group_data['provider'])

        # Update Close Button State
        manually_closed_flag = trade_group_data.get("manually_closed", False)
        new_button_state = tk.DISABLED if manually_closed_flag else tk.NORMAL
        if self.widgets['close_button'].cget('state') != new_button_state:
            self.widgets['close_button'].config(state=new_button_state)

        # Update Receivers
        receivers_data = trade_group_data['receivers_data']
        current_rec_names = set(self.receiver_rows.keys())
        new_rec_names = {rec['name'] for rec in receivers_data}

        for name in current_rec_names - new_rec_names:
            self.receiver_rows[name]['frame'].destroy()
            del self.receiver_rows[name]

        for i, rec_data in enumerate(sorted(receivers_data, key=lambda x: x['name'])):
            rec_name = rec_data['name']
            if rec_name not in self.receiver_rows:
                rec_frame, rec_widgets = self._create_trade_details_widgets(self.widgets['receivers_frame'], rec_data, "Receiver")
                rec_frame.grid(row=i, column=0, sticky="ew", padx=2, pady=2)
                self.receiver_rows[rec_name] = {'frame': rec_frame, 'widgets': rec_widgets}
            else:
                self._update_trade_details_widgets(self.receiver_rows[rec_name]['widgets'], rec_data)

    def _create_trade_details_widgets(self, parent, trade_data, style_prefix):
        frame = ttk.Frame(parent, borderwidth=1, relief="solid", style=f"{style_prefix}.TFrame", padding=5)
        
        if trade_data.get("status"):
            label = ttk.Label(frame, text=f"{trade_data['name']}: {trade_data['status']}", style=f"{style_prefix}.TLabel")
            label.pack(anchor="w")
            return frame, {'status_label': label}

        line1 = ttk.Label(frame, text=f"{trade_data['name']} (Ticket: {trade_data['ticket']})", style=f"{style_prefix}.TLabel")
        line1.pack(anchor="w")
        line2 = ttk.Label(frame, text=f"Sym: {trade_data['symbol']} {trade_data['type']} Vol: {trade_data['volume']}", style=f"{style_prefix}.TLabel")
        line2.pack(anchor="w")
        line3 = ttk.Label(frame, text=f"Open: {trade_data['open_price']} SL: {trade_data.get('sl',0.0)} TP: {trade_data.get('tp',0.0)}", style=f"{style_prefix}.TLabel")
        line3.pack(anchor="w")
        
        profit_frame = ttk.Frame(frame, style=f"{style_prefix}.TFrame")
        profit_frame.pack(fill="x")
        ttk.Label(profit_frame, text="Profit: ", style=f"{style_prefix}.TLabel").pack(side="left")
        profit_val = trade_data.get('profit', 0.0)
        profit_color = "green" if profit_val >= 0 else "red"
        profit_label = ttk.Label(profit_frame, text=f"{profit_val}", foreground=profit_color, style=f"{style_prefix}.TLabel")
        profit_label.pack(side="left")

        time_frame = ttk.Frame(frame, style=f"{style_prefix}.TFrame")
        time_frame.pack(fill="x", anchor="w")
        ttk.Label(time_frame, text="Time: ", style=f"{style_prefix}.TLabel").pack(side="left")
        time_label = ttk.Label(time_frame, text=f"{get_datetime_from_timestamp_gui(trade_data.get('open_time'))}", style=f"{style_prefix}.TLabel")
        time_label.pack(side="left")

        widgets = {'line1': line1, 'line2': line2, 'line3': line3, 'profit': profit_label, 'time': time_label}
        return frame, widgets

    def _update_trade_details_widgets(self, widgets, new_data):
        if new_data.get("status"):
            if 'status_label' in widgets and widgets['status_label'].cget('text') != f"{new_data['name']}: {new_data['status']}":
                widgets['status_label'].config(text=f"{new_data['name']}: {new_data['status']}")
            return

        if 'status_label' in widgets: # Was a status, now a full trade, needs recreation
            # This case is complex, for now assume it doesn't happen or requires full widget destruction/recreation
            return

        widgets['line1'].config(text=f"{new_data['name']} (Ticket: {new_data['ticket']})")
        widgets['line2'].config(text=f"Sym: {new_data['symbol']} {new_data['type']} Vol: {new_data['volume']}")
        widgets['line3'].config(text=f"Open: {new_data['open_price']} SL: {new_data.get('sl',0.0)} TP: {new_data.get('tp',0.0)}")
        profit_val = new_data.get('profit', 0.0)
        profit_color = "green" if profit_val >= 0 else "red"
        widgets['profit'].config(text=f"{profit_val}", foreground=profit_color)
        widgets['time'].config(text=f"{get_datetime_from_timestamp_gui(new_data.get('open_time'))}")

class TradeCopierApp:
    def __init__(self, root, action_queue_to_manager):
        self.root = root
        self.root.title("MT5 Trade Copier")
        self.root.geometry("1200x800")

        self.action_queue_to_manager = action_queue_to_manager
        self.gui_update_queue = Queue()

        self.trade_widgets = {}

        self._setup_ui()
        self.start_gui_update_listener()

    def _setup_ui(self):
        # Configure styles
        self.style = ttk.Style()
        self.style.configure("Header.TFrame", background=STYLE_HEADER_BG)
        self.style.configure("Header.TLabel", background=STYLE_HEADER_BG, foreground=STYLE_HEADER_FG, font=("Arial", 10, "bold"))
        self.style.configure("Provider.TFrame", background=STYLE_PROVIDER_BG)
        self.style.configure("Receiver.TFrame", background=STYLE_RECEIVER_BG)
        self.style.configure("Trade.TLabel", background=STYLE_BG, font=("Arial", 9))
        self.style.configure("Provider.TLabel", background=STYLE_PROVIDER_BG, font=("Arial", 9))
        self.style.configure("Receiver.TLabel", background=STYLE_RECEIVER_BG, font=("Arial", 9))
        
        # Configure button styles
        self.style.configure("Action.TButton", background=STYLE_BUTTON_BG, foreground=STYLE_BUTTON_FG, font=("Arial", 9, "bold"))
        self.style.map("Action.TButton",
                      background=[('active', STYLE_BUTTON_HOVER_BG)],
                      foreground=[('active', STYLE_BUTTON_FG)])
        self.style.configure("Close.TButton", background=STYLE_CLOSE_BUTTON_BG, foreground=STYLE_BUTTON_FG, font=("Arial", 9, "bold"))
        self.style.map("Close.TButton",
                      background=[('active', STYLE_CLOSE_BUTTON_HOVER_BG)],
                      foreground=[('active', STYLE_BUTTON_FG)])

        self.root.configure(bg=STYLE_BG)
        
        # Main paned window
        main_paned_window = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        main_paned_window.pack(fill=tk.BOTH, expand=True)

        # Trades frame container
        trades_frame_container = ttk.Frame(main_paned_window, relief=tk.GROOVE, borderwidth=2)
        main_paned_window.add(trades_frame_container, weight=3)

        # Canvas for scrollable trades
        self.trades_canvas = tk.Canvas(trades_frame_container, bg=STYLE_BG, highlightbackground=STYLE_BG)
        self.trades_scrollbar_y = ttk.Scrollbar(trades_frame_container, orient=tk.VERTICAL, command=self.trades_canvas.yview)
        self.trades_scrollbar_x = ttk.Scrollbar(trades_frame_container, orient=tk.HORIZONTAL, command=self.trades_canvas.xview)

        self.scrollable_trades_frame = ttk.Frame(self.trades_canvas)

        self.scrollable_trades_frame.bind(
            "<Configure>",
            lambda e: self.trades_canvas.configure(scrollregion=self.trades_canvas.bbox("all"))
        )
        
        self.canvas_window = self.trades_canvas.create_window((0, 0), window=self.scrollable_trades_frame, anchor="nw")
        self.root.bind("<Configure>", self._on_resize)

        # Connect scrollbars to canvas
        self.trades_canvas.configure(yscrollcommand=self.trades_scrollbar_y.set, xscrollcommand=self.trades_scrollbar_x.set)
        self.trades_canvas.pack(side="left", fill="both", expand=True)
        self.trades_scrollbar_y.pack(side="right", fill="y")
        self.trades_scrollbar_x.pack(side="bottom", fill="x")

        # Header (placed once)
        self.header_frame = ttk.Frame(self.scrollable_trades_frame, style="Header.TFrame")
        self.header_frame.grid(row=0, column=0, sticky="ew", pady=5, padx=5)
        self.header_frame.columnconfigure(1, weight=1)
        ttk.Label(self.header_frame, text="UID / Status", style="Header.TLabel").grid(row=0, column=0, padx=5, sticky="w")
        ttk.Label(self.header_frame, text="Provider Trade", style="Header.TLabel").grid(row=0, column=1, padx=5, sticky="w")
        ttk.Label(self.header_frame, text="Receiver Trades", style="Header.TLabel").grid(row=0, column=2, padx=5, sticky="w")
        ttk.Label(self.header_frame, text="Actions", style="Header.TLabel").grid(row=0, column=3, padx=5, sticky="w")
        self.next_trade_row = 1 # Keep track of where to insert next trade frame

        # Bottom frame container for logs
        bottom_frame_container = ttk.Frame(main_paned_window, relief=tk.GROOVE, borderwidth=2)
        main_paned_window.add(bottom_frame_container, weight=1)

        # Controls frame
        controls_frame = ttk.Frame(bottom_frame_container)
        controls_frame.pack(pady=10, padx=10, fill=tk.X)
        self.close_all_button = ttk.Button(controls_frame, text="Close All Active Trades", command=self._on_close_all_trades, style="Action.TButton")
        self.close_all_button.pack(side=tk.LEFT, padx=5)

        # Summary frame
        summary_frame = ttk.Frame(controls_frame)
        summary_frame.pack(side=tk.LEFT, padx=20)

        # Provider PNL
        provider_pnl_frame = ttk.Frame(summary_frame)
        provider_pnl_frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(provider_pnl_frame, text="Provider PNL: ", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
        self.provider_pnl_value_label = ttk.Label(provider_pnl_frame, text="0.00", font=("Arial", 9, "bold"))
        self.provider_pnl_value_label.pack(side=tk.LEFT)

        # Receiver PNL
        receiver_pnl_frame = ttk.Frame(summary_frame)
        receiver_pnl_frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(receiver_pnl_frame, text="Receiver PNL: ", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
        self.receiver_pnl_value_label = ttk.Label(receiver_pnl_frame, text="0.00", font=("Arial", 9, "bold"))
        self.receiver_pnl_value_label.pack(side=tk.LEFT)

        # Provider Trades
        provider_trades_frame = ttk.Frame(summary_frame)
        provider_trades_frame.pack(side=tk.LEFT, padx=5)
        ttk.Label(provider_trades_frame, text="Provider Trades: ", font=("Arial", 9, "bold")).pack(side=tk.LEFT)
        self.provider_trades_value_label = ttk.Label(provider_trades_frame, text="0", font=("Arial", 9, "bold"))
        self.provider_trades_value_label.pack(side=tk.LEFT)

        # Log section
        log_label = ttk.Label(bottom_frame_container, text="Application Log / Status Messages:", font=("Arial", 10, "bold"))
        log_label.pack(pady=(5,0), padx=10, anchor="w")
        self.log_text = scrolledtext.ScrolledText(bottom_frame_container, height=10, wrap=tk.WORD, state=tk.DISABLED, bg="#ffffff", font=("Consolas", 9))
        self.log_text.pack(pady=5, padx=10, fill=tk.BOTH, expand=True)

        # Bind mousewheel for scrolling
        self.root.bind_all("<MouseWheel>", self._on_mousewheel)

    def _on_resize(self, event):
        # This check is to prevent the event from firing for child widget resizes
        if event.widget == self.root:
            canvas_width = self.trades_canvas.winfo_width()
            if canvas_width > 1: # Ensure we have a valid width
                self.trades_canvas.itemconfig(self.canvas_window, width=canvas_width)

    def _on_mousewheel(self, event):
        """
        Handles mouse wheel scrolling.
        Scrolls the trades_canvas, but tries not to interfere with other
        scrollable widgets like the log text box.
        """
        widget = self.root.winfo_containing(event.x_root, event.y_root)
        if widget is None:
            return

        # Check if the widget is the log_text or a child of it
        w = widget
        while w is not None and w != self.root:
            if w == self.log_text:
                # Let the ScrolledText handle its own scrolling
                return
            w = w.master

        # If not related to the log, scroll the main canvas
        self.trades_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_close_all_trades(self):
        if messagebox.askyesno("Confirm Close All", "Are you sure you want to attempt to close ALL active trades?"):
            try:
                self.log_message("System", "User initiated 'Close All Trades'.")
                self.action_queue_to_manager.put_nowait({"type": "close_all_trades"})
            except Full:
                self.log_message("System", "Action queue is full. Could not send 'Close All' command.")
                logging.warning("GUI: Action queue to manager is full. 'Close All' action dropped.")

    def _on_close_universal_trade(self, universal_id):
        if messagebox.askyesno("Confirm Close Trade Group", f"Close this trade group (UID: {universal_id[:8]})?"):
            try:
                self.log_message("System", f"User initiated close for UID: {universal_id[:8]}.")
                self.action_queue_to_manager.put_nowait({"type": "close_universal_trade", "universal_id": universal_id})
            except Full:
                self.log_message("System", f"Action queue is full. Could not send 'Close UID {universal_id[:8]}' command.")
                logging.warning(f"GUI: Action queue to manager is full. 'Close UID {universal_id[:8]}' action dropped.")

    def log_message(self, account_name, message):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.log_message, account_name, message)
            return
        self.log_text.config(state=tk.NORMAL)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        self.log_text.insert(tk.END, f"{timestamp} [{account_name}]: {message}\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)
        logging.info(f"GUI_LOG [{account_name}]: {message}")


    def _format_uid_status_text(self, uid, trade_group):
        uid_status_text = f"UID: {uid[:8]}"
        if trade_group.get("manually_closed"):
            uid_status_text += "\n(Manually Closed)"
        return uid_status_text

    def update_trades_display(self, trades_data_from_manager):
        if threading.current_thread() != threading.main_thread():
            self.root.after(0, self.update_trades_display, trades_data_from_manager)
            return

        current_uids_on_gui = set(self.trade_widgets.keys())
        uids_from_data = {trade['universal_id'] for trade in trades_data_from_manager}

        for uid_to_remove in current_uids_on_gui - uids_from_data:
            if uid_to_remove in self.trade_widgets:
                self.trade_widgets[uid_to_remove].frame.destroy()
                del self.trade_widgets[uid_to_remove]
                logging.debug(f"GUI: Removed widgets for UID {uid_to_remove}")

        for i, trade_group in enumerate(trades_data_from_manager):
            uid = trade_group['universal_id']
            if uid not in self.trade_widgets:
                trade_row = TradeGroupRow(self.scrollable_trades_frame, trade_group, self)
                self.trade_widgets[uid] = trade_row
            else:
                self.trade_widgets[uid].update(trade_group)

            self.trade_widgets[uid].frame.grid(row=i + 1, column=0, columnspan=4, sticky="ew", pady=3, padx=5)

        # --- Summary Calculation ---
        total_provider_pnl = 0.0
        total_receiver_pnl = 0.0
        total_provider_trades = len(uids_from_data)

        for trade_group in trades_data_from_manager:
            total_provider_pnl += trade_group['provider'].get('profit', 0.0)
            for rec_data in trade_group['receivers_data']:
                total_receiver_pnl += rec_data.get('profit', 0.0)

        # Update summary labels
        provider_pnl_color = "green" if total_provider_pnl >= 0 else "red"
        receiver_pnl_color = "green" if total_receiver_pnl >= 0 else "red"

        self.provider_pnl_value_label.config(text=f"{total_provider_pnl:.2f}", foreground=provider_pnl_color)
        self.receiver_pnl_value_label.config(text=f"{total_receiver_pnl:.2f}", foreground=receiver_pnl_color)
        self.provider_trades_value_label.config(text=f"{total_provider_trades}")


        self.scrollable_trades_frame.update_idletasks()
        self.trades_canvas.config(scrollregion=self.trades_canvas.bbox("all"))


    

    def process_gui_updates(self):
        """Processes messages from the TradeManager via the queue."""
        try:
            while not self.gui_update_queue.empty():
                message = self.gui_update_queue.get_nowait()
                msg_type = message.get("type")

                if msg_type == "status" or msg_type == "error":
                    self.log_message(message.get("account", "System"), message.get("message"))
                elif msg_type == "update_trades":
                    self.update_trades_display(message.get("data", []))

                self.gui_update_queue.task_done()
        except Exception as e:
            logging.error(f"Error processing GUI update: {e}")
        finally:
            # Reschedule this method to run again
            self.root.after(100, self.process_gui_updates) # Poll queue every 100ms

    def start_gui_update_listener(self):
        """Starts the periodic check for messages from TradeManager."""
        self.root.after(100, self.process_gui_updates)

    def on_closing(self, trade_manager_stop_func):
        """Handles the window close event."""
        if messagebox.askokcancel("Quit", "Do you want to quit the Trade Copier?"):
            self.log_message("System", "Shutdown initiated by user...")
            if trade_manager_stop_func:
                # Run stop in a new thread to avoid blocking GUI if it takes time
                stop_thread = threading.Thread(target=trade_manager_stop_func, name="ManagerStopThread")
                stop_thread.start()
                # Give it a moment, then destroy. Or wait for thread if critical.
                # For now, just destroy. TradeManager should save state in its stop.
            self.root.destroy()
        else:
            self.log_message("System", "Shutdown cancelled by user.")