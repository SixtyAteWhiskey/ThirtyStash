# Security

## Supported deployment model

ThirtyStash currently has **no user authentication**. It is designed for a
trusted LAN, private VPN, or a reverse proxy that provides authentication.
Do not expose port 3055 directly to the public Internet.

State-changing requests are protected with CSRF tokens, but CSRF protection is
not a substitute for authentication or network access control.

## Reporting a vulnerability

Please avoid publishing exploit details in a public issue before a fix is
available. Use GitHub's private vulnerability reporting feature if it is
enabled for the repository, or contact the repository maintainer privately.

Do not include real inventory, prescription, household, backup, or other
sensitive personal data in a vulnerability report.
