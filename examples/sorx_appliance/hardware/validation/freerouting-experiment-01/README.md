# Rejected routing experiment

Freerouting 1.9.0 timed out after 1,200 seconds before producing a session.
No board candidate was imported or promoted. Raw geometry warnings and exact
input/tool hashes are retained here. This is failed engineering evidence, not
a routing or release pass. The executed script snapshot is retained for review.

The live command ran from hardware/tools and the router also appended to its
historical default logs/freerouting.log. That incidental file was restored
byte-for-byte from the branch HEAD after the isolated container exited. Future
experiments must use a dedicated process working directory for all implicit logs.
