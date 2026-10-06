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

server {
    listen 127.0.0.1:8085;
    client_max_body_size 256m;
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

    gzip on;
    gzip_vary on;
    gzip_proxied any;
    gzip_types text/plain text/css application/json application/javascript text/javascript image/svg+xml;

    location / {
        proxy_pass http://odoo_http;
        proxy_set_header Host $http_host;
        proxy_set_header Connection "";
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;   # use $http_x_forwarded_for behind another proxy
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    location /websocket {
        proxy_pass http://odoo_evented;
        proxy_buffering off;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Disable Debian's `sites-enabled/default` unless you want a public port 80, and keep Odoo itself bound to
loopback (`http_interface = 127.0.0.1` in `odoo.conf`). If you use the Cloudflare module's tunnel, set the
tunnel's "Catch-all Service" to this proxy (`http://localhost:8085`), not `http://localhost:8069`; the
module's default is still 8069, so change it on every tunnel you create.

`tools/provision.py` installs and enables this proxy on a production host (the `hams-tunnel-origin.conf`
entry in `tools/infrastructure.py`).
