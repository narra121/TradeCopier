# Trade Copier

Automated trade copier for MetaTrader 5 provider -> multiple receivers with optional duplicate trade opening on provider.

## Features
- Detects new provider trades and immediately opens a duplicate provider trade (configurable)
- Copies trades to one or more receiver accounts with per-receiver filters
- Retries failed copy attempts with back-off
- SL/TP synchronization updates receivers when provider changes
- Manual close (single universal trade or all trades)
- Persistent trade state across restarts
- Rolling log files (size + count limited)
- Actions-only logging mode to reduce log volume
- Extensive diagnostics (startup environment snapshot & periodic position reports)

## Configuration
Edit configs under `config/`:
- `config.json` (default)
- `config_dev.json` / `config_prod.json`

Key settings:
```json
"duplicate_provider_trades": true,
"duplicate_retry_interval_seconds": 30,
"log_actions_only": true,
"log_max_size_mb": 10,
"log_max_files": 20
```

## Run
```bash
python main.py --config config/config_dev.json
```
(Adjust path for Windows PowerShell.)

## Build (PyInstaller)
Two spec files are present. Example:
```bash
pyinstaller TradeCopierApp.spec
```

## Requirements
See `requirements.txt`.

## Notes
- Ensure MT5 terminals exist at the configured `terminal_path` locations.
- Enable Algo Trading in each terminal or duplicate opens may fail (retcode 10027/10028).

## License
Proprietary / Internal Use
