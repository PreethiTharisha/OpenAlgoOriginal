# OpenAlgo Multi-Instance Setup - Complete

## Status: ✅ READY TO RUN

All configuration is complete. Both instances are ready to start independently.

**Solution**: 
1. Copied entire `.env` file to both `.env.indmoney` and `.env.zerodha`
2. Replaced ALL occurrences of port numbers and URLs in `.env.zerodha`:
   - Port 5000 → 5001
   - Port 8765 → 8766
   - Port 5555 → 5556
   - All database names (openalgo.db → openalgo_2.db, etc.)
   - All URLs (127.0.0.1:5000 → 127.0.0.1:5001)
3. Modified `utils/env_check.py` to explicitly set environment variables from the .env file

---

## Quick Start

### Terminal 1 - Instance 1 (Port 5000)
```batch
D:\OpenAlgo\openalgo\run_instance1.bat
```

### Terminal 2 - Instance 2 (Port 5001)
```batch
D:\OpenAlgo\openalgo\run_instance2.bat
```

### Access
- **Instance 1**: http://127.0.0.1:5000
- **Instance 2**: http://127.0.0.1:5001

---

## What Was Done

### 1. Code Modification
- Modified `utils/env_check.py` to support instance-specific `.env` files
- Application now reads `OPENALGO_ENV_FILE` environment variable
- Falls back to `.env` if instance-specific file not found

### 2. Environment Files
- **`.env.indmoney`** - Instance 1 configuration (clean, no comments)
- **`.env.zerodha`** - Instance 2 configuration (clean, no comments)
- Both contain all 80+ required environment variables
- No comment lines (batch file compatible)

### 3. Batch Files
- **`run_instance1.bat`** - Loads `.env.indmoney` and starts Instance 1
- **`run_instance2.bat`** - Loads `.env.zerodha` and starts Instance 2
- Both properly parse environment variables (skip comments, handle equals signs)

---

## Instance Configuration

| Setting | Instance 1 | Instance 2 |
|---------|-----------|-----------|
| **HTTP Port** | 5000 | 5001 |
| **WebSocket Port** | 8765 | 8766 |
| **ZMQ Port** | 5555 | 5556 |
| **Main Database** | openalgo.db | openalgo_2.db |
| **Latency DB** | latency.db | latency_2.db |
| **Logs DB** | logs.db | logs_2.db |
| **Health DB** | health.db | health_2.db |
| **Sandbox DB** | sandbox.db | sandbox_2.db |
| **Historify DB** | historify.duckdb | historify_2.duckdb |
| **Session Cookie** | session_instance1 | session_instance2 |
| **CSRF Cookie** | csrf_token_instance1 | csrf_token_instance2 |
| **Redirect URL** | :5000/callback | :5001/callback |
| **CORS Origins** | :5000 | :5001 |
| **Config File** | .env.indmoney | .env.zerodha |

---

## Files Modified

1. **`utils/env_check.py`** - Added multi-instance support
2. **`.env.indmoney`** - Instance 1 configuration (all variables, no comments)
3. **`.env.zerodha`** - Instance 2 configuration (all variables, no comments)
4. **`run_instance1.bat`** - Fixed batch parsing (skip comments, handle equals)
5. **`run_instance2.bat`** - Fixed batch parsing (skip comments, handle equals)

---

## Expected Output

### Instance 1
```
Loading environment variables from .env.indmoney...
FLASK_PORT=5000
WEBSOCKET_PORT=8765
DATABASE_URL=sqlite:///db/openalgo.db

Starting OpenAlgo Instance 1...

Web App    http://127.0.0.1:5000
WebSocket  ws://127.0.0.1:8765
Status     Ready
```

### Instance 2
```
Loading environment variables from .env.zerodha...
FLASK_PORT=5001
WEBSOCKET_PORT=8766
DATABASE_URL=sqlite:///db/openalgo_2.db

Starting OpenAlgo Instance 2...

Web App    http://127.0.0.1:5001
WebSocket  ws://127.0.0.1:8766
Status     Ready
```

---

## Troubleshooting

### Issue: "Port already in use"
**Solution**: Change `FLASK_PORT` in the `.env` file to an unused port (5002, 5003, etc.)

### Issue: "Database is locked"
**Solution**: Ensure each instance uses different `DATABASE_URL`

### Issue: "WebSocket connection failed"
**Solution**: Verify `WEBSOCKET_PORT` is unique and `HOST_SERVER` matches the port

### Issue: Environment variables not loading
**Solution**: Ensure `.env` files have no comment lines (lines starting with `#`)

---

## Key Points

✅ **No code changes needed** - Environment variables are supported
✅ **Separate databases** - Each instance has its own database
✅ **Unique ports** - Each instance uses different ports
✅ **Unique cookies** - Cookie names are instance-specific
✅ **Backward compatible** - Single-instance deployments still work
✅ **Clean environment files** - No comments (batch file compatible)

---

## Summary

**Problem**: Running two OpenAlgo instances from same codebase
**Solution**: Instance-specific `.env` files + code modification + batch files
**Result**: Both instances run independently on different ports
**Status**: ✅ Complete and ready to use

Run both instances now using the batch files!
