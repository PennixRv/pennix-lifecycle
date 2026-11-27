#!/usr/bin/env bash
set -euo pipefail
umask 022

if [[ $# != 1 || $1 != /* || $1 == / || $(id -u) == 0 ]]; then
    printf '%s\n' 'usage (unprivileged): native/build.sh /absolute/new-staging-directory' >&2
    exit 2
fi
out=$1
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
mkdir -m 0700 -- "$out"
install -d -m 0755 -- "$out/usr/lib/pennix-lifecycle"
install -d -m 0700 -- "$out/var/lib/pennix-lifecycle/arch-transactions"
lib="$out/usr/lib/pennix-lifecycle"
flags=(-std=c17 -O2 -Wall -Wextra -Wpedantic -Werror -Wl,-z,relro,-z,now,--no-undefined)
cc "${flags[@]}" -fPIC -shared "$root/native/arch_transaction.c" -lalpm -o "$lib/arch-transaction.so"
cc "${flags[@]}" "$root/native/arch_transaction.c" "$root/native/arch_executor.c" -lalpm -o "$lib/arch-executor"
install -m 0755 -- "$root/native/arch_commit.py" "$lib/arch-commit"
install -m 0644 -- "$root/scripts/arch_policy.py" "$lib/arch_policy.py"
