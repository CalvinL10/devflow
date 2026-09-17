# Security policy

## Supported scope

DevFlow is a local, single-user demonstration system. It has no authentication,
authorization, tenant isolation, or hardened internet-facing deployment profile. Do not
expose the API, frontend, dispatcher socket, database, or managed workspace to untrusted
networks or users.

The Compose dispatcher has Docker-daemon access and is trusted infrastructure. Candidate
containers use defense-in-depth restrictions, but those controls are not a complete security
sandbox and do not prove resistance to container or kernel escape.

## Reporting a vulnerability

Please report security issues privately through GitHub's **Report a vulnerability** feature
when it is enabled for the repository. If private reporting is unavailable, open a minimal
issue asking the maintainer for a private contact channel; do not include exploit details,
secrets, or personal data in a public issue.

Include the affected commit, prerequisites, impact, reproduction steps, and any suggested
mitigation. Security fixes must not weaken existing input validation, transport assumptions,
data isolation, or container restrictions.
