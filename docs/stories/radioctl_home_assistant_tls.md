# Story: radioctl Talks to a Home Assistant That Serves Only HTTPS

As a **station owner** whose Home Assistant (the home-automation server that switches the shack's power
and accessories) answers only over HTTPS, I want **radioctl (the relay's radio-control program) to connect to it with TLS (transport
encryption) using trust that I supply**, so that the connection is encrypted and the right server is
verified without trusting the public internet's certificate authorities.

## Scenario: Opening the connection

radioctl opens a TCP (standard reliable network) connection to the Home Assistant address. When TLS is configured the connection is
wrapped in TLS before any request is sent, and the server's certificate must match the address or the
owner's pin; if the connection or handshake fails, the attempt fails with a "connect" or "TLS" reason and
the caller retries with backoff [@ANCHOR: radioctl:radioctl_station_ha_open_stream].

## Scenario: Choosing how to trust the server

The owner sets exactly one of two things: a PEM file (a text format for certificates) with their own certificate authority, or the
SHA-256 fingerprint of the server's certificate (a "pin"). Setting both, or neither, is a configuration
error. Verification is never switched off and the system's public roots are never used, because Home
Assistant is on the owner's own network [@ANCHOR: radioctl:radioctl_station_tls_connector].

- With a certificate authority file, the file must hold at least one usable certificate.
- With a pin, exactly that certificate is accepted whatever its name or expiry.

## Scenario: Entering the fingerprint

The fingerprint is 64 hex digits in any case; colons and spaces are ignored, so a value copied from a
browser or `openssl` (a certificate command-line tool) is accepted as is. Anything else is a configuration error naming the 64-digit
SHA-256 rule [@ANCHOR: radioctl:radioctl_station_tls_parse_pin].

## Scenario: Which name the certificate must carry

Home Assistant is reached by IP address, so the certificate must carry that IP address as a subject alternative name entry; the
name is never taken from the configuration [@ANCHOR: radioctl:radioctl_station_tls_server_name].
