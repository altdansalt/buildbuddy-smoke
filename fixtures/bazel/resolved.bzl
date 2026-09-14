# Replace default WORKSPACE initialization, which otherwise eagerly loads
# rules_cc/rules_java. These are ALREADY EXTRACTED parts of Bazel itself:
# its test runtime and the constraints used by that runtime's Windows select().
# No downloaded rules/platform/toolchains. The harness substitutes the absolute,
# setup-provisioned installation path before running Bazel.
resolved = [{"native": '''
local_repository(name = "bazel_tools", path = "SMOKE_INSTALL_BASE/embedded_tools")
local_repository(name = "platforms", path = "SMOKE_INSTALL_BASE/platforms")
'''}]
