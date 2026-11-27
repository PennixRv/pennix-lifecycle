#define _POSIX_C_SOURCE 200809L
#include "arch_transaction.h"

#include <fcntl.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void interrupt_transaction(void *context, alpm_event_t *event)
{
    (void)context;
    if (event->type == ALPM_EVENT_TRANSACTION_DONE)
        _exit(91);
}

static void report_error(void *context, alpm_loglevel_t level, const char *format, va_list args)
{
    (void)context;
    if (level & ALPM_LOG_ERROR)
        vfprintf(stderr, format, args);
}

static int by_name(const void *a, const void *b)
{
    return strcmp(((const struct arch_installed_package *)a)->name,
                  ((const struct arch_installed_package *)b)->name);
}

static bool reject_policy(void *context)
{
    return access(alpm_option_get_lockfile(context), F_OK) != 0;
}

/* Test runner only: paths and repository policy are not production inputs. */
int main(int argc, char **argv)
{
    if (argc < 14 || (argc - 8) % 6)
        return 3;
    const char *receipts = getenv("PENNIX_TEST_RECEIPT_DIR");
    if (!receipts)
        return 3;
    int dirfd = open(receipts, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (dirfd < 0)
        return 3;
    struct arch_transaction_receipt receipt = {
        dirfd, getenv("PENNIX_TEST_RECEIPT_ID"), getenv("PENNIX_TEST_REQUEST_DIGEST"),
    };
    if (!strcmp(argv[6], "observe")) {
        int result = arch_observe_receipt(&receipt);
        close(dirfd);
        return result;
    }
    alpm_errno_t error;
    alpm_handle_t *handle = alpm_initialize(argv[1], argv[2], &error);
    if (!handle) {
        close(dirfd);
        return 3;
    }

    int result = 3;
    alpm_handle_t *snapshot = NULL;
    struct arch_installed_package *installed = NULL;
    alpm_handle_t *competitor = NULL;
    bool competitor_locked = false;
    if (!strcmp(argv[6], "locked")) {
        competitor = alpm_initialize(argv[1], argv[2], &error);
        if (!competitor || alpm_trans_init(competitor, 0) != 0)
            goto done;
        competitor_locked = true;
    }
    int siglevel = ALPM_SIG_PACKAGE;
    if (!strcmp(argv[6], "optional"))
        siglevel |= ALPM_SIG_PACKAGE_OPTIONAL;
    if (!strcmp(argv[6], "interrupted") &&
        alpm_option_set_eventcb(handle, interrupt_transaction, NULL) != 0)
        goto done;
    const char *hooks = getenv("PENNIX_TEST_HOOK_DIR");
    if (hooks && alpm_option_add_hookdir(handle, hooks) != 0)
        goto done;
    alpm_db_t *repo = alpm_register_syncdb(handle, "extra", siglevel);
    if (alpm_option_add_cachedir(handle, argv[3]) != 0 ||
        alpm_option_set_gpgdir(handle, argv[4]) != 0 ||
        alpm_option_set_logfile(handle, argv[5]) != 0 ||
        alpm_option_set_logcb(handle, report_error, NULL) != 0 ||
        !repo || alpm_db_add_server(repo, "file:///nonexistent-pennix-fixture") != 0)
        goto done;

    size_t count = (size_t)(argc - 8) / 6;
    struct arch_reviewed_package expected[8];
    if (count > sizeof expected / sizeof *expected)
        goto done;
    for (size_t i = 0; i < count; ++i) {
        char **row = argv + 8 + i * 6;
        expected[i] = (struct arch_reviewed_package){
            *row[0] ? row[0] : NULL, row[1], *row[2] ? row[2] : NULL, row[3], row[4],
            *row[5] ? row[5] : NULL,
        };
    }
    const char *targets[] = {argv[7]};
    snapshot = alpm_initialize(argv[1], argv[2], &error);
    if (!snapshot)
        goto done;
    const alpm_list_t *baseline = alpm_db_get_pkgcache(alpm_get_localdb(snapshot));
    size_t installed_count = alpm_list_count(baseline);
    if (installed_count) {
        installed = calloc(installed_count, sizeof(*installed));
        if (!installed)
            goto done;
        size_t n = 0;
        for (const alpm_list_t *item = baseline; item; item = item->next) {
            installed[n++] = (struct arch_installed_package){
                alpm_pkg_get_name(item->data), alpm_pkg_get_version(item->data),
            };
        }
        qsort(installed, installed_count, sizeof(*installed), by_name);
        if (!strcmp(argv[6], "stale-baseline"))
            installed[0].version = "stale";
    }
    result = arch_commit_reviewed(handle, targets, *argv[7] ? 1 : 0, expected, count,
                                 installed, installed_count,
                                 getenv("PENNIX_TEST_LOCAL_SIGNER"), &receipt,
                                 !strcmp(argv[6], "policy-drift") ? reject_policy : NULL, handle);

done:
    free(installed);
    if (snapshot)
        alpm_release(snapshot);
    if (competitor_locked && alpm_trans_release(competitor) != 0)
        result = 3;
    if (competitor)
        alpm_release(competitor);
    if (alpm_release(handle) != 0)
        result = 3;
    close(dirfd);
    return result;
}
