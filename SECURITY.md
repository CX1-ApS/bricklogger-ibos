# Security policy

The iBOS source holds a token to a building's data in the cloud, so a
vulnerability is taken seriously even where it seems small. The source only
reads; the iBOS Data API it uses is read-only.

## Reporting a vulnerability

Report it **privately**, through GitHub's
[private vulnerability reporting](https://github.com/CX1-ApS/bricklogger-ibos/security/advisories/new)
on this repository, and not in a public issue. Say what is affected, how it
can be reproduced, and which version you ran. You will get an answer within a
few working days, and a fix is released as a new patch version with the
report credited unless you ask otherwise.

## Supported versions

Fixes are made for the newest version. It is installed with
`bricklogger plugins add bricklogger-ibos`, which also upgrades it.
