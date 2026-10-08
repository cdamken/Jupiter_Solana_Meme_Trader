<?php
/* index.php — PHP proxy for the Jupiter panel.
 * Place at cloud.damken.com/carlos/jupiter/ (ownCloud Alias /carlos -> WEB-PAGE).
 * Forwards all requests to the Flask backend at 127.0.0.1:8791 (keepalive.sh).
 * Protected by .htaccess Basic Auth (inherit from /carlos/hub/ or add its own).
 * Shared secret (X-Panel-Token) read from .panel_token next to this file. */
$backend = "http://127.0.0.1:8791";

// Strip the /carlos/jupiter prefix from the path
$path = parse_url($_SERVER["REQUEST_URI"], PHP_URL_PATH);
$sub  = preg_replace('#^/carlos/jupiter#', '', $path);
if ($sub === "" || $sub === false) $sub = "/";
if ($sub[0] !== "/") $sub = "/" . $sub;
$qs  = $_SERVER["QUERY_STRING"] ?? "";
$url = $backend . $sub . ($qs !== "" ? "?" . $qs : "");

// CSRF: reject only if Origin/Referer is a known different host
if ($_SERVER["REQUEST_METHOD"] === "POST") {
    $host    = parse_url("http://" . ($_SERVER["HTTP_HOST"] ?? ""), PHP_URL_HOST);
    $src     = $_SERVER["HTTP_ORIGIN"] ?? ($_SERVER["HTTP_REFERER"] ?? "");
    $srchost = ($src !== "" && $src !== "null") ? parse_url($src, PHP_URL_HOST) : null;
    if ($srchost !== null && $host !== null && strcasecmp($srchost, $host) !== 0) {
        http_response_code(403);
        echo "<h2>403</h2><p>Cross-origin request rejected.</p>";
        exit;
    }
}

$panel_token = "";
$tokfile = __DIR__ . "/.panel_token";
if (is_readable($tokfile)) $panel_token = trim(file_get_contents($tokfile));

$ch = curl_init($url);
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_CONNECTTIMEOUT, 3);
curl_setopt($ch, CURLOPT_TIMEOUT, 15);
if ($_SERVER["REQUEST_METHOD"] === "POST") {
    $headers = ["Content-Type: application/x-www-form-urlencoded"];
    if ($panel_token !== "") $headers[] = "X-Panel-Token: " . $panel_token;
    curl_setopt($ch, CURLOPT_POST, true);
    curl_setopt($ch, CURLOPT_POSTFIELDS, file_get_contents("php://input"));
    curl_setopt($ch, CURLOPT_HTTPHEADER, $headers);
}
$resp = curl_exec($ch);
$ct   = curl_getinfo($ch, CURLINFO_CONTENT_TYPE);
$code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
curl_close($ch);

if ($resp === false || $code === 0) {
    http_response_code(502);
    echo "<h2>Jupiter panel unavailable</h2>"
       . "<p>Backend (127.0.0.1:8791) not responding. "
       . "Check keepalive.sh is in carlos crontab (*/1 * * * *).</p>";
    exit;
}
if ($ct) header("Content-Type: " . $ct);
http_response_code($code ?: 200);
echo $resp;
