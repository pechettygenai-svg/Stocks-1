@echo off
REM Starts the Stock Forecasting Agent for the PHP site on http://127.0.0.1:8000
REM (local only; the site reaches it through sf_proxy.php). Keep this window open.
REM Edit these two lines once:
set "STOCKS_REPO=C:\Users\spech\Stocks-1"
set "SEC_USER_AGENT=stock-forecaster your-email@example.com"
REM Optional keyed forecast feeds (free keys):
REM set "FINNHUB_API_KEY=..."
REM set "FMP_API_KEY=..."
REM set "ALPHAVANTAGE_API_KEY=..."

cd /d "%STOCKS_REPO%" || (echo Stocks-1 not found at %STOCKS_REPO% & pause & exit /b 1)
call .venv\Scripts\activate.bat || (echo Run setup first: python -m venv .venv and pip install -e . & pause & exit /b 1)
stock-forecaster serve --host 127.0.0.1 --port 8000
pause
