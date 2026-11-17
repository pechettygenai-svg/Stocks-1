<?php
// Forwards the Stock Forecasting Agent UI and /v1 API to the local Python service,
// for logged-in users only. The service itself listens on 127.0.0.1 and is never
// exposed directly. Open it as sf_proxy.php/ (stock_research.php does this).
if (session_status() == PHP_SESSION_NONE) {
    session_start();
}
$sessData = !empty($_SESSION['sessData']) ? $_SESSION['sessData'] : '';
if (empty($sessData['userLoggedIn']) || empty($sessData['userID'])) {
    http_response_code(401);
    header('Content-Type: application/json');
    echo json_encode(['detail' => 'Log in to use stock research.']);
    exit;
}
session_write_close(); // long analyses must not lock the PHP session for other pages

$service = rtrim(getenv('SF_SERVICE_URL') ?: 'http://127.0.0.1:8000', '/');
$path = isset($_SERVER['PATH_INFO']) ? $_SERVER['PATH_INFO'] : '';
$query = isset($_SERVER['QUERY_STRING']) && $_SERVER['QUERY_STRING'] !== '' ? '?' . $_SERVER['QUERY_STRING'] : '';

if ($path === '') {
    // The UI uses relative URLs, so it must be loaded with a trailing slash.
    header('Location: ' . basename($_SERVER['SCRIPT_NAME']) . '/' . $query);
    exit;
}
if (strpos($path, '..') !== false
    || !preg_match('#^/(v1(/[A-Za-z0-9_.\-/]*)?|static/[A-Za-z0-9_.\-]+|healthz)?$#', $path)) {
    http_response_code(404);
    exit;
}

$headers = ['Accept: ' . (isset($_SERVER['HTTP_ACCEPT']) ? $_SERVER['HTTP_ACCEPT'] : '*/*')];
if (!empty($_SERVER['CONTENT_TYPE'])) {
    $headers[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE'];
}
$ctx = stream_context_create(['http' => [
    'method' => $_SERVER['REQUEST_METHOD'],
    'header' => implode("\r\n", $headers),
    'content' => file_get_contents('php://input'),
    'ignore_errors' => true, // pass 4xx/5xx bodies through instead of failing
    'timeout' => 120,
]]);
$body = @file_get_contents($service . $path . $query, false, $ctx);

if ($body === false) {
    http_response_code(502);
    header('Content-Type: ' . ($path === '/' ? 'text/html' : 'application/json'));
    $msg = "The stock research service is not running at $service. "
        . 'Start it with start_stock_service.bat, then reload this page.';
    echo $path === '/' ? '<p style="font-family:sans-serif;padding:20px">' . htmlspecialchars($msg) . '</p>'
        : json_encode(['detail' => $msg]);
    exit;
}
$status = 502;
foreach ($http_response_header as $h) {
    if (preg_match('#^HTTP/\S+\s+(\d{3})#', $h, $m)) {
        $status = (int) $m[1];
    } elseif (preg_match('#^(Content-Type|Content-Disposition|Cache-Control):#i', $h)) {
        header($h);
    }
}
http_response_code($status);
echo $body;
