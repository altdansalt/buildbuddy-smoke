# Replace default WORKSPACE suffix initialization: Bazel 8 otherwise eagerly
# loads rules_cc/rules_java even when our rule uses no language toolchains.
# There are deliberately NO repositories, including no bazel_tools/platforms.
resolved = []
