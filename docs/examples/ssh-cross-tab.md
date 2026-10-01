# Compare Two SSH Hosts With One AI Agent

[Back to StandTerm](../../README.md)

Use two existing SSH tabs to investigate why the same service behaves
differently on two hosts. The agent can inspect each authorized session and
compare its findings while you keep both terminals visible.

## Set Up

1. Connect to both hosts in StandTerm, using direct SSH or saved jump routes.
   Complete authentication and host-key checks yourself.
2. Connect your external agent using the [agent setup guide](../user-guide.md#agent-and-external-agent-mirror).
   For this example, run it as the same OS user and in the same environment as
   Core: WSL when Core runs in WSL. No AI runtime is needed on either SSH target.
3. In **Settings > General > Agent access**, select **Approval required + token**.
   Choose **Authorize agent** on each intended SSH tab. Each tab receives its
   own authorization; authorizing one does not grant access to the other.
4. Have the agent discover the current instance and verify each terminal with
   `hello`. When using the CLI, select each discovered terminal explicitly with
   `--terminal <id>`; the [connection reference](standterm-external-agent-skill/references/connection.md)
   covers discovery and per-terminal handoffs.

## Give the Agent a Task

Replace the host names below with the two connections you opened:

> Compare the service version, relevant configuration and recent error logs on
> host A and host B. Propose diagnostic commands for my approval, keep findings
> labeled by host, and report the differences before making any changes.

Review input proposals in the corresponding tab. The agent uses the existing
interactive sessions, so its commands and their output remain visible to you.
If you only want it to compare output already on screen, select **Read only**
instead; that mode cannot run diagnostic commands.

The useful result is a host-by-host comparison grounded in observed output, such
as a version mismatch or a differing configuration value. The agent must report
missing evidence rather than assume both hosts were checked successfully.

## Why Use Multiple Tabs?

You retain the authenticated sessions and decide which hosts the agent can
access. The agent can carry context between them without asking you to copy
terminal output into a chat or establish a second set of SSH connections.

This is targeted interaction with separately authorized terminals, not command
broadcast to every connected host. Approval applies to terminal input; it does
not prove that a proposed shell command is harmless. Pause or revoke access in
either tab when the investigation is complete.

For approved file movement between the hosts, see
[cross-tab file transfer](../user-guide.md#standterm-files). Agent file copies
require their own explicit approval.
