# RTK

RTK is an optional token-optimized CLI proxy for development commands. If available, verify the binary with `rtk --version` and `rtk gain`; the latter distinguishes Rust Token Killer from the unrelated Rust Type Kit.

Use RTK's meta commands directly: `rtk gain`, `rtk gain --history`, `rtk discover`. Use `rtk proxy <cmd>` for unfiltered output when debugging. Some agent environments rewrite commands through an RTK hook automatically; do not assume that a hook is installed in every environment.
