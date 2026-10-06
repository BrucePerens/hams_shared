# Self-hosting: Odoo needs a buffering reverse proxy in front

Do not point browsers, a Cloudflare tunnel, or any other client path straight at Odoo's HTTP port (8069).
Put a buffering reverse proxy (nginx is what hams.com runs) in front of it.

## Why

Odoo's prefork HTTP workers set a socket timeout on every send (`ODOO_HTTP_SOCKET_TIMEOUT`, default 2
seconds). A client that reads more slowly than Odoo writes, which is every browser on a real network
behind a tunnel or CDN, stalls the send, and Odoo closes the connection. On hams.com a 35 MB download was
cut at 17 to 24 MB, and the browser's Resume was cut the same way. Odoo's own source says to use "a good
buffering reverse proxy". The proxy takes Odoo's reply at full speed, spills to disk past its memory
buffers, and hands it to the client at the client's pace. Uploads are buffered the same way, so a slow
upload does not hold a worker either. It also compresses text, which shrinks the large JavaScript bundle.

## Minimal nginx site

Listen on loopback only unless the proxy itself is the public listener. Odoo needs `proxy_mode = True` and
must see the real client address; behind another proxy (Cloudflare) pass `X-Forwarded-For` through
untouched, otherwise extend it with `$remote_addr`.

```nginx
upstream odoo_http    { server 127.0.0.1:8069; keepalive 16; }
upstream odoo_evented { server 127.0.0.1:8072; }   # live bus / websocket worker

map $http_upgrade $connection_upgrade { default upgrade; "" ""; }

# One year, immutable, only for a successful reply to a hashed bundle; a redirect or error must not be cached.
map $upstream_status $hashed_asset_cache_control {
    default "no-cache";
    200     "public, max-age=31536000, immutable";
    206     "public, max-age=31536000, immutable";
    304     "public, max-age=31536000, immutable";
}

server {
    listen 127.0.0.1:8085;

    # Default body limit; bigger uploads get their own location below.
    client_max_body_size 16m;
    # These two are the gap between reads, not the whole transfer: a slow upload that keeps moving passes,
    # a client that stops sending is dropped.
    client_header_timeout 20s;
    client_body_timeout 60s;
    send_timeout 300s;
    reset_timedout_connection on;

    proxy_http_version 1.1;
    proxy_read_timeout 720s;
    proxy_send_timeout 720s;
    proxy_redirect off;

    proxy_buffering on;
    proxy_buffer_size 16k;
    proxy_buffers 64 64k;
    proxy_busy_buffers_size 256k;
    proxy_max_temp_file_size 1024m;
    proxy_request_buffering on;

    # Locations that set no proxy_set_header of their own inherit these. One proxy_set_header inside a
    # location discards all of them, so repeat the whole set there (as /websocket does).
    proxy_set_header Host $http_host;
    proxy_set_header Connection "";
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;   # use $http_x_forwarded_for behind another proxy
    proxy_set_header X-Forwarded-Proto $scheme;

    # Text only; images, fonts (woff2), zips and installers are already compressed.
    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_min_length 1024;
    gzip_types text/plain text/css text/csv application/json application/javascript text/javascript
               application/manifest+json image/svg+xml application/wasm;

    location / {
        proxy_pass http://odoo_http;
    }

    # Odoo's back office (signed-in staff). Odoo's own file cap is 128 MB (web.max_file_upload_size);
    # base64 makes that about 171 MB on the wire.
    location ~ ^/(?:web/dataset/call_kw|web/binary/upload_attachment|mail/attachment/upload|base_import/set_file)(?:/|$) {
        client_max_body_size 192m;
        client_body_timeout 120s;
        proxy_pass http://odoo_http;
    }

    # Odoo's hashed bundles: /web/assets/<website id>/<hash>/<bundle>.min.js. The hash is in the URL, so the
    # bytes never change. The debug bundles ("debug" is not hex), HTML and API replies keep Odoo's headers.
    location ~ "^/web/assets/(?:[0-9]+/)?[0-9a-f]{7,64}/[^/]+\.(?:css|js)$" {
        proxy_pass http://odoo_http;
        proxy_hide_header Cache-Control;
        proxy_hide_header Expires;
        add_header Cache-Control $hashed_asset_cache_control;
    }

    location /websocket {
        proxy_pass http://odoo_evented;
        proxy_buffering off;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

hams.com's own file (`nginx/prod/hams-tunnel-origin.conf` in the private repository) adds a few narrower
upload locations for its modules: an 8m limit for classified-ad photos, 52m for the logbook's ADIF web upload,
160m for the relay build publish routes. Add a location with its own `client_max_body_size` for any route
of yours that takes bigger bodies than 16m. The limit is checked against `Content-Length` before the body
is read, so an oversized upload is refused with 413 at once; a chunked body is refused when it passes the
limit. A body past `client_body_buffer_size` is spooled to nginx's `client_temp` directory, so keep disk free
there. nginx cannot tell a signed-in client from an anonymous one, so each larger limit is a limit anyone can
use; put a rate limit in front if you see abuse.

Disable Debian's `sites-enabled/default` unless you want a public port 80, and keep Odoo itself bound to
loopback (`http_interface = 127.0.0.1` in `odoo.conf`). If you use the Cloudflare module's tunnel, set the
tunnel's "Catch-all Service" to this proxy (`http://localhost:8085`), not `http://localhost:8069`; the
module's default is still 8069, so change it on every tunnel you create.

`tools/provision.py` installs and enables this proxy on a production host (the `hams-tunnel-origin.conf`
entry in `tools/infrastructure.py`).
