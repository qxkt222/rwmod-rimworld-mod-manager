# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in rwmod, please report it privately
instead of opening a public issue, so it can be fixed before details are
widely known.

**How to report:**
- Open a GitHub issue on [qxkt222/rwmod-rimworld-mod-manager](https://github.com/qxkt222/rwmod-rimworld-mod-manager)
  and mark it with the `security` label, **or**
- Email the maintainer directly (see the GitHub profile for contact info).

Please include:
- A description of the vulnerability and its impact
- Steps to reproduce (the minimum setup needed)
- Affected versions / endpoints
- Any suggested fix, if you have one

## Scope

rwmod is a local-first desktop tool. By default it listens on localhost; when
the host firewall allows it, LAN clients can reach the web UI. The threat
model is: **a malicious LAN peer or a crafted uploaded file** (mod zip,
ModsConfig.xml, `.rwmod` bundle, modlist).

## What we care about

- Path traversal (zip extraction, backup/restore, config paths)
- Command injection via SteamCMD arguments or workshop IDs
- Arbitrary file read/write/deletion on the host
- SSRF via the Skymods fallback downloader
- Zip-bomb / decompression exhaustion
- Auth bypass on the JWT / WebSocket / login endpoints
- Secrets (Steam API key, `~/.rwmod.secret`) leaking into exports, logs or URLs

## Out of scope

- Public-internet deployment without HTTPS and reverse-proxy auth (not a
  supported configuration — the web UI is designed for localhost/LAN)
- Steam Workshop / SteamCMD / smods.ru ToS compliance

## Supported versions

| Version | Supported          |
|---------|--------------------|
| 0.5.x   | ✅                 |
| < 0.5   | ❌ (upgrade)       |
