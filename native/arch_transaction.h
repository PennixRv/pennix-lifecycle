#ifndef PENNIX_ARCH_TRANSACTION_H
#define PENNIX_ARCH_TRANSACTION_H

#include <alpm.h>
#include <stdbool.h>
#include <stddef.h>

struct arch_reviewed_package {
    const char *repo; /* NULL for a separately approved local package. */
    const char *name;
    const char *installed; /* NULL means not installed. */
    const char *version;
    const char *sha256;
    const char *filename; /* Required basename for a local package. */
};

struct arch_installed_package {
    const char *name;
    const char *version;
};

enum arch_transaction_result {
    ARCH_COMMITTED,
    ARCH_REFUSED,
    ARCH_OUTCOME_UNKNOWN,
};

struct arch_transaction_receipt {
    int dirfd; /* Borrowed descriptor for the executor's own receipt directory. */
    const char *id; /* 32 lowercase hex digits, unique to the Lifecycle step. */
    const char *request_sha256; /* Digest of the complete authenticated request. */
};

/* Reports native commit evidence only; callers still check live postconditions. */
enum arch_transaction_result arch_observe_receipt(const struct arch_transaction_receipt *receipt);

/*
 * Internal API, not a privileged entry point. The caller owns the handle and
 * must set it up from trusted native configuration, with snapshots of the
 * reviewed sync databases and exactly one private staging cache. Use a fresh
 * handle with the real target's package database lock; do not preload local
 * package data. The cache and its ancestors must not be replaceable by an
 * untrusted writer. Each package and detached signature must already be
 * present. The caller must separately authorize local artifacts and supply
 * their enrolled signer fingerprint from trusted policy, never the review
 * request. NULL disables local artifacts. This function does not import trust.
 * The executor, not the unprivileged request, opens the receipt directory and
 * derives the receipt identity from the authenticated request and step.
 * installed is the complete reviewed baseline sorted by package name;
 * do not derive it from the live database during execution.
 * When supplied, policy_current rechecks the executor's native policy with the
 * package lock held, immediately before the first possible write.
 * It owns the native transaction and borrows the inputs for the call.
 */
enum arch_transaction_result arch_commit_reviewed(
    alpm_handle_t *handle, const char *const *targets, size_t target_count,
    const struct arch_reviewed_package *expected, size_t expected_count,
    const struct arch_installed_package *installed, size_t installed_count,
    const char *local_signer, const struct arch_transaction_receipt *receipt,
    bool (*policy_current)(void *), void *policy_context);

#endif
