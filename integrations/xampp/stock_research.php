<?php
// Stock Forecasting Agent inside the site: same login check and menu as the other
// pages; the app runs in an iframe (so its styles don't clash with Kendo) through
// sf_proxy.php. Link here with ?ticker=XYZ to start an analysis directly.
if (session_status() == PHP_SESSION_NONE) {
    session_start();
}
$sessData = !empty($_SESSION['sessData']) ? $_SESSION['sessData'] : '';
if (empty($sessData['userLoggedIn']) || empty($sessData['userID'])) {
    header('Location:index.php');
    exit;
}
include 'Welcome.php';
$ticker = isset($_GET['ticker']) ? strtoupper(preg_replace('/[^A-Za-z0-9.\-^=/ ]/', '', $_GET['ticker'])) : '';
$src = 'sf_proxy.php/' . ($ticker !== '' ? '?ticker=' . rawurlencode($ticker) : '');
?>
<div style="position:absolute; top:120px; left:0; right:0; bottom:0;">
  <iframe src="<?php echo htmlspecialchars($src); ?>" title="Stock research"
          style="width:100%; height:100%; border:0; display:block;"></iframe>
</div>
