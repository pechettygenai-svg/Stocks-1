# Embedding in a PHP site (XAMPP/WAMP)

Drop-in files for a PHP site that uses the `$_SESSION['sessData']['userLoggedIn']`
login pattern (e.g. StockSym). The Python service stays on `127.0.0.1:8000`; PHP
pages reach it only through `sf_proxy.php`, which rejects users who aren't logged in.

| File | Purpose |
| --- | --- |
| `sf_proxy.php` | Login-checked proxy for `/`, `/static/*`, `/v1/*`, `/healthz`. Set `SF_SERVICE_URL` to change the service address. |
| `stock_research.php` | Site page: includes `Welcome.php` (menu) and shows the app in an iframe. `?ticker=XYZ` starts an analysis. |
| `start_stock_service.bat` | Starts the service on Windows. Edit `STOCKS_REPO` and `SEC_USER_AGENT` first. |

Setup:
1. Copy the two PHP files into the site's `public_html`.
2. Add a menu link to `stock_research.php`, and optionally link grid symbols to
   `stock_research.php?ticker=<symbol>`.
3. Run `start_stock_service.bat` and keep its window open while using the site.

PHP needs `allow_url_fopen=On` (the XAMPP default). Watchlists and run history are
shared by all site users, because the service has no per-user accounts.
