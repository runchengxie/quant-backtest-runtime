# Partition transport v7 proposal (inactive)

[中文页面](partition-transport-v7.zh-CN.md)

This is a design proposal, not an accepted job/engine request schema. The active
v6 session request still embeds its existing inputs and retains its existing size
limit. A `partition_manifest.v1` reference is not a valid v6 input extension.
No gateway, worker, CLI or capability currently advertises this proposal.

A future versioned engine consumer could accept a manifest digest plus explicitly
operator-approved input and task roots, validate financial payload schemas, and
consume [verified partition input v1](../partition-inputs-v1.md). Runtime would own
reference validation and bounded I/O. The financial engine would own matching,
accounting, information visibility and checkpoint compatibility. Job controls,
resource budgets, cancellation and publication verification must stay authoritative.

Activation requires a separately reviewed engine request version and job version,
capability negotiation, end-to-end bounded input/result channels, pinned engine
integration tests, and evidence that existing schemas and fingerprints are unchanged.
Passing file paths through unchecked subprocess arguments or raising the v6 transport
limit does not satisfy these requirements. The exact future wire shape remains open.

A checkpoint adapter must atomically bind its financial checkpoint version and
configuration identity to the input cursor's manifest digest and next-row position.
It must preserve pending reports and all financial state, reject incompatible state,
and demonstrate event, fill, fee, cash, position and final NAV equivalence across
partition boundaries, batch sizes and resumed execution. Input cursor correctness
alone supplies none of those financial guarantees. No production financial engine
checkpoint or streaming session transport is implemented by this proposal.
