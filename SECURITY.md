# Security

Convrse Device Control operates across SSH and rooted ADB connections. Never
commit private keys, PEM files, device logs, diagnostic archives, customer
addresses, signing certificates, or Apple notarization credentials.

The repository ignores these files by default. Supply connection keys through
the application file picker and signing identities through the operating
system keychain/environment only.

If credentials are accidentally committed, revoke and replace them before
removing them from Git history. Deleting the visible file alone is not enough.
