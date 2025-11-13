# Host Security HOWTO

This guide explains how to enable HTTPS, mutual TLS (mTLS), and PAM-backed HTTP Basic authentication for the Firecracker CloudStack Agent.

## Relevant configuration keys

### `security.tls`

| Key | Required | Description |
| - | - | - |
| `enabled` | yes | Enables/disables HTTPS. When `false`, the agent listens via HTTP. |
| `cert_file` | yes (if HTTPS) | Server certificate in PEM format. |
| `key_file` | yes (if HTTPS) | Private key that matches `cert_file`. |
| `ca_file` | optional | CA bundle used to validate clients (needed for mTLS). |
| `client_auth` | optional | `none` (default), `optional`, or `required`. |

### `auth`

| Key | Required | Description |
| - | - | - |
| `enabled` | yes | When `true`, every `/v1/*` route requires Basic Auth. |
| `service` | yes (if enabled) | Name of the PAM stack under `/etc/pam.d/<service>`. |

---

## Generating TLS material

```bash
sudo install -d -m 0700 /etc/cloudstack/tls-cert
cd /etc/cloudstack/tls-cert

# Certificate Authority
sudo openssl req -x509 -nodes -newkey rsa:4096 -keyout ca.key -out ca.crt \
  -days 3650 -subj "/CN=Firecracker CA"

# Server certificate
HOST_FQDN=$(hostname -f)
HOST_IP=$(hostname -I | awk '{print $1}')
sudo openssl req -nodes -newkey rsa:4096 -keyout server.key -out server.csr \
  -subj "/CN=${HOST_FQDN}"
sudo openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 825 -sha256 -extensions v3_req \
  -extfile <(cat <<EOF
[v3_req]
subjectAltName=DNS:${HOST_FQDN},IP:${HOST_IP}
keyUsage=digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
EOF
)

sudo chown root:root *.crt *.key
sudo chmod 0640 server.key
```

Update `/etc/cloudstack/firecracker-agent.json`:

```json
"security": {
  "tls": {
    "enabled": true,
    "cert_file": "/etc/cloudstack/tls-cert/server.crt",
    "key_file": "/etc/cloudstack/tls-cert/server.key",
    "client_auth": "none"
  }
}
```

Restart the service: `sudo systemctl restart firecracker-cloudstack-agent`.

---

## Enabling mTLS

1. Issue a client certificate signed by the same CA:

```bash
sudo openssl req -nodes -newkey rsa:4096 -keyout client.key -out client.csr \
  -subj "/CN=cloudstack"
sudo openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days 825 -sha256 -extensions v3_req \
  -extfile <(cat <<EOF
[v3_req]
extendedKeyUsage=clientAuth
keyUsage=digitalSignature
subjectAltName=DNS:cloudstack
EOF
)
```

2. Configure the agent:

```json
"security": {
  "tls": {
    "enabled": true,
    "cert_file": "/etc/cloudstack/tls-cert/server.crt",
    "key_file": "/etc/cloudstack/tls-cert/server.key",
    "ca_file": "/etc/cloudstack/tls-cert/ca.crt",
    "client_auth": "required"
  }
}
```

3. Deploy `client.crt`, `client.key`, and `ca.crt` on the CloudStack Management Server. In the host payload/extension, provide:

```json
{
  "host_url": "https://firecracker-host.example.com",
  "host_port": 8443,
  "client_cert": "/etc/cloudstack/firecracker/client.crt",
  "client_key": "/etc/cloudstack/firecracker/client.key",
  "ca_bundle": "/etc/cloudstack/firecracker/ca.crt"
}
```

> **Lab / internal CA**: If the CloudStack management server cannot trust your lab certificate chain, set `skip_ssl_verification=true` in the host key/values (or pass `--skip-ssl-verification true` to `firecracker.py`). This makes the extension ignore TLS validation—use only with self-signed or internal certs.

---

## PAM (HTTP Basic) authentication

1. Ensure `python3-pamela` is installed (already a package dependency).

2. Create `/etc/pam.d/firecracker-agent` (or the name in `auth.service`). Example for local accounts:

```
auth     required pam_unix.so
account  required pam_unix.so
```

3. Configure the agent:

```json
"auth": {
  "enabled": true,
  "service": "firecracker-agent"
}
```

4. Users authenticate with system credentials (`/etc/shadow`). For LDAP/SSSD, adjust the PAM stack accordingly.

---

## Verification

1. **TLS**: `curl -vk https://HOST:8443/healthz`.
2. **mTLS**: `curl --cert client.crt --key client.key --cacert ca.crt https://HOST:8443/healthz`.
3. **PAM**: `curl -u user:pass https://HOST:8443/v1/host/summary`.

> **Self-signed certificates**: add `-k` (or `--insecure`) to the `curl` commands above when targeting hosts that use lab-only TLS material and aren’t trusted by your CA bundle.

If something fails, review `journalctl -u firecracker-cloudstack-agent -n 100` for messages such as “TLS enabled” or “Authentication enabled (PAM service=...)” plus handshake errors.

Following these steps secures the host with HTTPS, client certificates, and PAM-backed authentication.
