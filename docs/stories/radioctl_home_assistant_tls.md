# Story: radioctl Talks to a Home Assistant That Serves Only HTTPS

As a **station owner** whose Home Assistant (the home-automation server that switches the shack's power
and accessories) answers only over HTTPS, I want **radioctl (the relay's radio-control program) to connect to it with TLS (transport
encryption) using trust that I supply**, so that the connection is encrypted and the right server is
verified without trusting the public internet's certificate authorities.

## Scenario: Opening the connection

radioctl opens a TCP (standard reliable network) connection to the Home Assistant address. When TLS is configured the connection is
wrapped in TLS before any request is sent, and the server's certificate must match the address or the
owner's pin; if the connection or handshake fails, the attempt fails with a "connect" or "TLS" reason and
the caller retries with backoff (function `radioctl_station_ha_open_stream`, described in the anchor reference below).

## Scenario: Choosing how to trust the server

The owner sets exactly one of two things: a PEM file (a text format for certificates) with their own certificate authority, or the
SHA-256 fingerprint of the server's certificate (a "pin"). Setting both, or neither, is a configuration
error. Verification is never switched off and the system's public roots are never used, because Home
Assistant is on the owner's own network (function `radioctl_station_tls_connector`, described in the anchor reference below).

- With a certificate authority file, the file must hold at least one usable certificate.
- With a pin, exactly that certificate is accepted whatever its name or expiry.

## Scenario: Entering the fingerprint

The fingerprint is 64 hex digits in any case; colons and spaces are ignored, so a value copied from a
browser or `openssl` (a certificate command-line tool) is accepted as is. Anything else is a configuration error naming the 64-digit
SHA-256 rule (function `radioctl_station_tls_parse_pin`, described in the anchor reference below).

## Scenario: Which name the certificate must carry

Home Assistant is reached by IP address, so the certificate must carry that IP address as a subject alternative name entry; the
name is never taken from the configuration (function `radioctl_station_tls_server_name`, described in the anchor reference below).

## Anchor reference

Each entry below describes one anchored function in full. The code is in `daemons/radioctl/crates/radioctl-station/src/tls.rs` and `homeassistant.rs`, and the tests that cover them are named in the see-also lines.

### `radioctl_station_ha_open_stream` [@ANCHOR: radioctl:radioctl_station_ha_open_stream]

**Purpose.** Opens the byte stream radioctl's Home Assistant driver speaks over: a TCP connection, wrapped in TLS when the owner configured TLS. It is called each time the driver (re)connects, so a lost connection is retried by the caller with backoff.

**Signature.** `async fn open_stream(addr: SocketAddr, tls: Option<&TlsConnector>) -> Result<Box<dyn Io>, String>`

| Argument | Type | Meaning |
|---|---|---|
| `addr` | `SocketAddr` | Home Assistant's IP address and port. The IP is also the name the certificate must carry. |
| `tls` | `Option<&TlsConnector>` | `None` gives plain TCP; `Some` runs the TLS handshake before any request is sent. Borrowed, not retained. |

**Returns.** A boxed stream usable for reading and writing (`Io`: read, write, unpin, send). Errors are strings that start with `connect:` (TCP failed) or `TLS:` (handshake or certificate check failed), so the caller can log which stage failed.

**Special concerns.** Asynchronous; it awaits the network and holds no lock. It sets TCP no-delay (ignoring failure to do so) to keep small request frames from being delayed. It touches only the network and never keys a transmitter. Nothing is cached, so each call opens a new connection.

**See also.** Tests in `tests/homeassistant.rs`; `radioctl_station_tls_connector`, `radioctl_station_tls_server_name`.

### `radioctl_station_tls_connector` [@ANCHOR: radioctl:radioctl_station_tls_connector]

**Purpose.** Builds the TLS client configuration for a Home Assistant that serves only HTTPS, using only trust the owner supplied. The public certificate authorities are never consulted and verification is never turned off.

**Signature.** `pub fn connector(tls: &HaTls) -> Result<TlsConnector, StationError>`

| Argument | Type | Meaning |
|---|---|---|
| `tls` | `&HaTls` | Exactly one of `ca_pem` (PEM text of the owner's certificate authority, or of the server's own self-signed certificate) and `pin_sha256` (a fingerprint, see `radioctl_station_tls_parse_pin`). Borrowed. |

**Returns.** A `TlsConnector` that uses the safe default protocol versions and the `ring` crypto provider. With a CA the connector verifies the chain and the name against those roots; with a pin it accepts exactly the certificate whose SHA-256 matches (any name, any expiry) but still checks the handshake signature, so the peer must hold the private key.

**Errors.** All are `StationErrorKind::Config`: both or neither of CA and pin set; the CA text is not PEM certificates; a certificate is not usable as a root; the CA text holds no certificate; the pin is malformed; TLS setup failed.

**Special concerns.** Synchronous and pure (no network, no files). It is the one place that installs a custom certificate verifier, which is why it accepts only the two shapes above.

**See also.** `HaTls`, `radioctl_station_tls_parse_pin`, `radioctl_station_ha_open_stream`; unit tests in `tls.rs` and `tests/homeassistant.rs`.

### `radioctl_station_tls_parse_pin` [@ANCHOR: radioctl:radioctl_station_tls_parse_pin]

**Purpose.** Turns the fingerprint an owner typed or pasted into the 32 raw bytes the pin verifier compares against. A value copied from a browser or from `openssl x509 -fingerprint` works as is.

**Signature.** `pub fn parse_pin(text: &str) -> Result<[u8; 32], StationError>`

| Argument | Type | Meaning |
|---|---|---|
| `text` | `&str` | 64 hexadecimal digits in either case. Colons and any whitespace are removed first. |

**Returns.** The 32 bytes of the SHA-256 digest, in order.

**Errors.** `StationErrorKind::Config` ("the certificate fingerprint must be 64 hex digits (SHA-256)") when, after removing colons and whitespace, the length is not 64 or a character is not a hex digit.

**Special concerns.** Pure and synchronous, no side effects. The fingerprint is public data, not a secret.

**See also.** `radioctl_station_tls_connector`; unit test in `tls.rs`.

### `radioctl_station_tls_server_name` [@ANCHOR: radioctl:radioctl_station_tls_server_name]

**Purpose.** Supplies the name the server's certificate must carry. Home Assistant is reached by IP address, so the name is that address as an IP entry (a subject alternative name of type IP); it is never taken from configuration, which prevents a configured name from steering verification.

**Signature.** `pub fn server_name(ip: IpAddr) -> ServerName<'static>`

| Argument | Type | Meaning |
|---|---|---|
| `ip` | `IpAddr` | The address radioctl connected to, IPv4 or IPv6. |

**Returns.** `ServerName::IpAddress` holding that address. It cannot fail.

**Special concerns.** Pure. It has no effect under a pinned certificate, whose verifier ignores the name; it matters for the certificate-authority mode.

**See also.** `radioctl_station_ha_open_stream` (the caller); unit test in `tls.rs`.
