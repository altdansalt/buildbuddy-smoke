"""Cacheable shell actions and real tests; no external rules or toolchains."""

def _smoke_copy_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run_shell(
        inputs = [ctx.file.src, ctx.file.nonce],
        outputs = [out],
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

def _smoke_large_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run_shell(
        inputs = [ctx.file.src, ctx.file.nonce],
        outputs = [out],
        # Harness writes deterministic partly compressible bytes before Bazel;
        # this remains a real, cacheable action with an explicit nonce input.
        command = '/bin/cat "$1" > "$2"',
        arguments = [ctx.file.src.path, out.path],
        mnemonic = "SmokeLarge",
        use_default_shell_env = False,
    )
    return [DefaultInfo(files = depset([out]))]

smoke_large = rule(
    implementation = _smoke_large_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "nonce": attr.label(allow_single_file = True, mandatory = True),
    },
)

def _smoke_receipt_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".txt")
    ctx.actions.run_shell(
        inputs = [ctx.file.src, ctx.file.nonce],
        outputs = [out],
        # Reading stdin keeps the receipt independent of output-root paths.
        command = '/usr/bin/sha256sum < "$1" > "$2"',
        arguments = [ctx.file.src.path, out.path],
        mnemonic = "SmokeReceipt",
        use_default_shell_env = False,
    )
    return [DefaultInfo(files = depset([out]))]

smoke_receipt = rule(
    implementation = _smoke_receipt_impl,
    attrs = {
        "src": attr.label(allow_single_file = True, mandatory = True),
        "nonce": attr.label(allow_single_file = True, mandatory = True),
    },
)

def _smoke_test_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".sh")
    # No sh_test/rules_shell, toolchain, interpreter lookup or external runfiles.
    ctx.actions.write(
        output = out,
        content = "#!/bin/bash\nprintf '%%s\\n' 'SMOKE_REAL_TEST %s %s'\nexit %d\n" % (
            ctx.label.name, ctx.attr.nonce, ctx.attr.exit_code,
        ),
        is_executable = True,
    )
    return [DefaultInfo(executable = out)]

smoke_test = rule(
    implementation = _smoke_test_impl,
    test = True,
    attrs = {"exit_code": attr.int(), "nonce": attr.string()},
)
