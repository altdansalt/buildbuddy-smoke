"""A single cacheable action; no language toolchains or external repositories."""

def _smoke_copy_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run_shell(
        inputs = [ctx.file.src, ctx.file.nonce],
        outputs = [out],
        # nonce forces a cache miss on independent runs, but stays identical for
        # the two builds within a run. The actual output has fixed exact bytes.
        command = '/bin/cat "$1" > "$2"',
        arguments = [ctx.file.src.path, out.path],
        mnemonic = "SmokeCopy",
        use_default_shell_env = False,
    )
    return [DefaultInfo(files = depset([out]))]

smoke_copy = rule(
    implementation = _smoke_copy_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "nonce": attr.label(allow_single_file = True, mandatory = True),
    },
)
