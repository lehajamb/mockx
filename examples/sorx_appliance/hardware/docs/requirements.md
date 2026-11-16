# SORX MVP board — requirements derived from the reference software

Read-only sources (not cloned, built or run): the two example READMEs in `lehajamb/mockx` at `main`, read 2026-11-16.

- Feed: <https://github.com/lehajamb/mockx/tree/main/examples/cpp_market_feed>
- SOR: <https://github.com/lehajamb/mockx/tree/main/examples/cpp_sor>

## What the stack does on the wire

| Per venue (×2 in the SOR example) | Transport | Demo endpoint (venue A / B) | Board port |
|---|---|---|---|
| ITCH 5.0 over MoldUDP64 | UDP multicast, ≤1400-byte payload, TTL 1 | 239.192.10.1:18100 / 239.192.10.2:18200 | MD-IN |
| GLIMPSE 5.0 snapshot over SoupBinTCP | TCP | :18101 / :18201 | MD-IN |
| Order entry, FIX 4.4 (QuickFIX C++) | TCP | :4101 / :4102 | OE |
| MockX HTTP API (test harness only) | TCP | :8101 / :8102 | MGMT |

One MD-IN port carries both multicast groups and both GLIMPSE sessions; one OE port carries both FIX sessions. Venue A/B map to the FEED A/B and FIX A/B LEDs.

## What that asks of the hardware

| Software fact (README) | Hardware consequence |
|---|---|
| One UDP receive thread and one book thread per venue, joined by lock-free SPSC rings; a coordinator; one QuickFIX `ThreadedSocketInitiator` owning two sessions | About 5 latency-relevant threads plus 2 FIX session threads. With 2 isolated cores they must be pinned and shared; 4 cores is the floor, 6 gives one core per hot thread. |
| Datagrams are received straight into ring slots; overflow, truncation or a sequence gap invalidates the book and forces a new GLIMPSE cut (fail closed) | NIC must not drop under burst: hardware RX queues, IRQ affinity to the isolated cores, and RX hardware timestamps to measure it. No Mold retransmit is used, so packet loss costs a full snapshot. |
| Coordinator needs both feeds `LIVE` and both FIX sessions logged on | These four states drive FEED A/B LIVE and FIX A/B LOGON LEDs directly. |
| Prices are integer ITCH Price(4), quantities are integer lots, no floating point on the hot path | No FPU/NEON-driven SoC choice; integer single-thread speed and cache matter. |
| C++20, CMake, GCC or Clang; feed client has no dependencies; SOR needs an installed QuickFIX C++ | Any AArch64 Linux with GCC ≥ 11. Native builds on the board are possible but cross-compiling is expected. RAM need is small; 2 GB is ample. |
| QuickFIX keeps session state (sequence numbers, message store) and logs on disk | Durable storage separate from boot media: on-module eMMC for the journal and logs, microSD for boot. |
| Exit codes distinguish feed failure, FIX timeout, invariant failure, rejected child | ALARM LED and hardware watchdog give the same fail-closed signal at box level. |

## Requirements carried into the design

1. Three role-separated GbE ports (MD-IN, OE, MGMT), each with IEEE 1588 hardware timestamping.
2. PPS input disciplining the NIC clocks, so wire-in to wire-out latency can be measured on the box itself.
3. Linux with PREEMPT_RT, ≥ 4 cores (2 isolated), ≥ 2 GB RAM.
4. Boot from microSD; journal and logs on eMMC.
5. Management and console reachable when the forwarding plane is down, and console reachable with main power off.

No numeric latency target was given in the brief. This board is sized for deterministic, measurable latency on Cortex-A cores under PREEMPT_RT, not for the sub-10 µs target of the FPGA-NIC appliance.
